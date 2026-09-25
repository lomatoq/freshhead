"""Local, versioned FashionCLIP image index and conservative visual attributes.

Weights are downloaded only in the explicitly started worker. The HTTP server
never imports torch. Taste retrieval is not a trained outfit-compatibility model.
"""
from __future__ import annotations

import io
import ipaddress
import json
import math
import os
import socket
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urljoin, urlsplit

import httpx

MODEL = 'patrickjohncyh/fashion-clip'
REVISION = '7e3ba62ce16b379a1ab479346b66f192e76f51b7'
PROMPT_VERSION = 2
DIMENSION = 512

# Labels are hypotheses, not measurements or calibrated probabilities.
LABELS = {
    'category': {
        'top': 'a product photo of a shirt, t-shirt, hoodie or sweater',
        'bottom': 'a product photo of trousers, pants or jeans',
        'footwear': 'a product photo of shoes, sneakers or boots',
        'outerwear': 'a product photo of a jacket or coat',
        'accessory': 'a product photo of a bag, belt, cap or necklace',
    },
    'colors': {c: f'a {c} colored fashion product' for c in
               ('black', 'white', 'cream', 'grey', 'blue', 'brown', 'beige',
                'olive', 'green', 'red', 'pink', 'yellow', 'orange', 'purple')},
    'top_fit': {'cropped': 'a cropped short length boxy top',
                'oversized': 'an oversized loose fitting top',
                'regular': 'a regular fit top', 'slim': 'a slim fitted top'},
    'bottom_fit': {'wide': 'wide leg baggy loose trousers',
                   'regular': 'regular straight leg trousers', 'slim': 'skinny slim fit trousers'},
    'shoe_mass': {'light': 'slim low profile flat sole shoes',
                  'medium': 'regular sneakers with a medium sole',
                  'heavy': 'bulky chunky platform shoes or heavy boots'},
    'pattern': {'plain': 'a plain solid color garment without a print',
                'statement': 'a garment with a large graphic print or striking pattern'},
    'material': {'denim': 'a garment made of denim fabric', 'knit': 'a knitted wool garment',
                 'leather': 'a smooth leather fashion product', 'suede': 'a suede fashion product',
                 'canvas': 'a heavy canvas cotton garment', 'nylon': 'a technical nylon garment',
                 'cotton': 'a plain cotton jersey garment'},
}


def safe_image_host(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or not p.hostname:
        raise ValueError('Only public HTTPS images are allowed')
    addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Local/private image address rejected')


def valid_vector(vector):
    return (isinstance(vector, list) and len(vector) == DIMENSION
            and all(isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x) for x in vector)
            and sum(x*x for x in vector) > .01)


def load_records(db):
    with db.connect() as c:
        rows = c.execute('''SELECT f.id,f.vector,f.attributes FROM image_features f
            JOIN products p ON p.id=f.id WHERE f.image=json_extract(p.body,'$.image')
            AND f.model=? AND f.revision=? AND f.prompt_version=?''',
                         (MODEL, REVISION, PROMPT_VERSION)).fetchall()
    result = {}
    for row in rows:
        try:
            vector, attributes = json.loads(row['vector']), json.loads(row['attributes'])
            if valid_vector(vector) and isinstance(attributes, dict):
                result[row['id']] = {'vector': vector, 'attributes': attributes}
        except (ValueError, TypeError):
            continue
    return result


def load_index(db):
    if not db.settings().ai_enabled:
        return {}
    return {pid: r['vector'] for pid, r in load_records(db).items()}


def apply_attributes(product, attributes):
    """Never overwrite explicit store text with a zero-shot guess."""
    p = product.model_copy(deep=True)
    p.visual_attributes = attributes
    used = []
    for field in ('category', 'fit', 'shoe_mass', 'pattern', 'material'):
        pred = attributes.get(field, {})
        if isinstance(pred, dict) and pred.get('accepted') and getattr(p, field) in ('', 'unknown'):
            value = pred.get('label')
            if field == 'category' and value not in LABELS['category']:
                continue
            if field == 'fit' and value not in set(LABELS['top_fit']) | set(LABELS['bottom_fit']):
                continue
            if field == 'shoe_mass' and value not in LABELS['shoe_mass']:
                continue
            if field == 'pattern' and value not in LABELS['pattern']:
                continue
            if field == 'material' and value not in LABELS['material']:
                continue
            setattr(p, field, value)
            used.append(field)
    color = attributes.get('colors', {})
    if not p.colors and isinstance(color, dict) and color.get('accepted') and color.get('label') in LABELS['colors']:
        p.colors = [color['label']]
        used.append('colors')
    if 'material' in used and p.material in ('denim', 'knit', 'leather', 'suede'):
        p.tags = sorted(set(p.tags) | {p.material})
    if used:
        p.attributes_source += '; AI guesses (not verified): ' + ', '.join(used)
    return p


