"""State/queue/stream compatibility checks; uses disposable fixtures only."""
import ast
import concurrent.futures
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import gzip
import io
import tarfile
import zipfile
import subprocess
import sqlite3
import tracemalloc
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from resource_state import StateDatabase, PersistentRegistry, BoundedWorkers, ExpiringCache, StreamQueue, path_lock
from log_journal import LogJournal
from runtime_logs import LogCursor
from container_metrics import container_metrics
from flask import Flask, jsonify, request, Response


def functions(*names):
    tree = ast.parse((ROOT / 'backend/app.py').read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
    scope = dict(os=os, shutil=shutil, tempfile=tempfile, threading=threading, time=time, json=json,
                 jsonify=jsonify, request=request, Response=Response, urllib=urllib,
                 container_metrics=container_metrics, path_lock=path_lock)
    exec(compile(ast.Module(body=nodes,type_ignores=[]), 'backend/app.py', 'exec'), scope)
    return scope


class CoreResourceTests(unittest.TestCase):
    def test_legacy_full_log_status_is_lossless_without_buffering_all_records(self):
        scope = functions('r_install_status')
        with tempfile.TemporaryDirectory() as folder:
            journal = LogJournal(str(Path(folder) / 'events.sqlite3'))
            lines = ['中文 🧬 ' + 'x' * 2048] * 6000
            journal.append('retained', 'R', lines)
            scope.update(_LOG_JOURNAL=journal, INSTALL_JOBS={'legacy': dict(status='done',exit_code=0,sse_run_id='retained')})
            with Flask(__name__).test_request_context('/r/install-status?job_id=legacy'):
                response = scope['r_install_status']()
                tracemalloc.start()
                size = sum(len(chunk) for chunk in response.response)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            self.assertGreater(size, 24 * 1024 * 1024)
            self.assertLess(peak, 4 * 1024 * 1024)
            with Flask(__name__).test_request_context('/r/install-status?job_id=legacy'):
                payload = scope['r_install_status']().get_json()
            self.assertEqual(payload['log'], ['[R] ' + line for line in lines])
            self.assertEqual(len(payload['events']), 6000)
            self.assertFalse(payload['has_more'])
            journal.close()

    def test_disk_full_cannot_report_an_uncommitted_job_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            database = StateDatabase(str(Path(folder) / 'state.sqlite3'))
            jobs = PersistentRegistry(database, 'r-install', jobs=True)
            jobs['job'] = dict(status='running', packages=['original'])
            db = database.db
            pages = db.execute('PRAGMA page_count').fetchone()[0]
            db.execute(f'PRAGMA max_page_count={pages}')
            record = jobs['job']
            with self.assertRaises(sqlite3.OperationalError):
                record.update(status='done', message='x' * 100000)
            self.assertEqual(record['status'], 'running')
            self.assertNotIn('message', record)
            with self.assertRaises(sqlite3.OperationalError):
                record['message'] = 'x' * 100000
            self.assertNotIn('message', record)
            database.close()
            restored = StateDatabase(database.path)
            result = PersistentRegistry(restored, 'r-install', jobs=True)['job']
            self.assertEqual(result['status'], 'failed')  # interrupted, never replayed
            self.assertEqual(result['packages'], ['original'])
            restored.close()

    def test_real_container_log_route_repeated_close_releases_workers(self):
        scope = functions('container_logs_stream', '_stream_container_logs_thread', '_collect_final_logs')
        workers = []
        real_thread = threading.Thread
        def thread(**kwargs):
            worker = real_thread(**kwargs)
            workers.append(worker)
            return worker
        class Stream:
            def __init__(self): self.closed = False
            def __iter__(self):
                for index in range(100000):
                    if self.closed: break
                    yield f'2026-10-21T00:00:00.000000001Z line {index}\n'.encode()
            def close(self): self.closed = True
        class Container:
            status = 'running'
            def logs(self, **kwargs): return Stream()
            def reload(self): pass
        scope.update(get_docker_client=lambda: SimpleNamespace(containers=SimpleNamespace(get=lambda _: Container())),
                     StreamQueue=StreamQueue, queue=queue, threading=SimpleNamespace(Event=threading.Event, Thread=thread),
                     log=Mock(), docker=SimpleNamespace(errors=SimpleNamespace(NotFound=KeyError)))
        app = Flask(__name__)
        for _ in range(100):
            with app.test_request_context():
                response = scope['container_logs_stream']('test')
                iterator = iter(response.response)
                self.assertIn('hello', next(iterator))
                response.close()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'Closed log subscriber retained a worker')

    def test_slow_log_reader_applies_backpressure_without_dropping_records(self):
        stop = threading.Event()
        stream = StreamQueue(stop, capacity=3, byte_capacity=100)
        expected = ['重复 🧬 ' + str(i) for i in range(1000)]
        def produce():
            for line in expected:
                stream.put({'type': 'log', 'line': line})
        worker = threading.Thread(target=produce)
        worker.start()
        actual = []
        while len(actual) < len(expected):
            with stream.mutex:
                self.assertLessEqual(stream._qsize(), 3)
                self.assertLessEqual(stream.buffered_bytes, 100)
            actual.append(stream.get(timeout=2)['line'])
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(actual, expected)

    def test_closing_full_log_queue_releases_blocked_producer(self):
        stop, finished = threading.Event(), threading.Event()
        stream = StreamQueue(stop, capacity=1)
        stream.put({'line': 'retained'})
        def blocked():
            with self.assertRaises(InterruptedError):
                stream.put({'line': 'waiting'})
            finished.set()
        worker = threading.Thread(target=blocked)
        worker.start()
        self.assertFalse(finished.wait(0.05))
        stop.set()
        worker.join(1)
        self.assertTrue(finished.is_set())
        self.assertFalse(worker.is_alive())

    def test_oversized_log_record_is_whole_and_does_not_allow_a_second_record(self):
        stop = threading.Event()
        stream = StreamQueue(stop, byte_capacity=16)
        text = '中文' * 10000
        stream.put({'line': text})
        self.assertEqual(stream.get()['line'], text)
        self.assertEqual(stream.buffered_bytes, 0)

    def test_journal_page_content_budget_and_streamed_export_are_lossless(self):
        scope = functions('download_run_logs')
        with tempfile.TemporaryDirectory() as folder:
            journal = LogJournal(str(Path(folder) / 'events.sqlite3'))
            lines = ['中文 🧬' * 20000] * 18 + ['x' * 400000, 'tail']
            journal.append('large', 'R', lines)
            page = journal.read('large', limit=1000)
            self.assertLess(len(page), len(lines))
            scope['_LOG_JOURNAL'] = journal
            with Flask(__name__).test_request_context('/logs/download?runId=large'):
                chunks = list(scope['download_run_logs']().response)
            self.assertEqual(''.join(chunks), ''.join('[R] ' + line + '\n' for line in lines))
            self.assertGreater(len(chunks), 2)
            journal.close()

    def test_tail_snapshot_and_follow_share_timestamp_cursor(self):
        calls, lines = [], []
        records = [b'2026-10-21T00:00:00.000000001Z repeat\n'] * 2
        class Stream:
            def __iter__(self): return iter(records)
            def close(self): pass
        def logs(**kw):
            calls.append(kw)
            return Stream()
        cursor = LogCursor()
        container = SimpleNamespace(logs=logs)
        cursor.read(container, lines.append, tail=20)
        cursor.read(container, lines.append, tail=20)
        self.assertEqual(lines, ['repeat', 'repeat'])
        self.assertEqual(calls[0]['tail'], 20)
        self.assertNotIn('tail', calls[1])

    def test_completed_native_processes_are_reaped_without_killing_active_ones(self):
        scope = functions('_native_process_snapshot')
        active = SimpleNamespace(pid=2, poll=lambda: None)
        scope.update(_NATIVE_PROCESSES={1: SimpleNamespace(poll=lambda: 0), 2: active},
                     _NATIVE_PROCESSES_LOCK=threading.Lock())
        self.assertEqual(scope['_native_process_snapshot'](), [active])
        self.assertEqual(list(scope['_NATIVE_PROCESSES']), [2])

    def test_evicted_terminal_jobs_and_tokens_remain_queryable_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'state.sqlite3')
            database = StateDatabase(path)
            jobs = PersistentRegistry(database, 'r-install', capacity=8, jobs=True)
            tokens = PersistentRegistry(database, 'webapp-file', capacity=8)
            for index in range(600):
                jobs[str(index)] = dict(status='pending', sse_run_id='run-'+str(index), packages=['demo'])
                jobs[str(index)].update(status='done', exit_code=0)
                tokens[str(index)] = dict(path='legacy plugin/input'+str(index))
            self.assertLessEqual(len(jobs.cache), 8)
            self.assertLessEqual(len(tokens.cache), 8)
            self.assertEqual(jobs['0']['sse_run_id'], 'run-0')
            self.assertEqual(tokens['0']['path'], 'legacy plugin/input0')
            database.close()
            restored = StateDatabase(path)
            self.assertEqual(PersistentRegistry(restored, 'r-install', jobs=True)['0']['status'], 'done')
            self.assertEqual(PersistentRegistry(restored, 'webapp-file')['0']['path'], 'legacy plugin/input0')
            restored.close()

    def test_crash_recovery_never_reexecutes_install_or_discards_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'state.sqlite3')
            database = StateDatabase(path)
            jobs = PersistentRegistry(database, 'r-install', jobs=True)
            jobs['interrupted'] = dict(status='running', sse_run_id='retained-log', packages=['user-package'])
            database.close()
            restored = StateDatabase(path)
            jobs = PersistentRegistry(restored, 'r-install', jobs=True)
            self.assertEqual(jobs.active_count(), 0)
            self.assertEqual(jobs['interrupted']['status'], 'failed')
            self.assertEqual(jobs['interrupted']['sse_run_id'], 'retained-log')
            self.assertEqual(jobs['interrupted']['packages'], ['user-package'])
            restored.close()

    def test_queue_has_one_writer_and_bounded_waiting_work(self):
        workers = BoundedWorkers('qa-install', capacity=3)
        entered, release = threading.Event(), threading.Event()
        seen = []
        workers.submit(lambda: (entered.set(), release.wait(5), seen.append('first')))
        self.assertTrue(entered.wait(2))
        for index in range(3): workers.submit(seen.append, index)
        with self.assertRaises(queue.Full): workers.submit(seen.append, 'overflow')
        release.set()
        workers.queue.join()
        self.assertEqual(seen, ['first',0,1,2])

    def test_worker_exception_does_not_stop_later_jobs(self):
        workers = BoundedWorkers('qa-errors')
        seen = []
        with patch('logging.exception') as error:
            workers.submit(lambda: 1/0)
            workers.submit(seen.append, 'after-error')
            workers.queue.join()
        self.assertEqual(seen, ['after-error'])
        error.assert_called_once()

    def test_cache_cap_and_expiry_do_not_touch_container_data(self):
        cache = ExpiringCache(capacity=4,ttl=5)
        for index in range(20): cache[index] = dict(stats={'id':index}, ts=time.time())
        self.assertEqual(len(cache), 4)
        cache['old'] = dict(stats={},ts=time.time()-6)
        self.assertIsNone(cache.get('old'))

    def test_multipart_unicode_and_long_log_records_are_lossless(self):
        text = '2026-10-21T00:00:00.000000001Z ' + '中文 🧬' * 30000 + '\n'
        raw = (text + text).encode()
        class Stream:
            closed = False
            def __iter__(self):
                yield b''
                for start in range(0,len(raw),8191): yield raw[start:start+8191]
                yield b''
            def close(self): self.closed = True
        stream = Stream()
        lines = []
        cursor = LogCursor()
        cursor.read(SimpleNamespace(logs=lambda **kwargs: stream),lines.append)
        self.assertEqual(lines, [text.split(' ',1)[1].rstrip('\n')]*2)
        self.assertEqual(cursor.occurrence, 2)
        self.assertTrue(stream.closed)

    def test_stream_reference_cleared_even_when_close_raises(self):
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([]))
        stream.close.side_effect = OSError('disconnected')
        reference = [None]
        with self.assertRaises(OSError): LogCursor().read(SimpleNamespace(logs=lambda **kw: stream),lambda _: None,stream_ref=reference)
        self.assertIsNone(reference[0])

    def test_stats_polling_and_stream_use_the_same_payload_and_close_upstream(self):
        raw = dict(cpu_stats={'cpu_usage':{'total_usage':20},'system_cpu_usage':100,'online_cpus':2},
                   precpu_stats={'cpu_usage':{'total_usage':10},'system_cpu_usage':60},
                   networks={'eth0':{'rx_bytes':3,'tx_bytes':4}},memory_stats={'usage':2**20,'limit':4*2**20})
        scope = functions('container_stats','container_stats_stream')
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([raw]))
        container = Mock(id='container')
        container.stats.side_effect = lambda **kwargs: stream if kwargs.get('stream') else raw
        scope.update(get_docker_client=lambda:SimpleNamespace(containers=SimpleNamespace(get=lambda _:container)),
                     _CONTAINER_LAST_STATS_LOCK=threading.Lock(),_CONTAINER_LAST_STATS=ExpiringCache())
        app = Flask(__name__)
        with app.test_request_context():
            expected = scope['container_stats']('container').get_json()['stats']
            response = scope['container_stats_stream']('container')
            item = next(iter(response.response))
            self.assertEqual(json.loads(item.removeprefix('data: '))['stats'],expected)
            response.close()
            stream.close.assert_called_once()

    def test_sse_close_before_first_read_releases_subscription(self):
        scope = functions('podman_events_stream')
        lock = threading.Lock()
        upstream = Mock()
        clients = []
        scope.update(_ensure_docker_events_thread=Mock(),queue=queue,json=json,
                     _DOCKER_EVENTS_CLIENTS=clients,_DOCKER_EVENTS_COND=threading.Condition(lock),
                     _DOCKER_EVENTS_STREAM=upstream,_DOCKER_EVENTS_GENERATION=0)
        with Flask(__name__).test_request_context():
            response = scope['podman_events_stream']()
            self.assertEqual(len(clients),1)
            response.close()
            self.assertEqual(clients,[])
            upstream.close.assert_called_once()

    def test_failed_binary_extraction_preserves_working_plugin(self):
        scope = functions('_install_plugin_binary_impl')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'bin'
            target.mkdir()
            (target/'old-tool').write_bytes(b'working old plugin')
            scope.update(_populate_plugin_binary=Mock(side_effect=ValueError('corrupt package')),invalidate_plugin_cache=Mock(),log=Mock())
            with self.assertRaises(ValueError): scope['_install_plugin_binary_impl']({},'bad.zip',str(target))
            self.assertEqual((target/'old-tool').read_bytes(), b'working old plugin')
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['bin'])

    def test_binary_publish_failure_rolls_back_old_tree(self):
        scope = functions('_install_plugin_binary_impl')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'bin'
            target.mkdir()
            (target/'old-tool').write_bytes(b'old')
            def populate(cfg,package,stage,progress): (Path(stage)/'new-tool').write_bytes(b'new')
            scope.update(_populate_plugin_binary=populate,invalidate_plugin_cache=Mock(),log=Mock())
            rename = os.rename
            def fail_publish(source,destination):
                if '.bin-install-' in str(source) and not str(source).endswith('.previous'): raise OSError('file locked')
                rename(source,destination)
            with patch.object(os,'rename',side_effect=fail_publish):
                with self.assertRaises(OSError): scope['_install_plugin_binary_impl']({},'new.zip',str(target))
            self.assertEqual((target/'old-tool').read_bytes(),b'old')

    def test_concurrent_double_gzip_installs_use_independent_temporary_files(self):
        scope=functions('_extract_plugin_payload_with_progress')
        scope.update(tarfile=tarfile,zipfile=zipfile,subprocess=subprocess,_HIDDEN_SUBPROCESS_KWARGS={})
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def extract(index):
                data=('unique plugin '+str(index)).encode()
                raw=io.BytesIO()
                with tarfile.open(fileobj=raw,mode='w') as archive:
                    entry=tarfile.TarInfo('binary');entry.size=len(data);archive.addfile(entry,io.BytesIO(data))
                package=root/f'{index}.tar.gz';package.write_bytes(gzip.compress(gzip.compress(raw.getvalue())))
                target=root/f'bin-{index}';target.mkdir()
                scope['_extract_plugin_payload_with_progress'](str(package),str(target))
                self.assertEqual((target/'binary').read_bytes(),data)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(extract,range(12)))
            self.assertFalse(list(root.glob('*.decompressed.tar*')))

    def test_local_api_accepts_legacy_clients_but_denies_remote_browser(self):
        scope = functions('_guard_runtime_shutdown')
        scope['_SHUTDOWN_REQUESTED'] = threading.Event()
        app = Flask(__name__)
        for origin in ('','null','http://127.0.0.1:12345','http://localhost:12345'):
            with app.test_request_context('http://127.0.0.1:12345/r/list-packages',headers={'Origin':origin}):
                self.assertIsNone(scope['_guard_runtime_shutdown']())
        for url,origin in [('http://127.0.0.1:12345/r/delete-packages','https://external.example'),
                           ('http://127.0.0.1:12345/r/delete-packages','http://localhost:invalid'),
                           ('http://127.0.0.1:12345/r/delete-packages','http://[broken'),
                           ('http://external.example:12345/r/delete-packages','null')]:
            with app.test_request_context(url,method='POST',headers={'Origin':origin}):
                self.assertEqual(scope['_guard_runtime_shutdown']()[1],403)


if __name__ == '__main__': unittest.main()
