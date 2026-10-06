# CLAUDE.md

## 作業ルール

- 作業の途中経過は日本語で報告すること。
- 画面（GUI）に関係する実装を行った場合は、その画面のスクリーンショットを共有すること。

## ダークモード（`docs/architecture.md`「ダークモード」）

- 画面はライト／ダークの両方で見えること。色を直接指定する（スタイルシート・独自描画）
  ときは、パレットの役割の色を使うか、`is_dark_theme()`（`gui/widgets_common.py`）で
  ライト／ダークの色を切り替える。ライトの色だけを書かない。
- スタイルシートで色を付けた部品の中のボタンは、背景・枠・文字色も明示する
  （Windows 11 ではスタイル任せだと文字が白になり読めないことがある）。
- 画面に手を入れたら、スクリーンショットはライトとダークの両方で確かめる。

## バージョン（`app_version.py`、`docs/packaging.md`「バージョン」）

- アプリに手を入れる変更（不具合の修正・機能の追加や変更・画面や文言の変更）をしたら、
  **必ずパッチ（末尾の数字）を1つ上げる**（`APP_VERSION` を変えるだけ。1つのPRにつき1回）。
  ドキュメント・テスト・ビルドの設定だけの変更では上げない。
- 少し大きめの変更（新しい機能・タブの追加、使い方が変わる変更など）のときは、
  **マイナー（真ん中の数字）を上げるかどうかを利用者に確認する**。上げたらパッチは0に戻す。

## 多言語対応（`i18n.py`、`docs/roadmap.md` §11）

- 画面・メッセージ・Undoのラベル・出力ファイルの文言（ログは除く）は、原文（日本語）を
  `tr("…")`（差し込みは `tr("{n}件", n=n)`）で包んで書く。後で訳す定数には `N_("…")`。
  `@undoable`・`undo_group`・`bind_undo_session` のラベルは記録時に訳されるので包まない。
- 文言を追加・変更したコミットでは、**同じコミットで `locales/` の3言語（en・vi・zh_CN）の
  訳も更新する**。訳語は `docs/i18n_glossary.md` に必ず従う（新しい用語は先に足す）。
- 訳の抜け・使われなくなった訳・差し込みの食い違い・包み忘れ・import 時に評価される
  `tr()`（モジュール直下・既定引数。定数には `N_()` を使う）は `tests/test_i18n.py`（`core`）が検出する。
- 日付は全言語で `2026-09-24` 形式。コメント・docstring・ログは翻訳しない。

## Undo/Redo

- GUIで編集可能な機能はすべてUndo/Redoで元に戻せること（内容・選択・アクティブタブを復元。フォーカスは復元しない）。
  - 例外: オプション設定（`gui/app_settings.py`・`gui/options_dialog.py`）はプロジェクトの内容ではないので対象外。
- `ProjectDatabase`(`gui/db.py`)の変更系メソッドには`@undoable("ラベル")`を付け、`_commit()`経由で書き込む。
- 複数DB呼び出し・複数選択への一括操作は`with db.undo_group("ラベル"):`で1つのUndo単位にまとめる。
- 連続して値が変わる入力（スピンボックス等）は`bind_undo_session()`でフォーカス単位の1Undoにまとめる。
- 新しいタブ・編集機能には`capture_ui_state()`/`restore_ui_state(state)`を実装する。
- 詳細は`docs/architecture.md`の「Undo/Redo」を参照。

## テストの実行範囲

テストは「必要な依存パッケージ＝実行コスト」で3階層に分類してある（`pytest.ini`の
`markers`、各テストファイル冒頭の`pytestmark`）。**変更した箇所に対応する階層だけを
実行し、全実行は下記の条件のときだけ行う。**

| マーカー | 対象モジュール | 必要な依存 | 件数 / 時間 |
| --- | --- | --- | --- |
| `core` | `gui/db.py` `gui/db_schema.py` `gui/undo_manager.py` `gui/gantt_edit.py` `i18n.py` `locales/` `app_version.py` | pytestのみ | 162件 / 約4秒 |
| `scheduler` | `project_scheduler.py` `gui/gantt_generator.py` `gui/summary_metrics.py` `gui/plan_confirmation.py` `gui/plan_actions.py` `gui/gantt_row_order.py` | + pandas, numpy | 211件 / 約5秒 |
| `gui` | `gui/`のウィジェット層・描画層 | + PySide6 + システムライブラリ | 247件 / 約60秒 |

変更したファイル → 実行するコマンド:

