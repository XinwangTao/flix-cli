# -*- mode: python ; coding: utf-8 -*-

# pywinpty ships native DLLs, so they have to be collected explicitly. A missing
# backend must not break the build: exec-only still works without PTY support.
_winpty_datas, _winpty_binaries, _winpty_hidden = [], [], []
try:
    from PyInstaller.utils.hooks import collect_all
    _winpty_datas, _winpty_binaries, _winpty_hidden = collect_all('winpty')
except Exception as _exc:  # noqa: BLE001 - build-time best effort
    print(f"[spec] winpty not collected: {_exc}")


a = Analysis(
    ['scripts/launch_relaydesk.py'],
    pathex=['src'],
    binaries=_winpty_binaries,
    datas=_winpty_datas,
    hiddenimports=['flixcli.cli', 'flixcli.mcp_server', 'flixcli.daemon',
                   'flixcli.ptyshell', 'flixcli.autoupdate'] + _winpty_hidden,
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
    name='RelayDesk',
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
    icon=['assets/relaydesk.ico'],
)
