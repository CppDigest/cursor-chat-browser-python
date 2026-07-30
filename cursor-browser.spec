# PyInstaller spec for Cursor Chat Browser (--onedir mode).
# Build:  pyinstaller cursor-browser.spec
# Output: dist/CursorChatBrowser/CursorChatBrowser.exe

import sys
from pathlib import Path

block_cipher = None
src = Path(SPECPATH)

a = Analysis(
    [str(src / "launcher.py")],
    pathex=[str(src)],
    binaries=[],
    datas=[
        (str(src / "templates"), "templates"),
        (str(src / "static"), "static"),
    ],
    hiddenimports=[
        "webview",  # pywebview must be installed at build time; hiddenimport alone is not enough
        "api.workspaces",
        "api.composers",
        "api.logs",
        "api.search",
        "api.export_api",
        "api.pdf",
        "api.config_api",
        "utils.exclusion_rules",
        "utils.workspace_path",
        "utils.path_helpers",
        "utils.text_extract",
        "utils.tool_parser",
        "utils.cli_chat_reader",
        "utils.cursor_md_exporter",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CursorChatBrowser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,           # --windowed: no console window
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="CursorChatBrowser",
)
