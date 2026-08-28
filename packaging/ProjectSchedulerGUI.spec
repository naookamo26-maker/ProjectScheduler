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

block_cipher = None

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))

a = Analysis(
    [os.path.join(REPO_ROOT, "run_gui.py")],
    pathex=[REPO_ROOT],
    binaries=[],
    datas=[],
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
