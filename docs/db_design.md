# DB設計

## 概要

GUI（`gui/`）の入力データは、1プロジェクト＝1ファイルのSQLiteデータベースに保存する。
保存ファイルの拡張子は `.pschedule` を使う。中身は標準的なSQLiteファイルであり、
拡張子だけこのツール専用にしている（`file` コマンドや一般的なSQLiteビューアで
中身を確認できる。既定のブラウザ関連付けやファイル選択ダイアログでの見分けやすさ
のためだけの命名で、フォーマット上の特別な処理は一切ない）。

CRUD一式は `gui/db.py` の `ProjectDatabase` クラスに、スキーマ定義（DDL）と
旧バージョンからのマイグレーションは `gui/db_schema.py` に実装する
（テーブルを足すたびに伸びるスキーマ側を、CRUDの実装から切り離すため。
スキーマ変更の手順は `gui/db_schema.py` の冒頭に記載）。いずれのモジュールも
PySide6（Qt）に一切依存しないため、GUIを起動せずに単体でテストできる
（`tests/test_gui_gantt_smoke.py` 等）。

接続のたびに `PRAGMA foreign_keys = ON` を明示的に設定する（SQLiteは
既定で外部キー制約が無効なため）。

## テーブル一覧

| テーブル | 役割 |
|---|---|
| `schema_meta` | スキーマバージョン管理用。`ProjectDatabase.open_existing`が旧バージョンの`.pschedule`を検出すると`_migrate_schema`で自動的に不足カラム等を追加する（例: v1→v2で`workflows.sort_order`を追加）。`open_existing`はマイグレーションの前に`check_openable`（`gui/db_schema.py`）でこのテーブル・行の有無と`schema_version`がこのアプリの対応範囲内かを検証し、ProjectSchedulerのファイルとして扱えない場合は`ProjectDatabaseError`にして拒否する（詳細は`docs/architecture.md`「ファイルを開く際の検証」参照） |
| `project` | プロジェクト名・開始日・`distribution_ratio`（ガントチャートタブの「配置コントロール」で調整する配置基準点、既定0.7。`project_scheduler.py`参照）（常に1行、`id=1`固定）。v17で計画の確定用に`replan_base_date`（確定行の無いタスクを置かない下限）・`replanned_at`（全面再計画の実行日）・`confirmed_at`（最後に確定した日時）・`confirmed_global_signature`（確定時の全体設定の指紋）を追加（`docs/roadmap.md` §8）。v18で`pending_replan_base_date`・`pending_replanned_at`（全面再計画を実行して、まだ確定していない間の基準日と実行日。「変更を確定」で`replan_base_date`・`replanned_at`へ移し、「変更を破棄」では確定時点のスナップショットに戻るので消える）を追加 |
| `milestones` | マイルストーン（名前・締切日・備考） |
| `teams` | チーム（名前・開発開始日からの既定の同時ライン数）。ライン数は3状態: NULL＝指定なし（上限を設けない。新規チームの既定）、0＝その期間は稼働なし、N＝N本 |
| `team_capacity_changes` | チームの同時ライン数が期間の途中で変わる場合の変更点（適用開始日・その日以降のライン数、こちらも同じ3状態）。`teams.max_lines`はいつまでも「最初の期間」の値として残る |
| `holidays` | 休業日（日付、任意でチームを指定。未指定は全チーム共通。備考あり） |
| `workflows` | ワークフロー（テンプレートの名前と、一覧での表示順`sort_order`） |
| `workflow_tasks` | ワークフロー内のタスク（名前・担当チーム・所要日数）。ノードグラフ上の座標は保持しない——依存の深さから毎回計算し直す（下記「座標は保存しない」参照） |
| `task_dependencies` | ワークフロー内のタスク依存（Internal_Depends相当、predecessor→successor）。`dep_type`が種別（`FS`=完了→開始 / `SS`=開始→開始）、`lag_days`が間に空ける営業日数（負ならリード＝先行の完了前に着手可）。既定は`FS`・`0` |
| `jobs` | ジョブ（ワークフローの実体化。名前・使用ワークフロー・既定マイルストーン・優先度・タグ）。`priority`はNULL可（未指定）——未指定は`project_scheduler.py`側で自動的に最低優先として扱う。`tags`はカンマ区切りの1文字列（例:「緊急, 顧客A」）で、書き込み経路（`gui/db.py`の`normalize_tags`）で正規化する。`stable_key`（v17）は作成時に決めて変えない安定キーで、スケジューラの配置のばらつきの種にする（内部IDを種にすると、ジョブを作り直すだけで無関係なジョブまで動くため。移行時は既存ジョブに今の内部IDの文字列を入れ、日程が動かないようにしている） |
| `job_task_overrides` | ジョブ単位でのタスク上書き（有効/無効・日数・マイルストーン・チームの差分のみ保持）。`start_pin_date`が開始固定日（実績確定・外部都合のピン留め）。**日付を「入力」として持つ唯一の場所**。`tags`がタスク タグ（`jobs.tags`＝ジョブ タグと同じ仕様のカンマ区切り文字列）。`status`が実際の進捗（NULL＝未着手／`in_progress`＝進行中／`done`＝完了。ユーザーが手動で記録する値で、日付からの推測ではない。プロジェクト分析タブの「タスクの状態」集計に使う） |
| `job_dependency_links` | ジョブ単位の依存リンク（「このジョブは、あのジョブに依存する」）。追加時に`workflow_dependency_templates`を参照し、タスク単位の依存を自動展開する |
| `job_external_dependencies` | ジョブをまたぐタスク依存（External_Dependencies相当）。`source_link_id`で`job_dependency_links`からの自動生成分か手動追加分かを区別する。`is_active`で（自動生成分も含め）削除せず一時的に無効化できる |
| `confirmed_schedule` | 合意した日程（v17、`docs/roadmap.md` §8-2）。確定したときの計算結果をタスクごとに持つ（開始・終了（exclusive）・日数・チーム・確定時の入力の指紋）。空なら未確定のファイル |
| `draft_base` | 変更案に入った時点のDB全体（zlib圧縮）。変更案の破棄に使う（v17、§8-8）。1行だけ |
| `draft_moves` | 変更案の中でガントからドラッグした開始日（v17、§8-8）。確定したら確定行へ書き込んで消す |
| `workflow_dependency_templates` | ワークフローペア単位の既定タスク対応（例: ワークフローAがワークフローBに依存する場合、Aのどのタスクが、Bのどのタスクの完了を待つか） |

