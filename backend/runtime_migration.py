"""Adopt an existing, format-compatible store without cloning its WSL disk.

Patch releases in the fixed engine's series are migration candidates, not
automatically compatible. The actual database, storage configuration and object
inventory must still pass the fixed CLI's checks before the change is committed.
"""
import hashlib
import json
import os
import re
import shlex
import tarfile


PENDING = 'PRIMIGENIUS_MIGRATION_PENDING'
TRANSACTION = '/usr/libexec/primigenius/.engine-migration'
VERSION_COMMAND = (
    'if [ -e ' + TRANSACTION + ' ]; then printf "' + PENDING + '\\n"; '
    'else /usr/bin/podman --version; fi'
)


class MigrationError(RuntimeError):
    """An actionable conflict that automatic reconnect cannot resolve."""


# Only these files can be changed. All snapshots of image/container/volume IDs
# live in /run (RAM); the existing image store, volumes and config stay in place.
MIGRATE_SCRIPT = r'''
set -euE
step=preflight
trap 'echo "Runtime migration failed during $step (exit $?)" >&2' ERR
action=$1; archive=$2; member=$3; digest=$4; target=$5; static=$6; database=$7
txn=/usr/libexec/primigenius/.engine-migration
mkdir -p /run/primigenius /usr/libexec/primigenius
exec 9>/run/primigenius/engine-migration.lock
flock -n 9 || { echo 'Another runtime migration is in progress' >&2; exit 75; }
ram=''
idle() {
    for p in /proc/[0-9]*/exe; do
        exe=$(readlink "$p" 2>/dev/null) || continue
        case "$exe" in
            */podman|*/podman\ \(deleted\)|*/primigenius-engine|*/primigenius-engine\ \(deleted\)|*/conmon)
                echo "Close the previous runtime first; a container engine or task is still active: $exe" >&2
                return 1;;
        esac
    done
}
owned() {
    [ ! -L "$txn" ] && [ -f "$txn/owner" ] &&
        [ "$(cat "$txn/owner")" = 'PrimiGenius runtime migration v1' ]
}
clean() {
    owned || { echo 'Unrecognized migration directory was preserved' >&2; return 1; }
    for f in staticdir database old new db.sql db.sql-wal db.sql-shm db.sql-journal bolt_state.db; do rm -f -- "$txn/$f"; done
    rm -f -- "$txn/phase" "$txn/owner"
    rmdir -- "$txn"
}
recover() {
    [ -e "$txn" ] || return 0
    if [ ! -L "$txn" ] && rmdir -- "$txn" 2>/dev/null; then return 0; fi
    owned || { echo 'Unrecognized migration directory was preserved' >&2; return 1; }
    phase=$(cat "$txn/phase" 2>/dev/null || true)
    if [ "$phase" = committed ] || [ "$phase" = preparing ]; then clean; return; fi
    [ "$phase" = prepared ] || { echo 'Incomplete migration journal was preserved for recovery' >&2; return 1; }
    idle || return 1
    saved=$(cat "$txn/staticdir")
    saved_db=$(cat "$txn/database")
    case "$saved_db" in db.sql|bolt_state.db) ;; *) return 1;; esac
    case "$saved" in /*) ;; *) echo 'Invalid migration database path' >&2; return 1;; esac
    [ "$saved" != / ] && [ -d "$saved" ] || return 1
    [ ! -L /usr/bin/podman ] || return 1
    if [ -f "$txn/old" ]; then mv -f -- "$txn/old" /usr/bin/podman; fi
    for f in db.sql db.sql-wal db.sql-shm db.sql-journal bolt_state.db; do
        if [ -f "$txn/$f" ]; then
            cp -p -- "$txn/$f" "$saved/$f.primigenius-restore"
            mv -f -- "$saved/$f.primigenius-restore" "$saved/$f"
        else
            [ "$f" != "$saved_db" ] || { echo 'Migration database backup is missing' >&2; return 1; }
            rm -f -- "$saved/$f"
        fi
    done
    sync -f "$saved"
    printf 'committed\n' > "$txn/phase"
    clean
}
finish() {
    status=$?; trap - EXIT HUP INT TERM
    if [ "$status" -ne 0 ]; then
        if ! recover; then echo 'Migration recovery is pending; existing storage and recovery files were preserved' >&2; fi
    fi
    if [ -n "$ram" ]; then
        for f in before after; do rm -f -- "$ram/$f"; done
        rmdir -- "$ram"
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' HUP INT TERM
recover
if [ "$action" = recover ]; then /usr/bin/podman --version; exit 0; fi
idle
[ -f /usr/bin/podman ] && [ ! -L /usr/bin/podman ] || { echo 'Unsupported Linux CLI installation was preserved' >&2; exit 1; }
[ "$(command -v podman)" -ef /usr/bin/podman ] || { echo 'A different podman executable is selected by PATH' >&2; exit 1; }
case "$database" in db.sql|bolt_state.db) ;; *) exit 1;; esac
[ -f "$static/$database" ] && [ ! -L "$static/$database" ] || { echo 'Existing database was not found; storage was preserved' >&2; exit 1; }
ram=$(mktemp -d /run/primigenius/migration.XXXXXX)
snapshot() {
    /usr/bin/podman info --format '{{.Host.DatabaseBackend}}|{{.Store.GraphRoot}}|{{.Store.RunRoot}}|{{.Store.GraphDriverName}}|{{.Store.VolumePath}}'
    printf '\nIMAGES\n'; /usr/bin/podman images --no-trunc --format '{{.ID}}' | LC_ALL=C sort -u
    printf '\nCONTAINERS\n'; /usr/bin/podman ps -a --no-trunc --format '{{.ID}}' | LC_ALL=C sort
    printf '\nVOLUMES\n'; /usr/bin/podman volume ls --format '{{.Name}}' | LC_ALL=C sort
    printf '\nNETWORKS\n'; /usr/bin/podman network ls --format '{{.ID}}|{{.Name}}' | LC_ALL=C sort
}
# pipefail is required: a failed podman listing must never look like an empty store.
set -o pipefail
[ -z "$(/usr/bin/podman ps -a --format '{{.State}}' | grep -Ev '^(created|configured|exited|stopped)$' || [ "$?" = 1 ])" ] || {
    echo 'Running or paused containers were preserved; finish the tasks before migration' >&2; exit 1;
}
step=inventory
snapshot > "$ram/before"
idle
umask 077
step=backup
mkdir "$txn"
printf 'PrimiGenius runtime migration v1\n' > "$txn/owner"
printf 'preparing\n' > "$txn/phase"
printf '%s\n' "$static" > "$txn/staticdir"
printf '%s\n' "$database" > "$txn/database"
for f in db.sql db.sql-wal db.sql-shm db.sql-journal bolt_state.db; do
    if [ -e "$static/$f" ]; then
        [ -f "$static/$f" ] && [ ! -L "$static/$f" ] || exit 1
        cp -p -- "$static/$f" "$txn/$f"
    fi
done
# Stream only the pinned CLI out of the already installed, verified archive.
# No expanded rootfs, second image store, or permanent backup is created.
step=extraction
tar -xOzf "$archive" -- "$member" > "$txn/new"
printf '%s  %s\n' "$digest" "$txn/new" | sha256sum -c - >/dev/null
step=verification
chmod 755 "$txn/new"
staged_version=$("$txn/new" --version)
[ "${staged_version##* }" = "$target" ] || { echo 'Staged Linux CLI has the wrong fixed version' >&2; exit 1; }
[ "$(stat -c %d "$txn/new")" = "$(stat -c %d /usr/bin/podman)" ] || exit 1
step=replacement
idle
ln -- /usr/bin/podman "$txn/old"
printf 'prepared\n' > "$txn/phase"
sync -f "$txn"
mv -f -- "$txn/new" /usr/bin/podman
step=validation
snapshot > "$ram/after"
before_digest=$(sha256sum < "$ram/before")
after_digest=$(sha256sum < "$ram/after")
[ "$before_digest" = "$after_digest" ] || { echo 'Existing storage inventory changed; restoring the previous engine' >&2; exit 1; }
printf '%s  /usr/bin/podman\n' "$digest" | sha256sum -c - >/dev/null
printf 'committed\n' > "$txn/phase"
sync -f "$txn"
step=cleanup
clean
printf 'PrimiGenius fixed engine %s adopted the existing store; migration files removed\n' "$target"
'''


