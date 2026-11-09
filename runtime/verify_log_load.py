"""Compare short real-container tasks with/without persistent log indexing."""
import json
import pathlib
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from embedded_runtime import EmbeddedRuntime, OwnedDockerClient
from log_journal import LogJournal
from runtime_logs import LogCursor


def main():
    distro = 'primigenius-runtime-test-20261017'
    with tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='log-live-') as folder:
        def wsl(command, timeout):
            return subprocess.run(['wsl.exe', '-d', distro, '-u', 'root', '--', 'sh', '-c', command],
                                  capture_output=True, text=True, timeout=timeout,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        manager = SimpleNamespace(app_install_dir=folder, runtime_channel='log-load-test',
                                  wsl_distro_name=distro, podman_data_dir=folder, _run_wsl=wsl,
                                  _connection_state='starting', _service_retry_after=0)
        manager.host_path_to_wsl = lambda p: '/mnt/' + p[0].lower() + p[2:].replace(chr(92), '/')
        runtime = EmbeddedRuntime(manager)
        runtime.bundle = str(ROOT / 'runtime/release')
        journal = LogJournal(str(pathlib.Path(folder) / 'logs.sqlite3'))
        client = None
        container = None
        timings = {'baseline': [], 'journal': []}
        counts = {'baseline': 0, 'journal': 0}
        try:
            if not runtime.start():
                raise RuntimeError(runtime.last_error + '\n' + '\n'.join(p.read_text(errors='replace') for p in pathlib.Path(folder).rglob('runtime-bridge.log')))
            client = OwnedDockerClient(owner=runtime.identity, base_url=runtime.url, version='1.41', timeout=30)
            # Existing fixture only: do not download or change images for this check.
            client.images.get('primigenius-test:fixture')
            for turn in range(20):
                order = ('baseline', 'journal') if turn % 2 == 0 else ('journal', 'baseline')
                for mode in order:
                    started = time.perf_counter()
                    container = client.containers.create('primigenius-test:fixture',
                        ['sh', '-c', 'i=0; while [ "$i" -lt 100 ]; do printf "repeat 中文\\n"; i=$((i+1)); done'],
                        network_mode='none', log_config={'type': 'json-file'})
                    container.start()
                    result = container.wait(timeout=30)
                    if result['StatusCode'] != 0:
                        raise RuntimeError(str(result))
                    lines = []
                    def emit(line):
                        if mode == 'journal':
                            journal.append('live', 'R', [line])
                        lines.append(line)
                    LogCursor().read(container, emit)
                    if lines != ['repeat 中文'] * 100:
                        raise RuntimeError('Container log records differ')
                    if mode == 'journal':
                        journal.flush()
                    counts[mode] += len(lines)
                    container.remove()
                    container = None
                    timings[mode].append(time.perf_counter() - started)
            if journal.latest('live') != 2000:
                raise RuntimeError('Journal records lost')
            report = {mode: {'tasks': len(values), 'records': counts[mode],
                             'p50_ms': statistics.median(values)*1000,
                             'p95_ms': sorted(values)[int(len(values)*0.95)-1]*1000}
                      for mode, values in timings.items()}
            report['scope'] = '20 tasks per variant; alternating order; 100 repeated UTF-8 lines per task; single machine'
            (ROOT / 'runtime/validation-log-load.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps(report, indent=2))
        finally:
            if container is not None:
                container.remove(force=True)
            if client is not None:
                client.close()
            runtime.stop()
            journal.close()


if __name__ == '__main__':
    main()
