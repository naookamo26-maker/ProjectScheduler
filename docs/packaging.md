# `.exe` パッケージング手順

GUI（`run_gui.py`）をPyInstallerでWindows向け実行ファイルに固める手順。

## 重要な制約: クロスコンパイル不可

PyInstallerは**クロスコンパイルに対応していない**——ビルドを実行したOS向けの
バイナリしか生成できない。したがって Windows用の `.exe` を作るには、
**Windows環境でビルドを実行する必要がある**（開発時のLinuxコンテナ上で
ビルドしても、生成されるのはLinux用の実行ファイルであり `.exe` にはならない）。

このため、以下2つの方法を用意している。

## 方法1: GitHub Actionsで自動ビルド（推奨、手元にWindows機が無くても可）

`.github/workflows/build-exe.yml` が `windows-latest` ランナー上でビルドを
自動実行する。トリガー:

- `gui/`, `project_scheduler.py`, `run_gui.py`, `requirements.txt`,
  `packaging/` のいずれかを変更して `main`/`master` ブランチにpushしたとき
- Actionsタブから手動実行（`workflow_dispatch`）

ビルドが成功すると、ワークフローの実行結果ページから
`ProjectSchedulerGUI-windows` という名前のArtifact（zip）をダウンロードできる。
展開すると `ProjectSchedulerGUI.exe` の単一ファイルが得られる
（依存ライブラリ・plotly.min.js等はすべて実行ファイル内に埋め込まれており、
他に配布するファイルは無い）。

## 方法2: 手元のWindows環境でビルド

```powershell
pip install -r requirements.txt
pip install pyinstaller
pyinstaller packaging\ProjectSchedulerGUI.spec
```

`dist\ProjectSchedulerGUI.exe` の1ファイルのみが生成される
（`--onefile` 構成。依存ライブラリ・plotly.min.js等の同梱データも実行ファイル
内に埋め込まれ、フォルダごと配布する必要はない）。この `.exe` 1つを配布・
コピーするだけで実行できる（起動時に一時フォルダへ自己展開するぶん、
複数ファイル構成の `--onedir` よりわずかに起動が遅くなる）。

## spec ファイルの構成（`packaging/ProjectSchedulerGUI.spec`）

- エントリポイントは `run_gui.py`。
- `pandas`/`plotly` を `hiddenimports` に明示している（通常は
  `pyinstaller-hooks-contrib` 同梱のフックで自動検出されるが、検出漏れ時の
  切り分けを容易にするため）。特に `plotly` はガントチャートHTML生成に
  `plotly.min.js`（パッケージ内データファイル）を埋め込むため、この
  データファイルが確実に同梱されることを開発時に確認済み
  （`plotly/package_data/plotly.min.js` が `_internal/` 配下に含まれる）。
- `console=False` によりGUIアプリとしてコンソールウィンドウを表示しない。
- `cryptography` を明示的に除外している。このアプリの依存関係には含まれず、
  一部の開発環境で検出される破損／非互換なシステム版 `cryptography` が
  ビルドを失敗させることがあったため（Windows上のクリーンな環境では
  通常発生しない）。
- `onefile=True`（`EXE(...)` に `a.binaries`/`a.zipfiles`/`a.datas` を直接
  渡し、`COLLECT(...)` を使わない構成）により、単一の `.exe` にすべてを
  埋め込む。フォルダ配布（`--onedir`）に戻したい場合は、`EXE(...)` の
  `onefile=True` を外して `exclude_binaries=True` にし、`COLLECT(exe,
  a.binaries, a.zipfiles, a.datas, ...)` を追加すればよい。

## 開発時の検証内容

本リポジトリのLinux開発環境では、以下をビルドが通ることで確認済み
（Windows実機での最終確認はGitHub Actions側で行う）:

- PySide6・pandas・plotly を含む依存グラフ全体が解析エラーなく
  ビルドできること
- `plotly.min.js` がデータファイルとして正しく同梱されること
- ビルドされた実行ファイルが実際に起動し、（オフスクリーンQtプラットフォーム
  上で）クラッシュせずGUIイベントループに入ること