DDLの正本は `gui/db_schema.py` の `_SCHEMA_SQL` を参照（このドキュメントは概要説明用で、
列の追加・変更が生じた場合は `_SCHEMA_SQL` 側を先に直し、本ドキュメントを追随させる）。

## 主要な設計判断

### 日付は「入力」として持たず、開始固定日として別に持つ

タスクに`start_date`を入力として持たせると、**計算結果と入力が同じ列に混ざる**。
依存関係・所要日数・休業日を変えた瞬間に矛盾し、それを繕うための整合性維持コードが
際限なく増える。

代わりに`job_task_overrides.start_pin_date`として分離した（MS Projectの
制約と同じ考え方——「開始日を固定する」という制約1種類だけに絞った形）。
守る不変条件はひとつだけ:

> **固定は入力、日程は出力。この分離を絶対に崩さない。**

固定は「解が満たすべき条件」であって「解」ではない。だから依存関係を足しても
休業日を変えても固定データ自体は壊れない。壊れるのは「解が見つからない」場合だけで、
それは事前の整合性維持ロジックではなく、**スケジューリング実行時の診断結果**として
扱う（締切超過と同じ扱い方。`Constraint_Violation`列）。

この設計の実利は、`cascade_milestone_to_successors`のような
「入力同士を事前に矛盾させないためのコード」を一切増やさずに済むこと。
参照整合性は外部キーだけで足り、固定が0件でも成立するため既存データは無変更。

