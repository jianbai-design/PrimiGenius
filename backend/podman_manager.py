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
            logger.warning(f'[PodmanManager] Elevated recovery was not started (Windows error {error_code})')
            return None
        if not info.hProcess:
            return None
        try:
            wait_result = wait_for_single_object(info.hProcess, max(1, int(timeout * 1000)))
            if wait_result != 0:  # WAIT_OBJECT_0
                logger.warning('[PodmanManager] Elevated recovery did not finish before the timeout')
                return None
            exit_code = wintypes.DWORD()
            if not get_exit_code_process(info.hProcess, ctypes.byref(exit_code)):
                return None
            return int(exit_code.value)
        finally:
            close_handle(info.hProcess)
    except Exception as e:
        logger.warning(f'[PodmanManager] Failed to launch elevated recovery: {e}')
        return None

_WINDOWS_NO_WINDOW = 0x08000000 | 0x00000200
_SUBPROCESS_KWARGS = {}
if platform.system() == 'Windows':
    _SUBPROCESS_KWARGS['creationflags'] = _WINDOWS_NO_WINDOW
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    _SUBPROCESS_KWARGS['startupinfo'] = si

def _run_hidden(cmd, timeout=60, capture=True, env=None):
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
    except FileNotFoundError:
        exe_name = cmd[0] if cmd else 'unknown'
        logger.error(f'[PodmanManager] Command not found: {exe_name}. Ensure the executable exists and is in PATH.')
        return None
    except PermissionError:
        exe_name = cmd[0] if cmd else 'unknown'
        logger.error(f'[PodmanManager] Permission denied running: {exe_name}. Try running as administrator.')
        return None
    except Exception as e:
        logger.error(f'[PodmanManager] _run_hidden failed: {e}, cmd={cmd}')
        import traceback
        logger.error(f'[PodmanManager] _run_hidden traceback: {traceback.format_exc()}')
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
        logger.error(f'[PodmanManager] Failed to start streaming command: {e}')
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
                    logger.error(f'[PodmanManager] Command timed out after {timeout}s')
                    return None
                continue
            if line is None:
                if proc.poll() is not None:
                    break
                continue
            decoded = _decode_command_output(line).strip()
            if decoded:
                output_lines.append(decoded)
                logger.info(f'[PodmanManager] [INIT] {decoded}')
            if timeout and (time.time() - start_time) > timeout:
                proc.kill()
                logger.error(f'[PodmanManager] Command timed out after {timeout}s')
                return None
        proc.wait(timeout=30)
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        logger.error(f'[PodmanManager] Streaming command failed: {e}')
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
    if channel == 'dev':
        return DEV_WSL_DISTRO_NAME
    if persisted_name in (LEGACY_WSL_DISTRO_NAME, STABLE_WSL_DISTRO_NAME):
        return persisted_name
    if legacy_owned:
        return LEGACY_WSL_DISTRO_NAME
    return STABLE_WSL_DISTRO_NAME


def machine_to_wsl_distro_name(machine_name):
    """Map Podman CLI machine names to the WSL distro names Podman registers."""
    if machine_name == LEGACY_WSL_DISTRO_NAME:
        return LEGACY_WSL_DISTRO_NAME
    return f'podman-{machine_name}'


