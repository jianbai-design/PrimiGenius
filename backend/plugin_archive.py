"""Validate plugin ZIPs before extraction and replace installations atomically."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile

MAX_FILES = 10000
MAX_EXPANDED = 1024 * 1024 * 1024
_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')


def valid_plugin_id(value):
    return (isinstance(value, str) and bool(_ID.fullmatch(value))
            and not value.endswith('.') and not _RESERVED.match(value))


def inspect_archive(archive, max_expanded=MAX_EXPANDED):
    entries, seen, total = [], set(), 0
    if len(archive.infolist()) > MAX_FILES:
        raise ValueError('压缩包文件数量超过限制')
    for info in archive.infolist():
        name = info.filename.rstrip('/')
        parts = name.split('/')
        if (not name or '\\' in info.orig_filename or '\\' in name or name.startswith('/') or
                any(p in ('', '.', '..') or p.endswith((' ', '.')) or
                    any(ord(c) < 32 or c in '<>:"|?*' for c in p) or _RESERVED.match(p)
                    for p in parts)):
            raise ValueError('压缩包包含不安全的路径')
        key = unicodedata.normalize('NFC', name).casefold()
        if key in seen:
            raise ValueError('压缩包包含重复或大小写冲突的路径')
        seen.add(key)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
            raise ValueError('压缩包不允许符号链接或特殊文件')
        if info.flag_bits & 1:
            raise ValueError('不支持加密压缩包')
        total += info.file_size
        if total > max_expanded:
            raise ValueError('解压后大小超过限制')
        entries.append(info)
    configs = [x for x in entries if not x.is_dir() and PurePosixPath(x.filename).name == 'config.json']
    if not configs:
        raise ValueError('无法在压缩包中找到 config.json')
    depth = min(len(PurePosixPath(x.filename).parts) for x in configs)
    configs = [x for x in configs if len(PurePosixPath(x.filename).parts) == depth]
    if len(configs) != 1:
        raise ValueError('一次只能上传一个插件')
    config_info = configs[0]
    root = str(PurePosixPath(config_info.filename).parent)
    prefix = '' if root == '.' else root + '/'
    if any(not x.is_dir() and not x.filename.startswith(prefix) for x in entries):
        raise ValueError('插件目录之外存在额外文件')
    if config_info.file_size > 2 * 1024 * 1024:
        raise ValueError('config.json 过大')
    config = json.loads(archive.read(config_info).decode('utf-8-sig'))
    if not isinstance(config, dict) or not valid_plugin_id(config.get('id')):
        raise ValueError('插件 ID 不合法')
    return config, prefix, entries


def extract_plugin(zip_path, target):
    with zipfile.ZipFile(zip_path) as archive:
        config, prefix, entries = inspect_archive(archive)
        for info in entries:
            relative = info.filename[len(prefix):] if info.filename.startswith(prefix) else ''
            if not relative:
                continue
            dest = Path(target).joinpath(*PurePosixPath(relative).parts)
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, dest.open('wb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if info.external_attr >> 16 & 0o111:
                    dest.chmod(dest.stat().st_mode | 0o111)
    return config


def pack_source(directory, output):
    source = Path(directory).resolve(strict=True)
    if not source.is_dir() or not (source / 'config.json').is_file():
        raise ValueError('请选择包含 config.json 的插件目录')
    ignored = {'.git', '__pycache__', '.cache', '.venv'}
    count = total = 0
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for current, directories, files in os.walk(source, followlinks=False):
            directories[:] = sorted(d for d in directories if d not in ignored)
            for name in directories + files:
                child = Path(current, name)
                if child.is_symlink() or getattr(child, 'is_junction', lambda: False)():
                    raise ValueError('插件源码不能包含符号链接')
            for name in sorted(files):
                file = Path(current, name)
                if file.resolve() == Path(output).resolve():
                    continue
                if name in ('.env', 'id_rsa', 'id_ed25519') or 'private' in name.lower() and name.lower().endswith(('.pem', '.key')):
                    raise ValueError('源码目录包含疑似凭据文件，请先移出: ' + name)
                count += 1
                total += file.stat().st_size
                if count > MAX_FILES or total > MAX_EXPANDED:
                    raise ValueError('插件目录超过文件数量或大小限制')
                archive.write(file, file.relative_to(source).as_posix())
    with zipfile.ZipFile(output) as archive:
        config, _, _ = inspect_archive(archive)
    return config, count


def package_upload(zip_path, output, version):
    """Write an upload copy with the explicitly selected version; preserve source files."""
    if not isinstance(version, str) or len(version) > 128 or not re.fullmatch(
            r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?', version):
        raise ValueError('版本号必填，格式例如 1.0.0 或 1.0.0-beta.1')
    if Path(zip_path).resolve() == Path(output).resolve():
        raise ValueError('上传副本不能覆盖原安装包')
    with zipfile.ZipFile(zip_path) as source:
        config, prefix, entries = inspect_archive(source)
        config['version'] = version
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as target:
            for info in entries:
                if info.filename == prefix + 'config.json':
                    target.writestr(info, json.dumps(config, ensure_ascii=False, indent=2).encode('utf-8'))
                else:
                    with source.open(info) as src, target.open(info, 'w') as dest:
                        shutil.copyfileobj(src, dest, 1024 * 1024)
    return config, len(entries)


def replace_plugin(zip_path, destination):
    """Stage beside the destination; leave the previous installation on failure."""
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix='.plugin-install-', dir=dest.parent)
    preserve_backup = False
    try:
        stage, backup = Path(temporary) / 'new', Path(temporary) / 'previous'
        stage.mkdir()
        config = extract_plugin(zip_path, stage)
        if dest.is_symlink() or getattr(dest, 'is_junction', lambda: False)():
            raise ValueError('安装目录不能是符号链接')
        if dest.exists():
            old_path = dest / 'config.json'
            old = json.loads(old_path.read_text(encoding='utf-8-sig')) if old_path.exists() else {}
            if old.get('id') != config.get('id'):
                raise ValueError('安装目标与插件 ID 不一致')
            old_source, source = old.get('marketplace'), config.get('marketplace')
            if old_source != source and ((old_source or {}).get('source') != (source or {}).get('source')
                                         or (old_source or {}).get('owner_github_id') != (source or {}).get('owner_github_id')):
                raise ValueError('不能用不同来源或不同作者的插件覆盖已有安装')
            os.replace(dest, backup)
        try:
            os.replace(stage, dest)
        except BaseException:
            if backup.exists():
                try:
                    os.replace(backup, dest)
                except OSError as exc:
                    preserve_backup = True
                    raise RuntimeError('恢复旧插件失败，旧版本已保留在: ' + str(backup)) from exc
            raise
        return config
    finally:
        if not preserve_backup:
            shutil.rmtree(temporary, ignore_errors=True)
