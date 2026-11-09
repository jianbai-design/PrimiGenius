"""Read-only WSL/storage discovery before choosing or provisioning a runtime.

Registry names alone do not establish ownership of a global Podman machine.
No distribution, disk, registry value or machine configuration is changed here.
"""
import json
import os
from pathlib import Path
import platform
import re

LEGACY = 'podman-machine-default'
STABLE = 'primigenius-stable'
DEV = 'primigenius-dev'
LXSS = r'Software\Microsoft\Windows\CurrentVersion\Lxss'


def read_registrations():
    if platform.system() != 'Windows':
        return []
    import winreg
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, LXSS)
    except FileNotFoundError:
        return []
    result = []
    with root:
        for index in range(winreg.QueryInfoKey(root)[0]):
            with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                def value(name, default=None):
                    try:
                        return winreg.QueryValueEx(key, name)[0]
                    except FileNotFoundError:
                        return default
                result.append(dict(name=value('DistributionName'), base=value('BasePath'),
                                   disk_name=value('VhdFileName', 'ext4.vhdx'), version=value('Version', 2)))
    return result


def distro_name(machine):
    return machine if machine == LEGACY else 'podman-' + machine


def owned_machine(machine, channel):
    prefix = DEV if channel == 'dev' else STABLE
    return isinstance(machine, str) and (machine == prefix or
        re.fullmatch(re.escape(prefix) + r'-(?:[2-9]|[1-9][0-9]+)', machine) is not None)


def disk_locations(data_root, machine):
    """All known old Podman and current embedded import layouts, without scanning drives."""
    names = set((machine, distro_name(machine)))
    for store in (Path(data_root) / 'machine' / 'wsldist',
                  Path(data_root) / 'containers' / 'podman' / 'machine' / 'wsl' / 'wsldist'):
        yield store
        for name in sorted(names):
            yield store / name
    for name in sorted(names):
        yield Path(data_root) / 'containers' / 'podman' / 'machine' / 'wsl' / name


def _host_path(value):
    value = str(value)
    # WSL registrations may use extended-length Windows paths for ordinary drives.
    if os.name == 'nt' and value.startswith('\\\\?\\'):
        value = ('\\\\' + value[8:]) if value[4:8].upper() == 'UNC\\' else value[4:]
    return os.path.abspath(value)


def _same(left, right):
    return os.path.normcase(_host_path(left)) == os.path.normcase(_host_path(right))


def _within(path, root):
    try:
        return os.path.commonpath((os.path.normcase(_host_path(path)),
                                   os.path.normcase(_host_path(root)))) == os.path.normcase(_host_path(root))
    except ValueError:
        return False


def _registered_root(base, machine, known_root):
    if not base or not os.path.isabs(base):
        return None
    base = Path(_host_path(base))
    if _within(base, known_root):
        return Path(known_root)
    # A registered old installation can be elsewhere after an application upgrade.
    # Require its exact storage layout plus app-specific ownership evidence.
    for root in base.parents:
        if not any(_same(base, candidate) for candidate in disk_locations(root, machine)):
            continue
        config = root.parent / ('podman-config-dev' if machine == DEV else 'podman-config')
        if owned_machine(machine, 'stable') or owned_machine(machine, 'dev'):
            return root  # These WSL names belong exclusively to PrimiGenius.
        if root.name.lower() == 'podman-data' and config.is_dir():
            return root
        metadata = config / 'machine-runtime.json'
        if metadata.is_file():
            try:
                saved = json.loads(metadata.read_text(encoding='utf-8-sig'))
                if saved.get('machine_name') == machine and saved.get('data_dir') and _same(saved['data_dir'], root):
                    return root
            except (OSError, ValueError, TypeError):
                pass
    return None


def _disk_status(path):
    try:
        stat = path.stat()
        if not path.is_file():
            return 'unavailable'
        return 'ready' if stat.st_size > 0 else 'empty'
    except (FileNotFoundError, NotADirectoryError):
        return 'missing'
    except OSError:
        return 'inaccessible'
    # An inaccessible selected disk is not an absent installation.


def _unused_runtime_root(data_root, machine):
    base = Path(data_root) / 'runtimes' / machine
    fresh_root, index = base, 2
    while fresh_root.exists() and any(fresh_root.iterdir()):
        fresh_root = base.with_name(base.name + '-' + str(index))
        index += 1
    return fresh_root


