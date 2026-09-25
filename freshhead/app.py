from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .catalog import CrawlError, STORES
from .db import DB
from .models import Settings
from .service import Service
from .style import SOURCES, build_outfits
from .vision import load_index

STATIC = Path(__file__).parent / 'static'


class Rating(BaseModel):
    value: Literal[-1, 0, 1] | None


class ImportBody(BaseModel):
    url: str = Field(max_length=2000)


class OutfitBody(BaseModel):
    anchor_id: str = Field(default='', max_length=50)
    mode: Literal['balanced', 'contrast', 'relaxed'] = 'balanced'


class SaveBody(OutfitBody):
    outfit_id: str = Field(max_length=50)


class DigestBody(BaseModel):
    send: bool = False


def create_app(db: DB | None = None):
    db = db or DB()
    service = Service(db)

    @asynccontextmanager
    async def lifespan(app):
        scheduler = asyncio.create_task(service.scheduler())
        yield
        tasks = [scheduler] + ([service.task] if service.task else [])
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title='Freshhead', version='0.1.0', lifespan=lifespan)
    app.state.db = db
    app.state.service = service
    # Loopback only by default. See README before deliberately publishing this single-user app.
    hosts = os.getenv('FRESHHEAD_ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware('http')
    async def security(request: Request, call_next):
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            origin = request.headers.get('origin')
            expected = f'{request.url.scheme}://{request.headers.get("host", "")}'
            if origin and origin.rstrip('/') != expected:
                return JSONResponse({'detail': 'Cross-origin writes are not allowed'}, status_code=403)
            if request.headers.get('x-freshhead') != '1':
                return JSONResponse({'detail': 'Missing local application request header'}, status_code=403)
            if int(request.headers.get('content-length') or 0) > 200000:
                return JSONResponse({'detail': 'Request too large'}, status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https: data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        return response

    @app.get('/api/state')
    def state():
        products = db.products()
        ratings = db.ratings()
        index = load_index(db)
        return {'version': '0.1.0', 'settings': db.settings().model_dump(),
                'counts': {'products': sum(not p.demo for p in products), 'demo': sum(p.demo for p in products),
                           'liked': sum(v == 1 for v in ratings.values()), 'rated': len(ratings),
                           'images_indexed': len(index)},
                'stores': [s.public() for s in STORES.values()], 'runs': db.runs(),
                'progress': service.progress, 'sources': SOURCES,
                'vision': 'FashionCLIP + metadata' if index else 'Признаки из описаний (без анализа фото)',
                'telegram_ready': bool(os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID')),
                'digest': db.digest(), 'saved_outfits': db.outfits()}

    @app.get('/api/products')
    def products(category: str = '', view: str = 'feed', q: str = '', store: str = ''):
        rows = service.ranker().feed(db.settings(), category=category, saved=view == 'saved', training=view == 'train')
        if q:
            rows = [p for p in rows if q.lower() in f"{p['title']} {p['brand']} {' '.join(p['tags'])}".lower()]
        if store:
            rows = [p for p in rows if p['store'] == store]
        return {'items': rows[:1200], 'total': len(rows)}

    @app.post('/api/products/{pid}/rating')
    def rate(pid: str, body: Rating):
        try:
            db.rate(pid, body.value)
        except KeyError:
            raise HTTPException(404, 'Товар не найден')
        return {'ok': True}

    @app.post('/api/import')
    async def import_url(body: ImportBody):
        if service.lock.locked():
            raise HTTPException(409, 'Сначала заверши текущую проверку магазинов')
        try:
            return (await service.import_url(body.url)).model_dump()
        except (ValueError, CrawlError) as e:
            raise HTTPException(422, str(e))
        except Exception:
            raise HTTPException(502, 'Магазин не ответил. Проверь соединение или попробуй позднее.')

    @app.post('/api/refresh')
    async def refresh(store: str = ''):
        if store and store not in STORES:
            raise HTTPException(404, 'Неизвестный магазин')
        if not service.start_refresh(store or None):
            raise HTTPException(409, 'Проверка уже выполняется')
        return {'started': True}

    @app.put('/api/settings')
    def settings(body: Settings):
        if any(s not in STORES for s in body.enabled_stores):
            raise HTTPException(422, 'Неизвестный магазин')
        db.set('settings', body.model_dump())
        return body

    @app.post('/api/outfits')
    def outfits(body: OutfitBody):
        try:
            return build_outfits(db.products(), db.ratings(), db.settings(), body.anchor_id, body.mode, load_index(db))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post('/api/outfits/save')
    def save(body: SaveBody):
        result = outfits(body)
        found = next((o for o in result['outfits'] if o['id'] == body.outfit_id), None)
        if not found:
            raise HTTPException(409, 'Каталог изменился; пересобери образ перед сохранением')
        data = dict(found)
        data.pop('id')
        db.save_outfit(body.outfit_id, data)
        return {'ok': True}

    @app.delete('/api/outfits/{oid}')
    def delete_outfit(oid: str):
        with db.connect() as c:
            c.execute('DELETE FROM outfits WHERE id=?', (oid,))
        return {'ok': True}

    @app.post('/api/digest')
    async def digest(body: DigestBody):
        if service.lock.locked():
            raise HTTPException(409, 'Проверка магазинов ещё выполняется')
        if getattr(app.state, 'digest_running', False):
            raise HTTPException(409, 'Подборка уже создаётся')
        app.state.digest_running = True
        try:
            return await service.make_digest(send=body.send)
        finally:
            app.state.digest_running = False

    @app.post('/api/demo')
    def demo():
        from .demo import seed_demo
        return {'count': seed_demo(db)}

    @app.delete('/api/demo')
    def remove_demo():
        db.clear_demo()
        return {'ok': True}

    @app.get('/api/export')
    def export():
        return JSONResponse({'products': [p.model_dump() for p in db.products()],
                             'ratings': db.ratings(), 'settings': db.settings().model_dump(),
                             'outfits': db.outfits()},
                            headers={'Content-Disposition': 'attachment; filename="freshhead-export.json"'})

    @app.get('/health')
    def health():
        return {'ok': True}

    @app.get('/')
    def home():
        return FileResponse(STATIC / 'index.html')

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app
