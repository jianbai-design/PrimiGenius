import os
import sys
import json
import time
import shutil
import subprocess
import threading
import platform
import logging
import re
import shlex
import queue
import locale
from embedded_runtime import EmbeddedRuntime, RUNTIME_PROBE_TIMEOUT

DEFAULT_DOCKERHUB_MIRRORS = [
    'docker.1ms.run',
    'docker.sparkcr.cn',
    'dockerproxy.net',
    'docker.1panel.live',
    'proxy.vvvv.ee',
    'hub.rat.dev',
    'docker-pull.ygxz.in',
    'docker.m.daocloud.io',
]

# Ignore retired presets from older app-generated configurations. Explicit
# custom mirrors are loaded separately and remain under the user's control.
RETIRED_DOCKERHUB_MIRRORS = frozenset({
    'dytt.online',
    'docker-0.unsee.tech',
    'dockerproxy.link',
    'dockerproxy.com',
    'docker.jiaxin.site',
    'registry.cyou',
    'docker.xuanyuan.me',
    'docker.nju.edu.cn',
    'docker.mirrors.sjtug.sjtu.edu.cn',
    '05f073ad3c0010ea0f4bc00b7105ec20.mirror.swr.myhuaweicloud.com',
})


def _run_elevated_windows_command(executable, args, timeout=60):
    """Run one fixed Windows command through UAC and return its exit code.

    This deliberately accepts an executable plus an argument list rather than
    a shell command.  Callers must keep both fixed: the elevation boundary is
    only for narrowly scoped host recovery, never user-provided input.
    """
    if platform.system() != 'Windows':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ('cbSize', wintypes.DWORD),
                ('fMask', wintypes.ULONG),
                ('hwnd', wintypes.HWND),
                ('lpVerb', wintypes.LPCWSTR),
                ('lpFile', wintypes.LPCWSTR),
                ('lpParameters', wintypes.LPCWSTR),
                ('lpDirectory', wintypes.LPCWSTR),
                ('nShow', ctypes.c_int),
                ('hInstApp', wintypes.HINSTANCE),
                ('lpIDList', wintypes.LPVOID),
                ('lpClass', wintypes.LPCWSTR),
                ('hkeyClass', wintypes.HKEY),
                ('dwHotKey', wintypes.DWORD),
                ('hIconOrMonitor', wintypes.HANDLE),
                ('hProcess', wintypes.HANDLE),
            ]

        shell_execute_ex = ctypes.windll.shell32.ShellExecuteExW
        shell_execute_ex.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
        shell_execute_ex.restype = wintypes.BOOL
        wait_for_single_object = ctypes.windll.kernel32.WaitForSingleObject
        get_exit_code_process = ctypes.windll.kernel32.GetExitCodeProcess
        close_handle = ctypes.windll.kernel32.CloseHandle

        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
        info.lpVerb = 'runas'
        info.lpFile = executable
        info.lpParameters = subprocess.list2cmdline(list(args))
        info.nShow = 0  # SW_HIDE; the UAC consent UI remains visible.
        if not shell_execute_ex(ctypes.byref(info)):
            error_code = ctypes.get_last_error()
            logger.warning(f'[RuntimeManager] Elevated recovery was not started (Windows error {error_code})')
            return None
        if not info.hProcess:
            return None
        try:
            wait_result = wait_for_single_object(info.hProcess, max(1, int(timeout * 1000)))
            if wait_result != 0:  # WAIT_OBJECT_0
                logger.warning('[RuntimeManager] Elevated recovery did not finish before the timeout')
                return None
            exit_code = wintypes.DWORD()
            if not get_exit_code_process(info.hProcess, ctypes.byref(exit_code)):
                return None
            return int(exit_code.value)
        finally:
            close_handle(info.hProcess)
    except Exception as e:
        logger.warning(f'[RuntimeManager] Failed to launch elevated recovery: {e}')
        return None

_WINDOWS_NO_WINDOW = 0x08000000 | 0x00000200
_SUBPROCESS_KWARGS = {}
if platform.system() == 'Windows':
    _SUBPROCESS_KWARGS['creationflags'] = _WINDOWS_NO_WINDOW
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    _SUBPROCESS_KWARGS['startupinfo'] = si

def _run_hidden(cmd, timeout=60, capture=True, env=None, raise_timeout=False):
    if platform.system() != 'Windows':
        kw = {}
        if env:
            kw['env'] = env
        if capture:
            return _decode_completed_process(
                subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
            )
        else:
            return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    kw = dict(_SUBPROCESS_KWARGS)
    if env:
        kw['env'] = env
    try:
        if capture:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
            return _decode_completed_process(result)
        else:
            kw['stdout'] = subprocess.DEVNULL
            kw['stderr'] = subprocess.DEVNULL
            proc = subprocess.Popen(cmd, **kw)
            return proc
    except subprocess.TimeoutExpired:
        if raise_timeout:
            raise
        logger.warning('[RuntimeManager] Command timed out after %ss: %s', timeout, cmd)
        return None
    except FileNotFoundError:
        exe_name = cmd[0] if cmd else 'unknown'
        logger.error(f'[RuntimeManager] Command not found: {exe_name}. Ensure the executable exists and is in PATH.')
        return None
    except PermissionError:
        exe_name = cmd[0] if cmd else 'unknown'
        logger.error(f'[RuntimeManager] Permission denied running: {exe_name}. Try running as administrator.')
        return None
    except Exception as e:
        logger.error(f'[RuntimeManager] _run_hidden failed: {e}, cmd={cmd}')
        import traceback
        logger.error(f'[RuntimeManager] _run_hidden traceback: {traceback.format_exc()}')
        return None


def _run_hidden_streaming(cmd, timeout=600, env=None):
    try:
        if platform.system() != 'Windows':
            kw = {}
            if env:
                kw['env'] = env
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        else:
            kw = dict(_SUBPROCESS_KWARGS)
            if env:
                kw['env'] = env
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    except Exception as e:
        logger.error(f'[RuntimeManager] Failed to start streaming command: {e}')
        return None

    output_lines = []
    line_queue = queue.Queue()
    start_time = time.time()

    def _reader():
        try:
            for line in iter(proc.stdout.readline, b''):
                line_queue.put(line)
        finally:
            line_queue.put(None)

    threading.Thread(target=_reader, daemon=True).start()
    try:
        while True:
            try:
                line = line_queue.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None and line_queue.empty():
                    break
                if timeout and (time.time() - start_time) > timeout:
                    proc.kill()
                    logger.error(f'[RuntimeManager] Command timed out after {timeout}s')
                    return None
                continue
            if line is None:
                if proc.poll() is not None:
                    break
                continue
            decoded = _decode_command_output(line).strip()
            if decoded:
                output_lines.append(decoded)
                logger.info(f'[RuntimeManager] [INIT] {decoded}')
            if timeout and (time.time() - start_time) > timeout:
                proc.kill()
                logger.error(f'[RuntimeManager] Command timed out after {timeout}s')
                return None
        proc.wait(timeout=30)
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        logger.error(f'[RuntimeManager] Streaming command failed: {e}')
        return None

    stdout = '\n'.join(output_lines)
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, '')
    return result

logger = logging.getLogger('podman_manager')

LEGACY_WSL_DISTRO_NAME = 'podman-machine-default'
STABLE_WSL_DISTRO_NAME = 'primigenius-stable'
DEV_WSL_DISTRO_NAME = 'primigenius-dev'
MACHINE_RUNTIME_CONFIG = 'machine-runtime.json'


def _runtime_channel():
    explicit = str(os.environ.get('PRIMIGENIUS_CHANNEL') or '').strip().lower()
    if explicit in ('dev', 'development'):
        return 'dev'
    if explicit in ('stable', 'production', 'prod'):
        return 'stable'
    return 'stable' if getattr(sys, 'frozen', False) else 'dev'


def choose_machine_name(channel, persisted_name=None, legacy_owned=False):
    """Pure selection policy used by startup and regression tests."""
    from runtime_selection import owned_machine
    if owned_machine(persisted_name, channel):
        return persisted_name
    if channel == 'dev':
        return DEV_WSL_DISTRO_NAME
    if persisted_name == LEGACY_WSL_DISTRO_NAME:
        return persisted_name
    if legacy_owned:
        return LEGACY_WSL_DISTRO_NAME
    return STABLE_WSL_DISTRO_NAME


def machine_to_wsl_distro_name(machine_name):
    """Map Runtime CLI machine names to the WSL distro names Runtime registers."""
    if machine_name == LEGACY_WSL_DISTRO_NAME:
        return LEGACY_WSL_DISTRO_NAME
    return f'podman-{machine_name}'


RUNTIME_CHANNEL = _runtime_channel()
_INITIAL_MACHINE_NAME = DEV_WSL_DISTRO_NAME if RUNTIME_CHANNEL == 'dev' else STABLE_WSL_DISTRO_NAME
WSL_DISTRO_NAME = machine_to_wsl_distro_name(_INITIAL_MACHINE_NAME)
PODMAN_API_HOST = None # Legacy import compatibility; no container TCP listener
PODMAN_API_PORT = None
PODMAN_TCP_URL = None
DOCKER_API_VERSION = '1.41'
DEFAULT_CONTAINERS_CONF = '[containers]\n\n[engine]\n\n[machine]\n'


