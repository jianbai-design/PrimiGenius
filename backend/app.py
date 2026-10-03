import sys
import io
import os
import time
import tarfile
import json
import shutil
import zipfile
import socket
import ssl
import docker
import datetime
import traceback
import uuid
import platform
from flask import Flask, jsonify, request, send_from_directory, Response 
from flask_cors import CORS  # type: ignore
from werkzeug.serving import make_server
import threading
import subprocess
import tempfile
import re
import logging
import shlex
import queue
import mimetypes
from collections import deque
import urllib.request
import urllib.error
import urllib.parse
import contextlib
import hashlib
import functools
from podman_manager import (
    PodmanManager,
    PODMAN_API_HOST,
    PODMAN_API_PORT,
    DOCKER_API_VERSION,
    _SUBPROCESS_KWARGS as _PODMAN_SUBPROCESS_KWARGS,
    _run_hidden,
)

_HIDDEN_SUBPROCESS_KWARGS = dict(_PODMAN_SUBPROCESS_KWARGS)

# Use UTF-8 for Windows console output.
if os.name == 'nt':
    try:
        os.system('chcp 65001 >nul')
    except:
        pass

# 强制 Python 环境使用 UTF-8
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='ignore')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='ignore')
except: pass

if getattr(sys, 'frozen', False):
    # PyInstaller 打包后的 app.exe 运行
    # sys.executable 是 .../resources/backend/app.exe
    # APP_INSTALL_DIR 是主程序安装目录（PrimiGenius.exe 所在目录）
    BASE_DIR = os.path.dirname(sys.executable)
    IS_FROZEN = True
    APP_INSTALL_DIR = os.path.abspath(os.path.join(BASE_DIR, '..', '..'))
    PLUGINS_DIR = os.path.join(APP_INSTALL_DIR, 'plugins')
else:
    # 源码运行（开发环境）
    # BASE_DIR = backend/   APP_INSTALL_DIR = 项目根目录
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    IS_FROZEN = False
    APP_INSTALL_DIR = os.path.abspath(os.path.join(BASE_DIR, '..'))
    PLUGINS_DIR = os.path.join(APP_INSTALL_DIR, 'plugins')

app = Flask(__name__)
CORS(app)
logging.getLogger('werkzeug').setLevel(logging.ERROR)
app.logger.disabled = True


@app.after_request
def _add_low_latency_headers(response):
    if request.path.startswith('/logs/') or request.path.startswith('/podman/') or request.path.startswith('/r/install-status'):
        response.headers.setdefault('Cache-Control', 'no-cache, no-store, must-revalidate')
        response.headers.setdefault('Pragma', 'no-cache')
        response.headers.setdefault('X-Accel-Buffering', 'no')
    return response


class _FrontendLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            if not msg.startswith('[PodmanManager]'):
                msg = f'[PodmanManager] {msg}'
            if 'log' in globals():
                log(msg)
            else:
                print(msg, flush=True)
        except Exception:
            pass


_frontend_log_handler = _FrontendLogHandler()
_frontend_log_handler.setLevel(logging.INFO)
_frontend_log_handler.setFormatter(logging.Formatter('%(message)s'))

# 记录前端提交的运行 ID -> 容器 ID 映射
RUNNING_CONTAINERS = {}
_LOG_CONTEXT = threading.local()
_LOG_EVENT_COND = threading.Condition()
_LOG_EVENT_SEQ = 0
_LOG_EVENTS = deque(maxlen=20000)
_IPV4_PATCH_LOCK = threading.Lock()
_NETWORK_PROFILE_LOCK = threading.Lock()
_PULL_SOURCE_STATS_LOCK = threading.Lock()
_PULL_SOURCE_STATS = {}  # registry_host -> {'ok': bool, 'latency_ms': int, 'failures': int, 'ts': float}
_IMAGE_PULL_LOCK = threading.Lock()
_REGISTRY_PROBE_CACHE_LOCK = threading.Lock()
_REGISTRY_PROBE_CACHE = {}  # registry_host -> {'latency_ms': int|None, 'ts': float}
_HUB_MANIFEST_CHECK_CACHE_LOCK = threading.Lock()
_HUB_MANIFEST_CHECK_CACHE = {}  # key: namespace/repo:tag -> {'ok': bool, 'reason': str, 'ts': float}
_BINARY_INSTALL_JOBS_LOCK = threading.Lock()
_BINARY_INSTALL_JOBS = {}
_ACTIVE_ANALYSIS_LOCK = threading.Lock()
_ACTIVE_ANALYSIS_REQUESTS = 0
_CONTAINER_LAST_STATS_LOCK = threading.Lock()
_CONTAINER_LAST_STATS = {}  # container_id -> {'stats': {...}, 'ts': float}


def _track_analysis_request(func):
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        global _ACTIVE_ANALYSIS_REQUESTS
        with _ACTIVE_ANALYSIS_LOCK:
            _ACTIVE_ANALYSIS_REQUESTS += 1
        try:
            return func(*args, **kwargs)
        finally:
            with _ACTIVE_ANALYSIS_LOCK:
                _ACTIVE_ANALYSIS_REQUESTS = max(0, _ACTIVE_ANALYSIS_REQUESTS - 1)
    return wrapped


def _podman_suspend_busy_state():
    with _ACTIVE_ANALYSIS_LOCK:
        analyses = _ACTIVE_ANALYSIS_REQUESTS
    manager = globals().get('_podman_mgr')
    r_installs = sum(
        1 for job in globals().get('INSTALL_JOBS', {}).values()
        if job.get('status') in ('pending', 'running')
    )
    with _BINARY_INSTALL_JOBS_LOCK:
        binary_installs = sum(
            1 for job in _BINARY_INSTALL_JOBS.values()
            if job.get('status') in ('pending', 'running')
        )
    return {
        'analyses': analyses,
        'image_pulls': 1 if globals().get('_pull_in_progress') and _pull_in_progress.is_set() else 0,
        'r_installs': r_installs,
        'binary_installs': binary_installs,
        'podman_setup': 1 if (
            globals().get('_podman_async_running') or getattr(manager, '_setup_running', False)
        ) else 0,
    }

_CUSTOM_MIRRORS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.primigenius_custom_mirrors.json')
_CUSTOM_MIRRORS_LOCK = threading.Lock()

DEFAULT_DOCKERHUB_MIRRORS = [
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

DEFAULT_REGISTRY_PROXIES = {
    'quay.io': [
        'quay.mirrors.ustc.edu.cn',
        'quay.nju.edu.cn',
        'quay.mirrors.sjtug.sjtu.edu.cn',
    ],
    'ghcr.io': [
        'ghcr.mirrors.ustc.edu.cn',
        'ghcr.nju.edu.cn',
        'ghcr.mirrors.sjtug.sjtu.edu.cn',
    ],
    'gcr.io': [
        'gcr.mirrors.ustc.edu.cn',
        'gcr.nju.edu.cn',
        'gcr.mirrors.sjtug.sjtu.edu.cn',
    ],
    'k8s.gcr.io': [
        'k8s-gcr.mirrors.ustc.edu.cn',
    ],
    'registry.k8s.io': [
        'k8s.m.daocloud.io',
        'k8s.mirrors.ustc.edu.cn',
        'k8s.nju.edu.cn',
        'k8s.mirrors.sjtug.sjtu.edu.cn',
    ],
}

HIDDEN_DOCKER_ACCEL_PREFIXES = [
    'docker.gh-proxy.cn',
    'gh-proxy.org/docker',
    'v4.gh-proxy.org/docker',
    'v6.gh-proxy.org/docker',
    'cdn.gh-proxy.org/docker',
]

HIDDEN_DOCKER_ACCEL_PUBLIC_REGISTRIES = {
    'docker.io',
    'registry-1.docker.io',
    'index.docker.io',
    'registry.hub.docker.com',
    'gcr.io',
    'ghcr.io',
    'registry.k8s.io',
    'k8s.gcr.io',
    'quay.io',
    'mcr.microsoft.com',
    'docker.elastic.co',
    'nvcr.io',
    'public.ecr.aws',
    'registry.gitlab.com',
    'registry.access.redhat.com',
    'container-registry.oracle.com',
    'registry.suse.com',
    'registry.opensuse.org',
}

_READONLY_CLIENT_TIMEOUT = 2

# Critical image shortcuts: always surface quickly and allow pull fallback even if preflight is unstable.
PRIORITY_IMAGE_NAMES = {
    'rocker/r-ver',
    'library/rocker/r-ver'
}

NETWORK_PROFILE_FILE = os.path.join(APP_INSTALL_DIR, '.primigenius_network_profile.json')
NETWORK_MODE_STRATEGIES = {
    'auto': {
        'search_sources': ['engine', 'hub', 'index', 'offline'],
        'prefer_ipv4': True,
        'search_timeouts': [3.0, 5.0, 8.0],
        'pull_mirrors': DEFAULT_DOCKERHUB_MIRRORS,
        'dockerhub_first': True
    },
    'cn': {
        'search_sources': ['hub', 'offline', 'index', 'engine'],
        'prefer_ipv4': True,
        'search_timeouts': [1.2, 2.5],
        'pull_mirrors': DEFAULT_DOCKERHUB_MIRRORS,
        'dockerhub_first': False
    },
    'global': {
        'search_sources': ['engine', 'hub', 'index', 'offline'],
        'prefer_ipv4': False,
        'search_timeouts': [4.0, 8.0, 12.0],
        'pull_mirrors': [],
        'dockerhub_first': True
    }
}


def _safe_network_mode(mode):
    m = str(mode or '').strip().lower()
    return m if m in NETWORK_MODE_STRATEGIES else 'auto'


def _load_network_profile():
    mode = 'auto'
    try:
        if os.path.exists(NETWORK_PROFILE_FILE):
            with open(NETWORK_PROFILE_FILE, 'r', encoding='utf-8') as f:
                payload = json.load(f)
            mode = _safe_network_mode((payload or {}).get('mode'))
    except Exception as e:
        log(f'[WARN] load network profile failed: {e}')
    return {'mode': mode}


def _save_network_profile(mode):
    mode = _safe_network_mode(mode)
    with _NETWORK_PROFILE_LOCK:
        os.makedirs(os.path.dirname(NETWORK_PROFILE_FILE), exist_ok=True)
        with open(NETWORK_PROFILE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'mode': mode, 'updated_at': int(time.time())}, f, ensure_ascii=False, indent=2)
    return {'mode': mode}


def _effective_network_mode():
    raw = os.environ.get('PRIMIGENIUS_NETWORK_MODE', '').strip().lower()
    if raw in NETWORK_MODE_STRATEGIES:
        return raw
    return _load_network_profile().get('mode', 'auto')


def _mode_strategy():
    return NETWORK_MODE_STRATEGIES.get(_effective_network_mode(), NETWORK_MODE_STRATEGIES['auto'])


def _normalize_image_name(name):
    """Normalize image name by stripping registry/tag/digest and lowercasing."""
    raw = (name or '').strip().lower()
    if not raw:
        return ''
    raw = raw.split('@', 1)[0]
    if raw.startswith('docker.io/'):
        raw = raw[len('docker.io/'):]
    elif raw.startswith('index.docker.io/'):
        raw = raw[len('index.docker.io/'):]
    elif raw.startswith('registry-1.docker.io/'):
        raw = raw[len('registry-1.docker.io/'):]
    elif raw.startswith('registry.hub.docker.com/'):
        raw = raw[len('registry.hub.docker.com/'):]

    slash = raw.rfind('/')
    colon = raw.rfind(':')
    if colon > slash:
        raw = raw[:colon]

    if '/' not in raw:
        raw = f'library/{raw}'
    return raw


def _is_priority_image_name(name):
    normalized = _normalize_image_name(name)
    return normalized in PRIORITY_IMAGE_NAMES


def _build_priority_candidates(query, limit=25):
    q = (query or '').strip().lower()
    if not q:
        return []

    is_rocker_hint = ('rocker' in q) or ('r-ver' in q)
    normalized_q = _normalize_image_name(q)
    if normalized_q in PRIORITY_IMAGE_NAMES:
        is_rocker_hint = True

    if not is_rocker_hint:
        return []

    # Critical system images must keep the version+digest contract all the way
    # from search result to detail lookup and pull.
    candidates = [_resolve_system_image_ref('rocker/r-ver')]
    out = []
    for nm in candidates[:int(limit)]:
        out.append({
            'name': nm,
            'description': 'Priority R runtime image (fast-path candidate).',
            'star_count': 0,
            'is_official': False,
            'is_automated': False
        })
    return out

def log(msg):
    run_id = getattr(_LOG_CONTEXT, 'run_id', None)
    channel = getattr(_LOG_CONTEXT, 'channel', 'Linux')
    run_prefix = f"[RUN:{run_id}] " if run_id else ""
    text = str(msg)
    for line in text.splitlines() or ['']:
        print(f"{run_prefix}[{channel}] {line}", flush=True)
        if run_id:
            _publish_run_log_event(str(run_id), str(channel), line)


logging.getLogger('podman_manager').addHandler(_frontend_log_handler)
logging.getLogger('podman_manager').setLevel(logging.INFO)
logging.getLogger('podman_manager').propagate = False


def _publish_run_log_event(run_id, channel, msg):
    global _LOG_EVENT_SEQ
    if not run_id:
        return
    with _LOG_EVENT_COND:
        for line in str(msg).splitlines() or ['']:
            _LOG_EVENT_SEQ += 1
            _LOG_EVENTS.append({
                'seq': _LOG_EVENT_SEQ,
                'runId': run_id,
                'channel': channel or 'Linux',
                'line': f'[{channel or "Linux"}] {line}',
                'ts': time.time()
            })
        _LOG_EVENT_COND.notify_all()


@app.route('/logs/stream', methods=['GET'])
def stream_run_logs():
    run_id = str(request.args.get('runId') or '').strip()
    if not run_id:
        return jsonify({'status': 'error', 'message': 'runId is required'}), 400
    try:
        last_seq = int(request.args.get('lastSeq') or 0)
    except Exception:
        last_seq = 0

    def generate():
        nonlocal last_seq
        yield f"data: {json.dumps({'type': 'hello', 'runId': run_id})}\n\n"
        while True:
            batch = []
            with _LOG_EVENT_COND:
                for event in list(_LOG_EVENTS):
                    if event.get('runId') == run_id and int(event.get('seq') or 0) > last_seq:
                        batch.append(event)
                if not batch:
                    _LOG_EVENT_COND.wait(timeout=3)
                    for event in list(_LOG_EVENTS):
                        if event.get('runId') == run_id and int(event.get('seq') or 0) > last_seq:
                            batch.append(event)

            if not batch:
                yield f"data: {json.dumps({'type': 'heartbeat', 'runId': run_id})}\n\n"
                continue

            for event in batch:
                last_seq = max(last_seq, int(event.get('seq') or 0))
                payload = {
                    'type': 'log',
                    'seq': last_seq,
                    'runId': run_id,
                    'channel': event.get('channel') or 'Linux',
                    'line': event.get('line') or ''
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


def _parse_image_ref(image_ref):
    """Split image ref into (name, tag, digest)."""
    ref = (image_ref or '').strip()
    digest = None
    tag = None
    if '@' in ref:
        ref, digest = ref.split('@', 1)

    name = ref
    slash = name.rfind('/')
    colon = name.rfind(':')
    if colon > slash:
        tag = name[colon + 1:]
        name = name[:colon]
    return name, tag, digest


def _dockerhub_repo_path(image_ref):
    """
    Return dockerhub-style repo path with tag/digest for mirror prefixing.
    Returns None for non-dockerhub explicit registries.
    """
    name, tag, digest = _parse_image_ref(image_ref)
    if not name:
        return None

    parts = name.split('/')
    first = parts[0] if parts else ''
    explicit_registry = ('.' in first) or (':' in first) or (first == 'localhost')

    if explicit_registry and first not in ('docker.io', 'registry-1.docker.io', 'index.docker.io', 'registry.hub.docker.com'):
        return None

    if explicit_registry:
        repo = '/'.join(parts[1:])
    else:
        repo = name

    if '/' not in repo:
        repo = f'library/{repo}'

    if digest:
        return f'{repo}@{digest}'
    if tag:
        return f'{repo}:{tag}'
    return f'{repo}:latest'


def _clean_hidden_docker_accel_prefix(prefix):
    value = str(prefix or '').strip().rstrip('/')
    value = value.removeprefix('https://').removeprefix('http://').strip('/')
    return value


def _hidden_docker_accel_prefixes():
    raw = os.environ.get('PRIMIGENIUS_HIDDEN_DOCKER_ACCEL_PREFIXES', '')
    env_items = [x for x in raw.split(',') if x.strip()]
    merged = []
    for item in list(HIDDEN_DOCKER_ACCEL_PREFIXES) + env_items:
        clean = _clean_hidden_docker_accel_prefix(item)
        if clean and clean not in merged:
            merged.append(clean)
    return merged


def _hidden_docker_accel_rank(image_ref):
    name, _tag, _digest = _parse_image_ref(image_ref)
    normalized = str(name or '').strip().lower()
    for idx, prefix in enumerate(_hidden_docker_accel_prefixes()):
        p = prefix.lower()
        if normalized == p or normalized.startswith(p + '/'):
            return idx
    return None


def _is_hidden_docker_accel_ref(image_ref):
    return _hidden_docker_accel_rank(image_ref) is not None


def _explicit_registry_for_image_ref(image_ref):
    name, _tag, _digest = _parse_image_ref(image_ref)
    first = (name.split('/')[0] if name else '').lower()
    if first and ('.' in first or ':' in first or first == 'localhost'):
        return first
    return ''


def _image_ref_with_default_tag(image_ref):
    name, tag, digest = _parse_image_ref(image_ref)
    if not name:
        return ''
    if digest:
        return f'{name}@{digest}'
    return f'{name}:{tag or "latest"}'


def _hidden_docker_accel_allowed(image_ref):
    if _env_flag('PRIMIGENIUS_DISABLE_HIDDEN_DOCKER_ACCEL', False):
        return False
    if _effective_network_mode() == 'global' and not _env_flag('PRIMIGENIUS_ENABLE_HIDDEN_DOCKER_ACCEL_GLOBAL', False):
        return False
    if _is_hidden_docker_accel_ref(image_ref):
        return False
    if _dockerhub_repo_path(image_ref):
        return True
    registry = _explicit_registry_for_image_ref(image_ref)
    return registry in HIDDEN_DOCKER_ACCEL_PUBLIC_REGISTRIES


def _hidden_docker_accel_candidates(image_ref):
    if not _hidden_docker_accel_allowed(image_ref):
        return []

    candidates = []
    repo_path = _dockerhub_repo_path(image_ref)
    def add(candidate):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for prefix in _hidden_docker_accel_prefixes():
        host_style = '/' not in prefix
        if repo_path:
            if host_style:
                add(f'{prefix}/{repo_path}')
            else:
                add(f'{prefix}/docker.io/{repo_path}')
        else:
            full_ref = _image_ref_with_default_tag(image_ref)
            if full_ref:
                add(f'{prefix}/{full_ref}')
    return candidates


def _load_custom_mirrors():
    try:
        with _CUSTOM_MIRRORS_LOCK:
            if os.path.exists(_CUSTOM_MIRRORS_FILE):
                with open(_CUSTOM_MIRRORS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data.get('mirrors', [])
    except Exception:
        pass
    return []


def _save_custom_mirrors(mirrors):
    with _CUSTOM_MIRRORS_LOCK:
        with open(_CUSTOM_MIRRORS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'mirrors': mirrors, 'updated_at': int(time.time())}, f, ensure_ascii=False, indent=2)


def _get_pull_mirrors():
    def clean(m):
        return str(m or '').strip().rstrip('/').removeprefix('https://').removeprefix('http://')

    custom = _load_custom_mirrors()
    custom = [clean(x) for x in custom if clean(x)]
    if custom:
        return custom

    raw = os.environ.get('PRIMIGENIUS_DOCKER_MIRRORS', '')
    env_mirrors = [clean(x) for x in raw.split(',') if clean(x)]
    if env_mirrors:
        return env_mirrors

    try:
        configured = _get_podman_manager()._get_configured_mirrors()
        configured = [clean(x) for x in configured if clean(x)]
        if configured:
            return configured
    except Exception as e:
        log(f'[WARN] Failed to read configured Podman mirrors: {e}')

    return list(_mode_strategy().get('pull_mirrors', DEFAULT_DOCKERHUB_MIRRORS))


def _get_registry_proxies(registry):
    registry = str(registry or '').strip().lower()
    defaults = list(DEFAULT_REGISTRY_PROXIES.get(registry, []))
    env_name = 'PRIMIGENIUS_' + re.sub(r'[^A-Z0-9]+', '_', registry.upper()).strip('_') + '_MIRRORS'
    raw = os.environ.get(env_name, '')
    env_items = [
        x.strip().rstrip('/').removeprefix('https://').removeprefix('http://')
        for x in raw.split(',')
        if x.strip()
    ]
    merged = []
    for item in env_items + defaults:
        if item and item not in merged:
            merged.append(item)
    return merged


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def _env_list(name, default_items):
    raw = os.environ.get(name, '')
    if not raw.strip():
        return list(default_items)
    return [x.strip() for x in raw.split(',') if x.strip()]


def _search_timeouts():
    raw = os.environ.get('PRIMIGENIUS_SEARCH_TIMEOUTS', '').strip()
    if not raw:
        return list(_mode_strategy().get('search_timeouts', [3.0, 5.0, 8.0]))
    vals = []
    for p in raw.split(','):
        p = p.strip()
        if not p:
            continue
        try:
            vals.append(max(0.8, float(p)))
        except Exception:
            continue
    return vals or [3.0, 5.0, 8.0]


def _effective_prefer_ipv4():
    raw = os.environ.get('PRIMIGENIUS_SEARCH_PREFER_IPV4')
    if raw is not None:
        return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(_mode_strategy().get('prefer_ipv4', True))


def _effective_search_sources():
    raw = os.environ.get('PRIMIGENIUS_SEARCH_SOURCES', '')
    if raw.strip():
        return _env_list('PRIMIGENIUS_SEARCH_SOURCES', ['engine', 'hub', 'index', 'offline'])
    return list(_mode_strategy().get('search_sources', ['engine', 'hub', 'index', 'offline']))


def _podman_api_version(mgr):
    raw = str(mgr.get_podman_version() or '').strip()
    match = re.search(r'(\d+\.\d+(?:\.\d+)?)', raw)
    if match:
        return f'v{match.group(1)}'
    if raw.startswith('v') and re.search(r'\d+\.\d+', raw):
        return raw.split()[0]
    return 'v5.0.0'


@contextlib.contextmanager
def _prefer_ipv4_dns(enabled):
    """Temporarily prioritize IPv4 address results for urllib DNS resolution."""
    if not enabled:
        yield
        return

    with _IPV4_PATCH_LOCK:
        original_getaddrinfo = socket.getaddrinfo

        def _v4_first_getaddrinfo(*args, **kwargs):
            infos = original_getaddrinfo(*args, **kwargs)
            try:
                infos = sorted(
                    infos,
                    key=lambda x: 0 if x[0] == socket.AF_INET else (1 if x[0] == socket.AF_INET6 else 2)
                )
            except Exception:
                pass
            return infos

        socket.getaddrinfo = _v4_first_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


def _build_pull_candidates(image_ref):
    """Build pull candidates respecting network mode and user configuration.
    
    Strategy:
    - auto mode: Docker Hub first, then mirrors as fallback
    - cn mode: mirrors first (faster in China), then Docker Hub as fallback
    - global mode: Docker Hub only, no mirrors
    - If user configured custom mirrors: custom mirrors first, then default mirrors, then Docker Hub
    - For non-Docker Hub registries (quay.io, gcr.io, etc.):
      1. Try original registry directly
      2. Try registry-specific proxies (e.g., quay.mirrors)
      3. Try Docker Hub mirrors with the repo path stripped of registry prefix (fuzzy match)
         e.g., quay.io/biocontainers/gffread -> try biocontainers/gffread on Docker Hub mirrors
    """
    candidates = []
    repo_path = _dockerhub_repo_path(image_ref)
    strategy = _mode_strategy()
    dockerhub_first = strategy.get('dockerhub_first', True)
    custom_mirrors = _load_custom_mirrors()
    has_custom = bool([m for m in custom_mirrors if m.strip()])
    hidden_accel_candidates = _hidden_docker_accel_candidates(image_ref)

    if repo_path:
        if has_custom:
            for mirror in _get_pull_mirrors():
                cand = f"{mirror}/{repo_path}"
                if cand not in candidates:
                    candidates.append(cand)
            for cand in hidden_accel_candidates:
                if cand not in candidates:
                    candidates.append(cand)
            if image_ref not in candidates:
                candidates.append(image_ref)
            for direct in (f'docker.io/{repo_path}', f'registry-1.docker.io/{repo_path}'):
                if direct not in candidates:
                    candidates.append(direct)
        elif dockerhub_first:
            candidates.append(image_ref)
            for direct in (f'docker.io/{repo_path}', f'registry-1.docker.io/{repo_path}', f'registry.hub.docker.com/{repo_path}'):
                if direct not in candidates:
                    candidates.append(direct)
            for cand in hidden_accel_candidates:
                if cand not in candidates:
                    candidates.append(cand)
            for mirror in _get_pull_mirrors():
                cand = f"{mirror}/{repo_path}"
                if cand not in candidates:
                    candidates.append(cand)
        else:
            for mirror in _get_pull_mirrors():
                cand = f"{mirror}/{repo_path}"
                if cand not in candidates:
                    candidates.append(cand)
            for cand in hidden_accel_candidates:
                if cand not in candidates:
                    candidates.append(cand)
            if image_ref not in candidates:
                candidates.append(image_ref)
            for direct in (f'docker.io/{repo_path}', f'registry-1.docker.io/{repo_path}'):
                if direct not in candidates:
                    candidates.append(direct)
        return candidates

    name, tag, digest = _parse_image_ref(image_ref)
    if not name:
        return [image_ref]

    parts = name.split('/')
    first = parts[0] if parts else ''
    explicit_registry = ('.' in first) or (':' in first) or (first == 'localhost')

    if not explicit_registry:
        return [image_ref]

    registry = first
    repo = '/'.join(parts[1:])
    if not repo:
        return [image_ref]

    tag_suffix = f':{tag}' if tag else (f'@{digest}' if digest else ':latest')
    full_repo = f'{repo}{tag_suffix}'

    candidates.append(image_ref)

    registry_proxies = _get_registry_proxies(registry)
    for proxy in registry_proxies:
        cand = f"{proxy}/{full_repo}"
        if cand not in candidates:
            candidates.append(cand)

    for cand in hidden_accel_candidates:
        if cand not in candidates:
            candidates.append(cand)

    dh_repo_path = repo
    dh_candidates = []
    for direct in (dh_repo_path, f'docker.io/{dh_repo_path}', f'registry-1.docker.io/{dh_repo_path}'):
        cand = f"{direct}{tag_suffix}"
        if cand not in candidates:
            dh_candidates.append(cand)
    for mirror in _get_pull_mirrors():
        cand = f"{mirror}/{dh_repo_path}{tag_suffix}"
        if cand not in candidates and cand not in dh_candidates:
            dh_candidates.append(cand)
    candidates.extend(dh_candidates)

    return candidates


def _has_explicit_registry(image_ref):
    name, _tag, _digest = _parse_image_ref(image_ref)
    first = (name.split('/')[0] if name else '')
    return bool(first and ('.' in first or ':' in first or first == 'localhost'))


def _pull_registry_host(image_ref):
    """Infer registry host for a concrete image reference."""
    name, _tag, _digest = _parse_image_ref(image_ref)
    if not name:
        return 'registry-1.docker.io'

    first = name.split('/')[0]
    explicit_registry = ('.' in first) or (':' in first) or (first == 'localhost')
    if explicit_registry:
        if first in ('docker.io', 'registry-1.docker.io', 'index.docker.io'):
            return 'registry-1.docker.io'
        if first == 'registry.hub.docker.com':
            return 'registry.hub.docker.com'
        return first
    return 'registry-1.docker.io'


def _configured_pull_mirror_hosts():
    hosts = set()
    for mirror in _get_pull_mirrors():
        mirror = str(mirror or '').strip().removeprefix('https://').removeprefix('http://').strip('/')
        if mirror:
            hosts.add(mirror.split('/')[0])
    return hosts


def _local_image_ref_variants(image_ref):
    """Generate refs Podman may use locally for one Docker Hub image."""
    refs = []

    def add(ref):
        ref = str(ref or '').strip()
        if ref and ref not in refs:
            refs.append(ref)

    add(image_ref)
    repo_path = _dockerhub_repo_path(image_ref)
    if repo_path:
        add(repo_path)
        add(f'docker.io/{repo_path}')
        add(f'registry-1.docker.io/{repo_path}')
        add(f'registry.hub.docker.com/{repo_path}')
        if repo_path.startswith('library/'):
            add(repo_path[len('library/'):])

    name, tag, digest = _parse_image_ref(image_ref)
    if name:
        add(f'{name}:{tag or "latest"}')
    return refs


def _image_exists_locally(client, image_ref):
    """Return True only when the image can be resolved from local storage."""
    refs = _local_image_ref_variants(image_ref)
    for ref in refs:
        try:
            client.images.get(ref)
            return True
        except Exception:
            pass

    try:
        mgr = _get_podman_manager()
        # A missing image is the slow path. Check every local alias in one WSL
        # process instead of paying WSL startup cost once per alias.
        checks = ' || '.join(f'podman image exists {shlex.quote(ref)}' for ref in refs)
        if checks:
            result = mgr._run_wsl(checks, timeout=20)
            if result and result.returncode == 0:
                return True
    except Exception:
        pass

    return False


def _ensure_image_exists_after_pull(client, pulled_ref, original_ref):
    """Guard against APIs that return success before an image is committed locally."""
    check_refs = []
    for ref in (original_ref, pulled_ref):
        for variant in _local_image_ref_variants(ref):
            if variant not in check_refs:
                check_refs.append(variant)

    for ref in check_refs:
        if _image_exists_locally(client, ref):
            return True

    raise RuntimeError(
        'pull finished but image was not found locally; checked: ' +
        ', '.join(check_refs[:8])
    )


def _probe_registry_latency(host, timeout_sec=2.5):
    """Probe the registry HTTPS endpoint; return usable latency or ``None``.

    A TCP-only probe incorrectly treated registries with broken TLS as healthy,
    sending multi-gigabyte pulls to sources that could never complete.
    Registry v2 endpoints normally answer with either 200 or 401.
    """
    if not host:
        return None
    now = time.time()
    with _REGISTRY_PROBE_CACHE_LOCK:
        cached = _REGISTRY_PROBE_CACHE.get(host)
        if cached and now - float(cached.get('ts') or 0.0) < 300:
            return cached.get('latency_ms')
    t0 = time.perf_counter()
    latency = None
    try:
        addresses = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        if not addresses:
            raise OSError(f'No IPv4 address for registry {host}')
        ip = addresses[0][4][0]
        raw_sock = socket.create_connection((ip, 443), timeout=timeout_sec)
        try:
            raw_sock.settimeout(timeout_sec)
            context = ssl.create_default_context()
            with context.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
                request_bytes = (
                    f'GET /v2/ HTTP/1.1\r\nHost: {host}\r\n'
                    'User-Agent: PrimiGenius/2.0\r\nConnection: close\r\n\r\n'
                ).encode('ascii')
                tls_sock.sendall(request_bytes)
                response_head = b''
                while b'\r\n' not in response_head and len(response_head) < 4096:
                    chunk = tls_sock.recv(512)
                    if not chunk:
                        break
                    response_head += chunk
            status_line = response_head.split(b'\r\n', 1)[0].decode('ascii', errors='replace')
            match = re.match(r'^HTTP/\d(?:\.\d)?\s+(\d{3})\b', status_line)
            status = int(match.group(1)) if match else 0
            if status in (200, 401):
                latency = int((time.perf_counter() - t0) * 1000)
        finally:
            try:
                raw_sock.close()
            except Exception:
                pass
    except Exception:
        latency = None
    with _REGISTRY_PROBE_CACHE_LOCK:
        _REGISTRY_PROBE_CACHE[host] = {'latency_ms': latency, 'ts': time.time()}
    return latency


def _order_pull_candidates(image_ref, candidates):
    """Order pull refs by mode bias + recent success cache + quick connectivity probes."""
    uniq = []
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        uniq.append(c)

    if len(uniq) <= 1:
        return uniq

    mode = _effective_network_mode()
    now = time.time()
    configured_hosts = _configured_pull_mirror_hosts()
    has_configured_mirrors = bool(configured_hosts)
    custom_mirror_hosts = set()
    for cm in _load_custom_mirrors():
        cm_clean = cm.strip().rstrip('/').removeprefix('https://').removeprefix('http://')
        if cm_clean:
            custom_mirror_hosts.add(cm_clean)

    with _PULL_SOURCE_STATS_LOCK:
        stats_snapshot = dict(_PULL_SOURCE_STATS)

    recent_ok_host = None
    for host, st in stats_snapshot.items():
        if st.get('ok') and (now - float(st.get('ts') or 0.0)) < 3600:
            if recent_ok_host is None or float(st.get('latency_ms', 999999)) < float(stats_snapshot.get(recent_ok_host, {}).get('latency_ms', 999999)):
                recent_ok_host = host

    host_latency = {}
    skip_probes = False
    if recent_ok_host:
        for c in uniq:
            h = _pull_registry_host(c)
            if h == recent_ok_host:
                skip_probes = True
                break
    if not skip_probes:
        unique_hosts = []
        for c in uniq:
            h = _pull_registry_host(c)
            if h not in unique_hosts:
                unique_hosts.append(h)
        if len(unique_hosts) > 1:
            probe_results = {}
            def _probe_host(h):
                probe_results[h] = _probe_registry_latency(h, timeout_sec=2.5)
            probe_threads = []
            for h in unique_hosts[:16]:
                t = threading.Thread(target=_probe_host, args=(h,), daemon=True)
                t.start()
                probe_threads.append(t)
            probe_deadline = time.monotonic() + 3.5
            for t in probe_threads:
                t.join(timeout=max(0.0, probe_deadline - time.monotonic()))
            host_latency.update(probe_results)
        else:
            for h in unique_hosts:
                host_latency[h] = _probe_registry_latency(h, timeout_sec=2.5)

    filtered = []
    for c in uniq:
        host = _pull_registry_host(c)
        is_dockerhub_direct = host in ('registry-1.docker.io', 'registry.hub.docker.com', 'docker.io')
        is_custom = host in custom_mirror_hosts
        is_hidden_accel = _is_hidden_docker_accel_ref(c)
        if mode == 'cn' and is_dockerhub_direct and c != image_ref and not is_custom:
            continue
        hidden_accel_global_enabled = _env_flag('PRIMIGENIUS_ENABLE_HIDDEN_DOCKER_ACCEL_GLOBAL', False)
        if mode == 'global' and is_hidden_accel and not hidden_accel_global_enabled:
            continue
        if mode == 'global' and not is_dockerhub_direct and c != image_ref and host not in configured_hosts and not is_custom and not (is_hidden_accel and hidden_accel_global_enabled):
            continue
        filtered.append(c)
    if not filtered:
        filtered = list(uniq)

    priority_pull = _is_priority_image_name(image_ref)
    scored = []
    for idx, c in enumerate(filtered):
        host = _pull_registry_host(c)
        score = 100.0 - float(idx)
        is_original = (c == image_ref)
        is_explicit = _has_explicit_registry(c)
        is_dockerhub_direct = host in ('registry-1.docker.io', 'registry.hub.docker.com')
        hidden_accel_rank = _hidden_docker_accel_rank(c)
        is_hidden_accel = hidden_accel_rank is not None

        if host in configured_hosts:
            score += 360.0

        if host in custom_mirror_hosts:
            score += 500.0

        if is_hidden_accel:
            rank_penalty = float(hidden_accel_rank or 0) * 75.0
            if mode == 'cn':
                score += max(0.0, 390.0 - rank_penalty)
            elif mode == 'auto':
                score -= 120.0 + rank_penalty
            else:
                score -= 300.0

        if is_original and has_configured_mirrors and not is_explicit:
            score += 260.0

        if has_configured_mirrors and is_dockerhub_direct and not is_original:
            score -= 260.0

        if host == 'registry.hub.docker.com':
            score -= 420.0

        probe_ms = host_latency.get(host)
        if isinstance(probe_ms, (int, float)):
            score += max(0.0, 35.0 - min(float(probe_ms), 35.0))
        elif skip_probes:
            pass
        else:
            # Keep an unreachable source as a last-resort fallback, but never
            # rank it ahead of a registry that completed TLS + /v2/ preflight.
            score -= 800.0

        st = stats_snapshot.get(host) or {}
        if st.get('ok'):
            score += 45.0
            age = now - float(st.get('ts') or 0.0)
            if age < 1800:
                score += 20.0
            if age < 600:
                score += 40.0

        failures = int(st.get('failures') or 0)
        score -= min(42.0, failures * 7.0)

        cached_ms = st.get('latency_ms')
        if isinstance(cached_ms, (int, float)):
            score += max(0.0, 90.0 - min(float(cached_ms), 90.0))

        if mode == 'cn':
            if is_dockerhub_direct and not is_original:
                score -= 160.0
            else:
                score += 70.0
        elif mode == 'auto':
            if has_configured_mirrors and is_dockerhub_direct and not is_original:
                score -= 120.0
        elif mode == 'global':
            if host == 'registry-1.docker.io' and not has_configured_mirrors:
                score += 30.0

        if priority_pull:
            if is_dockerhub_direct and not is_original:
                score -= 45.0
            else:
                score += 85.0
            # For the large pinned R image, favor a source that completed the
            # live registry probe instead of the static mirror-list position.
            if host in configured_hosts and host not in custom_mirror_hosts:
                score -= 280.0
            if is_original and has_configured_mirrors and not is_explicit:
                score -= 180.0
            if mode == 'auto' and is_hidden_accel:
                score += 260.0
            if isinstance(probe_ms, (int, float)):
                score += max(0.0, 350.0 - min(float(probe_ms), 350.0))
            if st.get('ok') and isinstance(cached_ms, (int, float)):
                score += max(0.0, 240.0 - min(float(cached_ms) / 1000.0, 240.0))

        scored.append((score, c, host, probe_ms))

    scored.sort(key=lambda x: x[0], reverse=True)
    ordered = []
    seen_dockerhub_direct = False
    for _score, candidate, host, _probe_ms in scored:
        is_dockerhub_direct = host in (
            'registry-1.docker.io', 'docker.io', 'index.docker.io'
        )
        if is_dockerhub_direct and seen_dockerhub_direct:
            continue
        ordered.append(candidate)
        if is_dockerhub_direct:
            seen_dockerhub_direct = True
    log('[DEBUG] Pull candidate order: ' + ' -> '.join(ordered))
    return ordered


def _note_pull_candidate_result(image_ref, ok, elapsed_ms):
    host = _pull_registry_host(image_ref)
    now = time.time()
    with _PULL_SOURCE_STATS_LOCK:
        prev = _PULL_SOURCE_STATS.get(host) or {'ok': False, 'latency_ms': 0, 'failures': 0, 'ts': 0}
        next_failures = max(0, int(prev.get('failures') or 0) - 1) if ok else int(prev.get('failures') or 0) + 1
        _PULL_SOURCE_STATS[host] = {
            'ok': bool(ok),
            'latency_ms': int(max(0, elapsed_ms or 0)),
            'failures': next_failures,
            'ts': now
        }


def _pull_single_ref_with_progress(client, target_ref, *, display_name=None, cancel_event=None, source_label=None):
    """Pull one concrete image ref, streaming native output directly. Prefers WSL CLI."""
    shown_name = display_name or target_ref
    src_tag = f'[{source_label}] ' if source_label else ''
    log(f"📥 {src_tag}Pulling {shown_name} ...")

    pulled_ok = False
    try:
        mgr = _get_podman_manager()
        priority_pull = _is_priority_image_name(display_name or target_ref)
        priority_retries = None
        priority_timeout = 21600
        if priority_pull:
            try:
                priority_retries = max(0, min(int(os.environ.get('PRIMIGENIUS_R_PULL_RETRIES', '2')), 6))
            except (TypeError, ValueError):
                priority_retries = 2
            try:
                priority_timeout = max(300, min(int(os.environ.get('PRIMIGENIUS_R_PULL_SOURCE_TIMEOUT', '1800')), 21600))
            except (TypeError, ValueError):
                priority_timeout = 1800
        direct_dockerhub = _pull_registry_host(target_ref) == 'registry-1.docker.io'
        pull_ref = target_ref
        if direct_dockerhub:
            repo_path = _dockerhub_repo_path(target_ref)
            if repo_path:
                pull_ref = f'docker.io/{repo_path}'
        for line in mgr.pull_image_stream_wsl(
            pull_ref,
            cancel_event=cancel_event,
            retry_count=priority_retries,
            total_timeout=priority_timeout,
            bypass_registry_mirrors=direct_dockerhub,
            serialize_layers=priority_pull,
        ):
            if cancel_event and cancel_event.is_set():
                return
            if line.startswith('ERROR:'):
                raise RuntimeError(line[6:].strip())
            l = line.strip()
            if not l:
                continue
            lower_l = l.lower()
            if 'error' in lower_l or 'denied' in lower_l or 'not in the allowlist' in lower_l:
                log(f'[DEBUG] {src_tag}{l}')
            else:
                log(f"📥 {src_tag}{l}")
        pulled_ok = True
        log(f"📥 {src_tag}Pull complete {shown_name}")
    except RuntimeError:
        raise
    except Exception as wsl_err:
        if cancel_event and cancel_event.is_set():
            return
        log(f'[DEBUG] {src_tag}WSL pull failed for {target_ref}: {wsl_err}, trying Libpod API')

    if not pulled_ok:
        try:
            mgr = _get_podman_manager()
            try:
                libpod_timeout = max(60, int(float(os.environ.get('PRIMIGENIUS_PULL_LIBPOD_TIMEOUT', '120'))))
            except Exception:
                libpod_timeout = 120
            if _pull_registry_host(target_ref) == 'registry.hub.docker.com':
                libpod_timeout = max(libpod_timeout, 60)

            podman_ver = _podman_api_version(mgr)
            import urllib.parse
            ref_encoded = urllib.parse.quote(target_ref, safe='')
            api_http_url = mgr.get_api_http_url() or f'http://{PODMAN_API_HOST}:{PODMAN_API_PORT}'
            pull_url = f'{api_http_url}/{podman_ver}/libpod/images/pull?reference={ref_encoded}'
            log(f'[DEBUG] {src_tag}Trying Libpod API pull ({libpod_timeout}s timeout)')
            req = urllib.request.Request(pull_url, method='POST')
            with urllib.request.urlopen(req, timeout=libpod_timeout) as resp:
                buf = ''
                for raw_chunk in iter(lambda: resp.read(4096), b''):
                    if cancel_event and cancel_event.is_set():
                        return
                    buf += raw_chunk.decode('utf-8', errors='replace')
                    while '\n' in buf:
                        line, buf = buf.split('\n', 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue
                        error_msg = chunk.get('error', '')
                        if error_msg:
                            raise RuntimeError(error_msg)
                        stream_msg = chunk.get('stream', '').strip()
                        if stream_msg:
                            log(f"📥 {src_tag}{stream_msg}")
            pulled_ok = True
            log(f"📥 {src_tag}Pull complete {shown_name}")
        except RuntimeError:
            raise
        except Exception as libpod_err:
            if cancel_event and cancel_event.is_set():
                return
            log(f'[DEBUG] {src_tag}Libpod API pull failed for {target_ref}: {libpod_err}, falling back to Docker API')

    if not pulled_ok:
        api = client.api
        for chunk in api.pull(target_ref, stream=True, decode=True):
            if cancel_event and cancel_event.is_set():
                return
            if not isinstance(chunk, dict):
                continue
            if chunk.get('error'):
                raise RuntimeError(str(chunk.get('error')))
            status = str(chunk.get('status') or '')
            layer_id = str(chunk.get('id') or '')
            progress = str(chunk.get('progress') or '')
            if status:
                msg = f"{status}"
                if layer_id:
                    msg = f"{layer_id}: {msg}"
                if progress:
                    msg = f"{msg} {progress}"
                log(f"📥 {src_tag}{msg}")
        pulled_ok = True
        log(f"📥 {src_tag}Pull complete {shown_name}")



def _cleanup_losing_mirror_tags(client, candidates, winner_ref, original_ref):
    """Remove mirror-tagged images left by losing parallel pull threads.
    Uses podman rmi with --no-prune to only remove the tag, not the layers.
    If the image still has other tags (like the original ref), only the alias is removed.
    Protects retag targets and all local image ref variants from deletion.
    """
    protected = set()
    protected.add(winner_ref)
    protected.add(original_ref)
    for ref in (winner_ref, original_ref):
        for variant in _local_image_ref_variants(ref):
            protected.add(variant)
    orig_name, orig_tag, orig_digest = _parse_image_ref(original_ref)
    if not orig_digest:
        tag_val = orig_tag or 'latest'
        fq_name = orig_name
        if '/' not in orig_name or ('.' not in orig_name.split('/')[0] and orig_name.split('/')[0] != 'localhost'):
            fq_name = f'docker.io/{orig_name}'
        protected.add(f'{fq_name}:{tag_val}')
        protected.add(f'{fq_name}')
    for cand in candidates:
        if cand in protected:
            continue
        try:
            _get_podman_manager()._run_wsl(f'podman rmi --no-prune {shlex.quote(cand)}', timeout=30)
        except Exception:
            pass


def _libpod_pull_image(image_ref, timeout=300):
    """Pull image preferring WSL podman pull, then Libpod API, then Docker API."""
    mgr = _get_podman_manager()
    try:
        for line in mgr.pull_image_stream_wsl(image_ref):
            if line.startswith('ERROR:'):
                raise RuntimeError(line[6:].strip())
        return True
    except RuntimeError:
        raise
    except Exception as wsl_err:
        log(f'[DEBUG] WSL pull failed for {image_ref}: {wsl_err}, trying Libpod API')
    try:
        podman_ver = _podman_api_version(mgr)
        import urllib.parse
        ref_encoded = urllib.parse.quote(image_ref, safe='')
        api_http_url = mgr.get_api_http_url() or f'http://{PODMAN_API_HOST}:{PODMAN_API_PORT}'
        pull_url = f'{api_http_url}/{podman_ver}/libpod/images/pull?reference={ref_encoded}'
        req = urllib.request.Request(pull_url, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buf = ''
            for raw_chunk in iter(lambda: resp.read(4096), b''):
                buf += raw_chunk.decode('utf-8', errors='replace')
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    error_msg = chunk.get('error', '')
                    if error_msg:
                        raise RuntimeError(error_msg)
        return True
    except RuntimeError:
        raise
    except Exception:
        return False

def _retag_to_original_if_needed(client, pulled_ref, original_ref):
    if pulled_ref == original_ref:
        return
    orig_name, orig_tag, orig_digest = _parse_image_ref(original_ref)
    if orig_digest:
        if pulled_ref == original_ref:
            return
        tag_val = orig_tag or 'latest'
        fq_name = orig_name
        if '/' not in orig_name or ('.' not in orig_name.split('/')[0] and orig_name.split('/')[0] != 'localhost'):
            fq_name = f'docker.io/{orig_name}'
        img_obj = client.images.get(pulled_ref)
        img_obj.tag(fq_name, tag=tag_val)
        log(f'[SYSTEM] Verified digest and tagged mirror image as {fq_name}:{tag_val}')
        try:
            _get_podman_manager()._run_wsl(f'podman rmi --no-prune {shlex.quote(pulled_ref)}', timeout=30)
            log(f'[SYSTEM] Removed accelerator alias after verified retag: {pulled_ref}')
        except Exception as e:
            log(f'[WARN] Failed to remove accelerator alias {pulled_ref}: {e}')
        return
    tag_val = orig_tag or 'latest'
    fq_name = orig_name
    if '/' not in orig_name or ('.' not in orig_name.split('/')[0] and orig_name.split('/')[0] != 'localhost'):
        fq_name = f'docker.io/{orig_name}'
    target_tag = f'{fq_name}:{tag_val}'
    is_dockerhub_variant = pulled_ref in (f'docker.io/{orig_name}', f'docker.io/{orig_name}:{tag_val}',
                                           f'registry-1.docker.io/{orig_name}', f'registry-1.docker.io/{orig_name}:{tag_val}',
                                           f'index.docker.io/{orig_name}', f'index.docker.io/{orig_name}:{tag_val}',
                                           target_tag)
    if is_dockerhub_variant:
        return
    try:
        img_obj = client.images.get(pulled_ref)
        img_obj.tag(fq_name, tag=tag_val)
    except Exception as e:
        log(f"[WARN] SDK retag failed for {pulled_ref} -> {target_tag}: {e}; trying WSL podman tag")
        result = _get_podman_manager()._run_wsl(
            f'podman tag {shlex.quote(pulled_ref)} {shlex.quote(target_tag)}',
            timeout=60
        )
        if result is None or result.returncode != 0:
            msg = (getattr(result, 'stderr', '') or getattr(result, 'stdout', '') or str(e)).strip()
            raise RuntimeError(f'failed to retag pulled image to {target_tag}: {msg}')
    log(f"[SYSTEM] Retagged {pulled_ref} -> {target_tag}")
    try:
        _get_podman_manager()._run_wsl(f'podman rmi --no-prune {shlex.quote(pulled_ref)}', timeout=30)
        log(f"[SYSTEM] Removed mirror tag: {pulled_ref}")
    except Exception as e:
        log(f"[WARN] Failed to remove mirror tag {pulled_ref}: {e}")


def _format_bytes(n):
    try:
        n = float(n or 0)
    except Exception:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.1f} {units[i]}"


def _parse_engine_dt(value):
    raw = str(value or '').strip()
    if not raw:
        return None
    raw = raw.replace('Z', '+00:00')
    try:
        return datetime.datetime.fromisoformat(raw)
    except Exception:
        return None


def _container_runtime_summary(container):
    attrs = getattr(container, 'attrs', {}) or {}
    state = attrs.get('State', {}) or {}
    started_at = _parse_engine_dt(state.get('StartedAt'))
    finished_at = _parse_engine_dt(state.get('FinishedAt'))
    now_dt = datetime.datetime.now(datetime.timezone.utc)

    duration_seconds = None
    if started_at:
        end_dt = now_dt if str(getattr(container, 'status', '') or state.get('Status') or '').lower() == 'running' else finished_at
        if end_dt:
            try:
                duration_seconds = max(0.0, (end_dt - started_at).total_seconds())
            except Exception:
                duration_seconds = None

    with _CONTAINER_LAST_STATS_LOCK:
        cached = dict(_CONTAINER_LAST_STATS.get(getattr(container, 'id', ''), {}) or {})

    return {
        'status': str(getattr(container, 'status', '') or state.get('Status') or 'unknown'),
        'running': bool(state.get('Running')),
        'exit_code': state.get('ExitCode'),
        'oom_killed': bool(state.get('OOMKilled')),
        'error': str(state.get('Error') or ''),
        'started_at': state.get('StartedAt') or '',
        'finished_at': state.get('FinishedAt') or '',
        'duration_seconds': duration_seconds,
        'duration_human': format_duration(duration_seconds or 0) if duration_seconds is not None else '',
        'pid': state.get('Pid'),
        'restart_count': attrs.get('RestartCount', 0),
        'size_rw': attrs.get('SizeRw', 0),
        'size_root_fs': attrs.get('SizeRootFs', 0),
        'last_stats': cached.get('stats') or None,
        'last_stats_at': cached.get('ts')
    }


def _container_detail_payload(container):
    container.reload()
    attrs = getattr(container, 'attrs', {}) or {}
    config = attrs.get('Config', {}) or {}
    host_config = attrs.get('HostConfig', {}) or {}
    network_settings = attrs.get('NetworkSettings', {}) or {}
    ports = network_settings.get('Ports', {}) or {}
    mounts = attrs.get('Mounts', []) or []
    image_name = ''
    try:
        img_tags = container.image.tags
        image_name = img_tags[0] if img_tags else (container.image.id[:12] if container.image else '')
    except Exception:
        image_name = str(config.get('Image') or '')

    env_items = []
    for item in (config.get('Env') or [])[:30]:
        env_items.append(str(item))

    mount_items = []
    for m in mounts:
        mount_items.append({
            'type': m.get('Type') or '',
            'source': m.get('Source') or '',
            'destination': m.get('Destination') or '',
            'mode': m.get('Mode') or '',
            'rw': bool(m.get('RW'))
        })

    port_items = []
    for key, bindings in ports.items():
        bound = []
        if isinstance(bindings, list):
            for b in bindings:
                bound.append({
                    'host_ip': b.get('HostIp') or '',
                    'host_port': b.get('HostPort') or ''
                })
        port_items.append({'container_port': key, 'bindings': bound})

    return {
        'status': 'success',
        'container': {
            'id': container.id[:12],
            'full_id': container.id,
            'name': container.name or container.id[:12],
            'image': image_name,
            'image_ref': str(config.get('Image') or image_name),
            'command': ' '.join(config.get('Cmd') or []) if isinstance(config.get('Cmd'), list) else str(config.get('Cmd') or ''),
            'entrypoint': ' '.join(config.get('Entrypoint') or []) if isinstance(config.get('Entrypoint'), list) else str(config.get('Entrypoint') or ''),
            'working_dir': str(config.get('WorkingDir') or ''),
            'user': str(config.get('User') or ''),
            'created': attrs.get('Created') or '',
            'platform': str(attrs.get('Platform') or ''),
            'ports': port_items,
            'mounts': mount_items,
            'env': env_items,
            'labels': config.get('Labels') or {},
            'network_mode': str(host_config.get('NetworkMode') or ''),
            'runtime': _container_runtime_summary(container)
        }
    }


def pull_image_with_progress(client, image_name):
    """Pull an image from ordered sources without duplicate concurrent downloads.

    Pulling the same multi-gigabyte image from several registries at once divided
    bandwidth, multiplied temporary storage usage, and required killing healthy
    transfers.  HTTPS preflight now orders sources and they are tried serially.
    A process-wide lock also prevents multiple UI/plugin pulls from competing for
    Podman's image store.
    """
    with _IMAGE_PULL_LOCK:
        _pull_in_progress.set()
        try:
            return _pull_image_with_progress_locked(client, image_name)
        finally:
            _pull_in_progress.clear()


def _pull_image_with_progress_locked(client, image_name):
    try:
        client = get_docker_client() or client
        candidates = _order_pull_candidates(image_name, _build_pull_candidates(image_name))
        errors = []

        log(f"📥 Trying {len(candidates)} ordered pull source(s) for {image_name} ...")
        for idx, cand in enumerate(candidates, start=1):
            started = time.perf_counter()
            try:
                source_label = _pull_registry_host(cand).split('.')[0] or f'Src{idx}'
                log(f"[DEBUG] Pull source {idx}/{len(candidates)}: trying {cand}")
                _pull_single_ref_with_progress(
                    client, cand, display_name=image_name, source_label=source_label
                )
                _retag_to_original_if_needed(client, cand, image_name)
                _ensure_image_exists_after_pull(client, cand, image_name)
                _note_pull_candidate_result(cand, True, (time.perf_counter() - started) * 1000.0)
                log(f"📥 Pull complete {image_name} (via {source_label})")
                return
            except Exception as e:
                _note_pull_candidate_result(cand, False, (time.perf_counter() - started) * 1000.0)
                errors.append(f"{cand}: {e}")
                if idx < len(candidates):
                    log(f"[DEBUG] Pull attempt {idx}/{len(candidates)} failed, trying next source...")
                else:
                    log(f"[WARN] All {len(candidates)} pull sources failed for {image_name}")
                client = get_docker_client() or client

        if image_name == R_DOCKER_IMAGE:
            try:
                log('[SYSTEM] Registry sources unavailable; trying the verified PrimiGenius R image archive...')
                _install_pinned_r_image_archive(client)
                log(f'[SYSTEM] Installed pinned R image from verified archive: {R_DOCKER_IMAGE_TAGGED}')
                return
            except Exception as archive_err:
                errors.append(f'PrimiGenius release archive: {archive_err}')
                log(f'[WARN] R image archive fallback failed: {archive_err}')

        all_io_error = bool(errors) and all('input/output error' in err for err in errors)
        if all_io_error:
            log('[WARN] All pull attempts failed with I/O error, restarting Podman and retrying...')
            try:
                mgr = _get_podman_manager()
                mgr.machine_stop()
                time.sleep(3)
                mgr.machine_start()
                time.sleep(5)
                if mgr.wait_for_engine(max_attempts=30, interval=2):
                    log('[SYSTEM] Podman restarted, retrying pull...')
                    global _docker_client
                    _docker_client = None
                    client = get_docker_client()
                    if client:
                        for idx, cand in enumerate(candidates, start=1):
                            try:
                                _pull_single_ref_with_progress(client, cand, display_name=image_name)
                                _retag_to_original_if_needed(client, cand, image_name)
                                _ensure_image_exists_after_pull(client, cand, image_name)
                                log(f"[SYSTEM] Pull complete after restart {image_name}")
                                return
                            except Exception as e2:
                                log(f"[DEBUG] Retry pull failed for {cand}: {e2}")
            except Exception as restart_err:
                log(f'[ERROR] Podman restart failed: {restart_err}')


        has_mirrors = bool(_get_pull_mirrors())
        if not has_mirrors:
            log('[ERROR] Image pull failed and no mirror sources configured.')
            log('[TIP] If you are in China, please switch network mode to "cn" or configure mirror sources in Settings > Network.')
        elif _effective_network_mode() == 'auto':
            log('[ERROR] All pull sources failed (Docker Hub + mirrors).')
            log('[TIP] Try switching network mode to "cn" (China) for mirror-priority pulling, or configure custom mirror sources in Settings > Network.')
        raise RuntimeError("; ".join(errors) or f'No pull source available for {image_name}')
    finally:
        pass


def _create_container_with_image_repair(client, create_kwargs, image_ref, *, phase='run', auto_build_fn=None):
    """Create a container and retry once if the image is unexpectedly missing."""
    img_ref = str(image_ref or '').strip()
    try:
        return client.containers.create(**create_kwargs)
    except docker.errors.ImageNotFound:
        if not img_ref:
            raise
        log(f"[WARN] Image missing during {phase}: {img_ref}; pulling with fallback and retrying...")
        _image_exists_cache.pop(img_ref, None)
        if auto_build_fn and auto_build_fn():
            _image_exists_cache[img_ref] = True
        else:
            pull_image_with_progress(client, img_ref)
            _image_exists_cache[img_ref] = True
        return client.containers.create(**create_kwargs)
    except docker.errors.APIError as e:
        if img_ref and 'No such image' in str(e):
            log(f"[WARN] Docker reported missing image during {phase}: {img_ref}; pulling with fallback and retrying...")
            _image_exists_cache.pop(img_ref, None)
            if auto_build_fn and auto_build_fn():
                _image_exists_cache[img_ref] = True
            else:
                pull_image_with_progress(client, img_ref)
                _image_exists_cache[img_ref] = True
            return client.containers.create(**create_kwargs)
        raise
    except (ConnectionError, OSError) as e:
        log(f"[WARN] Connection error during {phase} container create, retrying after reconnect...")
        global _docker_client
        _docker_client = None
        time.sleep(2)
        client = get_docker_client()
        if not client:
            raise
        return client.containers.create(**create_kwargs)

_docker_client = None
_podman_mgr = None
_podman_client_lock = threading.RLock()
_containers_cache = {'data': None, 'time': 0}
_images_cache = {'data': None, 'time': 0}
_image_archive_lock = threading.Lock()
_CACHE_TTL = 10
_pull_in_progress = threading.Event()
_images_refresh_lock = threading.Lock()
_containers_refresh_lock = threading.Lock()
_podman_info_refresh_lock = threading.Lock()
_cache_preload_done = False
_cache_preload_lock = threading.Lock()

def _get_podman_manager():
    global _podman_mgr, _WSL_DISTRO_NAME
    if _podman_mgr is None:
        _podman_mgr = PodmanManager(APP_INSTALL_DIR)
        _WSL_DISTRO_NAME = _podman_mgr.wsl_distro_name
    return _podman_mgr

def _get_podman_socket_path():
    mgr = _get_podman_manager()
    return mgr.get_socket_path()

_podman_start_fail_count = 0
_PODMAN_MAX_START_RETRIES = 3
_podman_permanently_failed = False
_podman_async_running = False
_podman_async_lock = threading.Lock()
_podman_start_retry_after = 0.0

def _ensure_podman_running():
    global _podman_start_fail_count, _podman_permanently_failed, _podman_async_running, _podman_start_retry_after
    mgr = _get_podman_manager()
    if mgr.is_engine_ready():
        _podman_start_fail_count = 0
        _podman_permanently_failed = False
        _podman_start_retry_after = 0.0
        return True

    if time.monotonic() < _podman_start_retry_after:
        return False

    if _podman_async_running:
        log("[SETUP] Podman setup already in progress, waiting...")
        for _ in range(60):
            time.sleep(1)
            if mgr.is_engine_ready():
                _podman_start_fail_count = 0
                _podman_permanently_failed = False
                return True
            if not _podman_async_running:
                break
        return mgr.is_engine_ready()

    if _podman_start_fail_count >= _PODMAN_MAX_START_RETRIES:
        _podman_start_fail_count = 0

    _podman_start_fail_count += 1
    log(f"[SETUP] Starting Podman (attempt {_podman_start_fail_count}/{_PODMAN_MAX_START_RETRIES})...")

    ok, msg = mgr.auto_setup()
    if not ok:
        _podman_start_retry_after = time.monotonic() + min(60, 3 * (2 ** max(0, _podman_start_fail_count - 1)))
        log(f"[SETUP] Podman start failed: {msg}")
        return False

    log("[SETUP] Waiting for Podman engine...")
    if mgr.wait_for_engine(max_attempts=30, interval=1):
        _podman_start_fail_count = 0
        _podman_permanently_failed = False
        _podman_start_retry_after = 0.0
        log("[SYSTEM] OK Podman engine ready")
        return True
    else:
        log("[SETUP] Podman engine did not become ready in time")
        return False

def _start_podman_async():
    global _podman_async_running
    if time.monotonic() < _podman_start_retry_after:
        return None
    with _podman_async_lock:
        if _podman_async_running:
            return None
        _podman_async_running = True

    def _worker():
        global _podman_start_fail_count, _podman_permanently_failed, _podman_async_running, _podman_start_retry_after
        try:
            mgr = _get_podman_manager()
            if mgr.is_engine_ready():
                _podman_start_fail_count = 0
                _podman_permanently_failed = False
                _podman_start_retry_after = 0.0
                get_docker_client()
                log("[SYSTEM] OK Podman engine ready")
                return
            log("[SYSTEM] [SETUP] Running auto setup to ensure WSL distro and Podman service...")
            ok, msg = mgr.auto_setup()
            if ok:
                _podman_start_fail_count = 0
                _podman_permanently_failed = False
                _podman_start_retry_after = 0.0
                if mgr.wait_for_engine(max_attempts=24, interval=0.5):
                    get_docker_client()
                    log("[SYSTEM] OK Auto setup complete, Podman engine ready")
                else:
                    _podman_start_retry_after = time.monotonic() + 30.0
                    diag = getattr(mgr, '_last_start_diagnostics', '') or 'no diagnostics available'
                    log(f"[ERROR] Auto setup completed but Podman API is not reachable: {diag}")
            else:
                _podman_start_retry_after = time.monotonic() + 30.0
                log(f"[ERROR] Auto setup failed: {msg}")
        finally:
            # Retry legacy-library retirement on every successful application
            # startup, not only after a package job or plugin run.
            try:
                mgr = _get_podman_manager()
                if mgr.is_engine_ready():
                    client = get_docker_client()
                    if client:
                        _complete_r_runtime_migration(client)
            except Exception as e:
                log(f'[WARN] Startup R migration cleanup deferred: {e}')
            with _podman_async_lock:
                _podman_async_running = False
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t

_docker_client_last_ping = 0
_docker_reconnecting = False
_docker_reconnect_failures = 0
_docker_reconnect_after = 0.0


def _close_docker_client(client):
    if client is None:
        return
    try:
        client.close()
    except Exception:
        pass


def _ping_docker_client(client, timeout=3):
    try:
        return client.ping(timeout=timeout)
    except TypeError:
        # Native Podman bindings do not accept Docker SDK's timeout keyword.
        return client.ping()


def _record_docker_reconnect_failure():
    global _docker_reconnect_failures, _docker_reconnect_after
    with _podman_client_lock:
        _docker_reconnect_failures = min(_docker_reconnect_failures + 1, 8)
        delay = min(60.0, 3.0 * (2 ** (_docker_reconnect_failures - 1)))
        _docker_reconnect_after = time.monotonic() + delay


def _record_docker_reconnect_success():
    global _docker_reconnect_failures, _docker_reconnect_after
    with _podman_client_lock:
        _docker_reconnect_failures = 0
        _docker_reconnect_after = 0.0


def _invalidate_docker_client(expected=None):
    global _docker_client, _docker_client_last_ping, _api_client_cache
    client = None
    with _podman_client_lock:
        if expected is None or _docker_client is expected:
            client = _docker_client
            _docker_client = None
            _docker_client_last_ping = 0
    _close_docker_client(client)
    cached_api = _api_client_cache.get('client') if '_api_client_cache' in globals() else None
    if cached_api is not None:
        try:
            cached_api.close()
        except Exception:
            pass
        _api_client_cache = {'client': None, 'time': 0}

def get_docker_client():
    global _docker_client, _docker_client_last_ping, _docker_reconnecting
    with _podman_client_lock:
        if _docker_client:
            now = time.time()
            if now - _docker_client_last_ping > 30:
                try:
                    _ping_docker_client(_docker_client, timeout=3)
                    _docker_client_last_ping = now
                    return _docker_client
                except Exception:
                    stale_client = _docker_client
                    _docker_client = None
                    _docker_client_last_ping = 0
                    _close_docker_client(stale_client)
            else:
                return _docker_client
        if _docker_reconnecting:
            return None
        if time.monotonic() < _docker_reconnect_after:
            return None
        _docker_reconnecting = True
    try:
        mgr = _get_podman_manager()
        if not mgr.is_engine_ready():
            if _podman_async_running or getattr(mgr, '_setup_running', False):
                return None
            if not _ensure_podman_running():
                _record_docker_reconnect_failure()
                return None
        if not mgr.is_engine_ready():
            _record_docker_reconnect_failure()
            return None
        docker_host = mgr.get_docker_host_env()
        if docker_host:
            os.environ['DOCKER_HOST'] = docker_host
        client = mgr.get_podman_client()
        if client is not None:
            with _podman_client_lock:
                _docker_client = client
                _docker_client_last_ping = time.time()
            _record_docker_reconnect_success()
            _trigger_cache_preload()
            return _docker_client
        new_client = docker.DockerClient(
            base_url=docker_host or os.environ.get('DOCKER_HOST'),
            version=DOCKER_API_VERSION,
            timeout=15
        )
        new_client.ping()
        with _podman_client_lock:
            _docker_client = new_client
            _docker_client_last_ping = time.time()
        _record_docker_reconnect_success()
        _trigger_cache_preload()
        return _docker_client
    except Exception:
        _record_docker_reconnect_failure()
        return None
    finally:
        with _podman_client_lock:
            _docker_reconnecting = False

def _trigger_cache_preload():
    global _cache_preload_done
    with _cache_preload_lock:
        if _cache_preload_done:
            return
        _cache_preload_done = True
    def _preload():
        _refresh_containers_cache()
        _refresh_images_cache()
    threading.Thread(target=_preload, daemon=True).start()

_api_client_cache = {'client': None, 'time': 0}

def _get_api_client(skip_ping=False):
    global _api_client_cache
    if _podman_permanently_failed:
        return None
    now = time.time()
    cached = _api_client_cache.get('client')
    if cached and now - _api_client_cache.get('time', 0) < 30:
        if skip_ping:
            return cached
        try:
            cached.ping(timeout=3)
            return cached
        except Exception:
            _api_client_cache = {'client': None, 'time': 0}
    mgr = _get_podman_manager()
    url = mgr.get_api_service_url()
    if not url:
        return None
    for attempt in range(2):
        try:
            api_client = docker.APIClient(
                base_url=url,
                version=DOCKER_API_VERSION,
                timeout=5
            )
            api_client.ping(timeout=3)
            _api_client_cache = {'client': api_client, 'time': time.time()}
            return api_client
        except Exception:
            if attempt == 0:
                os.environ['DOCKER_HOST'] = url
                time.sleep(0.3)
    return None


def _get_readonly_api_client(timeout=_READONLY_CLIENT_TIMEOUT):
    """Create a short-lived API client for UI reads so long jobs cannot monopolize it."""
    try:
        mgr = _get_podman_manager()
        url = mgr.get_api_service_url() or os.environ.get('DOCKER_HOST', '')
        if not url:
            return None
        return docker.APIClient(
            base_url=url,
            version=DOCKER_API_VERSION,
            timeout=timeout
        )
    except Exception:
        return None


def _close_readonly_api_client(api):
    try:
        if api:
            api.close()
    except Exception:
        pass

_WSL_DISTRO_NAME = None


def _active_wsl_distro_name():
    global _WSL_DISTRO_NAME
    if not _WSL_DISTRO_NAME:
        _WSL_DISTRO_NAME = _get_podman_manager().wsl_distro_name
    return _WSL_DISTRO_NAME

def _run_hidden_wsl(cmd_args, timeout=30, capture=True):
    result = _run_hidden(cmd_args, timeout=timeout, capture=capture)
    if result is None:
        log(f'[WARN] _run_hidden_wsl failed: cmd={cmd_args}')
    return result

def _robust_container_wait(container, timeout=None):
    """Wait for container to finish, with WSL-based fallback if Docker API fails.

    Args:
        container: Docker/Podman container object.
        timeout: Max seconds to wait. None or 0 means wait indefinitely
                 until the container exits on its own or is manually stopped.
    """
    container_id = container.id
    indefinite = timeout is None or timeout <= 0

    # Phase 1: try Docker SDK wait (only with timeout if specified)
    try:
        if indefinite:
            # Docker SDK wait(None) may block forever; use a very large value
            result = container.wait(timeout=86400)
        else:
            result = container.wait(timeout=timeout)
        return result
    except Exception as e:
        log(f'[WARN] Docker API wait failed ({e}), falling back to WSL poll...')

    # Phase 2: WSL poll loop — indefinite if timeout is None/0
    start_time = time.time()
    poll_interval = 5
    while True:
        elapsed = time.time() - start_time
        if not indefinite and elapsed >= timeout:
            log(f'[WARN] WSL poll timeout for container {container_id[:12]} after {timeout}s')
            break
        try:
            proc = _run_hidden_wsl(
                ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--',
                 'podman', 'inspect', container_id, '--format', '{{.State.Status}}'],
                timeout=10, capture=True
            )
            status = (proc.stdout or '').strip() if proc else ''
            if status in ('exited', 'dead', 'stopped'):
                try:
                    exit_proc = _run_hidden_wsl(
                        ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--',
                         'podman', 'inspect', container_id, '--format', '{{.State.ExitCode}}'],
                        timeout=10, capture=True
                    )
                    exit_code = int((exit_proc.stdout or '').strip() or '-1') if exit_proc else -1
                except Exception:
                    exit_code = -1
                log(f'[INFO] WSL poll: container {container_id[:12]} status={status}, exit_code={exit_code}')
                return {'StatusCode': exit_code}
            if status == 'running':
                pass
            elif not status:
                log(f'[DEBUG] WSL poll: container {container_id[:12]} inspect returned empty, retrying...')
            else:
                log(f'[DEBUG] WSL poll: container {container_id[:12]} status={status}, continuing to wait...')
        except Exception:
            pass
        time.sleep(poll_interval)

    # Phase 3: only reached when a finite timeout was exceeded
    try:
        final_proc = _run_hidden_wsl(
            ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--',
             'podman', 'inspect', container_id, '--format', '{{.State.Status}}'],
            timeout=10, capture=True
        )
        final_status = (final_proc.stdout or '').strip() if final_proc else ''
        if final_status == 'running':
            log(f'[WARN] Container {container_id[:12]} is still running after timeout, extending wait...')
            extra_start = time.time()
            while time.time() - extra_start < 600:
                try:
                    proc2 = _run_hidden_wsl(
                        ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--',
                         'podman', 'inspect', container_id, '--format', '{{.State.Status}}'],
                        timeout=10, capture=True
                    )
                    s2 = (proc2.stdout or '').strip() if proc2 else ''
                    if s2 in ('exited', 'dead', 'stopped'):
                        try:
                            ec_proc = _run_hidden_wsl(
                                ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--',
                                 'podman', 'inspect', container_id, '--format', '{{.State.ExitCode}}'],
                                timeout=10, capture=True
                            )
                            ec = int((ec_proc.stdout or '').strip() or '-1') if ec_proc else -1
                        except Exception:
                            ec = -1
                        log(f'[INFO] WSL poll (extended): container {container_id[:12]} status={s2}, exit_code={ec}')
                        return {'StatusCode': ec}
                except Exception:
                    pass
                time.sleep(5)
            log(f'[WARN] Container {container_id[:12]} still running after extended wait, returning -1')
        else:
            log(f'[INFO] WSL poll: container {container_id[:12]} final status={final_status}')
    except Exception:
        pass
    return {'StatusCode': -1}

def _wsl_stream_logs(container, stop_event, seen_set, on_line):
    import queue as _queue
    si = _HIDDEN_SUBPROCESS_KWARGS.get('startupinfo')
    proc = None
    try:
        proc = subprocess.Popen(
            ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--', 'podman', 'logs', '-f', container.id],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            creationflags=0x08000000 | 0x00000200,
            startupinfo=si
        )
    except Exception:
        pass

    if proc is None or proc.poll() is not None:
        if proc:
            first_line = ''
            try:
                first_line = proc.stdout.readline() if proc.stdout else ''
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                pass
            if first_line and ('journald' in first_line.lower() or 'not supported' in first_line.lower()):
                log('[DEBUG] podman logs -f not supported (journald driver), falling back to polling')
            return _wsl_poll_logs(container, stop_event, seen_set, on_line)
        return False

    line_queue = _queue.Queue()
    journald_error = [False]

    def _reader():
        while True:
            try:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.rstrip('\n\r')
                if 'journald' in line.lower() and 'not supported' in line.lower():
                    journald_error[0] = True
                    break
                line_queue.put(line)
            except Exception:
                break

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    while not stop_event.is_set():
        try:
            line = line_queue.get(timeout=0.5)
            _emit_live_container_log(line, seen_set, on_line)
        except _queue.Empty:
            if not reader.is_alive() and line_queue.empty():
                break

    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        pass

    if journald_error[0]:
        return _wsl_poll_logs(container, stop_event, seen_set, on_line)
    return True

def _wsl_poll_logs(container, stop_event, seen_set, on_line):
    seen = 0
    container_id = container.id
    while not stop_event.is_set():
        try:
            proc = _run_hidden_wsl(
                ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--', 'podman', 'logs', container_id],
                timeout=10, capture=True
            )
            if proc and proc.stdout:
                lines = proc.stdout.replace('\r\n', '\n').replace('\r', '\n').split('\n')
                for line in lines[seen:]:
                    line = line.rstrip('\r')
                    if line:
                        _emit_live_container_log(line, seen_set, on_line)
                seen = len(lines)
        except Exception:
            pass
        try:
            status_proc = _run_hidden_wsl(
                ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--',
                 'podman', 'inspect', container_id, '--format', '{{.State.Status}}'],
                timeout=5, capture=True
            )
            actual_status = (status_proc.stdout or '').strip() if status_proc else ''
            if actual_status != 'running':
                break
        except Exception:
            break
        time.sleep(1)
    try:
        proc = _run_hidden_wsl(
            ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--', 'podman', 'logs', container_id],
            timeout=10, capture=True
        )
        if proc and proc.stdout:
            lines = proc.stdout.replace('\r\n', '\n').replace('\r', '\n').split('\n')
            for line in lines[seen:]:
                line = line.rstrip('\r')
                if line:
                    _emit_live_container_log(line, seen_set, on_line)
    except Exception:
        pass
    return True

def _emit_live_container_log(line, seen_set, on_line):
    if not line:
        return
    if isinstance(seen_set, dict):
        seen_set[line] = int(seen_set.get(line, 0)) + 1
        on_line(line)
        return
    if line not in seen_set:
        seen_set.add(line)
        on_line(line)

def _emit_replayed_container_log(line, seen_set, on_line):
    if not line:
        return
    if isinstance(seen_set, dict):
        already_emitted = int(seen_set.get(line, 0))
        if already_emitted > 0:
            seen_set[line] = already_emitted - 1
            return
        on_line(line)
        return
    if line not in seen_set:
        seen_set.add(line)
        on_line(line)

def _stream_container_logs_thread(container, stop_event, seen_set, on_line, stream_ref_list=None):
    if platform.system() == 'Windows':
        _wsl_stream_logs(container, stop_event, seen_set, on_line)
        return
    _got_any = False
    try:
        stream = container.attach(stream=True, logs=True, stdout=True, stderr=True)
        if stream_ref_list is not None:
            stream_ref_list[0] = stream
        for raw_chunk in stream:
            if stop_event.is_set():
                break
            text = raw_chunk.decode('utf-8', errors='replace') if isinstance(raw_chunk, bytes) else str(raw_chunk)
            for line in text.splitlines():
                line = line.rstrip('\r')
                if line:
                    _got_any = True
                    _emit_live_container_log(line, seen_set, on_line)
    except Exception:
        pass
    finally:
        if stream_ref_list is not None:
            stream_ref_list[0] = None

    if _got_any:
        return
    if stop_event.is_set():
        return

def _collect_final_logs(container, seen_set, on_line):
    try:
        raw = container.attach(stdout=True, stderr=True, stream=False, logs=True)
        if isinstance(raw, bytes):
            raw_text = raw.decode('utf-8', errors='replace')
        else:
            raw_text = str(raw)
        replayed_any = False
        for raw_line in raw_text.splitlines():
            l = raw_line.rstrip('\n')
            if l:
                replayed_any = True
            _emit_replayed_container_log(l, seen_set, on_line)
        if replayed_any:
            return
    except Exception:
        pass

    if platform.system() == 'Windows':
        try:
            proc = _run_hidden_wsl(
                ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--', 'podman', 'logs', container.id],
                timeout=10, capture=True
            )
            if proc is None:
                return
            replayed_any = False
            for line in (proc.stdout or '').splitlines():
                l = line.rstrip('\n\r')
                if l:
                    replayed_any = True
                _emit_replayed_container_log(l, seen_set, on_line)
            if replayed_any:
                return
        except Exception:
            pass
    try:
        raw = container.logs(stdout=True, stderr=True)
        if isinstance(raw, bytes):
            raw_text = raw.decode('utf-8', errors='replace')
        else:
            raw_text = str(raw)
        for raw_line in raw_text.splitlines():
            l = raw_line.rstrip('\n')
            _emit_replayed_container_log(l, seen_set, on_line)
    except Exception:
        pass

# 默认输出目录和R包目录放在主程序安装目录下
USER_DOCS = os.path.join(APP_INSTALL_DIR, 'outputs')
os.makedirs(USER_DOCS, exist_ok=True)

# Plugins are stored directly in the app's own plugins/ directory (under r/ and linux/ subdirs).
# No duplication to Documents — single location only.
for _subcat in ['r', 'linux']:
    os.makedirs(os.path.join(PLUGINS_DIR, _subcat), exist_ok=True)
log(f"[SYSTEM] Plugins directory: {PLUGINS_DIR}")

def find_binary_path(plugin_dir, binary_name):
    bin_root = os.path.join(plugin_dir, 'bin')
    if not os.path.exists(bin_root): return None, None
    def _rel(root):
        """Return relative path from bin_root, with '.' normalized to '' for cleaner paths."""
        r = os.path.relpath(root, bin_root).replace("\\", "/")
        return '' if r == '.' else r

    # Pass 1: exact name match (case-sensitive)
    for root, dirs, files in os.walk(bin_root):
        if binary_name in files:
            return os.path.join(root, binary_name), _rel(root)
    # Pass 2: case-insensitive exact match
    bn_lower = binary_name.lower()
    for root, dirs, files in os.walk(bin_root):
        for f in files:
            if f.lower() == bn_lower:
                return os.path.join(root, f), _rel(root)
    # Pass 3: prefix match (e.g. binary_name="hisat2" matches "hisat2-build")
    for root, dirs, files in os.walk(bin_root):
        for f in files:
            if f.startswith(binary_name) and not f.endswith('.zip') and not f.endswith('.json'):
                return os.path.join(root, f), _rel(root)
    # Pass 4: the binary might be inside a 'bin/' subdirectory within the extracted package
    inner_bin = os.path.join(bin_root, 'bin')
    if os.path.isdir(inner_bin):
        for root, dirs, files in os.walk(inner_bin):
            if binary_name in files:
                return os.path.join(root, binary_name), _rel(root)
    return None, None


# ==========================================
# Smart Execution Type Detection
# ==========================================

# Known script/runtime file extensions and their invocation prefixes
_EXEC_TYPE_MAP = {
    '.jar':  'java',
    '.py':   'python',
    '.pl':   'perl',
    '.sh':   'shell',
    '.r':    'rscript',
    '.rb':   'ruby',
    '.lua':  'lua',
    '.js':   'node',
}

def detect_execution_type(cfg):
    """
    Determine how to invoke the plugin's tool.

    Priority:
      1. Explicit `execution_type` field in config  (java | python | perl | shell | rscript | binary | ...)
      2. Auto-detect from binary_name file extension
      3. Default to 'binary' (native compiled ELF)
    """
    explicit = cfg.get('execution_type')
    if explicit:
        return explicit.lower().strip()

    binary_name = cfg.get('binary_name', '')
    if binary_name:
        ext = os.path.splitext(binary_name)[1].lower()
        if ext in _EXEC_TYPE_MAP:
            return _EXEC_TYPE_MAP[ext]

    return 'binary'


def build_version_command(cfg):
    """Build a version-check command appropriate for the execution type."""
    custom_cmd = str(cfg.get('version_command', '')).strip()
    if custom_cmd:
        return custom_cmd

    exec_type = detect_execution_type(cfg)
    bin_name  = cfg.get('binary_name', '')
    bin_sub   = cfg.get('binary_subdir', '')

    if not bin_name:
        return ''

    is_local_binary = bool(cfg.get('local_binary'))
    # Build clean tool root path (avoid double-slash when bin_sub is empty)
    tool_path = f"/tool_root/{bin_sub}/{bin_name}" if bin_sub else f"/tool_root/{bin_name}"
    tool_dir  = f"/tool_root/{bin_sub}" if bin_sub else "/tool_root"
    tool_ref = tool_path if is_local_binary else bin_name

    def _local_native(*args):
        attempts = " || ".join([f"(cd {tool_dir} && ./{bin_name} {arg} 2>&1)" for arg in args])
        return attempts

    def _plain_native(*args):
        attempts = " || ".join([f"{tool_ref} {arg} 2>&1" for arg in args])
        return attempts

    if exec_type == 'java':
        return f"java -jar {tool_ref} --version 2>&1 || java -version 2>&1 | head -1"
    elif exec_type == 'python':
        return f"python3 {tool_ref} --version 2>&1 || python3 --version 2>&1"
    elif exec_type == 'perl':
        return f"perl {tool_ref} --version 2>&1 || perl -v 2>&1 | head -2"
    elif exec_type == 'shell':
        shell_bin = str(cfg.get('shell', 'bash')).strip().lower()
        if shell_bin not in ('bash', 'sh'):
            shell_bin = 'bash'
        force_shell_entrypoint = bool(cfg.get('force_shell_entrypoint'))
        return f"{shell_bin} {tool_ref} --version 2>&1 || {shell_bin} --version 2>&1 | head -1"
    elif exec_type == 'rscript':
        return f"Rscript {tool_ref} --version 2>&1 || Rscript --version 2>&1"
    elif exec_type == 'node':
        return f"node {tool_ref} --version 2>&1 || node --version 2>&1"
    else:
        # native binary
        if is_local_binary:
            return _local_native('--version', '-version', '-v', '-V', '--help')
        return _plain_native('--version', '-version', '-v', '-V', '--help')


def extract_image_tag(image_name):
    """Extract a meaningful tag from a Docker image reference."""
    if not image_name:
        return ''
    image_ref = str(image_name).split('@', 1)[0].strip()
    if not image_ref:
        return ''
    last_slash = image_ref.rfind('/')
    last_colon = image_ref.rfind(':')
    if last_colon > last_slash:
        return image_ref[last_colon + 1:].strip()
    return ''


def fallback_tool_version(cfg):
    """Provide a readable non-empty fallback instead of a bare Unknown string."""
    explicit = str(cfg.get('tool_version', '')).strip()
    if explicit:
        return explicit

    image_tag = extract_image_tag(cfg.get('docker_image', ''))
    if image_tag and image_tag.lower() != 'latest':
        return image_tag

    bin_name = str(cfg.get('binary_name', '')).strip()
    if bin_name:
        return f"{bin_name} (local binary)" if cfg.get('local_binary') else f"{bin_name} (container binary)"

    return 'Unavailable'


def parse_tool_version_output(output_text, cfg=None):
    """Extract a concise version string from command output."""
    if not output_text:
        return ''

    lines = []
    seen = set()
    for raw_line in str(output_text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)

    if not lines:
        return ''

    bin_name = str((cfg or {}).get('binary_name', '')).strip().lower()
    version_hint = re.compile(r'(version|v\.?\s*\d|release|kent source version|build\s*\d|\d+\.\d+)')
    noisy_hint = re.compile(r'(usage:|copyright|license|error:|not found|permission denied|unknown)')

    for line in lines:
        lower_line = line.lower()
        if noisy_hint.search(lower_line) and not version_hint.search(lower_line):
            continue
        if version_hint.search(lower_line):
            return line[:200]
        if bin_name and bin_name in lower_line and 'usage:' not in lower_line:
            return line[:200]

    for line in lines:
        lower_line = line.lower()
        if noisy_hint.search(lower_line):
            continue
        return line[:200]

    return ''


# ==========================================
# Smart Parameter Helpers
# ==========================================

def is_param_required(param):
    """
    Determine if a parameter is required.
    Checks both the explicit 'required' field AND whether the label text contains '*'.
    """
    if param.get('required'):
        return True
    label = param.get('label', '')
    label_text = _display_name(label) if isinstance(label, dict) else str(label)
    return '*' in label_text


def is_param_empty(val):
    """Check if a parameter value is effectively empty."""
    if val is None:
        return True
    if isinstance(val, str) and val.strip() == '':
        return True
    if isinstance(val, list) and len(val) == 0:
        return True
    return False


def clean_command(cmd):
    """
    Post-process a command string to fix common substitution artifacts:
    - Collapse multiple spaces into one
    - Remove empty flag-value pairs (e.g., '--flag  ' with no value after)
    - Strip leading/trailing whitespace per && segment
    - Preserve bash case-statement ;; terminators
    """
    import re
    # Protect bash case-statement ;; by replacing with placeholder
    cmd = cmd.replace(';;', '\x00DOUBLE_SEMI\x00')
    # Collapse multiple spaces (but not inside quotes)
    cmd = re.sub(r'  +', ' ', cmd)
    # Clean up whitespace around && and || and ;
    cmd = re.sub(r'\s*(&&|\|\||;)\s*', r' \1 ', cmd)
    # Restore bash case-statement ;;
    cmd = cmd.replace('\x00DOUBLE_SEMI\x00', ';;')
    return cmd.strip()


def _get_plugin_name(cfg, lang='en'):
    raw = cfg.get('name', '')
    if isinstance(raw, dict):
        return raw.get(lang) or raw.get('en') or raw.get('zh') or ''
    return raw if isinstance(raw, str) else ''

def _load_json_file(path):
    """Load JSON with BOM tolerance and encoding fallback.

    Some config files may have truncated UTF-8 sequences (e.g. from
    incomplete downloads or encoding conversion artifacts). We try
    multiple strategies before giving up.
    """
    with open(path, 'rb') as f:
        raw = f.read()
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'latin-1'):
        try:
            text = raw.decode(enc)
            return json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
        return json.load(f)

def load_all_plugins():
    """Load all plugin configs with a short TTL cache to avoid redundant disk I/O."""
    now = time.time()
    if hasattr(load_all_plugins, '_cache') and hasattr(load_all_plugins, '_cache_time'):
        if now - load_all_plugins._cache_time < 5:  # 5-second TTL
            return load_all_plugins._cache

    plugins = {}
    if not os.path.exists(PLUGINS_DIR):
        load_all_plugins._cache = plugins
        load_all_plugins._cache_time = now
        return {}
    # Support both flat structure and categorized structure (linux/, r/ subdirs)
    search_dirs = [PLUGINS_DIR]
    for subcat in ['linux', 'r']:
        subdir = os.path.join(PLUGINS_DIR, subcat)
        if os.path.isdir(subdir):
            search_dirs.append(subdir)
    
    for search_dir in search_dirs:
        for pid in os.listdir(search_dir):
            # Skip category subdirectories when scanning root
            if search_dir == PLUGINS_DIR and pid in ['linux', 'r']:
                continue
            ppath = os.path.join(search_dir, pid)
            cpath = os.path.join(ppath, 'config.json')
            if os.path.exists(cpath):
                try:
                    cfg = _load_json_file(cpath)
                    cfg['plugin_dir'] = ppath
                    if str(cfg.get('type') or '').strip().lower().startswith('r'):
                        cfg['configured_docker_image'] = cfg.get('docker_image')
                        cfg['docker_image'] = R_DOCKER_IMAGE
                    if cfg.get('local_binary'):
                        full, rel = find_binary_path(ppath, cfg['binary_name'])
                        cfg['is_installed'] = bool(full)
                        if full: cfg['binary_subdir'] = rel
                    elif cfg.get('run_mode') == 'native_gui':
                        if cfg.get('java_app'):
                            main_jar = cfg.get('main_jar', '')
                            jar_path = os.path.join(ppath, 'bin', main_jar)
                            cfg['is_installed'] = os.path.isfile(jar_path)
                        else:
                            launch_command = cfg.get('launch_command', '')
                            if launch_command:
                                launch_path = launch_command if os.path.isabs(launch_command) else os.path.join(ppath, cfg.get('working_dir', ''), launch_command)
                                cfg['is_installed'] = os.path.isfile(launch_path)
                            else:
                                cfg['is_installed'] = True
                    elif cfg.get('run_mode') == 'native_java_cli':
                        main_jar = cfg.get('main_jar', '')
                        jar_path = os.path.join(ppath, 'bin', main_jar)
                        cfg['is_installed'] = os.path.isfile(jar_path)
                    elif cfg.get('run_mode') == 'webapp':
                        entry = cfg.get('webapp_entry', 'index.html')
                        entry_path = os.path.join(ppath, entry)
                        cfg['is_installed'] = os.path.isfile(entry_path)
                    else: cfg['is_installed'] = True

                    if 'category' not in cfg: cfg['category'] = "默认分类"
                    plugins[cfg['id']] = cfg
                except Exception as e:
                    log(f"[WARN] Skip invalid plugin config: {cpath} ({e})")

    load_all_plugins._cache = plugins
    load_all_plugins._cache_time = now
    return plugins

def invalidate_plugin_cache():
    """Clear plugin config cache so newly installed binaries are detected immediately."""
    if hasattr(load_all_plugins, '_cache'):
        delattr(load_all_plugins, '_cache')
    if hasattr(load_all_plugins, '_cache_time'):
        delattr(load_all_plugins, '_cache_time')

def extract_sample_name(file_obj):
    filename = file_obj
    if isinstance(file_obj, list):
        if len(file_obj) > 0: filename = file_obj[0]
        else: return "unknown_sample"
    filename = os.path.basename(filename)
    extensions = ['.fq.gz', '.fastq.gz', '.fq', '.fastq', '.gz', '.pdb', '.pdbqt', '.bam', '.sam', '.fasta', '.fa', '.txt', '.csv']
    for ext in extensions:
        if filename.endswith(ext):
            filename = filename[:-len(ext)]
            break
    pair_suffixes = ['_1', '_2', '_R1', '_R2', '.1', '.2']
    for suffix in pair_suffixes:
        if filename.endswith(suffix):
            filename = filename[:-len(suffix)]
            break
    # Sanitize: replace spaces and special chars to avoid shell issues
    filename = filename.replace(' ', '_')
    return filename

def format_duration(seconds):
    return str(datetime.timedelta(seconds=int(seconds)))


# ---- R Docker runtime management ----
from docker.types import Mount  # type: ignore

R_VERSION = '4.6.1'
BIOCONDUCTOR_VERSION = '3.23'
R_DOCKER_IMAGE_REPOSITORY = 'rocker/r-ver'
R_DOCKER_IMAGE_TAG = R_VERSION
# Docker Hub linux/amd64 manifest digest, verified for rocker/r-ver:4.6.1.
# Keep the human-readable tag as well as the immutable digest in the reference.
R_DOCKER_IMAGE_DIGEST = 'sha256:18aac8c3b1fece4c60720f5f9b0dc066e04409c7de2896b28528e7be0bdd9535'
R_DOCKER_IMAGE_TAGGED = f'{R_DOCKER_IMAGE_REPOSITORY}:{R_DOCKER_IMAGE_TAG}'
R_DOCKER_IMAGE = f'{R_DOCKER_IMAGE_TAGGED}@{R_DOCKER_IMAGE_DIGEST}'
R_DOCKER_IMAGE_CONFIG_DIGEST = 'sha256:766543abfc3d0b569060c1b3a0c6f674e7a9c1258fb638d64a00a506c5a20fd7'
R_IMAGE_ARCHIVE_NAME = 'rocker_r-ver-4.6.1.tar.gz'
R_IMAGE_ARCHIVE_SHA256 = 'a9b6b8b1190ed9c18138c5c9033788c47ad4e1ae46a1e1ea03bc761a84ee00d2'
R_IMAGE_ARCHIVE_SIZE = 397702823
R_IMAGE_ARCHIVE_URL = (
    f'https://github.com/jianbai-design/PrimiGenius-images/'
    f'releases/download/r-{R_VERSION}/{R_IMAGE_ARCHIVE_NAME}'
)
R_IMAGE_ARCHIVE_ACCELERATORS = (
    'https://gh-proxy.com/',
    'https://gh-proxy.org/',
    'https://v4.gh-proxy.org/',
    'https://v6.gh-proxy.org/',
    'https://cdn.gh-proxy.org/',
    'https://ghproxy.net/',
    'https://gh-proxy.cn/',
    'https://mirror.ghproxy.com/',
    'https://gh.llkk.cc/',
    'https://hubp.llkk.cc/',
)
R_LEGACY_DOCKER_IMAGE = f'{R_DOCKER_IMAGE_REPOSITORY}:latest'
R_CUSTOM_RECIPE_VERSION = 4
R_CUSTOM_IMAGE = f'primigenius-r-base:r{R_VERSION}-bioc{BIOCONDUCTOR_VERSION}-v{R_CUSTOM_RECIPE_VERSION}'
R_LEGACY_CUSTOM_IMAGES = (
    'primigenius-r-base:install-v2',
    f'primigenius-r-base:r{R_VERSION}-bioc{BIOCONDUCTOR_VERSION}-v3',
)
R_LIBRARY_GENERATION = f'r{R_VERSION}-bioc{BIOCONDUCTOR_VERSION}'
R_LEGACY_LIBS_DIR = os.path.join(APP_INSTALL_DIR, 'r_libs')
R_LEGACY_BACKEND_LIBS_DIR = os.path.join(BASE_DIR, 'r_libs')
R_LIBS_DIR = os.path.join(APP_INSTALL_DIR, f'r_libs_{R_LIBRARY_GENERATION.replace(".", "_")}')
R_RUNTIME_STATE_FILE = os.path.join(APP_INSTALL_DIR, '.primigenius_r_runtime.json')
R_PACKAGE_LOCK_FILE = os.path.join(APP_INSTALL_DIR, '.primigenius_r_package.lock')
os.makedirs(R_LIBS_DIR, exist_ok=True)
R_LIBS_ENV = {'R_LIBS': '/r_libs:/usr/local/lib/R/site-library:/usr/lib/R/library', 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8'}
R_SYSTEM_DEPS = (
    'ca-certificates build-essential gfortran make pkg-config cmake '
    'libuv1-dev libgdal-dev libproj-dev libgeos-dev libudunits2-dev '
    'libharfbuzz-dev libfribidi-dev libfontconfig1-dev libfreetype6-dev '
    'libpng-dev libtiff-dev libjpeg-dev libcairo2-dev libxt-dev '
    'libcurl4-openssl-dev libxml2-dev libssl-dev libgit2-dev '
    'zlib1g-dev libbz2-dev liblzma-dev libsqlite3-dev'
)
_R_APT_HTTPS_SETUP = (
    "sed -i 's|http://archive.ubuntu.com|https://archive.ubuntu.com|g; "
    "s|http://security.ubuntu.com|https://security.ubuntu.com|g' "
    "/etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true;"
)
R_APT_MIRRORS = (
    ('tuna', 'https://mirrors.tuna.tsinghua.edu.cn/ubuntu'),
    ('ustc', 'https://mirrors.ustc.edu.cn/ubuntu'),
    ('aliyun', 'https://mirrors.aliyun.com/ubuntu'),
    ('official', 'https://archive.ubuntu.com/ubuntu'),
)
_R_APT_MIRROR_CACHE = {'items': None, 'time': 0.0}
_R_APT_MIRROR_LOCK = threading.Lock()
_R_LOCALE_SETUP = "sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen 2>/dev/null; locale-gen en_US.UTF-8 2>/dev/null; export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8;"


def _r_image_archive_urls():
    accelerated = [prefix + R_IMAGE_ARCHIVE_URL for prefix in R_IMAGE_ARCHIVE_ACCELERATORS]
    return accelerated + [R_IMAGE_ARCHIVE_URL] if _effective_network_mode() == 'cn' else [R_IMAGE_ARCHIVE_URL] + accelerated


def _probe_r_image_archive_url(url, timeout=6.0):
    """Measure a tiny verified range request without downloading the archive."""
    started = time.perf_counter()
    request_obj = urllib.request.Request(url, headers={
        'User-Agent': f'PrimiGenius/{R_VERSION}',
        'Accept': 'application/octet-stream,*/*',
        'Range': 'bytes=0-65535',
        'Cache-Control': 'no-cache',
    })
    try:
        with urllib.request.urlopen(request_obj, timeout=timeout) as response:
            first_chunk = response.read(64 * 1024)
        if not first_chunk.startswith(b'\x1f\x8b'):
            return None
        return int((time.perf_counter() - started) * 1000)
    except Exception:
        return None


def _ordered_r_image_archive_urls():
    """Probe candidates concurrently, then download once from the fastest."""
    urls = _r_image_archive_urls()
    results = {}

    def probe(url):
        results[url] = _probe_r_image_archive_url(url)

    threads = [threading.Thread(target=probe, args=(url,), daemon=True) for url in urls]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 7.0
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))

    reachable = []
    for index, url in enumerate(urls):
        latency = results.get(url)
        if isinstance(latency, (int, float)):
            reachable.append((latency, index, url))
    reachable.sort(key=lambda item: (item[0], item[1]))
    ordered = [url for _latency, _index, url in reachable]
    ordered.extend(url for url in urls if url not in ordered)
    if reachable:
        summary = ' -> '.join(
            f'{urllib.parse.urlparse(url).netloc} ({latency} ms)'
            for latency, _index, url in reachable
        )
        log(f'[DEBUG] R archive probe order: {summary}')
    return ordered


def _sha256_file(path_value):
    digest = hashlib.sha256()
    with open(path_value, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _download_r_image_archive(destination):
    if os.path.isfile(destination):
        if os.path.getsize(destination) == R_IMAGE_ARCHIVE_SIZE and _sha256_file(destination) == R_IMAGE_ARCHIVE_SHA256:
            log('[SYSTEM] Reusing verified R image archive from an earlier attempt')
            return destination
        os.remove(destination)

    part_path = destination + '.part'
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    errors = []
    log('[ARCHIVE_STATUS] Probing')
    urls = _ordered_r_image_archive_urls()
    for index, url in enumerate(urls, start=1):
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
            log(f'[DEBUG] R archive source {index}/{len(urls)}: {url}')
            request_obj = urllib.request.Request(url, headers={
                'User-Agent': f'PrimiGenius/{R_VERSION}',
                'Accept': 'application/octet-stream,*/*',
            })
            digest = hashlib.sha256()
            received = 0
            last_percent = -1
            with urllib.request.urlopen(request_obj, timeout=60) as response, open(part_path, 'wb') as output:
                total = int(response.headers.get('Content-Length') or 0)
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    percent = min(99, int(received * 100 / (total or R_IMAGE_ARCHIVE_SIZE)))
                    if percent != last_percent:
                        log(f'[ARCHIVE_PROGRESS] {percent} {received} {total or R_IMAGE_ARCHIVE_SIZE}')
                        last_percent = percent
            if received != R_IMAGE_ARCHIVE_SIZE:
                raise RuntimeError(f'archive size mismatch: expected {R_IMAGE_ARCHIVE_SIZE}, got {received}')
            actual_sha = digest.hexdigest()
            if actual_sha != R_IMAGE_ARCHIVE_SHA256:
                raise RuntimeError(f'archive SHA-256 mismatch: expected {R_IMAGE_ARCHIVE_SHA256}, got {actual_sha}')
            os.replace(part_path, destination)
            return destination
        except Exception as exc:
            errors.append(f'{url}: {exc}')
            if os.path.exists(part_path):
                os.remove(part_path)
    raise RuntimeError('; '.join(errors))


def _install_pinned_r_image_archive(client):
    mgr = _get_podman_manager()
    archive_dir = os.path.join(mgr.podman_data_dir, 'downloads')
    archive_path = _download_r_image_archive(os.path.join(archive_dir, R_IMAGE_ARCHIVE_NAME))
    imported = False
    try:
        with _image_archive_lock:
            wsl_path = mgr.host_path_to_wsl(archive_path)
            log('[ARCHIVE_STATUS] Loading')
            result = mgr._run_wsl(f'podman load -i {shlex.quote(wsl_path)}', timeout=24 * 60 * 60)
            if result is None or result.returncode != 0:
                detail = ((getattr(result, 'stderr', '') or '') or (getattr(result, 'stdout', '') or '')).strip()
                raise RuntimeError(detail or 'podman load failed')
        log('[ARCHIVE_STATUS] Verifying')
        client = get_docker_client() or client
        image = client.images.get(R_DOCKER_IMAGE_TAGGED)
        actual_id = str(getattr(image, 'id', '') or '').lower()
        if actual_id != R_DOCKER_IMAGE_CONFIG_DIGEST:
            try:
                mgr._run_wsl(f'podman rmi --no-prune {shlex.quote(R_DOCKER_IMAGE_TAGGED)}', timeout=60)
            except Exception:
                pass
            raise RuntimeError(f'imported image ID mismatch: expected {R_DOCKER_IMAGE_CONFIG_DIGEST}, got {actual_id or "unknown"}')
        _image_exists_cache[R_DOCKER_IMAGE] = True
        imported = True
    finally:
        if imported and os.path.exists(archive_path):
            os.remove(archive_path)
R_CUSTOM_RECIPE_SHA256 = hashlib.sha256(json.dumps({
    'recipe_version': R_CUSTOM_RECIPE_VERSION,
    'base_image': R_DOCKER_IMAGE,
    'system_dependencies': R_SYSTEM_DEPS.split(),
    'apt_https_setup': _R_APT_HTTPS_SETUP,
    'apt_mirrors': R_APT_MIRRORS,
    'locale': 'en_US.UTF-8',
    'apt_retries': 3,
    'apt_timeout_seconds': 30,
    'apt_no_install_recommends': True,
}, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _resolve_system_image_ref(image_ref):
    """Upgrade unqualified/current R runtime refs to the immutable system ref."""
    raw = str(image_ref or '').strip()
    if not raw or not _is_priority_image_name(raw):
        return raw
    _name, tag, digest = _parse_image_ref(raw)
    if digest:
        return raw
    if not tag or tag.lower() in ('latest', R_DOCKER_IMAGE_TAG.lower()):
        return R_DOCKER_IMAGE
    return raw


def _is_pinned_r_runtime_local_ref(image_ref):
    """Recognize the pinned R base through canonical or accelerator aliases."""
    raw = str(image_ref or '').strip()
    if not raw:
        return False
    name, tag, digest = _parse_image_ref(raw)
    path_parts = [part for part in str(name or '').lower().strip('/').split('/') if part]
    if len(path_parts) < 2 or path_parts[-2:] != ['rocker', 'r-ver']:
        return False
    if digest:
        return digest.lower() == R_DOCKER_IMAGE_DIGEST.lower()
    return str(tag or '').lower() == R_DOCKER_IMAGE_TAG.lower()


def _is_custom_r_runtime_local_ref(image_ref):
    """Recognize hidden PrimiGenius R runtime images, including local aliases."""
    raw = str(image_ref or '').strip()
    if not raw:
        return False
    name, _tag, _digest = _parse_image_ref(raw)
    return str(name or '').lower().strip('/').split('/')[-1:] == ['primigenius-r-base']


def _remove_custom_r_runtime_images(client):
    """Remove every hidden custom R image before its pinned base is removed."""
    removed_ids = set()
    for image in client.images.list(all=True):
        attrs = getattr(image, 'attrs', {}) or {}
        tags = list(getattr(image, 'tags', None) or attrs.get('RepoTags') or [])
        labels = ((attrs.get('Config') or {}).get('Labels') or {})
        is_r_build_layer = (
            labels.get('org.primigenius.r-version') == R_VERSION and
            labels.get('org.primigenius.base-digest') == R_DOCKER_IMAGE_DIGEST
        )
        if not any(_is_custom_r_runtime_local_ref(tag) for tag in tags) and not is_r_build_layer:
            continue
        image_ref = str(getattr(image, 'id', '') or tags[0]).strip()
        if not image_ref or image_ref in removed_ids:
            continue
        client.images.remove(image=image_ref, force=True)
        removed_ids.add(image_ref)
        log(f'[SYSTEM] Cascade-removed custom R image: {image_ref}')
    return removed_ids

# ---- Java runtime management ----
JAVA_DIR = os.path.join(APP_INSTALL_DIR, 'java')
JAVA_JRE_DIR = os.path.join(JAVA_DIR, 'jre')
JAVA_EXE = os.path.join(JAVA_JRE_DIR, 'bin', 'java.exe')
JAVA_JAVAW_EXE = os.path.join(JAVA_JRE_DIR, 'bin', 'javaw.exe')
DEFAULT_JAVA_VERSION = '8'
_JAVA_RUNTIMES = {
    '8': {
        'label': 'JRE 8',
        'dir': JAVA_JRE_DIR,
        'zip_names': ('jre8.zip',),
    },
}
_JAVA_READY = {}

def _normalize_java_version(version=None):
    raw = str(version or '').strip().lower()
    if not raw or raw in ('default', 'legacy', 'java8', 'jre8', 'jdk8', '8', '1.8'):
        return DEFAULT_JAVA_VERSION
    match = re.search(r'(\d+)', raw)
    if match and match.group(1) in _JAVA_RUNTIMES:
        return match.group(1)
    return DEFAULT_JAVA_VERSION

def _java_runtime_spec(version=None):
    version = _normalize_java_version(version)
    return version, _JAVA_RUNTIMES[version]

def _java_home(version=None):
    _, spec = _java_runtime_spec(version)
    return spec['dir']

def _java_exe_path(version=None, gui=False):
    home = _java_home(version)
    exe_name = 'javaw.exe' if gui else 'java.exe'
    return os.path.join(home, 'bin', exe_name)

def _find_embedded_jre_zip(version=None):
    version, spec = _java_runtime_spec(version)
    zip_names = spec.get('zip_names') or (f'jre{version}.zip',)
    if IS_FROZEN:
        base_dirs = [
            os.path.join(APP_INSTALL_DIR, 'resources'),
            os.path.join(BASE_DIR, '..'),
            APP_INSTALL_DIR,
        ]
    else:
        base_dirs = [
            os.path.join(APP_INSTALL_DIR, 'build'),
            APP_INSTALL_DIR,
        ]
    for base in base_dirs:
        for zip_name in zip_names:
            p = os.path.normpath(os.path.join(base, zip_name))
            if os.path.isfile(p):
                return p
    return None

def _find_extracted_java_home(root_dir):
    java_bin = os.path.join(root_dir, 'bin', 'java.exe')
    if os.path.isfile(java_bin):
        return root_dir
    try:
        for item in os.listdir(root_dir):
            item_path = os.path.join(root_dir, item)
            if not os.path.isdir(item_path):
                continue
            java_bin = os.path.join(item_path, 'bin', 'java.exe')
            if os.path.isfile(java_bin):
                return item_path
    except Exception:
        pass
    return None

def ensure_java_environment(version=None, progress_cb=None):
    global _JAVA_READY
    if callable(version) and progress_cb is None:
        progress_cb = version
        version = None
    version, spec = _java_runtime_spec(version)
    target_dir = spec['dir']
    target_java = _java_exe_path(version, gui=False)
    label = spec.get('label', f'JRE {version}')

    if version not in _JAVA_READY:
        _JAVA_READY[version] = os.path.isfile(target_java)
    if _JAVA_READY[version]:
        return True

    zip_path = _find_embedded_jre_zip(version)
    if not zip_path:
        log(f"[ERROR] Embedded {label} zip not found. Cannot setup Java environment offline.")
        if progress_cb:
            progress_cb(100, f'{label} not embedded in installation')
        return False

    if progress_cb:
        progress_cb(10, f'Extracting embedded {label}...')
    import zipfile as _zf
    os.makedirs(JAVA_DIR, exist_ok=True)
    extract_dir = os.path.join(JAVA_DIR, f'.extract-{version}-{uuid.uuid4().hex[:8]}')
    try:
        if os.path.isdir(extract_dir):
            shutil.rmtree(extract_dir)
        os.makedirs(extract_dir, exist_ok=True)
        with _zf.ZipFile(zip_path, 'r') as zf:
            if progress_cb:
                progress_cb(20, 'Extracting JRE files...')
            zf.extractall(extract_dir)
            if progress_cb:
                progress_cb(80, 'Organizing JRE directory...')
        extracted_home = _find_extracted_java_home(extract_dir)
        if not extracted_home:
            raise RuntimeError(f'java.exe not found in {zip_path}')
        if os.path.isdir(target_dir):
            shutil.rmtree(target_dir)
        shutil.move(extracted_home, target_dir)
        try:
            if os.path.isdir(extract_dir):
                shutil.rmtree(extract_dir)
        except Exception:
            pass
        _JAVA_READY[version] = os.path.isfile(target_java)
        if _JAVA_READY[version]:
            if progress_cb:
                progress_cb(100, 'Java environment ready')
            return True
        else:
            log(f"[ERROR] Java exe not found after extraction: {target_java}")
            if progress_cb:
                progress_cb(100, 'Java setup incomplete')
            return False
    except Exception as e:
        try:
            if os.path.isdir(extract_dir):
                shutil.rmtree(extract_dir)
        except Exception:
            pass
        log(f"[ERROR] Failed to setup {label} environment: {e}")
        if progress_cb:
            progress_cb(100, f'Java setup failed: {e}')
        return False

def get_java_home(version=None):
    home = _java_home(version)
    if os.path.isdir(home):
        return home
    return None

def get_java_exe(gui=True, version=None):
    if gui:
        javaw = _java_exe_path(version, gui=True)
        if os.path.isfile(javaw):
            return javaw
    java = _java_exe_path(version, gui=False)
    if os.path.isfile(java):
        return java
    return None

def get_plugin_java_version(cfg):
    for key in ('java_version', 'java_runtime', 'required_java', 'required_java_version'):
        if cfg.get(key):
            return _normalize_java_version(cfg.get(key))
    return DEFAULT_JAVA_VERSION

def _get_windows_known_folder(csidl):
    if os.name != 'nt':
        return ''
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        hr = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
        if hr == 0 and buf.value:
            return os.path.normpath(buf.value)
    except Exception:
        pass
    return ''

def _looks_like_app_internal_home(path_value):
    if not path_value:
        return True
    try:
        p = os.path.abspath(os.path.normpath(path_value))
        app_root = os.path.abspath(APP_INSTALL_DIR)
        return p == app_root or p.startswith(app_root + os.sep)
    except Exception:
        return False

def _resolve_windows_gui_home():
    """Return a real Windows profile dir for native Swing apps.

    Podman integration can leave USERPROFILE/HOME pointing at the app-local
    podman-config directory. Java 8's Windows file chooser then asks the Shell
    API for Desktop and can crash before showing the open-file dialog.
    """
    candidates = []
    desktop_parents = set()

    desktop_dir = _get_windows_known_folder(16)  # CSIDL_DESKTOPDIRECTORY
    if desktop_dir and os.path.isdir(desktop_dir):
        parent = os.path.dirname(desktop_dir)
        if parent:
            desktop_parents.add(os.path.abspath(os.path.normpath(parent)))
            candidates.append(parent)

    users_root = 'C:\\Users'
    if os.path.isdir(users_root):
        try:
            for name in os.listdir(users_root):
                if name.lower() in ('public', 'default', 'default user', 'all users'):
                    continue
                candidate = os.path.join(users_root, name)
                if os.path.isdir(os.path.join(candidate, 'Desktop')):
                    candidates.append(candidate)
        except Exception:
            pass

    profile_dir = _get_windows_known_folder(40)  # CSIDL_PROFILE
    if profile_dir:
        candidates.append(profile_dir)

    for key in ('USERPROFILE', 'HOME'):
        val = os.environ.get(key, '')
        if val:
            candidates.append(val)

    hd = os.environ.get('HOMEDRIVE', '')
    hp = os.environ.get('HOMEPATH', '')
    if hd and hp:
        candidates.append(hd + hp)

    username = os.environ.get('USERNAME', '')
    if username:
        candidates.append(os.path.join('C:\\Users', username))

    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.abspath(os.path.normpath(candidate))
        desktop_candidate = os.path.join(candidate, 'Desktop')
        if (os.path.isdir(candidate) and
                not _looks_like_app_internal_home(candidate) and
                (candidate in desktop_parents or os.path.isdir(desktop_candidate))):
            return candidate

    return os.path.expanduser('~')
_R_SYSTEM_DEPS_INSTALLED_KEY = 'r_system_deps_installed'

_image_exists_cache = {}

_r_libs_package_set = set()
_r_libs_package_set_lower = {}

# Packages shipped in the pinned R base image's system library (.Library).
# Keep this as the single source of truth for host-side dependency checks and
# generated R install/pre-flight scripts. These packages must never be copied
# into the persistent user library merely because they are absent from
# R_LIBS_DIR.
_R_IMAGE_LIBRARY_PACKAGES = frozenset({
    'base', 'boot', 'class', 'cluster', 'codetools', 'compiler', 'datasets',
    'foreign', 'graphics', 'grDevices', 'grid', 'KernSmooth', 'lattice',
    'MASS', 'Matrix', 'methods', 'mgcv', 'nlme', 'nnet', 'parallel', 'rpart',
    'spatial', 'splines', 'stats', 'stats4', 'survival', 'tcltk', 'tools',
    'utils',
})
_R_BASE_PACKAGES = frozenset({'R', *_R_IMAGE_LIBRARY_PACKAGES})
_R_BASE_PACKAGES_LOWER = {pkg.lower(): pkg for pkg in _R_BASE_PACKAGES}
_R_BASE_PACKAGES_R = 'c(' + ','.join(json.dumps(pkg) for pkg in sorted(_R_BASE_PACKAGES)) + ')'


def _is_r_image_package(pkg_name):
    return str(pkg_name or '').lower() in _R_BASE_PACKAGES_LOWER

def _scan_r_libs_package_set():
    global _r_libs_package_set, _r_libs_package_set_lower
    pkgs = set()
    lower_map = {}
    if os.path.isdir(R_LIBS_DIR):
        for entry in os.listdir(R_LIBS_DIR):
            if entry.startswith('00LOCK'):
                continue
            desc_path = os.path.join(R_LIBS_DIR, entry, 'DESCRIPTION')
            if os.path.isfile(desc_path):
                pkgs.add(entry)
                lower_map[entry.lower()] = entry
    _r_libs_package_set = pkgs
    _r_libs_package_set_lower = lower_map
    log(f'[SYSTEM] Scanned r_libs: {len(pkgs)} packages found')

_scan_r_libs_package_set()


@contextlib.contextmanager
def _r_cross_process_lock(exclusive):
    """Shared/exclusive OS file lock used across backend processes."""
    os.makedirs(os.path.dirname(R_PACKAGE_LOCK_FILE), exist_ok=True)
    handle = open(R_PACKAGE_LOCK_FILE, 'a+b')
    try:
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class _Overlapped(ctypes.Structure):
                _fields_ = [
                    ('Internal', ctypes.c_size_t),
                    ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wintypes.DWORD),
                    ('OffsetHigh', wintypes.DWORD),
                    ('hEvent', wintypes.HANDLE),
                ]

            overlapped = _Overlapped()
            lock_file_ex = ctypes.windll.kernel32.LockFileEx
            lock_file_ex.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
            lock_file_ex.restype = wintypes.BOOL
            flags = 0x00000002 if exclusive else 0
            os_handle = msvcrt.get_osfhandle(handle.fileno())
            if not lock_file_ex(os_handle, flags, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped)):
                raise ctypes.WinError()
            try:
                yield
            finally:
                unlock_file_ex = ctypes.windll.kernel32.UnlockFileEx
                unlock_file_ex(os_handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped))
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class _RPackageRWLock:
    """Writer-preferring lock for the shared R library.

    R analyses may run together, while install, repair, and delete operations
    are exclusive. Waiting writers prevent new readers from starving a package
    mutation indefinitely.
    """
    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextlib.contextmanager
    def read(self):
        with self._condition:
            while self._writer or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
        try:
            with _r_cross_process_lock(False):
                yield
        finally:
            with self._condition:
                self._readers -= 1
                self._condition.notify_all()

    @contextlib.contextmanager
    def write(self):
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            with _r_cross_process_lock(True):
                yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()


_R_PACKAGE_RW_LOCK = _RPackageRWLock()


def _r_custom_recipe_text():
    lines = [
        f'FROM {R_DOCKER_IMAGE}',
        f'LABEL org.primigenius.r-version="{R_VERSION}"',
        f'LABEL org.primigenius.bioconductor-version="{BIOCONDUCTOR_VERSION}"',
        f'LABEL org.primigenius.base-digest="{R_DOCKER_IMAGE_DIGEST}"',
        f'LABEL org.primigenius.recipe-version="{R_CUSTOM_RECIPE_VERSION}"',
        f'LABEL org.primigenius.recipe-sha256="{R_CUSTOM_RECIPE_SHA256}"',
        'ARG APT_MIRROR=https://archive.ubuntu.com/ubuntu',
        'RUN set -eu; \\',
        '    echo "[APT] Using ${APT_MIRROR}"; \\',
        "    sed -i -E \"s#https?://(archive\\.ubuntu\\.com|security\\.ubuntu\\.com)/ubuntu/?#${APT_MIRROR}/#g\" /etc/apt/sources.list.d/ubuntu.sources; \\",
        '    echo "[APT] Refreshing Ubuntu package indexes..."; \\',
        '    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Languages=none -o Dpkg::Use-Pty=0 update; \\',
        '    echo "[APT] Installing R system libraries..."; \\',
        '    DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Dpkg::Use-Pty=0 install -y --no-install-recommends \\',
        '        locales \\',
    ]
    lines.extend(f'        {dep} \\' for dep in R_SYSTEM_DEPS.split())
    lines.extend([
        '    && \\',
        "    sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && \\",
        '    locale-gen en_US.UTF-8 && \\',
        '    apt-get clean && rm -rf /var/lib/apt/lists/*',
        'ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8',
        '',
    ])
    return '\n'.join(lines)


def _probe_r_apt_mirror(name, mirror, timeout=4.0):
    """Validate a Noble package index and measure initial transfer time."""
    url = mirror.rstrip('/') + '/dists/noble/InRelease'
    started = time.perf_counter()
    req = urllib.request.Request(url, headers={'User-Agent': f'PrimiGenius/{R_VERSION}'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        sample = resp.read(131072)
        content_type = str(resp.headers.get('Content-Type') or '').lower()
    if (not sample or 'text/html' in content_type or
            b'BEGIN PGP SIGNED MESSAGE' not in sample[:4096]):
        raise RuntimeError(f'invalid Ubuntu Noble index from {url}')
    return {
        'name': name,
        'url': mirror.rstrip('/'),
        'latency_ms': int((time.perf_counter() - started) * 1000),
    }


def _ordered_r_apt_mirrors(force=False):
    """Return verified APT mirrors fastest-first, retaining safe fallbacks."""
    now = time.time()
    with _R_APT_MIRROR_LOCK:
        cached = _R_APT_MIRROR_CACHE.get('items')
        if not force and cached and now - float(_R_APT_MIRROR_CACHE.get('time') or 0) < 1800:
            return list(cached)

    mode = _effective_network_mode()
    candidates = list(R_APT_MIRRORS)
    if mode == 'global':
        candidates = [item for item in candidates if item[0] == 'official']

    results = []
    results_lock = threading.Lock()

    def probe(item):
        try:
            result = _probe_r_apt_mirror(*item)
            with results_lock:
                results.append(result)
        except Exception as e:
            log(f'[R-APT] Probe failed for {item[0]}: {e}')

    threads = [threading.Thread(target=probe, args=(item,), daemon=True) for item in candidates]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5.0
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))

    results.sort(key=lambda item: item['latency_ms'])
    ordered = [item['url'] for item in results]
    for _name, url in candidates:
        clean = url.rstrip('/')
        if clean not in ordered:
            ordered.append(clean)
    with _R_APT_MIRROR_LOCK:
        _R_APT_MIRROR_CACHE['items'] = list(ordered)
        _R_APT_MIRROR_CACHE['time'] = time.time()
    if results:
        log(f"[R-APT] Selected {results[0]['name']} ({results[0]['latency_ms']} ms) for custom image build")
    else:
        log('[R-APT] No mirror probe succeeded; build will use ordered fallback sources')
    return ordered


def _custom_r_image_matches_contract(image):
    try:
        labels = ((image.attrs or {}).get('Config') or {}).get('Labels') or {}
        return (
            labels.get('org.primigenius.r-version') == R_VERSION and
            labels.get('org.primigenius.bioconductor-version') == BIOCONDUCTOR_VERSION and
            labels.get('org.primigenius.base-digest') == R_DOCKER_IMAGE_DIGEST and
            labels.get('org.primigenius.recipe-version') == str(R_CUSTOM_RECIPE_VERSION) and
            labels.get('org.primigenius.recipe-sha256') == R_CUSTOM_RECIPE_SHA256
        )
    except Exception:
        return False


def _ensure_pinned_r_base_image(client=None, progress_cb=None):
    """Ensure the immutable R base image through the common resilient pull path."""
    client = client or get_docker_client()
    if not client:
        raise RuntimeError('Podman is not running')
    if _image_exists_locally(client, R_DOCKER_IMAGE):
        _image_exists_cache[R_DOCKER_IMAGE] = True
        return True
    if progress_cb:
        progress_cb(f'[SYSTEM] Pulling pinned R image: {R_DOCKER_IMAGE}')
    pull_image_with_progress(client, R_DOCKER_IMAGE)
    _ensure_image_exists_after_pull(client, R_DOCKER_IMAGE, R_DOCKER_IMAGE)
    _image_exists_cache[R_DOCKER_IMAGE] = True
    return True

def _get_r_image():
    """Return the best available R image: custom image with system deps if built, else base image."""
    if R_CUSTOM_IMAGE in _image_exists_cache:
        return R_CUSTOM_IMAGE
    client = get_docker_client()
    if client:
        try:
            image = client.images.get(R_CUSTOM_IMAGE)
            if _custom_r_image_matches_contract(image):
                _image_exists_cache[R_CUSTOM_IMAGE] = True
                return R_CUSTOM_IMAGE
        except Exception:
            pass
    return R_DOCKER_IMAGE

def _ensure_custom_r_image(client=None, silent=False, progress_cb=None):
    """Build custom R image with system deps pre-installed if it doesn't exist.
    Only called when R base image is already available.
    silent: if True, suppress SSE log output (build logs go to backend console only)."""
    if R_CUSTOM_IMAGE in _image_exists_cache:
        return True
    if not client:
        client = get_docker_client()
    if not client:
        return False
    try:
        image = client.images.get(R_CUSTOM_IMAGE)
        if _custom_r_image_matches_contract(image):
            _image_exists_cache[R_CUSTOM_IMAGE] = True
            return True
    except docker.errors.ImageNotFound:
        pass
    except Exception:
        pass
    _saved_run_id = getattr(_LOG_CONTEXT, 'run_id', None)
    _saved_channel = getattr(_LOG_CONTEXT, 'channel', None)
    if silent:
        _LOG_CONTEXT.run_id = None
        _LOG_CONTEXT.channel = None
    try:
        try:
            _ensure_pinned_r_base_image(client, progress_cb=progress_cb)
        except docker.errors.ImageNotFound:
            return False
        except Exception as e:
            log(f'[WARN] Pinned R base image unavailable: {e}')
            return False
        dockerfile_dir = os.path.join(APP_INSTALL_DIR, '.docker')
        os.makedirs(dockerfile_dir, exist_ok=True)
        dockerfile_path = os.path.join(dockerfile_dir, 'Dockerfile.r_base')
        with open(dockerfile_path, 'w', encoding='utf-8') as f:
            f.write(_r_custom_recipe_text())
        msg = f'[SYSTEM] Building custom R image {R_CUSTOM_IMAGE} with system deps...'
        log(msg)
        if progress_cb:
            progress_cb(msg)
        apt_mirrors = _ordered_r_apt_mirrors()
        build_errors = []
        for attempt, apt_mirror in enumerate(apt_mirrors, start=1):
            msg = f'[R-APT] Build source {attempt}/{len(apt_mirrors)}: {apt_mirror}'
            log(msg)
            if progress_cb:
                progress_cb(msg)
            try:
                stream = client.api.build(
                    path=dockerfile_dir,
                    dockerfile=dockerfile_path,
                    tag=R_CUSTOM_IMAGE,
                    rm=True,
                    forcerm=True,
                    decode=True,
                    pull=False,
                    buildargs={'APT_MIRROR': apt_mirror},
                )
                for chunk in stream:
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get('stream'):
                        line = str(chunk.get('stream')).strip()
                        if line:
                            msg = f'[BUILD] {line}'
                            log(msg)
                            if progress_cb:
                                progress_cb(msg)
                    if chunk.get('error'):
                        raise RuntimeError(str(chunk.get('error')))
                break
            except Exception as build_error:
                build_errors.append(f'{apt_mirror}: {build_error}')
                if attempt >= len(apt_mirrors):
                    raise RuntimeError('all APT build sources failed: ' + '; '.join(build_errors))
                msg = f'[R-APT] Source failed, switching mirror: {build_error}'
                log(msg)
                if progress_cb:
                    progress_cb(msg)
        built_image = client.images.get(R_CUSTOM_IMAGE)
        if not _custom_r_image_matches_contract(built_image):
            raise RuntimeError('custom R image labels do not match the pinned runtime contract')
        validation_container = None
        try:
            validation_container = client.containers.run(
                image=R_CUSTOM_IMAGE,
                command=['Rscript', '-e', f'stopifnot(as.character(getRversion()) == "{R_VERSION}")'],
                detach=True,
                platform='linux/amd64',
                log_config={'type': 'json-file'},
            )
            validation_result = _robust_container_wait(validation_container, timeout=120)
            validation_exit = int(validation_result.get('StatusCode', -1)) if isinstance(validation_result, dict) else -1
            if validation_exit != 0:
                raise RuntimeError(f'custom R image failed the pinned R version smoke test (exit {validation_exit})')
        finally:
            if validation_container is not None:
                try:
                    validation_container.remove(force=True)
                except Exception:
                    pass
        _image_exists_cache[R_CUSTOM_IMAGE] = True
        msg = f'[SYSTEM] Custom R image {R_CUSTOM_IMAGE} built successfully'
        log(msg)
        if progress_cb:
            progress_cb(msg)
        return True
    except Exception as e:
        msg = f'[WARN] Failed to build custom R image: {e}'
        log(msg)
        if progress_cb:
            progress_cb(msg)
        _image_exists_cache.pop(R_CUSTOM_IMAGE, None)
        return False
    finally:
        if silent:
            _LOG_CONTEXT.run_id = _saved_run_id
            _LOG_CONTEXT.channel = _saved_channel

R_REPOSITORY_BUNDLES = (
    ('tuna', 'https://mirrors.tuna.tsinghua.edu.cn/CRAN', 'https://mirrors.tuna.tsinghua.edu.cn/bioconductor'),
    ('ustc', 'https://mirrors.ustc.edu.cn/CRAN', 'https://mirrors.ustc.edu.cn/bioc'),
    ('nju', 'https://mirrors.nju.edu.cn/CRAN', 'https://mirrors.nju.edu.cn/bioconductor'),
    ('zju', 'https://mirrors.zju.edu.cn/CRAN', 'https://mirrors.zju.edu.cn/bioconductor'),
    ('global', 'https://cloud.r-project.org', 'https://bioconductor.org'),
)
R_RUNTIME = {'repos': 'auto'}
_R_REPO_AUTO_CACHE = {'selection': None, 'time': 0.0}
_R_REPO_AUTO_LOCK = threading.Lock()


def _normalize_cran_repo_url(url):
    raw = str(url or '').strip()
    if not raw:
        raw = 'https://cloud.r-project.org'
    if not re.match(r'^https?://', raw, re.IGNORECASE):
        raw = f'https://{raw}'
    return raw.rstrip('/')


def _resolve_bioc_mirror_from_cran(repo_url):
    cran_repo = _normalize_cran_repo_url(repo_url)
    host = ''
    try:
        host = (urllib.parse.urlparse(cran_repo).netloc or '').lower()
    except Exception:
        host = ''

    host_to_bioc = {
        'mirrors.tuna.tsinghua.edu.cn': 'https://mirrors.tuna.tsinghua.edu.cn/bioconductor',
        'mirrors.ustc.edu.cn': 'https://mirrors.ustc.edu.cn/bioc',
        'mirrors.nju.edu.cn': 'https://mirrors.nju.edu.cn/bioconductor',
        'mirrors.zju.edu.cn': 'https://mirrors.zju.edu.cn/bioconductor',
    }
    return host_to_bioc.get(host, 'https://bioconductor.org')


def _resolve_r_repo_bundle(repo_url):
    if str(repo_url or '').strip().lower() == 'auto':
        return _select_auto_r_repo_bundle()
    cran_repo = _normalize_cran_repo_url(repo_url)
    bioc_mirror = _resolve_bioc_mirror_from_cran(cran_repo)
    return cran_repo, bioc_mirror


def _probe_r_repo_bundle(name, cran_repo, bioc_mirror, timeout=4.0):
    urls = (
        cran_repo.rstrip('/') + '/src/contrib/PACKAGES.gz',
        bioc_mirror.rstrip('/') + f'/packages/{BIOCONDUCTOR_VERSION}/bioc/src/contrib/PACKAGES.gz',
    )
    started = time.perf_counter()
    for url in urls:
        req = urllib.request.Request(url, headers={'User-Agent': f'PrimiGenius/{R_VERSION}'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            head = resp.read(256)
            content_type = str(resp.headers.get('Content-Type') or '').lower()
        if (not head or 'text/html' in content_type or
                head.lstrip().lower().startswith(b'<!doctype html') or
                (url.endswith('.gz') and not head.startswith(b'\x1f\x8b'))):
            raise RuntimeError(f'invalid package index from {url}')
    return {
        'name': name,
        'cran': cran_repo,
        'bioc': bioc_mirror,
        'latency_ms': int((time.perf_counter() - started) * 1000),
    }


def _select_auto_r_repo_bundle(force=False):
    now = time.time()
    with _R_REPO_AUTO_LOCK:
        cached = _R_REPO_AUTO_CACHE.get('selection')
        if not force and cached and now - float(_R_REPO_AUTO_CACHE.get('time') or 0) < 1800:
            return cached['cran'], cached['bioc']

    results = []
    result_lock = threading.Lock()

    def probe(bundle):
        try:
            result = _probe_r_repo_bundle(*bundle)
            with result_lock:
                results.append(result)
        except Exception as e:
            log(f'[R-REPO] Auto probe failed for {bundle[0]}: {e}')

    threads = [threading.Thread(target=probe, args=(bundle,), daemon=True) for bundle in R_REPOSITORY_BUNDLES]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5.0
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))

    if results:
        selected = min(results, key=lambda item: item['latency_ms'])
    else:
        selected = {
            'name': 'global-fallback',
            'cran': 'https://cloud.r-project.org',
            'bioc': 'https://bioconductor.org',
            'latency_ms': None,
        }
    with _R_REPO_AUTO_LOCK:
        _R_REPO_AUTO_CACHE['selection'] = selected
        _R_REPO_AUTO_CACHE['time'] = time.time()
    log(f'[R-REPO] Auto selected {selected["name"]}: CRAN={selected["cran"]}, Bioconductor={selected["bioc"]}')
    return selected['cran'], selected['bioc']


def _load_r_runtime_state():
    try:
        if os.path.isfile(R_RUNTIME_STATE_FILE):
            with open(R_RUNTIME_STATE_FILE, 'r', encoding='utf-8') as handle:
                data = json.load(handle) or {}
            return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f'[WARN] Failed to load R runtime state: {e}')
    return {}


def _save_r_runtime_state(**updates):
    state = _load_r_runtime_state()
    state.update(updates)
    state.update({
        'runtime_schema': 2,
        'r_version': R_VERSION,
        'bioconductor_version': BIOCONDUCTOR_VERSION,
        'base_image': R_DOCKER_IMAGE,
        'base_image_digest': R_DOCKER_IMAGE_DIGEST,
        'custom_image': R_CUSTOM_IMAGE,
        'recipe_version': R_CUSTOM_RECIPE_VERSION,
        'recipe_sha256': R_CUSTOM_RECIPE_SHA256,
        'library_generation': R_LIBRARY_GENERATION,
        'updated_at': int(time.time()),
    })
    tmp_path = R_RUNTIME_STATE_FILE + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, R_RUNTIME_STATE_FILE)
    return state


_saved_r_runtime_state = _load_r_runtime_state()
_saved_repo_mode = str(_saved_r_runtime_state.get('repo_mode') or 'auto').strip()
R_RUNTIME['repos'] = _saved_repo_mode if _saved_repo_mode else 'auto'


def _complete_r_runtime_migration(client=None, lock_held=False):
    """Retire legacy R state only after the pinned runtime has succeeded."""
    if not lock_held:
        with _R_PACKAGE_RW_LOCK.write():
            return _complete_r_runtime_migration(client, lock_held=True)
    current_library = os.path.normcase(os.path.abspath(R_LIBS_DIR))
    legacy_libraries = []
    for candidate in (R_LEGACY_LIBS_DIR, R_LEGACY_BACKEND_LIBS_DIR):
        path = os.path.abspath(candidate)
        key = os.path.normcase(path)
        if (key != current_library and os.path.basename(path).lower() == 'r_libs' and
                all(os.path.normcase(existing) != key for existing in legacy_libraries)):
            legacy_libraries.append(path)

    state = _load_r_runtime_state()
    if (state.get('migration_complete') and state.get('cleanup_complete') and
            state.get('library_generation') == R_LIBRARY_GENERATION and
            not any(os.path.isdir(path) for path in legacy_libraries)):
        return
    client = client or get_docker_client()
    if not client or not _image_exists_locally(client, R_DOCKER_IMAGE):
        return
    try:
        image = client.images.get(R_CUSTOM_IMAGE)
        if not _custom_r_image_matches_contract(image):
            return
    except Exception:
        return

    validation_container = None
    try:
        validation_code = (
            '.libPaths(c("/r_libs", .libPaths())); '
            'stopifnot(as.character(getRversion()) == "' + R_VERSION + '"); '
            'stopifnot(requireNamespace("BiocManager", quietly=TRUE)); '
            'stopifnot(as.character(BiocManager::version()) == "' + BIOCONDUCTOR_VERSION + '")'
        )
        validation_container = client.containers.run(
            image=R_CUSTOM_IMAGE,
            command=['Rscript', '-e', validation_code],
            mounts=[Mount(target='/r_libs', source=R_LIBS_DIR, type='bind', read_only=True)],
            environment=R_LIBS_ENV,
            detach=True,
            platform='linux/amd64',
            log_config={'type': 'json-file'},
        )
        validation_result = _robust_container_wait(validation_container, timeout=120)
        validation_exit = int(validation_result.get('StatusCode', -1)) if isinstance(validation_result, dict) else -1
        if validation_exit != 0:
            log(f'[WARN] R migration validation exited with code {validation_exit}')
            return
    except Exception as e:
        log(f'[WARN] R migration validation not yet ready: {e}')
        return
    finally:
        if validation_container is not None:
            try:
                validation_container.remove(force=True)
            except Exception:
                pass

    cleanup = {
        'legacy_library_removed': False,
        'legacy_libraries_removed': [],
        'legacy_libraries_pending': [],
        'legacy_tags_removed': [],
        'legacy_tags_pending': [],
    }
    for legacy_library in legacy_libraries:
        if not os.path.isdir(legacy_library):
            continue
        try:
            shutil.rmtree(legacy_library)
            cleanup['legacy_library_removed'] = True
            cleanup['legacy_libraries_removed'].append(legacy_library)
            log(f'[SYSTEM] Removed legacy R library after successful migration: {legacy_library}')
        except Exception as e:
            cleanup['legacy_libraries_pending'].append(legacy_library)
            log(f'[WARN] Legacy R library cleanup pending for {legacy_library}: {e}')

    for old_ref in (R_LEGACY_DOCKER_IMAGE,) + tuple(R_LEGACY_CUSTOM_IMAGES):
        try:
            client.images.remove(image=old_ref, force=False, noprune=True)
            cleanup['legacy_tags_removed'].append(old_ref)
            log(f'[SYSTEM] Removed legacy R image tag after successful migration: {old_ref}')
        except Exception:
            try:
                client.images.get(old_ref)
                cleanup['legacy_tags_pending'].append(old_ref)
            except Exception:
                pass
    cleanup_complete = (
        not any(os.path.isdir(path) for path in legacy_libraries) and
        not cleanup['legacy_tags_pending']
    )
    try:
        _save_r_runtime_state(
            migration_complete=True,
            cleanup_complete=cleanup_complete,
            migrated_at=int(time.time()),
            cleanup=cleanup,
            repo_mode=R_RUNTIME.get('repos', 'auto'),
        )
    except Exception as e:
        log(f'[WARN] Failed to save R migration cleanup state: {e}')








# install jobs: job_id -> {'status': 'pending/running/done/failed', 'log': deque([...]), 'exit_code': int}
INSTALL_JOBS = {}


def ensure_r_docker_image():
    r_img = _get_r_image()
    if r_img in _image_exists_cache:
        return True, 'R available'
    client = get_docker_client()
    if not client:
        return False, 'Docker is not running'
    try:
        if r_img == R_DOCKER_IMAGE:
            _ensure_pinned_r_base_image(client)
        else:
            client.images.get(r_img)
        _image_exists_cache[r_img] = True
        return True, 'R available'
    except docker.errors.ImageNotFound:
        pass
    except Exception as e:
        return False, f'Docker error: {e}'
    if r_img != R_DOCKER_IMAGE:
        try:
            client.images.get(R_DOCKER_IMAGE)
            _image_exists_cache[R_DOCKER_IMAGE] = True
            return True, 'R available'
        except docker.errors.ImageNotFound:
            pass
        except Exception as e:
            return False, f'Docker error: {e}'
    return False, 'image_not_found'


def _run_r_container_with_realtime_logs(client, command, mounts, environment, line_handler=None, poll_seconds=0.5, install_system_deps=False):
    """Run an R container in detached mode and stream logs without Docker logs follow.
    If install_system_deps is True, prepend apt-get install for common R system dependencies."""
    container = None
    collected = []

    def _on_line(line):
        text = str(line or '').rstrip('\r\n')
        if not text:
            return
        collected.append(text)
        if line_handler:
            line_handler(text)

    try:
        r_image = _get_r_image()
        if r_image != R_CUSTOM_IMAGE and install_system_deps:
            if _ensure_custom_r_image(client, silent=True):
                r_image = R_CUSTOM_IMAGE
        needs_deps_prefix = install_system_deps and r_image != R_CUSTOM_IMAGE
        if needs_deps_prefix:
            deps_check = (
                'if ! dpkg -s libuv1-dev >/dev/null 2>&1; then '
                '  echo "[SYS-DEPS] Installing R system dependencies..."; '
                f'  {_R_APT_HTTPS_SETUP} '
                f'  apt-get -o Acquire::Retries=5 update -qq && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 install -y -qq locales {R_SYSTEM_DEPS} 2>/dev/null; '
                f'  {_R_LOCALE_SETUP} '
                '  echo "[SYS-DEPS] System dependencies installed."; '
                'fi; '
            )
            if isinstance(command, list):
                command = ['bash', '-c', deps_check + 'stdbuf -oL -eL ' + ' '.join(f"'{c}'" if ' ' in c else c for c in command)]
            elif isinstance(command, str):
                command = ['bash', '-c', deps_check + command]
        elif isinstance(command, list) and command and str(command[0]).lower() == 'rscript':
            command = ['bash', '-c', 'stdbuf -oL -eL ' + ' '.join(f"'{c}'" if ' ' in c else c for c in command)]

        container = client.containers.run(
            image=r_image,
            command=command,
            mounts=mounts,
            environment=environment,
            detach=True,
            user='0',
            platform='linux/amd64',
            log_config={'type': 'json-file'}
        )
        _stream_container_logs_realtime(container, _on_line, poll_seconds=poll_seconds)
        result = _robust_container_wait(container, timeout=None)
        exit_code = int(result.get('StatusCode', -1)) if isinstance(result, dict) else -1
        return exit_code, collected
    finally:
        if container is not None:
            try:
                container.remove()
            except Exception:
                pass


def _ensure_svglite_installed(client):
    mounts = [Mount(target='/r_libs', source=R_LIBS_DIR, type='bind')]
    check_code = 'if (requireNamespace("svglite", quietly=TRUE)) cat("SVGLITE_READY") else cat("SVGLITE_MISSING")'

    try:
        check_exit, check_lines = _run_r_container_with_realtime_logs(
            client,
            ["Rscript", "-e", check_code],
            mounts=mounts,
            environment=R_LIBS_ENV,
            line_handler=None,
            poll_seconds=0.5,
            install_system_deps=True
        )
        check_text = '\n'.join(check_lines)
        if check_exit == 0 and 'SVGLITE_READY' in check_text:
            log('[SYSTEM] svglite package is already installed.')
            return

        if check_exit != 0:
            log('[WARN] svglite precheck returned non-zero exit code, switching to auto-install flow.')

        log('[SYSTEM] svglite package not found, installing automatically...')

        def _install():
            with _R_PACKAGE_RW_LOCK.write():
                return _install_locked()

        def _install_locked():
            try:
                repos, _ = _resolve_r_repo_bundle(R_RUNTIME.get('repos', 'auto'))
                install_code = (
                    f'selected_repo <- "{repos}"; '
                    'ppm_repos <- c(CRAN=selected_repo); '
                    'options(repos = ppm_repos); '
                    'options(HTTPUserAgent = sprintf("R/%s R (%s)", getRversion(), paste(getRversion(), R.version["platform"], R.version["arch"], R.version["os"]))); '
                    'options(timeout=600, download.file.method="libcurl"); '
                    'ncpus <- suppressWarnings(as.integer(parallel::detectCores(logical=FALSE))); '
                    'if (!is.finite(ncpus) || is.na(ncpus)) ncpus <- 1L; '
                    'ncpus <- max(1L, min(4L, ncpus)); '
                    'Sys.setenv(MAKEFLAGS=paste0("-j", ncpus)); '
                    'cat(paste0("Using CRAN mirror: ", getOption("repos")[["CRAN"]], "\\n")); '
                    'install.packages("svglite", lib="/r_libs", dependencies=c("Depends","Imports","LinkingTo"), Ncpus=ncpus)'
                )

                install_exit, _ = _run_r_container_with_realtime_logs(
                    client,
                    ["Rscript", "-e", install_code],
                    mounts=mounts,
                    environment=R_LIBS_ENV,
                    line_handler=lambda line: log(f'[SVGLITE] {line}'),
                    poll_seconds=0.5,
                    install_system_deps=True
                )
                if install_exit != 0:
                    log(f'[WARN] Failed to auto-install svglite (exit code {install_exit}).')
                    return

                verify_exit, verify_lines = _run_r_container_with_realtime_logs(
                    client,
                    ["Rscript", "-e", check_code],
                    mounts=mounts,
                    environment=R_LIBS_ENV,
                    line_handler=None,
                    poll_seconds=0.5,
                    install_system_deps=True
                )
                verify_text = '\n'.join(verify_lines)
                if verify_exit == 0 and 'SVGLITE_READY' in verify_text:
                    log('[SYSTEM] svglite package installed successfully.')
                    _bump_r_libs_revision('auto-install-svglite')
                else:
                    log('[WARN] svglite auto-install completed but verification failed.')
            except Exception:
                log('[WARN] Failed to auto-install svglite.')

        threading.Thread(target=_install, daemon=True).start()
    except Exception:
        log('[WARN] svglite precheck unavailable, continuing startup.')


def _enqueue_job_log(job_id, line):
    job = INSTALL_JOBS.get(job_id)
    if not job: return
    dq = job['log']
    dq.append(line)
    if len(dq) > 1000:
        while len(dq) > 800:
            dq.popleft()
    run_id = job.get('sse_run_id')
    if run_id:
        _publish_run_log_event(run_id, 'R', line)


def _create_r_script_mount(r_code, prefix='r_job'):
    script_dir = os.path.join(tempfile.gettempdir(), 'primigenius_rscripts')
    os.makedirs(script_dir, exist_ok=True)
    safe_prefix = re.sub(r'[^A-Za-z0-9._-]+', '_', str(prefix or '')).strip('._-')[:80] or 'r_job'
    script_name = f'{safe_prefix}_{uuid.uuid4().hex}.R'
    host_path = os.path.join(script_dir, script_name)
    script_text = str(r_code or '')
    # R 代码由大量片段拼接而成，默认是超长单行。这里强制格式化成多行，避免单行分隔符导致的 parse 错误。
    script_text = script_text.replace('; ', ';\n')
    script_text = re.sub(r'\{\s+(?=[A-Za-z_])', '{\n', script_text)
    script_text = re.sub(r'\}\s+(?!\s*else\b)(?=[A-Za-z_])', '}\n', script_text)
    if not script_text.endswith('\n'):
        script_text += '\n'
    with open(host_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(script_text)
    return host_path, Mount(target='/r_job_script', source=script_dir, type='bind', read_only=True), f'/r_job_script/{script_name}'


def _stream_container_logs_realtime(container, line_handler, poll_seconds=0.5):
    pending = ''

    def _emit_text(text):
        nonlocal pending
        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]+', '', str(text or ''))
        pending += clean.replace('\r\n', '\n').replace('\r', '\n')
        parts = pending.split('\n')
        pending = parts.pop() if parts else ''
        for line in parts:
            line = line.rstrip('\r')
            if line.strip():
                line_handler(line)

    # Preferred path: attach stream with demux to avoid docker multiplexed binary framing noise.
    try:
        stream = container.attach(stream=True, logs=True, stdout=True, stderr=True, demux=True)
        for chunk in stream:
            if isinstance(chunk, tuple):
                stdout_chunk, stderr_chunk = chunk
                if stdout_chunk:
                    _emit_text(stdout_chunk.decode('utf-8', errors='replace'))
                if stderr_chunk:
                    _emit_text(stderr_chunk.decode('utf-8', errors='replace'))
            else:
                text = chunk.decode('utf-8', errors='replace') if isinstance(chunk, bytes) else str(chunk)
                _emit_text(text)
        if pending.strip():
            line_handler(pending.strip())
        return
    except TypeError:
        pass
    except Exception:
        pass

    # Fallback path: polling full logs and emitting only incremental lines.
    seen = 0
    warned = False

    def _read_once():
        nonlocal seen, warned
        try:
            out = container.logs(stdout=True, stderr=True)
        except Exception:
            if not warned:
                warned = True
            return
        if not out:
            return
        text = out.decode('utf-8', errors='replace') if isinstance(out, bytes) else str(out)
        lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        for line in lines[seen:]:
            line = line.rstrip('\r')
            if line.strip():
                line_handler(line)
        seen = len(lines)

    while True:
        _read_once()
        try:
            container.reload()
            if container.status != 'running':
                break
        except Exception:
            break
        time.sleep(poll_seconds)
    _read_once()






def _run_r_install_job(job_id, pkgs, repo=None, local_path=None, skip_repair=False):
    job = INSTALL_JOBS[job_id]
    _enqueue_job_log(job_id, '[SYSTEM] Waiting for exclusive R package-library lock...')
    with _R_PACKAGE_RW_LOCK.write():
        _enqueue_job_log(job_id, '[SYSTEM] Acquired exclusive R package-library lock; dependencies will be rechecked.')
        return _run_r_install_job_locked(job_id, pkgs, repo, local_path, skip_repair)


def _run_r_install_job_locked(job_id, pkgs, repo=None, local_path=None, skip_repair=False):
    """Install R packages inside Docker container with persistent volume."""
    _LOG_CONTEXT.channel = 'R'
    _LOG_CONTEXT.run_id = None
    job = INSTALL_JOBS[job_id]
    job['status'] = 'running'
    client = get_docker_client()
    if not client:
        job['status'] = 'failed'
        job['exit_code'] = -1
        _enqueue_job_log(job_id, 'Docker is not running')
        log('Docker is not running')
        return

    repos, bioc_mirror = _resolve_r_repo_bundle(repo or R_RUNTIME.get('repos'))
    script_host_path = None
    try:
        # Ensure the immutable base image through the optimized common pull path.
        if R_DOCKER_IMAGE not in _image_exists_cache:
            _ensure_pinned_r_base_image(
                client,
                progress_cb=lambda line: _enqueue_job_log(job_id, line)
            )

        mounts = [Mount(target='/r_libs', source=R_LIBS_DIR, type='bind')]
        env = R_LIBS_ENV.copy()

        if local_path:
            pkg_dir = os.path.dirname(os.path.abspath(local_path))
            pkg_name = os.path.basename(local_path)
            mounts.append(Mount(target='/pkg_src', source=pkg_dir, type='bind', read_only=False))

            # Detect if it's a GitHub-style zip (contains source dir, not a tar.gz)
            if pkg_name.lower().endswith('.zip'):
                # Unzip inside container then install with devtools/remotes
                r_code = (
                    'tryCatch({'
                    f'  unzip("/pkg_src/{pkg_name}", exdir="/tmp/rpkg");'
                    '  dirs <- list.dirs("/tmp/rpkg", recursive=FALSE);'
                    '  pkg_path <- dirs[1];'
                    '  if (file.exists(file.path(pkg_path, "DESCRIPTION"))) {'
                    '    if (requireNamespace("remotes", quietly=TRUE)) {'
                    '      remotes::install_local(pkg_path, lib="/r_libs", upgrade="never")'
                    '    } else {'
                    '      install.packages("remotes", lib="/r_libs");'
                    '      remotes::install_local(pkg_path, lib="/r_libs", upgrade="never")'
                    '    }'
                    '  } else {'
                    f'    install.packages("/pkg_src/{pkg_name}", repos=NULL, type="source", lib="/r_libs")'
                    '  }'
                    '}, error=function(e) { cat(paste0("ERROR: ", e$message, "\\n")); quit(status=1) })'
                )
            elif pkg_name.lower().endswith('.tar.gz') or pkg_name.lower().endswith('.tgz'):
                r_code = f'install.packages("/pkg_src/{pkg_name}", repos=NULL, type="source", lib="/r_libs")'
            else:
                r_code = f'install.packages("/pkg_src/{pkg_name}", repos=NULL, type="source", lib="/r_libs")'
        else:
            cols = ','.join([f'"{p}"' for p in pkgs])
            pkg_vec = f'c({cols})'
            r_code = (
                f'selected_repo <- "{repos}"; '
                f'selected_bioc_mirror <- "{bioc_mirror}"; '
                'ppm_repos <- c(CRAN=selected_repo); '
                'options(repos = ppm_repos); '
                'cat(paste0("Using CRAN mirror: ", getOption("repos")[["CRAN"]], "\\n")); '
                'options(HTTPUserAgent = sprintf("R/%s R (%s)", getRversion(), paste(getRversion(), R.version["platform"], R.version["arch"], R.version["os"]))); '
                'options(timeout=600, download.file.method="libcurl"); '
                'options(warn=1); '
                '.libPaths(c("/r_libs", .libPaths())); '
                'ncpus <- suppressWarnings(as.integer(parallel::detectCores(logical=FALSE))); '
                'if (!is.finite(ncpus) || is.na(ncpus)) ncpus <- 1L; '
                'ncpus <- max(1L, min(4L, ncpus)); '
                'Sys.setenv(MAKEFLAGS=paste0("-j", ncpus)); '
                'if (!requireNamespace("BiocManager", quietly = TRUE)) { '
                '  cat("Installing BiocManager first...\\n"); '
                '  install.packages("BiocManager", lib="/r_libs", Ncpus=ncpus) }; '
                'suppressPackageStartupMessages(library(BiocManager)); '
                f'target_bioc_version <- "{BIOCONDUCTOR_VERSION}"; '
                'options(BioC_mirror=selected_bioc_mirror); '
                f'bioc_repos <- BiocManager::repositories(version=target_bioc_version, site_repository="{repos}"); '
                'merged_repos <- bioc_repos; '
                'if ("CRAN" %in% names(merged_repos) && "CRAN" %in% names(ppm_repos)) merged_repos["CRAN"] <- ppm_repos[["CRAN"]]; '
                'if (!("UserMirror" %in% names(merged_repos))) merged_repos <- c(merged_repos, UserMirror=selected_repo); '
                'options(repos = merged_repos); '
                'cat(paste0("Using Bioconductor mirror: ", selected_bioc_mirror, "\\n")); '
                'cat(paste0("Resolved repositories: ", paste(names(getOption("repos")), getOption("repos"), sep="=", collapse=" | "), "\\n")); '
                'safe_available_packages <- function(repos, label) { '
                '  cat(paste0("Refreshing package index: ", label, "\\n")); '
                '  out <- tryCatch({ '
                '    setTimeLimit(elapsed=60, transient=TRUE); '
                '    on.exit(setTimeLimit(cpu=Inf, elapsed=Inf, transient=FALSE), add=TRUE); '
                '    available.packages(repos=repos) '
                '  }, error=function(e) { cat("WARN: package index unavailable for ", label, ": ", conditionMessage(e), "\\n", sep=""); NULL }); '
                '  if (is.null(out)) cat(paste0("WARN: continuing without package index for ", label, "\\n")) else cat(paste0("Package index ready for ", label, ": ", nrow(out), " entries\\n")); '
                '  out '
                '}; '
                'available_db <- safe_available_packages(bioc_repos, "CRAN+Bioc"); '
                'bioc_only_repos <- bioc_repos[names(bioc_repos) != "CRAN"]; '
                'available_names <- if (is.null(available_db)) character(0) else rownames(available_db); '
                f'base_pkgs <- {_R_BASE_PACKAGES_R}; '
                'dep_types <- c("Depends","Imports","LinkingTo"); '
                'canonicalize_pkg <- function(pkg) { '
                '  if (is.null(pkg) || is.na(pkg) || !nzchar(pkg)) return(pkg); '
                '  if (length(available_names) == 0) return(pkg); '
                '  hit <- available_names[tolower(available_names) == tolower(pkg)]; '
                '  if (length(hit) > 0) { '
                '    if (!identical(hit[[1]], pkg)) cat(paste0("Package name normalized: ", pkg, " -> ", hit[[1]], "\\n")); '
                '    return(hit[[1]]) '
                '  }; '
                '  pkg '
                '}; '
                'normalize_pkg_vec <- function(pkgs) { '
                '  unique(vapply(pkgs, canonicalize_pkg, character(1), USE.NAMES=FALSE)) '
                '}; '
                'pkg_is_ready <- function(pkg) { '
                '  pkg <- canonicalize_pkg(pkg); '
                '  isTRUE(tryCatch(requireNamespace(pkg, quietly=TRUE), error=function(e) FALSE)) '
                '}; '
                'is_bioc_pkg <- function(pkg) { '
                '  pkg <- canonicalize_pkg(pkg); '
                '  if (is.null(available_db) || !("Repository" %in% colnames(available_db)) || !(pkg %in% rownames(available_db))) return(NA); '
                '  repo_value <- available_db[pkg, "Repository"]; '
                '  isTRUE(grepl("bioconductor", repo_value, ignore.case=TRUE)) '
                '}; '
                'resolve_chain <- function(pkgs) { '
                '  pkgs <- normalize_pkg_vec(unique(pkgs[nzchar(pkgs) & !is.na(pkgs)])); '
                '  if (length(pkgs) == 0 || is.null(available_db)) return(character(0)); '
                '  deps <- tryCatch(tools::package_dependencies(pkgs, db=available_db, which=c("Depends","Imports","LinkingTo"), recursive=TRUE), error=function(e) list()); '
                '  normalize_pkg_vec(unique(unlist(deps, use.names=FALSE))) '
                '}; '
                'cleanup_partial_dirs <- function() { '
                '  locks <- list.dirs("/r_libs", recursive=FALSE, full.names=TRUE); '
                '  locks <- locks[grepl("00LOCK", basename(locks))]; '
                '  for (lk in locks) { '
                '    subdirs <- list.dirs(lk, recursive=FALSE, full.names=TRUE); '
                '    for (sd in subdirs) { '
                '      tgt <- file.path("/r_libs", basename(sd)); '
                '      if (!dir.exists(tgt)) { '
                '        cat(paste0("Restoring ", basename(sd), " from lock backup\\n")); '
                '        file.rename(sd, tgt) '
                '      } '
                '    }; '
                '    unlink(lk, recursive=TRUE, force=TRUE) '
                '  }; '
                '  staged <- file.path("/r_libs", "00new"); '
                '  if (dir.exists(staged)) { '
                '    staged_pkgs <- list.dirs(staged, recursive=FALSE, full.names=TRUE); '
                '    for (sd in staged_pkgs) { '
                '      tgt <- file.path("/r_libs", basename(sd)); '
                '      if (!dir.exists(tgt)) { '
                '        cat(paste0("Restoring ", basename(sd), " from 00new staging\\n")); '
                '        file.rename(sd, tgt) '
                '      } '
                '    }; '
                '    unlink(staged, recursive=TRUE, force=TRUE) '
                '  } '
                '}; '
                'install_single_pkg <- function(pkg, force=FALSE, label="install") { '
                '  pkg <- canonicalize_pkg(pkg); '
                '  cat(paste0("Installing single package: ", pkg, if (force) " (force=TRUE, type=source)" else "", "\\n")); '
                '  tryCatch({ '
                '    withCallingHandlers('
                '      BiocManager::install(pkg, lib="/r_libs", version=target_bioc_version, update=FALSE, ask=FALSE, force=force, type="source", dependencies=dep_types), '
                '      warning=function(w) { cat("WARN: ", conditionMessage(w), "\\n", sep=""); invokeRestart("muffleWarning") }'
                '    ) '
                '  }, error=function(e) { cat("ERR: ", conditionMessage(e), "\\n", sep="") }); '
                '  pkg_is_ready(pkg) '
                '}; '
                'install_targets <- function(targets, force=FALSE, label="install", use_bioc_only=FALSE) { '
                '  targets <- unique(targets[nzchar(targets) & !is.na(targets) & !(targets %in% base_pkgs)]); '
                '  if (length(targets) == 0) return(invisible(TRUE)); '
                '  install_type <- if (force) "source" else getOption("pkgType"); '
                '  cat(paste0("Installing dependency set for ", label, ": ", paste(targets, collapse=", "), if (force) " (force=TRUE, type=source)" else "", if (use_bioc_only) " (Bioc-only repos)" else "", "\\n")); '
                '  if (use_bioc_only) { '
                '    old_repos <- getOption("repos"); '
                '    options(repos=bioc_only_repos); '
                '  }; '
                '  withCallingHandlers('
                '    tryCatch({ '
                '      BiocManager::install(targets, lib="/r_libs", version=target_bioc_version, update=FALSE, ask=FALSE, force=force, dependencies=dep_types, Ncpus=ncpus, type=install_type, site_repository=if (use_bioc_only) NULL else selected_repo) '
                '    }, error=function(e) { cat("ERR: ", conditionMessage(e), "\\n", sep="") }), '
                '    warning=function(w) { cat("WARN: ", conditionMessage(w), "\\n", sep=""); invokeRestart("muffleWarning") }'
                '  ); '
                '  if (use_bioc_only) { options(repos=old_repos) }; '
                '  unresolved <- targets[!vapply(targets, pkg_is_ready, logical(1))]; '
                '  unresolved '
                '}; '
                'install_with_fallback <- function(targets, label="install") { '
                '  targets <- normalize_pkg_vec(unique(targets[nzchar(targets) & !is.na(targets) & !(targets %in% base_pkgs)])); '
                '  if (length(targets) == 0) return(invisible(TRUE)); '
                '  cat(paste0("=== Primary install attempt (CRAN+Bioc): ", paste(targets, collapse=", "), " ===\\n")); '
                '  unresolved_1 <- install_targets(targets, force=FALSE, label=label); '
                '  if (length(unresolved_1) == 0) return(invisible(TRUE)); '
                '  cat(paste0("=== Failed packages, retrying one by one with source: ", paste(unresolved_1, collapse=", "), " ===\\n")); '
                '  for (pkg in unresolved_1) { '
                '    cat(paste0("Retrying: ", pkg, "\\n")); '
                '    install_single_pkg(pkg, force=TRUE, label=paste0("retry ", pkg)); '
                '    if (pkg_is_ready(pkg)) { '
                '      cat(paste0("SUCCESS: ", pkg, " installed on retry\\n")); '
                '    } else { '
                '      cat(paste0("FAILED: ", pkg, " still not ready\\n")); '
                '    } '
                '  } '
                '  invisible(TRUE) '
                '}; '
            )
            # Phase 1: keep install path fast; repair is scoped to requested chain below
            if not skip_repair:
                r_code += (
                    'cleanup_partial_dirs(); '
                    'cat("=== Phase 1: preparing requested package installation ===\\n"); '
                )
            # Phase 2: install requested packages with intelligent fallback
            r_code += (
                'cleanup_partial_dirs(); '
                f'pkgs_to_install <- normalize_pkg_vec(unique({pkg_vec})); '
                'requested_chain <- normalize_pkg_vec(unique(c(pkgs_to_install, resolve_chain(pkgs_to_install)))); '
                'requested_chain <- requested_chain[!(requested_chain %in% base_pkgs)]; '
                'cat(paste0("\\n=== Full dependency chain to install ===\\n")); '
                'cat(paste0(paste(requested_chain, collapse=", "), "\\n")); '
                '  missing_chain <- requested_chain[!vapply(requested_chain, pkg_is_ready, logical(1))]; '
                '  if (length(missing_chain) == 0) { '
                '    cat("All requested packages and dependency chain are already ready\\n"); '
                '  } else { '
                '    cat(paste0("Installing missing packages from requested chain: ", paste(missing_chain, collapse=", "), "\\n")); '
                '    install_with_fallback(missing_chain, label="requested chain"); '
                '  }; '
                '  cleanup_partial_dirs(); '
                '  still_missing <- requested_chain[!vapply(requested_chain, pkg_is_ready, logical(1))]; '
                '  if (length(still_missing) > 0) { '
                '    cat(paste0("=== Final repair pass for unresolved packages: ", paste(still_missing, collapse=", "), " ===\\n")); '
                '    install_with_fallback(still_missing, label="final repair pass"); '
                '    cleanup_partial_dirs(); '
                '    still_missing <- requested_chain[!vapply(requested_chain, pkg_is_ready, logical(1))]; '
                '  }; '
                '  if (length(still_missing) == 0) { '
                '    cat("VERIFY_OK: all requested packages and dependency chain are ready\\n"); '
                '    quit(status=0, save="no") '
                '  } else { '
                '    cat(paste0("VERIFY_FAIL: packages still missing or broken after all attempts: ", paste(still_missing, collapse=", "), "\\n")); '
                '    quit(status=1, save="no") '
                '  } '
            )

        script_host_path, script_mount, script_path_in_container = _create_r_script_mount(
            r_code, prefix=f'r_install_{job_id}'
        )
        mounts.append(script_mount)

        r_image = _get_r_image()
        if r_image != R_CUSTOM_IMAGE:
            _enqueue_job_log(job_id, '[SYSTEM] Preparing R build environment...')
            if _ensure_custom_r_image(client, silent=True, progress_cb=lambda line: _enqueue_job_log(job_id, line)):
                r_image = R_CUSTOM_IMAGE
        if r_image == R_CUSTOM_IMAGE:
            deps_prefix = ''
        else:
            deps_prefix = (
                'if ! dpkg -s libuv1-dev >/dev/null 2>&1; then '
                '  echo "[SYS-DEPS] Installing R system dependencies..."; '
                f'  {_R_APT_HTTPS_SETUP} '
                f'  apt-get -o Acquire::Retries=5 update -qq && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 install -y -qq locales {R_SYSTEM_DEPS} 2>/dev/null; '
                f'  {_R_LOCALE_SETUP} '
                '  echo "[SYS-DEPS] System dependencies installed."; '
                'fi; '
            )
        container = client.containers.run(
            image=r_image,
            command=["bash", "-c", deps_prefix + "stdbuf -oL -eL Rscript " + script_path_in_container],
            mounts=mounts, environment=env,
            detach=True, user='0', platform='linux/amd64',
            log_config={'type': 'json-file'}
        )

        _stream_container_logs_realtime(
            container,
            lambda line: _enqueue_job_log(job_id, line),
            poll_seconds=0.2
        )

        result = _robust_container_wait(container, timeout=None)
        exit_code = result.get('StatusCode', -1)
        job['exit_code'] = exit_code
        job['status'] = 'done' if exit_code == 0 else 'failed'
        _enqueue_job_log(job_id, f'Process exited with code {exit_code}')
        log(f'Process exited with code {exit_code}')
        if exit_code == 0:
            _bump_r_libs_revision('install-job-success')
            _complete_r_runtime_migration(client, lock_held=True)
        try:
            container.remove()
        except:
            pass
    except Exception as e:
        job['status'] = 'failed'
        job['exit_code'] = -2
        _enqueue_job_log(job_id, f'Exception: {str(e)}')
        log(f'Exception: {str(e)}')
    finally:
        if script_host_path:
            try:
                os.remove(script_host_path)
            except Exception:
                pass




def _run_rscript_docker(script_path, params, out_dir, plugin_dir, extra_mounts=None, required_packages=None, plugin_name=None, cache_dir=None):
    """Run an R script inside Docker container.
    Automatically injects svglite override so that any call to svg() or
    cairo_svg() will produce svglite-based SVG output (editable text nodes).
    extra_mounts: optional list of additional Docker Mount objects (for workflow file paths).
    required_packages: optional list of declared R packages to check and repair before script execution.
    cache_dir: optional host directory mounted read-write at /cache for persistent plugin data."""
    client = get_docker_client()
    if not client:
        return {'status': 'error', 'message': 'Docker is not running'}
    # Ensure the immutable base image through the optimized common pull path.
    if R_DOCKER_IMAGE not in _image_exists_cache:
        try:
            _ensure_pinned_r_base_image(client)
        except Exception as e:
            return {'status': 'error', 'message': f'Failed to pull pinned R Docker image: {e}'}

    rel_script = os.path.relpath(script_path, plugin_dir).replace('\\', '/')
    container_script = f'/scripts/{rel_script}'
    abs_out = os.path.abspath(out_dir)

    mounts = [
        Mount(target='/r_libs', source=R_LIBS_DIR, type='bind'),
        Mount(target='/scripts', source=os.path.abspath(plugin_dir), type='bind', read_only=True),
        Mount(target='/workspace', source=abs_out, type='bind'),
    ]
    if cache_dir:
        abs_cache = os.path.abspath(cache_dir)
        os.makedirs(abs_cache, exist_ok=True)
        mounts.append(Mount(target='/cache', source=abs_cache, type='bind'))
    if extra_mounts:
        mounts.extend(extra_mounts)
    env = R_LIBS_ENV.copy()

    # ---- Pre-flight: only check current plugin declared package chain ----
    safe_pkgs = _normalize_r_package_names(required_packages)

    if safe_pkgs:
        preflight_cache_key = '|'.join(safe_pkgs)
        host_dep_status = _host_r_dependency_status(safe_pkgs)
        if _has_cached_r_preflight(preflight_cache_key) and host_dep_status.get('ready'):
            chain_count = len(host_dep_status.get('chain') or [])
            log(f"[R-DOCKER] Pre-flight cache hit and host dependency chain is intact ({chain_count} packages), skipping pre-flight")
        elif host_dep_status.get('ready'):
            chain_count = len(host_dep_status.get('chain') or [])
            log(f"[R-DOCKER] Host dependency chain already ready ({chain_count} packages), skipping pre-flight")
            _mark_cached_r_preflight(preflight_cache_key)
        else:
            host_missing_chain = host_dep_status.get('missing') or []
            if host_missing_chain:
                log(f"[R-DOCKER] Pre-flight repair required, package chain missing on host: {', '.join(host_missing_chain)}")
                _drop_cached_r_preflight(preflight_cache_key)
                log(f"[R-DOCKER] Pre-flight: checking declared package chain ({', '.join(safe_pkgs)}) ...")
                repos, bioc_mirror = _resolve_r_repo_bundle(R_RUNTIME.get('repos', 'auto'))
                pkg_vec = 'c(' + ','.join([f'"{p}"' for p in safe_pkgs]) + ')'
                preflight_code = (
                    'selected_repo <- "' + repos + '"; '
                    'selected_bioc_mirror <- "' + bioc_mirror + '"; '
                    'ppm_repos <- c(CRAN=selected_repo); '
                    'options(repos = ppm_repos); '
                    'cat(paste0("Using CRAN mirror: ", getOption("repos")[["CRAN"]], "\\n")); '
                    'options(HTTPUserAgent = sprintf("R/%s R (%s)", getRversion(), paste(getRversion(), R.version["platform"], R.version["arch"], R.version["os"]))); '
                    'options(timeout=600, download.file.method="libcurl"); '
                    '.libPaths(c("/r_libs", .libPaths())); '
                    'ncpus <- suppressWarnings(as.integer(parallel::detectCores(logical=FALSE))); '
                    'if (!is.finite(ncpus) || is.na(ncpus)) ncpus <- 1L; '
                    'ncpus <- max(1L, min(2L, ncpus)); '
                    'Sys.setenv(MAKEFLAGS=paste0("-j", ncpus)); '
                    'if (!requireNamespace("BiocManager", quietly = TRUE)) { '
                    '  install.packages("BiocManager", lib="/r_libs", Ncpus=ncpus) }; '
                    'suppressPackageStartupMessages(library(BiocManager)); '
                    f'target_bioc_version <- "{BIOCONDUCTOR_VERSION}"; '
                    'options(BioC_mirror=selected_bioc_mirror); '
                    f'bioc_repos <- BiocManager::repositories(version=target_bioc_version, site_repository="{repos}"); '
                    'merged_repos <- bioc_repos; '
                    'if ("CRAN" %in% names(merged_repos) && "CRAN" %in% names(ppm_repos)) merged_repos["CRAN"] <- ppm_repos[["CRAN"]]; '
                    'if (!("UserMirror" %in% names(merged_repos))) merged_repos <- c(merged_repos, UserMirror=selected_repo); '
                    'options(repos = merged_repos); '
                    'cat(paste0("Using Bioconductor mirror: ", selected_bioc_mirror, "\\n")); '
                    'cat(paste0("Resolved repositories: ", paste(names(getOption("repos")), getOption("repos"), sep="=", collapse=" | "), "\\n")); '
                    'safe_available_packages <- function(repos, label) { '
                    '  cat(paste0("Refreshing package index: ", label, "\\n")); '
                    '  out <- tryCatch({ '
                    '    setTimeLimit(elapsed=45, transient=TRUE); '
                    '    on.exit(setTimeLimit(cpu=Inf, elapsed=Inf, transient=FALSE), add=TRUE); '
                    '    available.packages(repos=repos) '
                    '  }, error=function(e) { cat("WARN: package index unavailable for ", label, ": ", conditionMessage(e), "\\n", sep=""); NULL }); '
                    '  if (is.null(out)) cat(paste0("WARN: continuing without package index for ", label, "\\n")) else cat(paste0("Package index ready for ", label, ": ", nrow(out), " entries\\n")); '
                    '  out '
                    '}; '
                    'available_db <- safe_available_packages(bioc_repos, "CRAN+Bioc"); '
                    'bioc_only_repos <- bioc_repos[names(bioc_repos) != "CRAN"]; '
                    'available_names <- if (is.null(available_db)) character(0) else rownames(available_db); '
                    f'base_pkgs <- {_R_BASE_PACKAGES_R}; '
                    'dep_types <- c("Depends","Imports","LinkingTo"); '
                    'canonicalize_pkg <- function(pkg) { '
                    '  if (is.null(pkg) || is.na(pkg) || !nzchar(pkg)) return(pkg); '
                    '  if (length(available_names) == 0) return(pkg); '
                    '  hit <- available_names[tolower(available_names) == tolower(pkg)]; '
                    '  if (length(hit) > 0) { '
                    '    if (!identical(hit[[1]], pkg)) cat(paste0("Package name normalized: ", pkg, " -> ", hit[[1]], "\\n")); '
                    '    return(hit[[1]]) '
                    '  }; '
                    '  pkg '
                    '}; '
                    'normalize_pkg_vec <- function(pkgs) { '
                    '  unique(vapply(pkgs, canonicalize_pkg, character(1), USE.NAMES=FALSE)) '
                    '}; '
                    'pkg_is_ready <- function(pkg) { '
                    '  pkg <- canonicalize_pkg(pkg); '
                    '  isTRUE(tryCatch(requireNamespace(pkg, quietly=TRUE), error=function(e) FALSE)) '
                    '}; '
                    'is_bioc_pkg <- function(pkg) { '
                    '  pkg <- canonicalize_pkg(pkg); '
                    '  if (is.null(available_db) || !("Repository" %in% colnames(available_db)) || !(pkg %in% rownames(available_db))) return(NA); '
                    '  repo_value <- available_db[pkg, "Repository"]; '
                    '  isTRUE(grepl("bioconductor", repo_value, ignore.case=TRUE)) '
                    '}; '
                    'resolve_chain <- function(pkgs) { '
                    '  pkgs <- normalize_pkg_vec(unique(pkgs[nzchar(pkgs) & !is.na(pkgs)])); '
                    '  if (length(pkgs) == 0 || is.null(available_db)) return(character(0)); '
                    '  deps <- tryCatch(tools::package_dependencies(pkgs, db=available_db, which=c("Depends","Imports","LinkingTo"), recursive=TRUE), error=function(e) list()); '
                    '  normalize_pkg_vec(unique(unlist(deps, use.names=FALSE))) '
                    '}; '
                    'install_targets <- function(targets, force=FALSE, label="install", use_bioc_only=FALSE) { '
                    '  targets <- unique(targets[nzchar(targets) & !is.na(targets) & !(targets %in% base_pkgs)]); '
                    '  if (length(targets) == 0) return(invisible(TRUE)); '
                    '  install_type <- if (force) "source" else getOption("pkgType"); '
                    '  cat(paste0("Installing dependency set for ", label, ": ", paste(targets, collapse=", "), if (force) " (force=TRUE, type=source)" else "", if (use_bioc_only) " (Bioc-only repos)" else "", "\\n")); '
                    '  if (use_bioc_only) { '
                    '    old_repos <- getOption("repos"); '
                    '    options(repos=bioc_only_repos); '
                    '  }; '
                    '  withCallingHandlers('
                    '    tryCatch({ '
                    '      BiocManager::install(targets, lib="/r_libs", version=target_bioc_version, update=FALSE, ask=FALSE, force=force, dependencies=dep_types, Ncpus=ncpus, type=install_type, site_repository=if (use_bioc_only) NULL else selected_repo) '
                    '    }, error=function(e) { cat("ERR: ", conditionMessage(e), "\\n", sep="") }), '
                    '    warning=function(w) { cat("WARN: ", conditionMessage(w), "\\n", sep=""); invokeRestart("muffleWarning") }'
                    '  ); '
                    '  if (use_bioc_only) { options(repos=old_repos) }; '
                    '  unresolved <- targets[!vapply(targets, pkg_is_ready, logical(1))]; '
                    '  unresolved '
                    '}; '
                    'install_with_fallback <- function(targets, label="install") { '
                    '  targets <- normalize_pkg_vec(unique(targets[nzchar(targets) & !is.na(targets) & !(targets %in% base_pkgs)])); '
                    '  if (length(targets) == 0) return(invisible(TRUE)); '
                    '  cat(paste0("=== Primary install attempt (CRAN+Bioc): ", paste(targets, collapse=", "), " ===\\n")); '
                    '  unresolved_1 <- install_targets(targets, force=FALSE, label=label); '
                    '  if (length(unresolved_1) == 0) return(invisible(TRUE)); '
                    '  cat(paste0("=== Failed packages: ", paste(unresolved_1, collapse=", "), " ===\\n")); '
                    '  # If repo metadata is unavailable, still retry all unresolved packages via Bioc-only repos. '
                    '  bioc_marks <- vapply(unresolved_1, is_bioc_pkg, logical(1), USE.NAMES=FALSE); '
                    '  retry_targets <- unresolved_1[is.na(bioc_marks) | bioc_marks]; '
                    '  if (length(retry_targets) == 0) retry_targets <- unresolved_1; '
                    '  if (length(retry_targets) > 0) { '
                    '    cat(paste0("=== Retry with Bioconductor-only repos: ", paste(retry_targets, collapse=", "), " ===\\n")); '
                    '    unresolved_2 <- install_targets(retry_targets, force=TRUE, label=paste0(label, " [Bioc-only, force]"), use_bioc_only=TRUE); '
                    '    if (length(unresolved_2) > 0) { '
                    '      cat(paste0("=== Still failing after Bioc retry: ", paste(unresolved_2, collapse=", "), " ===\\n")); '
                    '      for (pkg in unresolved_2) { '
                    '        cat(paste0("Attempting per-package force source install: ", pkg, "\\n")); '
                    '        withCallingHandlers('
                    '          tryCatch({ '
                    '            BiocManager::install(pkg, lib="/r_libs", version=target_bioc_version, update=FALSE, ask=FALSE, force=TRUE, type="source", dependencies=dep_types) '
                    '          }, error=function(e) { cat("ERR: ", conditionMessage(e), "\\n", sep="") }), '
                    '          warning=function(w) { cat("WARN: ", conditionMessage(w), "\\n", sep=""); invokeRestart("muffleWarning") }'
                    '        ); '
                    '      } '
                    '    } '
                    '  }; '
                    '  invisible(TRUE) '
                    '}; '
                    f'declared <- normalize_pkg_vec(unique({pkg_vec})); '
                    'declared <- declared[nzchar(declared)]; '
                    'target_chain <- normalize_pkg_vec(unique(c(declared, resolve_chain(declared)))); '
                    'target_chain <- target_chain[nzchar(target_chain) & !is.na(target_chain) & !(target_chain %in% base_pkgs)]; '
                    'to_install <- target_chain[!vapply(target_chain, pkg_is_ready, logical(1))]; '
                    'cat(paste0("PRECHECK_INSTALL_COUNT=", length(to_install), "\\n")); '
                    'if (length(to_install) > 0) { '
                    '  cat("[R-DOCKER] Repairing ", length(to_install), " packages for current plugin chain: ", paste(to_install, collapse=", "), "\\n", sep=""); '
                    '  install_with_fallback(to_install, label="preflight repair"); '
                    '  still_bad <- target_chain[!vapply(target_chain, pkg_is_ready, logical(1))]; '
                    '} else { '
                    '  cat("[R-DOCKER] Declared package chain already ready\\n"); '
                    '  still_bad <- character(0); '
                    '}; '
                    'cat(paste0("PRECHECK_REMAINING_COUNT=", length(still_bad), "\\n")); '
                    'if (length(still_bad) > 0) { '
                    '  cat("[R-DOCKER] Remaining broken packages after repair: ", paste(still_bad, collapse=", "), "\\n", sep=""); '
                    '  quit(status=1, save="no") '
                    '} '
                )
                preflight_script_host_path = None
                preflight_container = None
                log('[R-DOCKER] Waiting for exclusive package-library lock before dependency repair...')
                preflight_error = None
                try:
                    with _R_PACKAGE_RW_LOCK.write():
                        log('[R-DOCKER] Package-library lock acquired; dependency state will be rechecked in the container.')
                        preflight_lines = []
                        preflight_script_host_path, preflight_script_mount, preflight_script_path = _create_r_script_mount(
                            preflight_code, prefix=f'r_preflight_{preflight_cache_key}'
                        )
                        mounts.append(preflight_script_mount)
                        preflight_r_image = _get_r_image()
                        if preflight_r_image != R_CUSTOM_IMAGE:
                            if _ensure_custom_r_image(client, silent=True):
                                preflight_r_image = R_CUSTOM_IMAGE
                        preflight_container = client.containers.run(
                            image=preflight_r_image,
                            command=["bash", "-c", "Rscript " + preflight_script_path],
                            mounts=mounts, environment=env,
                            detach=True, user='0', platform='linux/amd64',
                            log_config={'type': 'json-file'}
                        )
                        _stream_container_logs_realtime(
                            preflight_container,
                            lambda line: (preflight_lines.append(line), log(line)),
                            poll_seconds=0.5
                        )
                        result = _robust_container_wait(preflight_container, timeout=300)
                        exit_code = result.get('StatusCode', -1)
                        if exit_code != 0:
                            _drop_cached_r_preflight(preflight_cache_key)
                            preflight_error = f'R package pre-flight repair failed with exit code {exit_code}'
                            log(f'[ERROR] {preflight_error}')
                        else:
                            install_count = 0
                            for _ln in preflight_lines:
                                m = re.search(r'PRECHECK_INSTALL_COUNT=(\d+)', _ln)
                                if m:
                                    install_count = int(m.group(1))
                                    break
                            if install_count > 0:
                                _bump_r_libs_revision('r-preflight-installed-missing')
                            _mark_cached_r_preflight(preflight_cache_key)
                except Exception as e:
                    _drop_cached_r_preflight(preflight_cache_key)
                    preflight_error = f'R package pre-flight failed: {e}'
                    log(f'[ERROR] {preflight_error}')
                finally:
                    if preflight_container is not None:
                        try:
                            preflight_container.remove(force=True)
                        except Exception:
                            pass
                    if preflight_script_host_path:
                        try:
                            os.remove(preflight_script_host_path)
                        except Exception:
                            pass
                if preflight_error:
                    return {'status': 'error', 'message': preflight_error}

    # Successful dependency preparation is sufficient to retire the old
    # library; the analysis itself does not need to finish first.
    _complete_r_runtime_migration(client)

    r_svg_override = (
        'if(requireNamespace("svglite",quietly=TRUE)){'
        '.psvg<-function(filename="Rplot.svg",width=7,height=7,...){'
        'da<-list(...);a<-list(file=filename,width=width,height=height);'
        'for(nm in names(da))if(nm%in%c("bg","pointsize","standalone","fix_text_size","scaling"))a[[nm]]<-da[[nm]];'
        'do.call(svglite::svglite,a)};'
        'assign("svg",.psvg,envir=globalenv());assign("cairo_svg",.psvg,envir=globalenv());'
        'tryCatch({ns<-asNamespace("grDevices");unlockBinding("svg",ns);assign("svg",.psvg,envir=ns);invisible(lockBinding("svg",ns))},error=function(e){});'
        'tryCatch({ns<-asNamespace("grDevices");unlockBinding("cairo_svg",ns);assign("cairo_svg",.psvg,envir=ns);invisible(lockBinding("cairo_svg",ns))},error=function(e){})'
        '}else{cat("[PrimiGenius] svglite not found, svg override skipped\\n")};'
    )
    r_bootstrap = (
        'invisible({' +
        r_svg_override +
        '});' +
        f'args<-commandArgs(trailingOnly=TRUE);'
        f'invisible(sys.source("{container_script}",envir=globalenv()))'
    )

    # Handle multi-line params (spreadsheet data, manual input, etc.)
    # Write them to temp files in output directory and pass the file path instead
    temp_param_files = []
    for k in list(params.keys()):
        v = str(params.get(k, '') or '')
        if '\n' in v and len(v) > 10:
            tmp_name = f'.primigenius_input_{k}.csv'
            tmp_path = os.path.join(abs_out, tmp_name)
            try:
                with open(tmp_path, 'w', encoding='utf-8', newline='') as tf:
                    tf.write(v)
                params[k] = f'/workspace/{tmp_name}'
                temp_param_files.append(tmp_path)
                log(f'[R-DOCKER] Wrote multi-line param "{k}" to temp file ({len(v)} chars)')
            except Exception as e:
                log(f'[WARN] Could not write temp param file for {k}: {e}')

    cmd_list = ['Rscript', '-e', r_bootstrap]
    for k, v in (params or {}).items():
        cmd_list.append(f'{k}={v}')
    log(f'[R-DOCKER] Running with svglite override: {container_script}')

    r_image = _get_r_image()
    if r_image != R_CUSTOM_IMAGE:
        if _ensure_custom_r_image(client, silent=True):
            r_image = R_CUSTOM_IMAGE
    if r_image == R_CUSTOM_IMAGE:
        deps_prefix = ''
    else:
        deps_prefix = (
            'if ! dpkg -s libuv1-dev >/dev/null 2>&1; then '
            '  echo "[SYS-DEPS] Installing R system dependencies..."; '
            f'  {_R_APT_HTTPS_SETUP} '
            f'  apt-get -o Acquire::Retries=5 update -qq && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 install -y -qq locales {R_SYSTEM_DEPS} 2>/dev/null; '
            f'  {_R_LOCALE_SETUP} '
            '  echo "[SYS-DEPS] System dependencies installed."; '
            'fi; '
        )
    bash_cmd = deps_prefix + ' '.join(f"'{c}'" if ' ' in c else c for c in cmd_list)

    try:
        try:
            with open(script_path, 'r', encoding='utf-8', errors='replace') as script_handle:
                script_mutates_library = bool(re.search(r'\b(?:install\.packages|BiocManager::install|remotes::install_)\s*\(', script_handle.read()))
        except Exception:
            script_mutates_library = False
        run_guard = _R_PACKAGE_RW_LOCK.write() if script_mutates_library else _R_PACKAGE_RW_LOCK.read()
        container = None
        try:
            with run_guard:
                container = client.containers.run(
                    image=r_image,
                    command=["bash", "-c", bash_cmd],
                    mounts=mounts, environment=env,
                    detach=True, user='0', platform='linux/amd64',
                    log_config={'type': 'json-file'}
                )
                out_lines = []
                _stream_container_logs_realtime(
                    container,
                    lambda line: (out_lines.append(line), log(line)),
                    poll_seconds=0.5
                )
                result = _robust_container_wait(container, timeout=600)
                exit_code = result.get('StatusCode', -1)
                has_success_marker = any('[SUCCESS]' in str(ln) for ln in out_lines)
                if exit_code != 0 and has_success_marker:
                    log(f'[R-DOCKER] Exit code {exit_code} but [SUCCESS] marker found in logs, treating as success')
                    exit_code = 0
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
        for tp in temp_param_files:
            try:
                if os.path.exists(tp):
                    os.remove(tp)
            except:
                pass
        if exit_code == 0:
            _complete_r_runtime_migration(client)
        return {'status': 'success' if exit_code == 0 else 'failed', 'exit_code': exit_code, 'log': out_lines}
    except Exception as e:
        for tp in temp_param_files:
            try:
                if os.path.exists(tp):
                    os.remove(tp)
            except:
                pass
        return {'status': 'error', 'message': str(e)}

@app.route('/get-plugins', methods=['GET'])
def get_plugins():
    def _display_name(name):
        try:
            if isinstance(name, str):
                return name
            if isinstance(name, dict):
                # prefer english, then chinese, then any value
                return name.get('en') or name.get('zh') or next(iter(name.values()))
            return str(name)
        except Exception:
            return ''

    p = list(load_all_plugins().values())
    p.sort(key=lambda x: (
        (x.get('category', '') if isinstance(x.get('category', ''), str) else _display_name(x.get('category', ''))),
        _display_name(x.get('name', ''))
    ))
    return jsonify({"status": "success", "plugins": p})

@app.route('/plugin-webapp/<plugin_id>/<path:filename>', methods=['GET'])
def serve_plugin_webapp(plugin_id, filename):
    """Serve static files for webapp plugins (HTML, JS, CSS, images, etc.)"""
    plugins = load_all_plugins()
    cfg = plugins.get(plugin_id)
    if not cfg:
        return jsonify({'status': 'error', 'message': 'Plugin not found'}), 404
    plugin_dir = cfg.get('plugin_dir', '')
    if not plugin_dir or not os.path.isdir(plugin_dir):
        return jsonify({'status': 'error', 'message': 'Plugin directory not found'}), 404
    # Protect against path traversal
    safe_path = os.path.normpath(os.path.join(plugin_dir, filename))
    if not safe_path.startswith(os.path.abspath(plugin_dir)):
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    if not os.path.isfile(safe_path):
        return jsonify({'status': 'error', 'message': 'File not found'}), 404
    # Determine MIME type
    ext = os.path.splitext(filename)[1].lower()
    mime_types = {
        '.html': 'text/html; charset=utf-8',
        '.htm': 'text/html; charset=utf-8',
        '.css': 'text/css; charset=utf-8',
        '.js': 'application/javascript; charset=utf-8',
        '.json': 'application/json; charset=utf-8',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.svg': 'image/svg+xml',
        '.ico': 'image/x-icon',
        '.woff': 'font/woff',
        '.woff2': 'font/woff2',
        '.ttf': 'font/ttf',
        '.eot': 'application/vnd.ms-fontobject',
        '.csv': 'text/csv; charset=utf-8',
        '.txt': 'text/plain; charset=utf-8',
        '.md': 'text/markdown; charset=utf-8',
        '.tree': 'text/plain; charset=utf-8',
    }
    mimetype = mime_types.get(ext, 'application/octet-stream')
    directory = os.path.dirname(safe_path)
    fname = os.path.basename(safe_path)
    return send_from_directory(directory, fname, mimetype=mimetype)


_WEBAPP_FILE_TOKENS = {}
_WEBAPP_FILE_TOKENS_LOCK = threading.Lock()


def _norm_abs_path(path):
    return os.path.abspath(os.path.normpath(os.path.expanduser(str(path or '').strip())))


def _path_is_under(path, root):
    try:
        path_abs = _norm_abs_path(path)
        root_abs = _norm_abs_path(root)
        return os.path.commonpath([path_abs, root_abs]) == root_abs
    except Exception:
        return False


def _webapp_bridge_enabled(cfg):
    bridge = cfg.get('webapp_bridge') or {}
    return cfg.get('run_mode') == 'webapp' and bool(bridge.get('enabled'))


def _webapp_bridge_cfg(plugin_id):
    cfg = load_all_plugins().get(plugin_id)
    if not cfg:
        return None, ({'status': 'error', 'message': 'Plugin not found'}, 404)
    if not _webapp_bridge_enabled(cfg):
        return None, ({'status': 'error', 'message': 'WebApp bridge is not enabled for this plugin'}, 403)
    return cfg, None


def _is_local_webapp_request():
    """Reject cross-site browser calls to local file bridge endpoints."""
    try:
        expected_port = int(request.environ.get('SERVER_PORT') or 0)
    except (TypeError, ValueError):
        return False
    origin = (request.headers.get('Origin') or '').strip()
    referer = (request.headers.get('Referer') or '').strip()
    for value in (origin, referer):
        if not value:
            continue
        try:
            parsed = urllib.parse.urlparse(value)
            hostname = (parsed.hostname or '').lower()
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            if hostname not in {'127.0.0.1', 'localhost'} or port != expected_port:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _webapp_allowed_roots(cfg):
    bridge = cfg.get('webapp_bridge') or {}
    roots = []
    root_keys = bridge.get('allowed_roots')
    if not isinstance(root_keys, list):
        root_keys = ['outputs', 'plugin']
    plugin_dir = cfg.get('plugin_dir') or ''
    mapping = {
        'outputs': USER_DOCS,
        'app': APP_INSTALL_DIR,
        'plugins': PLUGINS_DIR,
        'plugin': plugin_dir,
    }
    cache_dir = cfg.get('cache_dir')
    if cache_dir and plugin_dir:
        mapping['cache'] = os.path.join(plugin_dir, cache_dir)
    for key in root_keys:
        root = mapping.get(str(key), str(key))
        if root:
            roots.append(_norm_abs_path(root))
    for extra in bridge.get('allowed_paths') or []:
        if extra:
            roots.append(_norm_abs_path(extra))
    return roots


def _webapp_file_extension_allowed(cfg, path):
    exts = (cfg.get('webapp_bridge') or {}).get('file_extensions') or []
    if not exts:
        return True
    normalized = {str(e).lower() if str(e).startswith('.') else '.' + str(e).lower() for e in exts}
    lower_name = os.path.basename(path).lower()
    # Genomics index extensions often have compound suffixes such as .vcf.gz.tbi.
    return any(lower_name.endswith(ext) for ext in normalized)


def _webapp_file_allowed(cfg, path):
    if not path or not os.path.isfile(path):
        return False, 'File not found'
    if not _webapp_file_extension_allowed(cfg, path):
        return False, 'File extension is not allowed for this webapp bridge'

    bridge = cfg.get('webapp_bridge') or {}
    roots = _webapp_allowed_roots(cfg)
    if any(_path_is_under(path, root) for root in roots):
        return True, ''
    if bool(bridge.get('allow_user_paths')):
        return True, ''
    return False, 'Access denied'


def _webapp_file_mime(path):
    mime, _ = mimetypes.guess_type(path)
    if mime:
        return mime
    ext = os.path.basename(path).lower()
    genomics_binary = (
        '.bam', '.bai', '.cram', '.crai', '.tbi', '.csi', '.bigwig', '.bw',
        '.bigbed', '.bb', '.2bit', '.hic'
    )
    if ext.endswith(('.vcf', '.bed', '.gff', '.gff3', '.gtf', '.sam', '.fasta', '.fa', '.fai')):
        return 'text/plain'
    if ext.endswith(genomics_binary) or ext.endswith(('.gz', '.bgz')):
        return 'application/octet-stream'
    return 'application/octet-stream'


def _webapp_task_cfg(cfg, task_id):
    bridge = cfg.get('webapp_bridge') or {}
    tasks = bridge.get('tasks') or {}
    if not isinstance(tasks, dict):
        return None, 'No webapp tasks are configured'
    task = tasks.get(str(task_id))
    if not isinstance(task, dict):
        return None, 'WebApp task is not configured for this plugin'
    script = str(task.get('script') or '').strip()
    if not script:
        return None, 'WebApp task script is not configured'
    plugin_dir = _norm_abs_path(cfg.get('plugin_dir') or '')
    script_path = _norm_abs_path(os.path.join(plugin_dir, script))
    if not _path_is_under(script_path, plugin_dir) or not os.path.isfile(script_path):
        return None, 'WebApp task script was not found in the plugin directory'
    return dict(task, script_path=script_path), ''


def _webapp_python_executable():
    if not IS_FROZEN and sys.executable and os.path.isfile(sys.executable):
        return sys.executable
    for candidate in ('python', 'python3'):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return ''


def _webapp_validate_task_payload_paths(cfg, task, payload):
    for field in task.get('path_fields') or []:
        value = payload.get(field)
        if value in (None, ''):
            continue
        path = _norm_abs_path(value)
        require_exists = field not in (task.get('optional_output_path_fields') or [])
        if require_exists:
            ok, reason = _webapp_file_allowed(cfg, path)
            if not ok:
                return False, f'{field}: {reason}'
        elif not _webapp_file_extension_allowed(cfg, path):
            return False, f'{field}: File extension is not allowed for this webapp bridge'
    return True, ''


def _webapp_public_cfg(cfg):
    bridge = cfg.get('webapp_bridge') or {}
    return {
        'status': 'success',
        'pluginId': cfg.get('id'),
        'bridge': {
            'enabled': _webapp_bridge_enabled(cfg),
            'allowUserPaths': bool(bridge.get('allow_user_paths')),
            'allowedRoots': bridge.get('allowed_roots') or ['outputs', 'plugin'],
            'fileExtensions': bridge.get('file_extensions') or [],
            'maxRegisterFiles': int(bridge.get('max_register_files') or 64),
            'supportsRange': True,
            'tasks': sorted((bridge.get('tasks') or {}).keys()) if isinstance(bridge.get('tasks'), dict) else [],
        },
        'paths': {
            'outputsDir': USER_DOCS.replace('\\', '/'),
            'appInstallDir': APP_INSTALL_DIR.replace('\\', '/'),
            'pluginDir': str(cfg.get('plugin_dir') or '').replace('\\', '/'),
        }
    }


@app.route('/plugin-webapp-api/<plugin_id>/bridge/config', methods=['GET'])
def plugin_webapp_bridge_config(plugin_id):
    cfg, err = _webapp_bridge_cfg(plugin_id)
    if err:
        payload, code = err
        return jsonify(payload), code
    if not _is_local_webapp_request():
        return jsonify({'status': 'error', 'message': 'Cross-origin bridge request denied'}), 403
    return jsonify(_webapp_public_cfg(cfg))


@app.route('/plugin-webapp-api/<plugin_id>/bridge/client.js', methods=['GET'])
def plugin_webapp_bridge_client(plugin_id):
    cfg, err = _webapp_bridge_cfg(plugin_id)
    if err:
        payload, code = err
        return Response(
            f"console.error({json.dumps(payload.get('message', 'WebApp bridge unavailable'))});",
            status=code,
            mimetype='application/javascript'
        )
    js = f"""
(function() {{
  const pluginId = {json.dumps(plugin_id)};
  const base = `/plugin-webapp-api/${{encodeURIComponent(pluginId)}}/bridge`;
  async function requestJson(path, options) {{
    const response = await fetch(base + path, Object.assign({{ credentials: 'same-origin' }}, options || {{}}));
    let payload = {{}};
    try {{ payload = await response.json(); }} catch (_) {{}}
    if (!response.ok || payload.status === 'error') {{
      throw new Error(payload.message || `WebApp bridge request failed (${{response.status}})`);
    }}
    return payload;
  }}
  const bridge = {{
    pluginId,
    config: () => requestJson('/config'),
    pickFiles: (options) => new Promise((resolve, reject) => {{
      if (!window.parent || window.parent === window) {{
        reject(new Error('Native file picker is available only inside the PrimiGenius desktop webapp frame'));
        return;
      }}
      const requestId = `${{Date.now()}}-${{Math.random().toString(36).slice(2)}}`;
      const timeoutMs = Math.max(1000, Math.min(Number(options && options.timeoutMs) || 120000, 300000));
      let timer = null;
      const cleanup = () => {{
        if (timer) window.clearTimeout(timer);
        window.removeEventListener('message', onMessage);
      }};
      const onMessage = (event) => {{
        const data = event && event.data ? event.data : {{}};
        if (!data || data.type !== 'primigenius:webapp:pickFiles:response' || data.requestId !== requestId) return;
        cleanup();
        if (!data.ok) {{
          reject(new Error(data.message || 'Native file picker failed'));
          return;
        }}
        resolve(data.result || {{ canceled: true, filePaths: [] }});
      }};
      timer = window.setTimeout(() => {{
        cleanup();
        reject(new Error('Native file picker timed out'));
      }}, timeoutMs);
      window.addEventListener('message', onMessage);
      window.parent.postMessage({{
        type: 'primigenius:webapp:pickFiles',
        requestId,
        pluginId,
        options: options || {{}}
      }}, '*');
    }}),
    registerPaths: (paths) => requestJson('/register-files', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ paths: Array.isArray(paths) ? paths : [paths] }})
    }}),
    registerPath: async (path) => {{
      const result = await bridge.registerPaths([path]);
      const file = result.files && result.files[0];
      if (!file) {{
        const first = result.errors && result.errors[0];
        throw new Error((first && first.message) || 'File could not be registered');
      }}
      return file;
    }},
    runTask: (taskId, payload) => requestJson(`/tasks/${{encodeURIComponent(taskId)}}`, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload || {{}})
    }}),
    fileUrl: (token, name) => `/plugin-webapp-data/${{encodeURIComponent(pluginId)}}/${{encodeURIComponent(token)}}/${{encodeURIComponent(name || 'file')}}`,
    resolveFile: async (file) => {{
      if (!file) return null;
      if (file.path) return bridge.registerPath(file.path);
      return {{
        status: 'success',
        name: file.name || 'local-file',
        size: file.size || 0,
        url: file,
        localObject: true
      }};
    }}
  }};
  window.PrimiGeniusWebAppBridge = bridge;
}})();
"""
    return Response(js, mimetype='application/javascript')


@app.route('/plugin-webapp-api/<plugin_id>/bridge/register-files', methods=['POST'])
def plugin_webapp_bridge_register_files(plugin_id):
    cfg, err = _webapp_bridge_cfg(plugin_id)
    if err:
        payload, code = err
        return jsonify(payload), code
    if not _is_local_webapp_request():
        return jsonify({'status': 'error', 'message': 'Cross-origin bridge request denied'}), 403

    data = request.json or {}
    paths = data.get('paths') or []
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        return jsonify({'status': 'error', 'message': 'paths must be a list'}), 400

    bridge = cfg.get('webapp_bridge') or {}
    max_files = int(bridge.get('max_register_files') or 64)
    if len(paths) > max_files:
        return jsonify({'status': 'error', 'message': f'Too many files; max is {max_files}'}), 400

    files = []
    errors = []
    for raw_path in paths:
        path = _norm_abs_path(raw_path)
        ok, reason = _webapp_file_allowed(cfg, path)
        if not ok:
            errors.append({'path': str(raw_path), 'message': reason})
            continue

        token = uuid.uuid4().hex
        name = os.path.basename(path)
        size = os.path.getsize(path)
        stat = os.stat(path)
        entry = {
            'plugin_id': plugin_id,
            'path': path,
            'name': name,
            'size': size,
            'mtime': stat.st_mtime,
            'mime': _webapp_file_mime(path),
            'created': time.time(),
        }
        with _WEBAPP_FILE_TOKENS_LOCK:
            _WEBAPP_FILE_TOKENS[token] = entry
        files.append({
            'status': 'success',
            'token': token,
            'name': name,
            'path': path.replace('\\', '/'),
            'size': size,
            'mtime': stat.st_mtime,
            'mime': entry['mime'],
            'url': f"/plugin-webapp-data/{urllib.parse.quote(plugin_id)}/{token}/{urllib.parse.quote(name)}",
            'range': True,
        })

    return jsonify({'status': 'success', 'files': files, 'errors': errors})


@app.route('/plugin-webapp-api/<plugin_id>/bridge/tasks/<task_id>', methods=['POST'])
def plugin_webapp_bridge_run_task(plugin_id, task_id):
    cfg, err = _webapp_bridge_cfg(plugin_id)
    if err:
        payload, code = err
        return jsonify(payload), code
    if not _is_local_webapp_request():
        return jsonify({'status': 'error', 'message': 'Cross-origin bridge request denied'}), 403

    task, reason = _webapp_task_cfg(cfg, task_id)
    if not task:
        return jsonify({'status': 'error', 'message': reason}), 404
    if str(task.get('runner') or 'python') != 'python':
        return jsonify({'status': 'error', 'message': 'Unsupported webapp task runner'}), 400

    payload = request.json or {}
    if not isinstance(payload, dict):
        return jsonify({'status': 'error', 'message': 'Task payload must be a JSON object'}), 400
    ok, reason = _webapp_validate_task_payload_paths(cfg, task, payload)
    if not ok:
        return jsonify({'status': 'error', 'message': reason}), 400

    python_exe = _webapp_python_executable()
    if not python_exe:
        return jsonify({'status': 'error', 'message': 'Python executable was not found for webapp task'}), 500

    bridge = cfg.get('webapp_bridge') or {}
    task_input = {
        'payload': payload,
        'context': {
            'pluginId': plugin_id,
            'pluginDir': _norm_abs_path(cfg.get('plugin_dir') or '').replace('\\', '/'),
            'outputsDir': USER_DOCS.replace('\\', '/'),
            'appInstallDir': APP_INSTALL_DIR.replace('\\', '/'),
            'allowedRoots': _webapp_allowed_roots(cfg),
            'fileExtensions': bridge.get('file_extensions') or [],
        }
    }
    timeout = max(1, min(int(task.get('timeout_seconds') or 300), 3600))
    run_kwargs = {
        'cwd': _norm_abs_path(cfg.get('plugin_dir') or ''),
        'input': json.dumps(task_input, ensure_ascii=False),
        'stdout': subprocess.PIPE,
        'stderr': subprocess.PIPE,
        'text': True,
        'encoding': 'utf-8',
        'errors': 'replace',
        'timeout': timeout,
    }
    if os.name == 'nt':
        run_kwargs['creationflags'] = _HIDDEN_SUBPROCESS_KWARGS.get('creationflags', 0)
        run_kwargs['startupinfo'] = _HIDDEN_SUBPROCESS_KWARGS.get('startupinfo', None)

    try:
        proc = subprocess.run([python_exe, task['script_path']], **run_kwargs)
    except subprocess.TimeoutExpired:
        return jsonify({'status': 'error', 'message': f'WebApp task timed out after {timeout}s'}), 504
    except Exception as exc:
        return jsonify({'status': 'error', 'message': f'WebApp task failed to start: {exc}'}), 500

    stdout = (proc.stdout or '').strip()
    stderr = (proc.stderr or '').strip()
    if proc.returncode != 0:
        return jsonify({
            'status': 'error',
            'message': stderr or stdout or f'WebApp task exited with code {proc.returncode}',
            'exitCode': proc.returncode,
        }), 500
    try:
        result = json.loads(stdout or '{}')
    except Exception:
        return jsonify({'status': 'error', 'message': 'WebApp task did not return JSON', 'stdout': stdout[:2000]}), 500
    if not isinstance(result, dict):
        return jsonify({'status': 'error', 'message': 'WebApp task returned a non-object JSON value'}), 500
    result.setdefault('status', 'success')
    return jsonify(result)


@app.route('/plugin-webapp-data/<plugin_id>/<token>/<path:filename>', methods=['GET', 'HEAD'])
def plugin_webapp_bridge_file(plugin_id, token, filename):
    cfg, err = _webapp_bridge_cfg(plugin_id)
    if err:
        payload, code = err
        return jsonify(payload), code
    if not _is_local_webapp_request():
        return jsonify({'status': 'error', 'message': 'Cross-origin bridge request denied'}), 403

    with _WEBAPP_FILE_TOKENS_LOCK:
        entry = dict(_WEBAPP_FILE_TOKENS.get(token) or {})
    if not entry or entry.get('plugin_id') != plugin_id:
        return jsonify({'status': 'error', 'message': 'File token not found'}), 404

    path = entry.get('path')
    ok, reason = _webapp_file_allowed(cfg, path)
    if not ok:
        return jsonify({'status': 'error', 'message': reason}), 403

    size = os.path.getsize(path)
    mime = entry.get('mime') or _webapp_file_mime(path)
    range_header = request.headers.get('Range', '').strip()
    headers = {
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-cache',
        'Content-Disposition': f"inline; filename*=UTF-8''{urllib.parse.quote(os.path.basename(path))}",
    }

    if not range_header:
        headers['Content-Length'] = str(size)
        if request.method == 'HEAD':
            return Response(status=200, mimetype=mime, headers=headers)

        def full_stream():
            with open(path, 'rb') as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        return Response(full_stream(), status=200, mimetype=mime, headers=headers)

    m = re.match(r'^bytes=(\d*)-(\d*)$', range_header)
    if not m:
        return Response(status=416, headers={'Content-Range': f'bytes */{size}', 'Accept-Ranges': 'bytes'})

    start_s, end_s = m.groups()
    if start_s == '' and end_s == '':
        return Response(status=416, headers={'Content-Range': f'bytes */{size}', 'Accept-Ranges': 'bytes'})
    if start_s == '':
        suffix = int(end_s)
        start = max(size - suffix, 0)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    if start >= size or start < 0 or end < start:
        return Response(status=416, headers={'Content-Range': f'bytes */{size}', 'Accept-Ranges': 'bytes'})
    end = min(end, size - 1)
    length = end - start + 1
    headers.update({
        'Content-Range': f'bytes {start}-{end}/{size}',
        'Content-Length': str(length),
    })
    if request.method == 'HEAD':
        return Response(status=206, mimetype=mime, headers=headers)

    def range_stream():
        remaining = length
        with open(path, 'rb') as fh:
            fh.seek(start)
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    return Response(range_stream(), status=206, mimetype=mime, headers=headers)

def _display_name(name):
    try:
        if isinstance(name, str):
            return name
        if isinstance(name, dict):
            return name.get('en') or name.get('zh') or next(iter(name.values()))
        return str(name)
    except Exception:
        return str(name)

def _evaluate_show_when(show_when, params):
    """
    在后端求值 showWhen 条件，判断该参数是否在当前参数组合下可见。
    支持: 单条件 dict, AND 数组, {all:[...]}, {any:[...]}
    """
    if not show_when:
        return True
    try:
        if isinstance(show_when, dict):
            # {any: [...]} — OR
            if 'any' in show_when:
                return any(_evaluate_show_when(c, params) for c in show_when['any'])
            # {all: [...]} — AND
            if 'all' in show_when:
                return all(_evaluate_show_when(c, params) for c in show_when['all'])
            # 原子条件 {param, op, value}
            p_id = show_when.get('param', '')
            op = show_when.get('op', 'eq')
            expected = show_when.get('value', '')
            actual = str(params.get(p_id, ''))
            if op == 'eq':    return actual == str(expected)
            if op == 'neq':   return actual != str(expected)
            if op == 'checked':   return actual.lower() in ('true', '1', 'on', 'yes')
            if op == 'unchecked': return actual.lower() not in ('true', '1', 'on', 'yes')
            if op == 'notEmpty':  return bool(actual)
            if op == 'empty':     return not bool(actual)
            return True  # 未知操作符默认可见
        if isinstance(show_when, list):
            # 裸数组 = AND
            return all(_evaluate_show_when(c, params) for c in show_when)
    except Exception:
        pass
    return True  # 求值失败默认可见


def _format_param_value(param, value):
    """Format a parameter value for execution log output."""
    try:
        if value is None or value == '':
            return '未设置'

        ptype = str(param.get('type', '')).lower()
        if ptype == 'file':
            def _short(v):
                try:
                    return os.path.basename(str(v)) or str(v)
                except Exception:
                    return str(v)
            if isinstance(value, list):
                flat = []
                for item in value:
                    if isinstance(item, list):
                        flat.extend([_short(x) for x in item if x])
                    elif item:
                        flat.append(_short(item))
                return ', '.join(flat) if flat else '未设置'
            return _short(value)

        if ptype == 'checkbox':
            enabled = bool(value) and str(value).lower() not in ('false', '0', 'none', 'off', '')
            if enabled:
                token = param.get('true_value')
                if token is None:
                    token = param.get('value')
                token_text = f" ({token})" if token not in (None, '', True) else ''
                return f'已启用{token_text}'
            return '未启用'

        if ptype == 'select':
            opts = param.get('options') or []
            for opt in opts:
                if isinstance(opt, dict) and str(opt.get('value', '')) == str(value):
                    return _display_name(opt.get('label', opt.get('value', value)))
                if not isinstance(opt, dict) and str(opt) == str(value):
                    return str(opt)
            return str(value)

        if isinstance(value, list):
            return ', '.join([str(v) for v in value if v not in (None, '')]) or '未设置'

        return str(value)
    except Exception:
        return str(value)


def _build_parameter_log_lines(cfg, params):
    """Build a readable parameter summary for the execution log."""
    lines = []
    seen = set()
    try:
        for param in cfg.get('parameters', []):
            pid = param.get('id')
            if not pid or param.get('type') in ('group_header', 'button'):
                continue
            if pid in seen:
                continue
            seen.add(pid)
            if pid not in params:
                continue
            value = params.get(pid)
            label = _display_name(param.get('label', pid))
            formatted = _format_param_value(param, value)
            lines.append(f"  - {label}: {formatted}")
    except Exception as e:
        lines.append(f"  - 参数清单生成失败: {e}")
    return lines

# === 查找真实配置文件的路径 ===
_config_path_cache = {}

def find_config_by_id(target_id):
    if not target_id:
        return None
    if target_id in _config_path_cache:
        cached = _config_path_cache[target_id]
        if os.path.exists(cached):
            return cached
        _config_path_cache.pop(target_id, None)
    for subcat in ['', 'linux/', 'r/']:
        p = os.path.join(PLUGINS_DIR, subcat, target_id, 'config.json')
        if os.path.exists(p):
            _config_path_cache[target_id] = p
            return p
    if not os.path.exists(PLUGINS_DIR):
        return None
    search_dirs = [PLUGINS_DIR]
    for subcat in ['linux', 'r']:
        subdir = os.path.join(PLUGINS_DIR, subcat)
        if os.path.isdir(subdir):
            search_dirs.append(subdir)
    for search_dir in search_dirs:
        for pid in os.listdir(search_dir):
            if search_dir == PLUGINS_DIR and pid in ['linux', 'r']:
                continue
            cpath = os.path.join(search_dir, pid, 'config.json')
            if os.path.exists(cpath):
                try:
                    cfg = _load_json_file(cpath)
                    if cfg.get('id') == target_id:
                        _config_path_cache[target_id] = cpath
                        return cpath
                except Exception:
                    pass
    return None

@app.route('/update-plugin-category', methods=['POST'])
def update_plugin_category():
    data = request.json
    try:
        target_path = find_config_by_id(data['pluginId'])
        if not target_path:
            return jsonify({"status": "error", "message": "无法找到该插件的配置文件 (ID不匹配)"})

        cfg = _load_json_file(target_path)

        raw_cat = str(data.get('category') or '').strip()
        if not raw_cat:
            raw_cat = '默认分类'

        action = data.get('action', 'add')  # 'add', 'remove', 'set'

        current = cfg.get('category', '')

        def _label(c):
            """Get display label from a category (string or i18n dict)."""
            if isinstance(c, dict):
                return c.get('en') or c.get('zh') or str(c)
            return str(c)

        if action == 'add':
            # Convert to list if not already
            if isinstance(current, list):
                cats = list(current)
            elif current:
                cats = [current]
            else:
                cats = []

            # Check if category with same label already present
            existing_labels = [_label(c) for c in cats]
            if raw_cat not in existing_labels:
                cats.append(raw_cat)

            # Store as single value if only one, else as array
            cfg['category'] = cats if len(cats) > 1 else (cats[0] if cats else raw_cat)

        elif action == 'remove':
            if isinstance(current, list):
                cats = [c for c in current if _label(c) != raw_cat]
                if len(cats) == 0:
                    cfg['category'] = '默认分类'
                elif len(cats) == 1:
                    cfg['category'] = cats[0]
                else:
                    cfg['category'] = cats
            elif _label(current) == raw_cat:
                cfg['category'] = '默认分类'
            # else: category doesn't match, no change

        else:  # 'set' — legacy behavior
            cfg['category'] = raw_cat

        if action == 'move':
            new_cat = data.get('category')
            if new_cat and isinstance(new_cat, dict):
                cfg['category'] = new_cat
            elif new_cat:
                cfg['category'] = new_cat

        # 'rename' — update category with bilingual object
        if action == 'rename':
            new_cat = data.get('newCategory')
            if new_cat:
                if isinstance(current, list):
                    old_label = data.get('oldLabel', '')
                    cats = []
                    for c in current:
                        if _label(c) == old_label:
                            cats.append(new_cat)
                        else:
                            cats.append(c)
                    cfg['category'] = cats if len(cats) > 1 else (cats[0] if cats else new_cat)
                else:
                    cfg['category'] = new_cat

        with open(target_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/install-plugin-panel', methods=['POST'])
def install_plugin_panel():
    data = request.json
    temp = tempfile.mkdtemp(prefix='primigenius_install_')
    try:

        # extract uploaded zip
        with zipfile.ZipFile(data['zipPath'], 'r') as z:
            z.extractall(temp)

        # find config.json inside extracted tree
        cfg_path = None
        for r, d, f in os.walk(temp):
            if 'config.json' in f:
                cfg_path = os.path.join(r, 'config.json')
                break

        if not cfg_path:
            try: shutil.rmtree(temp, ignore_errors=True)
            except: pass
            return jsonify({"status": "error", "message": "无法在压缩包中找到 config.json"})

        cfg = _load_json_file(cfg_path)

        # Check if plugin already exists (may have different folder name than id)
        existing_cfg = find_config_by_id(cfg.get('id', ''))
        if existing_cfg:
            dest = os.path.dirname(existing_cfg)
        else:
            # Determine subdirectory based on plugin type
            ptype = str(cfg.get('type', '')).lower()
            if ptype.startswith('r'):
                dest = os.path.join(PLUGINS_DIR, 'r', cfg['id'])
            else:
                dest = os.path.join(PLUGINS_DIR, 'linux', cfg['id'])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            try: shutil.rmtree(dest, ignore_errors=True)
            except: pass

        shutil.move(os.path.dirname(cfg_path), dest)

        # Refresh plugin cache so new version is reflected
        invalidate_plugin_cache()

        # best-effort cleanup; do not treat cleanup failure as install failure
        try:
            shutil.rmtree(temp, ignore_errors=True)
        except Exception as e:
            log(f"[WARN] Failed to remove temp_install: {e}")

        return jsonify({
            "status": "success",
            "pluginId": cfg.get("id", ""),
            "version": cfg.get("version", "")
        })
    except Exception as e:
        try: shutil.rmtree(temp, ignore_errors=True)
        except: pass
        return jsonify({"status": "error", "message": str(e)})

@app.route('/uninstall-plugin', methods=['POST'])
def uninstall_plugin():
    data = request.json
    config_path = find_config_by_id(data['pluginId'])
    if config_path:
        folder_path = os.path.dirname(config_path)
        try:
            # remove the plugin folder only; do not touch parent category dirs (metadata-only approach)
            shutil.rmtree(folder_path)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})
    return jsonify({"status": "error", "message": "Plugin not found"})

@app.route('/setup-plugin-binary', methods=['POST'])
def setup_plugin_binary():
    data = request.json
    cfg = load_all_plugins().get(data['pluginId'])
    if not cfg:
        return jsonify({"status": "error", "message": "Plugin not found"})
    target = os.path.join(cfg['plugin_dir'], 'bin')
    if os.path.exists(target): shutil.rmtree(target)
    os.makedirs(target)
    src_path = data['zipPath']
    try:
        _install_plugin_binary_impl(cfg, src_path, target, progress_cb=None)

        return jsonify({"status": "success"})
    except Exception as e:
        log(f"[ERROR] setup-plugin-binary failed: {e}")
        return jsonify({"status": "error", "message": str(e)})


def _set_binary_install_job(job_id, **fields):
    with _BINARY_INSTALL_JOBS_LOCK:
        j = _BINARY_INSTALL_JOBS.get(job_id) or {}
        j.update(fields)
        j['updated_at'] = time.time()
        _BINARY_INSTALL_JOBS[job_id] = j


def _create_binary_install_job(plugin_id, src_path):
    job_id = str(uuid.uuid4())
    _set_binary_install_job(
        job_id,
        status='running',
        progress=0,
        message='Queued',
        pluginId=plugin_id,
        zipPath=src_path,
        error=''
    )
    return job_id


def _extract_plugin_payload_with_progress(src_path, target, progress_cb=None):
    """Extract plugin payload and report progress percent in [10, 70]."""
    lp = src_path.lower()

    def _emit(idx, total):
        if not progress_cb:
            return
        if total <= 0:
            pct = 10
        else:
            pct = 10 + int(60 * (idx / total))
        progress_cb(min(70, max(10, pct)), 'Extracting package...')

    if lp.endswith('.zip'):
        with zipfile.ZipFile(src_path, 'r') as z:
            infos = z.infolist()
            total = max(1, len(infos))
            for i, info in enumerate(infos, start=1):
                z.extract(info, target)
                if i == total or i % 10 == 0:
                    _emit(i, total)
        return

    if lp.endswith('.tar.gz') or lp.endswith('.tgz'):
        extracted = False
        # Strategy 1: tarfile r:gz (standard gzip-compressed tar)
        try:
            with tarfile.open(src_path, 'r:gz') as tar:
                members = tar.getmembers()
                total = max(1, len(members))
                for i, m in enumerate(members, start=1):
                    tar.extract(m, target)
                    if i == total or i % 10 == 0:
                        _emit(i, total)
            extracted = True
        except tarfile.TarError:
            pass
        # Strategy 2: gzip decompress then tarfile r: (two-step for non-standard gzip)
        # Also handles double-gzip (e.g. .tar.gz where gzip layer wraps another gzip)
        if not extracted:
            try:
                import gzip as _gzip
                import tempfile as _tf
                _tmp = os.path.join(_tf.gettempdir(), '_primigenius_extract_tmp.tar')
                with _gzip.open(src_path, 'rb') as gz_in:
                    with open(_tmp, 'wb') as f_out:
                        shutil.copyfileobj(gz_in, f_out)
                # Check if the decompressed content is still gzip (double-gzip case)
                _decomp_attempts = 0
                while _decomp_attempts < 5:
                    with open(_tmp, 'rb') as _check_f:
                        _peek = _check_f.read(2)
                    if _peek[:2] == b'\x1f\x8b':
                        _tmp2 = _tmp + '.gz'
                        try:
                            with _gzip.open(_tmp, 'rb') as _gz2:
                                with open(_tmp2, 'wb') as _fo2:
                                    shutil.copyfileobj(_gz2, _fo2)
                            os.replace(_tmp2, _tmp)
                            _decomp_attempts += 1
                        except Exception:
                            try: os.remove(_tmp2)
                            except: pass
                            break
                    else:
                        break
                with tarfile.open(_tmp, 'r:') as tar:
                    members = tar.getmembers()
                    total = max(1, len(members))
                    for i, m in enumerate(members, start=1):
                        tar.extract(m, target)
                        if i == total or i % 10 == 0:
                            _emit(i, total)
                try: os.remove(_tmp)
                except: pass
                extracted = True
            except Exception:
                try: os.remove(_tmp)
                except: pass
        # Strategy 3: maybe it's actually a plain tar (wrong extension)
        if not extracted:
            try:
                with tarfile.open(src_path, 'r:') as tar:
                    members = tar.getmembers()
                    total = max(1, len(members))
                    for i, m in enumerate(members, start=1):
                        tar.extract(m, target)
                        if i == total or i % 10 == 0:
                            _emit(i, total)
                extracted = True
            except tarfile.TarError:
                pass
        # Strategy 4: system tar command (most tolerant)
        if not extracted:
            tar_cmd = shutil.which('tar')
            if tar_cmd:
                try:
                    proc = subprocess.run([tar_cmd, '-xf', src_path, '-C', target],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                          creationflags=_HIDDEN_SUBPROCESS_KWARGS.get('creationflags', 0),
                                          startupinfo=_HIDDEN_SUBPROCESS_KWARGS.get('startupinfo', None))
                    if proc.returncode == 0:
                        extracted = True
                except Exception:
                    pass
        if not extracted:
            raise RuntimeError(f"Failed to extract {src_path}: all strategies failed (tried r:gz, gzip+r:, r:, system tar)")
    elif lp.endswith('.tar.bz2') or lp.endswith('.tbz2'):
        mode = 'r:bz2'
    elif lp.endswith('.tar.xz') or lp.endswith('.txz'):
        mode = 'r:xz'
    elif lp.endswith('.tar'):
        mode = 'r:'
    else:
        shutil.copy2(src_path, target)
        if progress_cb:
            progress_cb(70, 'Package copied')
        return

    if lp.endswith('.tar.gz') or lp.endswith('.tgz'):
        pass
    else:
        with tarfile.open(src_path, mode) as tar:
            members = tar.getmembers()
            total = max(1, len(members))
            for i, m in enumerate(members, start=1):
                tar.extract(m, target)
                if i == total or i % 10 == 0:
                    _emit(i, total)

    # Handle nested archives (e.g. .tar.gz containing .tar)
    for _ in range(3):
        nested = None
        for f in os.listdir(target):
            fl = f.lower()
            if fl.endswith('.tar.gz') or fl.endswith('.tgz') or fl.endswith('.tar.bz2') or fl.endswith('.tbz2') or fl.endswith('.tar.xz') or fl.endswith('.txz') or fl.endswith('.tar') or fl.endswith('.zip'):
                nested = os.path.join(target, f)
                break
        if not nested:
            break
        nl = nested.lower()
        tmp_extract = target + '__nested_tmp'
        os.makedirs(tmp_extract, exist_ok=True)
        nested_extracted = False
        try:
            if nl.endswith('.zip'):
                with zipfile.ZipFile(nested, 'r') as z:
                    z.extractall(tmp_extract)
                nested_extracted = True
            elif nl.endswith('.tar.gz') or nl.endswith('.tgz'):
                try:
                    with tarfile.open(nested, 'r:gz') as t:
                        t.extractall(tmp_extract)
                    nested_extracted = True
                except tarfile.TarError:
                    try:
                        import gzip as _gzip2
                        _tmp2 = os.path.join(tempfile.gettempdir(), '_primigenius_nested_tmp.tar')
                        with _gzip2.open(nested, 'rb') as gz2:
                            with open(_tmp2, 'wb') as fo2:
                                shutil.copyfileobj(gz2, fo2)
                        with tarfile.open(_tmp2, 'r:') as t2:
                            t2.extractall(tmp_extract)
                        try: os.remove(_tmp2)
                        except: pass
                        nested_extracted = True
                    except Exception:
                        try: os.remove(_tmp2)
                        except: pass
                if not nested_extracted:
                    try:
                        with tarfile.open(nested, 'r:') as t:
                            t.extractall(tmp_extract)
                        nested_extracted = True
                    except tarfile.TarError:
                        pass
            elif nl.endswith('.tar.bz2') or nl.endswith('.tbz2'):
                try:
                    with tarfile.open(nested, 'r:bz2') as t:
                        t.extractall(tmp_extract)
                    nested_extracted = True
                except tarfile.TarError:
                    pass
            elif nl.endswith('.tar.xz') or nl.endswith('.txz'):
                try:
                    with tarfile.open(nested, 'r:xz') as t:
                        t.extractall(tmp_extract)
                    nested_extracted = True
                except tarfile.TarError:
                    pass
            elif nl.endswith('.tar'):
                try:
                    with tarfile.open(nested, 'r:') as t:
                        t.extractall(tmp_extract)
                    nested_extracted = True
                except tarfile.TarError:
                    pass
            # Fallback: system tar for any nested archive
            if not nested_extracted:
                tar_cmd = shutil.which('tar')
                if tar_cmd and (nl.endswith('.tar') or nl.endswith('.tar.gz') or nl.endswith('.tgz') or nl.endswith('.tar.bz2') or nl.endswith('.tbz2') or nl.endswith('.tar.xz') or nl.endswith('.txz')):
                    try:
                        proc = subprocess.run([tar_cmd, '-xf', nested, '-C', tmp_extract],
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                              creationflags=_HIDDEN_SUBPROCESS_KWARGS.get('creationflags', 0),
                                              startupinfo=_HIDDEN_SUBPROCESS_KWARGS.get('startupinfo', None))
                        if proc.returncode == 0:
                            nested_extracted = True
                    except Exception:
                        pass
            if not nested_extracted:
                shutil.rmtree(tmp_extract, ignore_errors=True)
                break
            os.remove(nested)
            for item in os.listdir(tmp_extract):
                s = os.path.join(tmp_extract, item)
                d = os.path.join(target, item)
                if os.path.exists(d):
                    if os.path.isdir(d):
                        shutil.rmtree(d, ignore_errors=True)
                    else:
                        os.remove(d)
                shutil.move(s, d)
            shutil.rmtree(tmp_extract, ignore_errors=True)
        except Exception as e:
            shutil.rmtree(tmp_extract, ignore_errors=True)
            break


def _install_plugin_binary_impl(cfg, src_path, target, progress_cb=None):
    if progress_cb:
        progress_cb(2, 'Preparing installation...')

    if os.path.exists(target):
        shutil.rmtree(target)
    os.makedirs(target)

    if progress_cb:
        progress_cb(8, 'Reading package...')

    _extract_plugin_payload_with_progress(src_path, target, progress_cb)

    if progress_cb:
        progress_cb(75, 'Normalizing directory structure...')

    _flatten_single_wrapper(target)

    if os.name != 'nt':
        all_files = []
        for root, dirs, files in os.walk(target):
            for f in files:
                all_files.append(os.path.join(root, f))
        total_files = max(1, len(all_files))
        for i, fpath in enumerate(all_files, start=1):
            try:
                st = os.stat(fpath)
                os.chmod(fpath, st.st_mode | 0o755)
            except:
                pass
            if progress_cb and (i == total_files or i % 50 == 0):
                pct = 80 + int(15 * (i / total_files))
                progress_cb(min(95, max(80, pct)), 'Applying permissions...')

    if progress_cb:
        progress_cb(98, 'Refreshing plugin cache...')

    invalidate_plugin_cache()

    if progress_cb:
        progress_cb(100, 'Installation complete')


@app.route('/setup-plugin-binary-start', methods=['POST'])
def setup_plugin_binary_start():
    data = request.json or {}
    plugin_id = data.get('pluginId')
    src_path = data.get('zipPath')
    cfg = load_all_plugins().get(plugin_id)
    if not cfg:
        return jsonify({"status": "error", "message": "Plugin not found"})
    if not src_path or not os.path.exists(src_path):
        return jsonify({"status": "error", "message": "Source package not found"})

    target = os.path.join(cfg['plugin_dir'], 'bin')
    job_id = _create_binary_install_job(plugin_id, src_path)

    def _worker():
        try:
            _install_plugin_binary_impl(
                cfg,
                src_path,
                target,
                progress_cb=lambda pct, msg: _set_binary_install_job(job_id, progress=int(pct), message=str(msg), status='running')
            )
            _set_binary_install_job(job_id, status='success', progress=100, message='Installation complete')
        except Exception as e:
            log(f"[ERROR] setup-plugin-binary-start failed: {e}")
            _set_binary_install_job(job_id, status='failed', message='Installation failed', error=str(e))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return jsonify({"status": "success", "job_id": job_id})


@app.route('/setup-plugin-binary-status', methods=['GET'])
def setup_plugin_binary_status():
    job_id = request.args.get('job_id', '').strip()
    if not job_id:
        return jsonify({"status": "error", "message": "job_id is required"})
    with _BINARY_INSTALL_JOBS_LOCK:
        j = _BINARY_INSTALL_JOBS.get(job_id)
    if not j:
        return jsonify({"status": "error", "message": "job not found"})
    return jsonify({
        "status": "success",
        "job": {
            "status": j.get('status', 'running'),
            "progress": int(j.get('progress', 0) or 0),
            "message": j.get('message', ''),
            "error": j.get('error', ''),
            "pluginId": j.get('pluginId', ''),
            "updated_at": j.get('updated_at', 0)
        }
    })


def _extract_plugin_payload(src_path, target):
    """Extract plugin archive with faster native tools when available, fallback to Python stdlib."""
    lp = src_path.lower()

    # Tar family: prefer external `tar` (faster C implementation) when available.
    if lp.endswith('.tar.gz') or lp.endswith('.tgz') or lp.endswith('.tar.bz2') or lp.endswith('.tbz2') or lp.endswith('.tar.xz') or lp.endswith('.txz') or lp.endswith('.tar'):
        tar_cmd = shutil.which('tar')
        if tar_cmd:
            proc = subprocess.run([tar_cmd, '-xf', src_path, '-C', target], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   creationflags=_HIDDEN_SUBPROCESS_KWARGS.get('creationflags', 0),
                                   startupinfo=_HIDDEN_SUBPROCESS_KWARGS.get('startupinfo', None))
            if proc.returncode == 0:
                return

        # Fallback: Python tarfile
        if lp.endswith('.tar.gz') or lp.endswith('.tgz'):
            try:
                with tarfile.open(src_path, 'r:gz') as tar:
                    tar.extractall(target)
                return
            except tarfile.TarError:
                pass
            import gzip as _gzip_fb
            import tempfile as _tf_fb
            _tmp_fb = os.path.join(_tf_fb.gettempdir(), '_primigenius_fb_extract.tar')
            try:
                with _gzip_fb.open(src_path, 'rb') as _gz_fb:
                    with open(_tmp_fb, 'wb') as _fo_fb:
                        shutil.copyfileobj(_gz_fb, _fo_fb)
                _da = 0
                while _da < 5:
                    with open(_tmp_fb, 'rb') as _chk:
                        _pk = _chk.read(2)
                    if _pk[:2] == b'\x1f\x8b':
                        _tmp_fb2 = _tmp_fb + '.gz'
                        try:
                            with _gzip_fb.open(_tmp_fb, 'rb') as _gz2_fb:
                                with open(_tmp_fb2, 'wb') as _fo2_fb:
                                    shutil.copyfileobj(_gz2_fb, _fo2_fb)
                            os.replace(_tmp_fb2, _tmp_fb)
                            _da += 1
                        except Exception:
                            try: os.remove(_tmp_fb2)
                            except: pass
                            break
                    else:
                        break
                with tarfile.open(_tmp_fb, 'r:') as tar:
                    tar.extractall(target)
                try: os.remove(_tmp_fb)
                except: pass
                return
            except Exception:
                try: os.remove(_tmp_fb)
                except: pass
            try:
                with tarfile.open(src_path, 'r:') as tar:
                    tar.extractall(target)
                return
            except tarfile.TarError:
                pass
        if lp.endswith('.tar.bz2') or lp.endswith('.tbz2'):
            with tarfile.open(src_path, 'r:bz2') as tar:
                tar.extractall(target)
            return
        if lp.endswith('.tar.xz') or lp.endswith('.txz'):
            with tarfile.open(src_path, 'r:xz') as tar:
                tar.extractall(target)
            return
        with tarfile.open(src_path, 'r:') as tar:
            tar.extractall(target)
        return

    if lp.endswith('.zip'):
        with zipfile.ZipFile(src_path, 'r') as z:
            z.extractall(target)
        return

    # Treat as a raw binary/source file — copy directly into bin/
    shutil.copy2(src_path, target)


def _flatten_single_wrapper(target):
    """
    If 'target' contains a single subdirectory and nothing else, move that
    subdirectory's contents up into target.  Repeat until stable.
    This handles GitHub source-code zips that wrap everything in a dir like
    'fastp-main/' or 'STAR-2.7.11b/'.
    """
    for _ in range(5):  # max 5 levels of unwrapping
        entries = os.listdir(target)
        if len(entries) != 1:
            break

        sole = os.path.join(target, entries[0])
        if not os.path.isdir(sole):
            break

        # Fast path: rename directory instead of moving files one by one.
        parent = os.path.dirname(target)
        tmp_unwrap = os.path.join(parent, os.path.basename(target) + '__unwrap_tmp')
        if os.path.exists(tmp_unwrap):
            shutil.rmtree(tmp_unwrap, ignore_errors=True)

        shutil.move(sole, tmp_unwrap)
        shutil.rmtree(target, ignore_errors=True)
        os.rename(tmp_unwrap, target)


def _plugin_dockerfile_path(cfg):
    plugin_dir = str((cfg or {}).get('plugin_dir') or '').strip()
    if not plugin_dir:
        return None
    dockerfile = os.path.join(plugin_dir, 'Dockerfile')
    return dockerfile if os.path.isfile(dockerfile) else None


def _try_build_plugin_image(client, cfg, image_ref):
    """Build plugin image from plugin-local Dockerfile when image cannot be pulled."""
    dockerfile_path = _plugin_dockerfile_path(cfg)
    if not dockerfile_path:
        return False

    plugin_dir = str(cfg.get('plugin_dir') or '').strip()
    plugin_id = str(cfg.get('id') or os.path.basename(plugin_dir) or 'unknown_plugin')
    log(f"[SYSTEM] Auto-build image for plugin {plugin_id}: {image_ref}")
    log(f"[SYSTEM] Build context: {plugin_dir}")

    try:
        stream = client.api.build(
            path=plugin_dir,
            dockerfile='Dockerfile',
            tag=image_ref,
            rm=True,
            decode=True,
            pull=False
        )
        for chunk in stream:
            if not isinstance(chunk, dict):
                continue
            if chunk.get('stream'):
                line = str(chunk.get('stream')).strip()
                if line:
                    log(f"[BUILD] {line}")
            if chunk.get('error'):
                raise RuntimeError(str(chunk.get('error')))
        client.images.get(image_ref)
        _image_exists_cache[image_ref] = True
        log(f"[SYSTEM] Auto-build success: {image_ref}")
        return True
    except Exception as e:
        log(f"[WARN] Auto-build image failed ({image_ref}): {e}")
        return False

def _run_native_gui_plugin(cfg, data):
    _LOG_CONTEXT.channel = 'Linux'
    plugin_name = _get_plugin_name(cfg)
    log(f"[SYSTEM] Native GUI launch: {plugin_name}")

    if cfg.get('java_app'):
        java_version = get_plugin_java_version(cfg)
        java_label = _JAVA_RUNTIMES.get(java_version, {}).get('label', f'JRE {java_version}')
        java_exe = get_java_exe(gui=True, version=java_version)
        if not java_exe:
            log(f"[SYSTEM] {java_label} not found, auto-installing embedded runtime...")
            if not ensure_java_environment(java_version):
                return jsonify({"status": "error", "message": f"Java runtime setup failed: embedded {java_label} is missing or could not be extracted"})
            java_exe = get_java_exe(gui=True, version=java_version)
            if not java_exe:
                return jsonify({"status": "error", "message": "Java 环境不可用"})

        java_console_exe = get_java_exe(gui=False, version=java_version) or java_exe
        java_home = get_java_home(java_version)
        log(f"[SYSTEM] Using {java_label}: {java_console_exe}")

        main_jar = cfg.get('main_jar', '')
        jar_path = os.path.join(cfg['plugin_dir'], 'bin', main_jar)
        if not os.path.isfile(jar_path):
            log(f"[ERROR] JAR file not found: {jar_path}")
            return jsonify({"status": "error", "message": f"JAR 文件未找到: {main_jar}，请先安装插件依赖"})

        jvm_args_str = cfg.get('jvm_args', '')
        jvm_args_list = jvm_args_str.split() if jvm_args_str else []

        _LINUX_ONLY_JVM_PREFIXES = (
            '-Dswing.crossplatformlaf=com.sun.java.swing.plaf.gtk',
            '-Dsun.java2d.xrender',
            '-Dsun.awt.shell.ShellFolder',
            '-Dswing.defaultlaf=com.sun.java.swing.plaf.gtk',
        )
        if platform.system() == 'Windows':
            filtered = []
            for a in jvm_args_list:
                if any(a.startswith(p) for p in _LINUX_ONLY_JVM_PREFIXES):
                    log(f"[SYSTEM] Stripping Linux-only JVM arg on Windows: {a}")
                    continue
                filtered.append(a)
            jvm_args_list = filtered

        app_args_str = cfg.get('app_args', '')
        app_args_list = app_args_str.split() if app_args_str else []

        launch_parts = [java_exe] + jvm_args_list + ['-jar', jar_path] + app_args_list
        cmd_line = ' '.join(f'"{p}"' if ' ' in p else p for p in launch_parts)

        log(f"[SYSTEM] Launching: {cmd_line}")
        try:
            import subprocess
            import threading

            java_dir = os.path.join(APP_INSTALL_DIR, 'java')
            os.makedirs(java_dir, exist_ok=True)
            log_file = os.path.join(java_dir, 'gui_stderr.log')

            if os.name == 'nt':
                user_home = _resolve_windows_gui_home()
                if not user_home or not os.path.isdir(user_home):
                    user_home = os.path.expanduser('~')
                log(f"[SYSTEM] Windows GUI user home: {user_home}")
                log(f"[SYSTEM] HOME env: {os.environ.get('HOME', '(not set)')}")
                log(f"[SYSTEM] USERPROFILE env: {os.environ.get('USERPROFILE', '(not set)')}")

                env = os.environ.copy()
                env['HOME'] = user_home
                env['USERPROFILE'] = user_home
                if java_home:
                    env['JAVA_HOME'] = java_home
                    env['PATH'] = os.path.join(java_home, 'bin') + os.pathsep + env.get('PATH', '')
                if len(user_home) >= 2 and user_home[1] == ':':
                    env['HOMEDRIVE'] = user_home[:2]
                    env['HOMEPATH'] = user_home[2:]
                for key in list(env.keys()):
                    if key.upper() in ('DISPLAY', 'WAYLAND_DISPLAY', 'XDG_SESSION_TYPE',
                                       'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
                        del env[key]
                    if key == 'HOME' and env[key] != user_home:
                        env[key] = user_home

                jvm_args_list = [a for a in jvm_args_list
                                 if not a.startswith('-Duser.dir=') and not a.startswith('-Duser.home=')]
                jvm_args_list.append(f'-Duser.home={user_home}')

                java_exe_cmd = java_console_exe

                launch_cmd = [java_exe_cmd] + jvm_args_list + ['-jar', jar_path] + app_args_list
                cmd_str = ' '.join(f'"{p}"' if ' ' in p else p for p in launch_cmd)
                log(f"[SYSTEM] Launching: {cmd_str}")

                bat_path = os.path.join(java_dir, 'launch_gui.bat')
                with open(bat_path, 'w', encoding='utf-8') as bf:
                    bf.write('@echo off\n')
                    bf.write(f'cd /d "{java_dir}"\n')
                    bf.write(f'set "HOME={user_home}"\n')
                    bf.write(f'set "USERPROFILE={user_home}"\n')
                    bf.write(f'set "HOMEDRIVE={user_home[:2]}"\n')
                    bf.write(f'set "HOMEPATH={user_home[2:]}"\n')
                    bf.write('set "DISPLAY="\n')
                    bf.write('set "WAYLAND_DISPLAY="\n')
                    bf.write('set "XDG_SESSION_TYPE="\n')
                    jvm_bat_str = ' '.join(f'"{a}"' if ' ' in a else a for a in jvm_args_list)
                    app_bat_str = ' '.join(f'"{a}"' if ' ' in a else a for a in app_args_list)
                    bf.write(f'"{java_exe_cmd}" {jvm_bat_str} -jar "{jar_path}" {app_bat_str} 2>"{log_file}"\n')
                log(f"[SYSTEM] BAT file written: {bat_path}")

                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0

                # Determine working directory: use configured working_dir if specified, otherwise use java_dir
                working_dir = java_dir
                if cfg.get('working_dir'):
                    configured_cwd = os.path.join(cfg['plugin_dir'], cfg['working_dir'])
                    if os.path.isdir(configured_cwd):
                        working_dir = configured_cwd
                        log(f"[SYSTEM] Using configured working directory: {working_dir}")

                log_fh = open(log_file, 'w', encoding='utf-8', errors='replace')
                proc = subprocess.Popen(
                    launch_cmd,
                    cwd=working_dir,
                    env=env,
                    stderr=log_fh,
                    startupinfo=si,
                    creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                log(f"[SYSTEM] Java process PID: {proc.pid}")
            else:
                bat_path = os.path.join(java_dir, 'launch_gui.bat')
                with open(bat_path, 'w', encoding='utf-8') as bf:
                    bf.write('@echo off\n')
                    bf.write(f'"{java_exe}" {" ".join(jvm_args_list)} -jar "{jar_path}" {" ".join(app_args_list)} 2>"{log_file}"\n')

                vbs_path = os.path.join(java_dir, 'launch_gui.vbs')
                with open(vbs_path, 'w', encoding='utf-8') as vf:
                    vf.write('Set objShell = CreateObject("WScript.Shell")\n')
                    vf.write(f'objShell.Run """{bat_path}""", 0, False\n')
                subprocess.Popen(
                    ['wscript.exe', '//B', '//Nologo', vbs_path],
                    cwd=os.path.expanduser('~'),
                )

            def _tail_java_log():
                import time
                time.sleep(3)
                try:
                    with open(log_file, 'r', encoding='utf-8', errors='replace') as lf:
                        deadline = time.time() + 30
                        while time.time() < deadline:
                            line = lf.readline()
                            if line:
                                log(line.rstrip())
                            else:
                                time.sleep(0.5)
                except Exception:
                    pass

            t = threading.Thread(target=_tail_java_log, daemon=True)
            t.start()

            log(f"[SYSTEM] {plugin_name} launched successfully")
            return jsonify({"status": "success", "message": f"{plugin_name} 已启动"})
        except Exception as e:
            log(f"[ERROR] Failed to launch {plugin_name}: {e}")
            return jsonify({"status": "error", "message": f"启动失败: {e}"})
    launch_command = str(cfg.get('launch_command') or '').strip()
    if launch_command:
        java_version = get_plugin_java_version(cfg) if cfg.get('java_version') else None
        java_home = get_java_home(java_version) if java_version else None
        if java_version and not java_home:
            java_label = _JAVA_RUNTIMES.get(java_version, {}).get('label', f'JRE {java_version}')
            log(f"[SYSTEM] {java_label} not found, auto-installing embedded runtime...")
            if not ensure_java_environment(java_version):
                return jsonify({"status": "error", "message": f"Java runtime setup failed: embedded {java_label} is missing or could not be extracted"})
            java_home = get_java_home(java_version)

        working_dir = cfg['plugin_dir']
        if cfg.get('working_dir'):
            configured_cwd = os.path.join(cfg['plugin_dir'], cfg['working_dir'])
            if os.path.isdir(configured_cwd):
                working_dir = configured_cwd

        command_path = launch_command
        if not os.path.isabs(command_path):
            command_path = os.path.join(working_dir, command_path)
        if not os.path.exists(command_path):
            return jsonify({"status": "error", "message": f"启动文件未找到: {launch_command}"})

        app_args = shlex.split(str(cfg.get('app_args') or ''), posix=False)
        env = os.environ.copy()
        if java_home:
            env['JAVA_HOME'] = java_home
            env['PATH'] = os.path.join(java_home, 'bin') + os.pathsep + env.get('PATH', '')

        try:
            import subprocess

            log_file = os.path.join(APP_INSTALL_DIR, 'java', 'gui_stderr.log')
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            log_fh = open(log_file, 'w', encoding='utf-8', errors='replace')

            if os.name == 'nt' and command_path.lower().endswith(('.bat', '.cmd')):
                launch_cmd = [os.environ.get('COMSPEC', 'cmd.exe'), '/c', command_path] + app_args
            else:
                launch_cmd = [command_path] + app_args

            log(f"[SYSTEM] Launching command: {' '.join(launch_cmd)}")
            si = subprocess.STARTUPINFO() if os.name == 'nt' else None
            if si:
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
            creationflags = 0
            if os.name == 'nt':
                creationflags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
            proc = subprocess.Popen(
                launch_cmd,
                cwd=working_dir,
                env=env,
                stdout=log_fh,
                stderr=log_fh,
                startupinfo=si,
                creationflags=creationflags,
            )
            log(f"[SYSTEM] Native process PID: {proc.pid}")
            return jsonify({"status": "success", "message": f"{plugin_name} 已启动"})
        except Exception as e:
            log(f"[ERROR] Failed to launch {plugin_name}: {e}")
            return jsonify({"status": "error", "message": f"启动失败: {e}"})

    return jsonify({"status": "error", "message": "不支持的 native_gui 插件类型"})


def _run_native_java_cli_plugin(cfg, data):
    _LOG_CONTEXT.channel = 'Linux'
    plugin_name = _get_plugin_name(cfg)
    log(f"[SYSTEM] Native Java CLI launch: {plugin_name}")

    java_version = get_plugin_java_version(cfg)
    java_label = _JAVA_RUNTIMES.get(java_version, {}).get('label', f'JRE {java_version}')
    java_exe = get_java_exe(gui=False, version=java_version)
    if not java_exe:
        log(f"[SYSTEM] {java_label} not found, auto-installing embedded runtime...")
        if not ensure_java_environment(java_version):
            return jsonify({"status": "error", "message": f"Java runtime setup failed: embedded {java_label} is missing or could not be extracted"})
        java_exe = get_java_exe(gui=False, version=java_version)
        if not java_exe:
            return jsonify({"status": "error", "message": "Java 环境不可用"})

    main_jar = cfg.get('main_jar', '')
    jar_path = os.path.join(cfg['plugin_dir'], 'bin', main_jar)
    if not os.path.isfile(jar_path):
        return jsonify({"status": "error", "message": f"JAR 文件未找到: {main_jar}，请先安装插件依赖"})

    host_out_dir = os.path.normpath(os.path.abspath(data.get('outDir') or ''))
    if not host_out_dir:
        return jsonify({"status": "error", "message": "缺少输出目录"})
    os.makedirs(host_out_dir, exist_ok=True)

    params = dict(data.get('params') or {})
    visible_file_set = set(data.get('_visibleFileParams') or [])
    file_params = {}
    for p in cfg.get('parameters', []):
        if p.get('type') != 'file':
            continue
        pid = p.get('id')
        raw = params.get(pid)
        val_list = raw if isinstance(raw, list) else ([raw] if raw else [])
        val_list = [v for v in val_list if v]
        if not val_list and p.get('required'):
            if visible_file_set and pid not in visible_file_set:
                continue
            if p.get('showWhen') and not _evaluate_show_when(p['showWhen'], params):
                continue
            return jsonify({"status": "error", "message": f"缺少必要文件: {_display_name(p.get('label'))}"})
        for item in val_list:
            if isinstance(item, str) and item and not os.path.exists(item):
                return jsonify({"status": "error", "message": f"输入文件不存在: {item}"})
        file_params[pid] = val_list

    execution_mode = cfg.get('execution_mode', 'loop')
    tasks = []
    if execution_mode == 'merge':
        task_params = params.copy()
        for pid, files in file_params.items():
            task_params[pid] = files
        sample_name = "analysis_task"
        if file_params:
            first_files = next(iter(file_params.values()))
            if first_files:
                sample_name = f"{extract_sample_name(first_files[0])}_combined_analysis"
        tasks.append({'params': task_params, 'sample': sample_name})
    else:
        passthrough_params = set(cfg.get('loop_passthrough_file_params') or [])
        loop_file_params = {
            pid: files for pid, files in file_params.items()
            if pid not in passthrough_params
        }
        if cfg.get('loop_strict_pairing'):
            multi_counts = {len(files) for files in loop_file_params.values() if len(files) > 1}
            if len(multi_counts) > 1:
                details = ', '.join(f"{pid}={len(files)}" for pid, files in loop_file_params.items() if files)
                return jsonify({
                    "status": "error",
                    "message": f"循环文件数量不匹配（{details}）。多文件参数必须数量一致；单个文件可在全部循环中共用。"
                })
        max_cycles = max([len(v) for v in loop_file_params.values()] + [1])
        for i in range(max_cycles):
            snapshot = params.copy()
            current_sample = f"result_{i + 1}"
            found_variable_param = False
            for pid, files in file_params.items():
                if pid in passthrough_params:
                    snapshot[pid] = files
                    continue
                if not files:
                    snapshot[pid] = None
                    continue
                target_item = files[i % len(files)] if len(files) > 1 else files[0]
                if len(files) > 1 or not found_variable_param:
                    current_sample = extract_sample_name(target_item)
                    found_variable_param = True
                snapshot[pid] = target_item
            tasks.append({'params': snapshot, 'sample': current_sample})

    def _stringify_value(value):
        if value is None:
            return ''
        if isinstance(value, list):
            return ' '.join(_stringify_value(v) for v in value)
        return str(value)

    def _render_template_arg(template, task_params, sample_name):
        value = str(template)
        replacements = {
            'output_dir': host_out_dir,
            'sample_name': sample_name,
            'plugin_dir': cfg['plugin_dir'],
        }
        for key, val in task_params.items():
            replacements[key] = _stringify_value(val)
        for key, val in replacements.items():
            value = value.replace('{' + key + '}', str(val))
        value = value.replace('/', os.sep)
        return value

    def _build_app_args(task_params, sample_name):
        if cfg.get('arg_templates'):
            out = []
            for item in cfg.get('arg_templates') or []:
                if isinstance(item, dict):
                    if item.get('showWhen') and not _evaluate_show_when(item.get('showWhen'), task_params):
                        continue
                    rendered = _render_template_arg(item.get('value', ''), task_params, sample_name).strip()
                    if not rendered:
                        continue
                    if item.get('split'):
                        out.extend(shlex.split(rendered, posix=False))
                    else:
                        out.append(rendered)
                else:
                    rendered = _render_template_arg(item, task_params, sample_name).strip()
                    if rendered:
                        out.append(rendered)
            return out

        cmd = _render_template_arg(cfg.get('command_template', ''), task_params, sample_name)
        return shlex.split(cmd, posix=False) if cmd else []

    jvm_args = shlex.split(str(cfg.get('jvm_args') or ''), posix=False)
    java_home = get_java_home(java_version)
    env = os.environ.copy()
    if java_home:
        env['JAVA_HOME'] = java_home
        env['PATH'] = os.path.join(java_home, 'bin') + os.pathsep + env.get('PATH', '')

    failed_tasks = []
    for idx, task in enumerate(tasks):
        import subprocess
        t_start = time.time()
        start_dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        app_args = _build_app_args(task['params'], task['sample'])
        cmd_list = [java_exe] + jvm_args + ['-jar', jar_path] + app_args
        safe_plugin_id = (cfg.get('id') or 'unknown').replace(' ', '_')
        log_path = os.path.join(host_out_dir, f"{safe_plugin_id}.{task['sample']}.primigenius.log")

        log(f"[SYSTEM] === Running Native Java Task {idx + 1}/{len(tasks)}: {task['sample']} ===")
        log(f"[CMD] {' '.join(cmd_list)}")

        with open(log_path, 'w', encoding='utf-8', buffering=1, errors='replace') as lf:
            lf.write("============================================================\n")
            lf.write(" PrimiGenius Native Java Execution Log\n")
            lf.write("============================================================\n")
            lf.write(f"Tool Name    : {plugin_name}\n")
            lf.write(f"Java Runtime : {java_label}\n")
            lf.write(f"Task Name    : {task['sample']}\n")
            lf.write(f"Start Time   : {start_dt}\n")
            lf.write(f"Command      : {' '.join(cmd_list)}\n")
            param_lines = _build_parameter_log_lines(cfg, task.get('params', {}))
            if param_lines:
                lf.write("Parameters   :\n")
                for line in param_lines:
                    lf.write(f"{line}\n")
            lf.write("------------------------------------------------------------\n\n")

            try:
                proc = subprocess.Popen(
                    cmd_list,
                    cwd=os.path.join(cfg['plugin_dir'], cfg.get('working_dir', 'bin')),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                )
                for line in proc.stdout:
                    line = line.rstrip()
                    log(line)
                    lf.write(line + '\n')
                exit_code = proc.wait()
            except Exception:
                exit_code = -1
                err_msg = traceback.format_exc()
                log(err_msg)
                lf.write(err_msg)

            duration_str = format_duration(time.time() - t_start)
            end_dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lf.write("\n------------------------------------------------------------\n")
            lf.write(f"End Time     : {end_dt}\n")
            lf.write(f"Total Time   : {duration_str}\n")
            if exit_code != 0:
                failed_tasks.append(task['sample'])
                lf.write(f"Status       : FAILED ({exit_code})\n")
                log(f"[ERROR] Task {task['sample']} failed with exit code {exit_code}")
            else:
                lf.write("Status       : SUCCESS\n")
                log(f"[SYSTEM] Task {task['sample']} completed successfully in {duration_str}.")
            lf.write("============================================================\n")

    if failed_tasks:
        return jsonify({"status": "error", "message": f"{len(failed_tasks)}/{len(tasks)} 个任务失败: {', '.join(failed_tasks[:5])}\n请查看输出目录中的 .primigenius.log 日志文件了解详情。"})
    return jsonify({"status": "success"})

@app.route('/run-plugin', methods=['POST'])
@_track_analysis_request
def run_plugin():
    data = request.json or {}
    _linux_textarea_temp_files = []
    _prev_run_id = getattr(_LOG_CONTEXT, 'run_id', None)
    _prev_channel = getattr(_LOG_CONTEXT, 'channel', None)
    _LOG_CONTEXT.run_id = data.get('clientRunId')
    _LOG_CONTEXT.channel = 'Linux'
    try:
        log(f"[SYSTEM] run_plugin request: {json.dumps({'pluginId': data.get('pluginId'), 'outDir': data.get('outDir')})}")
        cfg = load_all_plugins().get(data.get('pluginId'))
        if not cfg:
            log(f"[ERROR] Plugin config not found for id: {data.get('pluginId')}")
            return jsonify({"status":"error", "message": "Plugin not found on server"})

        # === native_gui mode: launch Java GUI directly on Windows (no Docker needed) ===
        if cfg.get('run_mode') == 'native_gui':
            return _run_native_gui_plugin(cfg, data)
        if cfg.get('run_mode') == 'native_java_cli':
            return _run_native_java_cli_plugin(cfg, data)

        client = get_docker_client()
        if not client: 
            log('[ERROR] run_plugin: Docker client unavailable')
            return jsonify({"status":"error", "message":"Docker 未启动"})

        # R plugins always use the pinned system runtime. Their config image is
        # retained as backwards-compatible metadata only.
        is_r_plugin = str(cfg.get('type') or '').strip().lower().startswith('r')
        if is_r_plugin:
            cfg['configured_docker_image'] = cfg.get('docker_image')
            cfg['docker_image'] = R_DOCKER_IMAGE
        elif 'docker_image' not in cfg:
            cfg['docker_image'] = R_DOCKER_IMAGE

        # Auto-upgrade R base image to custom image with system deps pre-installed
        if cfg['docker_image'] == R_DOCKER_IMAGE:
            cfg['docker_image'] = _get_r_image()
            if cfg['docker_image'] == R_DOCKER_IMAGE:
                if _ensure_custom_r_image(client, silent=True):
                    cfg['docker_image'] = R_CUSTOM_IMAGE

        log(f"[SYSTEM] Task received: {data['pluginId']}")
        img = cfg['docker_image']
        # Ensure docker image is available locally (skip check if cached)
        if img not in _image_exists_cache:
            try:
                client.images.get(img)
                _image_exists_cache[img] = True
            except docker.errors.ImageNotFound:
                if img == R_CUSTOM_IMAGE:
                    if _ensure_custom_r_image(client, silent=True):
                        img = R_CUSTOM_IMAGE
                        cfg['docker_image'] = img
                        _image_exists_cache[img] = True
                    else:
                        img = R_DOCKER_IMAGE
                        cfg['docker_image'] = img
                        try:
                            client.images.get(img)
                            _image_exists_cache[img] = True
                        except docker.errors.ImageNotFound:
                            return jsonify({
                                "status": "error",
                                "error_type": "docker_pull_required",
                                "docker_image": img,
                                "message": f"请先在镜像管理中下载 R 基础镜像: {img}"
                            })
                else:
                    log(f"[SYSTEM] Image {img} not found locally, attempting to pull...")
                    if _try_build_plugin_image(client, cfg, img):
                        log(f"[SYSTEM] Using auto-built local image: {img}")
                    else:
                        try:
                            pull_image_with_progress(client, img)
                            _image_exists_cache[img] = True
                            log(f"[SYSTEM] Pulled image {img} successfully.")
                        except Exception as e:
                            msg = f"Failed to pull docker image {img} : {e}"
                            log(f"[ERROR] {msg}")
                            return jsonify({
                                "status": "error",
                                "error_type": "docker_pull_failed",
                                "docker_image": img,
                                "message": (
                                    "无法拉取 Docker 镜像: %s" % img
                                )
                            })
            except Exception as e:
                log(f"[WARN] Unexpected error when checking image {img}: {e}")
                if img == R_CUSTOM_IMAGE:
                    img = R_DOCKER_IMAGE
                    cfg['docker_image'] = img
                elif _try_build_plugin_image(client, cfg, img):
                    log(f"[SYSTEM] Using auto-built local image after fallback check: {img}")
                else:
                    try:
                        pull_image_with_progress(client, img)
                        _image_exists_cache[img] = True
                        log(f"[SYSTEM] Pulled image {img} after fallback attempt.")
                    except Exception as e2:
                        msg = f"Failed to pull docker image {img} after fallback: {e2}"
                        log(f"[ERROR] {msg}")
                        return jsonify({"status": "error", "error_type": "docker_pull_failed", "docker_image": img, "message": "无法拉取 Docker 镜像: %s" % img})
        
        host_out_dir = os.path.normpath(os.path.abspath(data['outDir']))
        if not os.path.exists(host_out_dir): os.makedirs(host_out_dir, exist_ok=True)
        
        internal_out_dir = "/workspace"
        execution_mode = cfg.get('execution_mode', 'loop')
        
# --- 前端传来 _visibleFileParams 列表，明确标识当前可见的 file 参数 ---
        visible_file_set = set(data.get('_visibleFileParams') or [])
        log(f"[DEBUG] _visibleFileParams from frontend: {visible_file_set}")
        log(f"[DEBUG] data['params'] keys: {list(data.get('params', {}).keys())}")

        file_params = {} 
        for p in cfg['parameters']:
            if p['type'] == 'file':
                pid = p['id']
                raw = data['params'].get(pid)
                val_list = raw if isinstance(raw, list) else ([raw] if raw else [])
                val_list = [v for v in val_list if v] 
                if not val_list and p.get('required'):
                    # 方案A（推荐）：前端明确告知可见参数列表
                    if visible_file_set and pid not in visible_file_set:
                        log(f"[DEBUG] Skip hidden file param '{pid}' (not in _visibleFileParams)")
                        continue
                    # 方案B（兜底）：后端求值 showWhen 条件
                    if p.get('showWhen') and not _evaluate_show_when(p['showWhen'], data['params']):
                        log(f"[DEBUG] Skip hidden file param '{pid}' (showWhen evaluated to hidden)")
                        continue
                    return jsonify({"status":"error", "message":f"缺少必要文件: {_display_name(p.get('label'))}"})  
                file_params[pid] = val_list
        
        # === Pre-flight: verify all input files exist on host ===
        def _check_files_exist(items):
            """Recursively check file existence, handling nested lists (paired-end)."""
            for item in items:
                if isinstance(item, list):
                    err = _check_files_exist(item)
                    if err: return err
                elif isinstance(item, str) and item:
                    if not os.path.exists(item):
                        return f"输入文件不存在: {os.path.basename(item)}\n路径: {item}\n请检查文件是否已被移动或删除。"
            return None

        for pid, val_list in file_params.items():
            err = _check_files_exist(val_list)
            if err:
                return jsonify({"status": "error", "message": err})

        tasks = [] 
        failed_tasks = []  # Track which tasks failed
        
        if execution_mode == 'merge':
            task_params = data['params'].copy()
            for pid, files in file_params.items():
                task_params[pid] = files 
            
            if file_params:
                first_key = list(file_params.keys())[0]
                if file_params[first_key]:
                    base = extract_sample_name(file_params[first_key][0])
                    sample_name = f"{base}_combined_analysis"
                else:
                    sample_name = "multi_sample_analysis"
            else:
                sample_name = "analysis_task"

            tasks.append({'params': task_params, 'sample': sample_name})
            log(f"[BATCH] Merge mode active. All files -> 1 Task.")
        else:
            passthrough_params = set(cfg.get('loop_passthrough_file_params') or [])
            loop_file_params = {
                pid: files for pid, files in file_params.items()
                if pid not in passthrough_params
            }
            if cfg.get('loop_strict_pairing'):
                multi_counts = {len(files) for files in loop_file_params.values() if len(files) > 1}
                if len(multi_counts) > 1:
                    details = ', '.join(f"{pid}={len(files)}" for pid, files in loop_file_params.items() if files)
                    return jsonify({
                        "status": "error",
                        "message": f"循环文件数量不匹配（{details}）。多文件参数必须数量一致；单个文件可在全部循环中共用。"
                    })
            max_cycles = 1
            for files in loop_file_params.values():
                if len(files) > max_cycles: max_cycles = len(files)
            log(f"[BATCH] Loop mode active. {max_cycles} Tasks generated.")
            
            for i in range(max_cycles):
                snapshot = data['params'].copy()
                current_sample = f"result_{i+1}"
                found_variable_param = False 
                
                for pid, files in file_params.items():
                    if pid in passthrough_params:
                        snapshot[pid] = files
                        continue
                    if not files: snapshot[pid] = None; continue
                    if len(files) > 1:
                        target_item = files[i % len(files)] 
                        current_sample = extract_sample_name(target_item)
                        found_variable_param = True
                    else:
                        target_item = files[0]
                        if not found_variable_param: current_sample = extract_sample_name(target_item)
                    snapshot[pid] = target_item 
                tasks.append({'params': snapshot, 'sample': current_sample})

        shell_bin = str(cfg.get('shell', 'bash')).strip().lower()
        if shell_bin not in ('bash', 'sh'):
            shell_bin = 'bash'
        force_shell_entrypoint = bool(cfg.get('force_shell_entrypoint'))

        def _run_container_with_image_repair(run_kwargs, phase='run'):
            """Run a container, and if image is unexpectedly missing, pull via fallback strategy then retry once."""
            img_ref = str(run_kwargs.get('image') or cfg.get('docker_image') or '').strip()
            try:
                return client.containers.run(**run_kwargs)
            except docker.errors.ImageNotFound:
                if not img_ref:
                    raise
                log(f"[WARN] Image missing during {phase}: {img_ref}; pulling with fallback and retrying...")
                _image_exists_cache.pop(img_ref, None)
                if not _try_build_plugin_image(client, cfg, img_ref):
                    pull_image_with_progress(client, img_ref)
                _image_exists_cache[img_ref] = True
                return client.containers.run(**run_kwargs)
            except docker.errors.APIError as e:
                if img_ref and 'No such image' in str(e):
                    log(f"[WARN] Docker reported missing image during {phase}: {img_ref}; pulling with fallback and retrying...")
                    _image_exists_cache.pop(img_ref, None)
                    if not _try_build_plugin_image(client, cfg, img_ref):
                        pull_image_with_progress(client, img_ref)
                    _image_exists_cache[img_ref] = True
                    return client.containers.run(**run_kwargs)
                raise

        def _create_container_with_plugin_image_repair(create_kwargs, phase='run'):
            img_ref = str(create_kwargs.get('image') or cfg.get('docker_image') or '').strip()
            return _create_container_with_image_repair(
                client,
                create_kwargs,
                img_ref,
                phase=phase,
                auto_build_fn=lambda: _try_build_plugin_image(client, cfg, img_ref)
            )

        tool_version_info = fallback_tool_version(cfg)
        try:
            version_cmd = build_version_command(cfg) if (cfg.get('binary_name') or cfg.get('version_command')) else ""

            if version_cmd:
                ver_volumes = { cfg['plugin_dir']: {'bind': '/scripts', 'mode': 'ro'} }
                if cfg.get('local_binary'): 
                    ver_volumes[os.path.join(cfg['plugin_dir'], 'bin')] = {'bind': '/tool_root', 'mode': 'rw'}
                
                _ver_result = [None]
                _ver_exc = [None]
                def _ver_run():
                    try:
                        _ver_result[0] = _run_container_with_image_repair({
                            'image': cfg['docker_image'],
                            **({'entrypoint': shell_bin, 'command': ["-c", version_cmd]} if force_shell_entrypoint else {'command': [shell_bin, "-c", version_cmd]}),
                            'volumes': ver_volumes,
                            'remove': True,
                            'user': '0',
                            'platform': "linux/amd64",
                            'log_config': {'type': 'json-file'}
                        }, phase='version-check')
                    except Exception as _ve:
                        _ver_exc[0] = _ve

                _ver_thread = threading.Thread(target=_ver_run, daemon=True)
                _ver_thread.start()
                _ver_thread.join(timeout=30)
                if _ver_thread.is_alive():
                    log(f"[WARN] Version detection timed out after 30s, skipping")
                    tool_version_info = fallback_tool_version(cfg)
                elif _ver_exc[0]:
                    raise _ver_exc[0]
                elif _ver_result[0]:
                    parsed_tool_version = parse_tool_version_output(_ver_result[0].decode('utf-8', errors='ignore'), cfg)
                    if parsed_tool_version:
                        tool_version_info = parsed_tool_version
                    log(f"[SYSTEM] Detected Tool Version: {tool_version_info}")
        except Exception as e:
            log(f"[WARN] Failed to detect version: {e}")
            tool_version_info = fallback_tool_version(cfg)

        for idx, task in enumerate(tasks):
            t_start = time.time()
            start_dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            log(f"[SYSTEM] === Running Task {idx+1}/{len(tasks)}: {task['sample']} ===")
            
            volumes = { 
                cfg['plugin_dir']: {'bind': '/scripts', 'mode': 'ro'}, 
                host_out_dir: {'bind': internal_out_dir, 'mode': 'rw'} 
            }
            if cfg.get('local_binary'): 
                volumes[os.path.join(cfg['plugin_dir'], 'bin')] = {'bind': '/tool_root', 'mode': 'ro'}
            # Mount scripts directory if it exists
            scripts_dir = os.path.join(cfg['plugin_dir'], 'scripts')
            if os.path.isdir(scripts_dir):
                volumes[scripts_dir] = {'bind': '/tool_root/scripts', 'mode': 'ro'}
            # Mount plugin cache directory if configured
            if cfg.get('cache_dir'):
                cache_host = os.path.join(cfg['plugin_dir'], cfg['cache_dir'])
                os.makedirs(cache_host, exist_ok=True)
                volumes[cache_host] = {'bind': '/cache', 'mode': 'rw'}
            
            mount_counter = 0
            path_mapping = {}  
            dir_mapping = {}   
            
            def get_mapped_path(host_p):
                """Map a host file path to a container path, creating volume mounts as needed.
                CRITICAL: checks existing volumes to avoid overwriting output dir mount."""
                nonlocal mount_counter
                host_p = os.path.abspath(host_p)
                if host_p in path_mapping: return path_mapping[host_p]
                
                parent = os.path.dirname(host_p)
                norm_parent = os.path.normcase(os.path.normpath(parent))
                
                if parent in dir_mapping:
                    mount_point = dir_mapping[parent]
                else:
                    # Reuse existing mounts for output and plugin directories.
                    existing_mount = None
                    for vol_host, vol_spec in list(volumes.items()):
                        if os.path.normcase(os.path.normpath(vol_host)) == norm_parent:
                            existing_mount = vol_spec['bind']
                            break
                    
                    if existing_mount:
                        mount_point = existing_mount
                        log(f"[DEBUG] Reusing existing volume mount: {parent} → {mount_point}")
                    else:
                        mount_point = f"/data/mnt_{mount_counter}"
                        volumes[parent] = {'bind': mount_point, 'mode': 'ro'}
                        mount_counter += 1
                    dir_mapping[parent] = mount_point
                
                safe_name = os.path.basename(host_p) 
                c_path = f"{mount_point}/{safe_name}"
                path_mapping[host_p] = c_path
                return c_path

            cmd = cfg['command_template']
            bin_dir = cfg.get('binary_subdir', '')
            if cfg.get('local_binary') and cfg.get('binary_name'):
                full_bin, rel_bin = find_binary_path(cfg['plugin_dir'], cfg['binary_name'])
                if full_bin:
                    bin_dir = rel_bin
                    cfg['binary_subdir'] = rel_bin
                    log(f"[SYSTEM] Resolved local binary: {full_bin} (subdir='{rel_bin}')")
                else:
                    log(f"[WARN] Local binary '{cfg.get('binary_name')}' not found under plugin bin directory")
            # Clean path: avoid double-slash when binary_dir is empty (e.g. /tool_root//bwa → /tool_root/bwa)
            # Quote sample_name in case it has special chars (already sanitized, but extra safety)
            safe_sample = task['sample'].replace("'", "'\\''")
            cmd = cmd.replace("{output_dir}", internal_out_dir).replace("{binary_dir}", bin_dir).replace("{sample_name}", safe_sample)
            if not bin_dir:
                cmd = cmd.replace("/tool_root//", "/tool_root/")
            
            for p in cfg['parameters']:
                if p['type'] == 'textarea':
                    pid = p['id']; val = task['params'].get(pid)
                    if val and isinstance(val, str) and len(val) > 0:
                        tmp_name = f'.primigenius_input_{idx + 1}_{pid}_{uuid.uuid4().hex}.txt'
                        tmp_path = os.path.join(host_out_dir, tmp_name)
                        try:
                            with open(tmp_path, 'w', encoding='utf-8', newline='') as tf:
                                tf.write(val)
                            _linux_textarea_temp_files.append(tmp_path)
                            task['params'][pid] = f'{internal_out_dir}/{tmp_name}'
                            log(f'[SYSTEM] Wrote textarea param "{pid}" to temp file ({len(val)} chars)')
                        except Exception as e:
                            log(f'[WARN] Could not write temp param file for {pid}: {e}')
            
            for p in cfg['parameters']:
                pid = p['id']; val = task['params'].get(pid)
                
                if p['type'] in ('file', 'directory') and val:
                    def map_recursive(item):
                        if isinstance(item, list): return [map_recursive(x) for x in item]
                        return get_mapped_path(item)
                    
                    mapped_val = map_recursive(val)
                    
                    def _shell_quote(s):
                        """Quote a path for shell if it contains spaces or special chars."""
                        s = str(s)
                        if ' ' in s or '(' in s or ')' in s or '&' in s:
                            return "'" + s.replace("'", "'\\''") + "'"
                        return s
                    
                    def flatten_to_string(item):
                        if isinstance(item, list): return " ".join([flatten_to_string(x) for x in item])
                        return _shell_quote(str(item))
                    
                    default_replacement = flatten_to_string(mapped_val)
                    cmd = cmd.replace(f"{{{pid}}}", default_replacement)
                    
                    if isinstance(mapped_val, list) and len(mapped_val) > 0:
                        cmd = cmd.replace(f"{{{pid}[0]}}", _shell_quote(str(mapped_val[0])))
                        if len(mapped_val) > 1: cmd = cmd.replace(f"{{{pid}[1]}}", _shell_quote(str(mapped_val[1])))
                        
                elif (
                    p.get('map_existing_path')
                    and isinstance(val, str)
                    and os.path.isabs(os.path.expanduser(val.strip().strip('"\'')))
                    and os.path.isfile(os.path.expanduser(val.strip().strip('"\'')))
                ):
                    # Opt-in support for text fields that accept either a name
                    # or a complete host file path. Existing paths are mounted
                    # exactly like file parameters; ordinary text is untouched.
                    host_text_path = os.path.expanduser(val.strip().strip('"\''))
                    mapped_text_path = get_mapped_path(host_text_path)
                    cmd = cmd.replace(f"{{{pid}}}", mapped_text_path)
                    log(f'[SYSTEM] Mapped text file path "{pid}": {host_text_path} → {mapped_text_path}')
                elif val is not None: 
                    if p['type'] == 'checkbox':
                        is_checked = bool(val) and str(val).lower() not in ('false', '0', 'none', 'off', '')
                        if is_checked:
                            replacement = str(p.get('true_value') if p.get('true_value') is not None else p.get('value', val))
                        else:
                            replacement = str(p.get('false_value', ''))
                        cmd = cmd.replace(f"{{{pid}}}", replacement)
                    elif p['type'] == 'tag_select':
                        if isinstance(val, list):
                            replacement = ','.join(str(item).strip() for item in val if str(item).strip())
                        else:
                            replacement = str(val)
                        cmd = cmd.replace(f"{{{pid}}}", replacement)
                    else:
                        cmd = cmd.replace(f"{{{pid}}}", str(val))
                else:
                    # Smart skip: non-required empty params → replace placeholder with empty string
                    # Required empty params also get empty string (validation already happened)
                    cmd = cmd.replace(f"{{{pid}}}", "")
            
            # Post-process: clean up whitespace artifacts from empty substitutions
            cmd = clean_command(cmd)
            
            outfile_name = f"{task['sample']}.{cfg.get('output_extension', 'txt')}"
            internal_outfile = f"{internal_out_dir}/{outfile_name}"
            host_outfile = os.path.join(host_out_dir, outfile_name)

            cmd = cmd.replace("{output_file}", internal_outfile)

            # Determine thread count: client top-level → plugin param → config default → host CPU
            threads = data.get('threads')
            if threads is None or threads == '':
                threads = data.get('params', {}).get('threads')
            if threads is None or threads == '':
                # Fallback: use the config's default value for threads
                for _pcfg in cfg.get('parameters', []):
                    if _pcfg.get('id') == 'threads' and 'default' in _pcfg:
                        threads = _pcfg['default']
                        break
            threads_env_value = None
            if isinstance(threads, str) and threads.strip().lower() in ('auto', 'automatic'):
                threads = 'AUTO'
                threads_env_value = str(os.cpu_count() or 1)
            else:
                try:
                    threads = int(threads) if (threads is not None and threads != '') else (os.cpu_count() or 1)
                except (ValueError, TypeError):
                    threads = os.cpu_count() or 1
                threads_env_value = str(threads)
            log(f"[DEBUG] Thread resolution: payload.threads={data.get('threads')}, params.threads={data.get('params',{}).get('threads')}, final={threads}")

            # Replace placeholder in command template if plugin uses {threads}
            cmd = cmd.replace("{threads}", str(threads))

            # Save the clean tool command BEFORE injecting pre-flight checks
            user_cmd = cmd  # this is what we show in the log

            log(f"[CMD] {user_cmd}")
            log(f"[SYSTEM] Using threads={threads} for this container run")
            # Debug: log all Docker volume mounts
            for _vh, _vs in volumes.items():
                log(f"[DEBUG] Volume: {_vh} -> {_vs['bind']} ({_vs['mode']})")

            # === Early warning: detect network/UNC paths that Docker cannot mount ===
            network_path_warnings = []
            for _host_f in path_mapping:
                # Detect UNC paths (\\server\share) or subst/mapped network drives
                if _host_f.startswith('\\\\') or _host_f.startswith('//'):
                    network_path_warnings.append(f"  - {_host_f} (UNC/网络路径)")

            # === Inject pre-flight file accessibility check inside container ===
            # If Podman hasn't shared the drive, the mount dir will be empty
            preflight_file_count = 0
            if path_mapping:
                checks = []
                for _host_f, _cont_f in path_mapping.items():
                    _drive = os.path.splitdrive(_host_f)[0] or '?'
                    preflight_file_count += 1
                    # Compact check: only print error details if file is missing
                    checks.append(
                        f"if [ ! -e '{_cont_f}' ]; then "
                        f"echo '[PrimiGenius] ERROR: 容器内无法访问文件: {os.path.basename(_host_f)}' >&2; "
                        f"echo '  容器路径: {_cont_f}' >&2; "
                        f"echo '  宿主路径: {_host_f}' >&2; "
                        f"echo '  挂载目录内容:' >&2; ls -la $(dirname '{_cont_f}')/ 2>&1 >&2 | head -3; "
                        f"echo '  提示: 请确认 Podman Machine 可访问 {_drive} 盘' >&2; "
                        f"echo '  提示: 网络盘/USB盘/移动硬盘无法被容器直接挂载，请复制到本地磁盘' >&2; "
                        f"exit 1; fi"
                    )
                cmd = " && ".join(checks) + " && " + cmd

            # Log file naming: plugin_id.sample_name.primigenius.log
            safe_plugin_id = (cfg.get('id') or 'unknown').replace(' ', '_')
            log_path = os.path.join(host_out_dir, f"{safe_plugin_id}.{task['sample']}.primigenius.log")
            
            c = None
            try:
                with open(log_path, 'w', encoding='utf-8', buffering=1) as lf:
                    lf.write("============================================================\n")
                    lf.write(f" PrimiGenius Execution Log\n")
                    lf.write("============================================================\n")
                    lf.write(f"Tool Name    : {_get_plugin_name(cfg)}\n")
                    lf.write(f"Tool Version : {tool_version_info}\n")
                    lf.write(f"Task Name    : {task['sample']}\n")
                    lf.write(f"Start Time   : {start_dt}\n")
                    lf.write(f"Command      : {user_cmd}\n")
                    param_lines = _build_parameter_log_lines(cfg, task.get('params', {}))
                    if param_lines:
                        lf.write("Parameters   :\n")
                        for _pline in param_lines:
                            lf.write(f"{_pline}\n")
                    if preflight_file_count:
                        lf.write(f"Pre-flight   : {preflight_file_count} 个输入文件已验证可访问性\n")
                    if network_path_warnings:
                        lf.write(f"⚠ 网络路径警告:\n")
                        for _w in network_path_warnings:
                            lf.write(f"{_w}\n")
                        lf.write(f"Podman Machine 无法挂载网络映射盘(UNC路径)，建议复制到本地磁盘。\n")
                    lf.write("------------------------------------------------------------\n\n")
                    
                    # Inject popular thread-related environment variables so many libraries respect the setting
                    envs = {
                        'OMP_NUM_THREADS': threads_env_value,
                        'OPENBLAS_NUM_THREADS': threads_env_value,
                        'MKL_NUM_THREADS': threads_env_value,
                        'NUMEXPR_NUM_THREADS': threads_env_value,
                        'VECLIB_MAXIMUM_THREADS': threads_env_value,
                        'THREADS': threads_env_value
                    }
                    _linux_plugin_name = _get_plugin_name(cfg)
                    _safe_linux_name = re.sub(r'[^a-zA-Z0-9_.-]', '', _linux_plugin_name.replace(' ', '-'))[:80] if _linux_plugin_name else None
                    _linux_create_kwargs = {
                        'image': cfg['docker_image'],
                        **({'entrypoint': shell_bin, 'command': ["-c", cmd]} if force_shell_entrypoint else {'command': [shell_bin, "-c", cmd]}),
                        'volumes': volumes,
                        'environment': envs,
                        'labels': {
                            'primigenius.plugin_id': str(cfg.get('id') or ''),
                            'primigenius.run_id': str(data.get('clientRunId') or '')
                        },
                        'user': '0',
                        'platform': "linux/amd64",
                        'log_config': {'type': 'json-file'}
                    }
                    if _safe_linux_name:
                        _linux_create_kwargs['name'] = f"primigenius-{_safe_linux_name}-{int(time.time())}"
                    c = _create_container_with_plugin_image_repair(_linux_create_kwargs, phase='task-run')
                    # record mapping from client-provided run id (if any) to actual container id
                    try:
                        client_run_id = data.get('clientRunId') if isinstance(data, dict) else None
                        if not client_run_id:
                            client_run_id = 'run-' + uuid.uuid4().hex
                        RUNNING_CONTAINERS[client_run_id] = c.id
                        log(f"[SYSTEM] Registered running container: runId={client_run_id} -> container={c.id}")
                        c._client_run_id = client_run_id
                    except Exception:
                        pass
                    _push_docker_event({'Type': 'container', 'Action': 'create', 'id': c.id, 'from': cfg.get('docker_image', ''), 'run_id': data.get('clientRunId', '')})
                    
                    _linux_seen = {}
                    _linux_stop = threading.Event()
                    _linux_log_thread_run_id = getattr(_LOG_CONTEXT, 'run_id', None)
                    _linux_log_thread_channel = getattr(_LOG_CONTEXT, 'channel', 'Linux')
                    _linux_stream_ref = [None]

                    def _on_linux_line(line):
                        log(line.strip())
                        lf.write(line + '\n')

                    def _linux_log_thread_main():
                        _LOG_CONTEXT.run_id = _linux_log_thread_run_id
                        _LOG_CONTEXT.channel = _linux_log_thread_channel
                        _stream_container_logs_thread(c, _linux_stop, _linux_seen, _on_linux_line, _linux_stream_ref)

                    _linux_log_thread = threading.Thread(target=_linux_log_thread_main, daemon=True)
                    _linux_log_thread.start()
                    c.start()
                    _push_docker_event({'Type': 'container', 'Action': 'start', 'id': c.id, 'from': cfg.get('docker_image', '')})

                    res = _robust_container_wait(c, timeout=None)
                    if res.get('StatusCode', -1) == -1:
                        try:
                            inspect_proc = _run_hidden_wsl(
                                ['wsl', '-d', _active_wsl_distro_name(), '-u', 'root', '--',
                                 'podman', 'inspect', c.id, '--format', '{{.State.Status}}'],
                                timeout=10, capture=True
                            )
                            actual_status = (inspect_proc.stdout or '').strip() if inspect_proc else ''
                            if actual_status == 'running':
                                log(f'[WARN] Linux container wait returned -1 but container is still running, continuing to wait indefinitely...')
                                extra_res = _robust_container_wait(c, timeout=None)
                                res = extra_res
                            else:
                                log(f'[WARN] Linux container wait returned -1, container status={actual_status}')
                                try:
                                    c.kill()
                                except Exception:
                                    pass
                        except Exception:
                            log(f'[WARN] Linux container wait returned -1, killing container')
                            try:
                                c.kill()
                            except Exception:
                                pass

                    _linux_stop.set()
                    if _linux_stream_ref[0]:
                        try:
                            _linux_stream_ref[0].close()
                        except Exception:
                            pass
                    _linux_log_thread.join(timeout=5)

                    _collect_final_logs(c, _linux_seen, _on_linux_line)
                    t_end = time.time()
                    end_dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    duration_sec = t_end - t_start
                    duration_str = format_duration(duration_sec)
                    
                    lf.write("\n------------------------------------------------------------\n")
                    lf.write(f"End Time     : {end_dt}\n")
                    lf.write(f"Total Time   : {duration_str}\n")
                    
                    if res['StatusCode'] != 0:
                        lf.write("Status       : FAILED ❌\n")
                        lf.write("============================================================\n")
                        log(f"[ERROR] Task {task['sample']} failed. ❌")
                        failed_tasks.append(task['sample'])
                        if os.path.exists(host_outfile):
                            try:
                                log(f"[SYSTEM] Cleaning up failed output: {host_outfile}")
                                os.remove(host_outfile)
                            except: pass
                    else:
                        lf.write("Status       : SUCCESS ✅\n")
                        lf.write("============================================================\n")
                        log(f"[SYSTEM] Task {task['sample']} Completed successfully in {duration_str}. ✅")

                        # --- Post-processing safeguard: strip Docker mount paths from text output files ---
                        try:
                            import re as _re
                            TEXT_EXTS = {'.txt', '.csv', '.tsv', '.tab', '.count', '.log', '.summary', '.counts', '.matrix', '.results', '.table', '.out', '.xls'}
                            SKIP_EXTS = {'.bam', '.bai', '.gz', '.zip', '.tar', '.rar', '.7z', '.ht2', '.fa', '.fasta', '.fq', '.fastq', '.sam', '.bed', '.gtf', '.gff', '.png', '.jpg', '.pdf', '.svg'}
                            # Build regex from all mounted paths (e.g. /data/mnt_0/, /data/mnt_1/, /workspace/)
                            mount_prefixes = set()
                            mount_prefixes.add(r'/data/mnt_\d+/')
                            mount_prefixes.add(r'/workspace/')
                            for hp, bind_info in volumes.items():
                                bp = bind_info['bind'] if isinstance(bind_info, dict) else bind_info
                                mount_prefixes.add(_re.escape(bp) + r'/')
                            path_strip_re = _re.compile('|'.join(mount_prefixes))
                            # Also strip any remaining directory prefixes from column headers (basename extraction)
                            remaining_path_re = _re.compile(r'(?<=["\t,;]|^)[^\t\n,;"]*/')

                            for fname in os.listdir(host_out_dir):
                                fpath = os.path.join(host_out_dir, fname)
                                if not os.path.isfile(fpath):
                                    continue
                                fext = os.path.splitext(fname)[1].lower()
                                if fext in SKIP_EXTS:
                                    continue
                                if fext not in TEXT_EXTS and not fname.endswith('.primigenius.log'):
                                    continue
                                if fname.endswith('.primigenius.log'):
                                    continue  # don't touch log files
                                try:
                                    with open(fpath, 'r', encoding='utf-8', errors='replace') as rf:
                                        original = rf.read()
                                    cleaned = path_strip_re.sub('', original)
                                    if cleaned != original:
                                        with open(fpath, 'w', encoding='utf-8') as wf:
                                            wf.write(cleaned)
                                        log(f"[SYSTEM] Stripped Docker mount paths from: {fname}")
                                except Exception as strip_err:
                                    log(f"[WARN] Path stripping failed for {fname}: {strip_err}")
                        except Exception as post_err:
                            log(f"[WARN] Post-processing path cleanup failed: {post_err}")
                        
            except Exception as e:
                err_msg = f"\n[CRITICAL ERROR] {traceback.format_exc()}"
                log(err_msg)
                try: 
                    with open(log_path, 'a', encoding='utf-8') as lf_err: 
                        lf_err.write(err_msg)
                except: pass
                if os.path.exists(host_outfile):
                    try: os.remove(host_outfile)
                    except: pass
            
            finally:
                if c:
                    try:
                        client_run_id = getattr(c, '_client_run_id', None)
                        if client_run_id and client_run_id in RUNNING_CONTAINERS:
                            RUNNING_CONTAINERS.pop(client_run_id, None)
                    except: pass
                    try:
                        if _safe_linux_name:
                            c.rename(f"primigenius-{_safe_linux_name}-{c.id[:12]}")
                        else:
                            c.rename(f"primigenius-linux-{c.id[:12]}")
                    except Exception:
                        pass
                    _schedule_container_cleanup(c.id)

        if failed_tasks:
            return jsonify({"status": "error", "message": f"{len(failed_tasks)}/{len(tasks)} 个任务失败: {', '.join(failed_tasks[:5])}\n请查看输出目录中的 .primigenius.log 日志文件了解详情。"})
        return jsonify({"status":"success"})
    except Exception as e:
        log(f"[ERROR] {str(e)}"); return jsonify({"status":"error", "message":str(e)})
    finally:
        for _textarea_tmp in _linux_textarea_temp_files:
            try:
                if os.path.isfile(_textarea_tmp):
                    os.remove(_textarea_tmp)
                    log(f'[SYSTEM] Removed temporary pasted-input file: {os.path.basename(_textarea_tmp)}')
            except Exception as _textarea_cleanup_error:
                log(f'[WARN] Could not remove temporary pasted-input file {os.path.basename(_textarea_tmp)}: {_textarea_cleanup_error}')
        if _prev_run_id is None:
            try:
                delattr(_LOG_CONTEXT, 'run_id')
            except Exception:
                pass
        else:
            _LOG_CONTEXT.run_id = _prev_run_id
        if _prev_channel is None:
            try:
                delattr(_LOG_CONTEXT, 'channel')
            except Exception:
                pass
        else:
            _LOG_CONTEXT.channel = _prev_channel


@app.route('/stop-container', methods=['POST'])
def stop_container():
    data = request.json or {}
    run_id = data.get('runId')
    container_id = data.get('containerId')
    client = get_docker_client()
    if not client: return jsonify({"status":"error", "message":"Docker 未启动"})
    try:
        # resolve container id from run id mapping if provided
        if run_id:
            cid = RUNNING_CONTAINERS.get(run_id)
            if not cid:
                return jsonify({"status":"error", "message":"Unknown runId or already finished"})
            container = client.containers.get(cid)
        elif container_id:
            container = client.containers.get(container_id)
        else:
            return jsonify({"status":"error", "message":"runId or containerId required"})

        try:
            container.kill()
            # attempt to remove mapping entries that reference this container
            for k, v in list(RUNNING_CONTAINERS.items()):
                if v == container.id:
                    RUNNING_CONTAINERS.pop(k, None)
            log(f"[SYSTEM] Killed container {container.id} via stop API")
            return jsonify({"status":"success"})
        except Exception as e:
            return jsonify({"status":"error", "message": str(e)})
    except docker.errors.NotFound:
        return jsonify({"status":"error", "message":"Container not found"})
    except Exception as e:
        return jsonify({"status":"error", "message": str(e)})

@app.route('/podman/containers/cleanup', methods=['POST'])
def cleanup_exited_containers():
    client = get_docker_client()
    if not client:
        return jsonify({'status': 'error', 'message': 'Docker not running'})
    try:
        containers = client.containers.list(all=True)
        removed = []
        for c in containers:
            try:
                name = c.name or ''
                status = c.status
                if status in ('exited', 'dead', 'created'):
                    c.remove(force=True)
                    removed.append(name or c.id[:12])
            except Exception:
                pass
        for k, v in list(RUNNING_CONTAINERS.items()):
            try:
                client.containers.get(v)
            except Exception:
                RUNNING_CONTAINERS.pop(k, None)
        global _containers_cache
        _containers_cache = {'data': _containers_cache.get('data'), 'time': 0}
        log(f'[SYSTEM] Cleaned up {len(removed)} exited containers')
        return jsonify({'status': 'success', 'removed': removed})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/podman/containers/<container_id>/remove', methods=['POST'])
def remove_single_container(container_id):
    client = get_docker_client()
    if not client:
        return jsonify({'status': 'error', 'message': 'Docker not running'})
    try:
        c = client.containers.get(container_id)
        if c.status == 'running':
            try:
                c.kill()
            except Exception:
                pass
        c.remove()
        log(f'[SYSTEM] User removed container {container_id[:12]}')
        return jsonify({'status': 'success'})
    except docker.errors.NotFound:
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/check-podman', methods=['GET'])
def check():
    mgr = _get_podman_manager()
    if mgr.is_engine_ready():
        return jsonify({"status": "success"})
    if get_docker_client():
        return jsonify({"status": "success"})
    return jsonify({"status": "error"})


def _fetch_containers_data():
    global _containers_cache
    now = time.time()
    api = _get_readonly_api_client()
    if api:
        try:
            raw_containers = api.containers(all=True, quiet=False)
            result = _parse_containers(raw_containers)
            resp_data = {'status': 'success', 'containers': result}
            _containers_cache = {'data': resp_data, 'time': now}
            _close_readonly_api_client(api)
            return True
        except Exception:
            _close_readonly_api_client(api)
    if _pull_in_progress.is_set():
        return False
    client = get_docker_client()
    if client:
        try:
            raw_containers = client.api.containers(all=True, quiet=False)
            result = _parse_containers(raw_containers)
            resp_data = {'status': 'success', 'containers': result}
            _containers_cache = {'data': resp_data, 'time': now}
            return True
        except Exception:
            pass
    return False

def _refresh_containers_cache():
    if not _containers_refresh_lock.acquire(blocking=False):
        return
    try:
        for _ in range(2):
            if _fetch_containers_data():
                return
            time.sleep(0.3)
    finally:
        _containers_refresh_lock.release()

def _refresh_containers_cache_sync():
    _fetch_containers_data()

@app.route('/podman/containers', methods=['GET'])
def list_containers():
    global _containers_cache
    now = time.time()
    fresh = request.args.get('fresh', '0') == '1'
    if fresh:
        if _fetch_containers_data() and _containers_cache['data']:
            return jsonify(_containers_cache['data'])
        return jsonify(_containers_cache['data'] or {'status': 'success', 'containers': []})
    if _containers_cache['data'] and now - _containers_cache['time'] < _CACHE_TTL:
        return jsonify(_containers_cache['data'])
    if _containers_cache['data']:
        threading.Thread(target=_refresh_containers_cache, daemon=True).start()
        return jsonify(_containers_cache['data'])
    threading.Thread(target=_refresh_containers_cache, daemon=True).start()
    return jsonify({'status': 'success', 'containers': []})

def _auto_remove_containers_by_ids(short_ids):
    client = get_docker_client()
    if not client:
        return
    for sid in short_ids:
        for attempt in range(3):
            try:
                for c in client.containers.list(all=True):
                    if c.id.startswith(sid):
                        c.remove(force=True)
                        _push_docker_event({'Type': 'container', 'Action': 'destroy', 'id': c.id})
                        break
                break
            except Exception:
                time.sleep(1)

def _auto_remove_single_container(container_id):
    time.sleep(2)
    client = get_docker_client()
    if not client:
        return
    for attempt in range(3):
        try:
            c = client.containers.get(container_id)
            if c.status in ('exited', 'dead', 'created'):
                c.remove(force=True)
                _push_docker_event({'Type': 'container', 'Action': 'destroy', 'id': container_id})
            return
        except docker.errors.NotFound:
            return
        except Exception:
            time.sleep(2)

def _parse_containers(raw_containers):
    result = []
    _auto_removed = []
    for c_data in raw_containers:
        try:
            cid = c_data.get('Id', '')
            names = c_data.get('Names', [])
            name = names[0].lstrip('/') if names else cid[:12]
            image_name = c_data.get('Image', '')[:60]
            for prefix in ('docker.io/library/', 'docker.io/', 'index.docker.io/library/', 'index.docker.io/', 'registry-1.docker.io/library/', 'registry-1.docker.io/'):
                if image_name.startswith(prefix):
                    image_name = image_name[len(prefix):]
                    break
            status = c_data.get('State', 'unknown')
            created = c_data.get('Created', '')
            is_stopped = status in ('exited', 'dead', 'created')
            if is_stopped:
                _auto_removed.append(cid[:12])
                continue
            run_id = None
            for k, v in RUNNING_CONTAINERS.items():
                if v == cid:
                    run_id = k
                    break
            result.append({
                'id': cid[:12],
                'full_id': cid,
                'name': name,
                'image': image_name,
                'status': status,
                'created': created,
                'run_id': run_id,
            })
        except Exception as e:
            result.append({
                'id': '?',
                'full_id': '',
                'name': 'error',
                'image': '',
                'status': 'error',
                'created': '',
                'run_id': None,
                'error': str(e)
            })
    if _auto_removed:
        threading.Thread(target=_auto_remove_containers_by_ids, args=(_auto_removed,), daemon=True).start()
    return result


@app.route('/podman/containers/<container_id>/detail', methods=['GET'])
def container_detail(container_id):
    client = get_docker_client()
    if not client:
        return jsonify({'status': 'error', 'message': 'Docker not running'})
    try:
        container = client.containers.get(container_id)
        return jsonify(_container_detail_payload(container))
    except docker.errors.NotFound:
        return jsonify({'status': 'error', 'message': 'Container not found'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/podman/containers/<container_id>/logs', methods=['GET'])
def container_logs_stream(container_id):
    client = get_docker_client()
    if not client:
        return jsonify({'status': 'error', 'message': 'Docker not running'})
    try:
        tail = int(request.args.get('tail', '200'))
    except Exception:
        tail = 200
    tail = max(20, min(1000, tail))

    try:
        container = client.containers.get(container_id)
    except docker.errors.NotFound:
        return jsonify({'status': 'error', 'message': 'Container not found'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

    def generate():
        stop_event = threading.Event()
        queue_items = queue.Queue()
        seen_lines = {}
        stream_ref = [None]

        def _emit_initial_logs():
            try:
                raw = container.logs(stdout=True, stderr=True, tail=tail)
                raw_text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
                for raw_line in raw_text.splitlines():
                    line = raw_line.rstrip('\r')
                    if not line:
                        continue
                    seen_lines[line] = int(seen_lines.get(line, 0)) + 1
                    queue_items.put({'type': 'log', 'line': line})
            except Exception as e:
                queue_items.put({'type': 'status', 'line': f'log preload failed: {e}'})

        def _worker():
            try:
                _emit_initial_logs()
                container.reload()
                if str(getattr(container, 'status', '') or '').lower() == 'running':
                    _stream_container_logs_thread(
                        container,
                        stop_event,
                        seen_lines,
                        lambda line: queue_items.put({'type': 'log', 'line': line}),
                        stream_ref
                    )
                _collect_final_logs(container, seen_lines, lambda line: queue_items.put({'type': 'log', 'line': line}))
                queue_items.put({'type': 'summary', 'summary': _container_detail_payload(container)['container']['runtime']})
            except Exception as e:
                queue_items.put({'type': 'error', 'message': str(e)})
            finally:
                queue_items.put({'type': 'done'})

        threading.Thread(target=_worker, daemon=True).start()

        try:
            yield f"data: {json.dumps({'type': 'hello', 'container_id': container_id})}\n\n"
            while True:
                try:
                    item = queue_items.get(timeout=15)
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                    continue

                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item.get('type') == 'done':
                    break
        finally:
            stop_event.set()
            if stream_ref[0]:
                try:
                    stream_ref[0].close()
                except Exception:
                    pass

    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/podman/container-stats/<container_id>', methods=['GET'])
def container_stats(container_id):
    client = get_docker_client()
    if not client:
        return jsonify({'status': 'error', 'message': 'Docker not running'})
    try:
        container = client.containers.get(container_id)
        stats = container.stats(stream=False)
        cpu_delta = stats.get('cpu_stats', {}).get('cpu_usage', {}).get('total_usage', 0) - \
                    stats.get('precpu_stats', {}).get('cpu_usage', {}).get('total_usage', 0)
        system_delta = stats.get('cpu_stats', {}).get('system_cpu_usage', 0) - \
                       stats.get('precpu_stats', {}).get('system_cpu_usage', 0)
        cpu_percent = 0.0
        num_cpus = stats.get('cpu_stats', {}).get('online_cpus', 1) or 1
        if system_delta > 0 and cpu_delta > 0:
            cpu_percent = round((cpu_delta / system_delta) * num_cpus * 100.0, 1)
        mem_usage = stats.get('memory_stats', {}).get('usage', 0)
        mem_limit = stats.get('memory_stats', {}).get('limit', 0)
        mem_percent = round((mem_usage / mem_limit) * 100.0, 1) if mem_limit > 0 else 0.0
        mem_usage_mb = round(mem_usage / (1024 * 1024), 1) if mem_usage else 0
        mem_limit_mb = round(mem_limit / (1024 * 1024), 1) if mem_limit else 0
        net_rx = 0
        net_tx = 0
        networks = stats.get('networks', {})
        if isinstance(networks, dict):
            for iface, vals in networks.items():
                net_rx += vals.get('rx_bytes', 0)
                net_tx += vals.get('tx_bytes', 0)
        blk_read = 0
        blk_write = 0
        blkio = stats.get('blkio_stats', {}).get('io_service_bytes_recursive', [])
        if isinstance(blkio, list):
            for entry in blkio:
                if entry.get('op') == 'read':
                    blk_read += entry.get('value', 0)
                elif entry.get('op') == 'write':
                    blk_write += entry.get('value', 0)
        pids = stats.get('pids_stats', {}).get('current', 0)
        payload = {
            'cpu_percent': cpu_percent,
            'mem_usage_mb': mem_usage_mb,
            'mem_limit_mb': mem_limit_mb,
            'mem_percent': mem_percent,
            'net_rx_bytes': net_rx,
            'net_tx_bytes': net_tx,
            'blk_read_bytes': blk_read,
            'blk_write_bytes': blk_write,
            'pids': pids,
            'num_cpus': num_cpus,
        }
        with _CONTAINER_LAST_STATS_LOCK:
            _CONTAINER_LAST_STATS[container.id] = {'stats': payload, 'ts': time.time()}
        return jsonify({
            'status': 'success',
            'stats': payload
        })
    except docker.errors.NotFound:
        return jsonify({'status': 'error', 'message': 'Container not found'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/podman/container-stats-stream/<container_id>', methods=['GET'])
def container_stats_stream(container_id):
    client = get_docker_client()
    if not client:
        return jsonify({'status': 'error', 'message': 'Docker not running'})
    try:
        container = client.containers.get(container_id)
    except Exception:
        return jsonify({'status': 'error', 'message': 'Container not found'})

    def generate():
        try:
            for raw_stats in container.stats(stream=True, decode=True):
                if not isinstance(raw_stats, dict):
                    continue
                cpu_delta = raw_stats.get('cpu_stats', {}).get('cpu_usage', {}).get('total_usage', 0) - \
                            raw_stats.get('precpu_stats', {}).get('cpu_usage', {}).get('total_usage', 0)
                system_delta = raw_stats.get('cpu_stats', {}).get('system_cpu_usage', 0) - \
                               raw_stats.get('precpu_stats', {}).get('system_cpu_usage', 0)
                cpu_percent = 0.0
                num_cpus = raw_stats.get('cpu_stats', {}).get('online_cpus', 1) or 1
                if system_delta > 0 and cpu_delta > 0:
                    cpu_percent = round((cpu_delta / system_delta) * num_cpus * 100.0, 1)
                mem_usage = raw_stats.get('memory_stats', {}).get('usage', 0)
                mem_limit = raw_stats.get('memory_stats', {}).get('limit', 0)
                mem_percent = round((mem_usage / mem_limit) * 100.0, 1) if mem_limit > 0 else 0.0
                mem_usage_mb = round(mem_usage / (1024 * 1024), 1) if mem_usage else 0
                mem_limit_mb = round(mem_limit / (1024 * 1024), 1) if mem_limit else 0
                net_rx = 0
                net_tx = 0
                networks = raw_stats.get('networks', {})
                if isinstance(networks, dict):
                    for iface, vals in networks.items():
                        net_rx += vals.get('rx_bytes', 0)
                        net_tx += vals.get('tx_bytes', 0)
                blk_read = 0
                blk_write = 0
                blkio = raw_stats.get('blkio_stats', {}).get('io_service_bytes_recursive', [])
                if isinstance(blkio, list):
                    for entry in blkio:
                        if entry.get('op') == 'read':
                            blk_read += entry.get('value', 0)
                        elif entry.get('op') == 'write':
                            blk_write += entry.get('value', 0)
                pids = raw_stats.get('pids_stats', {}).get('current', 0)
                payload = {
                    'cpu_percent': cpu_percent,
                    'mem_usage_mb': mem_usage_mb,
                    'mem_limit_mb': mem_limit_mb,
                    'mem_percent': mem_percent,
                    'net_rx_bytes': net_rx,
                    'net_tx_bytes': net_tx,
                    'blk_read_bytes': blk_read,
                    'blk_write_bytes': blk_write,
                    'pids': pids,
                    'num_cpus': num_cpus,
                }
                with _CONTAINER_LAST_STATS_LOCK:
                    _CONTAINER_LAST_STATS[container.id] = {'stats': payload, 'ts': time.time()}
                yield f"data: {json.dumps({'type': 'stats', 'stats': payload})}\n\n"
        except GeneratorExit:
            pass
        except Exception:
            pass

    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


_DOCKER_EVENTS_CLIENTS = []
_DOCKER_EVENTS_LOCK = threading.Lock()
_DOCKER_EVENTS_COND = threading.Condition(_DOCKER_EVENTS_LOCK)
_DOCKER_EVENTS_THREAD_STARTED = False

def _push_docker_event(event_dict):
    global _containers_cache, _images_cache
    if event_dict.get('Type') == 'container':
        if _containers_cache['data']:
            _containers_cache = {'data': _containers_cache['data'], 'time': 0}
    elif event_dict.get('Type') == 'image':
        if _images_cache['data']:
            _images_cache = {'data': _images_cache['data'], 'time': 0}
    with _DOCKER_EVENTS_LOCK:
        dead = []
        for i, q in enumerate(_DOCKER_EVENTS_CLIENTS):
            try:
                q.put_nowait(json.dumps(event_dict, ensure_ascii=False))
            except Exception:
                dead.append(i)
        for i in reversed(dead):
            _DOCKER_EVENTS_CLIENTS.pop(i)

def _docker_events_broadcaster():
    global _DOCKER_EVENTS_THREAD_STARTED
    retry_delay = 3.0
    while True:
        with _DOCKER_EVENTS_COND:
            while not _DOCKER_EVENTS_CLIENTS:
                _DOCKER_EVENTS_COND.wait()
        try:
            client = None
            client = get_docker_client()
            if not client:
                with _DOCKER_EVENTS_COND:
                    _DOCKER_EVENTS_COND.wait(timeout=retry_delay)
                retry_delay = min(60.0, retry_delay * 2)
                continue
            retry_delay = 3.0
            for event in client.events(decode=True):
                if not isinstance(event, dict):
                    continue
                evt_action = event.get('Action', '')
                evt_type = event.get('Type', '')
                evt_id = event.get('id', '')
                if evt_type == 'container' and evt_action in ('die', 'stop') and evt_id:
                    try:
                        c = client.containers.get(evt_id)
                        c_status = c.status
                        if c_status in ('exited', 'dead', 'created'):
                            threading.Thread(target=lambda cid=evt_id: _auto_remove_single_container(cid), daemon=True).start()
                    except Exception:
                        pass
                msg = json.dumps(event, ensure_ascii=False)
                with _DOCKER_EVENTS_LOCK:
                    dead = []
                    for i, q in enumerate(_DOCKER_EVENTS_CLIENTS):
                        try:
                            q.put_nowait(msg)
                        except Exception:
                            dead.append(i)
                    for i in reversed(dead):
                        _DOCKER_EVENTS_CLIENTS.pop(i)
        except Exception:
            _invalidate_docker_client(client if 'client' in locals() else None)
            _record_docker_reconnect_failure()
            with _DOCKER_EVENTS_COND:
                _DOCKER_EVENTS_COND.wait(timeout=retry_delay)
            retry_delay = min(60.0, retry_delay * 2)


def _ensure_docker_events_thread():
    global _DOCKER_EVENTS_THREAD_STARTED
    if _DOCKER_EVENTS_THREAD_STARTED:
        return
    _DOCKER_EVENTS_THREAD_STARTED = True
    t = threading.Thread(target=_docker_events_broadcaster, daemon=True)
    t.start()


@app.route('/podman/events', methods=['GET'])
def podman_events_stream():
    _ensure_docker_events_thread()
    q = queue.Queue(maxsize=200)
    with _DOCKER_EVENTS_COND:
        _DOCKER_EVENTS_CLIENTS.append(q)
        _DOCKER_EVENTS_COND.notify_all()

    def generate():
        try:
            yield f"data: {json.dumps({'type': 'hello'})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        except GeneratorExit:
            pass
        finally:
            with _DOCKER_EVENTS_LOCK:
                try:
                    _DOCKER_EVENTS_CLIENTS.remove(q)
                except ValueError:
                    pass

    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/podman/diagnostics', methods=['GET'])
def podman_diagnostics():
    """Return diagnostics about Podman client, version, and basic network connectivity."""
    client = get_docker_client()
    out = { 'podman_available': False }
    if not client:
        out['error'] = 'Podman client unavailable or not running'
        return jsonify(out)

    out['podman_available'] = True
    try:
        out['version'] = client.version()
    except Exception as e:
        out['version_error'] = str(e)

    try:
        out['info'] = client.info()
    except Exception as e:
        out['info_error'] = str(e)

    registry = 'registry-1.docker.io'
    try:
        addrs = socket.getaddrinfo(registry, 443)
        out['registry_dns'] = [a[4][0] for a in addrs]
    except Exception as e:
        out['registry_dns_error'] = str(e)

    # TCP connect test to registry (IPv4 & IPv6 attempts)
    def try_connect(host, port, timeout=5):
        s = None
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close(); return True, None
        except Exception as e:
            return False, str(e)

    tcp_results = {}
    # test each resolved address if available, otherwise test by name
    test_addrs = out.get('registry_dns') or [registry]
    for a in test_addrs:
        ok, err = try_connect(a, 443)
        tcp_results[str(a)] = {'ok': ok, 'error': err}
    out['tcp_connect'] = tcp_results

    # also test basic DNS for google as external check
    try:
        out['google_dns'] = socket.getaddrinfo('google.com', 443)[0][4][0]
    except Exception as e:
        out['google_dns_error'] = str(e)

    return jsonify(out)


@app.route('/list-dir', methods=['GET'])
def list_dir():
    path = request.args.get('path')
    recursive = request.args.get('recursive', '0') == '1'
    try:
        if not path: return jsonify({"status":"error", "message":"path required"})
        abs_path = os.path.abspath(path)
        if not (abs_path.startswith(USER_DOCS) or abs_path.startswith(PLUGINS_DIR) or abs_path.startswith(APP_INSTALL_DIR) or os.getenv('ALLOW_LIST_ALL') == '1'):
            return jsonify({"status":"error", "message":"Access denied"})
        if not os.path.exists(abs_path): return jsonify({"status":"error", "message":"Path not found"})
        entries = []
        if recursive:
            def walk(d):
                try:
                    for f in os.listdir(d):
                        full = os.path.join(d, f)
                        if os.path.isfile(full): entries.append(full)
                        elif os.path.isdir(full): walk(full)
                except: pass
            walk(abs_path)
        else:
            for f in os.listdir(abs_path):
                full = os.path.join(abs_path, f)
                if os.path.isfile(full): entries.append(full)
        return jsonify({"status":"success", "files": entries})
    except Exception as e:
        return jsonify({"status":"error", "message": str(e)})


@app.route('/get-app-paths', methods=['GET'])
def get_app_paths():
    """Return key application directories so the frontend can use them."""
    return jsonify({
        'status': 'success',
        'appInstallDir': APP_INSTALL_DIR.replace('\\', '/'),
        'outputsDir': USER_DOCS.replace('\\', '/'),
    })


@app.route('/remove-dir', methods=['POST'])
def remove_dir():
    data = request.json or {}
    target = data.get('path')
    try:
        if not target: return jsonify({"status":"error", "message":"path required"})
        abs_path = os.path.abspath(target)
        # Allow paths under APP_INSTALL_DIR (covers USER_DOCS / outputs), or legacy BioFramework dir
        bio_fw_dir = os.path.join(os.path.expanduser('~'), 'Documents', 'BioFramework')
        allowed = (
            abs_path.startswith(os.path.abspath(APP_INSTALL_DIR)) or
            abs_path.startswith(USER_DOCS) or
            abs_path.startswith(bio_fw_dir) or
            os.getenv('ALLOW_LIST_ALL') == '1'
        )
        if not allowed:
            return jsonify({"status":"error", "message":"Access denied"})
        if not os.path.exists(abs_path): return jsonify({"status":"error", "message":"Path not found"})
        # remove directory tree
        shutil.rmtree(abs_path)
        return jsonify({"status":"success"})
    except Exception as e:
        return jsonify({"status":"error", "message": str(e)})


# ---- R related endpoints ----
@app.route('/r/scan-deps', methods=['POST'])
def r_scan_deps():
    """Scan R script for library/require/requireNamespace calls to auto-detect dependencies."""
    import re
    data = request.json or {}
    plugin_id = data.get('pluginId')
    if not plugin_id:
        return jsonify({'status': 'error', 'message': 'pluginId required'})
    cfg_path = find_config_by_id(plugin_id)
    if not cfg_path:
        return jsonify({'status': 'error', 'message': 'plugin not found'})
    plugin_dir = os.path.dirname(cfg_path)
    cfg = _load_json_file(cfg_path)
    script_rel = cfg.get('script', '')
    if not script_rel:
        return jsonify({'status': 'success', 'packages': cfg.get('packages', [])})
    script_path = os.path.join(plugin_dir, script_rel)
    if not os.path.isfile(script_path):
        return jsonify({'status': 'success', 'packages': cfg.get('packages', [])})
    try:
        with open(script_path, 'r', encoding='utf-8', errors='replace') as f:
            code = f.read()
    except:
        return jsonify({'status': 'success', 'packages': cfg.get('packages', [])})
    # Match library(pkg), library("pkg"), require('pkg'), requireNamespace("pkg"), etc.
    # Strategy: match quoted strings AND bare names, then filter out obvious R variable names
    pat_quoted = r'(?:library|require|requireNamespace|loadNamespace)\s*\(\s*["\']([A-Za-z][A-Za-z0-9._]*)["\']'
    pat_bare = r'(?:library|require)\s*\(\s*([A-Za-z][A-Za-z0-9._]{2,})\s*[\),]'
    # Filter out dynamic library(varname, character.only=TRUE) calls
    pat_char_only = r'(?:library|require)\s*\(\s*([A-Za-z][A-Za-z0-9._]{2,})\s*,\s*character\.only'
    char_only_vars = set(re.findall(pat_char_only, code))
    found = (set(re.findall(pat_quoted, code)) | set(re.findall(pat_bare, code))) - char_only_vars
    # R base packages that don't need installation
    base_pkgs = {'base','compiler','datasets','grDevices','graphics','grid','methods','parallel',
                 'splines','stats','stats4','tcltk','tools','utils'}
    found -= base_pkgs
    # Merge with config packages
    config_pkgs = set(cfg.get('packages', []))
    all_pkgs = sorted(config_pkgs | found)
    # Update config if new packages found
    if found - config_pkgs:
        cfg['packages'] = all_pkgs
        try:
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            log(f'Auto-detected new packages for {plugin_id}: {found - config_pkgs}')
        except:
            pass
    return jsonify({'status': 'success', 'packages': all_pkgs, 'detected': sorted(found), 'config_original': sorted(config_pkgs)})


@app.route('/r/check-runtime', methods=['GET'])
def r_check_runtime():
    """Check if Podman is available and R image is ready."""
    client = get_docker_client()
    if not client:
        return jsonify({'status': 'error', 'message': 'Podman is not running'})
    found_img = None
    try:
        custom = client.images.get(R_CUSTOM_IMAGE)
        if not _custom_r_image_matches_contract(custom):
            raise RuntimeError('custom image contract mismatch')
        try:
            if not _image_exists_locally(client, R_DOCKER_IMAGE):
                raise docker.errors.ImageNotFound(R_DOCKER_IMAGE)
            found_img = R_CUSTOM_IMAGE
            _image_exists_cache[R_CUSTOM_IMAGE] = True
            _image_exists_cache[R_DOCKER_IMAGE] = True
        except Exception:
            pass
    except Exception:
        pass
    if not found_img:
        try:
            if not _image_exists_locally(client, R_DOCKER_IMAGE):
                raise docker.errors.ImageNotFound(R_DOCKER_IMAGE)
            found_img = R_DOCKER_IMAGE
            _image_exists_cache[R_DOCKER_IMAGE] = True
        except Exception:
            pass
    if not found_img:
        for stale_key in [R_CUSTOM_IMAGE, R_DOCKER_IMAGE]:
            _image_exists_cache.pop(stale_key, None)
        return jsonify({
            'status': 'image_missing',
            'message': f'R image {R_DOCKER_IMAGE} is not installed',
            'docker_image': R_DOCKER_IMAGE
        })
    repos, bioc_mirror = _resolve_r_repo_bundle(R_RUNTIME.get('repos'))
    return jsonify({
        'status': 'success',
        'rscript': f'Podman: {found_img}',
        'version': R_VERSION,
        'bioconductor_version': BIOCONDUCTOR_VERSION,
        'repo_mode': R_RUNTIME.get('repos', 'auto'),
        'repos': repos,
        'bioc_mirror': bioc_mirror,
        'docker_image': R_DOCKER_IMAGE,
        'custom_image': R_CUSTOM_IMAGE,
        'library_generation': R_LIBRARY_GENERATION,
    })


@app.route('/r/set-repo', methods=['POST'])
def r_set_repo():
    data = request.json or {}
    url = data.get('repo')
    if not url:
        return jsonify({'status': 'error', 'message': 'repo required'})
    repos, bioc_mirror = _resolve_r_repo_bundle(url)
    repo_mode = 'auto' if str(url).strip().lower() == 'auto' else repos
    R_RUNTIME['repos'] = repo_mode
    _save_r_runtime_state(repo_mode=repo_mode)
    return jsonify({'status': 'success', 'repo_mode': repo_mode, 'repos': repos, 'bioc_mirror': bioc_mirror})


# Cache for installed R packages list (avoid spinning up containers repeatedly)
_r_packages_cache = {'packages': None, 'time': 0}
_R_PKG_CACHE_TTL = 30  # seconds
_r_libs_revision = 0
R_PREFLIGHT_CACHE_FILE = os.path.join(APP_INSTALL_DIR, f'.primigenius_r_preflight_cache_{R_LIBRARY_GENERATION}.json')
_R_PREFLIGHT_CACHE_MAX = 256
_r_preflight_cache_lock = threading.Lock()

_CONTAINER_CLEANUP_DELAY = 5

def _schedule_container_cleanup(container_id):
    def _cleanup():
        time.sleep(_CONTAINER_CLEANUP_DELAY)
        client = get_docker_client()
        if not client:
            return
        for attempt in range(5):
            try:
                c = client.containers.get(container_id)
                c_status = c.status
                if c_status == 'running':
                    try:
                        c.kill()
                        time.sleep(2)
                    except Exception:
                        pass
                c.remove(force=True)
                log(f'[SYSTEM] Auto-cleaned container {container_id[:12]}')
                _push_docker_event({'Type': 'container', 'Action': 'destroy', 'id': container_id})
                return
            except docker.errors.NotFound:
                log(f'[SYSTEM] Container {container_id[:12]} already removed')
                return
            except Exception as e:
                if attempt < 4:
                    log(f'[WARN] Container cleanup attempt {attempt+1} failed for {container_id[:12]}: {e}, retrying...')
                    time.sleep(3)
                else:
                    log(f'[WARN] Container cleanup failed after 5 attempts for {container_id[:12]}: {e}')
    t = threading.Thread(target=_cleanup, daemon=True)
    t.start()
    log(f'[SYSTEM] Container {container_id[:12]} auto-cleanup scheduled')


def _cleanup_stale_containers():
    client = get_docker_client()
    if not client:
        return
    removed = []
    try:
        for c in client.containers.list(all=True):
            name = (c.name or '')
            status = c.status
            if status in ('exited', 'dead', 'created'):
                try:
                    c.remove(force=True)
                    removed.append(name or c.id[:12])
                except Exception:
                    pass
    except Exception:
        pass
    if removed:
        global _containers_cache
        _containers_cache = {'data': _containers_cache.get('data'), 'time': 0}
        log(f'[SYSTEM] Startup cleanup: removed {len(removed)} stale containers: {", ".join(removed)}')


def _cleanup_orphaned_r_containers(client=None):
    if client is None:
        client = get_docker_client()
    if not client:
        return
    removed = []
    try:
        for c in client.containers.list(all=True):
            name = (c.name or '')
            status = c.status
            if status in ('exited', 'dead', 'created'):
                try:
                    c.remove(force=True)
                    removed.append(name or c.id[:12])
                except Exception:
                    pass
    except Exception:
        pass
    if removed:
        global _containers_cache
        _containers_cache = {'data': _containers_cache.get('data'), 'time': 0}
        log(f'[SYSTEM] Orphaned container cleanup: removed {len(removed)} containers: {", ".join(removed)}')


def _normalize_r_package_names(pkgs):
    safe_pkgs = []
    for p in (pkgs or []):
        sp = str(p or '').strip()
        if re.match(r'^[A-Za-z][A-Za-z0-9._]*$', sp):
            safe_pkgs.append(sp)
    return sorted(set(safe_pkgs))


def _load_cached_r_preflight():
    try:
        if not os.path.exists(R_PREFLIGHT_CACHE_FILE):
            return {}
        with open(R_PREFLIGHT_CACHE_FILE, 'r', encoding='utf-8') as f:
            payload = json.load(f) or {}
        raw_cache = payload.get('cache')
        if not isinstance(raw_cache, dict):
            return {}
        cache = {}
        for key, value in raw_cache.items():
            ts = None
            if isinstance(value, (int, float)):
                ts = float(value)
            elif isinstance(value, dict):
                ts_raw = value.get('ts')
                if isinstance(ts_raw, (int, float)):
                    ts = float(ts_raw)
            if ts and ts > 0:
                cache[str(key)] = ts
        return cache
    except Exception:
        return {}


_r_preflight_cache = _load_cached_r_preflight()  # key: "pkg1|pkg2|..." -> ts


def _persist_cached_r_preflight_locked():
    if len(_r_preflight_cache) > _R_PREFLIGHT_CACHE_MAX:
        stale = sorted(_r_preflight_cache.items(), key=lambda kv: kv[1])[:len(_r_preflight_cache) - _R_PREFLIGHT_CACHE_MAX]
        for key, _ in stale:
            _r_preflight_cache.pop(key, None)
    payload = {
        'version': 2,
        'cache': {k: {'ts': v} for k, v in _r_preflight_cache.items()},
        'updated_at': int(time.time())
    }
    with open(R_PREFLIGHT_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _has_cached_r_preflight(cache_key):
    with _r_preflight_cache_lock:
        return cache_key in _r_preflight_cache


def _mark_cached_r_preflight(cache_key):
    with _r_preflight_cache_lock:
        _r_preflight_cache[cache_key] = time.time()
        _persist_cached_r_preflight_locked()


def _drop_cached_r_preflight(cache_key):
    with _r_preflight_cache_lock:
        if cache_key not in _r_preflight_cache:
            return
        _r_preflight_cache.pop(cache_key, None)
        _persist_cached_r_preflight_locked()


def _clear_cached_r_preflight(reason=''):
    with _r_preflight_cache_lock:
        if not _r_preflight_cache:
            return
        _r_preflight_cache.clear()
        _persist_cached_r_preflight_locked()
    if reason:
        log(f'[SYSTEM] Cleared persistent R pre-flight cache ({reason})')


def _host_has_r_package(pkg_name):
    if pkg_name in _r_libs_package_set:
        return True
    return pkg_name.lower() in _r_libs_package_set_lower


def _host_r_package_dir(pkg_name):
    actual = _r_libs_package_set_lower.get(str(pkg_name or '').lower(), pkg_name)
    if not actual:
        return None
    pkg_dir = os.path.join(R_LIBS_DIR, actual)
    desc_path = os.path.join(pkg_dir, 'DESCRIPTION')
    if os.path.isdir(pkg_dir) and os.path.isfile(desc_path):
        return pkg_dir
    return None


def _read_r_description_fields(pkg_name):
    pkg_dir = _host_r_package_dir(pkg_name)
    if not pkg_dir:
        return {}
    desc_path = os.path.join(pkg_dir, 'DESCRIPTION')
    fields = {}
    current = None
    try:
        with open(desc_path, 'r', encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.rstrip('\r\n')
                if not line:
                    continue
                if line[:1].isspace() and current:
                    fields[current] = fields.get(current, '') + ' ' + line.strip()
                    continue
                if ':' not in line:
                    continue
                key, val = line.split(':', 1)
                current = key.strip()
                fields[current] = val.strip()
    except Exception:
        return {}
    return fields




def _parse_r_dependency_names(value):
    deps = []
    for part in str(value or '').split(','):
        token = re.sub(r'\([^)]*\)', '', part).strip()
        if not token:
            continue
        token = token.split()[0].strip()
        if re.match(r'^[A-Za-z][A-Za-z0-9._]*$', token) and not _is_r_image_package(token):
            deps.append(token)
    return deps


def _host_r_dependency_status(root_packages):
    """Fast host-side dependency-chain check using installed DESCRIPTION files.

    This intentionally avoids starting R/Podman. It is used as the cheap guard
    before trusting the persistent pre-flight cache, so manually deleted
    dependency directories are detected on the next plugin run.
    """
    roots = _normalize_r_package_names(root_packages)
    seen = set()
    missing = set()
    chain = set()

    def visit(pkg):
        key = str(pkg or '').lower()
        if not key or key in seen:
            return
        seen.add(key)
        if _is_r_image_package(pkg):
            chain.add(_R_BASE_PACKAGES_LOWER[key])
            return
        actual = _r_libs_package_set_lower.get(key, pkg)
        if not _host_r_package_dir(actual):
            missing.add(pkg)
            return
        chain.add(actual)
        fields = _read_r_description_fields(actual)
        for dep_field in ('Depends', 'Imports', 'LinkingTo'):
            for dep in _parse_r_dependency_names(fields.get(dep_field, '')):
                visit(dep)

    for root in roots:
        visit(root)

    return {
        'ready': len(missing) == 0,
        'missing': sorted(missing),
        'chain': sorted(chain),
    }


def _bump_r_libs_revision(reason=''):
    global _r_libs_revision
    _r_libs_revision += 1
    _r_packages_cache['packages'] = None
    _scan_r_libs_package_set()
    if reason:
        log(f'[SYSTEM] R libs revision bumped to {_r_libs_revision} ({reason})')


@app.route('/r/list-packages', methods=['GET'])
def r_list_packages():
    """List installed R packages by reading DESCRIPTION files directly from host r_libs directory.
    This avoids spinning up Docker containers, making the check nearly instant."""
    now = time.time()
    # Return cached result if still fresh
    if _r_packages_cache['packages'] is not None and now - _r_packages_cache['time'] < _R_PKG_CACHE_TTL:
        return jsonify({'status': 'success', 'packages': _r_packages_cache['packages']})
    try:
        pkgs = []
        if os.path.isdir(R_LIBS_DIR):
            for entry in os.listdir(R_LIBS_DIR):
                if entry.startswith('00LOCK'):
                    continue
                desc_path = os.path.join(R_LIBS_DIR, entry, 'DESCRIPTION')
                if not os.path.isfile(desc_path):
                    continue
                try:
                    with open(desc_path, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read()
                    pkg_name = None
                    pkg_version = None
                    for line in content.split('\n'):
                        if line.startswith('Package:'):
                            pkg_name = line.split(':', 1)[1].strip()
                        elif line.startswith('Version:'):
                            pkg_version = line.split(':', 1)[1].strip()
                        if pkg_name and pkg_version:
                            break
                    if pkg_name:
                        pkgs.append({'package': pkg_name, 'version': pkg_version or '?', 'builtin': False})
                except Exception:
                    pass
        persistent_names = {item['package'].lower() for item in pkgs}
        for pkg_name in sorted(_R_IMAGE_LIBRARY_PACKAGES, key=str.lower):
            if pkg_name.lower() not in persistent_names:
                pkgs.append({'package': pkg_name, 'version': 'image', 'builtin': True})
        _r_packages_cache['packages'] = pkgs
        _r_packages_cache['time'] = now
        return jsonify({'status': 'success', 'packages': pkgs})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/r/install-packages', methods=['POST'])
def r_install_packages():
    data = request.json or {}
    pkgs = data.get('packages') or []
    repo = data.get('repo') or R_RUNTIME.get('repos')
    skip_repair = data.get('skip_repair', False)
    pkgs = _normalize_r_package_names(pkgs)
    if not pkgs:
        return jsonify({'status': 'error', 'message': 'packages required'})
    image_pkgs = [pkg for pkg in pkgs if _is_r_image_package(pkg)]
    install_pkgs = [pkg for pkg in pkgs if not _is_r_image_package(pkg)]
    job_id = 'job-' + uuid.uuid4().hex
    sse_run_id = 'rinstall-' + uuid.uuid4().hex
    initial_log = deque()
    if image_pkgs:
        initial_log.append('[SYSTEM] Already provided by the pinned R base image: ' + ', '.join(image_pkgs))
    INSTALL_JOBS[job_id] = {'status': 'pending', 'log': initial_log, 'exit_code': None, 'packages': pkgs, 'sse_run_id': sse_run_id}
    if install_pkgs:
        thr = threading.Thread(target=_run_r_install_job, args=(job_id, install_pkgs, repo, None, skip_repair), daemon=True)
        thr.start()
    else:
        INSTALL_JOBS[job_id]['status'] = 'done'
        INSTALL_JOBS[job_id]['exit_code'] = 0
    return jsonify({'status': 'success', 'job_id': job_id, 'run_id': sse_run_id})


@app.route('/r/install-status', methods=['GET'])
def r_install_status():
    job_id = request.args.get('job_id')
    if not job_id or job_id not in INSTALL_JOBS:
        return jsonify({'status': 'error', 'message': 'unknown job_id'})
    job = INSTALL_JOBS[job_id]
    lines = list(job['log'])
    return jsonify({'status': 'success', 'job_id': job_id, 'state': job['status'], 'exit_code': job.get('exit_code'), 'log': lines})


@app.route('/r/delete-packages', methods=['POST'])
def r_delete_packages():
    with _R_PACKAGE_RW_LOCK.write():
        return _r_delete_packages_locked()


def _r_delete_packages_locked():
    """Delete installed R packages by removing their directories from R_LIBS_DIR."""
    data = request.json or {}
    pkgs = data.get('packages') or []
    if not pkgs:
        return jsonify({'status': 'error', 'message': 'packages required'})
    deleted = []
    failed = []
    for pkg in pkgs:
        # Sanitize: only allow simple package name characters
        safe_name = ''.join(c for c in pkg if c.isalnum() or c in '._-')
        if not safe_name or safe_name != pkg:
            failed.append({'package': pkg, 'reason': 'invalid name'})
            continue
        pkg_dir = os.path.join(R_LIBS_DIR, safe_name)
        if os.path.isdir(pkg_dir):
            try:
                import shutil
                shutil.rmtree(pkg_dir)
                deleted.append(safe_name)
            except Exception as e:
                failed.append({'package': safe_name, 'reason': str(e)})
        else:
            reason = 'provided by base image' if _is_r_image_package(safe_name) else 'not found'
            failed.append({'package': safe_name, 'reason': reason})
    # Invalidate cache
    if deleted:
        _bump_r_libs_revision('delete-packages')
        _clear_cached_r_preflight('delete-packages')
    else:
        _r_packages_cache['packages'] = None
    return jsonify({'status': 'success', 'deleted': deleted, 'failed': failed})


@app.route('/r/run-script', methods=['POST'])
@_track_analysis_request
def r_run_script():
    """Run R script inside Docker. Accepts multipart form with file uploads."""
    _prev_run_id = getattr(_LOG_CONTEXT, 'run_id', None)
    _prev_channel = getattr(_LOG_CONTEXT, 'channel', None)
    _LOG_CONTEXT.run_id = request.form.get('clientRunId')
    _LOG_CONTEXT.channel = 'R'
    try:
        pluginId = request.form.get('pluginId')
        script = request.form.get('script')
        params_raw = request.form.get('params') or '{}'
        default_r_outputs = os.path.join(USER_DOCS, 'r_outputs')
        out_dir = request.form.get('outDir') or default_r_outputs
        try:
            params = json.loads(params_raw)
        except:
            params = {}

        if not script or not pluginId:
            return jsonify({'status': 'error', 'message': 'pluginId and script required'})

        cfg_path = find_config_by_id(pluginId)
        if not cfg_path:
            return jsonify({'status': 'error', 'message': 'plugin not found'})

        # Create a unique timestamped subfolder per run.  Milliseconds plus a
        # short UUID keep rapid re-runs from colliding within the same second.
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
        run_suffix = uuid.uuid4().hex[:6]
        safe_name = pluginId.replace(' ', '_') if pluginId else 'unknown'
        out_dir = os.path.join(out_dir, f'{safe_name}_{ts}_{run_suffix}')

        plugin_dir = os.path.dirname(cfg_path)
        script_path = os.path.join(plugin_dir, script)
        if not os.path.exists(script_path):
            return jsonify({'status': 'error', 'message': 'script not found'})

        os.makedirs(out_dir, exist_ok=True)

        # Load plugin config once for both branches
        cfg = load_all_plugins().get(pluginId) or {}

        # --- Workflow mode: if no file uploads, map host file paths to container paths ---
        if not request.files:
            # Check params for host file paths and create volume mappings
            file_param_ids = set()
            for p_def in (cfg.get('parameters') or []):
                if p_def.get('type') == 'file':
                    file_param_ids.add(p_def.get('id', ''))

            mount_counter = [0]
            dir_mapping = {}
            extra_mounts = []

            def map_host_path(host_path):
                host_path = os.path.abspath(host_path)
                parent = os.path.dirname(host_path)
                if parent not in dir_mapping:
                    mount_point = f'/data/mnt_{mount_counter[0]}'
                    dir_mapping[parent] = mount_point
                    extra_mounts.append(Mount(target=mount_point, source=parent, type='bind', read_only=True))
                    mount_counter[0] += 1
                return f'{dir_mapping[parent]}/{os.path.basename(host_path)}'

            for pid in list(params.keys()):
                if pid in file_param_ids and params[pid]:
                    host_p = params[pid]
                    if isinstance(host_p, str):
                        pieces = [part.strip() for part in host_p.split(',') if part.strip()]
                        if len(pieces) > 1:
                            mapped = [map_host_path(part) for part in pieces if os.path.isabs(part) and os.path.exists(part)]
                            if mapped:
                                params[pid] = ','.join(mapped)
                        elif os.path.isabs(host_p) and os.path.exists(host_p):
                            params[pid] = map_host_path(host_p)

            plugin_cache_dir = os.path.join(plugin_dir, cfg['cache_dir']) if cfg.get('cache_dir') else None
            res = _run_rscript_docker(
                script_path, params, out_dir, plugin_dir,
                extra_mounts=extra_mounts,
                required_packages=cfg.get('packages'),
                plugin_name=_get_plugin_name(cfg),
                cache_dir=plugin_cache_dir
            )
            res['outDir'] = out_dir
            return jsonify(res)

        # --- Normal mode: file uploads ---
        # Detect which params are multi-file from plugin config
        multi_file_ids = set()
        for p_def in (cfg.get('parameters') or []):
            if p_def.get('type') == 'file' and (p_def.get('multiple') or p_def.get('allow_multiple')):
                multi_file_ids.add(p_def.get('id', ''))

        saved_files = []
        for key in set(request.files.keys()):
            file_list = request.files.getlist(key)
            if key in multi_file_ids and len(file_list) > 1:
                # Multi-file parameter: save all files, pass comma-separated paths
                paths = []
                for f in file_list:
                    safe_filename = os.path.basename(f.filename or '')
                    if not safe_filename:
                        continue
                    dest = os.path.join(out_dir, safe_filename)
                    f.save(dest)
                    saved_files.append(safe_filename)
                    paths.append(f'/workspace/{safe_filename}')
                params[key] = ','.join(paths)
            else:
                # Single file parameter
                f = file_list[0]
                safe_filename = os.path.basename(f.filename or '')
                if not safe_filename:
                    continue
                dest = os.path.join(out_dir, safe_filename)
                f.save(dest)
                saved_files.append(safe_filename)
                params[key] = f'/workspace/{safe_filename}'

        # Legacy fallback: if only 'datafile' is expected and not already set
        if saved_files and 'datafile' not in params:
            params['datafile'] = f'/workspace/{saved_files[0]}'

        plugin_cache_dir = os.path.join(plugin_dir, cfg['cache_dir']) if cfg.get('cache_dir') else None
        res = _run_rscript_docker(
            script_path, params, out_dir, plugin_dir,
            required_packages=cfg.get('packages'),
            plugin_name=_get_plugin_name(cfg),
            cache_dir=plugin_cache_dir
        )

        # Clean up uploaded input files from output directory
        for fname in saved_files:
            fpath = os.path.join(out_dir, fname)
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception:
                pass

        res['outDir'] = out_dir
        return jsonify(res)
    except Exception as e:
        err_msg = traceback.format_exc()
        log(f'[ERROR] R run-script failed before response: {e}')
        log(err_msg)
        return jsonify({
            'status': 'error',
            'message': str(e),
            'error_type': 'r_run_script_exception'
        })
    finally:
        if _prev_run_id is None:
            try:
                delattr(_LOG_CONTEXT, 'run_id')
            except Exception:
                pass
        else:
            _LOG_CONTEXT.run_id = _prev_run_id
        if _prev_channel is None:
            try:
                delattr(_LOG_CONTEXT, 'channel')
            except Exception:
                pass
        else:
            _LOG_CONTEXT.channel = _prev_channel
    


@app.route('/r/list-outputs', methods=['GET'])
def r_list_outputs():
    """List all files and folders in output directory for export. Accepts optional ?outDir= and ?path= params."""
    custom_dir = request.args.get('outDir', '').strip()
    current_path = request.args.get('path', '').strip()
    default_r_outputs = os.path.join(USER_DOCS, 'r_outputs')
    out_root = custom_dir if custom_dir and os.path.isabs(custom_dir) else default_r_outputs
    if not os.path.exists(out_root):
        return jsonify({'status': 'success', 'files': [], 'folders': [], 'dir': out_root, 'current_path': out_root})
    
    # If current_path is provided, use it for navigation
    if current_path:
        # Ensure the current_path is within out_root for security
        safe_current_path = os.path.normpath(current_path)
        if not safe_current_path.startswith(os.path.abspath(out_root)):
            return jsonify({'status': 'error', 'message': 'Access denied'}), 403
        list_dir = safe_current_path
    else:
        list_dir = out_root
    
    files = []
    folders = []
    try:
        for f in os.listdir(list_dir):
            full = os.path.join(list_dir, f)
            if os.path.isfile(full):
                size = os.path.getsize(full)
                ext = os.path.splitext(f)[1].lower()
                is_image = ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.tif', '.svg']
                files.append({'name': f, 'size': size, 'ext': ext, 'is_image': is_image, 'path': full.replace('\\', '/')})
            elif os.path.isdir(full):
                folders.append({'name': f, 'path': full.replace('\\', '/')})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
    files.sort(key=lambda x: x['name'])
    folders.sort(key=lambda x: x['name'])
    
    return jsonify({
        'status': 'success', 
        'files': files, 
        'folders': folders, 
        'dir': out_root, 
        'current_path': list_dir.replace('\\', '/'),
        'parent_path': os.path.dirname(list_dir).replace('\\', '/') if list_dir != out_root else None
    })


@app.route('/r/read-file', methods=['GET'])
def r_read_file():
    """Read text file content for preview."""
    fpath = request.args.get('path', '').strip()
    if not fpath or not os.path.isfile(fpath):
        return jsonify({'status': 'error', 'message': 'File not found'}), 404
    try:
        with open(fpath, 'rb') as f:
            raw = f.read(5 * 1024 * 1024)  # max 5MB
        
        import re
        
        def has_chinese(text):
            """Check if text contains any CJK characters"""
            return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef\u3000-\u303f]', text))
        
        content = None
        has_high = any(b > 127 for b in raw[:1024])
        
        if has_high:
            # Try UTF-8 first
            try:
                decoded_utf8 = raw.decode('utf-8')
                # Only use UTF-8 if it has no replacement chars AND has Chinese chars
                if '\ufffd' not in decoded_utf8 and has_chinese(decoded_utf8):
                    content = decoded_utf8
            except UnicodeDecodeError:
                pass
            
            # If UTF-8 didn't produce Chinese text, try GBK
            if content is None:
                try:
                    decoded_gbk = raw.decode('gbk')
                    if '\ufffd' not in decoded_gbk and has_chinese(decoded_gbk):
                        content = decoded_gbk
                except UnicodeDecodeError:
                    pass
            
            # If GBK failed, try GB2312
            if content is None:
                try:
                    decoded_gb2312 = raw.decode('gb2312')
                    if '\ufffd' not in decoded_gb2312 and has_chinese(decoded_gb2312):
                        content = decoded_gb2312
                except UnicodeDecodeError:
                    pass
        else:
            content = raw.decode('utf-8', errors='replace')
        
        if content is None:
            content = raw.decode('utf-8', errors='replace')
        
        return jsonify({'status': 'success', 'content': content, 'name': os.path.basename(fpath)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/r/serve-file', methods=['GET'])
def r_serve_file():
    """Serve any file from a known output directory (for image preview etc)."""
    fpath = request.args.get('path', '').strip()
    if not fpath or not os.path.isfile(fpath):
        return jsonify({'status': 'error', 'message': 'file not found'}), 404
    
    # Get file extension and set proper MIME type
    ext = os.path.splitext(fpath)[1].lower()
    mime_types = {
        '.pdf': 'application/pdf',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.svg': 'image/svg+xml',
        '.csv': 'text/csv',
        '.tsv': 'text/tab-separated-values',
        '.txt': 'text/plain',
        '.json': 'application/json',
        '.html': 'text/html',
        '.css': 'text/css',
        '.js': 'application/javascript',
        '.xml': 'application/xml',
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.xls': 'application/vnd.ms-excel',
    }
    mimetype = mime_types.get(ext, 'application/octet-stream')
    
    directory = os.path.dirname(fpath)
    filename = os.path.basename(fpath)
    return send_from_directory(directory, filename, mimetype=mimetype)


@app.route('/r_outputs/<path:filename>', methods=['GET'])
def serve_r_output(filename):
    # Check if a custom outDir is provided via query param
    custom_dir = request.args.get('outDir', '').strip()
    default_r_outputs = os.path.join(USER_DOCS, 'r_outputs')
    out_root = custom_dir if custom_dir and os.path.isabs(custom_dir) else default_r_outputs
    if not os.path.exists(out_root):
        return jsonify({'status': 'error', 'message': 'no outputs yet'}), 404
    # protect path traversal
    safe_path = os.path.normpath(os.path.join(out_root, filename))
    if not safe_path.startswith(os.path.abspath(out_root)):
        return jsonify({'status':'error','message':'Access denied'}), 403
    # send file if exists
    if not os.path.exists(safe_path):
        return jsonify({'status':'error','message':'file not found'}), 404
    return send_from_directory(out_root, filename)

# ==========================================
# Podman Image Management API
# ==========================================

_podman_info_cache = {'data': None, 'time': 0}
_podman_wsl_cache = {'data': None, 'time': 0}

def _podman_loading_payload(message='Podman deployment is starting'):
    mgr = _get_podman_manager()
    disk_image_location = ''
    try:
        disk_image_location = mgr.podman_data_dir if os.path.exists(mgr.podman_data_dir) else ''
    except Exception:
        pass
    machine_exists = False
    machine_running = False
    try:
        machine_exists = bool(mgr._is_wsl_distro_registered())
        machine_running = bool(mgr.is_engine_ready())
    except Exception:
        pass
    return {
        'status': 'success',
        'loading': True,
        'setup_running': bool(_podman_async_running or getattr(mgr, '_setup_running', False)),
        'message': message,
        'docker_root_dir': '',
        'disk_image_location': disk_image_location,
        'storage_driver': '',
        'server_version': '',
        'os': '',
        'total_memory': 0,
        'images_count': 0,
        'containers_count': 0,
        'machine_exists': machine_exists,
        'machine_running': machine_running,
        'machine_status': {
            'status': 'success',
            'machines': [{'Name': mgr.machine_name, 'Running': machine_running}] if machine_exists else [],
            'running': machine_running,
            'count': 1 if machine_exists else 0,
            'source': 'wsl-registry' if machine_exists else 'setup'
        }
    }

@app.route('/podman/info', methods=['GET'])
def podman_info():
    global _podman_info_cache
    now = time.time()
    if _podman_info_cache['data'] and now - _podman_info_cache['time'] < 5:
        return jsonify(_podman_info_cache['data'])
    if _podman_info_cache['data']:
        threading.Thread(target=_refresh_podman_info_cache, daemon=True).start()
        return jsonify(_podman_info_cache['data'])
    mgr = _get_podman_manager()
    if not mgr.is_engine_ready():
        if not _podman_permanently_failed:
            _start_podman_async()
        return jsonify(_podman_loading_payload())
    if _fetch_podman_info_data():
        return jsonify(_podman_info_cache['data'])
    threading.Thread(target=_refresh_podman_info_cache, daemon=True).start()
    return jsonify(_podman_loading_payload('Podman information is loading'))


def _podman_info_payload(info):
    disk_image_location = ''
    try:
        mgr = _get_podman_manager()
        disk_image_location = mgr.podman_data_dir
        if not os.path.exists(disk_image_location):
            disk_image_location = ''
    except:
        pass
    return {
        'status': 'success',
        'docker_root_dir': info.get('DockerRootDir', ''),
        'disk_image_location': disk_image_location,
        'storage_driver': info.get('Driver', ''),
        'server_version': info.get('ServerVersion', ''),
        'os': info.get('OperatingSystem', ''),
        'total_memory': info.get('MemTotal', 0),
        'images_count': info.get('Images', 0),
        'containers_count': info.get('Containers', 0)
    }


def _fetch_podman_info_data():
    global _podman_info_cache
    now = time.time()
    api = _get_readonly_api_client()
    if api:
        try:
            info = api.info()
            _podman_info_cache = {'data': _podman_info_payload(info), 'time': now}
            _close_readonly_api_client(api)
            return True
        except Exception as e:
            _close_readonly_api_client(api)
            log(f'[DEBUG] readonly podman_info failed: {e}')
    if _pull_in_progress.is_set():
        return False
    client = get_docker_client()
    if client:
        try:
            info = client.info()
            _podman_info_cache = {'data': _podman_info_payload(info), 'time': now}
            return True
        except Exception as e:
            log(f'[ERROR] podman_info: {e}')
    return False

def _refresh_podman_info_cache():
    if not _podman_info_refresh_lock.acquire(blocking=False):
        return
    try:
        _fetch_podman_info_data()
    finally:
        _podman_info_refresh_lock.release()

def _fetch_images_data():
    global _images_cache
    now = time.time()
    api = _get_readonly_api_client()
    if api:
        try:
            raw_images = api.images(all=False)
            result = _parse_images(raw_images)
            resp_data = {'status': 'success', 'images': result}
            _images_cache = {'data': resp_data, 'time': now}
            _close_readonly_api_client(api)
            return True
        except Exception:
            _close_readonly_api_client(api)
    if _pull_in_progress.is_set():
        return False
    client = get_docker_client()
    if client:
        try:
            raw_images = client.api.images(all=False)
            result = _parse_images(raw_images)
            resp_data = {'status': 'success', 'images': result}
            _images_cache = {'data': resp_data, 'time': now}
            return True
        except Exception:
            pass
    return False

def _refresh_images_cache():
    if not _images_refresh_lock.acquire(blocking=False):
        return
    try:
        for _ in range(2):
            if _fetch_images_data():
                return
            time.sleep(0.3)
    finally:
        _images_refresh_lock.release()

def _refresh_images_cache_sync():
    _fetch_images_data()

@app.route('/podman/images', methods=['GET'])
def podman_list_images():
    global _images_cache
    now = time.time()
    if _images_cache['data'] and now - _images_cache['time'] < _CACHE_TTL:
        return jsonify(_images_cache['data'])
    if _images_cache['data']:
        threading.Thread(target=_refresh_images_cache, daemon=True).start()
        return jsonify(_images_cache['data'])
    mgr = _get_podman_manager()
    if not mgr.is_engine_ready():
        _start_podman_async()
        return jsonify({
            'status': 'success',
            'images': [],
            'loading': True,
            'setup_running': True,
            'message': 'Podman engine is starting'
        })
    if _fetch_images_data():
        return jsonify(_images_cache['data'])
    threading.Thread(target=_refresh_images_cache, daemon=True).start()
    return jsonify({
        'status': 'success',
        'images': [],
        'loading': True,
        'message': 'Podman images are loading'
    })

def _parse_images(raw_images):
    _hidden_prefixes = ('primigenius-r-base',)
    _strip_pfx = ('docker.io/library/', 'docker.io/', 'index.docker.io/library/', 'index.docker.io/', 'registry-1.docker.io/library/', 'registry-1.docker.io/', 'localhost/')
    result = []
    seen_entries = set()
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    for img_data in raw_images:
        tags = img_data.get('RepoTags') or ['<none>:<none>']
        short_id = img_data['Id'].replace('sha256:', '')[:12]
        _hide = False
        for t in tags:
            repo_part = t.rsplit(':', 1)[0] if ':' in t else t
            for pfx in _strip_pfx:
                if repo_part.startswith(pfx):
                    repo_part = repo_part[len(pfx):]
                    break
            for hp in _hidden_prefixes:
                if repo_part == hp or repo_part.startswith(hp + ':') or repo_part.startswith(hp + '/'):
                    _hide = True
                    break
            if _hide:
                break
        if _hide:
            continue
        for tag in tags:
            slash = tag.rfind('/')
            colon = tag.rfind(':')
            parts = [tag[:colon], tag[colon + 1:]] if colon > slash else [tag, 'latest']
            is_system_r_image = _is_pinned_r_runtime_local_ref(tag)
            if is_system_r_image:
                display_repo = R_DOCKER_IMAGE_REPOSITORY
                display_tag = R_DOCKER_IMAGE_TAG
                canonical_ref = R_DOCKER_IMAGE_TAGGED
                local_ref = R_DOCKER_IMAGE_TAGGED
                display_name = R_DOCKER_IMAGE_TAGGED
                entry_key = ('system-r', img_data['Id'])
            else:
                display_repo = parts[0]
                for pfx in ('docker.io/library/', 'docker.io/', 'index.docker.io/library/', 'index.docker.io/', 'registry-1.docker.io/library/', 'registry-1.docker.io/'):
                    if display_repo.startswith(pfx):
                        display_repo = display_repo[len(pfx):]
                        break
                display_tag = parts[1] if len(parts) > 1 else 'latest'
                canonical_ref = f'{display_repo}:{display_tag}' if display_tag != '<none>' else display_repo
                local_ref = tag
                display_name = display_repo
                entry_key = (img_data['Id'], tag)
            if entry_key in seen_entries:
                continue
            seen_entries.add(entry_key)
            raw_size = img_data.get('Size', 0)
            if raw_size >= 1e9:
                size_str = f'{round(raw_size / 1e9, 2)} GB'
            elif raw_size >= 1e6:
                size_str = f'{round(raw_size / 1e6, 1)} MB'
            elif raw_size >= 1e3:
                size_str = f'{round(raw_size / 1e3, 1)} KB'
            else:
                size_str = f'{raw_size} B'
            created_ts = img_data.get('Created', 0)
            try:
                ct = datetime.datetime.fromtimestamp(created_ts, tz=datetime.timezone.utc)
                delta = now_dt - ct
                days = delta.days
                if days < 1:
                    created_str = 'today'
                elif days == 1:
                    created_str = 'yesterday'
                elif days < 30:
                    created_str = f'{days} days ago'
                elif days < 365:
                    months = days // 30
                    created_str = f'{months} month{"s" if months > 1 else ""} ago'
                else:
                    years = days // 365
                    created_str = f'{years} year{"s" if years > 1 else ""} ago'
            except:
                created_str = str(created_ts)
            result.append({
                'id': short_id,
                'full_id': img_data['Id'],
                'repo': display_repo,
                'original_repo': parts[0],
                'original_ref': tag,
                'local_ref': local_ref,
                'canonical_ref': canonical_ref,
                'display_name': display_name,
                'is_system_r_image': is_system_r_image,
                'tag': display_tag,
                'size': size_str,
                'created': created_str,
                'image_id': short_id
            })
    result.sort(key=lambda x: x['repo'].lower())
    return result


@app.route('/podman/images/remove', methods=['POST'])
def podman_remove_image():
    """Remove a local image by ID."""
    client = get_docker_client()
    if not client:
        return jsonify({'status': 'error', 'message': 'Podman is not running'})
    data = request.get_json(force=True)
    image_id = data.get('image_id', '').strip()
    repo = data.get('repo', '').strip()
    if not image_id:
        return jsonify({'status': 'error', 'message': 'No image_id provided'})
    _strip_repo = repo
    for pfx in ('docker.io/library/', 'docker.io/', 'index.docker.io/library/', 'index.docker.io/', 'registry-1.docker.io/library/', 'registry-1.docker.io/', 'localhost/'):
        if _strip_repo.startswith(pfx):
            _strip_repo = _strip_repo[len(pfx):]
            break
    if _strip_repo == 'primigenius-r-base' or _strip_repo.startswith('primigenius-r-base:'):
        return jsonify({'status': 'error', 'message': 'Cannot remove system-required image'})
    try:
        if _is_pinned_r_runtime_local_ref(repo):
            _remove_custom_r_runtime_images(client)
        if _strip_repo == 'quay.io/biocontainers/star' or _strip_repo.startswith('quay.io/biocontainers/star:'):
            try:
                client.images.remove('primigenius-star-align:latest', force=True)
                log(f'[SYSTEM] Cascade-removed custom image primigenius-star-align:latest along with base image')
            except Exception:
                pass
        client.images.remove(image=image_id, force=True)
        _image_exists_cache.clear()
        _images_cache['data'] = None
        _images_cache['time'] = 0
        threading.Thread(target=_refresh_images_cache, daemon=True).start()
        log(f'[SYSTEM] Removed image: {image_id}')
        return jsonify({'status': 'success'})
    except Exception as e:
        log(f'[ERROR] podman_remove_image: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


def _validate_local_tar_path(raw_path, must_exist):
    path_value = os.path.abspath(str(raw_path or '').strip())
    if not raw_path or not os.path.isabs(path_value):
        raise ValueError('A local absolute path is required')
    if not path_value.lower().endswith('.tar'):
        raise ValueError('Only .tar image archives are supported')
    if must_exist:
        if not os.path.isfile(path_value):
            raise ValueError(f'Image archive not found: {path_value}')
    else:
        parent = os.path.dirname(path_value)
        if not os.path.isdir(parent):
            raise ValueError(f'Export directory not found: {parent}')
    return path_value


@app.route('/podman/images/export', methods=['POST'])
def podman_export_image():
    """Export one local image as a Docker-compatible tar archive."""
    mgr = _get_podman_manager()
    data = request.get_json(force=True) or {}
    image_ref = str(data.get('image') or '').strip()
    if not image_ref:
        return jsonify({'status': 'error', 'message': 'No image selected'}), 400
    try:
        output_path = _validate_local_tar_path(data.get('path'), must_exist=False)
        if not mgr.is_engine_ready():
            return jsonify({'status': 'error', 'message': 'Podman is not running'}), 503
        if not _image_archive_lock.acquire(blocking=False):
            return jsonify({'status': 'error', 'message': 'Another image import or export is already running'}), 409
        try:
            wsl_path = mgr.host_path_to_wsl(output_path)
            command = (
                f'podman save --format docker-archive -o {shlex.quote(wsl_path)} '
                f'{shlex.quote(image_ref)}'
            )
            result = mgr._run_wsl(command, timeout=24 * 60 * 60)
            if result is None or result.returncode != 0:
                detail = ((result.stderr or '') or (result.stdout or '')).strip() if result else 'Podman command failed'
                raise RuntimeError(detail)
        finally:
            _image_archive_lock.release()
        return jsonify({'status': 'success', 'path': output_path, 'image': image_ref})
    except Exception as e:
        log(f'[ERROR] podman_export_image: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/podman/images/load', methods=['POST'])
def podman_load_image_archive():
    """Load images from a local Docker/OCI tar archive."""
    global _images_cache
    mgr = _get_podman_manager()
    data = request.get_json(force=True) or {}
    try:
        input_path = _validate_local_tar_path(data.get('path'), must_exist=True)
        if not mgr.is_engine_ready():
            return jsonify({'status': 'error', 'message': 'Podman is not running'}), 503
        if not _image_archive_lock.acquire(blocking=False):
            return jsonify({'status': 'error', 'message': 'Another image import or export is already running'}), 409
        try:
            wsl_path = mgr.host_path_to_wsl(input_path)
            result = mgr._run_wsl(f'podman load -i {shlex.quote(wsl_path)}', timeout=24 * 60 * 60)
            if result is None or result.returncode != 0:
                detail = ((result.stderr or '') or (result.stdout or '')).strip() if result else 'Podman command failed'
                raise RuntimeError(detail)
            output = ((result.stdout or '') + '\n' + (result.stderr or '')).strip()
            _image_exists_cache.clear()
            _images_cache = {'data': None, 'time': 0}
            _refresh_images_cache_sync()
        finally:
            _image_archive_lock.release()
        return jsonify({'status': 'success', 'path': input_path, 'output': output})
    except Exception as e:
        log(f'[ERROR] podman_load_image_archive: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/podman/images/search', methods=['GET'])
def podman_search_images():
    """Search Docker Hub for images (Podman-compatible)."""
    client = None
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'status': 'error', 'message': 'Empty search query'})

    def _normalize_direct_name(name):
        n = (name or '').strip().lower()
        if not n:
            return ''
        if n.startswith('docker.io/'):
            n = n[len('docker.io/'):]
        if n.startswith('index.docker.io/'):
            n = n[len('index.docker.io/'):]
        if n.startswith('registry-1.docker.io/'):
            n = n[len('registry-1.docker.io/'):]
        if ':' in n and n.rsplit(':', 1)[1]:
            n = n.rsplit(':', 1)[0]
        if '/' not in n:
            n = f'library/{n}'
        return n

    def _build_offline_candidates(q, limit=25):
        qn = _normalize_direct_name(q)
        if not qn:
            return []

        candidates = [qn]
        # 对单词查询再补一个原样候选，方便用户直接拉取
        raw = (q or '').strip().lower()
        if raw and raw != qn and '/' in raw:
            candidates.append(raw)
        if raw and '/' not in raw and re.match(r'^[a-z0-9][a-z0-9._-]*$', raw):
            candidates.append(f'quay.io/biocontainers/{raw}')
            candidates.append(f'biocontainers/{raw}')

        out = []
        for nm in candidates[:int(limit)]:
            out.append({
                'name': nm,
                'description': 'Direct pull candidate (network fallback mode).',
                'star_count': 0,
                'is_official': nm.startswith('library/'),
                'is_automated': False
            })
        return out

    def _map_hub_items(items):
        mapped = []
        for it in items or []:
            namespace = (it.get('namespace') or '').strip()
            name = (it.get('name') or '').strip()
            if not name:
                continue
            full_name = f"{namespace}/{name}" if namespace else name
            mapped.append({
                'name': full_name,
                'description': it.get('description') or '',
                'star_count': it.get('star_count') or 0,
                'is_official': bool(it.get('is_official')),
                'is_automated': False
            })
        return mapped

    def _normalize_for_fuzzy(text):
        return re.sub(r'[^a-z0-9]+', '', str(text or '').lower())

    def _fuzzy_score_item(item, q):
        q_norm = _normalize_for_fuzzy(q)
        if not q_norm:
            return 0

        name = str((item or {}).get('name') or '')
        desc = str((item or {}).get('description') or '')
        name_l = name.lower()
        name_norm = _normalize_for_fuzzy(name)
        desc_norm = _normalize_for_fuzzy(desc)

        if name_norm == q_norm:
            return 100000

        idx = name_norm.find(q_norm)
        if idx >= 0:
            return 90000 - idx

        raw_idx = name_l.find(str(q or '').strip().lower())
        if raw_idx >= 0:
            return 80000 - raw_idx

        # Subsequence fuzzy match for lightweight typo tolerance.
        qi = 0
        gap_penalty = 0
        last_pos = -1
        for i, ch in enumerate(name_norm):
            if qi < len(q_norm) and ch == q_norm[qi]:
                if last_pos >= 0:
                    gap_penalty += max(0, i - last_pos - 1)
                last_pos = i
                qi += 1
        if qi == len(q_norm):
            return 50000 - min(gap_penalty, 2000)

        if q_norm in desc_norm:
            return 10000

        return -1

    def _rank_search_results(items, q):
        scored = []
        for item in items or []:
            score = _fuzzy_score_item(item, q)
            if score < 0:
                continue
            try:
                stars = int((item or {}).get('star_count') or 0)
            except Exception:
                stars = 0
            scored.append((score, stars, item))

        scored.sort(key=lambda x: (-x[0], -x[1], str((x[2] or {}).get('name') or '')))
        return [x[2] for x in scored]

    def _search_via_http_source(q, source, limit=25):
        if source == 'hub':
            url = (
                'https://hub.docker.com/v2/search/repositories/'
                f'?query={urllib.parse.quote(q)}&page_size={int(limit)}'
            )
        elif source == 'index':
            url = (
                'https://index.docker.io/v1/search'
                f'?q={urllib.parse.quote(q)}&n={int(limit)}'
            )
        else:
            raise ValueError(f'Unknown http search source: {source}')

        prefer_ipv4 = _effective_prefer_ipv4()
        last_err = None
        for timeout in _search_timeouts():
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'PrimiGenius/2.0'})
                with _prefer_ipv4_dns(prefer_ipv4):
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        payload = json.loads(resp.read().decode('utf-8', errors='replace'))
                items = payload.get('results') or []
                return _map_hub_items(items)
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        return []

    limit = 25
    try:
        limit = int(request.args.get('limit', '25'))
    except Exception:
        limit = 25
    limit = max(1, min(100, limit))
    verify_downloadable = str(request.args.get('verify', '1')).strip().lower() not in ('0', 'false', 'no', 'off')

    def _finalize_results(items, source_name, warning='', skip_verify=False):
        ranked_items = _rank_search_results(items or [], query)
        final_items = ranked_items if ranked_items else (items or [])
        if skip_verify or (not verify_downloadable):
            payload = {'status': 'success', 'results': final_items, 'source': source_name}
            if warning:
                payload['warning'] = warning
            return jsonify(payload)

        original_items = list(final_items)
        filtered, dropped = _filter_downloadable_results(original_items, limit)

        # If verification filtering removes everything, keep original search items
        # to ensure Hub-searchable images remain visible in UI.
        use_filtered = bool(filtered)
        shown_results = filtered if use_filtered else original_items[:limit]

        payload = {'status': 'success', 'results': shown_results, 'source': source_name, 'verified': bool(use_filtered)}
        warns = []
        if warning:
            warns.append(str(warning))
        if dropped:
            warns.append(f'filtered {len(dropped)} legacy/unavailable images')
            log(f"[SYSTEM] docker search filtered: {'; '.join(dropped[:8])}")
        if (not use_filtered) and original_items:
            warns.append('verification fallback: showing unverified Hub results')
        if warns:
            payload['warning'] = '; '.join(warns)
        return jsonify(payload)

    # Fast path for direct image names in CN mode (e.g. namespace/repo):
    # return immediate candidate to avoid waiting on unstable overseas search APIs.
    mode_now = _effective_network_mode()
    qraw = query.strip()
    q_norm = _normalize_for_fuzzy(qraw)
    query_variants = [qraw]
    if q_norm and q_norm not in query_variants:
        query_variants.append(q_norm)
    if q_norm and len(q_norm) >= 6:
        # Broaden with a shorter token to improve fuzzy recall on Docker Hub side.
        broad = q_norm[:max(3, min(5, len(q_norm) // 2))]
        if broad not in query_variants:
            query_variants.append(broad)

    priority_fast = _build_priority_candidates(qraw, limit=limit)
    if priority_fast:
        return _finalize_results(
            priority_fast,
            'priority-fastpath',
            'Priority image fast-path enabled; pull will use mirror fallback strategy.',
            skip_verify=True
        )

    # Do not short-circuit slash-form queries (namespace/repo).
    # Always run real Hub search first so official searchable images are discoverable.

    configured_sources = _effective_search_sources()
    allowed = {'engine', 'hub', 'index', 'offline'}
    sources = [s.lower() for s in configured_sources if s.lower() in allowed]
    if not sources:
        sources = ['engine', 'hub', 'index', 'offline']

    errors = []
    for src in sources:
        try:
            if src == 'engine':
                if client is None:
                    client = get_docker_client()
                if not client:
                    errors.append('engine: Podman API unavailable')
                    continue
                results = []
                seen_names = set()
                for qv in query_variants:
                    tmp = client.images.search(qv, limit=limit)
                    if not isinstance(tmp, list):
                        continue
                    for it in tmp:
                        nm = str((it or {}).get('name') or '')
                        if nm and nm in seen_names:
                            continue
                        if nm:
                            seen_names.add(nm)
                        results.append(it)
                if results:
                    return _finalize_results(results, 'engine')
                errors.append('engine: empty result')
                continue

            if src in ('hub', 'index'):
                results = []
                seen_names = set()
                for qv in query_variants:
                    tmp = _search_via_http_source(qv, src, limit=limit)
                    for it in tmp:
                        nm = str((it or {}).get('name') or '')
                        if nm and nm in seen_names:
                            continue
                        if nm:
                            seen_names.add(nm)
                        results.append(it)
                if results:
                    return _finalize_results(results, f'{src}-http-fallback')
                errors.append(f'{src}: empty result')
                continue

            if src == 'offline':
                offline = _build_offline_candidates(query, limit=limit)
                return _finalize_results(
                    offline,
                    'offline-candidate-fallback',
                    '; '.join(errors) if errors else 'Search source unavailable',
                    skip_verify=True
                )
        except Exception as e:
            errors.append(f'{src}: {e!r}')
            log(f'[WARN] podman_search_images source failed [{src}]: {e!r}')
            continue

    # 理论上不会到这里；兜底保持前端可用
    return _finalize_results(
        _build_offline_candidates(query, limit=limit),
        'offline-candidate-fallback',
        '; '.join(errors) if errors else 'Search failed',
        skip_verify=True
    )


@app.route('/podman/images/hub-size', methods=['GET'])
def podman_hub_image_size():
    """Get the compressed size of an image from Docker Hub API."""
    image_ref = _resolve_system_image_ref(request.args.get('image', '').strip())
    if not image_ref:
        return jsonify({'status': 'error', 'message': 'No image name'})
    normalized, namespace, repo, tag = _split_hub_image_ref(image_ref)
    url = f'https://hub.docker.com/v2/repositories/{namespace}/{repo}/tags/{tag}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'PrimiGenius/2.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        full_size = data.get('full_size', 0)
        if full_size and full_size >= 1e9:
            size_str = f'{round(full_size / 1e9, 2)} GB'
        elif full_size and full_size >= 1e6:
            size_str = f'{round(full_size / 1e6, 1)} MB'
        else:
            size_str = f'{round((full_size or 0) / 1e3, 1)} KB'
        return jsonify({'status': 'success', 'full_size': full_size, 'size_str': size_str, 'image': normalized, 'tag': tag})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


def _split_hub_image_ref(image_ref):
    """Parse image ref into (normalized_name, namespace, repo, tag)."""
    raw = (image_ref or '').strip()
    tag = 'latest'

    no_digest = raw.split('@', 1)[0]
    slash = no_digest.rfind('/')
    colon = no_digest.rfind(':')
    if colon > slash:
        tag = no_digest[colon + 1:].strip() or 'latest'
        no_digest = no_digest[:colon]

    name = no_digest
    if name.startswith('docker.io/'):
        name = name[len('docker.io/'):]
    elif name.startswith('index.docker.io/'):
        name = name[len('index.docker.io/'):]
    elif name.startswith('registry-1.docker.io/'):
        name = name[len('registry-1.docker.io/'):]

    parts = name.split('/')
    if len(parts) == 1:
        namespace, repo = 'library', parts[0]
    else:
        namespace, repo = parts[0], '/'.join(parts[1:])

    normalized = f"{namespace}/{repo}"
    return normalized, namespace, repo, tag


def _is_probably_dockerhub_ref(image_ref):
    """Return True when image ref most likely targets Docker Hub."""
    raw = (image_ref or '').strip()
    if not raw:
        return False

    name = raw.split('@', 1)[0]
    slash = name.rfind('/')
    colon = name.rfind(':')
    if colon > slash:
        name = name[:colon]

    if name.startswith('docker.io/') or name.startswith('index.docker.io/') or name.startswith('registry-1.docker.io/'):
        return True

    first = name.split('/', 1)[0]
    if '.' in first or ':' in first or first == 'localhost':
        return False
    return True


def _hub_manifest_cache_get(normalized, tag):
    key = f"{normalized}:{tag or 'latest'}"
    now = time.time()
    with _HUB_MANIFEST_CHECK_CACHE_LOCK:
        item = _HUB_MANIFEST_CHECK_CACHE.get(key)
        if item and (now - float(item.get('ts', 0))) < 6 * 3600:
            return bool(item.get('ok')), str(item.get('reason') or '')
    return None, None


def _hub_manifest_cache_put(normalized, tag, ok, reason):
    key = f"{normalized}:{tag or 'latest'}"
    with _HUB_MANIFEST_CHECK_CACHE_LOCK:
        _HUB_MANIFEST_CHECK_CACHE[key] = {
            'ok': bool(ok),
            'reason': str(reason or ''),
            'ts': time.time()
        }


def _is_hub_ref_downloadable(image_ref):
    """Preflight check: tag must resolve to schema v2/OCI manifest.
    Returns (ok, reason)."""
    if not _is_probably_dockerhub_ref(image_ref):
        return True, 'non-dockerhub-skip'

    normalized, namespace, repo, tag = _split_hub_image_ref(image_ref)
    cached_ok, cached_reason = _hub_manifest_cache_get(normalized, tag)
    if cached_ok is not None:
        return cached_ok, cached_reason

    token_url = (
        'https://auth.docker.io/token?service=registry.docker.io&scope=' +
        urllib.parse.quote(f'repository:{namespace}/{repo}:pull', safe=':/')
    )
    manifest_url = f'https://registry-1.docker.io/v2/{namespace}/{repo}/manifests/{tag}'
    accept_header = ', '.join([
        'application/vnd.oci.image.manifest.v1+json',
        'application/vnd.docker.distribution.manifest.v2+json',
        'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.oci.image.index.v1+json',
        'application/vnd.docker.distribution.manifest.v1+json'
    ])

    prefer_ipv4 = _effective_prefer_ipv4()
    try:
        req = urllib.request.Request(token_url, headers={'User-Agent': 'PrimiGenius/2.0'})
        with _prefer_ipv4_dns(prefer_ipv4):
            with urllib.request.urlopen(req, timeout=6) as resp:
                token_payload = json.loads(resp.read().decode('utf-8', errors='replace'))
        token = token_payload.get('token') or token_payload.get('access_token')
        if not token:
            reason = 'docker-hub-token-missing'
            _hub_manifest_cache_put(normalized, tag, False, reason)
            return False, reason

        req = urllib.request.Request(
            manifest_url,
            headers={
                'User-Agent': 'PrimiGenius/2.0',
                'Authorization': f'Bearer {token}',
                'Accept': accept_header
            }
        )
        with _prefer_ipv4_dns(prefer_ipv4):
            with urllib.request.urlopen(req, timeout=8) as resp:
                ctype = (resp.headers.get('Content-Type') or '').lower()

        if 'manifest.v1+prettyjws' in ctype or 'manifest.v1+json' in ctype:
            reason = 'legacy-manifest-v1'
            _hub_manifest_cache_put(normalized, tag, False, reason)
            return False, reason
        if ('manifest.v2+json' in ctype) or ('manifest.list.v2+json' in ctype) or ('oci.image.manifest.v1+json' in ctype) or ('oci.image.index.v1+json' in ctype):
            _hub_manifest_cache_put(normalized, tag, True, 'ok')
            return True, 'ok'

        reason = f'unknown-manifest-content-type:{ctype or "n/a"}'
        _hub_manifest_cache_put(normalized, tag, False, reason)
        return False, reason
    except urllib.error.HTTPError as e:
        reason = f'http-{getattr(e, "code", "error")}'
        _hub_manifest_cache_put(normalized, tag, False, reason)
        return False, reason
    except Exception as e:
        reason = f'preflight-error:{e}'
        _hub_manifest_cache_put(normalized, tag, False, reason)
        return False, reason


def _is_hard_preflight_failure(reason):
    """Only deterministic failures should block pulling.
    Network/timeouts/CDN issues must not prevent actual docker pull attempts."""
    r = str(reason or '').strip().lower()
    if not r:
        return False
    return r in ('legacy-manifest-v1', 'http-404')


def _is_soft_preflight_failure(reason):
    return not _is_hard_preflight_failure(reason)


def _filter_downloadable_results(results, limit):
    out = []
    dropped = []
    for item in (results or []):
        name = (item or {}).get('name') if isinstance(item, dict) else None
        if not name:
            continue

        # Never hide critical image candidates because remote preflight can be flaky on CN links.
        if _is_priority_image_name(name):
            out.append(item)
            if len(out) >= limit:
                break
            continue

        check_ref = name if ':' in name else f'{name}:latest'
        ok, reason = _is_hub_ref_downloadable(check_ref)
        if ok or _is_soft_preflight_failure(reason):
            out.append(item)
            if len(out) >= limit:
                break
        else:
            dropped.append(f'{name} ({reason})')
    return out, dropped


@app.route('/podman/images/detail', methods=['GET'])
def podman_hub_image_detail():
    """Get Docker Hub repository details and tag-level metadata for a searched image."""
    image_ref = _resolve_system_image_ref(request.args.get('image', '').strip())
    if not image_ref:
        return jsonify({'status': 'error', 'message': 'No image name'})

    normalized, namespace, repo, tag = _split_hub_image_ref(image_ref)
    repo_api = f'https://hub.docker.com/v2/repositories/{namespace}/{repo}'
    tag_api = f'https://hub.docker.com/v2/repositories/{namespace}/{repo}/tags/{tag}'

    headers = {'User-Agent': 'PrimiGenius/2.0'}
    prefer_ipv4 = _effective_prefer_ipv4()
    warnings = []
    repo_data = None
    tag_data = None

    try:
        req = urllib.request.Request(repo_api, headers=headers)
        with _prefer_ipv4_dns(prefer_ipv4):
            with urllib.request.urlopen(req, timeout=8) as resp:
                repo_data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except Exception as e:
        warnings.append(f'repository detail unavailable: {e}')

    try:
        req = urllib.request.Request(tag_api, headers=headers)
        with _prefer_ipv4_dns(prefer_ipv4):
            with urllib.request.urlopen(req, timeout=8) as resp:
                tag_data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except Exception as e:
        warnings.append(f'tag detail unavailable: {e}')

    if not repo_data and not tag_data:
        return jsonify({'status': 'error', 'message': '; '.join(warnings) or 'Docker Hub request failed'})

    full_size = (tag_data or {}).get('full_size') or 0
    arches = []
    for img in (tag_data or {}).get('images') or []:
        os_name = (img or {}).get('os') or ''
        arch_name = (img or {}).get('architecture') or ''
        variant = (img or {}).get('variant') or ''
        item = '/'.join([x for x in [os_name, arch_name, variant] if x])
        if item and item not in arches:
            arches.append(item)

    full_desc = (repo_data or {}).get('full_description') or ''
    if len(full_desc) > 12000:
        full_desc = full_desc[:12000] + '\n...'

    payload = {
        'status': 'success',
        'image': normalized,
        'namespace': namespace,
        'repo': repo,
        'tag': tag,
        'hub_url': f'https://hub.docker.com/r/{namespace}/{repo}',
        'description': (repo_data or {}).get('description') or '',
        'full_description': full_desc,
        'star_count': (repo_data or {}).get('star_count') or 0,
        'pull_count': (repo_data or {}).get('pull_count') or 0,
        'is_official': bool((repo_data or {}).get('is_official')),
        'last_updated': (tag_data or {}).get('last_updated') or (repo_data or {}).get('last_updated') or '',
        'full_size': full_size,
        'size_str': _format_bytes(full_size),
        'architectures': arches,
        'source': 'hub-v2'
    }
    if warnings:
        payload['warning'] = '; '.join(warnings)
    return jsonify(payload)


@app.route('/podman/images/pull', methods=['POST'])
def podman_pull_image():
    """Pull an image with streaming progress via SSE. Uses parallel mirror sources."""
    data = request.get_json(force=True)
    image_name = _resolve_system_image_ref(data.get('image', '').strip())
    archive_only = data.get('archive_only') is True
    if not image_name:
        return jsonify({'status': 'error', 'message': 'No image name provided'})
    if archive_only and image_name != R_DOCKER_IMAGE:
        return jsonify({'status': 'error', 'message': 'Archive fallback is only available for the pinned R image'}), 400

    pull_run_id = f'imgpull_{uuid.uuid4().hex[:8]}'
    pull_error = [None]

    def generate():
        def _pull_thread():
            _LOG_CONTEXT.run_id = pull_run_id
            _LOG_CONTEXT.channel = 'Pull'
            try:
                client = get_docker_client()
                if not client:
                    pull_error[0] = 'Cannot connect to Podman API'
                    return
                if archive_only:
                    with _IMAGE_PULL_LOCK:
                        _pull_in_progress.set()
                        try:
                            log('[SYSTEM] Downloading the verified R image archive from GitHub Release')
                            _install_pinned_r_image_archive(client)
                        finally:
                            _pull_in_progress.clear()
                else:
                    pull_image_with_progress(client, image_name)
                found = False
                for _attempt in range(3):
                    if _image_exists_locally(client, image_name):
                        found = True
                        break
                    time.sleep(1)
                if not found:
                    raise RuntimeError(f'Image {image_name} reported as pulled but not found in local storage after final verification')
                _image_exists_cache.pop(image_name, None)
                _images_cache['data'] = None
                _images_cache['time'] = 0
                threading.Thread(target=_refresh_images_cache, daemon=True).start()
            except Exception as e:
                pull_error[0] = str(e)
            finally:
                _LOG_CONTEXT.run_id = None
                _publish_run_log_event(pull_run_id, 'Pull', '[__PULL_DONE__]')

        threading.Thread(target=_pull_thread, daemon=True).start()

        try:
            last_seq = 0
            last_emit = time.monotonic()
            while True:
                with _LOG_EVENT_COND:
                    _LOG_EVENT_COND.wait(timeout=0.5)
                    events = [e for e in _LOG_EVENTS if e.get('runId') == pull_run_id and e.get('seq', 0) > last_seq]

                for evt in sorted(events, key=lambda e: e.get('seq', 0)):
                    last_seq = evt.get('seq', last_seq)
                    raw_line = evt.get('line', '')

                    if raw_line.endswith('[__PULL_DONE__]'):
                        if pull_error[0]:
                            yield f"data: {json.dumps({'type': 'error', 'message': pull_error[0]})}\n\n"
                        else:
                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return

                    lower = raw_line.lower()
                    archive_progress = re.search(r'\[ARCHIVE_PROGRESS\]\s+(\d+)\s+(\d+)\s+(\d+)', raw_line)
                    archive_status = re.search(r'\[ARCHIVE_STATUS\]\s+(\w+)', raw_line)
                    if archive_progress:
                        percent, current, total = (int(value) for value in archive_progress.groups())
                        yield f"data: {json.dumps({'type': 'progress', 'percent': percent, 'current': current, 'total': total, 'status': 'Downloading', 'layer': 'PrimiGenius archive'})}\n\n"
                    elif archive_status:
                        phase = archive_status.group(1)
                        phase_percent = 0 if phase == 'Probing' else 99
                        yield f"data: {json.dumps({'type': 'progress', 'percent': phase_percent, 'current': 0, 'total': 0, 'status': phase, 'layer': 'PrimiGenius archive'})}\n\n"
                    elif 'pull complete' in lower:
                        # Layer transfer has finished, but local tag/digest checks
                        # still need to complete. Reserve 100% for the final done event.
                        yield f"data: {json.dumps({'type': 'progress', 'percent': 99, 'current': 0, 'total': 0, 'status': 'Verifying', 'layer': ''})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'status', 'status': raw_line, 'id': ''})}\n\n"
                    last_emit = time.monotonic()

                # Keep the local fetch/SSE stream alive while Podman is busy on
                # a large layer and produces no newline-delimited status output.
                if not events and (time.monotonic() - last_emit) >= 10:
                    yield ': keepalive\n\n'
                    last_emit = time.monotonic()
        finally:
            pass
    return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/podman/network-profile', methods=['GET'])
def podman_network_profile_get():
    mode = _effective_network_mode()
    strategy = NETWORK_MODE_STRATEGIES.get(mode, NETWORK_MODE_STRATEGIES['auto'])
    return jsonify({
        'status': 'success',
        'mode': mode,
        'mode_options': ['auto', 'cn', 'global'],
        'strategy': {
            'search_sources': strategy.get('search_sources', []),
            'prefer_ipv4': bool(strategy.get('prefer_ipv4', True)),
            'search_timeouts': strategy.get('search_timeouts', [])
        }
    })


@app.route('/podman/network-profile', methods=['POST'])
def podman_network_profile_set():
    data = request.get_json(force=True) or {}
    mode = _safe_network_mode(data.get('mode'))
    saved = _save_network_profile(mode)
    strategy = NETWORK_MODE_STRATEGIES.get(mode, NETWORK_MODE_STRATEGIES['auto'])
    log(f'[SYSTEM] Podman network profile set to: {mode}')
    return jsonify({
        'status': 'success',
        'mode': saved.get('mode', mode),
        'strategy': {
            'search_sources': strategy.get('search_sources', []),
            'prefer_ipv4': bool(strategy.get('prefer_ipv4', True)),
            'search_timeouts': strategy.get('search_timeouts', [])
        }
    })


@app.route('/podman/plugin-image', methods=['POST'])
def podman_switch_plugin_image():
    """Switch a plugin's docker_image in its config.json."""
    data = request.get_json(force=True)
    plugin_id = data.get('pluginId', '').strip()
    new_image = data.get('docker_image', '').strip()
    if not plugin_id or not new_image:
        return jsonify({'status': 'error', 'message': 'Missing pluginId or docker_image'})
    plugins = load_all_plugins()
    cfg = plugins.get(plugin_id)
    if not cfg:
        return jsonify({'status': 'error', 'message': 'Plugin not found'})
    config_path = os.path.join(cfg['plugin_dir'], 'config.json')
    try:
        raw = _load_json_file(config_path)
        raw['docker_image'] = new_image
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(raw, f, ensure_ascii=False, indent=4)
        log(f'[SYSTEM] Plugin {plugin_id} docker_image changed to {new_image}')
        return jsonify({'status': 'success'})
    except Exception as e:
        log(f'[ERROR] docker_switch_plugin_image: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/podman/config', methods=['GET'])
def podman_config_read():
    mgr = _get_podman_manager()
    try:
        content, config_path = mgr.read_config()
        mirrors = mgr._get_configured_mirrors()
        return jsonify({'status': 'success', 'config': content, 'path': config_path, 'registry-mirrors': mirrors})
    except Exception as e:
        log(f'[ERROR] podman_config_read: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/podman/mirror', methods=['POST'])
def podman_mirror_configure():
    mgr = _get_podman_manager()
    data = request.get_json(force=True)
    mirrors = data.get('mirrors', [])
    mirrors = [m.strip().removeprefix('https://').removeprefix('http://') for m in mirrors if m and m.strip()]
    try:
        mgr.configure_mirror(mirrors)
        log(f'[SYSTEM] Podman mirror configured: {mirrors}')
        return jsonify({'status': 'success', 'mirrors': mirrors})
    except Exception as e:
        log(f'[ERROR] podman_mirror_configure: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/podman/custom-mirrors', methods=['GET'])
def podman_custom_mirrors_get():
    mirrors = _load_custom_mirrors()
    return jsonify({'status': 'success', 'mirrors': mirrors})


@app.route('/podman/custom-mirrors', methods=['POST'])
def podman_custom_mirrors_set():
    data = request.get_json(force=True) or {}
    mirrors = data.get('mirrors', [])
    mirrors = [m.strip().rstrip('/').removeprefix('https://').removeprefix('http://') for m in mirrors if m and str(m).strip()]
    _save_custom_mirrors(mirrors)
    log(f'[SYSTEM] Custom mirrors updated: {mirrors}')
    return jsonify({'status': 'success', 'mirrors': mirrors})


@app.route('/podman/config', methods=['POST'])
def podman_config_write():
    mgr = _get_podman_manager()
    data = request.get_json(force=True)
    config_str = data.get('config', '').strip()
    if not config_str:
        return jsonify({'status': 'error', 'message': 'Empty configuration'})
    try:
        config_path = mgr.write_config(config_str)
        log(f'[SYSTEM] Podman containers.conf updated: {config_path}')
        if platform.system() == 'Windows':
            mgr.machine_stop()
            time.sleep(2)
            mgr.machine_start()
            log('[SYSTEM] Podman Machine restarting after config change ...')
        return jsonify({'status': 'success', 'path': config_path})
    except Exception as e:
        log(f'[ERROR] podman_config_write: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/podman/machine/relocate', methods=['POST'])
def podman_machine_relocate():
    mgr = _get_podman_manager()
    data = request.get_json(force=True)
    new_path = data.get('path', '').strip()
    if not new_path:
        return jsonify({'status': 'error', 'message': 'No target path provided'})
    log(f'[SYSTEM] Podman Machine relocate: target = {new_path}')
    try:
        ok, message = mgr.relocate_data_dir(new_path)
        if not ok:
            return jsonify({'status': 'error', 'message': message})
        log(f'[SYSTEM] Podman Machine storage relocated to {message} successfully.')
        return jsonify({'status': 'success', 'new_path': message})
    except Exception as e:
        log(f'[ERROR] podman_machine_relocate: {e}')
        return jsonify({'status': 'error', 'message': str(e)})


# ==========================================
# Podman Machine Management API
# ==========================================

@app.route('/podman/machine/status', methods=['GET'])
def podman_machine_status():
    mgr = _get_podman_manager()
    return jsonify(mgr.machine_status())


@app.route('/podman/machine/init', methods=['POST'])
def podman_machine_init():
    mgr = _get_podman_manager()
    data = request.get_json(force=True) or {}
    cpus = data.get('cpus', 4)
    memory = data.get('memory', 8192)
    disk_size = data.get('disk_size', 100)
    name = data.get('name', '').strip()
    ok, msg = mgr.machine_init(cpus, memory, disk_size, name)
    if ok:
        return jsonify({'status': 'success', 'output': msg})
    return jsonify({'status': 'error', 'message': msg})


@app.route('/podman/machine/start', methods=['POST'])
def podman_machine_start():
    mgr = _get_podman_manager()
    data = request.get_json(force=True) or {}
    name = data.get('name', '').strip()
    ok, msg = mgr.machine_start(name)
    if ok:
        return jsonify({'status': 'success', 'output': msg})
    return jsonify({'status': 'error', 'message': msg})


@app.route('/podman/machine/stop', methods=['POST'])
def podman_machine_stop():
    mgr = _get_podman_manager()
    data = request.get_json(force=True) or {}
    name = data.get('name', '').strip()
    ok, msg = mgr.machine_stop(name)
    if ok:
        return jsonify({'status': 'success', 'output': msg})
    return jsonify({'status': 'error', 'message': msg})


@app.route('/podman/machine/reset', methods=['POST'])
def podman_machine_reset():
    mgr = _get_podman_manager()
    data = request.get_json(force=True) or {}
    name = data.get('name', '').strip()
    cpus = data.get('cpus', 4)
    memory = data.get('memory', 8192)
    disk_size = data.get('disk_size', 100)
    ok, msg = mgr.machine_reset(cpus, memory, disk_size, name)
    if ok:
        return jsonify({'status': 'success', 'output': msg})
    return jsonify({'status': 'error', 'message': msg})


@app.route('/podman/auto-setup', methods=['POST'])
def podman_auto_setup():
    mgr = _get_podman_manager()
    ok, msg = mgr.auto_setup()
    if ok:
        return jsonify({'status': 'success', 'message': msg})
    return jsonify({'status': 'error', 'message': msg})


@app.route('/podman/wsl-status', methods=['GET'])
def podman_wsl_status():
    global _podman_wsl_cache
    now = time.time()
    include_usage = str(request.args.get('include_usage', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    if (not include_usage) and _podman_wsl_cache['data'] and now - _podman_wsl_cache['time'] < 5:
        return jsonify(_podman_wsl_cache['data'])
    mgr = _get_podman_manager()
    setup_running = bool(_podman_async_running or getattr(mgr, '_setup_running', False))
    engine_ready = mgr.is_engine_ready()
    if setup_running and not engine_ready:
        machine_exists = False
        try:
            machine_exists = bool(mgr._is_wsl_distro_registered())
        except Exception:
            pass
        resp_data = {
            'status': 'success',
            'wsl_enabled': True,
            'wsl2_feature_enabled': True,
            'depends_on_wsl': platform.system() == 'Windows',
            'wsl_default_version': None,
            'wsl_distro_version': None,
            'wsl_distro_registered': machine_exists,
            'wsl_status_text': '',
            'wsl_distro_list_text': '',
            'podman_available': True,
            'podman_version': '',
            'machine_exists': machine_exists,
            'machine_running': False,
            'api_service_running': False,
            'setup_running': True,
            'client_type': 'starting',
            'machine_name': mgr.machine_name,
            'wsl_distro_name': mgr.wsl_distro_name,
            'runtime_channel': mgr.runtime_channel,
            'data_dir': mgr.podman_data_dir,
            'config_dir': mgr.podman_config_dir,
            'data_usage': ''
        }
        _podman_wsl_cache = {'data': resp_data, 'time': now}
        return jsonify(resp_data)
    wsl_info = mgr.get_wsl_status_details()
    api_running = False
    if mgr._wsl_service_started and mgr._test_tcp_connection():
        api_running = True
    elif mgr._api_service_proc is not None:
        try:
            mgr._api_service_proc.poll()
            api_running = mgr._api_service_proc.returncode is None
        except Exception:
            pass
    client_type = 'none'
    if api_running:
        try:
            client = getattr(mgr, '_api_client', None) or _api_client_cache.get('client') or _docker_client
            if client is not None:
                try:
                    from podman import PodmanClient as NativePodmanClient
                    client_type = 'podman-native' if isinstance(client, NativePodmanClient) else 'docker-sdk'
                except ImportError:
                    client_type = 'docker-sdk'
            else:
                client_type = 'api-ready'
        except Exception:
            client_type = 'api-ready'
    resp_data = {
        'status': 'success',
        'wsl_enabled': bool(wsl_info.get('wsl_available')),
        'wsl2_feature_enabled': bool(wsl_info.get('wsl2_enabled')),
        'depends_on_wsl': platform.system() == 'Windows',
        'wsl_default_version': wsl_info.get('default_version'),
        'wsl_distro_version': wsl_info.get('distro_version'),
        'wsl_distro_registered': bool(wsl_info.get('distro_registered')),
        'wsl_status_text': wsl_info.get('status_text') or '',
        'wsl_distro_list_text': wsl_info.get('distro_list_text') or '',
        'podman_available': mgr.is_podman_available(),
        'podman_version': mgr.get_podman_version(),
        'machine_exists': mgr.machine_exists(),
        'machine_running': mgr.machine_is_running(),
        'api_service_running': api_running,
        'setup_running': setup_running,
        'client_type': client_type,
        'machine_name': mgr.machine_name,
        'wsl_distro_name': mgr.wsl_distro_name,
        'runtime_channel': mgr.runtime_channel,
        'data_dir': mgr.podman_data_dir,
        'config_dir': mgr.podman_config_dir,
        'data_usage': mgr.get_data_usage() if include_usage else ''
    }
    if not include_usage:
        _podman_wsl_cache = {'data': resp_data, 'time': now}
    return jsonify(resp_data)


@app.route('/podman/cleanup', methods=['POST'])
def podman_full_cleanup():
    mgr = _get_podman_manager()
    mgr.cleanup_all()
    return jsonify({'status': 'success', 'message': 'All Podman data cleaned up'})


@app.route('/podman/shutdown', methods=['POST'])
def podman_shutdown():
    mgr = _get_podman_manager()
    data = request.get_json(force=True) or {}
    stop_machine = data.get('stop_machine', True)
    if stop_machine:
        mgr.stop_containers_and_machine()
    else:
        try:
            mgr._run_podman(['stop', '--all'], timeout=30)
        except Exception:
            pass
    return jsonify({'status': 'success'})


@app.route('/podman/suspend', methods=['POST'])
def podman_suspend():
    busy = _podman_suspend_busy_state()
    if any(busy.values()):
        return jsonify({
            'status': 'busy',
            'message': 'Podman was kept running because work is active.',
            'active': busy,
        })
    ok, message = _get_podman_manager().prepare_for_host_suspend()
    return jsonify({
        'status': 'success' if ok else 'error',
        'message': message,
        'active': busy,
    }), (200 if ok else 503)


@app.route('/podman/resume', methods=['POST'])
def podman_resume():
    busy = _podman_suspend_busy_state()
    if any(busy.values()):
        return jsonify({
            'status': 'busy',
            'message': 'Automatic recovery was skipped because work is active.',
            'active': busy,
        })
    ok, message = _get_podman_manager().auto_setup()
    return jsonify({
        'status': 'success' if ok else 'error',
        'message': message,
        'active': busy,
    }), (200 if ok else 503)


@app.route('/podman/api-service/start', methods=['POST'])
def podman_api_service_start():
    mgr = _get_podman_manager()
    ok = mgr.start_api_service(timeout=0)
    if ok:
        return jsonify({'status': 'success', 'message': 'Podman API service started'})
    return jsonify({'status': 'error', 'message': 'Failed to start API service'})


@app.route('/podman/api-service/stop', methods=['POST'])
def podman_api_service_stop():
    mgr = _get_podman_manager()
    mgr.stop_api_service()
    return jsonify({'status': 'success', 'message': 'Podman API service stopped'})


@app.route('/podman/client-type', methods=['GET'])
def podman_client_type():
    mgr = _get_podman_manager()
    client = mgr.get_podman_client()
    if client is None:
        return jsonify({'status': 'error', 'client_type': 'none', 'message': 'No Podman client available'})
    try:
        from podman import PodmanClient as NativePodmanClient  # type: ignore
        if isinstance(client, NativePodmanClient):
            return jsonify({'status': 'success', 'client_type': 'podman-native', 'message': 'Using Podman Python Bindings'})
    except ImportError:
        pass
    return jsonify({'status': 'success', 'client_type': 'docker-sdk', 'message': 'Using Docker SDK (Podman compatible)'})


if __name__ == '__main__':
    log("[SYSTEM] Starting PrimiGenius backend...")
    log("[SYSTEM] Initializing PodmanManager...")
    _get_podman_manager()
    log("[SYSTEM] Starting Podman asynchronously...")
    _start_podman_async()
    def _warmup_plugin_cache():
        time.sleep(3)
        try:
            load_all_plugins()
            log("[SYSTEM] Plugin config cache warmed up")
        except Exception:
            pass
        try:
            _cleanup_stale_containers()
        except Exception:
            pass
    threading.Thread(target=_warmup_plugin_cache, daemon=True).start()
    # Let Windows atomically assign an available loopback port. This avoids fixed-port
    # conflicts and Windows/Hyper-V excluded port ranges on end-user machines.
    server = make_server('127.0.0.1', 0, app, threaded=True)
    api_port = int(server.server_port)
    log(f"[SYSTEM] Starting API server on port {api_port} (Podman will be ready in background)...")
    # Machine-readable startup handshake consumed by the Electron main process.
    print(f"PRIMIGENIUS_API_PORT={api_port}", flush=True)
    server.serve_forever()
