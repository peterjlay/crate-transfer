import sys
from PyInstaller.utils.hooks import copy_metadata

analysis = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=copy_metadata("pyrekordbox") + copy_metadata("sqlcipher3-wheels"),
    hiddenimports=["sqlalchemy.dialects.sqlite.pysqlcipher", "sqlcipher3.dbapi2"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest"],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Crate Transfer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/CrateTransfer.ico" if sys.platform == "win32" else None,
)
collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="Crate Transfer",
)
if sys.platform == "darwin":
    application = BUNDLE(
        collection,
        name="Crate Transfer.app",
        bundle_identifier="app.cratetransfer.desktop",
        version="0.2.0",
        icon="assets/CrateTransfer.icns",
        info_plist={
            "NSHighResolutionCapable": True,
            "NSLocalNetworkUsageDescription": "Discover paired computers and transfer selected DJ playlists, crates and music over your local network.",
            "NSBonjourServices": ["_cratetransfer._tcp"],
            "LSMinimumSystemVersion": "12.0",
        },
    )
