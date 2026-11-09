"""Use the application's pinned R runtime and normal dependency workflow."""
from app import _get_podman_manager, get_docker_client, _ensure_pinned_r_base_image


if __name__ == '__main__':
    ok, message = _get_podman_manager().auto_setup()
    if not ok:
        raise SystemExit(message)
    _ensure_pinned_r_base_image(get_docker_client(), progress_cb=print)
