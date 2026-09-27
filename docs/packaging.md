# `.exe` パッケージング手順

GUI（`run_gui.py`）をPyInstallerでWindows向け実行ファイルに固める手順。

## 重要な制約: クロスコンパイル不可

PyInstallerは**クロスコンパイルに対応していない**——ビルドを実行したOS向けの
バイナリしか生成できない。したがって Windows用の `.exe` を作るには、
**Windows環境でビルドを実行する必要がある**（開発時のLinuxコンテナ上で
ビルドしても、生成されるのはLinux用の実行ファイルであり `.exe` にはならない）。

このため、以下2つの方法を用意している。

## 方法1: GitHub Actionsでビルド（推奨、手元にWindows機が無くても可）

`.github/workflows/build-exe.yml` が `windows-latest` ランナー上でビルドを
実行する。**実行は手動のみ**（`workflow_dispatch`）。GitHubの無料プランでは
Artifactの保存容量に上限があり、pushのたびに自動ビルドすると上限に達したため、
自動実行はやめた。

実行手順: GitHubのリポジトリ画面 → Actionsタブ → 左の一覧から
「Build Windows exe」→「Run workflow」→ ビルドしたいブランチを選んで実行。

ビルドが成功すると、ワークフローの実行結果ページから
`ProjectSchedulerGUI-windows` という名前のArtifact（zip）をダウンロードできる。
展開すると次の2ファイルが得られる。

| ファイル | 内容 |
| --- | --- |
| `ProjectSchedulerGUI.exe` | アプリ本体（依存ライブラリ・plotly.min.js等はすべて実行ファイル内に埋め込まれている） |
| `user_guide_ja.pdf` | 利用者ガイド（`docs/user_guide/`） |

利用者ガイドは、`.exe` とは別のジョブ（`build-user-guide`、`ubuntu-latest`）で
`scripts/user_guide/build_pdf.py` を実行して作り、`.exe` のジョブがそれを受け取って
同じzipに入れる。画面の画像はリポジトリにあるもの（`docs/user_guide/images/`）を
そのまま使い、ここでは撮り直さない。画面を変えたときは、画像を撮り直してコミットしてから
ビルドする（`docs/user_guide/README.md`）。受け渡し用の `user-guide` というArtifactも
一覧に出るが、1日で自動削除される。
Artifactは**7日で自動削除**される（`retention-days: 7`）ので、必要な`.exe`は
それまでにダウンロードしておく。

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
- `icon=` に起動用アイコン `assets/icon/app_icon.ico` を指定している（エクスプローラー・
  ショートカットに出る）。同じファイルを `datas` でも同梱し、実行時に
  `gui/main.py` の `app_icon_path()` が読んでウィンドウ・タスクバーのアイコンにする。
- `cryptography` を明示的に除外している。このアプリの依存関係には含まれず、
  一部の開発環境で検出される破損／非互換なシステム版 `cryptography` が
  ビルドを失敗させることがあったため（Windows上のクリーンな環境では
  通常発生しない）。
- `onefile=True`（`EXE(...)` に `a.binaries`/`a.zipfiles`/`a.datas` を直接
  渡し、`COLLECT(...)` を使わない構成）により、単一の `.exe` にすべてを
  埋め込む。フォルダ配布（`--onedir`）に戻したい場合は、`EXE(...)` の
  `onefile=True` を外して `exclude_binaries=True` にし、`COLLECT(exe,
  a.binaries, a.zipfiles, a.datas, ...)` を追加すればよい。

## 起動用アイコン（`assets/icon/`）

| ファイル | 内容 |
| --- | --- |
| `app_icon.svg` | 原画（48px以上で使う）。1本目＝完了、2本目＝今日まで進行、3本目＝未着手、赤の破線＝今日 |
| `app_icon_small.svg` | 32px以下で使う簡略版。破線が潰れるので今日の線を太い実線にしている |
| `app_icon.ico` | 生成物。16〜256pxの9サイズをPNG埋め込み形式でまとめたもの |
| `app_icon_256.png` | 生成物。ドキュメント等に貼るためのプレビュー |

原画を直したら `python scripts/build_icon.py` を実行し、生成物もコミットする
（PySide6だけで生成するので追加の依存は要らない）。

## 開発時の検証内容

本リポジトリのLinux開発環境では、以下をビルドが通ることで確認済み
（Windows実機での最終確認はGitHub Actions側で行う）:

- PySide6・pandas・plotly を含む依存グラフ全体が解析エラーなく
  ビルドできること
- `plotly.min.js` がデータファイルとして正しく同梱されること
- ビルドされた実行ファイルが実際に起動し、（オフスクリーンQtプラットフォーム
  上で）クラッシュせずGUIイベントループに入ること

## バージョン（`app_version.py`）

アプリのバージョンは `app_version.py` の `APP_VERSION`（例: `1.0.0`）が唯一の定義。
spec はビルド時にここから `build/version_info.txt` を生成して `EXE(version=...)` に渡し、
`.exe` のファイルのプロパティ（詳細）にファイルバージョン・製品バージョンとして埋め込む。
画面の「ヘルプ」→「バージョン情報」と、利用者ガイドの表紙（`scripts/user_guide/build_pdf.py`）も
同じ定義を読むので、バージョンを上げるときは `app_version.py` だけを変える。