def _decode_command_output(data):
    if data is None:
        return ''
    if isinstance(data, str):
        return data.replace('\x00', '')
    sample = data[:200]
    if data.startswith((b'\xff\xfe', b'\xfe\xff')) or sample.count(b'\x00') > max(2, len(sample) // 5):
        encodings = ['utf-16', 'utf-16le', 'utf-8', locale.getpreferredencoding(False) or 'utf-8', 'gbk']
    else:
        encodings = ['utf-8', locale.getpreferredencoding(False) or 'utf-8', 'gbk', 'utf-16', 'utf-16le']
    for enc in encodings:
        try:
            return data.decode(enc).replace('\x00', '')
        except Exception:
            continue
    return data.decode('utf-8', errors='replace').replace('\x00', '')


def _decode_completed_process(result):
    if result is None:
        return None
    result.stdout = _decode_command_output(result.stdout)
    result.stderr = _decode_command_output(result.stderr)
    return result


def _prepend_path_once(path_value, new_entry):
    if not new_entry:
        return path_value or ''
    sep = os.pathsep
    normalized_new = os.path.normcase(os.path.normpath(new_entry))
    entries = [p for p in (path_value or '').split(sep) if p]
    for entry in entries:
        try:
            if os.path.normcase(os.path.normpath(entry)) == normalized_new:
                return path_value or ''
        except Exception:
            continue
    return new_entry + (sep + (path_value or '') if path_value else '')


class PodmanManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, app_install_dir=None):
        if self._initialized:
            return
        self._initialized = True

        if app_install_dir is None:
            if getattr(sys, 'frozen', False):
                app_install_dir = os.path.abspath(
                    os.path.join(os.path.dirname(sys.executable), '..', '..'))
            else:
                app_install_dir = os.path.abspath(
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

        self.app_install_dir = app_install_dir
        self.podman_dir = os.path.join(app_install_dir, 'podman')
        self.runtime_channel = _runtime_channel()
        data_dir_name = 'podman-data-dev' if self.runtime_channel == 'dev' else 'podman-data'
        config_dir_name = 'podman-config-dev' if self.runtime_channel == 'dev' else 'podman-config'
        self.podman_config_dir = os.path.join(app_install_dir, config_dir_name)
        self.machine_runtime_config = os.path.join(self.podman_config_dir, MACHINE_RUNTIME_CONFIG)
        default_data_dir = os.path.join(app_install_dir, data_dir_name)
        self.podman_data_dir = self._load_persisted_data_dir(default_data_dir)
        self.machine_name, self.machine_selection_reason = self._select_machine_name()
        self.wsl_distro_name = machine_to_wsl_distro_name(self.machine_name)
        global WSL_DISTRO_NAME
        WSL_DISTRO_NAME = self.wsl_distro_name

        logger.info(f'[RuntimeManager] app_install_dir={app_install_dir}')
        logger.info(f'[RuntimeManager] runtime_directory={self.podman_dir}')
        logger.info(f'[RuntimeManager] sys.executable={sys.executable}')
        logger.info(f'[RuntimeManager] sys.frozen={getattr(sys, "frozen", False)}')
        logger.info(f'[RuntimeManager] runtime_channel={self.runtime_channel}')
        logger.info(f'[RuntimeManager] machine_name={self.machine_name} ({self.machine_selection_reason})')
        logger.info(f'[RuntimeManager] wsl_distro_name={self.wsl_distro_name}')
        logger.info(f'[RuntimeManager] api_port={PODMAN_API_PORT}')

        self.podman_exe = 'podman' # Linux-only compatibility

        self._api_service_proc = None
        self._api_client = None
        self._api_client_lock = threading.RLock()
        self._wsl_service_started = False
        self._engine_ready_cache = {'value': False, 'time': 0.0}
        self._wsl_distro_cache = {'value': None, 'time': 0.0}
        self._wsl_storage_missing = bool(self._runtime_selection_error)
        self._wsl_ip_cache = {'value': None, 'time': 0.0}
        self._podman_api_host = PODMAN_API_HOST
        self._last_wsl_config_sync = 0.0
        self._last_start_diagnostics = ''
        self._setup_lock = threading.Lock()
        self._setup_running = False
        self._wsl_recovery_lock = threading.Lock()
        self._wsl_recovery_retry_after = 0.0
        self._last_wsl_recovery_error = ''
        # API service lifecycle is owned by this manager. All callers share the
        # same lock/state so concurrent UI and worker requests cannot launch or
        # kill competing Runtime service processes.
        self._service_lock = threading.RLock()
        self._connection_state = 'stopped'
        self._service_failures = 0
        self._service_retry_after = 0.0
        self._runtime_desired = False
        self._runtime_monitor_started = False
        self.on_state_change = None

        self.runtime = EmbeddedRuntime(self)
        if self._runtime_selection_error:
            self.runtime.last_error = self._runtime_selection_error
            self._connection_state = 'attention'
            logger.warning('[RuntimeManager] %s', self._runtime_selection_error)
        else:
            self._setup_environment()
            self._persist_machine_selection()

        self._machine_lock = threading.Lock()

        logger.info(f'[RuntimeManager] Initialized Linux container helper')
        logger.info(f'[RuntimeManager] Data dir: {self.podman_data_dir}')
        logger.info(f'[RuntimeManager] Config dir: {self.podman_config_dir}')

    @staticmethod
    def _path_is_within(path_value, parent_value):
        try:
            path_norm = os.path.normcase(os.path.abspath(path_value))
            parent_norm = os.path.normcase(os.path.abspath(parent_value))
            return os.path.commonpath([path_norm, parent_norm]) == parent_norm
        except Exception:
            return False

    def _load_persisted_machine_name(self):
        try:
            with open(self.machine_runtime_config, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get('channel') != self.runtime_channel:
                return None
            name = str(data.get('machine_name') or '').strip()
            from runtime_selection import owned_machine
            if name == LEGACY_WSL_DISTRO_NAME or owned_machine(name, self.runtime_channel):
                return name
        except Exception:
            pass
        return None

    def _load_persisted_data_dir(self, default_path):
        """Restore a user-selected Runtime storage root across app restarts."""
        try:
            with open(self.machine_runtime_config, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get('channel') != self.runtime_channel:
                return os.path.abspath(default_path)
            saved = str(data.get('data_dir') or '').strip()
            if saved and os.path.isabs(saved):
                return os.path.abspath(saved)
        except Exception:
            pass
        return os.path.abspath(default_path)

    def _select_machine_name(self):
        from runtime_selection import select_runtime
        plan = select_runtime(self.runtime_channel, self.podman_data_dir, self.machine_runtime_config)
        self._runtime_selection_error = plan['error']
        if not plan['error']:
            self.podman_data_dir = plan['data_dir']
            self._runtime_provisioning_pending = plan['pending']
            self._runtime_fresh = plan.get('fresh', False)
        return plan['machine'], plan['reason']

    def _persist_machine_selection(self):
        if getattr(self, '_runtime_selection_error', ''):
            return
        payload = {
            'schema_version': 1,
            'channel': self.runtime_channel,
            'machine_name': self.machine_name,
            'wsl_distro_name': self.wsl_distro_name,
            'selection_reason': self.machine_selection_reason,
            'provisioning_pending': getattr(self, '_runtime_provisioning_pending', False),
            'api_port': PODMAN_API_PORT,
            'data_dir': self.podman_data_dir,
        }
        try:
            if os.path.isfile(self.machine_runtime_config):
                with open(self.machine_runtime_config, encoding='utf-8') as stream:
                    previous = json.load(stream)
                for key in ('requested_resources', 'resource_scope'):
                    if key in previous:
                        payload[key] = previous[key]
                history = previous.get('selection_history', [])
                if not isinstance(history, list):
                    history = []
                if previous.get('machine_name') != self.machine_name or previous.get('data_dir') != self.podman_data_dir:
                    history = history + [{key: previous.get(key) for key in
                                          ('machine_name', 'wsl_distro_name', 'data_dir', 'selection_reason')}]
                if history:
                    payload['selection_history'] = history[-8:]
                if getattr(self, '_runtime_fresh', False) and previous != payload:
                    # Save the old configuration before replacing a lost runtime's
                    # selection; disks and their registrations remain untouched.
                    archive_dir = os.path.join(self.podman_config_dir, 'registration-recovery')
                    os.makedirs(archive_dir, exist_ok=True)
                    archive = os.path.join(archive_dir, 'selection-' + str(time.time_ns()) + '.json')
                    with open(archive, 'x', encoding='utf-8') as stream:
                        json.dump(previous, stream, ensure_ascii=False, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
            os.makedirs(self.podman_config_dir, exist_ok=True)
            tmp_path = self.machine_runtime_config + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8', newline='\n') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write('\n')
            os.replace(tmp_path, self.machine_runtime_config)
            self._runtime_fresh = False
        except Exception as e:
            logger.warning(f'[RuntimeManager] Failed to persist machine selection: {e}')
            if getattr(self, '_runtime_fresh', False):
                self._runtime_selection_error = 'Cannot safely save the new runtime selection: ' + str(e)
                self._wsl_storage_missing = True
                self.runtime.last_error = self._runtime_selection_error
                self._set_connection_state('attention')

    @staticmethod
    def host_path_to_wsl(host_path):
        """Convert an absolute Windows drive path to the corresponding WSL mount path."""
        value = os.path.abspath(str(host_path or '').strip())
        drive, tail = os.path.splitdrive(value)
        if not drive or drive.startswith('\\\\'):
            raise ValueError('Only local absolute drive paths are supported')
        tail = tail.replace('\\', '/').lstrip('/')
        return f'/mnt/{drive[0].lower()}/{tail}'

    def _set_data_dir(self, data_dir):
        self.podman_data_dir = os.path.abspath(data_dir)
        self._setup_environment()
        self._persist_machine_selection()

    def _update_machine_config_paths(self, old_root, new_root):
        """Replace storage-root references in Runtime JSON config files; return backups."""
        backups = {}
        roots = [
            os.path.join(self.podman_config_dir, '.config', 'containers', 'podman', 'machine'),
            os.path.join(self.podman_config_dir, 'containers'),
        ]
        old_variants = (old_root, old_root.replace('\\', '/'))
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _, filenames in os.walk(root):
                for filename in filenames:
                    if not filename.lower().endswith('.json'):
                        continue
                    path_value = os.path.join(dirpath, filename)
                    try:
                        with open(path_value, 'r', encoding='utf-8') as f:
                            original = f.read()
                        updated = original.replace(old_variants[0], new_root)
                        updated = updated.replace(old_variants[1], new_root.replace('\\', '/'))
                        if updated != original:
                            backups[path_value] = original
                            with open(path_value, 'w', encoding='utf-8', newline='\n') as f:
                                f.write(updated)
                    except Exception as e:
                        raise RuntimeError(f'Failed to update Runtime config {path_value}: {e}')
        return backups

    @staticmethod
    def _restore_text_backups(backups):
        for path_value, content in backups.items():
            try:
                with open(path_value, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(content)
            except Exception:
                pass

    def _replace_wsl_base_path(self, old_root, new_root):
        """Move this machine's HKCU WSL BasePath reference and return the old registry value."""
        if platform.system() != 'Windows':
            return None
        import winreg
        root = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Lxss',
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
        try:
            for index in range(winreg.QueryInfoKey(root)[0]):
                child_name = winreg.EnumKey(root, index)
                child = winreg.OpenKey(root, child_name, 0, winreg.KEY_READ | winreg.KEY_WRITE)
                try:
                    name, _ = winreg.QueryValueEx(child, 'DistributionName')
                    if name != self.wsl_distro_name:
                        continue
                    base_path, value_type = winreg.QueryValueEx(child, 'BasePath')
                    if not self._path_is_within(base_path, old_root):
                        raise RuntimeError(f'WSL BasePath is outside the current data directory: {base_path}')
                    relative = os.path.relpath(base_path, old_root)
                    replacement = os.path.join(new_root, relative)
                    winreg.SetValueEx(child, 'BasePath', 0, value_type, replacement)
                    return (child_name, base_path, value_type)
                finally:
                    winreg.CloseKey(child)
        finally:
            winreg.CloseKey(root)
        raise RuntimeError(f'WSL distribution not found: {self.wsl_distro_name}')

    @staticmethod
    def _restore_wsl_base_path(registry_backup):
        if not registry_backup or platform.system() != 'Windows':
            return
        try:
            import winreg
            child_name, old_value, value_type = registry_backup
            key_path = rf'Software\Microsoft\Windows\CurrentVersion\Lxss\{child_name}'
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
            try:
                winreg.SetValueEx(key, 'BasePath', 0, value_type, old_value)
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass

    def relocate_data_dir(self, new_path):
        """Relocate the complete Runtime data root and roll back if the moved machine cannot start."""
        if platform.system() != 'Windows':
            return False, 'Storage migration is currently supported on Windows only'
        source = os.path.abspath(self.podman_data_dir)
        target = os.path.abspath(str(new_path or '').strip())
        if not os.path.isabs(target):
            return False, 'Target path must be absolute'
        if os.path.normcase(source) == os.path.normcase(target):
            return False, 'Target path is the current storage path'
        if self._path_is_within(target, source) or self._path_is_within(source, target):
            return False, 'Target path cannot contain or be inside the current storage path'
        if os.path.exists(target) and os.listdir(target):
            return False, 'Target directory must be empty'
        if not os.path.isdir(source):
            return False, f'Current storage directory not found: {source}'

        os.makedirs(target, exist_ok=True)
        config_backups = {}
        registry_backup = None
        switched = False
        try:
            self.machine_stop()
            _run_hidden(['wsl', '--terminate', self.wsl_distro_name], timeout=30, capture=True)
            copy_result = _run_hidden([
                'robocopy', source, target, '/E', '/COPY:DAT', '/DCOPY:DAT',
                '/R:2', '/W:1', '/XJ', '/NFL', '/NDL', '/NJH', '/NJS', '/NP'
            ], timeout=24 * 60 * 60, capture=True)
            if copy_result is None or copy_result.returncode >= 8:
                detail = ((copy_result.stdout or '') + '\n' + (copy_result.stderr or '')).strip() if copy_result else ''
                raise RuntimeError(f'Failed to copy Runtime data (robocopy code {getattr(copy_result, "returncode", "unknown")}): {detail}')

            config_backups = self._update_machine_config_paths(source, target)
            registry_backup = self._replace_wsl_base_path(source, target)
            self._set_data_dir(target)
            switched = True
            self._wsl_distro_cache = {'value': None, 'time': 0.0}
            ok, message = self.machine_start()
            if not ok:
                raise RuntimeError(message)

            try:
                shutil.rmtree(source)
            except Exception as cleanup_error:
                # The new machine is already verified. Never roll back to an old tree
                # that may have been partially removed; leave the remainder for a later cleanup.
                logger.warning(f'[RuntimeManager] Migration succeeded but old data cleanup failed: {cleanup_error}')
            logger.info(f'[RuntimeManager] Runtime data relocated: {source} -> {target}')
            return True, target
        except Exception as e:
            if switched:
                self.machine_stop()
            self._restore_wsl_base_path(registry_backup)
            self._restore_text_backups(config_backups)
            self._set_data_dir(source)
            self._wsl_distro_cache = {'value': None, 'time': 0.0}
            try:
                self.machine_start()
            except Exception:
                pass
            return False, str(e)



    def _ensure_base_config_files(self):
        os.makedirs(self.podman_config_dir, exist_ok=True)
        os.makedirs(self.podman_data_dir, exist_ok=True)

        containers_conf = os.path.join(self.podman_config_dir, 'containers', 'containers.conf')
        os.makedirs(os.path.dirname(containers_conf), exist_ok=True)
        if not os.path.exists(containers_conf):
            with open(containers_conf, 'w', encoding='utf-8') as f:
                f.write(DEFAULT_CONTAINERS_CONF)

        storage_conf_path = os.path.join(self.podman_config_dir, 'storage.conf')
        if not os.path.exists(storage_conf_path):
            with open(storage_conf_path, 'w', encoding='utf-8') as f:
                f.write('[storage]\ndriver = "overlay"\n')
        return storage_conf_path


    def _setup_environment(self):
        # Native GUI/Java children retain the real Windows user profile.
        self._ensure_base_config_files()

    def _run_podman(self, args, timeout=60, capture=True):
        cmd = ['wsl.exe', '-d', self.wsl_distro_name, '-u', 'root', '--exec', 'podman'] + list(args)
        return _run_hidden(cmd, timeout=timeout, capture=capture)

    def _run_podman_streaming(self, args, timeout=600):
        cmd = ['wsl.exe', '-d', self.wsl_distro_name, '-u', 'root', '--exec', 'podman'] + list(args)
        return _run_hidden_streaming(cmd, timeout=timeout)

    def _run_wsl(self, cmd_str, timeout=30):
        if platform.system() != 'Windows':
            return None
        full_cmd = ['wsl', '-d', self.wsl_distro_name, '-u', 'root', '--exec', '/bin/bash', '-c', cmd_str]
        result = _run_hidden(full_cmd, timeout=timeout, capture=True)
        if result is None:
            logger.error('[RuntimeManager] wsl command failed (hidden runner returned None)')
            return None
        return _decode_completed_process(result)

    def _run_host_command(self, args, timeout=20, raise_timeout=False):
        if args and args[0] == 'wsl':
            result = _run_hidden(args, timeout=timeout, capture=True, raise_timeout=raise_timeout)
            if result is None:
                return None
            return _decode_completed_process(result)
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout, **_SUBPROCESS_KWARGS)
            return _decode_completed_process(result)
        except subprocess.TimeoutExpired:
            if raise_timeout:
                raise
            logger.warning('[RuntimeManager] Host command timed out after %ss: %s', timeout, args)
            return None
        except Exception as e:
            logger.error(f'[RuntimeManager] Host command failed {args}: {e}')
            return None

    def _tcp_docker_host(self):
        # Compatibility method name; all traffic now uses local IPC.
        return self.runtime.url

    def get_api_http_url(self):
        # Native Libpod calls use runtime.request() through the npipe adapter.
        return None


    def pull_image_stream_wsl(self, image_ref, first_output_timeout=120, resolve_timeout=300, idle_timeout=0, total_timeout=21600, cancel_event=None, retry_count=None, bypass_registry_mirrors=False, serialize_layers=False):
        """Pull image inside WSL using podman pull, yielding progress lines.

        Runtime does not continuously print byte progress when stdout is a pipe.
        A large layer can therefore be downloading normally for many minutes
        without producing a new line.  ``idle_timeout`` is intentionally
        disabled by default; callers may still opt into it for diagnostics.

        Args:
            cancel_event: Optional threading.Event; when set, the pull subprocess
                          is killed and the generator ends gracefully.
        """
        try:
            retry_count = int(os.environ.get('PRIMIGENIUS_PULL_RETRIES', '6')) if retry_count is None else int(retry_count)
        except (TypeError, ValueError):
            retry_count = 6
        retry_count = max(0, min(retry_count, 20))
        retry_delay = str(os.environ.get('PRIMIGENIUS_PULL_RETRY_DELAY', '5s') or '5s').strip().lower()
        if not re.fullmatch(r'\d+(?:ms|s|m|h)', retry_delay):
            retry_delay = '5s'
        pull_args = [
            'podman', 'pull',
            f'--retry={retry_count}',
            f'--retry-delay={retry_delay}',
            image_ref,
        ]
        env_parts = []
        if bypass_registry_mirrors:
            env_parts.append('CONTAINERS_REGISTRIES_CONF=/dev/null')
        if serialize_layers:
            env_parts.append("CONTAINERS_CONF_OVERRIDE=<(echo -e '[engine]\\nimage_parallel_copies=1')")
        env_prefix = (' '.join(env_parts) + ' ') if env_parts else ''
        cmd_str = env_prefix + ' '.join(shlex.quote(part) for part in pull_args) + ' 2>&1'
        full_cmd = ['wsl', '-d', self.wsl_distro_name, '-u', 'root', '--exec', '/bin/bash', '-c', cmd_str]
        try:
            proc = subprocess.Popen(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **_SUBPROCESS_KWARGS)
            line_queue = queue.Queue()
            recent_lines = []

            def _reader():
                try:
                    for raw_line in iter(proc.stdout.readline, b''):
                        line_queue.put(raw_line)
                finally:
                    line_queue.put(None)

            threading.Thread(target=_reader, daemon=True).start()
            started = time.monotonic()
            last_output = started
            got_output = False
            active_transfer = False
            reader_done = False

            while True:
                if cancel_event and cancel_event.is_set():
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
                try:
                    raw_line = line_queue.get(timeout=1)
                except queue.Empty:
                    now = time.monotonic()
                    if cancel_event and cancel_event.is_set():
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        return
                    if proc.poll() is not None and line_queue.empty():
                        break
                    if not got_output and first_output_timeout and (now - started) > first_output_timeout:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        yield f'ERROR: podman pull produced no output for {int(first_output_timeout)}s'
                        return
                    if got_output and not active_transfer and resolve_timeout and (now - last_output) > resolve_timeout:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        yield f'ERROR: podman pull registry resolution timeout after {int(resolve_timeout)}s'
                        return
                    if got_output and idle_timeout and (now - last_output) > idle_timeout:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        yield f'ERROR: podman pull idle timeout after {int(idle_timeout)}s'
                        return
                    if total_timeout and (now - started) > total_timeout:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        yield f'ERROR: podman pull total timeout after {int(total_timeout)}s'
                        return
                    continue

                if raw_line is None:
                    reader_done = True
                    if proc.poll() is not None:
                        break
                    continue

                line = raw_line.decode('utf-8', errors='replace').rstrip('\n\r')
                if line:
                    got_output = True
                    last_output = time.monotonic()
                    lower_line = line.lower()
                    if (
                        'copying blob' in lower_line
                        or 'copying config' in lower_line
                        or 'writing manifest' in lower_line
                        or 'pulling fs layer' in lower_line
                        or 'downloading' in lower_line
                    ):
                        active_transfer = True
                    recent_lines.append(line)
                    recent_lines = recent_lines[-12:]
                    yield line

                if reader_done and proc.poll() is not None and line_queue.empty():
                    break

            rc = proc.wait()
            if rc != 0:
                tail = '; '.join(recent_lines[-4:]).strip()
                detail = f' ({tail})' if tail else ''
                yield f'ERROR: podman pull exited with code {rc}{detail}'
        except FileNotFoundError:
            yield 'ERROR: wsl command not found'
        except Exception as e:
            yield f'ERROR: {e}'

    def _write_wsl_registries_conf(self):
        mirrors = self._get_configured_mirrors()
        docker_mirrors = mirrors
        def _merged(items):
            out = []
            for item in items:
                if item and item not in out:
                    out.append(item)
            return out
        k8s_mirrors = _merged([
            'k8s.m.daocloud.io',
            'k8s.mirrors.ustc.edu.cn',
            'k8s.nju.edu.cn',
            'k8s.mirrors.sjtug.sjtu.edu.cn',
        ])
        gcr_mirrors = _merged([
            'gcr.mirrors.ustc.edu.cn',
            'gcr.nju.edu.cn',
            'gcr.mirrors.sjtug.sjtu.edu.cn',
        ])
        ghcr_mirrors = _merged([
            'ghcr.mirrors.ustc.edu.cn',
            'ghcr.nju.edu.cn',
            'ghcr.mirrors.sjtug.sjtu.edu.cn',
        ])
        quay_mirrors = _merged([
            'quay.mirrors.ustc.edu.cn',
            'quay.nju.edu.cn',
            'quay.mirrors.sjtug.sjtu.edu.cn',
        ])

        reg_content = 'unqualified-search-registries = ["docker.io"]\n\n'

        registry_prefixes = [
            ('docker.io', docker_mirrors),
            ('registry-1.docker.io', docker_mirrors),
            ('index.docker.io', docker_mirrors),
            ('k8s.gcr.io', k8s_mirrors),
            ('registry.k8s.io', k8s_mirrors),
            ('gcr.io', gcr_mirrors),
            ('ghcr.io', ghcr_mirrors),
            ('quay.io', quay_mirrors),
        ]

        for prefix, prefix_mirrors in registry_prefixes:
            reg_content += '[[registry]]\n'
            reg_content += f'prefix = "{prefix}"\n'
            reg_content += f'location = "{prefix}"\n'
            for m in prefix_mirrors:
                reg_content += f'  [[registry.mirror]]\n  location = "{m}"\n'
            reg_content += '\n'

        reg_file = os.path.join(self.podman_config_dir, '_registries_tmp.conf')
        with open(reg_file, 'w', encoding='utf-8') as f:
            f.write(reg_content)

        win_path = reg_file.replace('\\', '/')
        wsl_path = f'/mnt/{win_path[0].lower()}{win_path[2:]}'
        result = self._run_wsl(f'mkdir -p /etc/containers && cp {wsl_path} /etc/containers/registries.conf && chmod 644 /etc/containers/registries.conf && echo REG_OK')
        if result and 'REG_OK' in (result.stdout or ''):
            logger.info('[RuntimeManager] WSL registries.conf written successfully.')
        else:
            logger.warning('[RuntimeManager] Failed to write WSL registries.conf, trying echo method...')
            escaped = reg_content.replace("'", "'\\''")
            self._run_wsl(f"mkdir -p /etc/containers && echo '{escaped}' > /etc/containers/registries.conf")

    def _get_configured_mirrors(self, include_defaults=False):
        default_mirrors = list(DEFAULT_DOCKERHUB_MIRRORS)
        config_path = os.path.join(self.podman_config_dir, 'containers', 'containers.conf')
        if not os.path.exists(config_path):
            return default_mirrors if include_defaults else []
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read()
            in_mirrors = False
            mirrors = []
            for line in content.split('\n'):
                stripped = line.strip()
                if stripped.startswith('registry-mirrors'):
                    in_mirrors = True
                    continue
                if stripped.startswith('[') or stripped.startswith('helper_'):
                    in_mirrors = False
                    continue
                if in_mirrors and stripped.startswith('"'):
                    m = stripped.strip('", ')
                    if m and '.' in m:
                        m = m.removeprefix('https://').removeprefix('http://').rstrip('/')
                        if m not in RETIRED_DOCKERHUB_MIRRORS and not m.startswith(('e:', 'c:', 'd:', '\\\\')):
                            mirrors.append(m)
                if stripped == ']':
                    in_mirrors = False
            return mirrors if mirrors else (default_mirrors if include_defaults else [])
        except Exception:
            return default_mirrors if include_defaults else []

    def _wsl_registered_distros_from_cli(self):
        if platform.system() != 'Windows':
            return []
        result = self._run_host_command(['wsl', '--list', '--quiet'], timeout=8)
        if not result or result.returncode != 0:
            return []
        names = []
        for line in (result.stdout or '').splitlines():
            cleaned = line.replace('\x00', '').strip().lstrip('*').strip()
            if cleaned:
                names.append(cleaned)
        return names

    def _is_wsl_distro_registered(self):
        if platform.system() != 'Windows':
            return False
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Lxss')
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                    try:
                        name, _ = winreg.QueryValueEx(subkey, 'DistributionName')
                        if name == self.wsl_distro_name:
                            return True
                    finally:
                        winreg.CloseKey(subkey)
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(key)
        except Exception:
            pass
        try:
            return self.wsl_distro_name in self._wsl_registered_distros_from_cli()
        except Exception:
            return False

    def _is_wsl2_functional(self):
        if platform.system() != 'Windows':
            return True
        result = self._run_host_command(['wsl', '--version'], timeout=10)
        if result and result.returncode == 0:
            out = (result.stdout or '').strip()
            if 'WSL' in out:
                logger.info(f'[RuntimeManager] WSL2 installed: {out.splitlines()[0] if out else "OK"}')
                return True
        result2 = self._run_host_command(['wsl', '--status'], timeout=10)
        if result2 and result2.returncode == 0:
            logger.info('[RuntimeManager] WSL2 status OK')
            return True
        return False

    def _prepare_wsl_registration(self):
        if platform.system() != 'Windows':
            return True
        try:
            from wsl_registration import repair_registration
            with self._service_lock:
                # Retry storage discovery after a user restores a path or an old
                # machine becomes available. Never switch a live engine's owner.
                if (getattr(self, '_runtime_selection_error', '') or
                        getattr(self, '_wsl_storage_missing', False)):
                    process = getattr(self.runtime, 'process', None)
                    if process is not None and process.poll() is None:
                        return False
                    self.machine_name, self.machine_selection_reason = self._select_machine_name()
                    if self._runtime_selection_error:
                        self.runtime.last_error = self._runtime_selection_error
                        self._wsl_storage_missing = True
                        self._set_connection_state('attention')
                        return False
                    self.wsl_distro_name = machine_to_wsl_distro_name(self.machine_name)
                    global WSL_DISTRO_NAME
                    WSL_DISTRO_NAME = self.wsl_distro_name
                    self._reset_wsl_runtime_state()
                    self._wsl_storage_missing = False
                    self.runtime.last_error = ''
                    self._setup_environment()
                    self._persist_machine_selection()
                    logger.info('[RuntimeManager] Recovered storage selection: %s (%s)',
                                self.wsl_distro_name, self.machine_selection_reason)
                result = repair_registration(self.wsl_distro_name, self.podman_data_dir, self.podman_config_dir,
                                             allow_orphan_replacement=getattr(self, '_runtime_provisioning_pending', False))
            if result:
                self._wsl_default_repair_pending = result['was_default']
                self._wsl_distro_cache = {'value': None, 'time': 0.0}
                logger.warning('[RuntimeManager] Recovered malformed WSL registration (%s); metadata backup: %s',
                               result['action'], result['archive'])
            return True
        except Exception as exc:
            self._wsl_storage_missing = True
            self.runtime.last_error = 'WSL registration recovery needs attention: ' + str(exc)
            self._set_connection_state('attention')
            return False

    def _is_wsl_distro_functional(self, force=False, timeout=45):
        requested_at = time.monotonic()
        with self._service_lock:
            return self._check_wsl_distro_functional(force, timeout, requested_at)

    def _check_wsl_distro_functional(self, force, timeout, requested_at):
        now = time.monotonic()
        cached = self._wsl_distro_cache
        # A concurrent caller also reuses the probe completed while it waited,
        # even if it requested an explicit refresh.
        if (not force or cached.get('time', 0) >= requested_at) and cached.get('value') is not None:
            ttl = 30.0 if cached.get('value') else 2.0
            if now - cached.get('time', 0.0) < ttl:
                return bool(cached.get('value'))
        if not self._is_wsl_distro_registered():
            self._wsl_distro_error = 'WSL distribution is not registered: ' + self.wsl_distro_name
            self._wsl_distro_cache = {'value': False, 'time': now}
            return False
        deadline = time.monotonic() + timeout
        check = None
        slow_announced = False
        timed_out = False
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                check = self._run_host_command(
                    ['wsl', '-d', self.wsl_distro_name, '--', 'echo', 'DISTRO_OK'],
                    timeout=min(6, max(.01, remaining)), raise_timeout=True
                )
                timed_out = False
                break  # A concrete WSL error needs attention, not more waiting.
            except subprocess.TimeoutExpired:
                timed_out = True
                if not slow_announced and time.monotonic() < deadline:
                    logger.info('[RuntimeManager] WSL startup is slow; continuing checks for %s (up to %ss).',
                                self.wsl_distro_name, timeout)
                    slow_announced = True
        if check and check.returncode == 0 and 'DISTRO_OK' in (check.stdout or ''):
            self._wsl_storage_missing = False
            self._wsl_distro_error = ''
            self._wsl_distro_cache = {'value': True, 'time': time.monotonic()}
            return True
        detail = ((check.stderr or check.stdout or '').strip()
                  if check is not None else ('WSL startup did not respond within %s seconds' % timeout
                                             if timed_out else 'WSL startup check could not execute'))
        self._wsl_storage_missing = ('ERROR_PATH_NOT_FOUND' in detail and
                                     ('MountDisk' in detail or '.vhdx' in detail.lower()))
        self._wsl_distro_error = 'WSL startup failed for {}: {}'.format(
            self.wsl_distro_name, detail or 'WSL did not return the readiness marker')
        if self._wsl_storage_missing:
            self._wsl_distro_error += (
                '\n旧环境的虚拟磁盘或存储路径不存在。请先从旧安装目录或备份恢复数据，'
                '保留相邻的 .primigenius-preserved 目录；不要重置环境或注销 WSL。'
                '恢复原路径后点击“启动运行环境”或重启软件。'
                '\nThe registered WSL disk or storage path is missing. Restore the original '
                'data from the old installation or backup; keep any adjacent '
                '.primigenius-preserved directory. Do not reset or unregister WSL. '
                'After restoring the original path, start the runtime again or restart the app.'
            )
        self._wsl_distro_cache = {'value': False, 'time': time.monotonic()}
        return False

    def _reset_wsl_runtime_state(self):
        """Discard only transient connection state; never touch WSL storage."""
        self._wsl_distro_cache = {'value': None, 'time': 0.0}
        self._wsl_ip_cache = {'value': None, 'time': 0.0}
        self._engine_ready_cache = {'value': False, 'time': 0.0}
        self._wsl_service_started = False
        self._api_service_proc = None
        if getattr(self, 'runtime', None) is not None:
            self.runtime.stop_bridge()
        self._set_connection_state('recovering')
        self._service_failures = 0
        self._service_retry_after = 0.0
        self._close_api_client()
        os.environ.pop('DOCKER_HOST', None)

    def _wait_for_wsl_distro(self, attempts=5, interval=1.0):
        for attempt in range(max(1, attempts)):
            if self._is_wsl_distro_functional(force=True, timeout=6):
                return True
            if attempt + 1 < attempts:
                time.sleep(interval)
        return False

    def _restart_wsl_control_service_elevated(self):
        """Restart the modern or legacy WSL host service through one UAC prompt."""
        if platform.system() != 'Windows':
            return False
        powershell = shutil.which('powershell.exe') or os.path.join(
            os.environ.get('SystemRoot', r'C:\Windows'),
            'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'
        )
        # The script is fixed and contains no user-controlled values.  Newer
        # Windows builds use WslService; older inbox WSL uses LxssManager.
        script = (
            "$ErrorActionPreference='Stop';"
            "$svc=Get-Service -Name 'WslService' -ErrorAction SilentlyContinue;"
            "if($null -eq $svc){$svc=Get-Service -Name 'LxssManager' -ErrorAction SilentlyContinue};"
            "if($null -eq $svc){exit 3};"
            "if($svc.Status -eq 'Stopped'){Start-Service -InputObject $svc -ErrorAction Stop}"
            "else{Restart-Service -InputObject $svc -Force -ErrorAction Stop};"
            "$svc.WaitForStatus('Running',[TimeSpan]::FromSeconds(20));"
            "if($svc.Status -ne 'Running'){exit 4};exit 0"
        )
        exit_code = _run_elevated_windows_command(
            powershell,
            ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
            timeout=45,
        )
        if exit_code != 0:
            logger.warning(
                f'[RuntimeManager] WSL service recovery was cancelled or failed (exit={exit_code})'
            )
            return False
        logger.info('[RuntimeManager] [RECOVERY] WSL control service restarted successfully')
        return True

    def _confirm_host_wide_wsl_recovery(self):
        """Ask before closing WSL sessions that may belong to other applications."""
        override = str(os.environ.get('PRIMIGENIUS_WSL_RECOVERY_CONSENT') or '').strip().lower()
        if override in ('1', 'true', 'yes', 'allow'):
            return True
        if override in ('0', 'false', 'no', 'deny'):
            return False
        if platform.system() != 'Windows':
            return False
        try:
            import ctypes
            message = (
                'PrimiGenius could not restart its container environment normally.\n\n'
                'The next repair step will close all running WSL and Docker sessions on this computer. '
                'It will not delete WSL distributions, images, containers, or files.\n\n'
                'Continue with WSL repair?'
            )
            flags = 0x00000004 | 0x00000030 | 0x00010000 | 0x00040000
            # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
            return ctypes.windll.user32.MessageBoxW(None, message, 'PrimiGenius WSL Recovery', flags) == 6
        except Exception as e:
            logger.warning(f'[RuntimeManager] Could not display WSL recovery consent: {e}')
            return False

    def _record_wsl_recovery_failure(self, reason):
        self._set_connection_state('recovery_failed')
        self._wsl_recovery_retry_after = time.monotonic() + 300.0
        self._last_wsl_recovery_error = (
            f'Existing Runtime WSL distro {self.wsl_distro_name} is not responding. '
            f'{reason} Its data was preserved; close other WSL applications and retry in five minutes.'
        )
        return False, self._last_wsl_recovery_error

    def _recover_unresponsive_wsl(self):
        """Recover a registered WSL distro without deleting or recreating it."""
        now = time.monotonic()
        retry_after = getattr(self, '_wsl_recovery_retry_after', 0.0)
        if now < retry_after:
            remaining = max(1, int(retry_after - now))
            previous = getattr(self, '_last_wsl_recovery_error', '') or (
                f'Automatic WSL recovery is cooling down for {remaining} seconds.'
            )
            return False, previous
        with self._wsl_recovery_lock:
            now = time.monotonic()
            retry_after = getattr(self, '_wsl_recovery_retry_after', 0.0)
            if now < retry_after:
                return False, getattr(self, '_last_wsl_recovery_error', '') or 'Automatic WSL recovery is cooling down.'
            logger.warning(
                f'[RuntimeManager] [RECOVERY] Starting non-destructive WSL recovery for {self.wsl_distro_name}'
            )

            # Level 1: affect only the PrimiGenius-owned distro.
            self._run_host_command(['wsl', '--terminate', self.wsl_distro_name], timeout=12)
            self._reset_wsl_runtime_state()
            if self._wait_for_wsl_distro(attempts=3, interval=1.0):
                logger.info('[RuntimeManager] [RECOVERY] WSL recovered after distro termination')
                self._wsl_recovery_retry_after = 0.0
                self._last_wsl_recovery_error = ''
                return True, 'WSL recovered after restarting the PrimiGenius environment'

            # Level 2: restart the WSL VM layer.  This is still data-preserving,
            # though it closes other active WSL sessions on the host.
            if not self._confirm_host_wide_wsl_recovery():
                logger.warning('[RuntimeManager] [RECOVERY] Host-wide WSL recovery was declined')
                return self._record_wsl_recovery_failure('Host-wide automatic recovery was cancelled.')
            logger.warning('[RuntimeManager] [RECOVERY] Distro restart failed; shutting down the WSL VM layer')
            shutdown_result = self._run_host_command(['wsl', '--shutdown'], timeout=20)
            if shutdown_result is None or shutdown_result.returncode != 0:
                return self._record_wsl_recovery_failure(
                    'WSL shutdown did not respond; restart Windows before trying again.'
                )
            self._reset_wsl_runtime_state()
            if self._wait_for_wsl_distro(attempts=5, interval=1.0):
                logger.info('[RuntimeManager] [RECOVERY] WSL recovered after VM shutdown')
                self._wsl_recovery_retry_after = 0.0
                self._last_wsl_recovery_error = ''
                return True, 'WSL recovered after restarting its virtual machine layer'

            # Level 3: a stuck WSL control service requires elevation.  UAC is
            # the user-visible consent boundary for this host-wide operation.
            logger.warning('[RuntimeManager] [RECOVERY] WSL VM remains unavailable; requesting service recovery via UAC')
            if self._restart_wsl_control_service_elevated():
                time.sleep(2)
                self._reset_wsl_runtime_state()
                if self._wait_for_wsl_distro(attempts=8, interval=1.0):
                    logger.info('[RuntimeManager] [RECOVERY] WSL recovered after control-service restart')
                    self._wsl_recovery_retry_after = 0.0
                    self._last_wsl_recovery_error = ''
                    return True, 'WSL recovered after restarting its Windows service'

            return self._record_wsl_recovery_failure(
                'Automatic recovery was cancelled or Windows could not restart the WSL service.'
            )






    def _close_api_client(self):
        with self._api_client_lock:
            client = self._api_client
            self._api_client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _set_connection_state(self, state):
        previous = self._connection_state
        self._connection_state = state
        listener = getattr(self, 'on_state_change', None)
        if previous != state and listener:
            try:
                listener(self.runtime.status())
            except Exception:
                logger.exception('[Runtime] Could not publish connection state')

    def _mark_service_ready(self):
        self._wsl_service_started = True
        self._wsl_storage_missing = False
        self._service_failures = 0
        self._service_retry_after = 0.0
        os.environ['DOCKER_HOST'] = self._tcp_docker_host()
        self._engine_ready_cache = {'value': True, 'time': time.monotonic()}
        self._set_connection_state('ready')

    def _mark_service_failed(self):
        self._wsl_service_started = False
        self._service_failures = min(self._service_failures + 1, 8)
        delay = min(60.0, 3.0 * (2 ** (self._service_failures - 1)))
        self._service_retry_after = time.monotonic() + delay
        self._engine_ready_cache = {'value': False, 'time': time.monotonic()}
        self._close_api_client()
        self._set_connection_state('backoff')

    def _monitor_runtime(self):
        while True:
            time.sleep(5)
            with self._service_lock:
                if not self._runtime_desired:
                    self._runtime_monitor_started = False
                    return
                if not self.is_engine_ready():
                    self._start_wsl_podman_service_locked()

    def _start_wsl_podman_service(self, force=False):
        with self._service_lock:
            self._runtime_desired = True
            if not self._runtime_monitor_started:
                self._runtime_monitor_started = True
                threading.Thread(target=self._monitor_runtime, name='runtime-recovery', daemon=True).start()
            return self._start_wsl_podman_service_locked(force=force)

    def _start_wsl_podman_service_locked(self, force=False):
        # Automatic API reconnect must not bypass failed storage discovery.
        # Explicit runtime startup retries discovery through auto_setup().
        if (getattr(self, '_runtime_selection_error', '') or
                getattr(self, '_wsl_storage_missing', False)):
            self._set_connection_state('attention')
            return False
        if getattr(self, '_runtime_provisioning_pending', False):
            self._set_connection_state('starting')
            return False  # auto_setup owns the import before IPC may start.
        if not force and time.monotonic() < self._service_retry_after:
            return False
        if self.is_engine_ready():
            self._mark_service_ready()
            return True
        recovering = self._service_failures or self._connection_state in ('ready', 'degraded', 'backoff', 'recovering')
        self._set_connection_state('recovering' if recovering else 'starting')
        try:
            if self.runtime.start():
                self._mark_service_ready()
                return True
        except Exception as exc:
            self.runtime.last_error = str(exc)
            logger.error('[Runtime] %s', exc)
        self._mark_service_failed()
        return False

    def _test_tcp_connection(self, timeout=RUNTIME_PROBE_TIMEOUT):
        # Compatibility method name: no IP inspection or TCP connection.
        return self.runtime.ping(timeout=timeout)

    def _stop_wsl_podman_service(self):
        with self._service_lock:
            self._runtime_desired = False
            self._close_api_client()
            self.runtime.stop()
            self._api_service_proc = None
            self._wsl_service_started = False
            self._engine_ready_cache = {'value': False, 'time': time.monotonic()}
            self._set_connection_state('stopped')

    def is_wsl_enabled(self):
        if platform.system() != 'Windows':
            return True
        try:
            result = self._run_host_command(['wsl', '--list', '--quiet'], timeout=10)
            return bool(result and result.returncode == 0)
        except Exception:
            return False

    def is_wsl2_feature_enabled(self):
        if platform.system() != 'Windows':
            return True
        try:
            # Prefer non-elevated WSL introspection. DISM often fails with 740
            # under normal user sessions even when WSL2 is fully available.
            if self._wsl_service_started or self._is_wsl_distro_registered():
                return True
            list_result = self._run_host_command(['wsl', '--list', '--verbose'], timeout=12)
            if list_result and list_result.returncode == 0:
                out = (list_result.stdout or '').replace('\r', '')
                if re.search(rf'{re.escape(WSL_DISTRO_NAME)}.*\b2\b', out):
                    return True
                if re.search(r'^\s*[* ]\s+\S+.*\b2\b\s*$', out, re.MULTILINE):
                    return True
            status_result = self._run_host_command(['wsl', '--status'], timeout=12)
            if status_result and status_result.returncode == 0:
                out = (status_result.stdout or '').replace('\r', '')
                if re.search(r'[:：]\s*2\b', out):
                    return True
                if 'default version' in out.lower():
                    return True
            result = self._run_host_command(
                ['dism.exe', '/online', '/get-featureinfo', '/featurename:VirtualMachinePlatform'],
                timeout=15
            )
            if result and result.returncode == 0 and 'State : Enabled' in (result.stdout or ''):
                return True
            return False
        except Exception:
            return False

    def get_wsl_status_details(self):
        info = {
            'wsl_available': False,
            'wsl2_enabled': False,
            'default_version': None,
            'distro_name': WSL_DISTRO_NAME,
            'distro_registered': self._is_wsl_distro_registered(),
            'distro_version': None,
            'distro_list_text': '',
            'status_text': ''
        }
        if platform.system() != 'Windows':
            info['wsl_available'] = True
            info['wsl2_enabled'] = True
            info['default_version'] = 2
            return info

        list_result = self._run_host_command(['wsl', '--list', '--verbose'], timeout=12)
        if list_result and list_result.returncode == 0:
            info['wsl_available'] = True
            info['distro_list_text'] = (list_result.stdout or '').strip()
            for line in info['distro_list_text'].splitlines():
                cleaned = line.replace('\x00', '').strip()
                if not cleaned or 'VERSION' in cleaned.upper():
                    continue
                m = re.search(r'(\d+)\s*$', cleaned)
                if m and WSL_DISTRO_NAME in cleaned:
                    info['distro_version'] = int(m.group(1))
                    break

        status_result = self._run_host_command(['wsl', '--status'], timeout=12)
        if status_result and status_result.returncode == 0:
            info['wsl_available'] = True
            info['status_text'] = (status_result.stdout or '').strip()
            m = re.search(r'[:：]\s*(\d+)\s*$', info['status_text'], re.MULTILINE)
            if m:
                info['default_version'] = int(m.group(1))

        info['wsl2_enabled'] = bool(
            self._wsl_service_started
            or info['distro_version'] == 2
            or info['default_version'] == 2
            or self.is_wsl2_feature_enabled()
        )
        return info

    def enable_wsl2(self):
        if platform.system() != 'Windows':
            return True, 'Not Windows, skipping'
        try:
            logger.info('[RuntimeManager] [SETUP] Enabling WSL2 features...')
            r1 = subprocess.run(
                ['dism.exe', '/online', '/enable-feature',
                 '/featurename:Microsoft-Windows-Subsystem-Linux',
                 '/all', '/norestart'],
                capture_output=True, text=True, timeout=120, **_SUBPROCESS_KWARGS
            )
            r2 = subprocess.run(
                ['dism.exe', '/online', '/enable-feature',
                 '/featurename:VirtualMachinePlatform',
                 '/all', '/norestart'],
                capture_output=True, text=True, timeout=120, **_SUBPROCESS_KWARGS
            )
            if r1.returncode == 0 and r2.returncode == 0:
                logger.info('[RuntimeManager] [OK] WSL2 features enabled')
                self._run_host_command(['wsl', '--set-default-version', '2'], timeout=15)
                return True, 'WSL2 features enabled. A system restart may be required.'
            elif r1.returncode == 3010 or r2.returncode == 3010:
                logger.info('[RuntimeManager] [OK] WSL2 features enabled (reboot required)')
                self._run_host_command(['wsl', '--set-default-version', '2'], timeout=15)
                return True, 'WSL2 features enabled. A system restart is required (code 3010).'
            else:
                err = (r1.stderr or '') + (r2.stderr or '')
                logger.warning(f'[RuntimeManager] dism enable failed: {err}')
                logger.info('[RuntimeManager] [SETUP] Trying wsl --install fallback...')
                install_result = self._run_host_command(
                    ['wsl', '--install', '--no-distribution'], timeout=180
                )
                if install_result and install_result.returncode == 0:
                    self._run_host_command(['wsl', '--set-default-version', '2'], timeout=15)
                    logger.info('[RuntimeManager] [OK] WSL2 installed via wsl --install fallback')
                    return True, 'WSL2 installed via wsl --install fallback.'
                return False, f'WSL2 feature enable failed (needs admin?): {err}'
        except Exception as e:
            logger.error(f'[RuntimeManager] enable_wsl2 exception: {e}')
            return False, str(e)

    def is_podman_available(self):
        return self.runtime.available()

    def _ensure_wsl2_ready(self):
        if platform.system() != 'Windows':
            return True, 'Not Windows'
        logger.info('[RuntimeManager] [PHASE 1] Checking WSL2 readiness...')
        try:
            test_result = self._run_host_command(
                ['wsl', '--list', '--verbose'], timeout=10
            )
            if test_result and test_result.returncode == 0:
                out = (test_result.stdout or '').replace('\x00', '')
                if re.search(r'\b2\b', out):
                    logger.info('[RuntimeManager] [PHASE 1] WSL2 is functional (distro with v2 found)')
                    return True, 'WSL2 ready'
        except Exception:
            pass
        logger.info('[RuntimeManager] [PHASE 1] WSL2 not functional, updating WSL kernel...')
        update_result = self._run_host_command(['wsl', '--update'], timeout=180)
        if update_result:
            out = (update_result.stdout or '').strip()
            logger.info(f'[RuntimeManager] [PHASE 1] WSL update: {out}')
            if update_result.returncode == 0:
                logger.info('[RuntimeManager] [PHASE 1] WSL kernel updated OK')
                return True, 'WSL2 updated'
        logger.warning('[RuntimeManager] [PHASE 1] WSL update failed or returned error')
        try:
            status_result = self._run_host_command(['wsl', '--status'], timeout=10)
            if status_result:
                logger.info(f'[RuntimeManager] [PHASE 1] WSL status: {(status_result.stdout or "").strip()}')
        except Exception:
            pass
        if not self.is_wsl2_feature_enabled():
            logger.info('[RuntimeManager] [PHASE 1] WSL2 feature not enabled, enabling...')
            ok, msg = self.enable_wsl2()
            if ok:
                return True, 'WSL2 enabled'
            return False, f'WSL2 not ready: {msg}'
        return True, 'WSL2 feature enabled but kernel may need reboot'

    def get_podman_version(self):
        try:
            return self.runtime.engine_info.get('version') or self.runtime.manifest()['podman_version']
        except (OSError, ValueError, KeyError):
            return None

    def machine_exists(self):
        return self._is_wsl_distro_registered()

    def machine_status(self):
        exists = self._is_wsl_distro_registered()
        ready = self.is_engine_ready() if exists else False
        return {'status': 'success', 'machines': [{'Name': self.machine_name, 'Running': ready}] if exists else [],
                'running': ready, 'count': int(exists), 'source': 'embedded-runtime', 'runtime': self.runtime.status()}

    def machine_is_running(self):
        return self.is_engine_ready()



    def machine_init(self, cpus=2, memory=4096, disk_size=60, name='', skip_wsl_check=False):
        if name and name != self.machine_name:
            return False, 'Only the owned environment can be initialized'
        if not self._prepare_wsl_registration():
            return False, self.runtime.last_error
        if self._is_wsl_distro_registered():
            if self._is_wsl_distro_functional(force=True):
                self._runtime_provisioning_pending = False
                self._persist_machine_selection()
                return True, 'Existing environment preserved'
            self.runtime.last_error = self._wsl_distro_error
            self._set_connection_state('attention')
            return False, self.runtime.last_error
        try:
            manifest = self.runtime.validate()
            archive = os.path.join(self.runtime.bundle, 'rootfs.tar.gz')
            expected = manifest.get('files', {}).get('rootfs.tar.gz')
            if not expected or not os.path.isfile(archive):
                return False, 'The verified initial runtime image is missing from this build'
            import hashlib
            with open(archive, 'rb') as f:
                if hashlib.file_digest(f, 'sha256').hexdigest() != expected:
                    return False, 'Runtime rootfs checksum mismatch'
            if not skip_wsl_check:
                ok, message = self._ensure_wsl2_ready()
                if not ok:
                    return False, message
            target = os.path.join(self.podman_data_dir, 'machine', 'wsldist')
            if os.path.isdir(target) and os.listdir(target):
                return False, 'Existing unregistered runtime data was preserved; choose recovery'
            os.makedirs(target, exist_ok=True)
            result = self._run_host_command(['wsl.exe', '--import', self.wsl_distro_name, target, archive, '--version', '2'], timeout=600)
            self._wsl_distro_cache = {'value': None, 'time': 0.0}
            if result is None or result.returncode:
                return False, (getattr(result, 'stderr', '') or 'WSL import failed; partial data was preserved')
            if getattr(self, '_wsl_default_repair_pending', False):
                default = self._run_host_command(['wsl', '--set-default', self.wsl_distro_name], timeout=15)
                if default is None or default.returncode:
                    return False, 'The environment was imported but its previous default selection could not be restored'
                self._wsl_default_repair_pending = False
            self._runtime_provisioning_pending = False
            self._persist_machine_selection()
            with open(self.machine_runtime_config, encoding='utf-8') as stream:
                settings = json.load(stream)
            settings['requested_resources'] = {'cpus': cpus, 'memory': memory, 'disk_size': disk_size}
            settings['resource_scope'] = 'WSL CPU and memory limits are VM-wide; no global configuration was modified'
            with open(self.machine_runtime_config, 'w', encoding='utf-8') as stream:
                json.dump(settings, stream, ensure_ascii=False, indent=2)
            return True, 'Embedded runtime initialized; requested resources saved. WSL global CPU/memory settings were preserved.'
        except Exception as exc:
            return False, str(exc)

    def machine_start(self, name=''):
        if name and name != self.machine_name:
            return False, 'The environment is not owned by this installation'
        return self.auto_setup()

    def machine_stop(self, name=''):
        if name and name != self.machine_name:
            return False, 'The environment is not owned by this installation'
        self._stop_wsl_podman_service()
        logger.info('[RuntimeManager] Runtime service stopped.')
        return True, 'Runtime service stopped'

    def machine_remove(self, name=''):
        if name and name != self.machine_name:
            return False, 'The environment is not owned by this installation'
        self._stop_wsl_podman_service()
        result = self._run_host_command(['wsl.exe', '--unregister', self.wsl_distro_name], timeout=60)
        self._wsl_distro_cache = {'value': None, 'time': 0.0}
        if result is None or result.returncode:
            return False, getattr(result, 'stderr', '') or 'Unable to remove the owned environment'
        return True, 'Owned environment removed'

    def machine_reset(self, cpus=4, memory=8192, disk_size=100, name=''):
        removed, message = self.machine_remove(name)
        if not removed:
            return False, message
        ok, msg = self.machine_init(cpus, memory, disk_size, name)
        if not ok:
            return False, f'Init after remove failed: {msg}'
        ok2, msg2 = self.machine_start(name)
        if not ok2:
            return False, f'Start after init failed: {msg2}'
        return True, 'Runtime reset successfully'

    def get_socket_path(self):
        return self.runtime.pipe

    def get_docker_host_env(self):
        return self.runtime.url

    def is_engine_ready(self):
        # Status callers must not queue behind a slow start/recovery or each
        # other. The lifecycle owner publishes the result when it completes.
        if not self._service_lock.acquire(blocking=False):
            return bool(self._engine_ready_cache.get('value'))
        try:
            return self._probe_engine_ready()
        finally:
            self._service_lock.release()

    def _probe_engine_ready(self):
        now = time.monotonic()
        if self._connection_state == 'backoff' and now < self._service_retry_after:
            return False
        cached = self._engine_ready_cache
        if now - cached.get('time', 0.0) < 1.0:
            if cached.get('value'):
                self._wsl_service_started = True
                self._set_connection_state('ready')
                os.environ['DOCKER_HOST'] = self._tcp_docker_host()
            return bool(cached.get('value'))

        if self._test_tcp_connection():
            if not self._wsl_service_started:
                self._wsl_service_started = True
                os.environ['DOCKER_HOST'] = self._tcp_docker_host()
            self._engine_ready_cache = {'value': True, 'time': time.monotonic()}
            self._set_connection_state('ready')
            return True
        if self._wsl_service_started:
            self._wsl_service_started = False
        self._engine_ready_cache = {'value': False, 'time': time.monotonic()}
        if self._connection_state == 'ready':
            self._set_connection_state('degraded')
        return False

    def wait_for_engine(self, max_attempts=30, interval=1):
        for i in range(max_attempts):
            if self.is_engine_ready():
                return True
            time.sleep(interval)
        return False

    def auto_setup(self):
        with self._setup_lock:
            if self._setup_running:
                logger.info('[RuntimeManager] Auto setup already running in another thread, waiting...')
                return True, 'Setup already in progress'
            self._setup_running = True
        try:
            return self._auto_setup_impl()
        finally:
            with self._setup_lock:
                self._setup_running = False

    def _auto_setup_impl(self):
        if not self.is_podman_available():
            self._set_connection_state('attention')
            return False, self.runtime.last_error
        if self.is_engine_ready():
            return True, 'Embedded runtime ready'
        self._set_connection_state('recovering' if self._connection_state in ('ready', 'degraded', 'backoff', 'recovering') else 'starting')
        if not self._prepare_wsl_registration():
            return False, self.runtime.last_error
        if self._is_wsl_distro_registered():
            if not self._is_wsl_distro_functional():
                self.runtime.last_error = (
                    getattr(self, '_wsl_distro_error', '')
                    or 'Existing WSL environment is unavailable.'
                ) + ' No distribution was reset or recreated during this startup attempt.'
                self._set_connection_state('attention' if getattr(self, '_wsl_storage_missing', False) else 'recovery_failed')
                return False, self.runtime.last_error
        else:
            ok, message = self.machine_init()
            if not ok:
                return False, message
        ok = self._start_wsl_podman_service()
        return ok, 'Embedded runtime ready' if ok else self.runtime.last_error

    def get_system_info(self):
        result = self._run_podman(['info', '--format', 'json'], timeout=15)
        if result is None or result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except Exception:
            return None

    def configure_mirror(self, mirrors):
        config_dir = os.path.join(self.podman_config_dir, 'containers')
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, 'containers.conf')

        mirrors = [m.removeprefix('https://').removeprefix('http://') for m in mirrors]

        new_config_parts = []
        new_config_parts.append('[containers]')
        new_config_parts.append('')
        new_config_parts.append('[engine]')
        if mirrors:
            new_config_parts.append('registry-mirrors = [')
            for i, m in enumerate(mirrors):
                comma = ',' if i < len(mirrors) - 1 else ''
                new_config_parts.append(f'  "{m}"{comma}')
            new_config_parts.append(']')
        new_config_parts.append('')
        new_config_parts.append('[machine]')
        new_config_parts.append('')

        with open(config_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(new_config_parts))

        if self._wsl_service_started:
            self._write_wsl_registries_conf()

            logger.info('[RuntimeManager] Mirror config also written inside WSL')

        logger.info(f'[RuntimeManager] Mirror configuration written to {config_path}')
        return True

    def read_config(self):
        config_path = os.path.join(self.podman_config_dir, 'containers', 'containers.conf')
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                return f.read(), config_path
        return DEFAULT_CONTAINERS_CONF, config_path

    def write_config(self, config_str):
        config_dir = os.path.join(self.podman_config_dir, 'containers')
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, 'containers.conf')
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(config_str)
        logger.info(f'[RuntimeManager] Config written to {config_path}')
        return config_path

    def cleanup_all(self):
        logger.info('[RuntimeManager] Full cleanup starting...')
        self._stop_wsl_podman_service()

        with self._api_client_lock:
            if self._api_client is not None:
                try:
                    self._api_client.close()
                except Exception:
                    pass
                self._api_client = None

        if platform.system() == 'Windows':
            try:
                _run_hidden(['wsl', '--unregister', self.wsl_distro_name],
                            timeout=30, capture=True)
            except Exception:
                pass

        try:
            if os.path.exists(self.podman_data_dir):
                shutil.rmtree(self.podman_data_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f'[RuntimeManager] Data dir cleanup failed: {e}')

        try:
            if os.path.exists(self.podman_config_dir):
                shutil.rmtree(self.podman_config_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f'[RuntimeManager] Config dir cleanup failed: {e}')

        logger.info('[RuntimeManager] Full cleanup complete.')

    def stop_containers_and_machine(self):
        # Callers drain task workers and logs before releasing the engine.
        client = self.get_podman_client()
        if client is not None:
            owned = client.containers.list(filters={'label': 'primigenius.owner=' + self.runtime.identity})
            if owned:
                raise RuntimeError('Active owned containers must be stopped and drained before shutdown')
        self._stop_wsl_podman_service()
        # Do not terminate the distro: other applications may share it.

    def prepare_for_host_suspend(self):
        self._stop_wsl_podman_service()
        return True, 'Local connection released; WSL workloads preserved'

    def start_api_service(self, timeout=0):
        if self._wsl_service_started:
            return True
        return self._start_wsl_podman_service()

    def stop_api_service(self):
        self._stop_wsl_podman_service()

    def get_api_service_url(self):
        return self.runtime.url

    def get_podman_client(self):
        with self._api_client_lock:
            if not self.is_engine_ready():
                return None
            if self._api_client is None:
                from embedded_runtime import OwnedDockerClient
                self._api_client = OwnedDockerClient(owner=self.runtime.identity, base_url=self.runtime.url, version=DOCKER_API_VERSION, timeout=15)
            return self._api_client

    def get_data_usage(self):
        data_dir = self.podman_data_dir
        total_size = 0
        if os.path.exists(data_dir):
            for root, dirs, files in os.walk(data_dir):
                for f in files:
                    try:
                        total_size += os.path.getsize(os.path.join(root, f))
                    except Exception:
                        pass
        if total_size >= 1e9:
            return f'{round(total_size / 1e9, 2)} GB'
        elif total_size >= 1e6:
            return f'{round(total_size / 1e6, 1)} MB'
        else:
            return f'{round(total_size / 1e3, 1)} KB'

    def pull_image(self, image_name, timeout=600):
        logger.info(f'[RuntimeManager] Pulling image: {image_name}')
        if not self.is_engine_ready():
            logger.info('[RuntimeManager] Engine not ready, attempting to start...')
            self.machine_start()
            if not self.is_engine_ready():
                return False, 'Container engine is not running'
        result = self._run_podman(['pull', image_name], timeout=timeout)
        if result is None:
            return False, 'Pull command failed (timeout or not found)'
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or 'Pull failed').strip()
        return True, (result.stdout or 'OK').strip()

    def list_images(self):
        result = self._run_podman(['images', '--format', 'json'], timeout=30)
        if result is None or result.returncode != 0:
            return []
        try:
            return json.loads(result.stdout)
        except Exception:
            return []

    def remove_image(self, image_id, force=False):
        cmd = ['rmi']
        if force:
            cmd.append('-f')
        cmd.append(image_id)
        result = self._run_podman(cmd, timeout=60)
        if result is None:
            return False, 'Remove command failed'
        if result.returncode != 0:
            return False, (result.stderr or 'Remove failed').strip()
        return True, (result.stdout or 'OK').strip()

    def list_containers(self, all=False):
        cmd = ['ps', '--format', 'json']
        if all:
            cmd.append('-a')
        result = self._run_podman(cmd, timeout=30)
        if result is None or result.returncode != 0:
            return []
        try:
            return json.loads(result.stdout)
        except Exception:
            return []

    def run_container(self, image, name=None, command=None, volumes=None, ports=None, detach=True, rm=False):
        cmd = ['run']
        if detach:
            cmd.append('-d')
        if rm:
            cmd.append('--rm')
        if name:
            cmd.extend(['--name', name])
        if volumes:
            for v in volumes:
                cmd.extend(['-v', v])
        if ports:
            for p in ports:
                cmd.extend(['-p', p])
        cmd.append(image)
        if command:
            cmd.extend(command if isinstance(command, list) else [command])
        result = self._run_podman(cmd, timeout=120)
        if result is None:
            return False, 'Run command failed'
        if result.returncode != 0:
            return False, (result.stderr or 'Run failed').strip()
        return True, (result.stdout or 'OK').strip()

    def stop_container(self, container_id):
        result = self._run_podman(['stop', container_id], timeout=30)
        if result is None:
            return False, 'Stop command failed'
        if result.returncode != 0:
            return False, (result.stderr or 'Stop failed').strip()
        return True, (result.stdout or 'OK').strip()

    def remove_container(self, container_id, force=False):
        cmd = ['rm']
        if force:
            cmd.append('-f')
        cmd.append(container_id)
        result = self._run_podman(cmd, timeout=30)
        if result is None:
            return False, 'Remove command failed'
        if result.returncode != 0:
            return False, (result.stderr or 'Remove failed').strip()
        return True, (result.stdout or 'OK').strip()
