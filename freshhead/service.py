from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from .catalog import CrawlError, PublicClient, STORES, extract, store_for
from .models import Product, canonical, now
from .style import Ranker, build_outfits
from .vision import load_index

log = logging.getLogger('freshhead')


class Service:
    def __init__(self, db):
        self.db = db
        self.lock = asyncio.Lock()
        self.task = None
        self.progress = {'running': False, 'store': '', 'message': ''}

    def ranker(self):
        return Ranker(self.db.products(), self.db.ratings(), load_index(self.db))

    def merge(self, p):
        old = self.db.product(p.id)
        # A shallow card must not erase richer details. Keep their original timestamp:
        # this deliberately leaves the record stale until a detail-page check succeeds.
        if old and p.extraction == 'catalog-card' and old.extraction != 'catalog-card':
            return False
        return self.db.upsert(p)

    async def scan_store(self, sid):
        store = STORES[sid]
        settings = self.db.settings()
        rid = self.db.begin_run(sid)
        count, added, errors = 0, 0, []
        self.progress.update(store=store.name, message='Читаю каталог')
        try:
            async with PublicClient(store) as client:
                candidates = []
                for seed in store.seeds:
                    if client.requests >= settings.page_budget:
                        break
                    try:
                        markup, final_url = await client.html(seed)
                        products, links = extract(markup, final_url, store)
                        if not products and not links and settings.browser_fallback:
                            markup, final_url = await client.render(final_url)
                            products, links = extract(markup, final_url, store)
                        candidates.extend(links)
                        for p in products:
                            added += self.merge(p)
                            count += 1
                    except CrawlError as e:
                        if e.status == 'blocked':
                            raise
                        errors.append(str(e))
                unique = list(dict.fromkeys(candidates))
                old = {p.url: p for p in self.db.products() if p.store == sid}
                # Sparse refresh rotates: shallow/new cards first, then oldest details.
                unique.sort(key=lambda u: (old[u].extraction != 'catalog-card', old[u].fetched_at)
                            if u in old else (False, ''))
                for url in unique:
                    if client.requests >= settings.page_budget:
                        break
                    self.progress['message'] = f'Проверяю карточки · {client.requests}/{settings.page_budget} запросов'
                    try:
                        markup, final_url = await client.html(url)
                        products, _ = extract(markup, final_url, store, detail=True)
                        p = next((p for p in products if p.url == canonical(final_url)), None)
                        if p:
                            added += self.merge(p)
                            count += 1
                    except CrawlError as e:
                        if e.status == 'blocked':
                            raise
                        errors.append(str(e))
                if not count:
                    raise CrawlError('Карточки не извлечены. Разметка изменилась, нужна JS-страница или источник недоступен. '
                                     + '; '.join(errors[:2]), 'empty')
                status = 'partial' if errors else 'ok'
                message = f'Обработано {count} карточек; новых {added}; запросов {client.requests}. Это ограниченный проход, не весь магазин.'
                if errors:
                    message += ' Ошибки: ' + '; '.join(errors[:2])
                self.db.finish_run(rid, status, message, count)
        except asyncio.CancelledError:
            self.db.finish_run(rid, 'interrupted', 'Приложение остановлено во время проверки', count)
            raise
        except CrawlError as e:
            self.db.finish_run(rid, e.status, str(e), count)
        except Exception as e:
            self.db.finish_run(rid, 'error', f'{type(e).__name__}: источник сейчас недоступен. Проверь интернет и журнал.', count)
            log.warning('Scan failed for %s: %s', sid, type(e).__name__)

    async def refresh(self, sid=None):
        async with self.lock:
            self.progress = {'running': True, 'store': '', 'message': 'Начинаю проверку'}
            try:
                ids = [sid] if sid else self.db.settings().enabled_stores
                for source in ids:
                    if source in STORES:
                        await self.scan_store(source)
            finally:
                self.progress = {'running': False, 'store': '', 'message': 'Проверка завершена'}

    def start_refresh(self, sid=None):
        if self.task and not self.task.done() or self.lock.locked():
            return False
        self.task = asyncio.create_task(self.refresh(sid))
        return True

    async def import_url(self, url):
        store = store_for(url)
        async with self.lock:
            async with PublicClient(store) as client:
                markup, final = await client.html(url)
                products, _ = extract(markup, final, store, detail=True)
                p = next((p for p in products if p.url == canonical(final)), None)
                if not p:
                    raise ValueError('Не удалось найти карточку этой вещи. Нужна прямая ссылка на товар, не на категорию.')
                self.db.upsert(p)
                return p

    async def make_digest(self, send=False):
        s = self.db.settings()
        day = datetime.now(ZoneInfo(s.timezone)).date().isoformat()
        previous = self.db.digest(day)
        # Persisted daily result remains the same on retries; completed sends are not repeated.
        if previous and send:
            body = previous
        else:
            sent = self.db.sent()
            feed = [p for p in self.ranker().feed(s) if not p['demo'] and p['id'] not in sent]
            # Recommend things not already liked first, without excluding their use in outfits.
            feed.sort(key=lambda p: (p.get('rating') == 1, -p['score']))
            checked = []
            for candidate in feed[:s.digest_count * 2]:
                try:
                    p = await self.import_url(candidate['url'])
                    from .style import allowed
                    if not allowed(p, s):
                        continue
                    row = p.model_dump()
                    score, reason = self.ranker().taste(p)
                    row.update(score=score, reason=reason)
                    checked.append(row)
                except (ValueError, CrawlError, httpx.HTTPError):
                    # Do not send an old unverified price as a new daily discovery.
                    continue
                if len(checked) >= s.digest_count:
                    break
            # Demo records are never used in a real daily digest or delivery.
            real = [p for p in self.db.products() if not p.demo]
            looks = build_outfits(real, self.db.ratings(), s, embeddings=load_index(self.db), limit=2)['outfits']
            body = {'day': day, 'created': now(), 'items': checked, 'outfits': looks,
                    'status': 'ready' if checked else 'empty', 'sent': False,
                    'message': 'Цены в подборке перепроверены. Наличие размера и доставку подтверди в магазине.' if checked
                        else 'Нет новых перепроверенных вещей. Проверь источники, фильтры и доступность сети.'}
            self.db.save_digest(day, body)
        if send and body['items']:
            result = await send_telegram(self.db, body)
            body.update(result)
            self.db.save_digest(day, body)
        return body

    async def scheduler(self):
        while True:
            try:
                s = self.db.settings()
                local = datetime.now(ZoneInfo(s.timezone))
                day = local.date().isoformat()
                if (s.daily_enabled and local.strftime('%H:%M') >= s.daily_time
                        and self.db.get('scheduler_day') != day and not self.lock.locked()):
                    await self.refresh()
                    body = await self.make_digest(send=True)
                    self.db.set('scheduler_day', day)
                    self.db.set('scheduler_last', {'day': day, 'status': body['status']})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning('Scheduler error: %s', type(e).__name__)
            await asyncio.sleep(30)


