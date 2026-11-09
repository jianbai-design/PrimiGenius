"""Assemble/check the runtime delivery; release requires recorded acceptance."""
import argparse
import hashlib
import json
import pathlib
import shutil
import tarfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
DIST = ROOT / 'release'
ARTIFACTS = ('primigenius-engine', 'primigenius-bridge-linux', 'primigenius-bridge.exe',
             'podman-linux', 'rootfs.tar.gz')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    global DIST
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--require-acceptance', action='store_true')
    parser.add_argument('--dist', type=pathlib.Path, default=DIST)
    args = parser.parse_args()
    DIST = args.dist.resolve()
    lock = json.loads((ROOT / 'runtime.lock.json').read_text())
    if args.verify:
        manifest = json.loads((DIST / 'manifest.json').read_text())
        assert manifest['podman_commit'] == lock['podman_commit'], 'Source baseline changed'
        for name in ARTIFACTS:
            assert sha(DIST / name) == manifest['files'][name], 'Checksum mismatch: ' + name
        for name, expected in manifest['files'].items():
            assert sha(DIST / name) == expected, 'Checksum mismatch: ' + name
        if args.require_acceptance:
            acceptance = json.loads((DIST / 'acceptance.json').read_text())
            assert acceptance['artifacts'] == manifest['files'], 'Acceptance is for different artifacts'
            for section in ('containers', 'plugins', 'gui', 'installer', 'rollback', 'performance'):
                assert acceptance['checks'].get(section) is True, 'Acceptance incomplete: ' + section
        print('Runtime bundle checksums verified')
        return
    archive = ROOT.parent / 'build/runtime-work/podman-source.tar.gz'
    assert sha(archive) == lock['source_sha256'], 'Upstream source checksum mismatch'
    DIST.mkdir(exist_ok=True)
    shutil.copyfile(archive, DIST / 'libpod-source.tar.gz')
    # Include complete bridge/engine/build sources plus vendored upstream licenses.
    with zipfile.ZipFile(DIST / 'primigenius-runtime-source.zip', 'w', zipfile.ZIP_DEFLATED) as out:
        sources = [p for p in ROOT.iterdir() if p.is_file() and
                   (p.suffix in ('.py', '.sh', '.md') or p.name.startswith('Containerfile') or
                    p.name == 'runtime.lock.json')]
        sources += [p for folder in ('engine', 'bridge') for p in (ROOT / folder).rglob('*')
                    if p.is_file() and p.suffix in ('.go', '.mod', '.sum')]
        # Runtime images, acceptance screenshots and build outputs are not source.
        # Recursing over all of ROOT would embed the offline R image a second time.
        for path in sorted(sources):
            out.write(path, path.relative_to(ROOT).as_posix())
    license_dir = DIST / 'licenses'
    license_dir.mkdir(exist_ok=True)
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            name = pathlib.PurePosixPath(member.name)
            if member.isfile() and name.name.lower().startswith(('license', 'copying', 'notice')):
                dest = license_dir.joinpath(*name.parts[1:])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(source.extractfile(member).read())
    for archive in (ROOT.parent / 'build/runtime-work/goproxy').rglob('*.zip'):
        with zipfile.ZipFile(archive) as source:
            for name in source.namelist():
                path = pathlib.PurePosixPath(name)
                if path.name.lower().startswith(('license', 'copying', 'notice')):
                    dest = license_dir.joinpath(*path.parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(source.read(name))
    with tarfile.open(DIST / 'rootfs.tar.gz') as rootfs:
        members = {m.name.lstrip('./'): m for m in rootfs.getmembers()}
        bundled = rootfs.extractfile(members['usr/libexec/primigenius/engine']).read()
        assert hashlib.sha256(bundled).hexdigest() == sha(DIST / 'primigenius-engine'), 'Rootfs engine is stale; rebuild rootfs'
        (DIST / 'rootfs-packages.txt').write_bytes(rootfs.extractfile(members['usr/libexec/primigenius/packages.txt']).read())
    lock['files'] = {p.relative_to(DIST).as_posix(): sha(p) for p in sorted(DIST.rglob('*'))
                     if p.is_file() and p.name not in ('manifest.json', 'acceptance.json')}
    (DIST / 'manifest.json').write_text(json.dumps(lock, indent=2) + '\n')
    print('Runtime bundle assembled with sources, licenses and SHA-256 manifest')


if __name__ == '__main__':
    main()