RUNTIME_CHANNEL = _runtime_channel()
_INITIAL_MACHINE_NAME = DEV_WSL_DISTRO_NAME if RUNTIME_CHANNEL == 'dev' else STABLE_WSL_DISTRO_NAME
WSL_DISTRO_NAME = machine_to_wsl_distro_name(_INITIAL_MACHINE_NAME)
PODMAN_API_HOST = '127.0.0.1'
PODMAN_API_PORT = 8889 if RUNTIME_CHANNEL == 'dev' else 8888
PODMAN_TCP_URL = f'tcp://{PODMAN_API_HOST}:{PODMAN_API_PORT}'
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

        logger.info(f'[PodmanManager] app_install_dir={app_install_dir}')
        logger.info(f'[PodmanManager] podman_dir={self.podman_dir}')
        logger.info(f'[PodmanManager] sys.executable={sys.executable}')
        logger.info(f'[PodmanManager] sys.frozen={getattr(sys, "frozen", False)}')
        logger.info(f'[PodmanManager] runtime_channel={self.runtime_channel}')
        logger.info(f'[PodmanManager] machine_name={self.machine_name} ({self.machine_selection_reason})')
        logger.info(f'[PodmanManager] wsl_distro_name={self.wsl_distro_name}')
        logger.info(f'[PodmanManager] api_port={PODMAN_API_PORT}')

        self.podman_exe = self._find_podman_exe()

        self._api_service_proc = None
        self._api_client = None
        self._api_client_lock = threading.RLock()
        self._wsl_service_started = False
        self._engine_ready_cache = {'value': False, 'time': 0.0}
        self._wsl_distro_cache = {'value': None, 'time': 0.0}
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
        # kill competing Podman service processes.
        self._service_lock = threading.RLock()
        self._connection_state = 'stopped'
        self._service_failures = 0
        self._service_retry_after = 0.0

        self._setup_environment()

        os.makedirs(self.podman_data_dir, exist_ok=True)
        os.makedirs(self.podman_config_dir, exist_ok=True)
        self._persist_machine_selection()

        self._machine_lock = threading.Lock()

        logger.info(f'[PodmanManager] Initialized. podman_exe={self.podman_exe}')
        logger.info(f'[PodmanManager] Data dir: {self.podman_data_dir}')
        logger.info(f'[PodmanManager] Config dir: {self.podman_config_dir}')

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
            if name in (LEGACY_WSL_DISTRO_NAME, STABLE_WSL_DISTRO_NAME, DEV_WSL_DISTRO_NAME):
                return name
        except Exception:
            pass
        return None

    def _load_persisted_data_dir(self, default_path):
        """Restore a user-selected Podman storage root across app restarts."""
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

    def _legacy_machine_owned_by_install(self):
        """Only adopt the legacy global name when this install owns its storage."""
        legacy_markers = [
            os.path.join(self.podman_data_dir, 'machine', 'wsldist', LEGACY_WSL_DISTRO_NAME, 'ext4.vhdx'),
            os.path.join(self.podman_data_dir, 'containers', 'podman', 'machine', 'wsl', LEGACY_WSL_DISTRO_NAME),
        ]
        if any(os.path.exists(marker) for marker in legacy_markers):
            return True
        if platform.system() != 'Windows':
            return False
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Lxss')
            index = 0
            while True:
                try:
                    child_name = winreg.EnumKey(key, index)
                    child = winreg.OpenKey(key, child_name)
                    try:
                        distro_name, _ = winreg.QueryValueEx(child, 'DistributionName')
                        if distro_name == LEGACY_WSL_DISTRO_NAME:
                            base_path, _ = winreg.QueryValueEx(child, 'BasePath')
                            if self._path_is_within(base_path, self.podman_data_dir):
                                return True
                    finally:
                        winreg.CloseKey(child)
                    index += 1
                except OSError:
                    break
            winreg.CloseKey(key)
        except Exception:
            pass
        return False

    def _select_machine_name(self):
        if self.runtime_channel == 'dev':
            return DEV_WSL_DISTRO_NAME, 'development channel'
        persisted = self._load_persisted_machine_name()
        if persisted:
            return choose_machine_name('stable', persisted_name=persisted), 'persisted stable selection'
        legacy_owned = self._legacy_machine_owned_by_install()
        selected = choose_machine_name('stable', legacy_owned=legacy_owned)
        reason = 'legacy storage owned by this install' if legacy_owned else 'new stable install'
        return selected, reason

    def _persist_machine_selection(self):
        payload = {
            'schema_version': 1,
            'channel': self.runtime_channel,
            'machine_name': self.machine_name,
            'wsl_distro_name': self.wsl_distro_name,
            'selection_reason': self.machine_selection_reason,
            'api_port': PODMAN_API_PORT,
            'data_dir': self.podman_data_dir,
        }
        try:
            os.makedirs(self.podman_config_dir, exist_ok=True)
            tmp_path = self.machine_runtime_config + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8', newline='\n') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write('\n')
            os.replace(tmp_path, self.machine_runtime_config)
        except Exception as e:
            logger.warning(f'[PodmanManager] Failed to persist machine selection: {e}')

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
        """Replace storage-root references in Podman JSON config files; return backups."""
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
                        raise RuntimeError(f'Failed to update Podman config {path_value}: {e}')
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
        """Relocate the complete Podman data root and roll back if the moved machine cannot start."""
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
                raise RuntimeError(f'Failed to copy Podman data (robocopy code {getattr(copy_result, "returncode", "unknown")}): {detail}')

            config_backups = self._update_machine_config_paths(source, target)
            registry_backup = self._replace_wsl_base_path(source, target)
            self._set_data_dir(target)
            switched = True
            self._ensure_wsldist_junction()
            self._wsl_distro_cache = {'value': None, 'time': 0.0}
            ok, message = self.machine_start()
            if not ok:
                raise RuntimeError(message)

            try:
                shutil.rmtree(source)
            except Exception as cleanup_error:
                # The new machine is already verified. Never roll back to an old tree
                # that may have been partially removed; leave the remainder for a later cleanup.
                logger.warning(f'[PodmanManager] Migration succeeded but old data cleanup failed: {cleanup_error}')
            logger.info(f'[PodmanManager] Podman data relocated: {source} -> {target}')
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

    def _find_podman_exe(self):
        candidates = [
            os.path.join(self.podman_dir, 'podman.exe'),
            os.path.join(self.podman_dir, 'podman'),
            os.path.join(self.app_install_dir, 'resources', 'podman', 'podman.exe'),
            os.path.join(self.app_install_dir, 'resources', 'podman', 'podman'),
        ]
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
            candidates.extend([
                os.path.join(exe_dir, '..', 'podman', 'podman.exe'),
                os.path.join(exe_dir, '..', '..', 'resources', 'podman', 'podman.exe'),
                os.path.join(exe_dir, '..', 'podman', 'podman'),
            ])
        if platform.system() == 'Windows':
            program_files = os.environ.get('ProgramFiles', r'C:\Program Files')
            candidates.append(os.path.join(program_files, 'RedHat', 'Podman', 'podman.exe'))
        for c in candidates:
            if os.path.isfile(c):
                abs_c = os.path.abspath(c)
                logger.info(f'[PodmanManager] Found podman at: {abs_c}')
                return abs_c
        which_result = shutil.which('podman')
        if which_result:
            logger.info(f'[PodmanManager] Found podman via PATH: {which_result}')
            return which_result
        logger.warning(f'[PodmanManager] podman.exe not found in any candidate path. Searched: {candidates}')
        return 'podman'

    def _podman_bin_dir(self):
        return os.path.dirname(self.podman_exe) if os.path.isfile(self.podman_exe) else self.podman_dir

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

    def _build_podman_env(self, include_docker_host=True):
        storage_conf_path = self._ensure_base_config_files()
        env = os.environ.copy()
        env.update({
            'XDG_CONFIG_HOME': self.podman_config_dir,
            'XDG_DATA_HOME': self.podman_data_dir,
            'PODMAN_CONFIG_DIR': self.podman_config_dir,
            'CONTAINERS_CONF_DIR': self.podman_config_dir,
            'CONTAINERS_STORAGE_CONF': storage_conf_path,
            'HOME': self.podman_config_dir,
        })

        if platform.system() == 'Windows':
            machine_dir = os.path.join(self.podman_data_dir, 'machine')
            os.makedirs(machine_dir, exist_ok=True)
            env.update({
                'CONTAINERS_MACHINE_PROVIDER_DIR': machine_dir,
                'APPDATA': self.podman_config_dir,
                'USERPROFILE': self.podman_config_dir,
                'LOCALAPPDATA': self.podman_data_dir,
            })

        podman_bin_dir = self._podman_bin_dir()
        if podman_bin_dir and os.path.exists(podman_bin_dir):
            env['PATH'] = _prepend_path_once(env.get('PATH', ''), podman_bin_dir)

        if include_docker_host and self._wsl_service_started:
            env['DOCKER_HOST'] = self._tcp_docker_host()
        else:
            env.pop('DOCKER_HOST', None)
        return env

    def _setup_environment(self):
        podman_bin_dir = self._podman_bin_dir()
        new_path = _prepend_path_once(os.environ.get('PATH', ''), podman_bin_dir)
        if new_path != os.environ.get('PATH', ''):
            os.environ['PATH'] = new_path
            logger.info(f'[PodmanManager] Added to PATH: {podman_bin_dir}')

        env = self._build_podman_env(include_docker_host=False)
        for key in (
            'XDG_CONFIG_HOME',
            'XDG_DATA_HOME',
            'PODMAN_CONFIG_DIR',
            'CONTAINERS_CONF_DIR',
            'CONTAINERS_STORAGE_CONF',
            'HOME',
            'CONTAINERS_MACHINE_PROVIDER_DIR',
            'APPDATA',
            'USERPROFILE',
            'LOCALAPPDATA',
        ):
            if key in env:
                os.environ[key] = env[key]
        os.environ.pop('DOCKER_HOST', None)

    def _run_podman(self, args, timeout=60, capture=True):
        cmd = [self.podman_exe] + args
        env = self._build_podman_env(include_docker_host=True)
        try:
            if capture:
                result = _run_hidden(cmd, timeout=timeout, capture=True, env=env)
                if result is None:
                    logger.error(f'[PodmanManager] podman command failed (hidden runner returned None)')
                    return None
                return result
            else:
                proc = _run_hidden(cmd, timeout=timeout, capture=False, env=env)
                return proc
        except FileNotFoundError:
            logger.error(f'[PodmanManager] podman executable not found: {self.podman_exe}')
            return None
        except subprocess.TimeoutExpired:
            logger.error(f'[PodmanManager] Command timed out: {" ".join(args)}')
            return None
        except Exception as e:
            logger.error(f'[PodmanManager] Command failed: {e}')
            return None

    def _run_podman_streaming(self, args, timeout=600):
        cmd = [self.podman_exe] + args
        env = self._build_podman_env(include_docker_host=True)
        try:
            result = _run_hidden_streaming(cmd, timeout=timeout, env=env)
            if result is None:
                logger.error('[PodmanManager] podman streaming command failed')
                return None
            return result
        except Exception as e:
            logger.error(f'[PodmanManager] podman streaming command failed: {e}')
            return None

    def _run_wsl(self, cmd_str, timeout=30):
        if platform.system() != 'Windows':
            return None
        full_cmd = ['wsl', '-d', WSL_DISTRO_NAME, '-u', 'root', '--exec', '/bin/bash', '-c', cmd_str]
        result = _run_hidden(full_cmd, timeout=timeout, capture=True)
        if result is None:
            logger.error('[PodmanManager] wsl command failed (hidden runner returned None)')
            return None
        return _decode_completed_process(result)

    def _run_host_command(self, args, timeout=20):
        if args and args[0] == 'wsl':
            result = _run_hidden(args, timeout=timeout, capture=True)
            if result is None:
                return None
            return _decode_completed_process(result)
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout, **_SUBPROCESS_KWARGS)
            return _decode_completed_process(result)
        except Exception as e:
            logger.error(f'[PodmanManager] Host command failed {args}: {e}')
            return None

    def _tcp_docker_host(self):
        return f'tcp://{self._podman_api_host}:{PODMAN_API_PORT}'

    def get_api_http_url(self):
        if self._wsl_service_started or self._engine_ready_cache.get('value'):
            return f'http://{self._podman_api_host}:{PODMAN_API_PORT}'
        return None

    def _get_wsl_ipv4(self, force=False):
        if platform.system() != 'Windows':
            return None
        if not self._is_wsl_distro_registered():
            self._wsl_ip_cache = {'value': None, 'time': time.monotonic()}
            return None
        now = time.monotonic()
        cached = self._wsl_ip_cache
        if not force:
            ttl = 30.0 if cached.get('value') else 10.0
            if now - cached.get('time', 0.0) < ttl:
                return cached.get('value')
        result = self._run_wsl(
            'ip -4 route get 1.1.1.1 2>/dev/null; ip -o -4 addr show scope global 2>/dev/null',
            timeout=5
        )
        text = ((result.stdout or '') + '\n' + (result.stderr or '')) if result else ''
        candidates = []
        for match in re.finditer(r'\bsrc\s+(\d+\.\d+\.\d+\.\d+)\b', text):
            candidates.append(match.group(1))
        for match in re.finditer(r'\binet\s+(\d+\.\d+\.\d+\.\d+)/', text):
            candidates.append(match.group(1))
        for ip in candidates:
            if not ip.startswith(('127.', '169.254.')):
                self._wsl_ip_cache = {'value': ip, 'time': now}
                return ip
        self._wsl_ip_cache = {'value': None, 'time': now}
        return None

    def pull_image_stream_wsl(self, image_ref, first_output_timeout=120, resolve_timeout=300, idle_timeout=0, total_timeout=21600, cancel_event=None, retry_count=None, bypass_registry_mirrors=False, serialize_layers=False):
        """Pull image inside WSL using podman pull, yielding progress lines.

        Podman does not continuously print byte progress when stdout is a pipe.
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
        full_cmd = ['wsl', '-d', WSL_DISTRO_NAME, '-u', 'root', '--exec', '/bin/bash', '-c', cmd_str]
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
            logger.info('[PodmanManager] WSL registries.conf written successfully.')
        else:
            logger.warning('[PodmanManager] Failed to write WSL registries.conf, trying echo method...')
            escaped = reg_content.replace("'", "'\\''")
            self._run_wsl(f"mkdir -p /etc/containers && echo '{escaped}' > /etc/containers/registries.conf")

    def _get_configured_mirrors(self, include_defaults=False):
        default_mirrors = [
            'docker.1ms.run',
            'docker.m.daocloud.io',
            'dockerproxy.link',
            'dockerproxy.com',
            'docker.1panel.live',
            'proxy.vvvv.ee',
            'docker.jiaxin.site',
            'registry.cyou',
            'docker.xuanyuan.me',
            'hub.rat.dev',
            'docker.nju.edu.cn',
            'docker.mirrors.sjtug.sjtu.edu.cn',
            '05f073ad3c0010ea0f4bc00b7105ec20.mirror.swr.myhuaweicloud.com',
        ]
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
                        m = m.removeprefix('https://').removeprefix('http://')
                        if not m.startswith(('e:', 'c:', 'd:', '\\\\')):
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
                        if name == WSL_DISTRO_NAME:
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
            return WSL_DISTRO_NAME in self._wsl_registered_distros_from_cli()
        except Exception:
            return False

    def _is_wsl2_functional(self):
        if platform.system() != 'Windows':
            return True
        result = self._run_host_command(['wsl', '--version'], timeout=10)
        if result and result.returncode == 0:
            out = (result.stdout or '').strip()
            if 'WSL' in out:
                logger.info(f'[PodmanManager] WSL2 installed: {out.splitlines()[0] if out else "OK"}')
                return True
        result2 = self._run_host_command(['wsl', '--status'], timeout=10)
        if result2 and result2.returncode == 0:
            logger.info('[PodmanManager] WSL2 status OK')
            return True
        return False

    def _is_wsl_distro_functional(self, force=False):
        now = time.monotonic()
        cached = self._wsl_distro_cache
        if not force and cached.get('value') is not None:
            ttl = 30.0 if cached.get('value') else 2.0
            if now - cached.get('time', 0.0) < ttl:
                return bool(cached.get('value'))
        if not self._is_wsl_distro_registered():
            self._wsl_distro_cache = {'value': False, 'time': now}
            return False
        check = self._run_host_command(
            ['wsl', '-d', WSL_DISTRO_NAME, '--', 'echo', 'DISTRO_OK'],
            timeout=6
        )
        if check and check.returncode == 0 and 'DISTRO_OK' in (check.stdout or ''):
            self._wsl_distro_cache = {'value': True, 'time': time.monotonic()}
            return True
        self._wsl_distro_cache = {'value': False, 'time': time.monotonic()}
        return False

    def _reset_wsl_runtime_state(self):
        """Discard only transient connection state; never touch WSL storage."""
        self._wsl_distro_cache = {'value': None, 'time': 0.0}
        self._wsl_ip_cache = {'value': None, 'time': 0.0}
        self._engine_ready_cache = {'value': False, 'time': 0.0}
        self._wsl_service_started = False
        self._api_service_proc = None
        self._connection_state = 'recovering'
        self._service_failures = 0
        self._service_retry_after = 0.0
        self._close_api_client()
        os.environ.pop('DOCKER_HOST', None)

    def _wait_for_wsl_distro(self, attempts=5, interval=1.0):
        for attempt in range(max(1, attempts)):
            if self._is_wsl_distro_functional(force=True):
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
                f'[PodmanManager] WSL service recovery was cancelled or failed (exit={exit_code})'
            )
            return False
        logger.info('[PodmanManager] [RECOVERY] WSL control service restarted successfully')
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
            logger.warning(f'[PodmanManager] Could not display WSL recovery consent: {e}')
            return False

    def _record_wsl_recovery_failure(self, reason):
        self._connection_state = 'recovery_failed'
        self._wsl_recovery_retry_after = time.monotonic() + 300.0
        self._last_wsl_recovery_error = (
            f'Existing Podman WSL distro {self.wsl_distro_name} is not responding. '
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
                f'[PodmanManager] [RECOVERY] Starting non-destructive WSL recovery for {self.wsl_distro_name}'
            )

            # Level 1: affect only the PrimiGenius-owned distro.
            self._run_host_command(['wsl', '--terminate', self.wsl_distro_name], timeout=12)
            self._reset_wsl_runtime_state()
            if self._wait_for_wsl_distro(attempts=3, interval=1.0):
                logger.info('[PodmanManager] [RECOVERY] WSL recovered after distro termination')
                self._wsl_recovery_retry_after = 0.0
                self._last_wsl_recovery_error = ''
                return True, 'WSL recovered after restarting the PrimiGenius environment'

            # Level 2: restart the WSL VM layer.  This is still data-preserving,
            # though it closes other active WSL sessions on the host.
            if not self._confirm_host_wide_wsl_recovery():
                logger.warning('[PodmanManager] [RECOVERY] Host-wide WSL recovery was declined')
                return self._record_wsl_recovery_failure('Host-wide automatic recovery was cancelled.')
            logger.warning('[PodmanManager] [RECOVERY] Distro restart failed; shutting down the WSL VM layer')
            shutdown_result = self._run_host_command(['wsl', '--shutdown'], timeout=20)
            if shutdown_result is None or shutdown_result.returncode != 0:
                return self._record_wsl_recovery_failure(
                    'WSL shutdown did not respond; restart Windows before trying again.'
                )
            self._reset_wsl_runtime_state()
            if self._wait_for_wsl_distro(attempts=5, interval=1.0):
                logger.info('[PodmanManager] [RECOVERY] WSL recovered after VM shutdown')
                self._wsl_recovery_retry_after = 0.0
                self._last_wsl_recovery_error = ''
                return True, 'WSL recovered after restarting its virtual machine layer'

            # Level 3: a stuck WSL control service requires elevation.  UAC is
            # the user-visible consent boundary for this host-wide operation.
            logger.warning('[PodmanManager] [RECOVERY] WSL VM remains unavailable; requesting service recovery via UAC')
            if self._restart_wsl_control_service_elevated():
                time.sleep(2)
                self._reset_wsl_runtime_state()
                if self._wait_for_wsl_distro(attempts=8, interval=1.0):
                    logger.info('[PodmanManager] [RECOVERY] WSL recovered after control-service restart')
                    self._wsl_recovery_retry_after = 0.0
                    self._last_wsl_recovery_error = ''
                    return True, 'WSL recovered after restarting its Windows service'

            return self._record_wsl_recovery_failure(
                'Automatic recovery was cancelled or Windows could not restart the WSL service.'
            )

    def _force_cleanup_broken_distro(self):
        if platform.system() != 'Windows':
            return False
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Lxss')
            i = 0
            to_delete = []
            while True:
                try:
                    n = winreg.EnumKey(key, i)
                    sk = winreg.OpenKey(key, n)
                    try:
                        nm, _ = winreg.QueryValueEx(sk, 'DistributionName')
                        if nm == WSL_DISTRO_NAME:
                            try:
                                bp, _ = winreg.QueryValueEx(sk, 'BasePath')
                                if not bp or not os.path.exists(bp):
                                    to_delete.append(n)
                                    logger.warning(f'[PodmanManager] Found broken distro registry: {n} (BasePath missing or invalid: {bp})')
                            except Exception:
                                to_delete.append(n)
                                logger.warning(f'[PodmanManager] Found broken distro registry: {n} (no BasePath value)')
                    finally:
                        winreg.CloseKey(sk)
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(key)

            if not to_delete:
                return False

            logger.info(f'[PodmanManager] Force deleting {len(to_delete)} broken distro registry key(s)...')
            for subkey_name in to_delete:
                full_path = rf'Software\Microsoft\Windows\CurrentVersion\Lxss\{subkey_name}'
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, full_path)
                    logger.info(f'[PodmanManager] Deleted broken registry key: {subkey_name}')
                except Exception as e:
                    logger.warning(f'[PodmanManager] Failed to delete registry key {subkey_name}: {e}')
            return True
        except Exception as e:
            logger.warning(f'[PodmanManager] Registry cleanup failed: {e}')
            return False

    def _is_wsl_service_healthy(self):
        if platform.system() != 'Windows':
            return True
        result = self._run_host_command(['wsl', '--list', '--quiet'], timeout=10)
        if result and result.returncode == 0:
            return True
        if result and result.returncode != 0:
            stderr = (result.stderr or '').lower()
            if 'e_unexpected' in stderr or '0xffffffff' in str(result.returncode) or result.returncode == -1:
                logger.warning('[PodmanManager] WSL service unhealthy (E_UNEXPECTED / 0xffffffff), broken distro registration likely')
                return False
        return True

    def _ensure_wsl_service_running(self):
        if platform.system() != 'Windows':
            return True
        if self._is_wsl_service_healthy():
            return True
        logger.info('[PodmanManager] WSL service unhealthy, attempting registry cleanup...')
        cleaned = self._force_cleanup_broken_distro()
        if cleaned:
            logger.info('[PodmanManager] Broken registry entries cleaned, retrying WSL...')
            time.sleep(2)
        for attempt in range(5):
            result = self._run_host_command(['wsl', '--list', '--quiet'], timeout=10)
            if result and result.returncode == 0:
                logger.info(f'[PodmanManager] WSL service healthy (attempt {attempt+1})')
                return True
            logger.info(f'[PodmanManager] WSL service not responsive, retrying... (attempt {attempt+1}/5)')
            time.sleep(3)
        logger.error('[PodmanManager] WSL service failed to start after cleanup and retries')
        return False

    def _sync_wsl_runtime_config(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_wsl_config_sync < 300:
            logger.info('[PodmanManager] WSL runtime config already synced recently, skipping.')
            return

        logger.info('[PodmanManager] Fixing /run/user/1000 permissions inside WSL...')
        result = self._run_wsl(
            'mkdir -p /run/user/1000 && chown 1000:1000 /run/user/1000 && chmod 700 /run/user/1000 && echo OK',
            timeout=8
        )
        if result is None or 'OK' not in (result.stdout or ''):
            logger.warning('[PodmanManager] Failed to fix /run/user/1000 permissions, trying anyway...')

        logger.info('[PodmanManager] Fixing DNS inside WSL...')
        dns_result = self._run_wsl(
            'echo "nameserver 8.8.8.8" > /etc/resolv.conf && '
            'echo "nameserver 114.114.114.114" >> /etc/resolv.conf && echo DNS_OK',
            timeout=8
        )
        if dns_result and 'DNS_OK' in (dns_result.stdout or ''):
            logger.info('[PodmanManager] DNS fixed successfully.')
        else:
            logger.warning('[PodmanManager] DNS fix failed, continuing anyway...')

        logger.info('[PodmanManager] Writing registries.conf inside WSL...')
        self._write_wsl_registries_conf()
        self._last_wsl_config_sync = time.monotonic()

    def _collect_wsl_start_diagnostics(self, reason=''):
        quoted_reason = shlex.quote(str(reason or 'unknown'))
        script = (
            'set +e; '
            f'echo reason={quoted_reason}; '
            'printf "podman_bin="; command -v podman || true; '
            'podman --version 2>&1 | head -n 1; '
            f'(ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | grep ":{PODMAN_API_PORT}" || true; '
            'ps -eo pid,ppid,args 2>/dev/null | grep "[p]odman .*system service" || true; '
            'if [ -f /tmp/primigenius-podman-service.log ]; then '
            'echo service_log_tail:; tail -n 30 /tmp/primigenius-podman-service.log; fi'
        )
        result = self._run_wsl(script, timeout=8)
        stdout = (result.stdout or '') if result else ''
        stderr = (result.stderr or '') if result else ''
        diagnostics = (stdout + '\n' + stderr).strip()
        self._last_start_diagnostics = diagnostics
        if diagnostics:
            logger.warning(f'[PodmanManager] WSL Podman diagnostics:\n{diagnostics}')
        return diagnostics

    def _close_api_client(self):
        with self._api_client_lock:
            client = self._api_client
            self._api_client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _mark_service_ready(self):
        self._wsl_service_started = True
        self._connection_state = 'ready'
        self._service_failures = 0
        self._service_retry_after = 0.0
        os.environ['DOCKER_HOST'] = self._tcp_docker_host()
        self._engine_ready_cache = {'value': True, 'time': time.monotonic()}

    def _mark_service_failed(self):
        self._wsl_service_started = False
        self._connection_state = 'backoff'
        self._service_failures = min(self._service_failures + 1, 8)
        delay = min(60.0, 3.0 * (2 ** (self._service_failures - 1)))
        self._service_retry_after = time.monotonic() + delay
        self._engine_ready_cache = {'value': False, 'time': time.monotonic()}
        self._close_api_client()

    def _start_wsl_podman_service(self, force=False):
        with self._service_lock:
            return self._start_wsl_podman_service_locked(force=force)

    def _start_wsl_podman_service_locked(self, force=False):
        if self._wsl_service_started and self._test_tcp_connection():
            self._mark_service_ready()
            return True

        if not force and time.monotonic() < self._service_retry_after:
            return False

        self._connection_state = 'starting'

        if self._test_tcp_connection():
            self._api_service_proc = None
            self._mark_service_ready()
            logger.info(f'[PodmanManager] Reusing existing Podman API service on {self._tcp_docker_host()}')
            return True

        if not self._is_wsl_distro_functional():
            logger.error('[PodmanManager] WSL distro not functional, cannot start service. Run auto_setup first.')
            self._mark_service_failed()
            return False

        self._sync_wsl_runtime_config()
        self._run_wsl(
            f'pkill -f "podman .*system service.*{PODMAN_API_PORT}" 2>/dev/null || true; '
            'rm -f /tmp/primigenius-podman-service.log; echo SERVICE_CLEANED',
            timeout=6
        )

        logger.info(f'[PodmanManager] Starting Podman API service on TCP port {PODMAN_API_PORT} inside WSL...')
        try:
            service_script = (
                'LOG=/tmp/primigenius-podman-service.log; '
                'PODMAN_BIN="$(command -v podman || true)"; '
                'if [ -z "$PODMAN_BIN" ] && [ -x /usr/bin/podman ]; then PODMAN_BIN=/usr/bin/podman; fi; '
                'if [ -z "$PODMAN_BIN" ] && [ -x /usr/sbin/podman ]; then PODMAN_BIN=/usr/sbin/podman; fi; '
                'if [ -z "$PODMAN_BIN" ]; then echo "$(date -Iseconds) podman binary not found" >> "$LOG"; exit 127; fi; '
                f'echo "$(date -Iseconds) starting $PODMAN_BIN system service tcp:0.0.0.0:{PODMAN_API_PORT}" >> "$LOG"; '
                f'exec "$PODMAN_BIN" system service --time=0 tcp:0.0.0.0:{PODMAN_API_PORT} >> "$LOG" 2>&1'
            )
            cmd_args = [
                'wsl', '-d', WSL_DISTRO_NAME, '-u', 'root', '--exec',
                '/bin/bash', '-lc', service_script
            ]
            proc = _run_hidden(cmd_args, timeout=0, capture=False)
            if proc is None:
                logger.warning('[PodmanManager] _run_hidden returned None, trying direct Popen fallback...')
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                proc = subprocess.Popen(
                    cmd_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000 | 0x00000200,
                    startupinfo=si
                )
                logger.info('[PodmanManager] WSL Podman service started via direct Popen fallback')
            else:
                logger.info(f'[PodmanManager] WSL Podman service process started via hidden runner')
            time.sleep(0.8)
            if proc is not None and proc.poll() is not None:
                logger.error(f'[PodmanManager] WSL Podman service exited immediately (code {proc.returncode})')
                self._api_service_proc = None
                self._collect_wsl_start_diagnostics('service exited immediately')
                self._mark_service_failed()
                return False
            self._api_service_proc = proc
        except Exception as e:
            logger.error(f'[PodmanManager] Failed to start WSL Podman service: {e}')
            import traceback
            logger.error(f'[PodmanManager] Traceback: {traceback.format_exc()}')
            self._mark_service_failed()
            return False

        for i in range(24):
            time.sleep(0.5)
            if self._test_tcp_connection():
                self._mark_service_ready()
                logger.info(f'[PodmanManager] Podman API service is ready on TCP port {PODMAN_API_PORT}')
                return True
            if i % 6 == 5:
                logger.info(f'[PodmanManager] Waiting for Podman API service... ({(i+1)//2}s)')

        logger.error('[PodmanManager] Podman API service did not become ready in 12 seconds')
        self._collect_wsl_start_diagnostics('tcp ping timeout')
        self._mark_service_failed()
        return False

    def _test_tcp_connection(self, timeout=0.5):
        import socket
        import urllib.request
        hosts = [PODMAN_API_HOST]
        if self._podman_api_host not in hosts:
            hosts.append(self._podman_api_host)
        wsl_ip = self._get_wsl_ipv4()
        if wsl_ip and wsl_ip not in hosts:
            hosts.append(wsl_ip)

        def _try_host(host):
            try:
                with socket.create_connection((host, PODMAN_API_PORT), timeout=timeout):
                    pass
            except Exception:
                return False
            base_url = f'http://{host}:{PODMAN_API_PORT}'
            for path in ('/_ping', f'/v{DOCKER_API_VERSION}/_ping'):
                try:
                    req = urllib.request.Request(base_url + path)
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        if resp.status == 200:
                            if host != self._podman_api_host:
                                logger.info(f'[PodmanManager] Podman API reachable via {host}:{PODMAN_API_PORT}')
                            self._podman_api_host = host
                            return True
                except Exception:
                    pass
            return False

        for host in hosts:
            if _try_host(host):
                return True

        if time.monotonic() - self._wsl_ip_cache.get('time', 0.0) > 15:
            fresh_wsl_ip = self._get_wsl_ipv4(force=True)
            if fresh_wsl_ip and fresh_wsl_ip not in hosts:
                if _try_host(fresh_wsl_ip):
                    return True
        return False

    def _stop_wsl_podman_service(self):
        with self._service_lock:
            if not self._wsl_service_started and not self._test_tcp_connection():
                return
            logger.info('[PodmanManager] Stopping WSL Podman service...')
            self._connection_state = 'stopping'
            self._run_wsl('pkill -f "podman system service" 2>/dev/null; echo done')
            self._api_service_proc = None
            self._wsl_service_started = False
            self._connection_state = 'stopped'
            self._service_failures = 0
            self._service_retry_after = 0.0
            self._engine_ready_cache = {'value': False, 'time': time.monotonic()}
            self._close_api_client()
            if 'DOCKER_HOST' in os.environ:
                del os.environ['DOCKER_HOST']

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
            logger.info('[PodmanManager] [SETUP] Enabling WSL2 features...')
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
                logger.info('[PodmanManager] [OK] WSL2 features enabled')
                self._run_host_command(['wsl', '--set-default-version', '2'], timeout=15)
                return True, 'WSL2 features enabled. A system restart may be required.'
            elif r1.returncode == 3010 or r2.returncode == 3010:
                logger.info('[PodmanManager] [OK] WSL2 features enabled (reboot required)')
                self._run_host_command(['wsl', '--set-default-version', '2'], timeout=15)
                return True, 'WSL2 features enabled. A system restart is required (code 3010).'
            else:
                err = (r1.stderr or '') + (r2.stderr or '')
                logger.warning(f'[PodmanManager] dism enable failed: {err}')
                logger.info('[PodmanManager] [SETUP] Trying wsl --install fallback...')
                install_result = self._run_host_command(
                    ['wsl', '--install', '--no-distribution'], timeout=180
                )
                if install_result and install_result.returncode == 0:
                    self._run_host_command(['wsl', '--set-default-version', '2'], timeout=15)
                    logger.info('[PodmanManager] [OK] WSL2 installed via wsl --install fallback')
                    return True, 'WSL2 installed via wsl --install fallback.'
                return False, f'WSL2 feature enable failed (needs admin?): {err}'
        except Exception as e:
            logger.error(f'[PodmanManager] enable_wsl2 exception: {e}')
            return False, str(e)

    def is_podman_available(self):
        result = self._run_podman(['--version'], timeout=5)
        if result and result.returncode == 0:
            return True
        return False

    def _ensure_wsl2_ready(self):
        if platform.system() != 'Windows':
            return True, 'Not Windows'
        logger.info('[PodmanManager] [PHASE 1] Checking WSL2 readiness...')
        try:
            test_result = self._run_host_command(
                ['wsl', '--list', '--verbose'], timeout=10
            )
            if test_result and test_result.returncode == 0:
                out = (test_result.stdout or '').replace('\x00', '')
                if re.search(r'\b2\b', out):
                    logger.info('[PodmanManager] [PHASE 1] WSL2 is functional (distro with v2 found)')
                    return True, 'WSL2 ready'
        except Exception:
            pass
        logger.info('[PodmanManager] [PHASE 1] WSL2 not functional, updating WSL kernel...')
        update_result = self._run_host_command(['wsl', '--update'], timeout=180)
        if update_result:
            out = (update_result.stdout or '').strip()
            logger.info(f'[PodmanManager] [PHASE 1] WSL update: {out}')
            if update_result.returncode == 0:
                logger.info('[PodmanManager] [PHASE 1] WSL kernel updated OK')
                return True, 'WSL2 updated'
        logger.warning('[PodmanManager] [PHASE 1] WSL update failed or returned error')
        try:
            status_result = self._run_host_command(['wsl', '--status'], timeout=10)
            if status_result:
                logger.info(f'[PodmanManager] [PHASE 1] WSL status: {(status_result.stdout or "").strip()}')
        except Exception:
            pass
        if not self.is_wsl2_feature_enabled():
            logger.info('[PodmanManager] [PHASE 1] WSL2 feature not enabled, enabling...')
            ok, msg = self.enable_wsl2()
            if ok:
                return True, 'WSL2 enabled'
            return False, f'WSL2 not ready: {msg}'
        return True, 'WSL2 feature enabled but kernel may need reboot'

    def get_podman_version(self):
        result = self._run_podman(['--version'], timeout=5)
        if result and result.returncode == 0:
            return result.stdout.strip()
        return None

    def machine_exists(self):
        if self._is_wsl_distro_registered():
            return True
        result = self._run_podman(['machine', 'list', '--format', 'json'], timeout=10)
        if result is None or result.returncode != 0:
            return False
        try:
            machines = json.loads(result.stdout) if result.stdout.strip() else []
            return len(machines) > 0
        except Exception:
            return False

    def machine_status(self):
        if self._wsl_service_started and self.is_engine_ready():
            return {
                'status': 'success',
                'machines': [{'Name': self.machine_name, 'Running': True}],
                'running': True,
                'count': 1
            }
        distro_registered = self._is_wsl_distro_registered()
        distro_functional = False
        if distro_registered:
            distro_functional = self._is_wsl_distro_functional()
        result = self._run_podman(['machine', 'list', '--format', 'json'], timeout=10)
        if result is None:
            if distro_registered:
                engine_ready = self.is_engine_ready()
                return {
                    'status': 'success',
                    'machines': [{'Name': self.machine_name, 'Running': engine_ready}],
                    'running': engine_ready,
                    'count': 1,
                    'source': 'wsl-registry',
                    'functional': distro_functional,
                    'message': 'Podman machine metadata is not ready yet; using WSL registration.'
                }
            return {'status': 'error', 'message': 'Podman command failed'}
        if result.returncode != 0:
            if distro_registered:
                engine_ready = self.is_engine_ready()
                return {
                    'status': 'success',
                    'machines': [{'Name': self.machine_name, 'Running': engine_ready}],
                    'running': engine_ready,
                    'count': 1,
                    'source': 'wsl-registry',
                    'functional': distro_functional,
                    'message': result.stderr.strip() or 'Podman machine metadata is not ready yet.'
                }
            return {'status': 'error', 'message': result.stderr.strip() or 'Unknown error'}
        try:
            machines = json.loads(result.stdout) if result.stdout.strip() else []
            if not machines and distro_registered:
                machines = [{'Name': self.machine_name, 'Running': self.is_engine_ready()}]
            running = any(m.get('Running', False) for m in machines)
            return {
                'status': 'success',
                'machines': machines,
                'running': running,
                'count': len(machines),
                'source': 'podman-machine-list' if result.stdout.strip() else 'wsl-registry',
                'functional': distro_functional if distro_registered else running
            }
        except Exception as e:
            if distro_registered:
                engine_ready = self.is_engine_ready()
                return {
                    'status': 'success',
                    'machines': [{'Name': self.machine_name, 'Running': engine_ready}],
                    'running': engine_ready,
                    'count': 1,
                    'source': 'wsl-registry',
                    'functional': distro_functional,
                    'message': str(e)
                }
            return {'status': 'error', 'message': str(e)}

    def machine_is_running(self):
        return self.is_engine_ready()

    def _get_wsl_default_machine_dir(self):
        local_app_data = os.environ.get('LOCALAPPDATA', os.path.expandvars(r'%LOCALAPPDATA%'))
        return os.path.join(local_app_data, 'containers', 'podman', 'machine', 'wsl', 'wsldist')

    def _ensure_wsldist_junction(self):
        if platform.system() != 'Windows':
            return
        default_wsldist = self._get_wsl_default_machine_dir()
        target_wsldist = os.path.join(self.podman_data_dir, 'machine', 'wsldist')
        os.makedirs(target_wsldist, exist_ok=True)
        if os.path.exists(default_wsldist):
            if os.path.islink(default_wsldist) or os.path.ismount(default_wsldist):
                try:
                    link_target = os.readlink(default_wsldist)
                    if os.path.normpath(link_target) == os.path.normpath(target_wsldist):
                        return
                except Exception:
                    pass
                return
            if os.listdir(default_wsldist):
                logger.info(f'[PodmanManager] Moving existing wsldist data to {target_wsldist}...')
                try:
                    _run_hidden(['wsl', '--shutdown'], timeout=15, capture=True)
                    time.sleep(3)
                except Exception:
                    pass
                try:
                    for item in os.listdir(default_wsldist):
                        src = os.path.join(default_wsldist, item)
                        dst = os.path.join(target_wsldist, item)
                        if not os.path.exists(dst):
                            shutil.move(src, dst)
                            logger.info(f'[PodmanManager] Moved: {item}')
                except Exception as e:
                    logger.warning(f'[PodmanManager] Failed to move wsldist data: {e}')
                    return
                try:
                    shutil.rmtree(default_wsldist, ignore_errors=True)
                except Exception as e:
                    logger.warning(f'[PodmanManager] Failed to remove old wsldist: {e}')
                    return
        else:
            parent = os.path.dirname(default_wsldist)
            os.makedirs(parent, exist_ok=True)
        try:
            subprocess.run(
                ['cmd', '/c', 'mklink', '/J', default_wsldist, target_wsldist],
                capture_output=True, timeout=10, **_SUBPROCESS_KWARGS
            )
            if os.path.exists(default_wsldist):
                logger.info(f'[PodmanManager] Created Junction: {default_wsldist} -> {target_wsldist}')
            else:
                logger.warning(f'[PodmanManager] Junction creation may have failed for {default_wsldist}')
        except Exception as e:
            logger.warning(f'[PodmanManager] Failed to create wsldist Junction: {e}')

    def machine_init(self, cpus=2, memory=4096, disk_size=60, name='', skip_wsl_check=False):
        if self._is_wsl_distro_functional():
            return True, 'WSL distro already functional'

        if not skip_wsl_check:
            logger.info('[PHASE 2] Cleaning up any broken WSL distro registration...')
            if self._is_wsl_distro_registered():
                logger.info('[PHASE 2] Distro registered but not functional, attempting cleanup...')
                self._run_host_command(['wsl', '--terminate', WSL_DISTRO_NAME], timeout=10)
                time.sleep(2)
                self._run_host_command(['wsl', '--unregister', WSL_DISTRO_NAME], timeout=15)
                time.sleep(2)

            if self._is_wsl_distro_registered():
                logger.warning('[PHASE 2] Distro still registered, force cleaning registry...')
                self._force_cleanup_broken_distro()
                time.sleep(2)

            if self._is_wsl_distro_registered():
                logger.error('[PHASE 2] Distro STILL registered, forcing full WSL shutdown...')
                self._run_host_command(['wsl', '--shutdown'], timeout=15)
                time.sleep(5)
                self._force_cleanup_broken_distro()
                time.sleep(2)

            if self.machine_exists():
                logger.info('[PHASE 2] Stale Podman VM found, removing...')
                self.machine_remove()
                time.sleep(2)

            if not self._ensure_wsl_service_running():
                return False, 'WSL service failed to start after cleanup. Please restart your computer and try again.'

            logger.info('[PHASE 2] Ensuring WSL2 is ready before machine init...')
            wsl_ok, wsl_msg = self._ensure_wsl2_ready()
            if not wsl_ok:
                return False, f'WSL2 not ready: {wsl_msg}. Please run "wsl --update" in a terminal and restart.'

            if not self._ensure_wsl_service_running():
                return False, 'WSL service failed to start. Please restart your computer and try again.'

        self._ensure_wsldist_junction()
        cmd = ['machine', 'init', '--rootful', '--now']
        cmd.extend(['--cpus', str(cpus)])
        cmd.extend(['--memory', str(memory)])
        cmd.extend(['--disk-size', str(disk_size)])
        cmd.append(self.machine_name)

        max_init_retries = 2
        for init_attempt in range(max_init_retries):
            if init_attempt > 0:
                logger.info(f'[PHASE 2] Retrying machine init (attempt {init_attempt+1}/{max_init_retries})...')
                if self._is_wsl_distro_registered():
                    self._run_host_command(['wsl', '--terminate', WSL_DISTRO_NAME], timeout=10)
                    time.sleep(2)
                    self._run_host_command(['wsl', '--unregister', WSL_DISTRO_NAME], timeout=15)
                    time.sleep(2)
                if self._is_wsl_distro_registered():
                    self._force_cleanup_broken_distro()
                    time.sleep(2)
                if not self._ensure_wsl_service_running():
                    if init_attempt < max_init_retries - 1:
                        continue
                    return False, 'WSL service failed to restart. Please restart your computer.'

            logger.info('[PHASE 2] Running podman machine init (downloading OS image, please wait...)...')
            logger.info(f'[PHASE 2] Init params: cpus={cpus}, memory={memory}MB, disk={disk_size}GB')
            result = self._run_podman_streaming(cmd, timeout=600)
            if result is None:
                if init_attempt < max_init_retries - 1:
                    logger.warning('[PHASE 2] Podman command returned None, retrying...')
                    continue
                return False, 'Podman command failed (timeout or not found)'
            logger.info(f'[PHASE 2] machine init returncode={result.returncode}')
            if result.returncode == 0:
                break

            err = (result.stderr or result.stdout or 'Init failed').strip()
            if 'already exists' in err.lower():
                logger.info('[PHASE 2] VM already exists, force removing and retrying...')
                self._run_host_command(['wsl', '--terminate', WSL_DISTRO_NAME], timeout=10)
                time.sleep(2)
                self._run_host_command(['wsl', '--unregister', WSL_DISTRO_NAME], timeout=15)
                time.sleep(2)
                if self._is_wsl_distro_registered():
                    self._force_cleanup_broken_distro()
                    time.sleep(2)
                self.machine_remove()
                time.sleep(2)
                if not self._ensure_wsl_service_running():
                    if init_attempt < max_init_retries - 1:
                        continue
                    return False, 'WSL service failed after cleanup.'
                if init_attempt < max_init_retries - 1:
                    continue
                return False, f'Machine init failed after removing stale VM: {err}'

            if '0xffffffff' in err or 'WSL import' in err:
                logger.warning(f'[PHASE 2] WSL import failed (0xffffffff) on attempt {init_attempt+1}')
                logger.info('[PHASE 2] Cleaning up broken registration and retrying...')
                self._force_cleanup_broken_distro()
                time.sleep(2)
                if not self._ensure_wsl_service_running():
                    if init_attempt < max_init_retries - 1:
                        continue
                    return False, 'WSL service failed after 0xffffffff error cleanup.'
                if init_attempt < max_init_retries - 1:
                    continue
                return False, f'WSL import failed after {max_init_retries} attempts. Error: {err}'

            if init_attempt < max_init_retries - 1:
                logger.warning(f'[PHASE 2] Init failed with: {err}, retrying...')
                continue
            return False, err
        else:
            return False, 'Machine init failed after all retries'

        logger.info('[PHASE 2] Podman Machine initialized and started (via --now).')
        time.sleep(3)
        for attempt in range(10):
            if self._is_wsl_distro_functional(force=True):
                logger.info(f'[PHASE 2] Distro functional check passed (attempt {attempt+1})')
                return True, (result.stdout or 'OK').strip()
            logger.info(f'[PHASE 2] Distro not yet functional, waiting... (attempt {attempt+1}/10)')
            time.sleep(3)
        logger.warning('[PHASE 2] Distro not functional after init+now, will try starting service separately.')
        return True, (result.stdout or 'OK').strip()

    def machine_start(self, name=''):
        if self._wsl_service_started and self.is_engine_ready():
            return True, 'Podman service already running'

        if not self.is_podman_available():
            logger.warning('[PodmanManager] Podman not available, re-searching...')
            self.podman_exe = self._find_podman_exe()
            if not self.is_podman_available():
                return False, f'Podman binary not found at {self.podman_exe}'

        if not self._is_wsl_distro_functional():
            return False, 'WSL distro not functional. Run auto_setup first.'

        self._ensure_wsldist_junction()
        ok = self._start_wsl_podman_service()
        if ok:
            return True, 'Podman service started via WSL direct management'
        return False, 'Failed to start Podman service via WSL'

    def machine_stop(self, name=''):
        self._stop_wsl_podman_service()
        logger.info('[PodmanManager] Podman service stopped.')
        return True, 'Podman service stopped'

    def machine_remove(self, name=''):
        self._stop_wsl_podman_service()
        if platform.system() == 'Windows':
            try:
                _run_hidden(['wsl', '--unregister', WSL_DISTRO_NAME],
                            timeout=30, capture=True)
            except Exception:
                pass
        result = self._run_podman(['machine', 'rm', '-f', self.machine_name], timeout=60)
        self._wsl_distro_cache = {'value': False, 'time': time.monotonic()}
        if result is None:
            return False, 'Podman command failed'
        return True, 'Machine removed'

    def machine_reset(self, cpus=4, memory=8192, disk_size=100, name=''):
        self.machine_remove(name)
        ok, msg = self.machine_init(cpus, memory, disk_size, name)
        if not ok:
            return False, f'Init after remove failed: {msg}'
        ok2, msg2 = self.machine_start(name)
        if not ok2:
            return False, f'Start after init failed: {msg2}'
        return True, 'Podman Machine reset successfully'

    def get_socket_path(self):
        if self._wsl_service_started:
            return None
        try:
            result = self._run_podman(['machine', 'inspect', self.machine_name], timeout=10)
            if result and result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                if isinstance(data, list) and len(data) > 0:
                    connection_info = data[0].get('ConnectionInfo', {})
                    podman_pipe = connection_info.get('PodmanPipe', {})
                    if podman_pipe and 'Path' in podman_pipe:
                        return podman_pipe['Path']
        except Exception:
            pass
        if platform.system() == 'Windows':
            return rf'\\.\pipe\{WSL_DISTRO_NAME}'
        return None

    def get_docker_host_env(self):
        if self._wsl_service_started:
            return self._tcp_docker_host()
        socket_path = self.get_socket_path()
        if not socket_path:
            return None
        if platform.system() == 'Windows':
            return f'npipe:{socket_path}'
        return f'unix://{socket_path}'

    def is_engine_ready(self):
        now = time.monotonic()
        cached = self._engine_ready_cache
        if now - cached.get('time', 0.0) < 1.0:
            if cached.get('value'):
                self._wsl_service_started = True
                self._connection_state = 'ready'
                os.environ['DOCKER_HOST'] = self._tcp_docker_host()
            return bool(cached.get('value'))

        if self._test_tcp_connection():
            if not self._wsl_service_started:
                self._wsl_service_started = True
                os.environ['DOCKER_HOST'] = self._tcp_docker_host()
            self._engine_ready_cache = {'value': True, 'time': time.monotonic()}
            self._connection_state = 'ready'
            return True
        if self._wsl_service_started:
            self._wsl_service_started = False
            self._connection_state = 'degraded'
        self._engine_ready_cache = {'value': False, 'time': time.monotonic()}
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
                logger.info('[PodmanManager] Auto setup already running in another thread, waiting...')
                return True, 'Setup already in progress'
            self._setup_running = True
        try:
            return self._auto_setup_impl()
        finally:
            with self._setup_lock:
                self._setup_running = False

    def _auto_setup_impl(self):
        logger.info('[PodmanManager] ========== Auto Setup Start ==========')

        if not self.is_podman_available():
            logger.info(f'[PodmanManager] [CHECK] Podman binary not found: {self.podman_exe}')
            self.podman_exe = self._find_podman_exe()
            if not self.is_podman_available():
                logger.error('[PodmanManager] [FAIL] Podman binary not found, please reinstall PrimiGenius')
                return False, f'Podman binary not found at {self.podman_exe}. Please reinstall PrimiGenius.'
        logger.info(f'[PodmanManager] [CHECK] Podman binary OK ({self.podman_exe})')

        if self.is_engine_ready():
            logger.info('[PodmanManager] [CHECK] Podman engine already ready, nothing to do.')
            return True, 'Podman is ready'

        if platform.system() == 'Windows':
            existing_distro = self._is_wsl_distro_registered()
            distro_functional = self._is_wsl_distro_functional()
            if existing_distro:
                if distro_functional:
                    logger.info('[PodmanManager] [CHECK] Existing WSL distro detected; preserving current deployment.')
                else:
                    recovered, recovery_msg = self._recover_unresponsive_wsl()
                    if not recovered:
                        return False, recovery_msg
            else:
                logger.info('[PodmanManager] ========== Phase 1: WSL2 Readiness ==========')
                wsl_ok, wsl_msg = self._ensure_wsl2_ready()
                if not wsl_ok:
                    logger.error(f'[PodmanManager] [FAIL] WSL2 not ready: {wsl_msg}')
                    return False, f'WSL2 setup failed: {wsl_msg}'
                logger.info(f'[PodmanManager] [OK] WSL2 ready: {wsl_msg}')

        if self._is_wsl_distro_functional():
            logger.info(f'[PodmanManager] [CHECK] WSL distro {WSL_DISTRO_NAME} functional OK')
        else:
            if self._is_wsl_distro_registered():
                # Existing installations may be temporarily unavailable after
                # sleep, a Windows update, or a pending reboot. Never unregister
                # or recreate an existing distro automatically: that could
                # destroy a working user's images and containers.
                recovered, recovery_msg = self._recover_unresponsive_wsl()
                if not recovered:
                    return False, recovery_msg
            else:
                logger.info('[PodmanManager] [CHECK] WSL distro not registered yet.')

            if not self._ensure_wsl_service_running():
                return False, 'WSL service failed to start. Please restart your computer and try again.'

            logger.info('[PodmanManager] ========== Phase 2: Podman Machine Setup ==========')
            logger.info('[PodmanManager] [SETUP] Creating WSL2 Linux environment for Podman (this may take a few minutes)...')
            ok, msg = self.machine_init(skip_wsl_check=True)
            if not ok:
                logger.error(f'[PodmanManager] [FAIL] Machine init failed: {msg}')
                return False, f'Machine init failed: {msg}'
            logger.info('[PodmanManager] [OK] Podman Machine created and started')

        if self.is_engine_ready():
            logger.info('[PodmanManager] [CHECK] Podman API service running OK')
        elif self._wsl_service_started:
            logger.info('[PodmanManager] [CHECK] Waiting for Podman API service...')
            if self.wait_for_engine(max_attempts=30, interval=1):
                logger.info('[PodmanManager] [OK] Podman API service ready')
            else:
                logger.warning('[PodmanManager] [WARN] Podman API not responding, restarting service...')
                self._stop_wsl_podman_service()
                ok = self._start_wsl_podman_service(force=True)
                if not ok:
                    logger.error('[PodmanManager] [FAIL] Podman API service restart failed')
                    return False, 'Failed to start Podman service'
        else:
            logger.info('[PodmanManager] ========== Phase 3: Starting Podman API Service ==========')
            ok = self._start_wsl_podman_service(force=True)
            if not ok:
                logger.warning('[PodmanManager] [WARN] First start attempt failed, retrying after WSL terminate...')
                self._run_host_command(['wsl', '--terminate', WSL_DISTRO_NAME], timeout=10)
                time.sleep(3)
                ok = self._start_wsl_podman_service(force=True)
                if not ok:
                    logger.error('[PodmanManager] [FAIL] Podman API service start failed')
                    return False, 'Failed to start Podman service'
            logger.info('[PodmanManager] [OK] Podman API service started')

        logger.info('[PodmanManager] ========== All Setup Complete, Podman Ready ==========')
        return True, 'Podman is ready'

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

            logger.info('[PodmanManager] Mirror config also written inside WSL')

        logger.info(f'[PodmanManager] Mirror configuration written to {config_path}')
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
        logger.info(f'[PodmanManager] Config written to {config_path}')
        return config_path

    def cleanup_all(self):
        logger.info('[PodmanManager] Full cleanup starting...')
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
                _run_hidden(['wsl', '--unregister', WSL_DISTRO_NAME],
                            timeout=30, capture=True)
            except Exception:
                pass

        try:
            if os.path.exists(self.podman_data_dir):
                shutil.rmtree(self.podman_data_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f'[PodmanManager] Data dir cleanup failed: {e}')

        try:
            if os.path.exists(self.podman_config_dir):
                shutil.rmtree(self.podman_config_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f'[PodmanManager] Config dir cleanup failed: {e}')

        logger.info('[PodmanManager] Full cleanup complete.')

    def stop_containers_and_machine(self):
        logger.info('[PodmanManager] Stopping all containers and machine...')
        self._stop_wsl_podman_service()
        logger.info('[PodmanManager] Containers and machine stopped.')

    def prepare_for_host_suspend(self):
        """Stop the owned Podman service and WSL distro before Windows sleeps."""
        self._stop_wsl_podman_service()
        if platform.system() != 'Windows':
            return True, 'Podman service stopped before system suspend'
        result = self._run_host_command(
            ['wsl', '--terminate', self.wsl_distro_name], timeout=12
        )
        self._reset_wsl_runtime_state()
        if result is not None and result.returncode == 0:
            logger.info('[PodmanManager] PrimiGenius WSL distro stopped before system suspend')
            return True, 'PrimiGenius container environment stopped before system suspend'
        logger.warning('[PodmanManager] Could not stop the WSL distro before system suspend')
        return False, 'The PrimiGenius WSL environment did not stop before system suspend'

    def start_api_service(self, timeout=0):
        if self._wsl_service_started:
            return True
        return self._start_wsl_podman_service()

    def stop_api_service(self):
        self._stop_wsl_podman_service()

    def get_api_service_url(self):
        if self._wsl_service_started:
            return self._tcp_docker_host()
        if platform.system() == 'Windows':
            socket_path = self.get_socket_path()
            if socket_path:
                return f'npipe:{socket_path}'
        return None

    def get_podman_client(self):
        with self._api_client_lock:
            if self._api_client is not None:
                try:
                    self._api_client.ping()
                    return self._api_client
                except Exception:
                    self._close_api_client()

            if not self.is_engine_ready():
                # Starting/recovering the service is deliberately owned by
                # auto_setup. A client lookup must never launch a second retry
                # loop of its own.
                return None

            url = self.get_api_service_url()
            if url is None:
                return None

            try:
                from podman import PodmanClient as NativePodmanClient  # type: ignore
                client = NativePodmanClient(base_url=url)
                client.version()
                self._api_client = client
                logger.info('[PodmanManager] Connected via Podman Python Bindings.')
                return self._api_client
            except ImportError:
                logger.info('[PodmanManager] podman Python package not available, using Docker SDK fallback.')
            except Exception as e:
                logger.info(f'[PodmanManager] Podman Python Bindings failed ({e}), using Docker SDK fallback.')

            try:
                import docker
                os.environ['DOCKER_HOST'] = url
                client = docker.DockerClient(
                    base_url=url,
                    version=DOCKER_API_VERSION,
                    timeout=15
                )
                client.ping()
                self._api_client = client
                logger.info('[PodmanManager] Connected via Docker SDK (Podman compatible).')
                return self._api_client
            except Exception as e:
                logger.error(f'[PodmanManager] Docker SDK fallback also failed: {e}')
                return None

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
        logger.info(f'[PodmanManager] Pulling image: {image_name}')
        if not self.is_engine_ready():
            logger.info('[PodmanManager] Engine not ready, attempting to start...')
            self.machine_start()
            if not self.is_engine_ready():
                return False, 'Podman engine is not running'
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
