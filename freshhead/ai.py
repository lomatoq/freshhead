"""Bounded AI subprocess jobs. No shell commands from requests, no cloud key."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path

from .vision import MODEL, REVISION, eligible_products, load_records


class AIJobs:
    def __init__(self, db):
        self.db = db
        self.process = None
        self.guard = threading.Lock()
        self.log = None

    @staticmethod
    def dependencies():
        return {name: importlib.util.find_spec(module) is not None for name, module in
                [('torch', 'torch'), ('transformers', 'transformers'), ('Pillow', 'PIL'),
                 ('numpy', 'numpy'), ('filelock', 'filelock')]}

    def locked(self):
        if not self.dependencies()['filelock']:
            return False
        from filelock import FileLock, Timeout
        try:
            with FileLock(str(self.db.path.parent / 'vision.lock'), timeout=0):
                return False
        except Timeout:
            return True

    def running(self):
        if self.process is not None and self.process.poll() is None:
            return True
        if self.log is not None:
            self.log.close()
            self.log = None
        return self.locked()

    def status(self):
        records = load_records(self.db)
        eligible = eligible_products(self.db)
        ratings = self.db.ratings()
        job = dict(self.db.get('ai_job', {}))
        running = self.running()
        if job.get('running') and not running:
            job.update(running=False, phase='interrupted', message='Процесс завершился. Уже обработанные фото сохранены.')
        deps = self.dependencies()
        return {'model': MODEL, 'revision': REVISION, 'enabled': self.db.settings().ai_enabled,
                'ready': all(deps.values()), 'dependencies': deps, 'indexed': len(records),
                'eligible': len(eligible), 'pending': sum(p.id not in records for p in eligible),
                'rated_indexed': sum(pid in records and val in (-1, 1) for pid, val in ratings.items()),
                'running': running, 'job': job,
                'can_stop': self.process is not None and self.process.poll() is None,
                'method': 'FashionCLIP visual retrieval + conservative attribute hypotheses + explicit outfit rules',
                'privacy': 'Local inference. Model download and public shop image requests require internet. Likes stay on this computer.'}

    def start(self, limit=None):
        with self.guard:
            if self.running():
                raise RuntimeError('Анализ фото уже выполняется')
            missing = [k for k, ok in self.dependencies().items() if not ok]
            if missing:
                raise ValueError('Установи AI-зависимости: ' + ', '.join(missing) + '. Запусти install-ai.bat или install-ai.sh.')
            limit = int(limit or self.db.settings().ai_batch_limit)
            if not 1 <= limit <= 1000:
                raise ValueError('Лимит AI: от 1 до 1000 фото')
            env = os.environ.copy()
            env.update(FRESHHEAD_DATA=str(self.db.path.resolve()), PYTHONUTF8='1',
                       HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1')
            self.log = open(self.db.path.parent / 'ai-worker.log', 'a', encoding='utf-8')
            try:
                self.process = subprocess.Popen([sys.executable, '-m', 'freshhead', 'index-images', '--limit', str(limit)],
                    cwd=Path(__file__).resolve().parent.parent, env=env,
                    stdout=self.log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except Exception:
                self.log.close()
                self.log = None
                raise
            return {'started': True, 'limit': limit}

    def stop(self):
        with self.guard:
            if self.process is None or self.process.poll() is not None:
                raise ValueError('Нет AI-процесса, запущенного этим сервером')
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            state = dict(self.db.get('ai_job', {}))
            state.update(running=False, phase='stopped', message='Остановлено. Готовые результаты сохранены.')
            self.db.set('ai_job', state)
            if self.log:
                self.log.close()
                self.log = None
            return {'stopped': True}