def vision_products(db, records=None):
    products = db.products()
    if not db.settings().ai_enabled:
        return products
    records = load_records(db) if records is None else records
    return [apply_attributes(p, records[p.id]['attributes']) if p.id in records else p for p in products]


def eligible_products(db):
    enabled = set(db.settings().enabled_stores)
    return [p for p in db.products() if not p.demo and p.image and p.store in enabled
            and p.available is not False and p.gender != 'kids' and p.category != 'accessory']


def select_pending(db, records, limit):
    ratings = db.ratings()
    pending = [p for p in eligible_products(db) if p.id not in records]
    priority = [p for p in pending if ratings.get(p.id) in (-1, 1)]
    buckets = defaultdict(deque)
    for p in pending:
        if ratings.get(p.id) not in (-1, 1):
            buckets[(p.store, p.category)].append(p)
    result = priority[:limit]
    keys = sorted(buckets)
    while len(result) < limit and any(buckets.values()):
        for key in keys:
            if buckets[key] and len(result) < limit:
                result.append(buckets[key].popleft())
    return result


class ImageDownloader:
    """One request stream per origin, including robots, with bounded public downloads."""
    def __init__(self):
        self._guard = threading.Lock()
        self._locks = {}
        self._robots = {}
        self._last = {}

    def _lock(self, host):
        with self._guard:
            return self._locks.setdefault(host, threading.RLock())

    @staticmethod
    def _body(client, url, maximum):
        safe_image_host(url)
        with client.stream('GET', url) as response:
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > maximum:
                    raise ValueError('Response exceeds the size limit')
            return response.status_code, dict(response.headers), bytes(data)

    def _robot_rules(self, client, host):
        from .catalog import Robots
        if host in self._robots:
            rules = self._robots[host]
            if rules is None:
                raise ValueError('Image origin robots.txt unavailable')
            return rules
        self._robots[host] = None
        url = f'https://{host}/robots.txt'
        # Validate redirects; an origin change is not silently trusted for robots.
        for _ in range(4):
            status, headers, body = self._body(client, url, 1_000_000)
            if status in (301, 302, 303, 307, 308):
                target = urljoin(url, headers.get('location', ''))
                if urlsplit(target).netloc != host:
                    raise ValueError('Cross-origin image robots redirect')
                url = target
                continue
            text = body.decode('utf-8', errors='replace').lstrip()
            if status == 404:
                rules = Robots('User-agent: *\nAllow: /')
            elif 200 <= status < 300 and not text.startswith('<'):
                rules = Robots(text)
            else:
                raise ValueError('Image origin robots.txt unavailable')
            self._robots[host] = rules
            self._last[host] = time.monotonic()
            return rules
        raise ValueError('Image robots redirect limit')

    def fetch(self, url):
        with httpx.Client(timeout=20, follow_redirects=False, headers={
                'User-Agent': 'Freshhead/0.2 image-indexer (+https://github.com/lomatoq/freshhead)'}) as client:
            for _ in range(4):
                safe_image_host(url)
                host = urlsplit(url).netloc
                with self._lock(host):
                    rules = self._robot_rules(client, host)
                    if not rules.allowed(url):
                        raise ValueError('Image path disallowed by robots.txt')
                    time.sleep(max(0, rules.delay - (time.monotonic() - self._last.get(host, 0))))
                    self._last[host] = time.monotonic()
                    status, headers, body = self._body(client, url, 10_000_000)
                if status in (301, 302, 303, 307, 308):
                    url = urljoin(url, headers.get('location', ''))
                    continue
                if not 200 <= status < 300:
                    raise ValueError(f'Image HTTP {status}')
                if not headers.get('content-type', '').startswith('image/'):
                    raise ValueError('Response is not an image')
                return body
        raise ValueError('Image redirect limit')


