"""Regression checks for local status and lossless, bounded log replay."""
import ast
import os
import concurrent.futures
import pathlib
import sys
import tempfile
import time
import unittest
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from log_journal import LogJournal
from runtime_logs import LogCursor
from flask import Flask, jsonify, request, Response


def functions(*names):
    tree = ast.parse((ROOT / 'backend/app.py').read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
    scope = dict(jsonify=jsonify, request=request, Response=Response, time=time)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'backend/app.py', 'exec'), scope)
    return scope


class StatusAndLogTests(unittest.TestCase):
    def test_recovered_manager_bypasses_an_older_sdk_retry_delay(self):
        import threading
        scope = functions('get_docker_client')
        client = Mock()
        manager = Mock()
        manager._connection_state = 'backoff'
        manager.is_engine_ready.return_value = True
        manager.get_docker_host_env.return_value = ''
        manager.get_podman_client.return_value = client
        scope.update(os=os, _podman_client_lock=threading.RLock(), _docker_client=None,
                     _docker_client_last_ping=0, _docker_reconnecting=False,
                     _docker_reconnect_after=time.monotonic()+60, _podman_mgr=manager,
                     _get_podman_manager=lambda: manager, _podman_async_running=False,
                     _record_docker_reconnect_success=Mock(), _trigger_cache_preload=Mock())
        self.assertIsNone(scope['get_docker_client']())
        manager.get_podman_client.assert_not_called()
        manager._connection_state = 'ready'
        self.assertIs(scope['get_docker_client'](), client)
        manager.get_podman_client.assert_called_once()

    def test_plugin_warmup_and_requests_share_one_scan_and_invalidate_safely(self):
        import threading
        scope = functions('load_all_plugins', 'invalidate_plugin_cache')
        with tempfile.TemporaryDirectory() as directory:
            plugin = pathlib.Path(directory) / 'fixture'
            plugin.mkdir()
            (plugin / 'config.json').write_text('{}')
            read = Mock(return_value={'id': 'fixture'})
            scope.update(os=os, PLUGINS_DIR=directory, _plugin_cache_lock=threading.RLock(),
                         _load_json_file=read, log=Mock())
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                values = list(pool.map(lambda _: scope['load_all_plugins'](), range(100)))
            self.assertEqual(read.call_count, 1)
            self.assertTrue(all(value is values[0] for value in values))
            scope['invalidate_plugin_cache']()
            self.assertIn('fixture', scope['load_all_plugins']())
            self.assertEqual(read.call_count, 2)

    def test_actual_application_log_initialization_keeps_only_each_launch(self):
        from resource_state import StateDatabase, PersistentRegistry
        tree = ast.parse((ROOT / 'backend/app.py').read_text(encoding='utf-8'))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == '_LOG_JOURNAL' for target in node.targets))
        with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='log-launch-') as folder:
            directory = pathlib.Path(folder)
            state = StateDatabase(str(directory / 'logs/state.sqlite3'))
            PersistentRegistry(state, 'r-install')['keep-job'] = {'status': 'done', 'packages': ['keep-package']}
            PersistentRegistry(state, 'webapp-file')['keep-token'] = {'path': 'existing input'}
            state.close()
            state_bytes = (directory / 'logs/state.sqlite3').read_bytes()
            old = LogJournal(str(directory / 'logs/events.sqlite3'))
            old.append('old-plugin', 'R', ['previous launch'])
            previous_seq = old.latest('*')
            old.close()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('PRIMIGENIUS_LOG_DIR', None)
                for turn in range(3):
                    scope = dict(os=os, APP_INSTALL_DIR=folder, LogJournal=LogJournal, __name__='__main__')
                    exec(compile(ast.Module(body=[assignment], type_ignores=[]), 'app-log-init', 'exec'), scope)
                    journal = scope['_LOG_JOURNAL']
                    self.assertEqual(journal.read('*'), [])
                    event = journal.append('current', 'R', [f'launch {turn}'])[0]
                    self.assertGreater(event['seq'], previous_seq)
                    previous_seq = event['seq']
                    journal.close()
                    self.assertEqual([e['line'] for e in journal.read('*')], [f'[R] launch {turn}'])
                    journal.close()
                    self.assertEqual((directory / 'logs/state.sqlite3').read_bytes(), state_bytes)

    def test_importing_backend_for_tools_does_not_clear_an_application_log(self):
        tree = ast.parse((ROOT / 'backend/app.py').read_text(encoding='utf-8'))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == '_LOG_JOURNAL' for target in node.targets))
        with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='log-import-') as folder:
            scope = dict(os=os, APP_INSTALL_DIR=folder, LogJournal=LogJournal, __name__='app')
            with mock.patch.dict(os.environ, {'PRIMIGENIUS_LOG_DIR': folder}):
                original = LogJournal(str(pathlib.Path(folder) / 'events.sqlite3'))
                original.append('active', 'R', ['this launch'])
                original.close()
                exec(compile(ast.Module(body=[assignment], type_ignores=[]), 'app-log-init', 'exec'), scope)
                journal = scope['_LOG_JOURNAL']
                self.assertEqual(journal.read('active')[0]['line'], '[R] this launch')
                journal.close()

    def test_current_session_history_and_export_exclude_previous_runs_without_deleting_them(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='log-session-') as folder:
            path = str(pathlib.Path(folder) / 'events.sqlite3')
            previous = LogJournal(path)
            previous.append('old-plugin', 'R', ['old analysis', 'old result.svg'])
            previous.close()
            journal = LogJournal(path)
            self.assertEqual(journal.session_start_seq, 2)
            journal.append('__system__', 'Linux', ['current startup'])
            journal.append('new-plugin', 'R', ['current task', 'current task'])
            scope = functions('log_history', 'download_run_logs')
            scope['_LOG_JOURNAL'] = journal
            app = Flask(__name__)
            with app.test_request_context('/logs/history?runId=*&scope=session&limit=1'):
                first = scope['log_history']().get_json()
            self.assertEqual([event['seq'] for event in first['events']], [3])
            self.assertTrue(first['has_more'])
            with app.test_request_context('/logs/history?runId=*&scope=session&afterSeq=3'):
                rest = scope['log_history']().get_json()
            self.assertEqual([event['line'] for event in rest['events']], ['[R] current task'] * 2)
            with app.test_request_context('/logs/download?runId=*&scope=session'):
                response = scope['download_run_logs']()
                journal.append('__system__', 'Linux', ['after download snapshot'])
                exported = response.get_data(as_text=True)
            self.assertNotIn('old analysis', exported)
            self.assertNotIn('after download snapshot', exported)
            self.assertEqual(exported.count('current task'), 2)
            with app.test_request_context('/logs/history?runId=old-plugin'):
                old = scope['log_history']().get_json()
            self.assertEqual(len(old['events']), 2)
            with app.test_request_context('/logs/download?runId=*'):
                self.assertIn('old analysis', scope['download_run_logs']().get_data(as_text=True))
            journal.close()
            self.assertEqual(journal.session_start_seq, 2)  # reconnect within this session
            journal.close()

    def test_empty_new_session_does_not_offer_pages_from_previous_sessions(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='log-session-') as folder:
            path = str(pathlib.Path(folder) / 'events.sqlite3')
            previous = LogJournal(path)
            previous.append('old', 'R', ['retained'])
            previous.close()
            journal = LogJournal(path)
            scope = functions('log_history', 'download_run_logs')
            scope['_LOG_JOURNAL'] = journal
            app = Flask(__name__)
            with app.test_request_context('/logs/history?runId=*&scope=session'):
                payload = scope['log_history']().get_json()
            self.assertEqual(payload['events'], [])
            self.assertFalse(payload['has_more'])
            with app.test_request_context('/logs/download?runId=*&scope=session'):
                self.assertEqual(scope['download_run_logs']().get_data(), b'')
            self.assertEqual(journal.latest('old'), 1)
            journal.close()

    def test_package_cache_coalesces_scans_and_invalidates_after_mutation(self):
        import os
        import threading
        scope = functions('r_list_packages', '_r_list_packages_locked', '_bump_r_libs_revision')
        app = Flask(__name__)
        with tempfile.TemporaryDirectory() as root:
            scope.update(os=os, _r_packages_cache={'packages': None, 'time': 0},
                         _r_packages_cache_lock=threading.RLock(), _r_libs_revision=0,
                         _R_PKG_CACHE_TTL=30, R_LIBS_DIR=root, _R_IMAGE_LIBRARY_PACKAGES=set(),
                         _scan_r_libs_package_set=lambda: None, log=lambda _: None)
            def read():
                with app.test_request_context():
                    return scope['r_list_packages']().get_json()
            real_listdir = os.listdir
            with mock.patch.object(os, 'listdir', wraps=real_listdir) as scan:
                with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                    results = list(pool.map(lambda _: read(), range(24)))
                self.assertEqual(1, scan.call_count)
            self.assertTrue(all(result['packages'] == [] for result in results))
            package = pathlib.Path(root) / 'demo'
            package.mkdir()
            (package / 'DESCRIPTION').write_text('Package: demo\nVersion: 1.0\n')
            scope['_bump_r_libs_revision']('partial install')
            current = read()
            self.assertEqual('demo', current['packages'][0]['package'])
            self.assertEqual(1, current['revision'])
            (package / 'DESCRIPTION').unlink()
            scope['_bump_r_libs_revision']('delete')
            self.assertEqual([], read()['packages'])

    def test_stale_image_response_explicitly_requests_followup(self):
        import threading
        scope = functions('podman_list_images')
        scope.update(threading=threading, _CACHE_TTL=30,
                     _images_cache={'data': {'status': 'success', 'images': [{'id': 'old'}]}, 'time': 0},
                     _refresh_images_cache=Mock())
        with Flask(__name__).test_request_context(), mock.patch.object(threading, 'Thread') as worker:
            result = scope['podman_list_images']().get_json()
        self.assertTrue(result['refreshing'])
        self.assertEqual('old', result['images'][0]['id'])
        worker.return_value.start.assert_called_once()

    def test_auto_setup_preserves_wsl_error_and_existing_storage(self):
        from podman_manager import PodmanManager
        manager = object.__new__(PodmanManager)
        manager.wsl_distro_name = 'test-existing'
        import threading
        manager._service_lock = threading.RLock()
        manager._wsl_distro_cache = {'value': None, 'time': 0}
        manager.runtime = SimpleNamespace(last_error='')
        manager._connection_state = 'starting'
        manager.is_podman_available = Mock(return_value=True)
        manager._prepare_wsl_registration = Mock(return_value=True)
        manager.is_engine_ready = Mock(return_value=False)
        manager._is_wsl_distro_registered = Mock(return_value=True)
        manager._run_host_command = Mock(return_value=SimpleNamespace(
            returncode=1, stderr='', stdout='Wsl/Service/ReadDistroConfig/E_UNEXPECTED: BasePath'))
        manager.machine_init = Mock()
        manager._start_wsl_podman_service = Mock()
        ok, message = manager._auto_setup_impl()
        self.assertFalse(ok)
        self.assertIn('BasePath', message)
        self.assertIn('test-existing', message)
        manager.machine_init.assert_not_called()
        manager._start_wsl_podman_service.assert_not_called()
        self.assertFalse(manager._is_wsl_distro_functional())
        manager._run_host_command.assert_called_once()
        manager._run_host_command.return_value = SimpleNamespace(returncode=0, stdout='DISTRO_OK', stderr='')
        self.assertTrue(manager._is_wsl_distro_functional(force=True))
        self.assertEqual('', manager._wsl_distro_error)

    def test_wsl_failure_preserves_diagnostic_without_launching_bridge(self):
        from embedded_runtime import EmbeddedRuntime
        import threading
        runtime = object.__new__(EmbeddedRuntime)
        runtime._lock = threading.RLock()
        runtime.ping = Mock(return_value=False)
        runtime.stop_bridge = Mock()
        runtime.validate = Mock(return_value={'podman_version': '5.8.5'})
        runtime.manager = SimpleNamespace(_run_wsl=Mock(return_value=SimpleNamespace(
            returncode=1, stderr='', stdout='WSL BasePath is missing')))
        with mock.patch('embedded_runtime.subprocess.Popen') as spawn:
            self.assertFalse(runtime.start())
        spawn.assert_not_called()
        self.assertIn('BasePath', runtime.last_error)

    def test_missing_wsl_disk_is_actionable_without_reset_or_reimport(self):
        from podman_manager import PodmanManager
        manager = object.__new__(PodmanManager)
        manager.wsl_distro_name = 'test-existing'
        import threading
        manager._service_lock = threading.RLock()
        manager._wsl_distro_cache = {'value': None, 'time': 0}
        manager._connection_state = 'starting'
        manager.runtime = SimpleNamespace(last_error='')
        manager.is_podman_available = Mock(return_value=True)
        manager._prepare_wsl_registration = Mock(return_value=True)
        manager.is_engine_ready = Mock(return_value=False)
        manager._is_wsl_distro_registered = Mock(return_value=True)
        manager._run_host_command = Mock(return_value=SimpleNamespace(returncode=1, stderr='',
            stdout='Wsl/Service/CreateInstance/MountDisk/HCS/ERROR_PATH_NOT_FOUND: D:\\old\\ext4.vhdx'))
        manager.machine_init = Mock()
        manager._start_wsl_podman_service = Mock()
        ok, message = manager._auto_setup_impl()
        self.assertFalse(ok)
        self.assertTrue(manager._wsl_storage_missing)
        self.assertEqual(manager._connection_state, 'attention')
        self.assertIn('.primigenius-preserved', message)
        self.assertIn('Do not reset or unregister', message)
        self.assertNotIn('Data was preserved', message)
        manager.machine_init.assert_not_called()
        manager._start_wsl_podman_service.assert_not_called()
        manager._run_host_command.return_value = SimpleNamespace(returncode=0, stdout='DISTRO_OK', stderr='')
        self.assertTrue(manager._is_wsl_distro_functional(force=True))
        self.assertFalse(manager._wsl_storage_missing)

    def test_missing_disk_does_not_trigger_repeated_automatic_start_attempts(self):
        scope = functions('_ensure_podman_running')
        manager = SimpleNamespace(_wsl_storage_missing=True,
            is_engine_ready=Mock(return_value=False), auto_setup=Mock(return_value=(False, 'missing disk')))
        scope.update(_get_podman_manager=lambda: manager, _podman_start_fail_count=0,
            _podman_permanently_failed=False, _podman_async_running=False, _podman_start_retry_after=0,
            _PODMAN_MAX_START_RETRIES=3, log=Mock())
        for _ in range(10):
            self.assertFalse(scope['_ensure_podman_running']())
        manager.auto_setup.assert_called_once()
        manager.is_engine_ready.return_value = True
        self.assertTrue(scope['_ensure_podman_running']())
        self.assertEqual(scope['_podman_start_retry_after'], 0)

    def test_r_preparation_reports_missing_disk_without_waiting_on_ipc(self):
        scope = functions('_offline_r_client')
        scope['_get_podman_manager'] = lambda: SimpleNamespace(
            _wsl_storage_missing=True, runtime=SimpleNamespace(last_error='restore the missing WSL disk'))
        with mock.patch('embedded_runtime.OwnedDockerClient') as client:
            with self.assertRaisesRegex(RuntimeError, 'restore the missing WSL disk'):
                scope['_offline_r_client']()
            client.assert_not_called()

    def test_explicit_auto_repo_selection_schedules_background_probe(self):
        scope = functions('r_set_repo')
        save, schedule = Mock(), Mock()
        scope.update(R_RUNTIME={}, _save_r_runtime_state=save, _schedule_r_repo_probe=schedule,
                     _r_repo_snapshot=lambda: {'repo_mode': 'auto', 'repo_pending': True})
        app = Flask(__name__)
        with app.test_request_context('/r/set-repo', method='POST', json={'repo': 'auto'}):
            result = scope['r_set_repo']().get_json()
        save.assert_called_once_with(repo_mode='auto')
        schedule.assert_called_once_with()
        self.assertTrue(result['repo_pending'])

    def test_local_status_only_reads_preparation_snapshot(self):
        scope = functions('r_check_runtime')
        environment = Mock()
        scope.update(_offline_r_environment=lambda: environment,
                     _r_repo_snapshot=lambda: {'repo_mode': 'auto', 'repo_pending': True},
                     R_DOCKER_IMAGE='base', R_CUSTOM_IMAGE='custom', R_VERSION='4',
                     BIOCONDUCTOR_VERSION='3', R_LIBRARY_GENERATION='4-3')
        app = Flask(__name__)
        with app.test_request_context():
            for state, expected in [('pending','image_missing'),('preparing','error'),('attention','error'),('ready','success')]:
                environment.snapshot.return_value = dict(state=state, ready=state=='ready', phase=state,
                    error='broken' if state=='attention' else None, image_id='sha256:id' if state=='ready' else None)
                result = scope['r_check_runtime']().get_json()
                self.assertEqual(expected, result['status'])
                self.assertEqual(state, result['runtime_state'])
        environment.ensure.assert_not_called()
        environment.start.assert_not_called()

    def test_readonly_repo_snapshot_does_not_wait_for_probe(self):
        scope = functions('_r_repo_snapshot')
        scope.update(R_RUNTIME={'repos': 'auto'}, _R_REPO_AUTO_CACHE={'selection': None})
        self.assertTrue(scope['_r_repo_snapshot']()['repo_pending'])
        scope['_R_REPO_AUTO_CACHE']['selection'] = {'cran': 'c', 'bioc': 'b'}
        self.assertEqual('c', scope['_r_repo_snapshot']()['repos'])

    def test_large_history_survives_reopen_concurrency_and_pagination(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='log-test-') as folder:
            path = str(pathlib.Path(folder) / 'events.sqlite3')
            journal = LogJournal(path)
            started = time.perf_counter()
            def write(worker):
                for batch in range(60):
                    journal.append(f'run-{worker}', 'R', ['重复 UTF-8 🧬'] * 100)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write, range(4)))
            self.assertEqual(24000, journal.latest('*'))
            journal.close()
            journal = LogJournal(path)
            for worker in range(4):
                after, count = 0, 0
                while True:
                    page = journal.read(f'run-{worker}', after, 137)
                    if not page:
                        break
                    self.assertLessEqual(len(page), 137)
                    self.assertTrue(all(event['line'] == '[R] 重复 UTF-8 🧬' for event in page))
                    self.assertGreater(page[0]['seq'], after)
                    count += len(page)
                    after = page[-1]['seq']
                self.assertEqual(6000, count)
            event = journal.append('run-0', 'R', ['after restart'])[0]
            self.assertEqual(24001, event['seq'])
            journal.close()
            print(f'24,000 committed events, replay and reopen: {time.perf_counter()-started:.3f}s')

    def test_abrupt_process_exit_retains_committed_wal_records(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='log-crash-') as folder:
            path = str(pathlib.Path(folder) / 'events.sqlite3')
            code = (f'import os,sys; sys.path.insert(0,{str(ROOT / "backend")!r}); '
                    f'from log_journal import LogJournal; j=LogJournal({path!r}); '
                    "[j.append('crash','R',['中文 repeated']) for _ in range(1500)]; os._exit(0)")
            subprocess.run([sys.executable, '-B', '-c', code], check=True, timeout=30)
            journal = LogJournal(path)
            self.assertEqual(1500, journal.latest('crash'))
            self.assertEqual(500, len(journal.read('crash', 1000)))
            journal.close()

    def test_sink_failure_does_not_advance_container_cursor(self):
        cursor = LogCursor()
        class Stream:
            def __iter__(self):
                yield b'2026-10-18T00:00:00.000000001Z repeat\n'
            def close(self):
                pass
        container = SimpleNamespace(logs=lambda **kw: Stream())
        with self.assertRaises(OSError):
            cursor.read(container, Mock(side_effect=OSError('disk full')))
        self.assertEqual('', cursor.timestamp)
        lines = []
        cursor.read(container, lines.append)
        cursor.read(container, lines.append)
        self.assertEqual(['repeat'], lines)

    def test_sse_resume_and_export_include_duplicate_lines(self):
        import json
        import threading
        with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='log-test-') as folder:
            journal = LogJournal(str(pathlib.Path(folder) / 'events.sqlite3'))
            journal.append('r', 'R', ['same', 'same', '中文'])
            scope = functions('stream_run_logs', 'download_run_logs', 'log_history')
            scope.update(_LOG_JOURNAL=journal, _LOG_EVENT_COND=threading.Condition(), json=json)
            app = Flask(__name__)
            with app.test_request_context('/logs/stream?runId=r', headers={'Last-Event-ID': '1'}):
                response = scope['stream_run_logs']()
                stream = iter(response.response)
                next(stream)
                self.assertIn('id: 2\n', next(stream))
                self.assertIn('id: 3\n', next(stream))
                response.close()
            with app.test_request_context('/logs/download?runId=r'):
                text = scope['download_run_logs']().get_data(as_text=True)
                self.assertEqual('[R] same\n[R] same\n[R] 中文\n', text)
            with app.test_request_context('/logs/history?runId=r&limit=1'):
                page = scope['log_history']().get_json()
                self.assertEqual(1, len(page['events']))
                self.assertTrue(page['has_more'])
            journal.close()


if __name__ == '__main__':
    unittest.main()
