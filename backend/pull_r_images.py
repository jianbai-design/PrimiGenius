import sys
import os
import subprocess
import time
import io
import platform

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

_SUBPROCESS_KWARGS = {}
if platform.system() == 'Windows':
    _SUBPROCESS_KWARGS['creationflags'] = 0x08000000 | 0x00000200
    _si = subprocess.STARTUPINFO()
    _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _si.wShowWindow = 0
    _SUBPROCESS_KWARGS['startupinfo'] = _si

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from podman_manager import PodmanManager

mgr = PodmanManager()

print(f"Machine exists: {mgr.machine_exists()}")
print(f"Machine running: {mgr.machine_is_running()}")

if not mgr.machine_is_running():
    print("Starting Podman Machine...")
    ok, msg = mgr.machine_start()
    print(f"Start: {ok}")
    time.sleep(15)

print(f"Engine ready: {mgr.is_engine_ready()}")

# Pull from docker.io directly with longer timeout
print("\nPulling rocker/r-ver:latest...")
env = os.environ.copy()
proc = subprocess.Popen(
    [mgr.podman_exe, 'pull', 'docker.io/library/rocker/r-ver:latest'],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    env=env,
    **_SUBPROCESS_KWARGS
)
start = time.time()
for line in proc.stdout:
    text = line.decode('utf-8', errors='replace').strip()
    if text:
        safe_text = text.encode('gbk', errors='replace').decode('gbk')
        elapsed = time.time() - start
        print(f"  [{elapsed:.0f}s] {safe_text}")
proc.wait()
print(f"\nReturn code: {proc.returncode}")

print("\nLocal images:")
result = mgr._run_podman(['images'], timeout=30)
if result:
    print((result.stdout or '').strip())
