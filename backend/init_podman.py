"""Initialize or diagnose the owned environment without deleting existing data."""
from podman_manager import PodmanManager


def main():
    manager = PodmanManager()
    ok, message = manager.auto_setup()
    print(message)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
