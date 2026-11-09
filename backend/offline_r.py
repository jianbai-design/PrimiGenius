"""Transactional, offline-only preparation of the application-owned R runtime."""
import contextlib
import hashlib
import gzip
import shutil
import json
import os
from pathlib import Path
import threading
import time


def sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_manifest(path, recipe):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema') != 1 or data.get('recipe_sha256') != recipe:
        raise RuntimeError('Bundled R environment version does not match this application')
    for field in ('image_id', 'archive_sha256'):
        value = data.get(field, '').removeprefix('sha256:')
        if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise RuntimeError('Invalid R environment manifest: ' + field)
    if data.get('archive') not in ('environment.tar.gz', 'environment.tar.xz'):
        raise RuntimeError('Invalid R environment archive name')
    for field in ('archive_bytes', 'image_bytes', 'required_free_bytes'):
        if not isinstance(data.get(field), int) or data[field] <= 0:
            raise RuntimeError('Invalid R environment size: ' + field)
    return data


class _StreamingArchive:
    """Do not expose the compressed file's length as the decoded HTTP length."""
    def __init__(self, stream):
        self.stream = stream

    def read(self, size=-1):
        return self.stream.read(size)

    def __iter__(self):
        return iter(lambda: self.read(1024 * 1024), b'')


@contextlib.contextmanager
def image_archive_stream(path):
    if Path(path).suffix == '.xz':
        import lzma
        # Native host decompression is faster than the engine's pure-Go XZ
        # decoder. Chunked transfer avoids a second, decompressed host archive.
        with lzma.open(path, 'rb') as stream:
            yield _StreamingArchive(stream)
    else:
        # Keep the proven gzip/recovery transport unchanged.
        with open(path, 'rb') as stream:
            yield stream