| 変更した場所 | コマンド |
| --- | --- |
| `gui/db.py`, `gui/db_schema.py`, `gui/undo_manager.py`, `gui/gantt_edit.py`, `i18n.py`, `locales/*.json`, `app_version.py` | `pytest -m core` |
| `project_scheduler.py`, `gui/gantt_generator.py`, `gui/summary_metrics.py`, `gui/plan_confirmation.py`, `gui/plan_actions.py`, `gui/gantt_row_order.py` | `pytest -m scheduler` |
| `gui/resource_histogram.py`, `gui/node_canvas.py`, `gui/gantt_view.py`, `gui/analysis_charts.py`, `gui/schedule_cache.py`, `gui/tab_*.py`, `gui/main.py`, `gui/widgets_common.py`, `gui/plan_band.py`, `gui/app_settings.py`, `gui/options_dialog.py`, `gui/gantt_task_editor.py`, `gui/replan_dialog.py` | `pytest -m gui` |
| ドキュメント・README・コメントのみ | 実行しない |

- 複数階層にまたがる変更は、まとめて指定する: `pytest -m "core or scheduler"`。
- DB層(`core`)を変えた場合、その上に乗る`scheduler`・`gui`も壊れうる。判断に迷ったら
  **上位の階層まで含める**（`pytest -m "not gui"` が中間の安全策として使える）。

### 全実行（`pytest`）を行う条件

次のいずれかに当てはまるときだけ、引数なしの`pytest`で620件すべてを回す。

- 利用者から明示的に「全部回して」と指示があったとき。
- 影響が横断的な変更をしたとき。具体的には、DBスキーマの変更（`gui/db_schema.py`・
  マイグレーション）、Undo/Redo基盤の変更（`@undoable`・`undo_group`・`UndoManager`）、
  スケジューリングアルゴリズム本体の変更、複数タブにまたがるリファクタリング、
  依存パッケージの更新。
- 上記に当たる変更を含んだままコミットする直前。

### 出力を増やさない

- `pytest.ini`の`addopts`で`-q --no-header --tb=short`が既定になっている。
  **`-v`は付けない**（620件のテスト名が出力を埋めるだけで、得られる情報は増えない）。
- 失敗を追うときは、全体を回し直さず、失敗したテストだけを名指しで再実行する:
  `pytest tests/test_undo_redo.py::test_foo --tb=long`

### 環境セットアップ（必要な階層のぶんだけ）

- `core`のみ: `pip install pytest`
- `scheduler`まで: 上記 + `pip install pandas numpy`
- `gui`まで: 上記 + `pip install -r requirements.txt`。Linuxでは加えて
  `apt-get update && apt-get install -y libegl1 libgl1 libxkbcommon0 libdbus-1-3`
  （これが無いとPySide6のimportが`libEGL.so.1`が見つからず失敗する）。
- 依存が足りない階層のテストは、collectエラーではなく**skip**になる。skip行が出るのは
  正常であり、原因を調査する必要はない。
- `QT_QPA_PLATFORM=offscreen`はルートの`conftest.py`が設定するので手動指定は不要。

## ドキュメント・ファイルの読み方

必要な範囲だけを読む。フォルダ全体・ファイル全体を無条件に読まない。

各ドキュメントの担当範囲。**該当するものだけ、該当する節だけ**を読む。

| ファイル | 内容 | 読むとき |
| --- | --- | --- |
| `docs/architecture.md` (1051行) | 全体構成・データフロー・Undo/Redo・各機能の内部設計 | 実装の設計判断に関わるとき。長いので`grep -n '^## ' docs/architecture.md`で目次を出し、該当する節だけを読む |
| `docs/screens.md` (728行) | タブごとの画面仕様 | GUIの画面仕様を変えるとき。該当タブの節だけ |
| `docs/db_design.md` (183行) | テーブル定義・設計判断 | スキーマ・永続化を変えるとき |
| `docs/requirements.md` (172行) | 要件定義・スコープ | 仕様の妥当性やスコープ外かどうかを判断するとき |
| `docs/roadmap.md` (494行) | 今後の検討メモ | 今後の方針を問われたときだけ。通常の実装時には不要 |
| `docs/packaging.md` (72行) | `.exe`ビルド手順 | `packaging/`やビルドを触るとき |
| `docs/user_guide/` | 利用者向けドキュメント（原稿・撮影/ビルドの手順・執筆方針は`outline.md`） | 利用者向けドキュメントを書く・直すとき。画面を変えたら画像の撮り直しを検討する |
| `docs/project_analysis_tab_design.md` (337行) | プロジェクト分析タブ（実装中）の設計案 | プロジェクト分析タブの実装・仕様を検討するときだけ |

コードを読むときは、まず`grep -n`で対象の定義位置を特定し、その周辺だけを読む。
特に大きいモジュール（`gui/db.py` 1550行、`project_scheduler.py` 1839行、
`gui/node_canvas.py` 1209行、`gui/tab_jobs.py`、`gui/tab_basic_info.py`）を
先頭から全部読むのは最後の手段にする。

次のファイルは読み込まない（生成物・バイナリで、読んでも設計判断の役に立たない）。

- `data/*.pschedule`（SQLiteのバイナリ）。中身が必要なら`gui/db.py`のAPI経由か
  `sqlite3`で必要な行だけを取り出す
- `output/`配下の生成物
