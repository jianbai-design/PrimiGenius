"""Own the in-process Libpod engine and its local Windows/WSL transport.

No Windows Podman client, TCP endpoint, or service-discovery fallback is used.
The manager remains the sole owner of recovery and retry scheduling.
"""
import hashlib
import json
import ntpath
import os
import subprocess
import threading
import time

import docker

RUNTIME_PROBE_TIMEOUT = 3.0


def _wsl_bind_source(source):
    # Preserve Linux paths and named volumes; only absolute drive paths map to WSL.
    if isinstance(source, str) and len(source) >= 3 and source[0].isalpha() and source[1] == ':' and source[2] in '/\\':
        drive, tail = ntpath.splitdrive(ntpath.normpath(source))
        return '/mnt/' + drive[0].lower() + '/' + tail.replace('\\', '/').lstrip('/')
    return source


class OwnedDockerClient(docker.DockerClient):
    def __init__(self, *, owner, **kwargs):
        super().__init__(**kwargs)
        self._control = docker.APIClient(base_url=kwargs['base_url'] + '-control',
                                         version=kwargs.get('version', '1.41'), timeout=10)
        self.api.stop_container = self._control.stop
        self.api.stop = self._control.stop
        self.api.kill = self._control.kill
        create = self.api.create_container

        def create_owned(*args, **options):
            labels = options.get('labels') or {}
            if isinstance(labels, list):
                labels = dict(item.split('=', 1) if '=' in item else (item, '') for item in labels)
            options['labels'] = {**labels, 'primigenius.owner': owner}
            host = options.get('host_config')
            if host:
                host = dict(host)
                if host.get('Mounts'):
                    host['Mounts'] = [dict(m, Source=_wsl_bind_source(m.get('Source')))
                                      if m.get('Type') == 'bind' else m for m in host['Mounts']]
                if host.get('Binds'):
                    binds = []
                    for bind in host['Binds']:
                        # The first colon belongs to a Windows drive, the second
                        # separates source and target. Preserve target/mode exactly.
                        if len(bind) > 2 and bind[1] == ':' and bind[2] in '/\\':
                            source, separator, target = bind[2:].partition(':')
                            if separator:
                                bind = _wsl_bind_source(bind[:2] + source) + ':' + target
                        binds.append(bind)
                    host['Binds'] = binds
                options['host_config'] = host
            return create(*args, **options)

        self.api.create_container = create_owned

    def close(self):
        self._control.close()
        super().close()