@contextlib.contextmanager
def preparation_lock(path):
    """A separate OS lock; never reuse the package-library lock."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0')
            handle.flush()
        if os.name == 'nt':
            import msvcrt
            deadline = time.monotonic() + 1800
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Another process is preparing the R environment')
                    time.sleep(.2)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class OfflineR:
    def __init__(self, bundle, state_dir, recipe, legacy_ref, matches, validate,
                 client_factory, check_space, log, consume_archive=False, on_state_change=None):
        self.bundle, self.state_dir = Path(bundle), Path(state_dir)
        self.recipe, self.legacy_ref = recipe, legacy_ref
        self.matches, self.validate = matches, validate
        self.client_factory, self.check_space, self.log = client_factory, check_space, log
        self.consume_archive = consume_archive
        self.on_state_change = on_state_change
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._worker = None
        self.image_id = None
        self.previous_image_id = None
        self._status = dict(state='pending', ready=False, phase='waiting', error=None)

    def snapshot(self):
        return dict(self._status, image_id=self.image_id, previous_image_id=self.previous_image_id, recipe_sha256=self.recipe,
                    transport='local', deployment='bundled-offline')

    def _set(self, state, phase, error=None):
        self._status = dict(state=state, ready=state == 'ready', phase=phase, error=error)
        self.log('[R-ENV] ' + phase + (': ' + str(error) if error else ''))
        if self.on_state_change:
            try:
                self.on_state_change(self.snapshot())
            except Exception as exc:
                self.log('[WARN] Could not publish R environment state: ' + str(exc))

    def start(self):
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                return
            if self.image_id:
                return
            def work():
                try:
                    self.ensure()
                except Exception:
                    pass  # recorded by ensure; explicit retry is available
            self._worker = threading.Thread(target=work, daemon=True, name='r-environment')
            self._worker.start()

    def ensure(self):
        if self.image_id:
            return self.image_id
        with self._lock:
            if self.image_id:
                return self.image_id
            self._set('preparing', 'checking')
            client = None
            try:
                with preparation_lock(self.state_dir / 'prepare.lock'):
                    client = self.client_factory()
                    if client is None:
                        raise RuntimeError('Local engine is not ready; retry after it starts')
                    manifest_path = self.bundle / 'manifest.json'
                    manifest = read_manifest(manifest_path, self.recipe) if manifest_path.is_file() else None
                    state_file = self.state_dir / 'active.json'
                    try:
                        old_state = json.loads(state_file.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        old_state = {}
                    refs = [manifest.get('image_id') if manifest else None,
                            old_state.get('image_id'), self.legacy_ref]
                    image = None
                    retained_id = old_state.get('image_id')
                    import docker
                    for ref in dict.fromkeys(filter(None, refs)):
                        try:
                            candidate = client.images.get(ref)
                        except docker.errors.ImageNotFound:
                            continue
                        if self.matches(candidate):
                            if ref == self.legacy_ref and not retained_id:
                                retained_id = candidate.id
                            try:
                                if not (old_state.get('validation_schema') == 1 and
                                        old_state.get('recipe_sha256') == self.recipe and
                                        old_state.get('image_id') == candidate.id):
                                    self._set('preparing', 'validating-existing')
                                    self.validate(client, candidate.id)
                                image = candidate
                                break
                            except Exception as exc:
                                self.log('[R-ENV] Existing environment retained but not activated: ' + str(exc))
                                if manifest and candidate.id == manifest['image_id']:
                                    # Reimporting the identical immutable image cannot
                                    # repair an incompatible external package library.
                                    raise
                    if image is None:
                        if not manifest:
                            raise RuntimeError('Offline R resources are missing. Run the installer containing the matching R environment to repair this installation.')
                        archive = self.bundle / 'stage' / manifest['archive']
                        recovery_manifest = self.state_dir / 'recovery' / 'manifest.json'
                        if not archive.is_file() and recovery_manifest.is_file():
                            manifest = read_manifest(recovery_manifest, self.recipe)
                            archive = recovery_manifest.parent / manifest['archive']
                        if not archive.is_file():
                            raise RuntimeError('Offline R archive is missing. Run the installer containing the matching R environment to restore it, then retry.')
                        self._set('preparing', 'verifying-archive')
                        if archive.stat().st_size != manifest['archive_bytes'] or sha256(archive) != manifest['archive_sha256']:
                            raise RuntimeError('Offline R archive checksum failed; repair using the installer containing the matching R environment')
                        self.check_space(manifest)
                        self._set('preparing', 'importing')
                        with image_archive_stream(archive) as stream:
                            events = client.api.load_image(stream)
                            try:
                                for event in events:
                                    if isinstance(event, (bytes, str)):
                                        event = json.loads(event)
                                    if event.get('error'):
                                        raise RuntimeError(event['error'])
                            finally:
                                close = getattr(events, 'close', None)
                                if close:
                                    close()
                        image = client.images.get(manifest['image_id'])
                        if not self.matches(image):
                            raise RuntimeError('Imported R environment does not match its recipe')
                        self._set('preparing', 'validating')
                        self.validate(client, image.id)
                    repository, tag = self.legacy_ref.rsplit(':', 1) if ':' in self.legacy_ref else (self.legacy_ref, 'latest')
                    if not client.api.tag(image.id, repository=repository, tag=tag):
                        raise RuntimeError('Cannot assign the compatible R environment image name')
                    # Publish only after validation; keep the previous identity for rollback.
                    state = dict(schema=1, validation_schema=1, image_id=image.id, recipe_sha256=self.recipe,
                                 previous_image_id=(retained_id if retained_id != image.id
                                                    else old_state.get('previous_image_id')), validated_at=int(time.time()))
                    temp = state_file.with_suffix('.tmp')
                    with temp.open('w', encoding='utf-8') as stream:
                        json.dump(state, stream, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, state_file)
                    self.image_id = image.id
                    self.previous_image_id = state['previous_image_id']
                    self._set('ready', 'ready')
                    if manifest and self.consume_archive:
                        archive = self.bundle / 'stage' / manifest['archive']
                        try:
                            archive.unlink(missing_ok=True)
                            # A restored image can generate another archive on
                            # explicit deletion; do not retain a second full copy.
                            (self.state_dir / 'recovery' / 'environment.tar.gz').unlink(missing_ok=True)
                        except OSError as exc:
                            self.log('[R-ENV] Environment ready; temporary archive cleanup pending: ' + str(exc))
                    return image.id
            except Exception as exc:
                self._set('attention', 'failed', str(exc))
                raise
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

    def remove(self, image_id, confirmed=False):
        """Delete only after a verified local recovery archive is durable."""
        if confirmed is not True:
            raise ValueError('Deleting the built-in R environment requires explicit confirmation')
        with self._lock, preparation_lock(self.state_dir / 'prepare.lock'):
            client = self.client_factory()
            try:
                target = client.images.get(image_id)
                if not self.matches(target):
                    raise RuntimeError('Image does not match the fixed R environment')
                if client.containers.list(all=True, filters={'ancestor': target.id}):
                    raise RuntimeError('R environment is still used by a container; finish or remove that task first')
                recovery = self.state_dir / 'recovery'
                recovery.mkdir(parents=True, exist_ok=True)
                archive = recovery / 'environment.tar.gz'
                manifest_file = recovery / 'manifest.json'
                valid = False
                if manifest_file.is_file() and archive.is_file():
                    manifest = read_manifest(manifest_file, self.recipe)
                    valid = (manifest['image_id'] == target.id and
                             archive.stat().st_size == manifest['archive_bytes'] and
                             sha256(archive) == manifest['archive_sha256'])
                if not valid:
                    size = int(target.attrs.get('Size') or 0)
                    if size <= 0 or shutil.disk_usage(recovery).free < size + 512 * 1024 * 1024:
                        raise RuntimeError('Insufficient space to preserve an offline R recovery archive; image was not deleted')
                    temp = archive.with_suffix('.tmp')
                    self._set('preparing', 'saving-recovery')
                    stream = target.save(chunk_size=1024 * 1024)
                    try:
                        with temp.open('wb') as output:
                            with gzip.GzipFile(fileobj=output, mode='wb', mtime=0, compresslevel=6) as compressed:
                                for chunk in stream:
                                    compressed.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        # Read the entire compressed stream to verify its CRC before deletion.
                        with gzip.open(temp, 'rb') as verify:
                            while verify.read(1024 * 1024):
                                pass
                        manifest = dict(schema=1, recipe_sha256=self.recipe, image_id=target.id,
                            archive='environment.tar.gz', archive_bytes=temp.stat().st_size,
                            archive_sha256=sha256(temp), image_bytes=size,
                            required_free_bytes=size * 2 + 512 * 1024 * 1024)
                        os.replace(temp, archive)
                        pending = manifest_file.with_suffix('.tmp')
                        with pending.open('w', encoding='utf-8') as output:
                            json.dump(manifest, output)
                            output.flush()
                            os.fsync(output.fileno())
                        os.replace(pending, manifest_file)
                    finally:
                        close = getattr(stream, 'close', None)
                        if close:
                            close()
                        temp.unlink(missing_ok=True)
                # Never force removal: an analysis may have started during export.
                client.images.remove(target.id, force=False, noprune=True)
                self.image_id = None
                self._set('attention', 'removed', 'R environment was removed. Use Restore environment to recover it offline.')
            except Exception:
                if self.image_id:
                    self._set('ready', 'ready')
                raise
            finally:
                client.close()

    def cleanup_previous(self, confirmed=False):
        if confirmed is not True:
            raise ValueError('Explicit confirmation is required')
        with self._lock, preparation_lock(self.state_dir / 'prepare.lock'):
            state_file = self.state_dir / 'active.json'
            state = json.loads(state_file.read_text(encoding='utf-8'))
            previous = state.get('previous_image_id')
            if not self.image_id or state.get('image_id') != self.image_id or not previous or previous == self.image_id:
                raise RuntimeError('No unused previous R environment is available')
            client = self.client_factory()
            try:
                image = client.images.get(previous)
                labels = (image.attrs.get('Config') or {}).get('Labels') or {}
                if not labels.get('org.primigenius.r-version'):
                    raise RuntimeError('Previous image is not an application R environment')
                if client.containers.list(all=True, filters={'ancestor': previous}):
                    raise RuntimeError('Previous environment is still referenced by a container')
                client.images.remove(previous, force=False, noprune=True)
                state['previous_image_id'] = None
                temp = state_file.with_suffix('.tmp')
                temp.write_text(json.dumps(state, indent=2), encoding='utf-8')
                os.replace(temp, state_file)
                self.previous_image_id = None
            finally:
                client.close()