async def send_telegram(db, body):
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return {'sent': False, 'delivery_message': 'Telegram не настроен. Подборка сохранена в приложении.'}
    sent = db.sent()
    delivered = 0
    async with httpx.AsyncClient(timeout=25) as client:
        for p in body['items']:
            if p.get('demo') or p['id'] in sent:
                continue
            price = f"{p['price']:g} {p['currency']}" if p.get('price') is not None else 'Цена не указана'
            text = f"FRESHHEAD · {body['day']}\n{p['title']}\n{p.get('brand') or p['store']} · {price}\n{p.get('reason', '')}\n{p['url']}"
            # Telegram can show a page preview for the first URL (photo or product).
            payload = {'chat_id': chat, 'text': text[:3900],
                       'link_preview_options': {'is_disabled': False, 'url': p.get('image') or p['url'], 'prefer_large_media': True}}
            try:
                r = await client.post(f'https://api.telegram.org/bot{token}/sendMessage', json=payload)
                if not r.is_success or not r.json().get('ok'):
                    return {'sent': False, 'delivery_message': 'Telegram отклонил отправку. Проверь токен и chat ID; отправленные вещи не повторятся.'}
                db.mark_sent(p['id'])  # only AFTER successful external delivery
                delivered += 1
                await asyncio.sleep(1.1)
            except (httpx.HTTPError, ValueError):
                return {'sent': False, 'delivery_message': 'Сеть прервала отправку. Результат сохранён; неопределённый исход может дать один повтор.'}
    return {'sent': True, 'delivery_message': f'Отправлено новых карточек: {delivered}'}
