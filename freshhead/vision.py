"""Optional FashionCLIP embedding index. Nothing downloads during server startup.

Run `python -m freshhead index-images` after installing requirements-vision.txt.
Only images attached to stored products are fetched; arbitrary URL requests and
local addresses are rejected. Redirects are validated at each hop.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

MODEL = 'patrickjohncyh/fashion-clip'


def safe_image_host(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or not p.hostname:
        raise ValueError('Only public HTTPS images are allowed')
    addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Local/private image address rejected')


def load_index(db):
    path = db.path.parent / 'embeddings.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text('utf-8'))
        if data.get('model') != MODEL:
            return {}
        # Invalidate embeddings when the source image URL changes.
        return {p.id: data['items'][p.id]['vector'] for p in db.products()
                if p.id in data.get('items', {}) and data['items'][p.id].get('image') == p.image}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def index_images(db, limit=200):
    import numpy as np
    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor
    from .catalog import Robots
    Image.MAX_IMAGE_PIXELS = 20_000_000
    path = db.path.parent / 'embeddings.json'
    data = json.loads(path.read_text('utf-8')) if path.exists() else {'model': MODEL, 'items': {}}
    if data.get('model') != MODEL:
        data = {'model': MODEL, 'items': {}}
    device = 'cuda' if torch.cuda.is_available() else ('mps' if torch.backends.mps.is_available() else 'cpu')
    print(f'Loading {MODEL} on {device}; first run downloads model weights.', flush=True)
    model = CLIPModel.from_pretrained(MODEL, trust_remote_code=False, use_safetensors=True).to(device).eval()
    processor = CLIPProcessor.from_pretrained(MODEL, trust_remote_code=False)
    count = 0
    robots_cache = {}
    last = {}
    with httpx.Client(timeout=25, follow_redirects=False, headers={'User-Agent': 'Freshhead/0.1 image-indexer'}) as client:
        for p in db.products():
            if p.demo or not p.image or data['items'].get(p.id, {}).get('image') == p.image:
                continue
            if count >= limit:
                break
            try:
                url = p.image
                for _ in range(4):
                    safe_image_host(url)
                    host = urlsplit(url).hostname
                    if host not in robots_cache:
                        rr = client.get(f'https://{host}/robots.txt')
                        if rr.status_code == 404:
                            robots_cache[host] = Robots('User-agent: *\nAllow: /')
                        elif rr.is_success and not rr.text.lstrip().startswith('<'):
                            robots_cache[host] = Robots(rr.text)
                        else:
                            raise ValueError('Image host robots.txt could not be verified')
                    rb = robots_cache[host]
                    if not rb.allowed(url):
                        raise ValueError('Image path disallowed by robots.txt')
                    time.sleep(max(0, rb.delay - (time.monotonic() - last.get(host, 0))))
                    last[host] = time.monotonic()
                    with client.stream('GET', url) as r:
                        if r.status_code in (301, 302, 303, 307, 308):
                            url = urljoin(url, r.headers.get('location', ''))
                            continue
                        r.raise_for_status()
                        if not r.headers.get('content-type', '').startswith('image/'):
                            raise ValueError('Not an image response')
                        raw = bytearray()
                        for chunk in r.iter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 10_000_000:
                                raise ValueError('Image too large')
                    break
                else:
                    raise ValueError('Too many image redirects')
                image = Image.open(io.BytesIO(raw)).convert('RGB')
                inputs = processor(images=image, return_tensors='pt').to(device)
                with torch.inference_mode():
                    vector = model.get_image_features(**inputs)
                    vector = vector / vector.norm(dim=-1, keepdim=True)
                data['items'][p.id] = {'image': p.image, 'vector': vector[0].cpu().tolist()}
                count += 1
                tmp = path.with_suffix('.tmp')
                tmp.write_text(json.dumps(data), 'utf-8')
                tmp.replace(path)
                print(f'Indexed {count}: {p.title}', flush=True)
            except Exception as e:
                # Do not log request URL query strings or credentials.
                print(f'Skipped {p.id}: {type(e).__name__}', flush=True)
    return count
