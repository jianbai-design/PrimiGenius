"""Remove only the stable/legacy WSL environment owned by this installation.

Called before program removal, in the original user's context. Never shuts down
WSL, removes Windows features, or recursively deletes runtime directories.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from runtime_selection import LEGACY, STABLE, owned_machine, disk_locations, distro_name, _host_path, _same, _within
from wsl_registration import LXSS, _values


def registrations(registry):
    try:
        root = registry.OpenKey(registry.HKEY_CURRENT_USER, LXSS)
    except FileNotFoundError:
        return [], None
    with root:
        try:
            default = registry.QueryValueEx(root, 'DefaultDistribution')
        except FileNotFoundError:
            default = None
        entries = []
        for index in range(registry.QueryInfoKey(root)[0]):
            identifier = registry.EnumKey(root, index)
            with registry.OpenKey(root, identifier) as child:
                values = _values(registry, child)
                entries.append(dict(id=identifier, values=values,
                                    fields={key: value for key, value, _ in values},
                                    subkeys=registry.QueryInfoKey(child)[0]))
        return entries, default


def _plain_path(path):
    """Reject junctions/symlinks, including any existing parent of a missing disk."""
    path = Path(_host_path(path))
    if not _same(path, path.resolve()):
        raise RuntimeError('Linked runtime paths were preserved: ' + str(path))
    return path


def removal_plan(install_dir, entries):
    install = _plain_path(install_dir)
    config = install / 'podman-config' / 'machine-runtime.json'
    saved_bytes = config.read_bytes() if config.is_file() else None
    saved = json.loads(saved_bytes.decode('utf-8-sig')) if saved_bytes is not None else {}
    if not isinstance(saved, dict):
        raise RuntimeError('Invalid runtime selection; no environment was removed')
    machine = saved.get('machine_name')
    if saved and (saved.get('channel') != 'stable' or not (machine == LEGACY or owned_machine(machine, 'stable'))
                  or saved.get('wsl_distro_name') != distro_name(machine)):
        raise RuntimeError('Runtime selection does not identify an owned stable/legacy environment')
    data = saved.get('data_dir') or str(install / 'podman-data')
    if not isinstance(data, str) or not os.path.isabs(data):
        raise RuntimeError('Invalid runtime data directory')
    data = _plain_path(data)
    if data == data.parent or _same(data, install) or data.name.lower() == 'podman-data-dev':
        raise RuntimeError('Unsafe runtime data directory; it was preserved')
    eligible = []
    supported = [LEGACY, STABLE]
    if owned_machine(machine, 'stable') and machine not in supported:
        supported.append(machine)
    for owned in supported:
        name = distro_name(owned)
        matches = [entry for entry in entries if entry['fields'].get('DistributionName') == name]
        if machine and owned != machine:
            continue
        if len(matches) > 1:
            raise RuntimeError('Duplicate WSL registrations were preserved: ' + name)
        if not matches:
            if machine:
                if data.exists() and any(data.rglob('*.vhdx')):
                    raise RuntimeError('Selected WSL registration is unavailable for this Windows user; existing disks were preserved')
                # An already removed environment is a valid retry, but its saved
                # selection must still be prepared for a later reinstall.
                eligible.append(dict(entry=None, base=None, locations=[], data=data,
                                     config=config, saved_bytes=saved_bytes))
            continue
        entry = matches[0]
        fields = entry['fields']
        base = fields.get('BasePath')
        locations = list(disk_locations(data, owned))
        if base:
            if not isinstance(base, str) or not os.path.isabs(base):
                raise RuntimeError('Invalid WSL BasePath; registration was preserved')
            base = _plain_path(base)
            if not any(_same(base, location) for location in locations):
                if machine:
                    raise RuntimeError('Selected WSL storage is outside this installation: ' + str(base))
                continue  # e.g. Podman Desktop's unrelated machine-default.
        elif not machine:
            if owned_machine(owned, 'stable'):
                raise RuntimeError('Cannot attribute a pathless WSL registration without installation metadata')
            continue
        if entry['subkeys'] or fields.get('Version') != 2:
            raise RuntimeError('Unexpected WSL registration structure; it was preserved')
        disk_name = fields.get('VhdFileName', 'ext4.vhdx')
        if not isinstance(disk_name, str) or not disk_name or Path(disk_name).name != disk_name:
            raise RuntimeError('Invalid WSL virtual disk filename')
        if base:
            _plain_path(base / disk_name)
            # No two registrations may share or contain the disk directory.
            for other in entries:
                other_base = other['fields'].get('BasePath')
                if other['id'] != entry['id'] and other_base and (
                        _within(base, other_base) or _within(other_base, base)):
                    raise RuntimeError('WSL storage overlaps another distribution; all environments were preserved')
        eligible.append(dict(entry=entry, base=base, locations=locations, disk_name=disk_name,
                             data=data, config=config, saved_bytes=saved_bytes))
    if len(eligible) > 1:
        raise RuntimeError('Multiple owned environments require an explicit saved selection')
    return eligible[0] if eligible else None


def _prepare_reinstall(plan, archive):
    if plan['saved_bytes'] is None:
        return
    if plan['config'].read_bytes() != plan['saved_bytes']:
        raise RuntimeError('Runtime was removed, but its selection changed; program files were preserved')
    selection_archive = archive.with_name(archive.stem + '-selection.json')
    with selection_archive.open('xb') as handle:
        handle.write(plan['saved_bytes'])
        handle.flush()
        os.fsync(handle.fileno())
    settings = json.loads(plan['saved_bytes'].decode('utf-8-sig'))
    settings['provisioning_pending'] = True
    settings['selection_reason'] = 'runtime removed during uninstall'
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=plan['config'].parent,
                                         prefix='uninstall-selection-', suffix='.tmp', delete=False) as handle:
            pending = Path(handle.name)
            json.dump(settings, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, plan['config'])
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def remove_runtime(install_dir, run=None):
    import winreg
    entries, default = registrations(winreg)
    plan = removal_plan(install_dir, entries)
    if plan is None:
        return 'No registered runtime belongs to this installation.'
    entry = plan['entry']
    if entry is None:
        backup = plan['config'].parent / 'registration-recovery'
        backup.mkdir(parents=True, exist_ok=True)
        _prepare_reinstall(plan, backup / ('uninstall-absent-' + str(time.time_ns()) + '.json'))
        return 'The owned runtime is already unregistered; storage preferences were retained for reinstall.'
    base = plan['base']
    if not base:
        disks = set()
        known = {_plain_path(location / plan['disk_name']) for location in plan['locations']}
        for directory in (plan['data'] / 'machine', plan['data'] / 'containers'):
            if directory.exists():
                for disk in directory.rglob('*.vhdx'):
                    if disk.is_file():
                        disks.add(_plain_path(disk))
        disks.update(disk for disk in known if disk.is_file())
        if len(disks) > 1 or not disks.issubset(known):
            raise RuntimeError('Ambiguous runtime disks were preserved; repair their registration first')
        if disks:
            base = next(iter(disks)).parent
            for other in entries:
                other_base = other['fields'].get('BasePath')
                if other['id'] != entry['id'] and other_base and (
                        _within(base, other_base) or _within(other_base, base)):
                    raise RuntimeError('Recovered storage overlaps another WSL distribution')
        elif Path(str(Path(install_dir)) + '.primigenius-preserved').exists():
            raise RuntimeError('An installation recovery backup exists; restore it before removing the runtime')
    backup = plan['config'].parent / 'registration-recovery'
    backup.mkdir(parents=True, exist_ok=True)
    archive = backup / ('uninstall-' + entry['id'].strip('{}') + '-' + str(time.time_ns()) + '.json')
    with archive.open('x', encoding='utf-8') as handle:
        json.dump(dict(schema=1, action='uninstall-runtime', registration=entry,
                       default_distribution=default), handle, ensure_ascii=False, indent=2,
                  default=lambda value: {'bytes_hex': value.hex()})
        handle.flush()
        os.fsync(handle.fileno())
    # Recheck after the durable backup, before any mutation or WSL command.
    current, current_default = registrations(winreg)
    if current != entries or current_default != default:
        raise RuntimeError('WSL registrations changed during uninstall; retry after other setup completes')
    current_selection = plan['config'].read_bytes() if plan['config'].is_file() else None
    if current_selection != plan['saved_bytes']:
        raise RuntimeError('Runtime selection changed during uninstall; no environment was removed')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LXSS, 0, winreg.KEY_READ | winreg.KEY_WRITE) as root:
        if base:
            if not plan['base']:
                with winreg.OpenKey(root, entry['id'], 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
                    winreg.SetValueEx(key, 'BasePath', 0, winreg.REG_SZ, str(base))
            runner = run or subprocess.run
            system = Path(os.environ['SystemRoot']) / 'System32'
            if os.environ.get('PROCESSOR_ARCHITEW6432'):
                system = Path(os.environ['SystemRoot']) / 'Sysnative'
            result = runner([str(system / 'wsl.exe'), '--unregister', entry['fields']['DistributionName']],
                            capture_output=True, timeout=120,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError('WSL did not finish unregistering the owned distribution; program files were preserved')
        else:
            # A pathless entry without a disk cannot be unregistered by WSL.
            # Remove its entire backed-up metadata key, never just BasePath.
            winreg.DeleteKey(root, entry['id'])
            if default and default[0] == entry['id']:
                if winreg.QueryValueEx(root, 'DefaultDistribution') == default:
                    winreg.DeleteValue(root, 'DefaultDistribution')
    remaining, after_default = registrations(winreg)
    if any(item['id'] == entry['id'] or item['fields'].get('DistributionName') ==
           entry['fields']['DistributionName'] for item in remaining):
        raise RuntimeError('WSL registration removal was incomplete; program files were preserved')
    if after_default and after_default[0] == entry['id']:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LXSS, 0, winreg.KEY_READ | winreg.KEY_WRITE) as root:
            if winreg.QueryValueEx(root, 'DefaultDistribution') == after_default:
                winreg.DeleteValue(root, 'DefaultDistribution')
        remaining, after_default = registrations(winreg)
    others = [item for item in entries if item['id'] != entry['id']]
    if remaining != others or (default and default[0] != entry['id'] and after_default != default):
        raise RuntimeError('Other WSL configuration changed concurrently; inspect it before continuing uninstall')
    # Preserve storage preferences, while explicitly allowing a later reinstall
    # to provision the deliberately removed environment at the same location.
    _prepare_reinstall(plan, archive)
    # WSL versions differ in whether empty import directories remain.
    for directory in sorted(set(plan['locations']), key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()  # Empty directories only; never delete leftover data.
        except (FileNotFoundError, OSError):
            pass
    return 'Removed owned WSL distribution: ' + entry['fields']['DistributionName']


def main(install_dir):
    try:
        detail, code = remove_runtime(install_dir), 0
    except Exception as exc:
        detail, code = str(exc), 2
    # A windowed frozen executable has no console. Keep an actionable result
    # outside program resources, including when cleanup blocks the uninstall.
    destination = Path(install_dir) / 'podman-config' / 'uninstall-result.txt'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(str(code) + '\n' + detail, encoding='utf-8')
    return code
