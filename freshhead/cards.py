"""Bounded card extraction for mixed storefront themes.

Never borrow a neighbouring product's image/price. No JavaScript execution,
private endpoints, or fabricated currency conversion.
"""
from __future__ import annotations

import json
import re
from copy import copy
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .models import Product, canonical


def item_url(url):
    p = urlsplit(url.strip())
    path = re.sub(r'/collections/[^/]+/products/', '/products/', p.path)
    return canonical(urlunsplit((p.scheme, p.netloc, path, '', '')))


def page_currency(soup):
    text = str(soup)
    match = re.search(r'Shopify\.currency\s*=\s*(\{[^;]+\})', text)
    if match:
        try:
            active = json.loads(match[1]).get('active', '')
            if re.fullmatch('[A-Z]{3}', active):
                return active
        except (ValueError, TypeError):
            pass
    for el in soup.select('meta[itemprop="priceCurrency"],meta[property="product:price:currency"]'):
        value = el.get('content', '')
        if re.fullmatch('[A-Z]{3}', value):
            return value
    return ''


def card_price(card, currency):
    from .catalog import money, price_text
    cc = card.select_one('[itemprop="priceCurrency"]')
    currency = cc.get('content') or cc.get_text(strip=True) if cc else currency
    # Known current/sale-price markers precede compare-at and lowest-30-day prices.
    selectors = ('[data-price-type="finalPrice"][data-price-amount]',
                 '[itemprop="price"][content]', '.price-item--sale',
                 '.price__sale .price-item--sale', 'ins', '[data-product-price]',
                 '.product-price__current', '.price__current')
    for selector in selectors:
        for el in card.select(selector):
            if el.find_parent(['del', 's']):
                continue
            price, cur = price_text(el.get_text(' ', strip=True))
            raw = el.get('content') or el.get('data-price-amount')
            if price is not None:
                return price, cur
            if currency and raw is not None and money(raw) is not None:
                return money(raw), currency
    clean = copy(card)
    for el in clean.select('del,s,[class*="compare"],[class*="lowest"],script,style'):
        el.decompose()
    leaves = [el.get_text(' ', strip=True) for el in clean.find_all(['span', 'strong', 'b', 'div'])
              if not el.find(['span', 'strong', 'b', 'div'])]
    priced = [price_text(t) for t in leaves if len(t) < 70 and price_text(t)[0] is not None]
    if priced:
        return priced[0]
    # Parsing the full title can join model numbers to prices, so prefer price nodes.
    for el in clean.select('[class*="price"], [data-price]'):
        text = el.get_text(' ', strip=True)
        result = price_text(text)
        if len(text) < 100 and result[0] is not None:
            return result
        if currency and re.fullmatch(r'\s*[\d.,\s]+\s*', text) and money(text) is not None:
            return money(text), currency
    text = clean.get_text(' ', strip=True)
    return price_text(text) if len(text) < 700 else (None, '')


def extract_cards(soup, url, store):
    from .catalog import enrich, valid_store_url
    if store.id == 'bstn':
        return bstn_cards(soup, url, store)
    if store.id == 'wss':
        return wss_cards(soup, url, store)
    result = {}
    currency = page_currency(soup)
    is_product = lambda u: valid_store_url(u, store) and bool(re.search(store.product_pattern, urlsplit(u).path))
    urls_cache = {}
    def product_urls(node):
        key = id(node)
        if key not in urls_cache:
            urls_cache[key] = {item_url(u) for a in node.select('a[href]')
                               if is_product(u := urljoin(url, a['href'].strip()))}
        return urls_cache[key]
    for a in soup.select('a[href]'):
        href = urljoin(url, a['href'].strip())
        if not is_product(href):
            continue
        href = item_url(href)
        if href in result:
            continue
        if a.find_parent(['header', 'footer', 'nav']):
            continue
        card = a
        for _ in range(9):
            parent = card.parent
            if not parent or parent.name in ('body', 'html', 'header', 'footer', 'nav'):
                break
            targets = product_urls(parent)
            if targets - {href}:
                break
            card = parent
            classes = ' '.join(card.get('class', []))
            if (card.find('img') and card_price(card, currency)[0] is not None
                    and (card.name in ('product-card', 'li') or re.search(r'product[-_]?(?:item|card|block|tile)', classes))):
                break
        img = a.find('img') or card.find('img')
        if not img:
            continue
        image = img.get('data-src') or img.get('data-original') or img.get('src') or ''
        if not image or image.startswith('data:'):
            srcset = img.get('data-srcset') or img.get('srcset') or ''
            image = srcset.split(',')[0].strip().split(' ')[0]
        # T4S lazy themes put width=1 in data-src; the browser selects from data-widths.
        # Only expand the placeholder when this exact width is advertised by the page.
        if img.get('data-widths'):
            try:
                widths = json.loads(img['data-widths'])
                if isinstance(widths, list) and 600 in widths:
                    image = re.sub(r'([?&]width=)1(?=&|$)', r'\g<1>600', image)
            except (ValueError, TypeError):
                pass
        image = urljoin(url, image.replace('{width}', '600')) if image else ''
        if not image.startswith('https://'):
            continue
        title_node = card.select_one('[itemprop="name"],.product-item-name,.product-card__title,.product-block__title,h3,h2,h4')
        title = title_node.get_text(' ', strip=True) if title_node else ''
        title = title or img.get('alt') or a.get('aria-label') or a.get('title') or a.get_text(' ', strip=True)
        title = re.sub(r'\s+', ' ', title).strip()
        if not title or len(title) > 350 or re.search(r'worry.free|shipping protection|shipping insurance|gift card', title, re.I):
            continue
        price, cur = card_price(card, currency)
        product_class = any('product' in ' '.join(n.get('class', [])) or n.name == 'product-card'
                            for n in [a, card, a.parent] if n)
        if price is None and not product_class:
            continue
        brand_node = card.select_one('[itemprop="brand"],.product-brand,.product-item-brand,[data-brand]')
        brand = (brand_node.get('data-brand') or brand_node.get_text(' ', strip=True)) if brand_node else store.default_brand
        p = enrich(Product(store=store.id, url=href, title=title, brand=brand[:100],
                           image=image, price=price, currency=cur, extraction='catalog-card'))
        # Category and gender from an explicit product path, never from a site's general audience.
        path = unquote(urlsplit(href).path).lower()
        if '/women/' in path or '/damskie/' in path:
            p.gender = 'women'
        elif '/men/' in path or '/meskie/' in path:
            p.gender = 'men'
        if p.category == 'unknown' and ('/shoes/' in path or '/footwear/' in path):
            p.category = 'footwear'
        if store.id == 'asics' and p.category == 'unknown' and re.search(r'\bgel[- ]|\bgt[- ]|\bskyhand\b', title, re.I):
            p.category = 'footwear'
        result[href] = p
    return list(result.values())


