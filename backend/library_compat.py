"""Reuse a proven-compatible existing R library in place, without copying it."""
import json
import os
import re


def _packages(path):
    if not os.path.isdir(path):
        return []
    try:
        with os.scandir(path) as entries:
            return [entry.path for entry in entries if entry.is_dir() and not entry.name.startswith(('00LOCK','.'))
                    and os.path.isfile(os.path.join(entry.path, 'DESCRIPTION'))]
    except OSError:
        return []


def resolve_library(primary, legacy, state_file, r_version, bioconductor_version):
    try:
        with open(state_file, encoding='utf-8') as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    # An already populated generation always wins. No merging or overwriting.
    if _packages(primary):
        return primary
    family = '.'.join(r_version.split('.')[:2])
    if state.get('bioconductor_version') and state['bioconductor_version'] != bioconductor_version:
        return primary
    recorded = str(state.get('r_version') or '') == r_version and str(state.get('bioconductor_version') or '') == bioconductor_version
    candidates = list(legacy)
    saved = state.get('library_path')
    if recorded and isinstance(saved, str) and os.path.isabs(saved):
        candidates.insert(0, saved)
    for candidate in dict.fromkeys(candidates):
        packages = _packages(candidate)
        if not packages:
            continue
        compatible = True
        for package in packages:
            try:
                with open(os.path.join(package, 'DESCRIPTION'), encoding='utf-8', errors='replace') as handle:
                    description = handle.read(128 * 1024)
            except OSError:
                compatible = False
                break
            built = re.search(r'^Built:\s*R\s+(\d+\.\d+)(?:\.\d+)?\s*;\s*([^;\r\n]+)', description, re.M)
            if built:
                if built[1] != family or built[2].strip() != 'x86_64-pc-linux-gnu':
                    compatible = False
                    break
            elif not recorded:
                compatible = False
                break
            if os.path.basename(package).lower() == 'biocversion':
                version = re.search(r'^Version:\s*(\d+\.\d+)', description, re.M)
                if not version or version[1] != bioconductor_version:
                    compatible = False
                    break
        if compatible:
            return candidate
    # Different/unknown ABIs remain untouched and are never loaded into a new R.
    return primary
