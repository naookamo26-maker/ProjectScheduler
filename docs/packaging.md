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
`ProjectSchedulerGUI-windows` という名前のArtifact（zip、`dist/
ProjectSchedulerGUI/` 一式）をダウンロードできる。展開して
`ProjectSchedulerGUI.exe` を実行する（同フォルダの `_internal/` は
実行に必要な同梱ファイル一式なので、`.exe` と同じ場所に置いたまま使う）。

## 方法2: 手元のWindows環境でビルド

```powershell
pip install -r requirements.txt
pip install pyinstaller
pyinstaller packaging\ProjectSchedulerGUI.spec
```

`dist\ProjectSchedulerGUI\` に `ProjectSchedulerGUI.exe` と `_internal\`
（依存ライブラリ・plotly.min.js等の同梱データ）が生成される。フォルダごと
配布する（`--onedir` 形式、既定）。

単一の `.exe` ファイルにまとめたい場合は、`packaging/ProjectSchedulerGUI.spec`
内のコメントに従い `EXE(...)` の呼び出しを1ファイル版に差し替える
（起動がわずかに遅くなる代わりに配布物が1ファイルになる）。

## spec ファイルの構成（`packaging/ProjectSchedulerGUI.spec`）

- エントリポイントは `run_gui.py`。
- `pandas`/`openpyxl`/`plotly` を `hiddenimports` に明示している（通常は
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

## 開発時の検証内容

本リポジトリのLinux開発環境では、以下をビルドが通ることで確認済み
（Windows実機での最終確認はGitHub Actions側で行う）:

- PySide6・pandas・openpyxl・plotly を含む依存グラフ全体が解析エラーなく
  ビルドできること
- `plotly.min.js` がデータファイルとして正しく同梱されること
- ビルドされた実行ファイルが実際に起動し、（オフスクリーンQtプラットフォーム
  上で）クラッシュせずGUIイベントループに入ること
