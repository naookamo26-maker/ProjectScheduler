# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for ProjectSchedulerGUI.
#
# ビルド方法（Windows環境で実行する。PyInstallerはクロスコンパイル非対応のため、
# .exe を作るにはWindows上でこのspecを実行する必要がある — 詳細は docs/packaging.md）:
#
#   pip install -r requirements.txt pyinstaller
#   pyinstaller packaging/ProjectSchedulerGUI.spec
#
# 単一ファイル（--onefile）としてビルドする。生成物は
# dist/ProjectSchedulerGUI.exe の1ファイルのみ（依存ライブラリ・plotly.min.js
# 等も実行ファイル内に埋め込まれる）。起動時に一時フォルダへ自己展開するため、
# --onedir構成（フォルダ配布）よりわずかに起動が遅くなるが、配布・受け渡しは
# この1ファイルをコピーするだけでよい。

import os
import sys

block_cipher = None

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))

# ファイルのプロパティ（詳細）に出るバージョン情報。番号は app_version.py が唯一の定義。
sys.path.insert(0, REPO_ROOT)
from app_version import APP_VERSION, windows_version_tuple  # noqa: E402

VERSION_NUMBERS = windows_version_tuple()
VERSION_FILE = os.path.join(REPO_ROOT, "build", "version_info.txt")
os.makedirs(os.path.dirname(VERSION_FILE), exist_ok=True)
with open(VERSION_FILE, "w", encoding="utf-8") as f:
    f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={VERSION_NUMBERS}, prodvers={VERSION_NUMBERS},
                    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable("040904B0", [
      StringStruct("FileDescription", "ProjectScheduler"),
      StringStruct("FileVersion", "{APP_VERSION}"),
      StringStruct("InternalName", "ProjectSchedulerGUI"),
      StringStruct("OriginalFilename", "ProjectSchedulerGUI.exe"),
      StringStruct("ProductName", "ProjectScheduler"),
      StringStruct("ProductVersion", "{APP_VERSION}"),
    ])]),
    VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
  ],
)
""")

a = Analysis(
    [os.path.join(REPO_ROOT, "run_gui.py")],
    pathex=[REPO_ROOT],
    binaries=[],
    # 多言語対応の辞書（i18n.py が sys._MEIPASS/locales から読む。docs/roadmap.md §11）
    # 起動用アイコン（gui/main.py の app_icon_path() が sys._MEIPASS/assets/icon から読む）
    datas=[
        (os.path.join(REPO_ROOT, "locales"), "locales"),
        (os.path.join(REPO_ROOT, "assets", "icon", "app_icon.ico"), os.path.join("assets", "icon")),
    ],
    # pandas/plotly はいずれも project_scheduler.py が使用する実行時依存で、
    # pyinstaller-hooks-contrib が同梱するフックにより通常は自動検出されるが、
    # 明示しておくことで検出漏れ時の切り分けを容易にする。
    hiddenimports=["pandas", "plotly"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # cryptography はこのアプリの依存関係には含まれない。一部の開発環境で
    # 破損した/非互換なシステム版cryptographyパッケージがたまたま検出され、
    # ビルドを失敗させることがあるため明示的に除外する。
    excludes=["cryptography"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ProjectSchedulerGUI",
    # エクスプローラー・デスクトップのショートカットに出るアイコン。
    # 原画は assets/icon/app_icon.svg、生成は scripts/build_icon.py。
    icon=os.path.join(REPO_ROOT, "assets", "icon", "app_icon.ico"),
    # ファイルのプロパティに出るバージョン情報（上で app_version.py から生成）
    version=VERSION_FILE,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUIアプリなのでコンソールウィンドウを表示しない
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    onefile=True,
)