def select_runtime(channel, data_root, config_path, registrations=None):
    """Return a selection plan; a non-empty error prohibits setup and config writes."""
    data_root = Path(_host_path(data_root))
    preferred = DEV if channel == 'dev' else STABLE
    plan = dict(machine=preferred, data_dir=str(data_root), reason='new ' + channel + ' install', error='', pending=True)
    try:
        config = Path(config_path)
        saved = json.loads(config.read_text(encoding='utf-8-sig')) if config.is_file() else {}
        if not isinstance(saved, dict):
            raise ValueError('Runtime selection configuration must be an object: ' + str(config))
        saved = saved if saved.get('channel') == channel else {}
        persisted = saved.get('machine_name')
        supported = [DEV] if channel == 'dev' else [LEGACY, STABLE]
        if not (owned_machine(persisted, channel) or (channel == 'stable' and persisted == LEGACY)):
            persisted = None
        if persisted:
            preferred = persisted
            plan.update(machine=preferred, reason='persisted ' + channel + ' selection')
        entries = read_registrations() if registrations is None else registrations
        for name in [persisted] + [str(entry.get('name') or '').removeprefix('podman-') for entry in entries]:
            if owned_machine(name, channel) and name not in supported:
                supported.append(name)
        candidates = {}
        unregistered = []
        for machine in supported:
            matches = [entry for entry in entries if entry.get('name') == distro_name(machine)]
            if len(matches) > 1:
                raise ValueError('Multiple WSL registrations use ' + distro_name(machine) + '; all registrations were preserved')
            entry = matches[0] if matches else None
            disk_name = entry.get('disk_name', 'ext4.vhdx') if entry else 'ext4.vhdx'
            if not isinstance(disk_name, str) or not disk_name or Path(disk_name).name != disk_name:
                raise ValueError('Invalid WSL disk filename for ' + distro_name(machine))
            base = entry.get('base') if entry else None
            root = _registered_root(base, machine, data_root) if base else None
            if entry and machine != LEGACY and not root:
                root = data_root
            # Recognize local legacy disks even when registration metadata is broken.
            local = []
            for directory in disk_locations(data_root, machine):
                disk = directory / disk_name
                if _disk_status(disk) in ('ready', 'empty'):
                    local.append(disk)
            if entry and (root or (persisted == machine) or local):
                if base and not root:
                    # A saved legacy selection can refer to another installation
                    # or Podman Desktop. Preserve it and provision our own name.
                    unregistered.extend(local)
                    continue
                root = root or data_root
                disk = Path(base) / disk_name if base else None
                status = _disk_status(disk) if disk else 'missing-base'
                if entry.get('version', 2) != 2:
                    status = 'unsupported-wsl-version'
                candidates[machine] = dict(root=root, disk=disk, status=status, registered=True, local=local)
            elif local:
                unregistered.extend(local)
        selected = candidates.get(preferred)
        if selected and selected['status'] == 'ready':
            plan.update(data_dir=str(selected['root']), reason='verified existing ' + preferred, pending=False)
            return plan
        if selected and selected['status'] in ('inaccessible', 'unavailable', 'unsupported-wsl-version', 'empty'):
            raise ValueError('The selected runtime storage is ' + selected['status'] + ': ' + str(selected['disk']) +
                             '. Its selection was preserved; no alternate environment was opened.')
        # A complete, owned legacy environment takes precedence over provisioning
        # a fresh stable environment or retaining its broken persisted selection.
        alternatives = [name for name in supported if name != preferred and
                        name in candidates and candidates[name]['status'] == 'ready']
        if len(alternatives) == 1:
            name = alternatives[0]
            plan.update(machine=name, data_dir=str(candidates[name]['root']),
                        reason='recovered existing ' + name + ' (previous selection: ' + preferred + ')', pending=False)
            return plan
        if selected and selected['status'] == 'missing-base':
            # Reuse the existing backed-up metadata repair when possible. An
            # orphan with no disk may now provision a new environment directly.
            pending = not selected['local']
            root = selected['root']
            target = root / 'machine' / 'wsldist'
            if pending and target.exists() and any(target.iterdir()):
                root = _unused_runtime_root(root, preferred)
            plan.update(data_dir=str(root), reason='existing registration needs BasePath recovery',
                        pending=pending, fresh=pending)
            return plan
        preserved = Path(config_path).parent.parent.with_name(Path(config_path).parent.parent.name + '.primigenius-preserved')
        canonical = DEV if channel == 'dev' else STABLE
        used = {str(entry.get('name') or '').casefold() for entry in entries}
        machine = persisted if owned_machine(persisted, channel) and saved.get('provisioning_pending') is True else canonical
        suffix = 2
        while distro_name(machine).casefold() in used:
            machine = canonical + '-' + str(suffix)
            suffix += 1
        # If an old removable drive is absent, provision next to this install.
        if not Path(data_root.anchor).exists():
            data_root = config.parent.parent / ('podman-data-dev' if channel == 'dev' else 'podman-data')
        target = data_root / 'machine' / 'wsldist'
        pending_same = saved.get('provisioning_pending') is True and machine == persisted
        if ((machine != canonical or preserved.exists()) and not pending_same) or unregistered or (target.exists() and any(target.iterdir())):
            # Do not overwrite an orphaned or partially imported disk.
            data_root = _unused_runtime_root(data_root, machine)
        fresh = bool(candidates or unregistered or preserved.exists() or
                     (persisted and saved.get('provisioning_pending') is not True))
        plan.update(machine=machine, data_dir=str(data_root), pending=True, fresh=fresh,
                    reason=('Previous runtime unavailable; automatically creating ' if fresh else 'new ' + channel + ' install: ') + machine)
        return plan
    except (OSError, ValueError, TypeError) as exc:
        plan['error'] = 'Runtime storage discovery needs attention: ' + str(exc)
        return plan