def _run(runtime, command, timeout=120):
    result = runtime.manager._run_wsl(command, timeout=timeout)
    if result is None or result.returncode:
        detail = (result.stderr or result.stdout) if result is not None else 'WSL migration command timed out'
        raise MigrationError(str(detail).strip() + ' No WSL disk or image store was reset or recreated.')
    return result


def _command(action, archive='', member='', digest='', target='', static='', database=''):
    return '/bin/bash -c ' + shlex.quote(MIGRATE_SCRIPT) + ' primigenius-migrate ' + ' '.join(
        shlex.quote(value) for value in (action, archive, member, digest, target, static, database))


def recover_pending(runtime):
    """Restore a fully prepared transaction, or finish already committed cleanup."""
    return _run(runtime, _command('recover')).stdout.strip().split()[-1]


def _pinned_cli(runtime, manifest):
    archive = os.path.join(runtime.bundle, 'rootfs.tar.gz')
    expected = manifest.get('files', {}).get('rootfs.tar.gz')
    if not expected:
        raise MigrationError('The fixed runtime archive has no checksum')
    with open(archive, 'rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise MigrationError('The fixed runtime archive checksum does not match; existing engine was preserved')
    with tarfile.open(archive, mode='r|gz') as source:
        for entry in source:
            if entry.name.removeprefix('./') == 'usr/bin/podman':
                if not entry.isfile():
                    raise MigrationError('The fixed Linux CLI must be a regular file')
                with source.extractfile(entry) as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                return runtime.manager.host_path_to_wsl(archive), entry.name, digest
    raise MigrationError('The fixed Linux CLI is missing from the runtime archive')


def migrate_existing(runtime, installed, manifest):
    target = manifest['podman_version']
    # Support patch-level upgrades and downgrades in the bundled release series,
    # including older installations. A different series needs a separate format
    # migration, never an implicit downgrade or replacement of the data disk.
    source_version = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', installed)
    target_version = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', target)
    if not source_version or not target_version or source_version.groups()[:2] != target_version.groups()[:2]:
        raise MigrationError('Existing Linux engine %s cannot yet be migrated to fixed engine %s; existing data was preserved' % (installed, target))
    archive, member, digest = _pinned_cli(runtime, manifest)
    probe = _run(runtime, '/usr/bin/podman --log-level=debug info --format=json', timeout=30)
    info = json.loads(probe.stdout)
    backend = info.get('host', {}).get('databaseBackend')
    if backend not in ('sqlite', 'boltdb'):
        raise MigrationError('The existing database backend is unsupported; existing data was preserved')
    static = None
    for line in (probe.stderr or '').splitlines():
        match = re.search(r'\bmsg=("(?:[^"\\]|\\.)*")', line)
        if not match:
            continue
        message = json.loads(match.group(1))
        if message.startswith('Using static dir '):
            static = message[len('Using static dir '):]
        override = re.fullmatch(r'Overriding static dir "(?:[^"\\]|\\.)*" with ("(?:[^"\\]|\\.)*") from database', message)
        if override:
            static = json.loads(override.group(1))
    if not static or not static.startswith('/') or static == '/' or any(c in static for c in '\r\n\0'):
        raise MigrationError('The existing database location could not be verified; existing data was preserved')
    database = 'db.sql' if backend == 'sqlite' else 'bolt_state.db'
    return _run(runtime, _command('migrate', archive, member, digest, target, static, database)).stdout.strip()
