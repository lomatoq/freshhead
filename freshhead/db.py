from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import Product, Settings, now, product_id


class DB:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.getenv('FRESHHEAD_DATA', 'data'))
        if self.path.suffix != '.sqlite3':
            self.path = self.path / 'freshhead.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, store TEXT NOT NULL,
                    body TEXT NOT NULL, first_seen TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ratings(id TEXT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
                    value INTEGER NOT NULL CHECK(value IN (-1,0,1)), updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, store TEXT NOT NULL,
                    status TEXT NOT NULL, message TEXT NOT NULL, count INTEGER NOT NULL,
                    started TEXT NOT NULL, ended TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outfits(id TEXT PRIMARY KEY, body TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS digests(day TEXT PRIMARY KEY, body TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS delivered(id TEXT PRIMARY KEY, sent TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS product_store ON products(store);
            ''')
            # A process may have exited in the middle of a scan; never display it as still running.
            c.execute("UPDATE runs SET status='interrupted',message='Предыдущий запуск прерван',ended=? WHERE status='running'", (now(),))

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def get(self, key, default=None):
        with self.connect() as c:
            r = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, key, value):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def settings(self) -> Settings:
        return Settings.model_validate(self.get('settings', {}))

    def upsert(self, p: Product) -> bool:
        p.id = p.id or product_id(p.store, p.url)
        with self.connect() as c:
            old = c.execute('SELECT first_seen FROM products WHERE id=?', (p.id,)).fetchone()
            if old:
                p.first_seen = old[0]
            c.execute('''INSERT INTO products VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                body=excluded.body,updated=excluded.updated''',
                (p.id, p.store, p.model_dump_json(), p.first_seen, now()))
        return old is None

    def products(self) -> list[Product]:
        with self.connect() as c:
            rows = c.execute('SELECT body FROM products ORDER BY first_seen DESC,id').fetchall()
        return [Product.model_validate_json(r[0]) for r in rows]

    def product(self, pid: str) -> Product | None:
        with self.connect() as c:
            r = c.execute('SELECT body FROM products WHERE id=?', (pid,)).fetchone()
        return Product.model_validate_json(r[0]) if r else None

    def ratings(self) -> dict[str, int]:
        with self.connect() as c:
            return {r[0]: r[1] for r in c.execute('SELECT id,value FROM ratings')}

    def rate(self, pid, value):
        if not self.product(pid):
            raise KeyError(pid)
        with self.connect() as c:
            if value is None:
                c.execute('DELETE FROM ratings WHERE id=?', (pid,))
            else:
                c.execute('INSERT OR REPLACE INTO ratings VALUES(?,?,?)', (pid, value, now()))

    def begin_run(self, store):
        with self.connect() as c:
            cur = c.execute('INSERT INTO runs(store,status,message,count,started,ended) VALUES(?,?,?,?,?,?)',
                            (store, 'running', 'Читаю публичный каталог', 0, now(), ''))
            return cur.lastrowid

    def finish_run(self, rid, status, message, count=0):
        with self.connect() as c:
            c.execute('UPDATE runs SET status=?,message=?,count=?,ended=? WHERE id=?',
                      (status, message[:1500], count, now(), rid))

    def runs(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 30')]

    def save_outfit(self, oid, body):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO outfits VALUES(?,?,?)', (oid, json.dumps(body, ensure_ascii=False), now()))

    def outfits(self):
        with self.connect() as c:
            return [dict(id=r[0], **json.loads(r[1])) for r in c.execute('SELECT id,body FROM outfits ORDER BY created DESC')]

    def digest(self, day=None):
        with self.connect() as c:
            r = c.execute('SELECT body FROM digests WHERE day=?', (day,)).fetchone() if day else c.execute('SELECT body FROM digests ORDER BY day DESC LIMIT 1').fetchone()
        return json.loads(r[0]) if r else None

    def save_digest(self, day, body):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO digests VALUES(?,?,?)', (day, json.dumps(body, ensure_ascii=False), now()))

    def sent(self):
        with self.connect() as c:
            return {r[0] for r in c.execute('SELECT id FROM delivered')}

    def mark_sent(self, pid):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO delivered VALUES(?,?)', (pid, now()))

    def clear_demo(self):
        with self.connect() as c:
            c.execute("DELETE FROM products WHERE store='demo'")
            # Demo outfit cards must not linger after removing their products.
            for row in c.execute('SELECT id,body FROM outfits').fetchall():
                if any(p.get('demo') for p in json.loads(row['body']).get('items', [])):
                    c.execute('DELETE FROM outfits WHERE id=?', (row['id'],))
