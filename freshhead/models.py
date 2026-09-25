from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator
from .stores_extra import NEW_IDS


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(url: str) -> str:
    p = urlsplit(url)
    # Colorways encoded in paths stay distinct. Size/tracking queries do not.
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', '', ''))


def product_id(store: str, url: str, variant: str = '') -> str:
    return hashlib.sha256(f'{store}:{canonical(url)}:{variant}'.encode()).hexdigest()[:24]


def textnorm(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', str(s).lower().replace('ł', 'l'))
                   if not unicodedata.combining(c))


Category = Literal['top', 'bottom', 'footwear', 'outerwear', 'accessory', 'unknown']


class Variant(BaseModel):
    size: str = ''
    color: str = ''
    price: float | None = Field(default=None, ge=0)
    currency: str = ''
    available: bool | None = None
    sku: str = ''
    url: str = ''


class Product(BaseModel):
    id: str = ''
    store: str
    url: str
    title: str = Field(min_length=1, max_length=600)
    brand: str = ''
    description: str = ''
    image: str = ''
    images: list[str] = Field(default_factory=list)
    category: Category = 'unknown'
    price: float | None = Field(default=None, ge=0)
    currency: str = ''
    price_from: bool = False
    available: bool | None = None
    variants: list[Variant] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    gender: str = 'unknown'
    fit: str = 'unknown'
    shoe_mass: str = 'unknown'
    pattern: str = 'unknown'
    material: str = ''
    extraction: str = 'unknown'
    demo: bool = False
    fetched_at: str = Field(default_factory=now)
    first_seen: str = Field(default_factory=now)
    visual_attributes: dict = Field(default_factory=dict)
    attributes_source: str = 'text heuristics; not visually verified'

    @field_validator('url')
    @classmethod
    def valid_url(cls, v: str) -> str:
        if urlsplit(v).scheme not in ('https', 'http'):
            raise ValueError('Expected a public product URL')
        return canonical(v)

    @field_validator('image', 'images', mode='before')
    @classmethod
    def safe_images(cls, v):
        def safe(s):
            return s if isinstance(s, str) and (s.startswith('https://') or s.startswith('/static/demo/')) else ''
        return [s for x in v if (s := safe(x))] if isinstance(v, list) else safe(v)


class Settings(BaseModel):
    enabled_stores: list[str] = Field(default_factory=lambda: ['eme', 'supersklep', 'jaded', 'walk'] + NEW_IDS)
    budgets: dict[str, float] = Field(default_factory=lambda: {'PLN': 1200, 'EUR': 280, 'GBP': 240, 'USD': 300})
    sizes: dict[str, list[str]] = Field(default_factory=dict)
    strict_sizes: bool = False
    hide_unknown_prices: bool = False
    gender: Literal['all', 'men', 'women'] = 'men'
    outfit_mode: Literal['balanced', 'contrast', 'relaxed'] = 'balanced'
    ai_enabled: bool = True
    ai_auto_index: bool = False
    ai_batch_limit: int = Field(default=192, ge=1, le=1000)
    daily_enabled: bool = False
    daily_time: str = '09:00'
    timezone: str = 'Europe/Warsaw'
    digest_count: int = Field(default=10, ge=1, le=20)
    page_budget: int = Field(default=24, ge=4, le=120)
    browser_fallback: bool = False
    season: Literal['any', 'warm', 'cool'] = 'any'

    @field_validator('daily_time')
    @classmethod
    def valid_time(cls, v):
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', v):
            raise ValueError('Use HH:MM')
        return v

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, v):
        from zoneinfo import ZoneInfo
        ZoneInfo(v)
        return v

    @field_validator('budgets')
    @classmethod
    def valid_budgets(cls, v):
        if any(not re.fullmatch('[A-Z]{3}', k) or not 0 < n <= 1000000 for k, n in v.items()):
            raise ValueError('Invalid currency or budget')
        return v
