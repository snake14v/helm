# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['helm_app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['server', 'aiop', 'ablit', 'router', 'bridge', 'events', 'guardian', 'model_rank', 'llm_providers', 'apihealth', 'budget', 'providers', 'approvals', 'terminals', 'models', 'logs', 'taskhealth', 'nba', 'doctor', 'agents_hq'],
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
    a.binaries,
    a.datas,
    [],
    name='GlassPanel',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
