import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from podman_manager import PodmanManager, _run_hidden


def _print_result(label, result):
    if result:
        print(f"  {label}: {result.returncode == 0}")
        if result.stdout:
            print(f"    stdout: {result.stdout.strip()}")
        if result.stderr:
            print(f"    stderr: {result.stderr.strip()}")
    else:
        print(f"  {label}: failed (hidden runner returned None)")


def main():
    mgr = PodmanManager()
    print(f"podman_exe: {mgr.podman_exe}")
    print(f"config_dir: {mgr.podman_config_dir}")
    print(f"data_dir: {mgr.podman_data_dir}")

    env = mgr._build_podman_env(include_docker_host=False)

    print("\nForce removing Podman Machine...")
    try:
        result = _run_hidden(
            [mgr.podman_exe, 'machine', 'rm', '-f', 'podman-machine-default'],
            timeout=30,
            capture=True,
            env=env,
        )
        _print_result('Machine rm', result)
    except Exception as e:
        print(f"  Machine rm exception: {e}")

    print("\nUnregistering WSL distro...")
    try:
        result = _run_hidden(['wsl', '--unregister', 'podman-machine-default'], timeout=30, capture=True)
        _print_result('WSL unregister', result)
    except Exception as e:
        print(f"  WSL unregister: {e}")

    machine_dir = os.path.join(mgr.podman_data_dir, 'machine')
    if os.path.exists(machine_dir):
        print(f"\nCleaning up {machine_dir}...")
        try:
            shutil.rmtree(machine_dir)
            print("  Deleted successfully")
        except Exception as e:
            print(f"  Delete failed: {e}")

    print("\nCleaning up connections...")
    for conn_name in ['podman-machine-default', 'podman-machine-default-root']:
        try:
            result = _run_hidden(
                [mgr.podman_exe, 'system', 'connection', 'rm', conn_name],
                timeout=10,
                capture=True,
                env=env,
            )
            _print_result(f"Remove {conn_name}", result)
        except Exception as e:
            print(f"  Remove {conn_name}: {e}")

    print("\nInitializing Podman Machine...")
    ok, msg = mgr.machine_init(cpus=4, memory=8192, disk_size=100)
    print(f"Init result: {ok}")
    if not ok:
        print(f"  Error: {msg}")

    if ok:
        print("\nStarting Podman Machine...")
        ok2, msg2 = mgr.machine_start()
        print(f"Start result: {ok2}")
        if not ok2:
            print(f"  Error: {msg2}")

    print(f"\nEngine ready: {mgr.is_engine_ready()}")
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