def wss_cards(soup, url, store):
    """WSS main-card attributes belong to that SKU, never its colour swatches."""
    from .catalog import enrich, valid_store_url, money
    from .models import textnorm
    rows = {}
    for a in soup.select('a[data-ga][href]'):
        href = item_url(urljoin(url, a['href']))
        if not valid_store_url(href, store) or not re.search(store.product_pattern, urlsplit(href).path):
            continue
        try:
            data = json.loads(a['data-ga'])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or not data.get('item_id') or not data.get('item_name'):
            continue
        container = a.find_parent(class_=re.compile(r'^(listing-product|product-box|product-card)$')) or a.parent
        img = a.find('img') or container.find('img')
        image = (img.get('data-src') or img.get('src') or '') if img else ''
        if image.startswith('data:') and img:
            image = (img.get('data-srcset') or img.get('srcset') or '').split(',')[0].strip().split(' ')[0]
        currency = str(data.get('currency', ''))
        if not re.fullmatch('[A-Z]{3}', currency):
            currency = ''
        title = str(data['item_name'])[:350]
        p = enrich(Product(store=store.id, url=href, title=title, brand=str(data.get('item_brand') or ''),
            description=str(data.get('item_category5') or ''), image=urljoin(url,image) if image else '',
            price=money(data.get('price')) if currency else None, currency=currency, extraction='catalog-card'))
        if textnorm(data.get('item_category2', '')) == 'obuwie':
            p.category = 'footwear'
        gender = textnorm(data.get('item_category', ''))
        if gender == 'mezczyzna': p.gender = 'men'
        elif gender == 'kobieta': p.gender = 'women'
        rows[p.id] = p
    return list(rows.values())


def bstn_cards(soup, url, store):
    """Read product hits embedded in the public page; no search API requests."""
    from .catalog import enrich, valid_store_url, money, walk_json
    from .models import Variant, textnorm
    node = soup.select_one('script#__NEXT_DATA__[type="application/json"]')
    if not node:
        return []
    try:
        data = json.loads(node.get_text())
    except (ValueError, TypeError):
        return []
    prefix = '/' + urlsplit(url).path.strip('/').split('/')[0] + '/'
    origin = 'https://' + urlsplit(url).netloc
    rows = {}
    for hit in walk_json(data):
        if not all(k in hit for k in ('objectID', 'name', 'image_url', 'url', 'price')):
            continue
        raw = hit['url']
        if not isinstance(raw, str):
            continue
        href = item_url(urljoin(origin + prefix, raw))
        if not valid_store_url(href, store) or not re.search(store.product_pattern, urlsplit(href).path):
            continue
        prices = hit.get('price', {})
        if not isinstance(prices, dict):
            continue
        currency = 'EUR' if 'EUR' in prices else next(iter(prices), '') if len(prices) == 1 else ''
        price_data = prices.get(currency, {})
        if not isinstance(price_data, dict):
            continue
        color = hit.get('color', [])
        p = enrich(Product(store=store.id, url=href, title=str(hit['name'])[:350], brand=str(hit.get('brand') or ''),
            image=str(hit['image_url']), price=money(price_data.get('default')), currency=currency,
            colors=color if isinstance(color,list) else [str(color)], extraction='embedded-json'))
        cats = ' '.join(str(c) for c in hit.get('categories.level1', []))
        if 'Footwear' in cats: p.category = 'footwear'
        genders = set(hit.get('gender', []))
        if {'men','women'} <= genders: p.gender = 'unisex'
        elif 'men' in genders: p.gender = 'men'
        elif 'women' in genders: p.gender = 'women'
        for group in hit.get('swatches_conf', {}).values():
            if not isinstance(group, dict): continue
            system = group.get('label', '')
            for item in group.get('items', []):
                stock = item.get('in_stock')
                p.variants.append(Variant(size=(str(system)+' '+str(item.get('label',''))).strip(),
                    available=True if stock == 1 else False if stock == 0 else None, sku=str(item.get('sku') or '')))
        if p.variants and any(v.available is not None for v in p.variants):
            p.available = any(v.available is True for v in p.variants)
        if hit.get('coming_soon') == 'Yes' or hit.get('app_only') is True or hit.get('instore_only') is True:
            p.available = False
        rows[p.id] = p
    return list(rows.values())
