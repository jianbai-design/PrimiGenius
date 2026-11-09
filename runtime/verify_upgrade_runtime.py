"""Offline execution/reconnect acceptance in a newly owned disposable WSL distro.

Existing plugin scripts and R packages are mounted read-only. No registered
user environment is modified or stopped. Only the distro created here is removed.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from embedded_runtime import EmbeddedRuntime, OwnedDockerClient, _wsl_bind_source
from runtime_logs import LogCursor


def summary(values):
    values = sorted(values)
    return dict(count=len(values), p50_ms=statistics.median(values) * 1000,
                p95_ms=values[max(0, (len(values) * 95 + 99) // 100 - 1)] * 1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--r-bundle', type=Path, default=ROOT / 'runtime/r-environment')
    parser.add_argument('--runtime-bundle', type=Path, default=ROOT / 'runtime/release')
    args = parser.parse_args()
    output = args.output.resolve()
    name = 'primigenius-runtime-test-' + uuid.uuid4().hex[:12]
    environment = dict(os.environ, WSL_UTF8='1')
    def command(arguments, timeout=180):
        return subprocess.run(arguments, check=True, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout, env=environment,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    def wsl(text, timeout):
        return subprocess.run(['wsl.exe', '-d', name, '-u', 'root', '--', 'sh', '-c', text],
                              capture_output=True, text=True, timeout=timeout, env=environment,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    report = dict(distro=name, checks={}, network='disabled for all containers')
    with tempfile.TemporaryDirectory(prefix='runtime-acceptance-', dir=ROOT / 'build') as temporary:
        folder = Path(temporary)
        manager = SimpleNamespace(app_install_dir=str(folder), runtime_channel='acceptance',
            wsl_distro_name=name, podman_data_dir=str(folder / 'transport'), _run_wsl=wsl,
            _connection_state='starting', _service_retry_after=0, host_path_to_wsl=_wsl_bind_source)
        runtime = EmbeddedRuntime(manager)
        runtime.bundle = str(args.runtime_bundle.resolve())
        client, registered, containers = None, False, []
        try:
            began = time.perf_counter()
            command(['wsl.exe', '--import', name, str(folder / 'distro'), str(args.runtime_bundle.resolve() / 'rootfs.tar.gz'), '--version', '2'])
            registered = True
            report['distro_import_seconds'] = time.perf_counter() - began
            began = time.perf_counter()
            assert runtime.start(), runtime.last_error
            report['engine_start_seconds'] = time.perf_counter() - began
            client = OwnedDockerClient(owner=runtime.identity, base_url=runtime.url, version='1.41', timeout=180)
            latencies = []
            for _ in range(100):
                begin = time.perf_counter()
                assert client.ping()
                latencies.append(time.perf_counter() - begin)
            report['ping'] = summary(latencies)
            manifest = json.loads((args.r_bundle / 'manifest.json').read_text(encoding='utf-8'))
            from offline_r import read_manifest, image_archive_stream
            manifest = read_manifest(args.r_bundle / 'manifest.json', manifest['recipe_sha256'])
            archive = args.r_bundle / 'stage' / manifest['archive']
            began = time.perf_counter()
            # Streaming SDK image load: never read the full archive into Python.
            with image_archive_stream(archive) as handle:
                for event in client.api.load_image(handle):
                    if isinstance(event, (bytes, str)): event = json.loads(event)
                    if event.get('error'): raise RuntimeError(event['error'])
            image = client.images.get(manifest['image_id'])
            report['offline_image_load_seconds'] = time.perf_counter() - began
            report['checks']['pinned_image_identity'] = image.id == manifest['image_id']
            report['image_id'] = image.id
            print('OFFLINE_IMAGE_LOADED ' + json.dumps({'seconds': report['offline_image_load_seconds']}), flush=True)
            library = ROOT / 'r_libs_r4_6_1-bioc3_23'
            plugins = ROOT / 'plugins/r/correlation_analysis_stats'
            files = [plugins / 'config.json', plugins / 'scripts/correlation_analysis_stats.R']
            packages = ['ggplot2','dplyr','svglite','stringr','jsonlite','readxl']
            files += [library / package / 'DESCRIPTION' for package in packages]
            hashes = {}
            for path in files:
                with path.open('rb') as handle:
                    hashes[str(path.relative_to(ROOT))] = hashlib.file_digest(handle, 'sha256').hexdigest()
            volumes = {str(library): {'bind': '/r_libs', 'mode': 'ro'},
                       str(plugins): {'bind': '/plugin', 'mode': 'ro'},
                       str(folder / 'work'): {'bind': '/workspace', 'mode': 'rw'}}
            (folder / 'work').mkdir()
            def run(arguments):
                c = client.containers.create(image.id, arguments, network_mode='none',
                    volumes=volumes, environment={'R_LIBS_USER': '/r_libs'}, working_dir='/workspace',
                    log_config={'Type': 'json-file'}, mem_limit='768m')
                containers.append(c)
                try:
                    c.start()
                    result = c.wait(timeout=120)
                    logs = c.logs()
                    assert result['StatusCode'] == 0, logs.decode(errors='replace')
                    return logs.decode(errors='replace')
                finally:
                    c.remove(force=True)
                    containers.remove(c)
            code = 'p<-c(' + ','.join(json.dumps(p) for p in packages) + ');' + \
                'for(n in p){library(n,character.only=TRUE);cat(n,as.character(packageVersion(n)),"\\n")};' + \
                'stopifnot(sum(1:100)==5050);cat("EXISTING_R_PACKAGES_OK\\n")'
            began = time.perf_counter()
            report['r_packages'] = run(['Rscript','-e',code])
            report['r_packages_seconds'] = time.perf_counter() - began
            report['checks']['existing_r_packages_readonly'] = 'EXISTING_R_PACKAGES_OK' in report['r_packages']
            # Exercise future source-package installation without writing to the
            # user's library or downloading any package/toolchain dependency.
            fixture = folder / 'work' / 'compile-smoke'
            (fixture / 'src').mkdir(parents=True)
            (fixture / 'DESCRIPTION').write_text(
                'Package: compilesmoke\nVersion: 1.0\nTitle: Compiler Acceptance\n'
                'Description: Exercise native source compilation.\nLicense: CC0\n'
                'Author: QA\nMaintainer: QA <qa@example.org>\n')
            (fixture / 'NAMESPACE').write_text('useDynLib(compilesmoke)\n')
            (fixture / 'src/check_c.c').write_text('void native_c(int *x) { *x += 1; }\n')
            (fixture / 'src/check_cpp.cpp').write_text('extern "C" void native_cpp(int *x) { *x += 2; }\n')
            (fixture / 'src/check_f.f90').write_text('subroutine native_f(x)\ninteger x\nx=x+3\nend\n')
            rust = folder / 'work' / 'rust-smoke'
            (rust / 'src').mkdir(parents=True)
            (rust / 'Cargo.toml').write_text('[package]\nname="compile-smoke"\nversion="0.1.0"\nedition="2021"\n')
            (rust / 'src/main.rs').write_text('fn main() { println!("{}", 40+2); }\n')
            native_checks = ('library(compilesmoke,lib.loc="/workspace/compiled-library");'
                'stopifnot(.C("native_c",as.integer(1),PACKAGE="compilesmoke")[[1]]==2L,'
                '.C("native_cpp",as.integer(1),PACKAGE="compilesmoke")[[1]]==3L,'
                '.Fortran("native_f",as.integer(1),PACKAGE="compilesmoke")[[1]]==4L);'
                'cat("NATIVE_R_COMPILE_OK\\n")')
            import shlex
            report['compilers'] = run(['bash', '-ec',
                'mkdir -p /workspace/compiled-library; '
                'R CMD INSTALL --library=/workspace/compiled-library /workspace/compile-smoke; '
                'Rscript -e ' + shlex.quote(native_checks) + '; '
                'cargo build --offline --manifest-path /workspace/rust-smoke/Cargo.toml; '
                'test "$(/workspace/rust-smoke/target/debug/compile-smoke)" = 42; '
                'printf "RUST_CARGO_COMPILE_OK\\n"'])
            report['checks']['c_cpp_fortran_rust_offline_compilation'] = (
                'NATIVE_R_COMPILE_OK' in report['compilers'] and 'RUST_CARGO_COMPILE_OK' in report['compilers'])
            (folder / 'work/input.csv').write_text('Variable_1,Variable_2,rho,pvalue,padj,n_complete\nA,B,0.9,0.001,0.01,10\nA,C,-0.8,0.002,0.02,10\n', encoding='utf-8')
            began = time.perf_counter()
            report['plugin_log'] = run(['Rscript','/plugin/scripts/correlation_analysis_stats.R',
                'correlation_file=/workspace/input.csv','outDir=/workspace/result'])
            report['plugin_seconds'] = time.perf_counter() - began
            outputs = sorted(p.name for p in (folder / 'work/result').iterdir())
            assert all(name in outputs for name in ('Edge.csv','Node.csv','Node_Degree.csv','Node_Degree.png','Node_Degree.svg'))
            report['plugin_outputs'] = outputs
            report['checks']['unchanged_plugin_native_outputs'] = True
            tasks = []
            def short_task(index):
                began = time.perf_counter()
                c = client.containers.create(image.id, ['sh','-c','printf "中文\\n中文\\n"'],
                    network_mode='none', log_config={'Type': 'json-file'})
                try:
                    c.start()
                    assert c.wait(timeout=30)['StatusCode'] == 0
                    lines = []
                    LogCursor().read(c, lines.append)
                    assert lines == ['中文','中文']
                finally: c.remove(force=True)
                return time.perf_counter() - began
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                tasks = list(pool.map(short_task, range(40)))
            report['short_tasks'] = summary(tasks)
            c = client.containers.run(image.id, ['sh','-c','i=0; while [ "$i" -lt 1000 ]; do echo "中文$i"; i=$((i+1)); sleep 0.05; done'],
                detach=True, network_mode='none', log_config={'Type': 'json-file'})
            containers.append(c)
            cursor, lines, reconnect = LogCursor(), [], []
            identity = c.id
            for _ in range(10):
                cursor.read(c, lines.append)
                client.close()
                runtime.stop_bridge()
                began = time.perf_counter()
                assert runtime.start(), runtime.last_error
                reconnect.append(time.perf_counter() - began)
                client = OwnedDockerClient(owner=runtime.identity, base_url=runtime.url, version='1.41', timeout=180)
                c = client.containers.get(identity)
                assert c.status == 'running'
                assert len(client.containers.list()) == 1
            c.kill()
            c.wait(timeout=30)
            cursor.read(c, lines.append)
            assert lines == ['中文' + str(i) for i in range(len(lines))]
            c.remove()
            containers.clear()
            report['reconnect'] = dict(**summary(reconnect), log_records=len(lines))
            report['checks']['reconnect_no_duplicate_task_or_log'] = True
            report['checks']['existing_image_survives_reconnect'] = client.images.get(image.id).id == image.id
            # The new shipped archive must coexist with old gzip recovery files.
            from offline_r import OfflineR
            import shutil
            recovery_client = lambda: OwnedDockerClient(owner=runtime.identity, base_url=runtime.url,
                                                         version='1.41', timeout=180)
            r_environment = OfflineR(args.r_bundle, folder / 'r-state', manifest['recipe_sha256'],
                manifest['source_image'], lambda candidate: candidate.id == manifest['image_id'],
                lambda connection, identity: None, recovery_client,
                lambda metadata: None, lambda line: print(line, flush=True))
            assert r_environment.ensure() == image.id
            metadata_only = folder / 'metadata-only'
            metadata_only.mkdir()
            shutil.copyfile(args.r_bundle / 'manifest.json', metadata_only / 'manifest.json')
            r_environment.bundle = metadata_only
            r_environment.remove(image.id, confirmed=True)
            assert r_environment.snapshot()['phase'] == 'removed'
            assert r_environment.ensure() == image.id
            report['checks']['gzip_recovery_without_shipped_archive'] = True
            for relative, digest in hashes.items():
                with (ROOT / relative).open('rb') as handle:
                    assert hashlib.file_digest(handle, 'sha256').hexdigest() == digest
            report['preserved_sha256'] = hashes
            report['checks']['existing_plugin_and_package_files_unchanged'] = True
            assert all(report['checks'].values())
            report['result'] = 'PASS'
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            for c in containers:
                try: c.remove(force=True)
                except Exception: pass
            if client: client.close()
            runtime.stop()
            if registered:
                # UUID name and import path belong exclusively to this invocation.
                assert name.startswith('primigenius-runtime-test-') and folder.resolve().parent == (ROOT / 'build').resolve()
                command(['wsl.exe','--unregister',name], timeout=120)
                report['disposable_distro_removed'] = True
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
