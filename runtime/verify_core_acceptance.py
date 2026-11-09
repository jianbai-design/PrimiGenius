"""Measured resource acceptance against a disposable copy of the real HTTP backend.

No user backend, package library, container engine or installation is started.
The output is evidence, not a claim about all historical Windows installations.
"""
import argparse
import concurrent.futures
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def distribution(values):
    values = sorted(values)
    return dict(count=len(values), p50_ms=statistics.median(values),
                p95_ms=values[max(0, (len(values) * 95 + 99) // 100 - 1)], max_ms=max(values))


def worker(folder, duration, output):
    import psutil  # Acceptance tooling only; not an application dependency.
    os.environ['PRIMIGENIUS_LOG_DIR'] = str(folder / 'logs')
    sys.path.insert(0, str(folder / 'backend'))
    started = time.perf_counter()
    import app as backend
    from resource_state import StreamQueue, StateDatabase, PersistentRegistry
    from werkzeug.serving import make_server
    import_seconds = time.perf_counter() - started
    process = psutil.Process()
    server = make_server('127.0.0.1', 0, backend.app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    timings = {'history': [], 'r_packages_cached': [], 'r_install_status': []}
    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return json.load(response)
    def measure(name, path):
        begin = time.perf_counter()
        payload = get(path)
        timings[name].append((time.perf_counter() - begin) * 1000)
        assert payload['status'] == 'success', payload
        return payload
    def resources():
        gc.collect()
        cpu = process.cpu_times()
        return dict(elapsed_seconds=round(time.perf_counter() - started, 3),
                    rss_bytes=process.memory_info().rss, handles=process.num_handles(),
                    threads=process.num_threads(), cpu_seconds=cpu.user + cpu.system)

    # Actual old-style files, without renaming their library or plugin locations.
    original = {}
    for relative, content in {
        'plugins/r/existing/config.json': b'{"id":"existing","type":"R_panel","packages":["custom"]}',
        'outputs/old result.tsv': b'gene\tvalue\na\t1\n',
        'podman-config/machine-runtime.json': b'{"identity":"legacy-do-not-replace"}',
        'r_libs_r4_6_1-bioc3_23/custom/DESCRIPTION': b'Package: custom\nVersion: 1.0\nBuilt: R 4.6.1; x86_64-pc-linux-gnu\n',
    }.items():
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        original[relative] = hashlib.sha256(content).hexdigest()
    for index in range(10000):
        for cache in (backend._CONTAINER_LAST_STATS, backend._PULL_SOURCE_STATS,
                      backend._REGISTRY_PROBE_CACHE, backend._HUB_MANIFEST_CHECK_CACHE):
            cache[str(index)] = dict(ts=time.time(), stats={})
    jobs = backend.INSTALL_JOBS
    jobs['legacy'] = dict(status='done', exit_code=0, sse_run_id='acceptance', packages=['custom'])
    backend._LOG_JOURNAL.append('acceptance', 'R', ['重复 中文 🧬'] * 100)
    for _ in range(40):
        measure('history', '/logs/history?runId=acceptance&limit=100')
        measure('r_packages_cached', '/r/list-packages')
        measure('r_install_status', '/r/install-status?job_id=legacy&afterSeq=0')
    for values in timings.values(): values.clear()
    warm = resources()
    samples, cycles, logs, next_sample = [warm], 0, 100, time.monotonic() + 10
    deadline = time.monotonic() + duration
    try:
        while time.monotonic() < deadline:
            key = str(cycles)
            jobs[key] = dict(status='pending', sse_run_id='acceptance', packages=['custom'])
            jobs[key].update(status='done', exit_code=0)
            backend._WEBAPP_FILE_TOKENS[key] = dict(path=str(folder / 'outputs/old result.tsv'))
            backend._LOG_JOURNAL.append('acceptance', 'R', ['重复 中文 🧬 ' + 'x' * 160] * 100)
            logs += 100
            for cache in (backend._CONTAINER_LAST_STATS, backend._PULL_SOURCE_STATS,
                          backend._REGISTRY_PROBE_CACHE, backend._HUB_MANIFEST_CHECK_CACHE):
                cache['cycle-' + key] = dict(ts=time.time(), stats={})
            measure('history', '/logs/history?runId=acceptance&afterSeq=' + str(max(0, logs - 100)))
            measure('r_packages_cached', '/r/list-packages')
            measure('r_install_status', '/r/install-status?job_id=legacy&afterSeq=' + str(max(0, logs - 100)))
            if cycles % 10 == 0:
                with urllib.request.urlopen(base + '/logs/stream?runId=acceptance&lastSeq=' + str(logs), timeout=10) as response:
                    assert b'hello' in response.readline()
            if time.monotonic() >= next_sample:
                samples.append(resources())
                print('ACCEPTANCE_PROGRESS ' + json.dumps(samples[-1]), flush=True)
                next_sample = time.monotonic() + 10
            cycles += 1
            time.sleep(0.1)
        # Concurrent cache reads use the real Flask routes and local HTTP sockets.
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            assert all(x['status'] == 'success' for x in pool.map(lambda _: get('/r/list-packages'), range(200)))
        # Check the oldest token after cache eviction; it must still be usable.
        assert backend._WEBAPP_FILE_TOKENS['0']['path'] == str(folder / 'outputs/old result.tsv')
        export_started = time.perf_counter()
        count, exported_bytes = 0, 0
        with urllib.request.urlopen(base + '/logs/download?runId=acceptance', timeout=30) as response:
            for chunk in iter(lambda: response.read(65536), b''):
                count += chunk.count(b'\n')
                exported_bytes += len(chunk)
        assert count == logs, (count, logs)
        export_seconds = time.perf_counter() - export_started
        for relative, digest in original.items():
            assert hashlib.sha256((folder / relative).read_bytes()).hexdigest() == digest
        backend._LOG_JOURNAL.flush()
        path = backend._STATE_DATABASE.path
        backend._STATE_DATABASE.close()
        restored = StateDatabase(path)
        assert PersistentRegistry(restored, 'r-install', jobs=True)['legacy']['status'] == 'done'
        assert len(PersistentRegistry(restored, 'r-install', jobs=True)) == cycles + 1
        restored.close()
        time.sleep(4)  # Allow disconnected SSE heartbeat/request threads to drain.
        final = resources()
        samples.append(final)
        bounds = dict(jobs=len(jobs.cache), tokens=len(backend._WEBAPP_FILE_TOKENS.cache),
                      stats=len(backend._CONTAINER_LAST_STATS), sources=len(backend._PULL_SOURCE_STATS),
                      probes=len(backend._REGISTRY_PROBE_CACHE), manifests=len(backend._HUB_MANIFEST_CHECK_CACHE))
        assert bounds['jobs'] <= 256 and bounds['tokens'] <= 256
        assert bounds['stats'] <= 1024 and bounds['sources'] <= 256 and bounds['probes'] <= 256 and bounds['manifests'] <= 1024
        assert final['rss_bytes'] - warm['rss_bytes'] < 64 * 1024 * 1024, (warm, final)
        assert final['handles'] <= warm['handles'] + 16, (warm, final)
        assert final['threads'] <= warm['threads'] + 4, (warm, final)
        report = dict(result='PASS', duration_seconds=duration, cycles=cycles, logs=logs,
                      source_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (folder / 'backend').glob('*.py')},
                      platform=platform.platform(), python=sys.version, import_seconds=import_seconds,
                      http={name: distribution(values) for name, values in timings.items()},
                      warm=warm, final=final, samples=samples, cache_bounds=bounds,
                      mean_cpu_percent_one_core=100 * (final['cpu_seconds'] - warm['cpu_seconds']) /
                          (final['elapsed_seconds'] - warm['elapsed_seconds']),
                      export=dict(bytes=exported_bytes, seconds=export_seconds, records=count),
                      preserved_sha256=original, restart_records=cycles + 1,
                      limits=dict(rss_growth_bytes=64 * 1024 * 1024, handle_growth=16, thread_growth=4),
                      scope='isolated real HTTP backend; no container or installer execution')
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('ACCEPTANCE_RESULT ' + json.dumps({k: report[k] for k in ('result','cycles','logs','http','cache_bounds')}), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(5)
        backend._LOG_JOURNAL.close()
        backend._STATE_DATABASE.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=int, default=300)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.duration < 1: parser.error('duration must be positive')
    args.output = args.output.resolve()
    # Fail before a long run when this execution context cannot write evidence.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('a', encoding='utf-8'): pass
    if args.worker:
        worker(args.worker, args.duration, args.output)
        return
    with tempfile.TemporaryDirectory(prefix='core-acceptance-', dir=ROOT / 'build') as temporary:
        folder = Path(temporary)
        shutil.copytree(ROOT / 'backend', folder / 'backend', ignore=lambda path, names: [n for n in names if not n.endswith('.py')])
        subprocess.run([sys.executable, '-B', __file__, '--worker', str(folder), '--duration', str(args.duration),
                        '--output', str(args.output)], check=True, timeout=args.duration + 120)


if __name__ == '__main__': main()
