"""Exercise bundled R against an explicitly supplied disposable distribution."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--distro', required=True)
    parser.add_argument('--existing-packages', action='store_true')
    parser.add_argument('--measure-disk', action='store_true')
    parser.add_argument('--delete-restore', action='store_true')
    args = parser.parse_args()
    if not args.distro.startswith('primigenius-runtime-test-'):
        raise SystemExit('Only a disposable test distribution is allowed')
    with tempfile.TemporaryDirectory(prefix='offline-r-verify-', dir=ROOT / 'build') as folder:
        work = Path(folder)
        os.environ['PRIMIGENIUS_LOG_DIR'] = str(work / 'logs')
        import app
        from podman_manager import PodmanManager
        from embedded_runtime import OwnedDockerClient
        from offline_r import OfflineR
        manager = PodmanManager(str(work / 'installation'))
        manager.wsl_distro_name = args.distro
        manager.runtime.bundle = str(ROOT / 'runtime/release')
        app._podman_mgr = manager
        if not args.existing_packages:
            (work / 'empty library').mkdir()
            app.R_LIBS_DIR = str(work / 'empty library')
        report = {'distro': args.distro, 'existing_packages': args.existing_packages}
        bundle = ROOT / 'runtime/r-environment'
        if args.delete_restore:
            recovery_bundle = work / 'metadata-only-bundle'
            recovery_bundle.mkdir()
            shutil.copyfile(ROOT/'runtime/r-environment/manifest.json', recovery_bundle/'manifest.json')
        samples, sampler_errors, stop = [], [], threading.Event()
        def used_bytes():
            result = subprocess.run(['wsl.exe', '-d', args.distro, '-u', 'root', '--exec',
                'df', '-B1', '--output=used', '/'], capture_output=True, text=True, check=True,
                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            return int(result.stdout.splitlines()[-1].strip())
        def sample():
            while not stop.wait(1):
                try:
                    samples.append(used_bytes())
                except Exception as exc:
                    sampler_errors.append(str(exc))
                    return
        sampler = None
        def client():
            return OwnedDockerClient(owner=manager.runtime.identity, base_url=manager.runtime.url,
                                     version='1.41', timeout=1800)
        def environment():
            return OfflineR(bundle, work / 'state', app.R_CUSTOM_RECIPE_SHA256,
                app.R_CUSTOM_IMAGE, app._custom_r_image_matches_contract,
                app._validate_offline_r_environment, client, app._check_offline_r_space,
                lambda text: print(text, flush=True))
        try:
            assert manager.runtime.start(), manager.runtime.last_error
            if args.measure_disk:
                samples.append(used_bytes())
                sampler = threading.Thread(target=sample)
                sampler.start()
            env = environment()
            started = time.monotonic()
            identity = env.ensure()
            report['first_seconds'] = time.monotonic() - started
            reopened = environment()
            reopened.validate = lambda *a: (_ for _ in ()).throw(AssertionError('Repeated startup validation'))
            started = time.monotonic()
            assert reopened.ensure() == identity
            report['reopen_seconds'] = time.monotonic() - started
            if args.delete_restore:
                env.bundle = recovery_bundle
                started = time.monotonic()
                env.remove(identity, confirmed=True)
                assert env.snapshot()['phase'] == 'removed'
                probe = client()
                try:
                    import docker
                    try:
                        probe.images.get(identity)
                    except docker.errors.ImageNotFound:
                        pass
                    else:
                        raise AssertionError('Image was not actually deleted')
                finally:
                    probe.close()
                report['delete_seconds'] = time.monotonic() - started
                started = time.monotonic()
                assert env.ensure() == identity
                report['restore_seconds'] = time.monotonic() - started
                report['restore_without_bundle_archive'] = True
            c = client()
            try:
                assert c.images.get(app.R_CUSTOM_IMAGE).id == identity
                task = c.containers.run(identity, ['Rscript', '-e', 'stopifnot(sum(1:100)==5050); cat("OFFLINE_R_OK\\n")'],
                    network_mode='none', detach=True, log_config={'type': 'json-file'})
                try:
                    assert task.wait(timeout=60)['StatusCode'] == 0
                    output = task.logs()
                    assert b'OFFLINE_R_OK' in output, output
                finally:
                    task.remove(force=True)
            finally:
                c.close()
            report['offline_r'] = True
            if args.measure_disk:
                stop.set()
                sampler.join()
                samples.append(used_bytes())
                report['filesystem_bytes'] = {'before': samples[0], 'after': samples[-1],
                    'sampled_peak': max(samples), 'sample_interval_seconds': 1, 'errors': sampler_errors}
            print(json.dumps(report), flush=True)
            (ROOT / 'runtime' / ('validation-r-delete-restore.json' if args.delete_restore else 'validation-offline-r-packages.json' if args.existing_packages else 'validation-offline-r.json')).write_text(
                json.dumps(report, indent=2), encoding='utf-8')
        finally:
            stop.set()
            if sampler:
                sampler.join()
            manager.runtime.stop()
            app._LOG_JOURNAL.close()


if __name__ == '__main__':
    main()
