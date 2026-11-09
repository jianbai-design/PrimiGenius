"""Build/export the pinned offline R runtime; never installs plugin R packages."""
import argparse
import ast
import lzma
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def contract():
    """Read the maintained recipe without importing or starting the application."""
    tree = ast.parse((ROOT / 'backend/app.py').read_text(encoding='utf-8'))
    assignments = {n.targets[0].id: n for n in tree.body if isinstance(n, ast.Assign)
                   and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)}
    wanted = {'R_VERSION', 'BIOCONDUCTOR_VERSION', 'R_CUSTOM_IMAGE', 'R_CUSTOM_RECIPE_SHA256',
              'R_CUSTOM_RECIPE_VERSION', 'R_DOCKER_IMAGE_DIGEST', 'R_DOCKER_IMAGE_CONFIG_DIGEST',
              'R_DOCKER_IMAGE_TAGGED', 'R_SYSTEM_DEPS', '_R_RUST_CHECK', 'R_TCLTK_CHECK'}
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in ('_r_custom_recipe_text', '_custom_r_image_matches_contract')]
    wanted.update(n.id for f in functions for n in ast.walk(f) if isinstance(n, ast.Name) and n.id in assignments)
    while True:
        expanded = wanted | {n.id for name in wanted for n in ast.walk(assignments[name])
                             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in assignments}
        if expanded == wanted:
            break
        wanted = expanded
    nodes = [n for n in tree.body if (isinstance(n, ast.Assign) and n in [assignments[k] for k in wanted]) or n in functions]
    scope = dict(hashlib=hashlib, json=json)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'r-contract', 'exec'), scope)
    return scope


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify(folder, recipe=None):
    sys.path.insert(0, str(ROOT / 'backend'))
    from offline_r import read_manifest
    c = contract()
    m = read_manifest(folder / 'manifest.json', recipe or c['R_CUSTOM_RECIPE_SHA256'])
    archive = folder / 'stage' / m['archive']
    if archive.stat().st_size != m['archive_bytes'] or sha(archive) != m['archive_sha256']:
        raise RuntimeError('R runtime archive checksum failed')
    for name, digest in m['metadata_files'].items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder.resolve()) or sha(path) != digest:
            raise RuntimeError('R runtime metadata checksum failed: ' + name)
    return m


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--distro', default='primigenius-runtime-test-20261017')
    parser.add_argument('--build', action='store_true', help='Build the maintained recipe in the selected release/test distro')
    parser.add_argument('--extend-runtime', type=Path, help='Extend a verified previous offline runtime without upgrading its existing system packages')
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/r-environment')
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify(args.output), indent=2))
        return
    if args.output.exists():
        raise RuntimeError('Output already exists; choose a new output directory to preserve rollback')
    c = contract()
    baseline = None
    if args.extend_runtime:
        old = json.loads((args.extend_runtime / 'manifest.json').read_text(encoding='utf-8'))
        baseline = verify(args.extend_runtime, old['recipe_sha256'])
        if (baseline['r_version'], baseline['bioconductor_version']) != (c['R_VERSION'], c['BIOCONDUCTOR_VERSION']):
            raise RuntimeError('Incremental runtime must preserve the R/Bioconductor version')
        if not args.build:
            raise RuntimeError('--extend-runtime requires --build')
    def run(*command, timeout=1800):
        result = subprocess.run(['wsl.exe', '-d', args.distro, '-u', 'root', '--exec', *command],
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout.strip()
    def linux(path):
        return '/mnt/' + str(path)[0].lower() + str(path)[2:].replace('\\', '/')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='r-release-', dir=ROOT / 'build') as scratch:
        scratch = Path(scratch)
        recipe = c['_r_custom_recipe_text']()
        if args.build:
            base_ref = baseline['image_id'] if baseline else 'docker.io/' + c['R_DOCKER_IMAGE_TAGGED']
            base = json.loads(run('podman', 'image', 'inspect', base_ref))[0]
            if not baseline and base['Id'].removeprefix('sha256:') != c['R_DOCKER_IMAGE_CONFIG_DIGEST'].removeprefix('sha256:'):
                raise RuntimeError('Release build requires the exact pinned base image')
            if baseline:
                labels = (base.get('Config') or {}).get('Labels') or {}
                expected = {'org.primigenius.recipe-sha256': baseline['recipe_sha256'],
                            'org.primigenius.r-version': c['R_VERSION'],
                            'org.primigenius.bioconductor-version': c['BIOCONDUCTOR_VERSION'],
                            'org.primigenius.base-digest': c['R_DOCKER_IMAGE_DIGEST']}
                if any(labels.get(key) != value for key, value in expected.items()):
                    raise RuntimeError('Previous runtime labels do not match its verified manifest and pinned base')
            build_recipe = 'FROM ' + base['Id'] + '\n' + recipe.split('\n', 1)[1]
            if baseline:
                build_recipe = build_recipe.replace('--no-install-recommends', '--no-install-recommends --no-upgrade')
            (scratch / 'Containerfile').write_text(build_recipe, encoding='utf-8')
            print(run('podman', 'build', '--pull=never', '-t', c['R_CUSTOM_IMAGE'],
                      '-f', linux(scratch / 'Containerfile'), linux(scratch), timeout=7200), flush=True)
        info = json.loads(run('podman', 'image', 'inspect', c['R_CUSTOM_IMAGE']))[0]
        from types import SimpleNamespace
        if not c['_custom_r_image_matches_contract'](SimpleNamespace(attrs=info)):
            raise RuntimeError('Existing image does not match the source recipe; use --build')
        cid = run('podman', 'create', '--network=none', c['R_CUSTOM_IMAGE'], 'sleep', '600')
        try:
            run('podman', 'start', cid)
            checks = 'set -e; ' + c['_R_RUST_CHECK'] + ' '
            checks += 'for dep in locales ' + c['R_SYSTEM_DEPS'] + '; do dpkg-query -W -f=\'${Status}\' "$dep" | grep -qx "install ok installed"; done; '
            checks += 'unset DISPLAY; Rscript -e \'stopifnot(as.character(getRversion()) == "' + c['R_VERSION'] + '"); ' + c['R_TCLTK_CHECK'] + 'cat("R_ENV_OK\\n")\''
            print(run('podman', 'exec', cid, 'bash', '-ec', checks), flush=True)
            inventory = 'p <- installed.packages(); cat(paste(sort(paste(p[,"Package"],p[,"Version"],sep="=")),collapse="\\n"))'
            r_packages = run('podman', 'exec', cid, 'Rscript', '-e', inventory)
            base_packages = ((args.extend_runtime / 'r-packages.txt').read_text(encoding='utf-8').strip() if baseline else
                             run('podman', 'run', '--rm', '--network=none', c['R_DOCKER_IMAGE_CONFIG_DIGEST'], 'Rscript', '-e', inventory))
            if r_packages != base_packages:
                raise RuntimeError('R package inventory differs from the pinned base; refusing to bundle plugin packages')
            package_list = run('podman', 'exec', cid, 'dpkg-query', '-W', '-f=${Package}\t${Version}\n')
            run('podman', 'exec', cid, 'bash', '-ec',
                "find /usr/share/doc -name copyright -type f -print0 > /tmp/license-files; "
                "find /usr/share/common-licenses /usr/local/lib/R -name 'COPYING*' -type f -print0 >> /tmp/license-files; "
                "tar --null -czf /tmp/licenses.tar.gz -T /tmp/license-files")
            run('podman', 'cp', cid + ':/tmp/licenses.tar.gz', linux(scratch / 'licenses.tar.gz'))
        finally:
            run('podman', 'rm', '-f', cid)
        if args.output.exists():
            raise RuntimeError('Output already exists; choose a new output directory to preserve rollback')
        staged = scratch / 'delivery'
        (staged / 'stage').mkdir(parents=True)
        # One compression envelope over uncompressed layers. Docker archives
        # retain the image config/identity and load on both old and new engines.
        raw = scratch / 'environment.tar'
        run('podman', 'save', '--format', 'docker-archive', '-o', linux(raw), info['Id'])
        archive = staged / 'stage/environment.tar.xz'
        sevenzip = ROOT / 'node_modules/7zip-bin/win/x64/7za.exe'
        if sys.platform == 'win32' and sevenzip.is_file():
            subprocess.run([str(sevenzip), 'a', '-txz', '-mx=6', '-mmt=4', str(archive), str(raw)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            with raw.open('rb') as source, lzma.open(archive, 'wb', preset=6) as output:
                shutil.copyfileobj(source, output, 1024*1024)
        (staged / 'Containerfile').write_text(recipe, encoding='utf-8')
        (staged / 'packages.txt').write_text(package_list + '\n', encoding='utf-8')
        (staged / 'r-packages.txt').write_text(r_packages + '\n', encoding='utf-8')
        shutil.copyfile(scratch / 'licenses.tar.gz', staged / 'licenses.tar.gz')
        image_id = info['Id'] if info['Id'].startswith('sha256:') else 'sha256:' + info['Id']
        manifest = dict(schema=1, r_version=c['R_VERSION'], bioconductor_version=c['BIOCONDUCTOR_VERSION'],
                        recipe_sha256=c['R_CUSTOM_RECIPE_SHA256'], image_id=image_id,
                        archive=archive.name, archive_sha256=sha(archive), archive_bytes=archive.stat().st_size,
                        image_bytes=info['Size'], required_free_bytes=info['Size'] * 2 + 512*1024*1024,
                        space_policy='conservative: two image copies plus 512 MiB; acceptance records measured peak separately',
                        source_image=c['R_CUSTOM_IMAGE'], network_disabled_smoke_test=True,
                        metadata_files={name: sha(staged/name) for name in ('Containerfile','packages.txt','r-packages.txt','licenses.tar.gz')})
        if baseline:
            (staged / 'build.Containerfile').write_text(build_recipe, encoding='utf-8')
            manifest['build_parent_image_id'] = baseline['image_id']
            manifest['metadata_files']['build.Containerfile'] = sha(staged / 'build.Containerfile')
        (staged / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        verify(staged)
        shutil.move(str(staged), str(args.output))
        print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    main()
