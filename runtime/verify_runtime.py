"""Live acceptance against an explicitly named disposable WSL distribution."""
import argparse
import json
import pathlib
import subprocess
import sys
import time
import concurrent.futures
import statistics
import tempfile
import io
import gc
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))
from embedded_runtime import EmbeddedRuntime, OwnedDockerClient
from runtime_logs import LogCursor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--distro', required=True)
    parser.add_argument('--stress', action='store_true')
    args = parser.parse_args()
    if not args.distro.startswith('primigenius-runtime-test-'):
        raise SystemExit('This check requires a disposable primigenius-runtime-test-* distro')
    def wsl(command, timeout):
        return subprocess.run(['wsl.exe', '-d', args.distro, '-u', 'root', '--', 'sh', '-c', command],
                              capture_output=True, text=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    manager = SimpleNamespace(app_install_dir=str(ROOT), runtime_channel=args.distro,
                              wsl_distro_name=args.distro, podman_data_dir=str(ROOT / 'build/runtime-work'),
                              _run_wsl=wsl, _connection_state='starting', _service_retry_after=0)
    manager.host_path_to_wsl = lambda p: '/mnt/' + p[0].lower() + p[2:].replace('\\', '/')
    runtime = EmbeddedRuntime(manager)
    client = None
    container = None
    try:
        started = time.monotonic()
        assert runtime.start(), runtime.last_error
        print(json.dumps({'identity': runtime.engine_info, 'startup_seconds': time.monotonic()-started}), flush=True)
        client = OwnedDockerClient(owner=runtime.identity, base_url=runtime.url, version='1.41', timeout=30)
        print(json.dumps({'ping': client.ping(), 'version': client.version()}), flush=True)
        try:
            client.images.get('primigenius-test:fixture')
        except Exception:
            client.api.import_image_from_file(str(ROOT / 'runtime/release/rootfs.tar.gz'), repository='primigenius-test', tag='fixture')
        container = client.containers.create('primigenius-test:fixture', ['sh', '-c', 'printf "hello\\n"; exit 7'],
                                             network_mode='none', name='primigenius-acceptance',
                                             log_config={'Type': 'json-file'})
        container.start()
        result = container.wait(timeout=30)
        logs = container.logs()
        assert result['StatusCode'] == 7, result
        assert logs == b'hello\n', repr(logs)
        container.reload()
        assert container.labels['primigenius.owner'] == runtime.identity
        print(json.dumps({'exit_code': result['StatusCode'], 'logs': logs.decode(), 'owner': True}), flush=True)
        container.remove()
        container = None
        if args.stress:
            report = {}
            def resources():
                import psutil
                host = psutil.Process(runtime.process.pid)
                return {'bridge_rss': host.memory_info().rss, 'bridge_handles': host.num_handles(),
                        'bridge_threads': host.num_threads(), 'python_handles': psutil.Process().num_handles()}
            def short_task(index):
                start = time.monotonic()
                c = client.containers.create('primigenius-test:fixture', ['sh', '-c', 'printf "中文\\n中文\\n"'],
                                             network_mode='none', log_config={'Type': 'json-file'})
                try:
                    c.start()
                    assert c.wait(timeout=30)['StatusCode'] == 0
                    lines = []
                    LogCursor().read(c, lines.append)
                    assert lines == ['中文', '中文'], lines
                finally:
                    c.remove(force=True)
                return time.monotonic() - start
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(short_task, range(20)))
                report['warm_resources'] = resources()
                timings = list(pool.map(short_task, range(100)))
            report['short_tasks'] = {'count': 100, 'concurrency': 4, 'p50_seconds': statistics.median(timings),
                                     'p95_seconds': sorted(timings)[94]}
            gc.collect()
            time.sleep(1)
            report['after_tasks'] = resources()
            print(json.dumps(report), flush=True)
            container = client.containers.run('primigenius-test:fixture',
                ['sh', '-c', 'i=0; while [ "$i" -lt 1000 ]; do printf "中文%s\\n" "$i"; i=$((i+1)); sleep 0.1; done'],
                network_mode='none', detach=True, log_config={'Type': 'json-file'})
            original_id = container.id
            cursor, lines = LogCursor(), []
            reconnects = []
            for index in range(20):
                cursor.read(container, lines.append)
                client.close()
                runtime.stop_bridge()
                began = time.monotonic()
                assert runtime.start(), runtime.last_error
                reconnects.append(time.monotonic() - began)
                client = OwnedDockerClient(owner=runtime.identity, base_url=runtime.url, version='1.41', timeout=30)
                container = client.containers.get(original_id)
                assert container.status == 'running'
                assert len(client.containers.list()) == 1, 'Task duplicated during reconnect'
            began = time.monotonic()
            container.kill()
            container.wait(timeout=15)
            report['cancel_seconds'] = time.monotonic() - began
            cursor.read(container, lines.append)
            assert lines == ['中文' + str(i) for i in range(len(lines))], 'Log resume lost/duplicated records'
            report['reconnects'] = {'count': 20, 'p50_seconds': statistics.median(reconnects), 'log_records': len(lines)}
            report['after_reconnect'] = resources()
            container.remove()
            container = None
            with tempfile.TemporaryDirectory(prefix='中文 mount ', dir=ROOT / 'build/runtime-work') as fixture:
                path = pathlib.Path(fixture)
                (path / '输入.txt').write_text('mount-data', encoding='utf-8')
                result = client.containers.run('primigenius-test:fixture',
                    ['sh', '-c', 'cat /input/输入.txt; if touch /input/forbidden; then exit 9; fi'],
                    network_mode='none', remove=True,
                    volumes={manager.host_path_to_wsl(str(path)): {'bind': '/input', 'mode': 'ro'}})
                assert b'mount-data' in result
            image, build_logs = client.images.build(fileobj=io.BytesIO(b'FROM primigenius-test:fixture\nRUN printf built >/built\n'),
                tag='primigenius-test:built', rm=True, network_mode='none')
            assert client.containers.run(image.id, ['cat', '/built'], remove=True, network_mode='none') == b'built'
            client.images.remove(image.id, force=True)
            report['mount_unicode_readonly'] = True
            report['dockerfile_build'] = True
            (ROOT / 'runtime/validation-live.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report), flush=True)
    finally:
        try:
            if container is not None:
                container.remove(force=True)
        finally:
            if client is not None:
                client.close()
            runtime.stop()


if __name__ == '__main__':
    main()
