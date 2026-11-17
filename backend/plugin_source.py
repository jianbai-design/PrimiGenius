"""Content-addressed text snapshots shared by desktop uploads and trusted review."""
import hashlib
from pathlib import Path

CHUNK_SIZE = 128 * 1024
MAX_GIT_FILE = 100 * 1024 * 1024
IGNORED = {'.git', '__pycache__', '.cache', '.venv'}


def blob_sha(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def text_chunks(file):
    """Bounded UTF-8 chunks; prefer physical line boundaries without truncating long lines."""
    pending = b''
    with file.open('rb') as stream:
        while True:
            raw = pending + stream.read(CHUNK_SIZE - len(pending))
            pending = b''
            if not raw:
                return
            if b'\0' in raw:
                raise UnicodeError('binary')
            cut = len(raw)
            if len(raw) == CHUNK_SIZE:
                newline = raw.rfind(b'\n')
                if newline >= CHUNK_SIZE // 2:
                    cut = newline + 1
                if raw[cut - 1:cut] == b'\r':
                    cut -= 1
            try:
                raw[:cut].decode('utf-8')
            except UnicodeDecodeError as exc:
                if exc.reason != 'unexpected end of data':
                    raise
                cut = exc.start
                raw[:cut].decode('utf-8')
            if not cut:
                raise UnicodeError('invalid UTF-8')
            pending = raw[cut:]
            yield raw[:cut]


def source_index(directory, modes=None):
    root = Path(directory)
    files, omitted = {}, {}
    for file in sorted(root.rglob('*')):
        if not file.is_file():
            continue
        name = file.relative_to(root).as_posix()
        if any(part in IGNORED for part in file.relative_to(root).parts):
            omitted[name] = 'cache'
            continue
        size = file.stat().st_size
        digest = hashlib.sha256()
        git = hashlib.sha1(b'blob ' + str(size).encode() + b'\0')
        chunks, offset, line, continued = [], 0, 1, False
        try:
            for raw in text_chunks(file):
                digest.update(raw)
                git.update(raw)
                chunks.append({'sha': blob_sha(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                               'offset': offset, 'size': len(raw), 'startLine': line, 'continuedLine': continued})
                offset += len(raw)
                line += raw.count(b'\n')
                continued = not raw.endswith(b'\n')
        except UnicodeError:
            omitted[name] = 'binary'
            continue
        if offset != size:
            raise ValueError('源码文件发生变化: ' + name)
        if size >= MAX_GIT_FILE:
            raise ValueError('文本源码超过 GitHub 单文件大小限制: ' + name)
        files[name] = {'sha': git.hexdigest(), 'sha256': digest.hexdigest(), 'size': size,
                       'mode': (modes or {}).get(name, '100755' if file.stat().st_mode & 0o111 else '100644'), 'chunks': chunks}
    return {'schema': 1, 'files': files, 'omitted': omitted}
