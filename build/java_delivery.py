"""Stage the complete existing JRE at its legacy install path, without a ZIP copy."""
import pathlib
import shutil
import zipfile


def prepare(archive, target, config):
    archive, target = pathlib.Path(archive), pathlib.Path(target)
    with zipfile.ZipFile(archive) as source:
        files = [entry for entry in source.infolist() if not entry.is_dir()]
        launchers = [entry.filename for entry in files if entry.filename.endswith('/bin/java.exe')]
        if len(launchers) != 1:
            raise RuntimeError('Expected exactly one embedded JRE')
        prefix = launchers[0][:-len('bin/java.exe')]
        for entry in files:
            relative = pathlib.PurePosixPath(entry.filename.removeprefix(prefix))
            if (not entry.filename.startswith(prefix) or relative.is_absolute() or
                    '..' in relative.parts or '\\' in entry.filename or ':' in entry.filename):
                raise RuntimeError('Unexpected embedded JRE path: ' + entry.filename)
            destination = target.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Reading each member also checks its ZIP CRC; no runtime files are trimmed.
            with source.open(entry) as reader, destination.open('xb') as writer:
                shutil.copyfileobj(reader, writer)
    for name in ('bin/java.exe', 'bin/javaw.exe', 'bin/server/jvm.dll', 'lib/rt.jar', 'release'):
        if not (target / name).is_file():
            raise RuntimeError('Missing embedded JRE file: ' + name)
    resources = config['extraResources']
    matches = [item for item in resources if item.get('to') == 'jre8.zip']
    if len(matches) != 1:
        raise RuntimeError('Embedded JRE packaging rule changed')
    resources.remove(matches[0])
    config.setdefault('extraFiles', []).append({'from': str(target), 'to': 'java/jre', 'filter': ['**/*']})
    return {'archive_bytes': archive.stat().st_size,
            'runtime_bytes': sum(entry.file_size for entry in files),
            'runtime_files': len(files), 'install_path': 'java/jre'}
