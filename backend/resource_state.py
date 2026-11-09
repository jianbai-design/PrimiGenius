"""Bounded hot state and installation workers; durable history stays queryable."""
from collections import OrderedDict, deque
from collections.abc import MutableMapping
import json
import os
import queue
import sqlite3
import threading
import time
import weakref
from contextlib import contextmanager

_PATH_LOCKS = weakref.WeakValueDictionary()
_PATH_LOCKS_LOCK = threading.Lock()


@contextmanager
def path_lock(path):
    key = os.path.normcase(os.path.realpath(path))
    with _PATH_LOCKS_LOCK:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
    with lock:
        yield


class _Record(dict):
    def __init__(self, value, save, lock):
        super().__init__(value)
        self._save, self._lock = save, lock

    def __setitem__(self, key, value):
        with self._lock:
            updated = dict(self)
            updated[key] = value
            self._save(updated)
            super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        with self._lock:
            updated = dict(self)
            updated.update(*args, **kwargs)
            self._save(updated)
            super().update(*args, **kwargs)


class StateDatabase:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.db = None
        self.namespaces = set()

    def connect(self):
        if self.db is None:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS records ('
                            'namespace TEXT, id TEXT, payload TEXT NOT NULL, status TEXT, '
                            'updated REAL NOT NULL, PRIMARY KEY(namespace,id))')
            self.db.execute('CREATE INDEX IF NOT EXISTS records_status ON records(namespace,status)')
            self.db.commit()
        return self.db

    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None


class PersistentRegistry(MutableMapping):
    def __init__(self, database, namespace, capacity=256, jobs=False):
        self.database, self.namespace = database, namespace
        self.capacity, self.jobs = capacity, jobs
        self.lock = database.lock
        self.cache = OrderedDict()

    def _connect(self):
        db = self.database.connect()
        if self.jobs and self.namespace not in self.database.namespaces:
            # Do not re-run a possibly completed install after a crash. Its logs
            # and package files remain intact; the user can safely recheck/retry.
            rows = db.execute("SELECT id,payload FROM records WHERE namespace=? AND status IN ('pending','running')",
                              (self.namespace,)).fetchall()
            with db:
                for key, payload in rows:
                    value = json.loads(payload)
                    value.update(status='failed', exit_code=-1, error='Previous application session was interrupted',
                                 message='Previous application session was interrupted')
                    db.execute('UPDATE records SET payload=?,status=?,updated=? WHERE namespace=? AND id=?',
                               (json.dumps(value, ensure_ascii=False), 'failed', time.time(), self.namespace, key))
            self.database.namespaces.add(self.namespace)
        return db

    def _save(self, key, value):
        payload = {k: v for k, v in value.items() if k != 'log'}
        db = self._connect()
        with db:
            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?)',
                       (self.namespace, key, json.dumps(payload, ensure_ascii=False), value.get('status'), time.time()))

    def _cache(self, key, value):
        record = _Record(value, lambda record: self._save(key, record), self.lock)
        self.cache[key] = record
        self.cache.move_to_end(key)
        for old in list(self.cache):
            if len(self.cache) <= self.capacity:
                break
            if self.cache[old].get('status') not in ('pending', 'running'):
                del self.cache[old]
        return record

    def __getitem__(self, key):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            row = self._connect().execute('SELECT payload FROM records WHERE namespace=? AND id=?',
                                          (self.namespace, key)).fetchone()
            if row is None:
                raise KeyError(key)
            value = json.loads(row[0])
            if self.jobs and self.namespace == 'r-install':
                value['log'] = deque()
            return self._cache(key, value)

    def __setitem__(self, key, value):
        with self.lock:
            self._save(key, value)
            self._cache(key, value)

    def __delitem__(self, key):
        with self.lock:
            db = self._connect()
            with db:
                cursor = db.execute('DELETE FROM records WHERE namespace=? AND id=?', (self.namespace, key))
            self.cache.pop(key, None)
            if not cursor.rowcount:
                raise KeyError(key)

    def __iter__(self):
        with self.lock:
            keys = [row[0] for row in self._connect().execute('SELECT id FROM records WHERE namespace=?', (self.namespace,))]
        return iter(keys)

    def __len__(self):
        with self.lock:
            return self._connect().execute('SELECT COUNT(*) FROM records WHERE namespace=?', (self.namespace,)).fetchone()[0]

    def active_count(self):
        with self.lock:
            return self._connect().execute("SELECT COUNT(*) FROM records WHERE namespace=? AND status IN ('pending','running')",
                                           (self.namespace,)).fetchone()[0]


class BoundedWorkers:
    def __init__(self, name, workers=1, capacity=32):
        self.name, self.workers = name, workers
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.started = False

    def submit(self, callback, *args):
        with self.lock:
            if not self.started:
                for index in range(self.workers):
                    threading.Thread(target=self._run, name=f'{self.name}-{index}', daemon=True).start()
                self.started = True
            self.queue.put_nowait((callback, args))

    def _run(self):
        while True:
            callback, args = self.queue.get()
            try:
                callback(*args)
            except Exception:
                import logging
                logging.exception('%s worker failed', self.name)
            finally:
                self.queue.task_done()


class StreamQueue(queue.Queue):
    """Lossless backpressure with cancellation, bounded by items and text size."""
    def __init__(self, stop, capacity=256, byte_capacity=1024 * 1024):
        super().__init__(maxsize=capacity)
        self.stop, self.byte_capacity, self.buffered_bytes = stop, byte_capacity, 0

    def _put(self, item):
        value, size = item
        self.buffered_bytes += size
        super()._put((value, size))

    def _get(self):
        value, size = super()._get()
        self.buffered_bytes -= size
        return value

    def put(self, item):
        # UTF-8 is <= 4 bytes/code point; avoid allocating a second encoded log.
        size = sum(len(value) * 4 for value in item.values() if isinstance(value, str))
        with self.not_full:
            while not self.stop.is_set():
                # A single oversized line remains intact. Only the next line
                # blocks; users still receive the complete original record.
                if (self._qsize() < self.maxsize and
                        (not self._qsize() or self.buffered_bytes + size <= self.byte_capacity)):
                    self._put((item, size))
                    self.unfinished_tasks += 1
                    self.not_empty.notify()
                    return
                self.not_full.wait(0.1)
            raise InterruptedError('Log subscriber closed')


class ExpiringCache(OrderedDict):
    """Used under the owner's lock; no background cleanup thread."""
    def __init__(self, capacity=1024, ttl=300):
        super().__init__()
        self.capacity, self.ttl = capacity, ttl

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.capacity:
            self.popitem(last=False)

    def get(self, key, default=None):
        value = super().get(key)
        if value is None:
            return default
        if time.time() - value.get('ts', 0) > self.ttl:
            self.pop(key, None)
            return default
        self.move_to_end(key)
        return value
