"""Separate taste similarity from outfit compatibility; all scores are heuristics.

No model is trained on an expert's name, no score is a probability. The editorial
sources inspire explainable rules, and deliberately contradictory silhouette modes
are preserved. See docs/STYLE_GUIDE.md for provenance and limitations.
"""
from __future__ import annotations

import hashlib
import itertools
import math
import random
import re
from collections import Counter
from datetime import datetime, timezone

from .models import Product, Settings, textnorm

SOURCES = [
    {'id': 'porter-proportions', 'title': 'How To Get Wide-Leg Trousers Right',
     'author': 'Ashley Ogawa Clarke; комментарии Benedict Browne', 'date': '2025-09-08',
     'url': 'https://www.mrporter.com/en-au/journal/fashion/wide-leg-pants-men-guide-25329258',
     'summary': 'Контраст объёмных брюк с укороченным верхом и более тонкой обувью — один из вариантов, не универсальный закон.'},
    {'id': 'guy-oversized', 'title': 'A Stylish Guy’s Guide To Oversized Clothes',
     'author': 'Derek Guy', 'date': '2023-08-08',
     'url': 'https://www.mrporter.com/en-nl/journal/fashion/dieworkwear-how-to-experiment-with-oversized-24929938',
     'summary': 'Важен задуманный крой и пропорции целого образа, а не простое увеличение размера.'},
    {'id': 'freshhead-editorial', 'title': 'Правила прототипа Freshhead',
     'author': 'Freshhead', 'date': '2026-09-25', 'url': '',
     'summary': 'Нейтральная палитра, один принт, сочетаемые фактуры и баланс массы — наши проверяемые гипотезы, а не цитаты экспертов.'},
]

NEUTRAL = {'black', 'white', 'cream', 'grey', 'brown', 'beige', 'olive'}


def features(p: Product) -> Counter:
    tokens = [t for t in re.findall(r'[a-z]{3,}', textnorm(p.title)) if t not in {'the', 'with', 'and', 'men', 'women'}]
    v = Counter(tokens)
    for token in p.tags:
        v['tag:' + token] += 3
    for color in p.colors:
        v['color:' + color] += 3
    if p.brand:
        v['brand:' + p.brand.lower()] += 2
    if p.fit != 'unknown':
        v['fit:' + p.fit] += 4
    if p.shoe_mass != 'unknown':
        v['mass:' + p.shoe_mass] += 3
    return v


def cosine(a, b):
    if isinstance(a, dict):
        dot = sum(x * b.get(k, 0) for k, x in a.items())
        norm = math.sqrt(sum(x*x for x in a.values()) * sum(x*x for x in b.values()))
    else:
        if len(a) != len(b):
            return 0.0
        dot = sum(x*y for x, y in zip(a, b))
        norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return dot / norm if norm else 0.0


def allowed(p: Product, s: Settings) -> bool:
    if p.available is False or p.gender == 'kids':
        return False
    if s.gender != 'all' and p.gender not in ('unknown', 'unisex', s.gender):
        return False
    if p.store not in s.enabled_stores and not p.demo:
        return False
    if s.hide_unknown_prices and (p.price is None or not p.currency):
        return False
    if p.price is not None and p.currency in s.budgets and p.price > s.budgets[p.currency]:
        return False
    # Unknown currencies are not silently compared against PLN.
    if p.price is not None and p.currency and s.budgets and p.currency not in s.budgets:
        return False
    sizes = {textnorm(x).strip().replace(',', '.') for x in s.sizes.get(p.category, []) if x.strip()}
    if sizes:
        observed = [v for v in p.variants if v.size]
        matching = [v for v in observed if textnorm(v.size).strip().replace(',', '.') in sizes]
        in_stock = [v for v in matching if v.available is True]
        if s.strict_sizes and not in_stock:
            return False
        if matching and all(v.available is False for v in matching):
            return False
        if observed and not matching:
            return False
    if s.season == 'warm' and 'cool' in p.tags:
        return False
    if s.season == 'cool' and p.category == 'bottom' and 'warm' in p.tags:
        return False
    return True


