"""Disk-backed, ordered log history; memory and SSE are views, not the archive."""
import os
import sqlite3
import threading
import time


class LogJournal:
    def __init__(self, path, *, fresh_session=False):
        self.path = path
        self.lock = threading.RLock()
        self.db = None
        self._session_start_seq = None
        self._fresh_session = fresh_session

    def _connect(self):
        if self.db is None:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA journal_mode=WAL')
            # WAL commits survive process restarts. Checkpoints sync to disk;
            # do not fsync every individual log line (measured to throttle streams).
            # Sudden OS/power failure durability is not claimed between checkpoints.
            db.execute('PRAGMA synchronous=NORMAL')
            db.execute('CREATE TABLE IF NOT EXISTS events ('
                       'seq INTEGER PRIMARY KEY AUTOINCREMENT, runId TEXT NOT NULL, '
                       'channel TEXT NOT NULL, line TEXT NOT NULL, ts REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS events_run_seq ON events(runId, seq)')
            db.commit()
            if self._session_start_seq is None:
                if self._fresh_session:
                    # Clear only log records, once per application launch. Keep
                    # sequence IDs monotonic and reuse freed SQLite pages rather
                    # than deleting a database/WAL that may still be open.
                    with db:
                        db.execute('DELETE FROM events')
                self._session_start_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            self.db = db
        return self.db

    @property
    def session_start_seq(self):
        with self.lock:
            self._connect()
            return self._session_start_seq

    def append(self, run_id, channel, lines):
        with self.lock:
            db = self._connect()
            events = []
            with db:
                for line in lines:
                    stamp = time.time()
                    text = f'[{channel}] {line}'
                    cursor = db.execute('INSERT INTO events(runId,channel,line,ts) VALUES(?,?,?,?)',
                                        (run_id, channel, text, stamp))
                    events.append(dict(seq=cursor.lastrowid, runId=run_id,
                                       channel=channel, line=text, ts=stamp))
            # Publish only after commit. Disk errors propagate; never silently evict data.
            return events

    def read(self, run_id, after=0, limit=500, until=None):
        with self.lock:
            db = self._connect()
            sql = 'SELECT * FROM events WHERE seq>?'
            args = [max(0, int(after))]
            if run_id != '*':
                sql += ' AND runId=?'
                args.append(run_id)
            if until is not None:
                sql += ' AND seq<=?'
                args.append(int(until))
            args.append(max(1, min(1000, int(limit))))
            # Bound page content as well as row count. One unusually long record
            # stays whole; pagination/export must never truncate a user's log.
            page, characters = [], 0
            for row in db.execute(sql + ' ORDER BY seq LIMIT ?', args):
                page.append(dict(row))
                characters += len(row['line'])
                if characters >= 256 * 1024:
                    break
            return page

    def latest(self, run_id):
        with self.lock:
            db = self._connect()
            if run_id == '*':
                return db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            return db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE runId=?', (run_id,)).fetchone()[0]

    def flush(self):
        with self.lock:
            if self.db is not None:
                result = self.db.execute('PRAGMA wal_checkpoint(FULL)').fetchone()
                if result[0]:
                    raise OSError('Log checkpoint is busy; archive was preserved')

    def close(self):
        with self.lock:
            if self.db is not None:
                self.flush()
                self.db.close()
                self.db = None
