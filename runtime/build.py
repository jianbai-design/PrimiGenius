"""Rebuild the embedded runtime using a named Linux build environment.

Requires Linux Podman in that build environment only, never Windows podman.exe.
The application itself deploys the resulting rootfs with wsl --import.
"""
import argparse
import gzip
import hashlib
import json
import pathlib
import shutil
import subprocess
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent
WORK = ROOT.parent / 'build/runtime-work'
DIST = ROOT / 'release'


def linux_path(path):
    path = str(path.resolve())
    return '/mnt/' + path[0].lower() + path[2:].replace('\\', '/')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--distro', required=True, help='Explicit Linux build environment with Podman installed')
    parser.add_argument('--go-proxy', default='https://proxy.golang.org')
    args = parser.parse_args()
    lock = json.loads((ROOT / 'runtime.lock.json').read_text())
    WORK.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(exist_ok=True)

    def download(url, target):
        if target.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + '.partial')
        subprocess.run(['curl.exe', '-fL', '--retry', '2', '--max-time', '300', url, '-o', str(temporary)], check=True)
        temporary.replace(target)

    archive = WORK / 'podman-source.tar.gz'
    download(lock['source_url'], archive)
    with archive.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == lock['source_sha256'], 'Source checksum mismatch'
    # go.sum authenticates the three module archives in the offline compiler.
    for module, version in [('github.com/!microsoft/go-winio', 'v0.6.2'),
                            ('github.com/hashicorp/yamux', 'v0.1.2'), ('golang.org/x/sys', 'v0.38.0')]:
        for extension in ('mod', 'info', 'zip'):
            relative = module + '/@v/' + version + '.' + extension
            download(args.go_proxy.rstrip('/') + '/' + relative, WORK / 'goproxy' / relative)

    def podman(*command, capture=False):
        result = subprocess.run(['wsl.exe', '-d', args.distro, '-u', 'root', '--exec', 'podman', *command],
                                check=True, capture_output=capture, text=capture)
        return result.stdout.strip() if capture else None

    builder = 'localhost/primigenius-runtime-builder:' + lock['podman_commit'][:12]
    image = 'localhost/primigenius-runtime-rootfs:' + lock['podman_commit'][:12]
    podman('build', '--network', 'host', '-t', builder, '-f', linux_path(ROOT / 'Containerfile.build'), linux_path(ROOT))
    podman('run', '--rm', '--network', 'none', '-e', 'GOPROXY=file:///inputs/goproxy', '-e', 'GOSUMDB=off',
           '-v', linux_path(ROOT) + ':/project:ro', '-v', linux_path(WORK) + ':/inputs:ro',
           '-v', linux_path(DIST) + ':/out', builder, '/bin/bash', '/project/build-linux.sh')
    podman('build', '--network', 'host', '-t', image, '-f', linux_path(ROOT / 'Containerfile.rootfs'), linux_path(ROOT))
    container = podman('create', image, '/bin/true', capture=True)
    raw = WORK / 'rootfs-export.tar'
    try:
        podman('export', '-o', linux_path(raw), container)
        # Deterministic archive metadata; do not alter ownership or permissions.
        with tarfile.open(raw) as source, (DIST / 'rootfs.tar.gz').open('wb') as output:
            with gzip.GzipFile(filename='', fileobj=output, mode='wb', mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode='w|') as target:
                    for member in sorted(source.getmembers(), key=lambda item: item.name):
                        member.mtime = 1792108800
                        member.pax_headers = {}
                        target.addfile(member, source.extractfile(member) if member.isfile() else None)
    finally:
        podman('rm', '-f', container)
        raw.unlink(missing_ok=True)
    subprocess.run(['python', '-B', str(ROOT / 'bundle.py')], check=True)
    subprocess.run(['python', '-B', str(ROOT / 'bundle.py'), '--verify'], check=True)


if __name__ == '__main__':
    main()