**当初は`SNET`/`SNLT`/`FNLT`/`START_ON`の4種別を専用テーブル
（`task_constraints`）に持たせる設計だったが、実装後に撤回した。** ジョブ数が
増えると個別ジョブ単位でのSNET/SNLT/FNLT設定は現実的に使われなくなり、それを
マイルストーン単位で一括設定する仕組みは概念が複雑になりすぎると判断したため。
経緯は`docs/roadmap.md`§4-1、設計の詳細は`docs/architecture.md`
「開始固定日と2パス構成」を参照。


### ノードグラフの座標は保存しない

`workflow_tasks`は当初`canvas_x`/`canvas_y`列を持ち、ノードビューでドラッグ
した位置をDBへ保存していた。**この2列は撤去した**（スキーマv10）。

座標は依存関係から`compute_auto_layout`で毎回計算し直せる、**保持しておく
意味のないデータ**だった。実害も出ている——タスクをコード経由で一括生成する
スクリプト（`scripts/generate_large_sample.py`）が座標の初期化を忘れると、
全ノードが既定値`(0, 0)`のまま重なって表示される不具合が実際に発生した。
依存テンプレートの疑似ノード（`workflow_dependency_templates`）は元々
座標カラムを持たず「表示のたびに計算し直す」設計だったため、この不具合は
「タスクノードだけ保存済みの座標を使い、疑似ノードだけ毎回計算し直す」
という非対称性そのものが原因だった。

対処は、非対称性を無くす方向——タスクノードも疑似ノードと同じく、座標を
一切保存せず毎回計算し直す方式に統一した。データを保持しないので、
「保存を忘れて壊れる」という経路が構造的に無くなる。ノードのドラッグ自体は
引き続きでき、その場限りの一時的な並べ替えとして使えるが、DBには書き込まず、
次に何か編集する・ファイルを開き直すと自動レイアウトに揃う。
詳細は`docs/architecture.md`「疑似ノードの座標はDBに保存しない」参照。


### IDはすべて内部の整数連番PK

`Workflow_ID`（例: `WF_CHAR`）のような、元のExcelフォーマットが使っていた
文字列IDはGUI上には一切登場しない。全てのテーブルは `INTEGER PRIMARY KEY
AUTOINCREMENT` を持ち、GUIのあらゆる参照（コンボボックス等）は名前で
表示・選択する。文字列ID形式への変換は `gui/gantt_generator.py` が
ガントチャート生成の直前にのみ、一時的に行う（後述）。

### 差分のみ保持する `job_task_overrides`

ジョブが使うワークフローの全タスクは自動的にそのジョブのタスクになる
（`workflow_tasks` から導出）。既定値（有効・標準日数・チーム）のままの
タスクについては `job_task_overrides` に行を作らない。ユーザーが
「このジョブだけ日数を変える」「このタスクを無効化する」等、既定から
外れる操作をしたときだけ1行作られる（`UNIQUE(job_id, workflow_task_id)`）。
既定に戻した場合は行を削除する（`ProjectDatabase.clear_job_task_override`）。

### マイルストーンの整合性はアプリ層（`gui/tab_jobs.py`）で維持する

`job_task_overrides.milestone_id`にはDB制約上の順序チェックは無い
（`task_dependencies`をまたいだ順序はSQLで表現しにくいため）。代わりに
`ProjectDatabase.minimum_milestone_end_date`（先行タスクの実効マイルストーン
より前は選択肢に出さない）と`cascade_milestone_to_successors`（先行タスクの
マイルストーンを変えた際、後続タスクが前倒しにならないよう自動的に揃える）
をGUI側（タブ3のマイルストーン上書き）が組み合わせて呼び出すことで、
「後継タスクが先行タスクより早いマイルストーンを持たない」という不変条件を
保つ。対象は同一ワークフロー内の`task_dependencies`のみ（ジョブをまたぐ
`job_external_dependencies`は対象外）。

