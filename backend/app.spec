# -*- mode: python ; coding: utf-8 -*-

import os

_spec_file = globals().get('__file__', 'app.spec')
_spec_dir = os.path.abspath(os.path.dirname(_spec_file)) if os.path.dirname(_spec_file) else os.getcwd()
_icon_path = os.path.abspath(os.path.join(_spec_dir, '..', 'build', 'icon.ico'))
_icon_arg = _icon_path if os.path.exists(_icon_path) else None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'docker',
        'docker.api',
        'docker.api.client',
        'docker.api.container',
        'docker.api.image',
        'docker.api.plugin',
        'docker.api.secret',
        'docker.api.service',
        'docker.api.swarm',
        'docker.api.volume',
        'docker.auth',
        'docker.credentials',
        'docker.errors',
        'docker.models',
        'docker.models.containers',
        'docker.models.images',
        'docker.models.networks',
        'docker.models.nodes',
        'docker.models.plugins',
        'docker.models.resource',
        'docker.models.secrets',
        'docker.models.services',
        'docker.models.swarm',
        'docker.models.volumes',
        'docker.transport',
        'docker.utils',
        'docker.utils.build',
        'docker.utils.config',
        'docker.utils.decorators',
        'docker.utils.fnmatch',
        'docker.utils.json_stream',
        'docker.utils.proxy',
        'docker.utils.socket',
        'win32file',
        'win32pipe',
        'win32event',
        'win32api',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='app',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=_icon_arg,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='app',
)