class FashionEncoder:
    def __init__(self):
        import torch
        from transformers import CLIPModel, CLIPProcessor
        self.torch = torch
        self.device = 'cuda' if torch.cuda.is_available() else ('mps' if torch.backends.mps.is_available() else 'cpu')
        self.device_name = torch.cuda.get_device_name(0) if self.device == 'cuda' else self.device
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        self.model = CLIPModel.from_pretrained(MODEL, revision=REVISION,
            trust_remote_code=False, use_safetensors=True).to(self.device).eval()
        self.processor = CLIPProcessor.from_pretrained(MODEL, revision=REVISION, trust_remote_code=False, use_fast=False)
        prompts = [text for values in LABELS.values() for text in values.values()]
        with torch.inference_mode():
            inputs = self.processor(text=prompts, return_tensors='pt', padding=True, truncation=True, max_length=77).to(self.device)
            vectors = self.model.get_text_features(**inputs)
            self.text_vectors = vectors / vectors.norm(dim=-1, keepdim=True)

    def encode(self, images, products):
        torch = self.torch
        with torch.inference_mode():
            inputs = self.processor(images=images, return_tensors='pt').to(self.device)
            vectors = self.model.get_image_features(**inputs)
            vectors = vectors / vectors.norm(dim=-1, keepdim=True)
            similarities = (vectors @ self.text_vectors.T).cpu().tolist()
        result = []
        for vector, sims, product in zip(vectors.cpu().tolist(), similarities, products):
            attrs, offset = {}, 0
            for group, labels in LABELS.items():
                values = sims[offset:offset+len(labels)]
                order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
                top, second = order[:2]
                margin = values[top] - values[second]
                attrs[group] = {'label': list(labels)[top], 'similarity': round(values[top], 4),
                                'margin': round(margin, 4), 'accepted': margin >= .025}
                offset += len(labels)
            cat = product.category
            if cat == 'unknown' and attrs['category']['accepted']:
                cat = attrs['category']['label']
            if cat in ('top', 'outerwear'):
                attrs['fit'] = attrs['top_fit']
            elif cat == 'bottom':
                attrs['fit'] = attrs['bottom_fit']
            if cat != 'footwear':
                attrs.pop('shoe_mass', None)
            else:
                attrs.pop('pattern', None)
            attrs.pop('top_fit', None)
            attrs.pop('bottom_fit', None)
            attrs['_model'] = MODEL
            attrs['_note'] = 'Zero-shot hypotheses; similarity/margin are not calibrated probabilities.'
            result.append((vector, attrs))
        return result


def index_images(db, limit=192, encoder_factory=None, downloader=None):
    from filelock import FileLock, Timeout
    from PIL import Image, ImageOps
    from .models import now
    Image.MAX_IMAGE_PIXELS = 20_000_000
    lock = FileLock(str(db.path.parent / 'vision.lock'))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise RuntimeError('AI indexing is already running')
    state = {'running': True, 'phase': 'loading', 'indexed': 0, 'failed': 0, 'done': 0,
             'total': 0, 'started': now(), 'model': MODEL, 'revision': REVISION,
             'pid': os.getpid(), 'message': 'Загрузка локальной модели; первый запуск скачивает веса', 'errors': []}
    def progress(**changes):
        state.update(changes, updated=now())
        db.set('ai_job', state)
    try:
        pending = select_pending(db, load_records(db), max(1, min(int(limit), 1000)))
        progress(total=len(pending))
        if not pending:
            progress(running=False, phase='completed', message='Новых фото для анализа нет')
            return 0
        encoder = (encoder_factory or FashionEncoder)()
        fetcher = downloader or ImageDownloader()
        progress(phase='indexing', device=encoder.device, device_name=encoder.device_name,
                 message='Анализ фото: категории, цвет, крой и визуальные сходства')
        def fetch(p):
            try:
                raw = fetcher.fetch(p.image)
                with Image.open(io.BytesIO(raw)) as src:
                    if min(src.size) < 32:
                        raise ValueError('Placeholder or tiny image: minimum dimension is 32 pixels')
                    image = ImageOps.exif_transpose(src).convert('RGB')
                    # Thumbnails reduce memory, while CLIP still applies its own preprocess.
                    image.thumbnail((1024, 1024))
                    return p, image, None
            except Exception as e:
                return p, None, str(e)[:150] if isinstance(e, ValueError) else type(e).__name__
        with ThreadPoolExecutor(max_workers=4) as pool:
            # Process small windows; do not retain the full catalog of decoded images.
            for start in range(0, len(pending), 12):
                fetched = list(pool.map(fetch, pending[start:start+12]))
                valid = [(p, im) for p, im, error in fetched if im is not None]
                for p, _, error in fetched:
                    if error:
                        state['failed'] += 1
                        state['errors'] = (state['errors'] + [{'id': p.id, 'store': p.store, 'reason': error}])[-25:]
                if valid:
                    encoded = encoder.encode([im for _, im in valid], [p for p, _ in valid])
                    with db.connect() as c:
                        for (p, _), (vector, attributes) in zip(valid, encoded):
                            if not valid_vector(vector):
                                raise ValueError('Invalid embedding returned by the model')
                            c.execute('''INSERT OR REPLACE INTO image_features
                                VALUES(?,?,?,?,?,?,?,?)''', (p.id, p.image, MODEL, REVISION, PROMPT_VERSION,
                                json.dumps(vector), json.dumps(attributes, ensure_ascii=False), now()))
                            state['indexed'] += 1
                for _, im in valid:
                    im.close()
                progress(done=min(start+12, len(pending)))
                print(f"AI {state['done']}/{state['total']}: indexed={state['indexed']}, failed={state['failed']}", flush=True)
        phase = 'completed' if state['indexed'] else 'empty'
        progress(running=False, phase=phase, message=f"Проанализировано {state['indexed']} фото; пропущено {state['failed']}")
        return state['indexed']
    except Exception as e:
        progress(running=False, phase='error', message=f'{type(e).__name__}: анализ не завершён. Подробности в data/ai-worker.log')
        raise
    finally:
        lock.release()
