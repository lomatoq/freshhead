"""Conservative public-page extraction. No private API, CAPTCHA or stealth code."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .models import Product, Variant, canonical, product_id, textnorm


@dataclass(frozen=True)
class Store:
    id: str
    name: str
    domain: str
    seeds: tuple[str, ...]
    product_pattern: str
    description: str
    default_brand: str = ''

    def public(self):
        d = asdict(self)
        d['url'] = 'https://' + self.domain
        return d


STORES = {
    'eme': Store('eme', 'EME Studios', 'emestudios.com',
        ('https://emestudios.com/pl/en/shop',), r'/(?:products?|shop)/[^/?]+|/pl/en/[^/?]+-[^/?]+',
        'Графика, трикотаж и свободный силуэт. Публичные страницы; API не используется.', 'EME Studios'),
    'supersklep': Store('supersklep', 'SUPERSKLEP', 'supersklep.pl',
        ('https://supersklep.pl/spodnie', 'https://supersklep.pl/koszulki',
         'https://supersklep.pl/bluzy', 'https://supersklep.pl/buty'), r'/i\d+-',
        'Кроссовки, ботинки, skate и streetwear. Цены и варианты из карточек.'),
    'jaded': Store('jaded', 'Jaded London', 'jadedldn.com',
        ('https://jadedldn.com/en-pl/collections/mens-all',), r'/products/[^/?]+',
        'Объёмный деним, сложные фактуры и более смелые сочетания.', 'Jaded London'),
    'walk': Store('walk', 'Walk in Paris', 'walkinparis.com',
        ('https://walkinparis.com/en/collections/all',), r'/products/[^/?]+',
        'Более спокойный городской гардероб: рубашки, брюки, трикотаж.', 'Walk in Paris'),
}


class CrawlError(RuntimeError):
    def __init__(self, message, status='error'):
        super().__init__(message)
        self.status = status


def store_for(url: str) -> Store:
    p = urlsplit(url)
    host = (p.hostname or '').lower().removeprefix('www.')
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Разрешены только HTTPS-ссылки магазинов из списка')
    for store in STORES.values():
        if host == store.domain:
            return store
    raise ValueError('Этот магазин пока не подключён')


def valid_store_url(url: str, store: Store) -> bool:
    try:
        return store_for(url).id == store.id
    except (ValueError, TypeError):
        return False


class Robots:
    """Small RFC-9309-style matcher supporting wildcard / end anchor / longest match.

    It intentionally fails closed when robots.txt cannot be retrieved. That may skip
    a store other crawlers would accept, but never silently defeats a site's choice.
    """
    def __init__(self, text: str):
        groups: list[tuple[list[str], list[tuple[str, str]], float]] = []
        agents, rules, delay = [], [], 2.5
        had_rule = False
        for line in text.splitlines() + ['User-agent: __end__']:
            line = line.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            key, value = [s.strip() for s in line.split(':', 1)]
            key = key.lower()
            if key == 'user-agent':
                if had_rule:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, had_rule = [], [], 2.5, False
                agents.append(value.lower())
            elif agents and key in ('allow', 'disallow'):
                had_rule = True
                if value:
                    rules.append((key, value))
            elif agents and key == 'crawl-delay':
                had_rule = True
                try:
                    delay = max(2.5, min(float(value), 120))
                except ValueError:
                    pass
        specific = [g for g in groups if any(a != '*' and a in 'freshhead' for a in g[0])]
        selected = specific or [g for g in groups if '*' in g[0]]
        self.rules = [r for _, rs, _ in selected for r in rs]
        self.delay = max([2.5] + [g[2] for g in selected])

    def allowed(self, url: str) -> bool:
        p = urlsplit(url)
        path = unquote(p.path) + ('?' + p.query if p.query else '')
        matches = []
        for kind, pattern in self.rules:
            end = pattern.endswith('$')
            pattern = pattern[:-1] if end else pattern
            regex = '^' + re.escape(unquote(pattern)).replace(r'\*', '.*') + ('$' if end else '')
            if re.search(regex, path):
                matches.append((len(pattern.replace('*', '')), kind == 'allow'))
        return max(matches)[1] if matches else True


def money(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if 0 <= float(value) < 10000000 else None
    s = re.sub(r'[^\d,.-]', '', str(value or ''))
    if not s:
        return None
    if ',' in s and '.' in s:
        dec = ',' if s.rfind(',') > s.rfind('.') else '.'
        s = s.replace('.' if dec == ',' else ',', '').replace(dec, '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        result = float(s)
        return result if 0 <= result < 10000000 else None
    except ValueError:
        return None


def price_text(text: str):
    # A currency is mandatory. A negative lookbehind prevents taking the last
    # digits of a model number (9060 + 849,90 must never become 60849,90).
    number = r"(?<![\d.,])((?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d{2})?)"
    hits = []
    for m in re.finditer(number + r"\s*(PLN|zł|EUR|GBP|USD)(?![A-Za-z])", text, re.I):
        hits.append((m.start(), money(m[1]), {'ZŁ': 'PLN'}.get(m[2].upper(), m[2].upper())))
    for m in re.finditer(r"(€|£|\$)\s*" + number, text):
        hits.append((m.start(), money(m[2]), {'€': 'EUR', '£': 'GBP', '$': 'USD'}[m[1]]))
    # Euro suffix is also common in European catalogs.
    for m in re.finditer(number + r"\s*(€)(?!\s*\d)", text):
        hits.append((m.start(), money(m[1]), 'EUR'))
    return max(hits, key=lambda h: h[0])[1:] if hits else (None, '')


def availability(value):
    s = str(value or '').lower()
    if any(x in s for x in ('outofstock', 'soldout', 'discontinued')):
        return False
    if any(x in s for x in ('instock', 'limitedavailability')):
        return True
    return None  # preorders/backorders are not in stock


def words(text: str, options: str) -> bool:
    return bool(re.search(r'(?<![a-z])(?:' + options + r')(?![a-z])', text))


def enrich(p: Product) -> Product:
    """Metadata-derived labels are exposed as inferred, never as a vision result."""
    title = textnorm(p.title)
    t = title + ' ' + textnorm(p.description[:3000])
    if words(title, r'snowboard|goggle|deck|deskorolk|bindings|skarpety|socks|belt|pasek|cap|czapka|bag|torba|hat|scarf|szalik|sunglasses'):
        p.category = 'accessory'
    elif words(title, r'buty|sneakers?|shoes?|boots?|loafers?|derby|clogs?|sandals?|trampki|chaussures|mocassins'):
        p.category = 'footwear'
    elif words(title, r'jacket|coat|kurtka|kurtki|plaszcz|bomber|parka|veste|blouson|overshirt'):
        p.category = 'outerwear'
    elif words(title, r'tee|shirt|t-shirt|tshirt|koszulka|koszulki|bluza|hoodie|sweatshirt|knit|sweater|cardigan|polo|longsleeve|pullover|sweter|chemise|pull|tricot'):
        p.category = 'top'
    elif words(title, r'jeans?|denim|trousers?|pants?|spodnie|joggers?|shorts?|szorty|pantalon|bermuda'):
        p.category = 'bottom'
    # Never infer adult men's sizing from a brand name or simply missing women's tag.
    if words(title, r'kids?|junior|jr|dzieciece|dzieciecy'):
        p.gender = 'kids'
    elif words(title, r'wmn|womens?|damska|damskie|damski|femme'):
        p.gender = 'women'
    elif words(t, r'unisex'):
        p.gender = 'unisex'
    elif words(t, r'mens?|meskie|meska|meski|homme'):
        p.gender = 'men'
    fits = [('cropped', r'cropped|crop'), ('wide', r'wide|baggy|loose|balloon|barrel|szerokie'),
            ('oversized', r'oversized|oversize|boxy'), ('slim', r'slim|skinny|fitted|tight'), ('regular', r'straight|regular')]
    for fit, pattern in fits:
        if words(t, pattern):
            p.fit = fit
            break
    cmap = {'black': r'black|czarn\w*|noir|charcoal|washed black',
            'white': r'white|bial\w*|blanc', 'cream': r'cream|ecru|ivory|off.white|angora',
            'grey': r'grey|gray|szar\w*|gris', 'blue': r'blue|niebiesk\w*|bleu|indigo|navy',
            'brown': r'brown|braz\w*|marron|chocolate', 'beige': r'beige|bezow\w*|sand|taupe',
            'olive': r'olive|khaki', 'green': r'green|zielon\w*|vert',
            'red': r'red|czerwon\w*|rouge|burgundy', 'pink': r'pink|rozow\w*|rose',
            'yellow': r'yellow|zolt\w*|jaune', 'orange': r'orange|pomarancz\w*', 'purple': r'purple|violet'}
    # Use explicit color field (when supplied) and title, not unrelated care-copy colors.
    explicit = ' '.join(p.colors)
    p.colors = [k for k, pat in cmap.items() if words(title + ' ' + textnorm(explicit), pat)][:3]
    tags = set(p.tags)
    for tag, pat in [('denim', r'denim|jeans?'), ('workwear', r'carpenter|double.knee|canvas|workwear|cargo|utility'),
                     ('knit', r'knit|sweter|sweater|cardigan|mohair|tricot'), ('graphic', r'graphic|print|logo|floral|stripe|nadruk'),
                     ('sport', r'runner|running|sport|technical|nylon'), ('leather', r'leather|skora|cuir'),
                     ('suede', r'suede|zamsz'), ('warm', r'linen|shorts?|sandals?|len'),
                     ('cool', r'wool|mohair|fleece|puffer|welna')]:
        if words(t, pat):
            tags.add(tag)
    p.tags = sorted(tags)
    p.pattern = 'statement' if 'graphic' in tags else p.pattern
    if p.category == 'footwear':
        if words(t, r'chunky|boot|boots|blundstone|martens|9060|trail|court graffik|hiking|platform'):
            p.shoe_mass = 'heavy'
        elif words(t, r'samba|gazelle|tokyo|mexico 66|loafer|low.profile|slim|ballet|gat'):
            p.shoe_mass = 'light'
    p.brand = p.brand.strip()
    p.id = p.id or product_id(p.store, p.url)
    return p


def walk_json(data):
    if isinstance(data, dict):
        yield data
        for v in data.values():
            if isinstance(v, (dict, list)):
                yield from walk_json(v)
    elif isinstance(data, list):
        for item in data:
            yield from walk_json(item)


def string_value(value):
    if isinstance(value, dict):
        return str(value.get('name', value.get('value', '')))
    return str(value or '')


def ld_product(data, store: Store, page_url: str) -> Product | None:
    title = string_value(data.get('name'))
    url = urljoin(page_url, data.get('url') or page_url)
    if not title or not valid_store_url(url, store):
        return None
    offers = data.get('offers', [])
    offers = offers if isinstance(offers, list) else [offers]
    offers = [o for o in offers if isinstance(o, dict)]
    offer = next((o for o in offers if availability(o.get('availability')) is True), offers[0] if offers else {})
    images = data.get('image', [])
    images = images if isinstance(images, list) else [images]
    images = [urljoin(page_url, x.get('url', x.get('contentUrl', '')) if isinstance(x, dict) else str(x)) for x in images if x]
    images = [x for x in images if x.startswith('https://')]
    variants = []
    for o in offers:
        variants.append(Variant(size=string_value(o.get('size', data.get('size'))),
            color=string_value(o.get('color', data.get('color'))), price=money(o.get('price')),
            currency=str(o.get('priceCurrency') or ''), available=availability(o.get('availability')),
            sku=string_value(o.get('sku', data.get('sku'))), url=urljoin(page_url, o.get('url') or url)))
    prices = [v.price for v in variants if v.price is not None and v.available is not False]
    p = Product(store=store.id, url=url, title=title,
        brand=string_value(data.get('brand')) or store.default_brand,
        description=BeautifulSoup(string_value(data.get('description')), 'html.parser').get_text(' ', strip=True)[:5000],
        image=images[0] if images else '', images=images[:6],
        price=money(offer.get('price', offer.get('lowPrice'))),
        price_from=('lowPrice' in offer or len(set(prices)) > 1),
        currency=str(offer.get('priceCurrency') or ''),
        available=availability(offer.get('availability')),
        variants=variants, colors=[string_value(data.get('color'))] if data.get('color') else [],
        material=string_value(data.get('material')), extraction='json-ld')
    if prices:
        p.price = min(prices)
    return enrich(p)


def extract(html: str, url: str, store: Store, detail: bool = False) -> tuple[list[Product], list[str]]:
    soup = BeautifulSoup(html, 'html.parser')
    products: dict[str, Product] = {}
    links: list[str] = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (ValueError, TypeError):
            continue
        for obj in walk_json(data):
            types = obj.get('@type', [])
            if isinstance(types, str):
                types = [types]
            if 'Product' in types:
                p = ld_product(obj, store, url)
                if p:
                    products[p.id] = p
            if 'ListItem' in types:
                target = obj.get('url') or (obj.get('item', {}).get('url') if isinstance(obj.get('item'), dict) else obj.get('item'))
                if isinstance(target, str) and valid_store_url(urljoin(url, target), store):
                    links.append(canonical(urljoin(url, target)))
    for a in soup.select('a[href]'):
        href = urljoin(url, a['href'])
        if not valid_store_url(href, store) or not re.search(store.product_pattern, urlsplit(href).path):
            continue
        href = canonical(href)
        links.append(href)
        if product_id(store.id, href) in products:
            continue
        img = a.find('img')
        card = a
        # Some shops place price/title next to the image anchor, inside a card.
        for _ in range(3):
            if price_text(card.get_text(' ', strip=True))[0] is not None:
                break
            parent = card.parent
            if not parent or len(parent.select('a[href]')) > 12:
                break
            card = parent
        text = card.get_text(' ', strip=True)
        price, currency = price_text(text)
        leaves = [el.get_text(' ', strip=True) for el in card.find_all(['span', 'ins', 'strong', 'b']) if not el.find(['span', 'ins', 'strong', 'b'])]
        leaf_prices = [price_text(t) for t in leaves if len(t) < 80 and price_text(t)[0] is not None]
        if leaf_prices:
            price, currency = leaf_prices[-1]
        title_node = card.select_one('[itemprop="name"],h3,h2,[class*="product-name"],[class*="product-title"]')
        title = title_node.get_text(' ', strip=True) if title_node else (img.get('alt', '') if img else '')
        if not title:
            title = re.split(r'\d+[.,]\d{2}\s*(?:PLN|EUR|zł|€)', a.get_text(' ', strip=True))[0].strip()
        image = (img.get('data-src') or img.get('src') or '') if img else ''
        if not image and img and img.get('srcset'):
            image = img['srcset'].split(',')[0].split()[0]
        if title and (image or price is not None) and len(title) < 350:
            p = enrich(Product(store=store.id, url=href, title=title, brand=store.default_brand,
                image=urljoin(url, image) if image else '', price=price, currency=currency, extraction='catalog-card'))
            products[p.id] = p
    if detail and not any(p.url == canonical(url) for p in products.values()):
        def meta(*keys):
            for key in keys:
                el = soup.find('meta', attrs={'property': key}) or soup.find('meta', attrs={'name': key})
                if el and el.get('content'):
                    return el['content']
            return ''
        h1 = soup.find('h1')
        title = h1.get_text(' ', strip=True) if h1 else meta('og:title')
        # OG-only imports remain incomplete, rather than fabricating stock or sizes.
        if title and re.search(store.product_pattern, urlsplit(url).path):
            p = enrich(Product(store=store.id, url=url, title=title[:600], brand=meta('product:brand') or store.default_brand,
                description=meta('og:description', 'description'), image=urljoin(url, meta('og:image')) if meta('og:image') else '',
                price=money(meta('product:price:amount', 'og:price:amount')),
                currency=meta('product:price:currency', 'og:price:currency'), extraction='open-graph'))
            products[p.id] = p
    return list(products.values()), list(dict.fromkeys(links))


class PublicClient:
    def __init__(self, store: Store):
        self.store = store
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(25), follow_redirects=False,
            headers={'User-Agent': 'Freshhead/0.1 (+https://github.com/lomatoq/freshhead; personal catalog)',
                     'Accept-Language': 'en,pl;q=0.9', 'Accept': 'text/html,application/json;q=0.9,*/*;q=0.5'})
        self.robots: Robots | None = None
        self.last_request = 0.0
        self.requests = 0

    async def __aenter__(self):
        try:
            response = await self._request('https://' + self.store.domain + '/robots.txt', robots=True)
            if response.status_code == 404:
                self.robots = Robots('User-agent: *\nAllow: /')
            elif response.is_success and not response.text.lstrip().startswith('<'):
                self.robots = Robots(response.text)
            else:
                raise CrawlError('robots.txt недоступен: источник пропущен, доступ не предполагается', 'blocked')
            return self
        except Exception:
            await self.client.aclose()
            raise

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def _request(self, url, robots=False):
        for _ in range(5):
            if not valid_store_url(url, self.store):
                raise CrawlError('Остановлен переход на другой домен', 'blocked')
            if not robots and (not self.robots or not self.robots.allowed(url)):
                raise CrawlError('robots.txt запрещает этот путь', 'blocked')
            delay = self.robots.delay if self.robots else 2.5
            await asyncio.sleep(max(0, delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.requests += 1
            async with self.client.stream('GET', url) as r:
                body = bytearray()
                async for part in r.aiter_bytes():
                    body.extend(part)
                    if len(body) > 8_000_000:
                        raise CrawlError('Страница превышает лимит 8 МБ')
                response = httpx.Response(r.status_code, headers=r.headers, content=bytes(body), request=r.request)
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get('location', ''))
                continue
            return response
        raise CrawlError('Слишком много перенаправлений')

    async def html(self, url):
        r = await self._request(url)
        if r.status_code in (401, 403, 429):
            raise CrawlError(f'HTTP {r.status_code}: защита или лимит. Повторы прекращены.', 'blocked')
        if not r.is_success:
            raise CrawlError(f'HTTP {r.status_code} при чтении {urlsplit(url).path}')
        if re.search(r'<title>[^<]*(?:just a moment|access denied|captcha)', r.text, re.I):
            raise CrawlError('Браузерная проверка: источник пропущен', 'blocked')
        return r.text, str(r.url)

    async def render(self, url):
        """Optional rendering, never used after an access denial.

        No network interception of private APIs. Browser requests outside the store
        (including trackers) are blocked. Site robots rules apply to every request.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise CrawlError('Установи requirements-browser.txt и Chromium для JS-страниц') from e
        if not self.robots or not self.robots.allowed(url):
            raise CrawlError('robots.txt запрещает рендеринг страницы', 'blocked')
        await asyncio.sleep(max(0, self.robots.delay - (time.monotonic() - self.last_request)))
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(locale='en-GB', user_agent='Freshhead/0.1 (Playwright; personal catalog)')
                async def gate(route):
                    target = route.request.url
                    if valid_store_url(target, self.store) and self.robots.allowed(target) and route.request.method == 'GET':
                        await route.continue_()
                    else:
                        await route.abort()
                await page.route('**/*', gate)
                r = await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                if r and r.status in (401, 403, 429):
                    raise CrawlError(f'Браузер получил HTTP {r.status}; обход не выполняется', 'blocked')
                await page.wait_for_timeout(1800)
                for _ in range(3):
                    await page.evaluate('window.scrollBy(0, window.innerHeight)')
                    await page.wait_for_timeout(700)
                return await page.content(), page.url
            finally:
                await browser.close()