class EmbeddedRuntime:
    def __init__(self, manager):
        self.manager = manager
        roots = [os.path.join(manager.app_install_dir, 'resources', 'runtime'),
                 os.path.join(manager.app_install_dir, 'runtime', 'release'),
                 os.path.join(manager.app_install_dir, 'runtime', 'dist')]
        self.bundle = next((p for p in roots if os.path.isfile(os.path.join(p, 'manifest.json'))), roots[-1])
        # The pipe ACL, enforced by the bridge, is restricted to the current SID.
        identity = '|'.join((os.path.normcase(os.path.abspath(manager.app_install_dir)),
                             manager.runtime_channel, os.environ.get('USERDOMAIN', ''),
                             os.environ.get('USERNAME', '')))
        self.identity = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
        self.pipe = r'\\.\pipe\primigenius-' + self.identity
        self.url = 'npipe:////./pipe/primigenius-' + self.identity
        self.control_url = self.url + '-control'
        self.socket = '/run/primigenius/' + self.identity + '/engine.sock'
        self.process = None
        self.client = None
        self.last_error = ''
        self.engine_info = {}
        self._validated = None
        self._lock = threading.RLock()

    def manifest(self):
        with open(os.path.join(self.bundle, 'manifest.json'), encoding='utf-8') as f:
            data = json.load(f)
        if data.get('engine') != 'primigenius-libpod' or data.get('schema_version') != 1:
            raise RuntimeError('Unsupported embedded runtime manifest')
        return data

    def validate(self):
        data = self.manifest()
        required = ('primigenius-engine', 'primigenius-bridge-linux', 'primigenius-bridge.exe')
        signature = [(n, os.stat(os.path.join(self.bundle, n)).st_size,
                      os.stat(os.path.join(self.bundle, n)).st_mtime_ns) for n in required]
        signature.append(('manifest', os.stat(os.path.join(self.bundle, 'manifest.json')).st_mtime_ns))
        if signature == self._validated:
            return data
        for name in required:
            expected = data.get('files', {}).get(name)
            if not expected:
                raise RuntimeError('Runtime artifact has no checksum: ' + name)
            with open(os.path.join(self.bundle, name), 'rb') as f:
                actual = hashlib.file_digest(f, 'sha256').hexdigest()
            if actual != expected:
                raise RuntimeError('Runtime artifact checksum mismatch: ' + name)
        self._validated = signature
        return data

    def available(self):
        try:
            self.validate()
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            self.last_error = str(exc)
            return False

    def api_client(self, timeout=3):
        return docker.APIClient(base_url=self.control_url, version='1.41', timeout=timeout)

    def request(self, method, path, **kwargs):
        """Unversioned internal/Libpod request over the SDK's npipe adapter."""
        timeout = kwargs.pop('timeout', 3)
        client = self.api_client(timeout=timeout)
        try:
            response = client.request(method, client.base_url.rstrip('/') + '/' + path.lstrip('/'), timeout=timeout, **kwargs)
            response.raise_for_status()
            return response, client
        except Exception:
            client.close()
            raise

    def ping(self, timeout=RUNTIME_PROBE_TIMEOUT):
        with self._lock:
            if self.process is not None and self.process.poll() is not None:
                self.last_error = 'Runtime bridge exited; see runtime-bridge.log'
                self.close_client()
                return False
            response = None
            try:
                if self.client is None:
                    self.client = self.api_client(timeout=timeout)
                response = self.client.get(self.client.base_url + '/primigenius/runtime', timeout=timeout)
                response.raise_for_status()
                data = response.json()
                if data.get('engine') != 'primigenius-libpod':
                    raise RuntimeError('The connected process is not the embedded engine')
                self.engine_info = data
                self.last_error = ''
                return True
            except Exception as exc:
                self.last_error = str(exc)
                self.close_client()
                return False
            finally:
                if response is not None:
                    response.close()

    def close_client(self):
        client, self.client = self.client, None
        if client is not None:
            client.close()

    def start(self, timeout=40):
        with self._lock:
            deadline = time.monotonic() + timeout
            if self.ping(timeout=min(RUNTIME_PROBE_TIMEOUT, max(0.05, deadline-time.monotonic()))):
                return True
            self.stop_bridge()
            manifest = self.validate()
            # Do not open an older/newer user's database with an unvalidated ABI.
            check = self.manager._run_wsl('podman --version', timeout=min(6, max(0.1, deadline-time.monotonic())))
            if check is None or check.returncode != 0:
                detail = ((check.stderr or check.stdout) if check is not None else 'WSL command timed out')
                self.last_error = 'Linux environment is unavailable: ' + str(detail).strip()
                # A host diagnostic on stdout is not a yamux stream. Preserve the
                # actual WSL error and leave the registered environment untouched.
                return False
            if check is not None and check.returncode == 0:
                installed = check.stdout.strip().split()[-1]
                if installed != manifest['podman_version']:
                    raise RuntimeError('Existing Linux engine version %s requires a matching embedded build; bundled version is %s. Existing data was preserved.' % (installed, manifest['podman_version']))
            cmd = [os.path.join(self.bundle, 'primigenius-bridge.exe'),
                   '--pipe', self.pipe, '--distro', self.manager.wsl_distro_name,
                   '--helper', self.manager.host_path_to_wsl(os.path.join(self.bundle, 'primigenius-bridge-linux')),
                   '--engine', self.manager.host_path_to_wsl(os.path.join(self.bundle, 'primigenius-engine')),
                   '--socket', self.socket]
            os.makedirs(self.manager.podman_data_dir, exist_ok=True)
            with open(os.path.join(self.manager.podman_data_dir, 'runtime-bridge.log'), 'wb') as log:
                self.process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
            while time.monotonic() < deadline:
                if self.ping(timeout=min(RUNTIME_PROBE_TIMEOUT, max(0.05, deadline-time.monotonic()))):
                    return True
                if self.process.poll() is not None:
                    break
                time.sleep(0.1)
            self.stop_bridge()
            return False

    def stop_bridge(self):
        self.close_client()
        proc, self.process = self.process, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def stop(self):
        with self._lock:
            if self.ping():
                try:
                    response, client = self.request('POST', '/primigenius/shutdown')
                    response.close()
                    client.close()
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and self.ping():
                        time.sleep(0.1)
                except Exception as exc:
                    self.last_error = str(exc)
            self.stop_bridge()

    def status(self):
        state = self.manager._connection_state
        state = {'degraded': 'recovering', 'backoff': 'recovering', 'recovery_failed': 'attention'}.get(state, state)
        return {'state': state, 'transport': 'npipe-stdio-unix',
                'storage_missing': bool(getattr(self.manager, '_wsl_storage_missing', False)),
                'engine': 'primigenius-libpod', 'engine_version': self.engine_info.get('version', ''),
                'engine_ready': state == 'ready', 'last_error': self.last_error if state not in ('stopped', 'undeployed') else '',
                'next_retry_seconds': max(0, round(self.manager._service_retry_after-time.monotonic(), 1))}