class Ranker:
    def __init__(self, products, ratings, embeddings=None):
        self.products = products
        self.ratings = ratings
        self.vectors = {p.id: features(p) for p in products}
        self.embeddings = embeddings or {}
        self.by_category = {}
        self._taste_cache = {}
        for p in products:
            self.by_category.setdefault((p.category, p.demo), []).append(p)

    def similarity(self, a, b):
        text = cosine(self.vectors[a.id], self.vectors[b.id])
        av, bv = self.embeddings.get(a.id), self.embeddings.get(b.id)
        # Both images must have embeddings from the same backend (validated on load).
        return .7 * max(0, cosine(av, bv)) + .3 * text if av is not None and bv is not None else text

    def taste(self, p):
        if p.id in self._taste_cache:
            return self._taste_cache[p.id]
        same = self.by_category.get((p.category, p.demo), [])
        positives = [(self.similarity(p, q), q) for q in same if self.ratings.get(q.id) == 1 and q.id != p.id]
        negatives = [self.similarity(p, q) for q in same if self.ratings.get(q.id) == -1 and q.id != p.id]
        positives.sort(key=lambda x: x[0], reverse=True)
        # Multiple liked modes survive: nearest-neighbour aggregation, not one average wardrobe.
        nearest = positives[:3]
        pos = sum(x[0] for x in nearest) / len(nearest) if nearest else 0
        neg = max(negatives, default=0)
        score = max(0, min(100, 48 + 48*pos - 38*neg))
        reason = f'Близко к «{nearest[0][1].title[:65]}»' if nearest and nearest[0][0] > .12 else 'Ещё изучаю твой вкус в этой категории'
        if p.id in self.embeddings and nearest and nearest[0][1].id in self.embeddings:
            reason += ' · фото + признаки'
        self._taste_cache[p.id] = (round(score), reason)
        return self._taste_cache[p.id]

    def feed(self, settings, category='', saved=False, training=False):
        items = []
        for p in self.products:
            rating = self.ratings.get(p.id)
            if saved and rating != 1:
                continue
            if training and rating is not None:
                continue
            if not saved and rating == -1:
                continue
            if category and p.category != category:
                continue
            if not allowed(p, settings) or p.category in ('unknown', 'accessory'):
                continue
            score, reason = self.taste(p)
            row = p.model_dump()
            row.update(score=score, reason=reason, rating=rating,
                       vision=bool(p.id in self.embeddings))
            items.append(row)
        items.sort(key=lambda p: (-p['score'], p['id']))
        if training:
            # Active learning MVP: interleave categories + deterministic daily shuffle.
            rng = random.Random(datetime.now(timezone.utc).date().isoformat())
            buckets = {}
            for item in items:
                buckets.setdefault((item['category'], item['store']), []).append(item)
            for bucket in buckets.values():
                rng.shuffle(bucket)
            items = [x for row in itertools.zip_longest(*buckets.values()) for x in row if x]
        return items


def compatibility(items: list[Product], mode: str):
    by = {p.category: p for p in items}
    bottom, top, shoe = by.get('bottom'), by.get('top'), by.get('footwear')
    if not all((bottom, top, shoe)):
        return 0, [], [], 'низкая'
    score, reasons, refs = 48, [], []
    wide = bottom.fit in ('wide', 'oversized')
    if wide:
        if mode == 'contrast' and top.fit in ('cropped', 'slim'):
            score += 16
            reasons.append('Объёмный низ + компактный верх: контраст пропорций')
            refs.append('porter-proportions')
        elif mode == 'relaxed' and top.fit in ('oversized', 'wide'):
            score += 16
            reasons.append('Свободный объём продолжается от верха к брюкам')
            refs.append('guy-oversized')
        elif mode == 'balanced' and top.fit in ('cropped', 'regular', 'oversized'):
            score += 12
            reasons.append('Выраженный силуэт брюк поддержан объёмом верха')
            refs.append('freshhead-editorial')
        if mode == 'contrast' and shoe.shoe_mass == 'light':
            score += 12
            reasons.append('Тонкий профиль обуви контрастирует с широкой штаниной')
            refs.append('porter-proportions')
        elif mode != 'contrast' and shoe.shoe_mass == 'heavy':
            score += 10
            reasons.append('Обувь с визуальным весом поддерживает объём низа')
            refs.append('freshhead-editorial')
    colors = set(c for p in items for c in p.colors)
    accents = colors - NEUTRAL
    if colors and len(accents) <= 1:
        score += 10
        reasons.append('Спокойная база' + (' с одним цветовым акцентом' if accents else ' без конкурирующих цветов'))
        refs.append('freshhead-editorial')
    if set(top.colors) & set(shoe.colors):
        score += 5
        reasons.append('Цвет обуви повторяется в верхе')
    statements = sum(p.pattern == 'statement' for p in items)
    if statements == 1:
        score += 7
        reasons.append('Один выразительный принт остаётся главным акцентом')
    elif statements > 1:
        score -= 6  # soft penalty, never a prohibition
    if 'workwear' in bottom.tags and shoe.shoe_mass == 'heavy':
        score += 7
        reasons.append('Утилитарные брюки и плотная обувь говорят на одном языке')
    if 'denim' in bottom.tags and 'knit' in top.tags:
        score += 7
        reasons.append('Фактура трикотажа контрастирует с денимом')
    # Never manufacture a reason when none of the observed metadata supports it.
    if not reasons:
        reasons = ['Базовый комплект; для оценки пропорций не хватает данных о крое']
    observed = sum(bool(p.colors) + (p.fit != 'unknown') for p in items)
    confidence = 'низкая' if observed < 3 else ('средняя' if observed < 5 else 'выше средней')
    return round(max(0, min(100, score))), reasons[:4], sorted(set(refs)), confidence


