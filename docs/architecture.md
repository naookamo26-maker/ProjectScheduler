# アーキテクチャ

## 全体構成

```
data/*.xlsx, data/*.pschedule ─┐
                                │  (Excel/DBそれぞれの読み込み)
project_scheduler.py ──────────┤  スケジューリングエンジン
  ├ _load_data(excel_file) ────┤  Excel専用の読み込み口（後方互換・CLI用）
  ├ _load_data_from_frames(…) ─┤  検証・整形の共通ロジック（Excel/DB共有）
  ├ run_resource_constrained_scheduler(excel_file, …)
  ├ run_resource_constrained_scheduler_from_frames(df_project, …, …)
  └ _run_scheduler_on_frames(…) ┘  スケジューリング本体（共有）

gui/ ───────────────────────────  PySide6デスクトップアプリ（.pschedule編集用）
  ├ db.py            SQLiteスキーマ + CRUD（Qt非依存）
  ├ main.py           MainWindow・4タブ組み立て・Fileメニュー
  ├ tab_basic_info.py  タブ1「基本情報設定」
  ├ tab_workflows.py    タブ2「ワークフロー設計」（node_canvas.pyをホスト）
  ├ node_canvas.py       ノードグラフエディタ（タスク依存関係の視覚編集）
  ├ tab_jobs.py            タブ3「ジョブ」
  ├ tab_dependencies.py     タブ4「依存関係」
  ├ widgets_common.py        タブ横断の共通UI部品
  └ gantt_generator.py        DB → project_scheduler.py 呼び出し → ガントチャート出力
```

## project_scheduler.py との連携方式

GUIは`project_scheduler.py`を**変更しつつ取り込む**方針を採る（Excelファイルへの
書き出しは行わない）。連携の核は、Excel専用だった読み込み処理をDataFrame処理と
Excel-IOに分離したことにある。

- `_load_data_from_frames(df_project, df_teams, df_ms, df_wf, df_jobs, df_jtasks=None, df_holidays=None, df_extdeps=None, df_wf_names=None)`
  — 列検証・`Milestone_ID`のインデックス化・任意データの既定値補完など、
  「DataFrームとして受け取ったデータを検証・整形する」ロジックのみを持つ。
  Excel由来かDB由来かを問わない共通の入口。`df_jtasks`等の任意項目は、
  `None`なら「そもそもデータが存在しない」、DataFrame（0行でも可）なら
  「存在する」ことを表す——Excelでシートの有無を見ていたのと同じ判定を、
  引数が`None`かどうかに一般化している。
- `_load_data(excel_file)` — `pd.ExcelFile`/`pd.read_excel`でシートを読み、
  `_load_data_from_frames`に委譲する薄いラッパー（Excel専用、CLI後方互換用）。
- `_run_scheduler_on_frames(...)` — スケジューリング本体（`_parse_tasks`〜
  `_build_scheduling_order`〜`_run_leveling`〜Gantt出力）。
  `run_resource_constrained_scheduler`と`run_resource_constrained_scheduler_from_frames`の
  両方から共有される。
- `run_resource_constrained_scheduler(excel_file, ...)` — 既存のExcelベース入口
  （CLIと過去の呼び出し互換のために維持）。
- `run_resource_constrained_scheduler_from_frames(df_project, ..., df_wf_names=None, ...)` —
  **GUIが使う新しい入口**。`gui/gantt_generator.py`がSQLiteの内容から直接
  DataFrameを組み立て、Excelファイル/バッファを一切経由せずにこの関数を呼ぶ。

この分離は純粋なリファクタリング（挙動変更なし）で、既存のサンプルデータに対する
出力（Mermaid Markdown・CSV相当のDataFrame内容）がリファクタ前後で完全一致する
ことを確認済み。

## GUI → スケジューラー のデータフロー

```
ProjectDatabase（gui/db.py）
    │  build_frames(db) が各テーブルをSQLで読み出し
    ▼
dict[str, DataFrame]（Project/Teams/Milestones/Workflows/Workflow_Names/
                      Jobs/Job_Tasks/Holidays/External_Dependencies 相当）
    │  gui/gantt_generator.py が
    │  project_scheduler.run_resource_constrained_scheduler_from_frames(...) を呼ぶ
    ▼
Mermaid Markdown (.md) / インタラクティブHTML (.html)
```

内部整数PKから`project_scheduler.py`が期待する文字列ID（例: `WF_001`）への
変換は、`build_frames`の中で一度だけ、ガントチャート生成の直前に行う。
GUIの画面上にこの文字列IDが表示されることはない。

`gui/gantt_generator.py`の`validate_for_generation(db)`は、生成前に
「プロジェクト名/開始日が未設定」「チーム/マイルストーン/ワークフロー/
ジョブが0件」等の未完成な状態を検出し、エラー文字列のリストを返す
（空リストなら生成可能）。これを先にチェックすることで、必須列を持たない
空のDataFrameがそのまま`_load_data_from_frames`に渡って分かりにくい例外に
なる事態を防ぐ。`gui/main.py`のFileメニュー「ガントチャートを生成...」は、
このチェック→出力フォルダ選択（既定は開いている`.pschedule`と同じ
フォルダ）→`generate_gantt()`呼び出しの順で動作し、`SchedulingError`系の
例外はダイアログに変換して表示する。

## ノードグラフの循環依存検出

`gui/node_canvas.py`の`WorkflowGraphScene`は、ワークフロー内のタスク依存を
`int -> list[int]`の隣接グラフとしてメモリ上に保持し、エッジ追加のたびに
「追加先ノードから追加元ノードに到達可能か」をBFSで判定して、循環になる
エッジを事前に拒否する。これは`project_scheduler.py`の
`_build_scheduling_order()`が採用しているトポロジカルソート（優先度付き
逆方向Kahn法、`CircularDependencyError`を事後的に検出）と同じ「依存グラフに
閉路がないこと」を保証する考え方を参考にしているが、キャンバス編集時点では
ジョブをまたいだ`g_id`（`"JOB_ID:TASK_ID"`）データ構造がまだ存在しないため、
ワークフロー内の単純な整数グラフに対する専用の事前チェックとして別実装して
いる。

ジョブをまたぐ外部依存（`job_external_dependencies`）については、キャンバスの
ような事前チェックは行わず、ガントチャート生成時に
`_build_scheduling_order()`が検出する`CircularDependencyError`をそのまま
GUI側でダイアログ表示する（プロジェクト全体の依存グラフを都度読み直す
コストが見合わないため）。

## DBの書き込みタイミング

`gui/widgets_common.py`の`CrudTableWidget`をはじめ、GUIの入力系ウィジェットは
明示的な「保存」ボタンを持たない。セル編集・コンボボックス選択・ノードの
ドラッグ確定などのイベントごとに、その場で`ProjectDatabase`のメソッドを呼んで
即座にコミットする（write-through方式）。`.pschedule`ファイルが常に唯一の
正となる。
