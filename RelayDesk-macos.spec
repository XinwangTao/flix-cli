# -*- mode: python ; coding: utf-8 -*-
"""macOS bundle for RelayDesk (onedir + .app).

Build on macOS:  pyinstaller RelayDesk-macos.spec --noconfirm --clean
Artifact:        dist/RelayDesk.app

onedir is intentional: a onefile .app unpacks ~110 MB to /tmp on every launch
and macOS Gatekeeper scans it each time, which made startup visibly slow.
"""

import tomllib
from pathlib import Path

# pyproject.toml is the single source of truth for the version: the bundle used
# to carry a hard-coded string that drifted from the tag being released.
_APP_VERSION = tomllib.loads(
    Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


# The frozen app has no pyproject.toml to read, so `flix --version`, `flix self`
# and MCP serverInfo would report 0.0.0+source. Bundle the installed package
# metadata (copy_metadata) so importlib.metadata resolves the real version.
try:
    from PyInstaller.utils.hooks import copy_metadata as _copy_metadata
    _version_datas = _copy_metadata('flixcli')
except Exception as _exc:  # noqa: BLE001 - build-time best effort
    print(f"[spec] flixcli metadata not bundled: {_exc}")
    _version_datas = []

a = Analysis(
    ['scripts/launch_relaydesk.py'],
    pathex=['src'],
    binaries=[],
    datas=_version_datas,
    hiddenimports=['flixcli.cli', 'flixcli.mcp_server', 'flixcli.daemon',
                   'flixcli.ptyshell'],
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
    name='RelayDesk',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/relaydesk.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='RelayDesk',
)
app = BUNDLE(
    coll,
    name='RelayDesk.app',
    icon='assets/relaydesk.icns',
    bundle_identifier='com.xinwangtao.relaydesk',
    info_plist={
        'CFBundleName': 'RelayDesk',
        'CFBundleDisplayName': 'RelayDesk',
        'CFBundleShortVersionString': _APP_VERSION,
        'CFBundleVersion': _APP_VERSION,
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        # macOS 14+ asks before an app touches the local network; the daemon
        # needs UDP multicast + LAN TCP for discovery and pairing.
        'NSLocalNetworkUsageDescription': 'RelayDesk 需要访问局域网，以发现并连接已配对的设备。',
    },
)