def build_outfits(products, ratings, settings, anchor_id='', mode='', embeddings=None, limit=6):
    mode = mode or settings.outfit_mode
    ranker = Ranker(products, ratings, embeddings)
    anchor = next((p for p in products if p.id == anchor_id), None)
    if anchor_id and anchor is None:
        raise ValueError('Выбранная вещь больше не найдена')
    if anchor and anchor.category not in ('top', 'bottom', 'footwear'):
        raise ValueError('Начни с верха, брюк или обуви')
    demo = anchor.demo if anchor else not any(not p.demo for p in products)
    pool = [p for p in products if p.demo == demo and allowed(p, settings) and ratings.get(p.id) != -1]
    if anchor and anchor not in pool:
        raise ValueError('Эта вещь скрыта твоими фильтрами, отсутствует в наличии или получила дизлайк')
    buckets = {}
    for category in ('top', 'bottom', 'footwear'):
        candidates = sorted([p for p in pool if p.category == category], key=lambda p: ranker.taste(p)[0], reverse=True)
        diverse, per_store = [], Counter()
        for p in candidates:
            if per_store[p.store] < 3:
                diverse.append(p)
                per_store[p.store] += 1
        chosen = (diverse + [p for p in candidates if p not in diverse])[:18]
        buckets[category] = [anchor] if anchor and anchor.category == category else chosen
    if not all(buckets.values()):
        missing = [k for k, v in buckets.items() if not v]
        return {'outfits': [], 'missing': missing, 'message': 'Нужны доступные вещи во всех трёх категориях'}
    combos = []
    for items in itertools.product(buckets['top'], buckets['bottom'], buckets['footwear']):
        match, reasons, refs, confidence = compatibility(list(items), mode)
        taste = sum(ranker.taste(p)[0] for p in items) / 3
        totals = {}
        for p in items:
            if p.price is not None and p.currency:
                totals[p.currency] = round(totals.get(p.currency, 0) + p.price, 2)
        key = '-'.join(p.id for p in items)
        combos.append({'id': hashlib.sha256(key.encode()).hexdigest()[:20],
            'items': [p.model_dump() for p in items], 'score': round(.7*match + .3*taste),
            'compatibility': match, 'taste': round(taste), 'reasons': reasons,
            'source_ids': refs, 'confidence': confidence, 'mode': mode, 'totals': totals,
            'note': ('Фото + описания; AI-признаки могут ошибаться. Сочетание оценивают правила, не обученная на образах модель. ' if any(p.visual_attributes for p in items) else 'Признаки из описаний. ') + 'Посадку, длину, доставку и наличие проверь отдельно.'})
    combos.sort(key=lambda o: -o['score'])
    selected = []
    for outfit in combos:
        ids = {p['id'] for p in outfit['items']}
        # At most one shared item between suggestions: not six almost identical looks.
        if any(len(ids & {p['id'] for p in other['items']}) >= 2 for other in selected):
            continue
        selected.append(outfit)
        if len(selected) == limit:
            break
    return {'outfits': selected, 'missing': []}
