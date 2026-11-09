"""Build a local NSIS installer; --release additionally requires acceptance."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from runtime_delivery import prepare as prepare_runtime_delivery
from build_r_environment import verify as verify_r_environment
from installer_templates import prepare as prepare_installer_templates
from java_delivery import prepare as prepare_java_delivery

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(command, cwd=ROOT):
    started = time.perf_counter()
    print('[build] ' + subprocess.list2cmdline([str(x) for x in command]), flush=True)
    subprocess.run([str(x) for x in command], cwd=cwd, check=True)
    return time.perf_counter() - started


def main():
    started = time.perf_counter()
    timings = {}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', action='store_true', help='Require complete recorded acceptance (never publishes)')
    parser.add_argument('--check', action='store_true', help='Check prerequisites without building')
    parser.add_argument('--r-bundle', type=pathlib.Path, default=ROOT / 'runtime/r-environment')
    parser.add_argument('--runtime-bundle', type=pathlib.Path, default=ROOT / 'runtime/release')
    args = parser.parse_args()
    args.r_bundle = args.r_bundle.resolve()
    args.runtime_bundle = args.runtime_bundle.resolve()
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node.js is not on PATH')
    for module in ('PyInstaller', 'flask', 'flask_cors', 'docker', 'requests', 'win32file', 'win32pipe'):
        if importlib.util.find_spec(module) is None:
            raise RuntimeError(f'Missing Python dependency: {module}; install it for {sys.executable}')
    for name in ('backend/app.spec', 'build/installer.nsh', 'build/installer-uninstall.nsh', 'build/installer-r-wait.ps1', 'build/icon.ico', 'build/jre8.zip',
                 'build/wsl.2.7.3.0.x64.msi', 'build/electron-builder-wrapper.js',
                 'node_modules/electron-builder/package.json', 'node_modules/electron/dist/electron.exe'):
        if not (ROOT / name).is_file():
            raise RuntimeError('Missing build resource: ' + name)
    timings['ui_compile'] = run([node, ROOT / 'node_modules/typescript/bin/tsc', '-p', ROOT / 'tsconfig.vector.json'])
    timings['preview_compile'] = run([node, ROOT / 'build/build_preview.js'])
    verify = [sys.executable, '-B', ROOT / 'runtime/bundle.py', '--verify', '--dist', args.runtime_bundle]
    timings['runtime_verify'] = run(verify)
    r_manifest = verify_r_environment(args.r_bundle)
    if args.release:
        acceptance = json.loads((args.r_bundle / 'acceptance.json').read_text(encoding='utf-8'))
        if acceptance.get('archive_sha256') != r_manifest['archive_sha256'] or not all(
                acceptance.get('checks', {}).get(name) is True for name in
                ('offline_install', 'upgrade', 'packages', 'gui', 'rollback', 'disk_usage')):
            raise RuntimeError('Offline R release acceptance is incomplete')
    if not args.release:
        print('[build] Local verification installer: full runtime/release acceptance is not asserted.', flush=True)
    if args.check:
        if args.release:
            with tempfile.TemporaryDirectory(prefix='installer-work-check-', dir=ROOT / 'build') as check_work:
                prepare_runtime_delivery(args.runtime_bundle, pathlib.Path(check_work) / 'runtime', True)
        print('[build] Prerequisites verified; no installer was built.')
        return
    output_root = ROOT / 'dist_output'
    output_root.mkdir(exist_ok=True)
    output = pathlib.Path(tempfile.mkdtemp(prefix=datetime.datetime.now().strftime('%Y%m%d-%H%M%S-'), dir=output_root))
    work = pathlib.Path(tempfile.mkdtemp(prefix='installer-work-', dir=ROOT / 'build'))
    try:
        runtime_delivery = work / 'runtime'
        stage_started = time.perf_counter()
        deduplication = prepare_runtime_delivery(args.runtime_bundle, runtime_delivery, args.release,
                                                 cache=ROOT / 'build/runtime-delivery-cache')
        timings['runtime_delivery'] = time.perf_counter() - stage_started
        print('[build] Runtime deduplication: ' + json.dumps(deduplication), flush=True)
        timings['backend_freeze'] = run([sys.executable, '-m', 'PyInstaller', 'app.spec', '--noconfirm',
             '--distpath', work / 'backend', '--workpath', work / 'pyinstaller'], ROOT / 'backend')
        backend = work / 'backend/app'
        if not (backend / 'app.exe').is_file():
            raise RuntimeError('Backend executable was not generated')
        config = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['build']
        java_delivery = prepare_java_delivery(ROOT / 'build/jre8.zip', work / 'jre', config)
        nsis_templates = prepare_installer_templates(ROOT, work)
        hooks = work / 'installer-hooks.nsh'
        hooks.write_text('!define PG_JAVA_DIRECT_DELIVERY\n' + hooks.read_text(encoding='utf-8'), encoding='utf-8')
        config['nsis']['include'] = str(work / 'installer-hooks.nsh')
        config['directories'] = {'app': str(ROOT), 'output': str(output), 'buildResources': str(ROOT / 'build')}
        for resource in config['extraResources']:
            resource['from'] = str(backend if resource['to'] == 'backend' else
                                   runtime_delivery if resource['to'] == 'runtime' else
                                   args.r_bundle if resource['to'] == 'r-environment' else ROOT / resource['from'])
        config_path = work / 'electron-builder.json'
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        timings['desktop_package_and_nsis'] = run([node, ROOT / 'build/electron-builder-wrapper.js', '--pg-nsis-templates', nsis_templates, '--config', config_path,
             '--win', '--x64', '--publish', 'never'])
        installers = list(output.glob('PrimiGenius-Setup-*.exe'))
        if len(installers) != 1 or installers[0].stat().st_size == 0:
            raise RuntimeError('Expected one non-empty NSIS installer')
        with installers[0].open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        report = {'installer': installers[0].name, 'sha256': digest,
                  'build_seconds': time.perf_counter() - started, 'stage_seconds': timings,
                  'r_environment': r_manifest,
                  'runtime_deduplication': deduplication,
                  'java_delivery': java_delivery,
                  'release_acceptance_required': args.release, 'published': False,
                  'installation_tested': False}
        (output / 'build-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(f'[build] SUCCESS: {installers[0]}\n[build] SHA256: {digest}', flush=True)
    finally:
        # Only remove this invocation's scratch directory, never previous builds.
        if work.resolve().parent != (ROOT / 'build').resolve() or not work.name.startswith('installer-work-'):
            raise RuntimeError('Unexpected scratch path; refusing cleanup')
        shutil.rmtree(work)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'[build] FAILED: {error}', file=sys.stderr)
        sys.exit(1)
