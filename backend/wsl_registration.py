"""Recover only an owned WSL2 registration whose BasePath is absent.

Existing disk references are never replaced. Registry-only recovery is archived
before changes; no disk, distribution directory or unrelated registration is removed.
"""
import json
import os
from pathlib import Path
import time

LXSS = r'Software\Microsoft\Windows\CurrentVersion\Lxss'


def _values(registry, key):
    return [registry.EnumValue(key, index) for index in range(registry.QueryInfoKey(key)[1])]


def repair_registration(name, data_dir, config_dir, allow_orphan_replacement=False):
    import winreg
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, LXSS, 0, winreg.KEY_READ | winreg.KEY_WRITE)
    except FileNotFoundError:
        return None
    with root:
        matches = []
        for index in range(winreg.QueryInfoKey(root)[0]):
            child_name = winreg.EnumKey(root, index)
            with winreg.OpenKey(root, child_name) as child:
                values = _values(winreg, child)
            fields = {key: value for key, value, _ in values}
            if fields.get('DistributionName') == name:
                matches.append((child_name, values, fields))
        if not matches:
            return None
        if len(matches) != 1:
            raise RuntimeError('Multiple WSL registrations use the owned name; preserve them for recovery: ' + name)
        child_name, values, fields = matches[0]
        if fields.get('BasePath'):
            return None
        if fields.get('Version') != 2:
            raise RuntimeError('The owned WSL registration has no BasePath and is not a verified WSL2 entry: ' + name)
        disk_name = fields.get('VhdFileName', 'ext4.vhdx')
        if not isinstance(disk_name, str) or not disk_name or Path(disk_name).name != disk_name:
            raise RuntimeError('Invalid WSL virtual disk name; registration was not modified')
        data = Path(data_dir).resolve()
        machine = name.removeprefix('podman-')
        roots = [data / 'machine' / 'wsldist', data / 'containers' / 'podman' / 'machine' / 'wsl' / 'wsldist']
        expected = {directory / disk_name for store in roots
                    for directory in (store, store / machine, store / name)}
        disks = {path.resolve() for path in expected if path.is_file()}
        for store in roots:
            if store.exists():
                for path in store.rglob('*.vhdx'):
                    if path.is_file():
                        disks.add(path.resolve())
        if any(not path.is_relative_to(data) for path in disks):
            raise RuntimeError('WSL disk resolves outside the owned data directory; registration was not modified')
        known = {path.resolve() for path in expected}
        if len(disks) > 1 or (disks and not disks.issubset(known)):
            raise RuntimeError('Existing WSL disks require manual path recovery; all disks and registration were retained')
        # An interrupted older installer may still hold the original disk here.
        preserved = Path(config_dir).resolve().parent.with_name(Path(config_dir).resolve().parent.name + '.primigenius-preserved')
        if not disks and preserved.exists() and not allow_orphan_replacement:
            raise RuntimeError('Restore the preserved installation before repairing WSL: ' + str(preserved))
        try:
            default, default_type = winreg.QueryValueEx(root, 'DefaultDistribution')
        except FileNotFoundError:
            default, default_type = None, None
        with winreg.OpenKey(root, child_name, 0, winreg.KEY_READ | winreg.KEY_WRITE) as child:
            if winreg.QueryInfoKey(child)[0]:
                raise RuntimeError('WSL registration contains additional subkeys; it was retained for recovery')
            if dict((key, (value, kind)) for key, value, kind in _values(winreg, child)) != dict(
                    (key, (value, kind)) for key, value, kind in values):
                raise RuntimeError('WSL registration changed during recovery; retry after setup completes')
            archive_dir = Path(config_dir) / 'registration-recovery'
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive = archive_dir / ('wsl-' + child_name.strip('{}') + '-' + str(time.time_ns()) + '.json')
            snapshot = dict(schema=1, registry_key=LXSS + '\\' + child_name, distribution=name,
                            values=values, default_distribution=default, default_type=default_type,
                            action='restore-base-path' if disks else 'quarantine-orphan')
            with archive.open('x', encoding='utf-8') as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2,
                          default=lambda value: {'bytes_hex': value.hex()})
                handle.flush()
                os.fsync(handle.fileno())
            if _values(winreg, child) != values:
                raise RuntimeError('WSL registration changed while its backup was written; no repair was applied')
            if disks:
                winreg.SetValueEx(child, 'BasePath', 0, winreg.REG_SZ, str(next(iter(disks)).parent))
                return dict(action='restore-base-path', archive=str(archive), was_default=False)
        # Only this malformed metadata key is removed, after its durable backup.
        # Unlike `wsl --unregister`, DeleteKey cannot delete a virtual disk.
        winreg.DeleteKey(root, child_name)
        was_default = default == child_name
        if was_default:
            current, _ = winreg.QueryValueEx(root, 'DefaultDistribution')
            was_default = current == child_name
            if was_default:
                winreg.DeleteValue(root, 'DefaultDistribution')
        return dict(action='quarantine-orphan', archive=str(archive), was_default=was_default)
