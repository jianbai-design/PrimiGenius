"""Create a deduplicated installer copy without modifying the development runtime."""
import gzip
import hashlib
import json
import pathlib
import shutil
import tarfile
import tempfile
import os


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _prepare(source, target, release=False):
    source, target = pathlib.Path(source), pathlib.Path(target)
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    duplicates = {'usr/libexec/primigenius/engine': 'primigenius-engine',
                  'usr/libexec/primigenius/bridge': 'primigenius-bridge-linux'}
    # Copy only verified delivery files. Build-only CLI is retained inside rootfs.
    target.mkdir()
    for name, expected in manifest['files'].items():
        src = source / name
        if not src.resolve().is_relative_to(source.resolve()) or sha(src) != expected:
            raise RuntimeError('Invalid runtime source: ' + name)
        if name in ('rootfs.tar.gz', 'podman-linux', 'SHA256SUMS'):
            continue
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    found = set()
    with tarfile.open(source / 'rootfs.tar.gz') as archive, (target / 'rootfs.tar.gz').open('wb') as raw:
        with gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|') as output:
                for member in archive:
                    name = member.name.removeprefix('./').rstrip('/')
                    if name in duplicates or name == 'usr/bin/podman':
                        external = duplicates.get(name, 'podman-linux')
                        if not member.isfile():
                            raise RuntimeError('Expected regular runtime binary: ' + name)
                        digest = hashlib.file_digest(archive.extractfile(member), 'sha256').hexdigest()
                        if digest != manifest['files'][external]:
                            raise RuntimeError('Runtime copies differ: ' + name)
                        found.add(name)
                    if name not in duplicates:
                        output.addfile(member, archive.extractfile(member) if member.isfile() else None)
    if found != set(duplicates) | {'usr/bin/podman'}:
        raise RuntimeError('Rootfs runtime layout differs from expected layout')
    manifest['files'] = {p.relative_to(target).as_posix(): sha(p)
                         for p in sorted(target.rglob('*')) if p.is_file()}
    checksums = ''.join(f'{digest}  {name}\n' for name, digest in manifest['files'].items())
    (target / 'SHA256SUMS').write_text(checksums, encoding='utf-8')
    manifest['files']['SHA256SUMS'] = sha(target / 'SHA256SUMS')
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    if release:
        acceptance = json.loads((source / 'acceptance.json').read_text(encoding='utf-8'))
        if acceptance['artifacts'] != manifest['files']:
            raise RuntimeError('Release acceptance must cover the deduplicated delivery')
        for section in ('containers', 'plugins', 'gui', 'installer', 'rollback', 'performance'):
            if acceptance['checks'].get(section) is not True:
                raise RuntimeError('Acceptance incomplete: ' + section)
        shutil.copyfile(source / 'acceptance.json', target / 'acceptance.json')
    return {'source_bytes': sum(p.stat().st_size for p in source.rglob('*') if p.is_file()),
            'delivery_bytes': sum(p.stat().st_size for p in target.rglob('*') if p.is_file()),
            'removed_rootfs_copies': sorted(duplicates), 'removed_outer_copy': 'podman-linux'}


def prepare(source, target, release=False, cache=None):
    if cache is None:
        return _prepare(source, target, release)
    source, target, cache = pathlib.Path(source), pathlib.Path(target), pathlib.Path(cache)
    original = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    for name, expected in original['files'].items():
        candidate = source / name
        if not candidate.resolve().is_relative_to(source.resolve()) or sha(candidate) != expected:
            raise RuntimeError('Invalid runtime source: ' + name)
    # Include the transformer implementation, not just the input archive version.
    key = hashlib.sha256((json.dumps(original, sort_keys=True) + sha(pathlib.Path(__file__))).encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    entry = cache / key

    def validate():
        manifest = json.loads((entry / 'files/manifest.json').read_text(encoding='utf-8'))
        if {k:v for k,v in manifest.items() if k != 'files'} != {k:v for k,v in original.items() if k != 'files'}:
            raise RuntimeError('Cached runtime metadata differs from source')
        for name, expected in manifest['files'].items():
            candidate = entry / 'files' / name
            if not candidate.resolve().is_relative_to((entry / 'files').resolve()) or sha(candidate) != expected:
                raise RuntimeError('Invalid cached runtime: ' + name)
        return manifest

    hit = entry.is_dir()
    if hit:
        validate()  # Fail closed on corruption; never package an unverified cache.
    else:
        with tempfile.TemporaryDirectory(prefix='delivery-', dir=cache) as temporary:
            staged = pathlib.Path(temporary)
            report = _prepare(source, staged / 'files')
            (staged / 'report.json').write_text(json.dumps(report), encoding='utf-8')
            # A concurrent producer may finish the same immutable entry first.
            try:
                os.replace(staged, entry)
            except OSError:
                if not entry.is_dir():
                    raise
            validate()
    manifest = validate()
    if release:
        acceptance = json.loads((source / 'acceptance.json').read_text(encoding='utf-8'))
        if acceptance['artifacts'] != manifest['files'] or not all(
                acceptance['checks'].get(name) is True for name in
                ('containers', 'plugins', 'gui', 'installer', 'rollback', 'performance')):
            raise RuntimeError('Release acceptance must cover the current deduplicated delivery')
    shutil.copytree(entry / 'files', target)
    if release:
        shutil.copyfile(source / 'acceptance.json', target / 'acceptance.json')
    report = json.loads((entry / 'report.json').read_text(encoding='utf-8'))
    return {**report, 'cache_hit': hit}