### 削除時の参照整合性はアプリ層で守る

`ON DELETE CASCADE/RESTRICT/SET NULL` はDB制約として設定しているが、
生の `sqlite3.IntegrityError` をユーザーに見せないよう、`ProjectDatabase`
の削除系メソッド（`delete_team`/`delete_workflow`等）が先に使用件数を
調べ、参照が残っている場合は `ReferencedEntityError` を送出してブロック
する。GUI側はこれを捕捉して分かりやすいダイアログに変換する。

| 削除対象 | 挙動 |
|---|---|
| チーム | `workflow_tasks`/`job_task_overrides`から参照があれば削除不可 |
| ワークフロー | `jobs`から参照があれば削除不可 |
| マイルストーン | 参照するJob/上書きがあれば警告（削除は許可、参照はNULLになる） |
| ジョブ | 他ジョブからの外部依存があれば警告（削除は許可、依存も連鎖削除） |
| ワークフロー内タスク | Job側の上書き/外部依存/内部依存があれば件数を警告 |

### 依存テンプレートとジョブ依存リンクの自動展開（`source_link_id`）

タブ4「依存関係」を廃止し、タブ3「ジョブ」に統合した際、ジョブをまたぐ
依存の入力をタスク単位からジョブ単位に簡素化する仕組みを追加した。

1. `workflow_dependency_templates` に、ワークフローペア単位で「Aワークフロー
   のこのタスクは、Bワークフローのこのタスクの完了を待つ」という既定ルールを
   タブ2「ワークフロー設計」で登録しておく。
2. タブ3「ジョブ」で、あるジョブが別のジョブに依存することを
   `job_dependency_links` に1行追加すると（`ProjectDatabase.
   add_job_dependency_link`）、2ジョブそれぞれのワークフローの組み合わせに
   該当する `workflow_dependency_templates` の行を検索し、対応する
   `job_external_dependencies` 行を自動生成する。生成された行には、元になった
   リンクの `id` を `source_link_id` として記録する。
3. `job_dependency_links` の行を削除すると、`source_link_id` がそれを指す
   `job_external_dependencies` 行は `ON DELETE CASCADE` により連動して削除
   される。ユーザーが個別に手動追加した依存（`source_link_id IS NULL`）は
   影響を受けない。

`source_link_id IS NULL` ＝ユーザーが個別に追加した例外的な依存、
`source_link_id`が非NULL＝テンプレートから自動生成された依存、という区別が
常に成り立つ。ガントチャート生成（`gui/gantt_generator.py`）は
`job_external_dependencies` テーブルをそのまま読むため、自動生成行・手動行の
区別なくスケジューリングに反映される。

### 休業日の一意性

全社共通休業日（`team_id IS NULL`）同士の重複は、SQLiteの`UNIQUE`制約では
（`NULL`同士は別物として扱われるため）捕捉できない。`add_holiday`/
`update_holiday` 内で事前に `SELECT ... WHERE team_id IS ?` によるチェックを
行い、`DuplicateNameError` を送出する。チーム別休業日は
`CREATE UNIQUE INDEX ux_holidays_team ... WHERE team_id IS NOT NULL` で
DB制約として保証している。

## 例外クラス

`gui/db.py` は以下の業務例外を定義する（いずれも `ProjectDatabaseError` を継承）:

- `DuplicateNameError` — 一意性制約違反（名前の重複等）
- `ReferencedEntityError` — 参照が残っている行を削除しようとした場合

GUI側はこれらをキャッチして `QMessageBox` に変換し、生のSQLite例外を
ユーザーに見せない。
