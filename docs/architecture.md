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
  ├ db.py            CRUD（Qt非依存）+ Undo記録の仕組み・明示的な保存
  ├ db_schema.py      SQLiteスキーマ定義 + 旧バージョンからのマイグレーション
  ├ undo_manager.py   Undo/Redoスタック（Qt非依存）
  ├ main.py           MainWindow・4タブ組み立て・File/Editメニュー・D&Dで開く
  ├ tab_basic_info.py  タブ1「基本情報設定」（チームはresource_histogram.pyの
  │                      リソースヒストグラムと連動するツリー表示）
  ├ resource_histogram.py  リソースヒストグラムの計算（純粋関数）・描画
  │                          （gui/gantt_view.pyのQGraphicsView描画方式を踏襲）
  ├ tab_workflows.py    タブ2「ワークフロー設計」（node_canvas.pyのノードビューと、
  │                       タスク表・依存テンプレート表のテーブルビューを切り替える）
  ├ node_canvas.py       ノードグラフエディタ（タスク依存関係の視覚編集、
  │                       依存テンプレートの疑似ノード、先行タスク複数選択、
  │                       フィット表示、ダイアログ開閉の共通ロジック）
  ├ tab_jobs.py            タブ3「ジョブ」（ワークフロー絞り込み、依存ジョブ
  │                          セクションを含む。旧タブ4はここに統合済み）
  ├ widgets_common.py        タブ横断の共通UI部品（列幅自動調整含む）
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

同じ`WorkflowGraphScene`の`auto_arrange()`は、タスクの追加・編集・削除や
依存エッジ・依存テンプレートの追加・編集・削除のたびに呼ばれ、
`compute_combined_layout`で全ノード・疑似ノードの座標を再計算し直す。
手動でドラッグした位置は、次に何か編集すると上書きされる。

## ノードビュー／テーブルビューの切り替えと依存テンプレートの疑似ノード

タブ2は、右ペインを「ノードビュー」（`WorkflowGraphView`）と「テーブルビュー」
（`gui/tab_workflows.py`のタスク表・依存テンプレート表）で切り替えられる。
両ビューは同じ`WorkflowGraphScene`を経由してDBを変更するため、一方の編集が
他方に即座に反映される。この共有を実現する設計は以下の通り。

- **ダイアログを開いてDB/シーンへ反映するロジックはビューに依存させない**——
  `add_task_via_dialog`/`edit_task_via_dialog`/`add_template_via_dialog`/
  `edit_template_via_dialog`（`gui/node_canvas.py`のモジュール関数）は、
  `WorkflowGraphScene`と親ウィジェットだけを引数に取り、`WorkflowGraphView`
  （右クリックメニュー・ダブルクリック）からも`WorkflowsTab`の`CrudSection`
  （＋追加/編集...ボタン）からも同じ関数を呼ぶ。挙動（重複名チェック・
  循環依存の拒否・チーム新規作成）が2箇所で食い違うことを防ぐための設計であり、
  今後ビューを追加する場合も、DB/シーンへの反映ロジックはここに置く。
- **変更通知は`WorkflowGraphScene.on_changed`フックで一方向に伝える**——
  `reload()`/`auto_arrange()`/`refresh_colors()`の末尾で呼ばれ、
  `WorkflowsTab._on_scene_changed`がタスク表・依存テンプレート表の両方を
  再描画する。ノードビュー側の編集（ドラッグ接続・右クリックメニュー）も
  内部的には必ずこれらのシーンメソッドを経由するため、テーブルビューが
  裏で選択されていても常に最新化される。
- **依存テンプレートの疑似ノードは、タスクの整列に一切影響を与えない**——
  タスク同士の縦方向の並び（アクティブなタスクの流れの見やすさ）を疑似ノードが
  乱さないよう、`compute_combined_layout`はまず`compute_auto_layout`で
  **タスクだけ**を整列し、その結果には手を加えない。疑似ノードは、横方向のみ
  対象タスク（`workflow_task_id`）の1つ上流の列に揃え、縦方向はタスク群の
  最上段よりさらに`TEMPLATE_LAYOUT_MARGIN`分上の帯へ、同じ列同士は`gap_y`
  間隔で積み上げて配置する（他の列のタスクの高さとは重ならない）。
  テーブルビューのタスク行の並び順（`compute_task_depths`経由）は
  この「タスクだけの整列」と同じ深さの考え方を使うため、「ノードビューが
  左→右なら、テーブルビューは上→下」という対応は保たれる
  （疑似ノードの縦位置はテーブル側の行順には影響しない——テーブルビューは
  タスク表と依存テンプレート表を別々に表示するため、後述の通り混在しない）。
- **疑似ノードの座標はDBに保存しない（割り切り）**——`workflow_dependency_templates`
  に座標カラムを追加すればドラッグ配置・永続化もできるが、テンプレートは
  「その場でタスクの1つ上流に自動整列されれば十分」という表示要件しか無いため、
  スキーマ変更のコストに見合わないと判断した。座標を計算する処理自体は
  `compute_template_positions`（タスクの座標 `{workflow_task_id: (x, y)}` を
  受け取り、対象タスクの1つ上流・重ならないよう積み上げた疑似ノードの座標を
  返す純粋関数）に切り出してあり、`compute_combined_layout`（`compute_auto_layout`
  で計算し直したタスク座標を渡す）と`WorkflowGraphScene.reload()`（DB保存済みの
  タスク座標——手動でドラッグしたものを含む——をそのまま渡す）の両方から共有する。
  こうすることで、`auto_arrange()`（タスクの追加・編集・削除や依存エッジ・
  依存テンプレートの追加・編集・削除のたびに呼ばれ、タスクの座標も
  `update_task_position`で保存し直す）だけでなく、`reload()`（ファイルを開いた時・
  ワークフローを選び直した時に呼ばれ、タスクの座標は変更しない）でも、疑似ノードが
  既定位置(0,0)に取り残されてタスクと重なる、という不具合を避けられる。

### 疑似ノードを含む削除とUndoの整合性

タスクを削除すると、そのタスクを`workflow_task_id`として参照する
`workflow_dependency_templates`行はDBスキーマの`ON DELETE CASCADE`により
自動的に連鎖削除される。Undo/Redoはメソッド呼び出しを記録するのではなく、
`undo_group`の前後でDB全体をシリアライズしたスナップショットを比較して
1エントリに積む方式（`gui/db.py`）のため、この種のFK連鎖削除も**追加の実装
無しに**同じUndo単位へ自動的に含まれる。

一方、キャンバス上の疑似ノード・接続線（`TemplateDependencyNodeItem`・
`EdgeItem`）はQtオブジェクトでありDBスナップショットの管理下にないため、
`_delete_node_unconfirmed`が対象タスクを削除する際、そのタスクに紐づく
疑似ノードをDBの連鎖削除とは別に明示的にシーンから取り除く
（`WorkflowGraphScene._remove_template_scene_item`）。

複数選択（タスク・依存関係・依存テンプレート疑似ノードを混在させた選択）を
まとめて削除する`_delete_selected`は、既存の「複数選択の一括操作は1つの
Undo単位にまとめる」規約に従い、種類を問わず1回の`db.undo_group`で処理する。
タスク削除によって疑似ノードが連鎖的に消えている場合（同じタスクとその
疑似ノードを同時に選択していた場合）に二重削除しないよう、依存関係の重複
削除防止と同じ「まだ残っているものだけを処理する」ガードを疑似ノードにも
適用する。ラベルは組み合わせ爆発を避け「選択した項目を削除」に統一している
（対象の内訳によって文言を出し分けない）。

依存テンプレート自体の削除（疑似ノード経由・テーブルビュー経由のいずれも）は、
既存の「依存テンプレート欄の削除は無確認」という仕様を踏襲し、確認ダイアログを
出さない。複数選択の一括削除に含まれていても、この無確認方針・件数計算には
影響しない（参照確認ダイアログの対象はタスクの被参照件数のみ）。

### タスク削除時の依存関係の橋渡し

`_delete_node_unconfirmed`は、削除するタスクの直前・直後のタスクを、削除後も
前後関係が保たれるよう橋渡しの依存関係で繋ぎ直す（A→B→CでBを削除すると
A→Cになる。分岐がある場合は先行タスク全体×後続タスク全体の組み合わせすべてに
橋渡しする）。橋渡し先の依存が既に存在する場合は`try_add_edge`が重複させず
スキップする。

この橋渡しが新たな閉路を生むことはない——削除するタスクが先行タスクから
後続タスクへ至る経路上にあった以上、橋渡し先は削除前から既に到達可能だった
ため（`_would_create_cycle`による事前チェックは働くが、この操作では常に
「既存」判定になるだけで拒否されることはない）。

複数選択でまとめて削除する場合（`_delete_selected`）も、1件ずつ順に処理する
ことで連鎖的な橋渡しが正しく働く——A→B→C→D→EからB・Dを同時に削除すると、
Bの処理でA→Cが繋がり、続くDの処理はその時点の後続関係（C→E）を見るため、
削除する順序に関わらず最終的にA→C→Eになる。ジョブをまたぐ依存
（`job_external_dependencies`）やジョブ側のタスク上書きは橋渡しの対象外
（ワークフロー内の前後関係のみを保つ機能のため）。

ジョブをまたぐ外部依存（`job_external_dependencies`）については、キャンバスの
ような事前チェックは行わず、ガントチャート生成時に
`_build_scheduling_order()`が検出する`CircularDependencyError`をそのまま
GUI側でダイアログ表示する（プロジェクト全体の依存グラフを都度読み直す
コストが見合わないため）。この方針は、依存テンプレートから自動生成された
行（`source_link_id`が非NULL）にも、手動で追加した行にも等しく適用される
——`gantt_generator.py`は`job_external_dependencies`テーブルを区別なく読む
ため、生成元による特別扱いは発生しない。

## 依存テンプレートの自動展開

`gui/tab_jobs.py`でジョブ単位の依存リンク（`job_dependency_links`）を
追加する際、`ProjectDatabase.add_job_dependency_link`が2ジョブそれぞれの
`workflow_id`を引き、`workflow_dependency_templates`から該当するペアの
テンプレート行を検索して`job_external_dependencies`へ`source_link_id`付きで
一括挿入する。

テンプレート自体（`workflow_dependency_templates`）は、ワークフロー間で
循環（WF1→WF2→WF3→WF1のような間接的なものも含む）にならないよう
`ProjectDatabase.add_dependency_template`/`update_dependency_template`が
`_would_create_workflow_template_cycle`で事前にチェックし拒否する
（キャンバス側の`_would_create_cycle`＝ワークフロー内タスクの循環検出とは
別軸・別グラフのチェック）。ただし、ジョブ単位の依存リンクそのもの
（`job_dependency_links`/`job_external_dependencies`）についてはこの
事前チェックの対象外で、他の外部依存と同様にガントチャート生成時の
`CircularDependencyError`検出に委ねる。詳細な自動展開のルールは
`docs/db_design.md`を参照。

## DBの書き込みタイミング（メモリ上のDB＋明示的な保存）

`gui/widgets_common.py`の`CrudSection`をはじめ、GUIの入力系ウィジェットは
セル編集・コンボボックス選択・ノードのドラッグ確定などのイベントごとに、
その場で`ProjectDatabase`のメソッドを呼ぶ。ただし`ProjectDatabase`自体が
`:memory:`のSQLite接続に対して即座にコミットするだけで、実ファイル
（`.pschedule`）への書き込みは行わない。

ファイルへの書き込みは`ProjectDatabase.save()`/`save_as()`を呼んだ時にのみ
発生する。`gui/main.py`がCtrl+S（上書き保存）・Ctrl+Shift+S（名前を付けて
保存）およびFileメニューにこれらを割り当て、`ProjectDatabase.on_change`
コールバック経由で未保存の変更（`is_dirty()`）をタイトルバーに反映する。
新規作成／プロジェクトを開く／ウィンドウを閉じる際に未保存の変更があれば、
保存するか破棄するかを確認するダイアログを出す。

### 保存は一時ファイル経由で原子的に置き換える

`_write_to()`は、書き込み先と同じフォルダに一時ファイルを作ってそこへ書き出し、
成功してから`os.replace()`で本来の名前に置き換える。書き込み先を先に削除して
から書く実装だと、途中で失敗した場合（ディスク満杯・権限エラー・書き込み中の
クラッシュ等）に**保存済みの内容ごと失われる**——Undo履歴はメモリ上にしか
無いため、こうなると復旧手段が無い。一時ファイル経由なら、失敗しても既存の
保存済みファイルは無傷のまま残る。

一時ファイルを同じフォルダに作るのは、`os.replace()`が同一ファイルシステム上で
しか原子的に置き換えられないため（テンポラリ領域が別ドライブにあると保証が
崩れる）。

### DBを閉じる順序

`gui/main.py`がプロジェクトを開き直す／ウィンドウを閉じる際は、**タブを片付けて
から旧DBを閉じる**。タブの差し替えでは入力欄からフォーカスが外れ、
`editingFinished`等のシグナルが発火して旧タブが自分の持つDBへ書き込もうと
するため、先に閉じると`Cannot operate on a closed database`になる
（Qtがスロット内の例外を握りつぶすため、画面上は無害に見えてしまう）。

## マイルストーンの整合性（不変条件と、それが後から崩れる経路）

ジョブのタスクに設定するマイルストーンは、次の不変条件を満たす必要がある。

> 同一ワークフロー内で、**先行タスクの実効マイルストーンの締切 <= 後続タスクの
> 実効マイルストーンの締切**（実効＝タスク上書きがあればそれ、無ければジョブの
> 既定マイルストーン。`effective_milestone`）

これが崩れると、後続タスクの締切が先行タスクより早いという実現不可能な計画に
なる。スケジューラは例外を出さず、ALAP計算（`_calc_raw_dates`の
`t_end = min(succ_starts, ms_end)`）で後続側の早い締切に引きずられた日程を
黙って作るため、**画面上でもガント出力上でも不整合に気付けない**。

タブ3（ジョブ作成）はこの不変条件を編集時に3つの仕掛けで守っている。

- 選択肢のフィルタ（`minimum_milestone_end_date`で先行タスクの締切より早い
  ものを除外）
- `enforce_milestone_floor`（自タスクが先行より早くなったら引き上げ）
- `cascade_milestone_to_successors`（後続が自分より早いままなら繰り下げ）

問題は、**いずれもタブ3で上書きを編集した瞬間にしか走らない**こと。不変条件の
判定材料は「マイルストーンの締切日」と「依存グラフ」であり、これらは上書きを
触らずに他タブから変えられる。そのため次の2経路で、後から静かに崩れる。

1. **タブ1でマイルストーンの締切日を変え、前後関係が入れ替わる**
   （`update_milestone`は日付を書き換えるだけで、ジョブ側を一切見ない）
2. **タブ2で、既にマイルストーンを設定済みのタスク間に依存関係を追加する**
   （マイルストーンの日付は変えていないのに、判定対象の組が増える）

崩れた後の実害は不整合そのものにとどまらない。タブ3を開き直すと選択肢の
フィルタが現在値を弾き、`make_fk_combo`の`findData`が-1になってコンボが
先頭の「（既定: …）」へフォールバックする。この状態で同じ行の日数やチームを
触ると`_on_override_changed`がその表示値（None）を書き戻し、**マイルストーンの
上書きが無言で消える**（データ損失）。

### 対処: 検査（plan）と適用（apply）の2段構え

- `plan_milestone_consistency_repair()` — 全ジョブを走査し、引き上げ対象と
  「現在→調整後」を返す。**書き込みは一切しない**。判定は
  `enforce_milestone_floor`と同じ規則を依存の深さ順に伝播させたもので、
  引き上げ結果がさらに後続へ波及する点（cascade相当）も再現する。
- `apply_milestone_consistency_repair(plan)` — 受け取った計画をそのまま適用。

計画と適用を分けているのは、**確認ダイアログに出した内容と実際に適用される
内容を必ず一致させる**ため（シミュレーションと本適用でロジックが分岐しない）。

GUI側は`confirm_and_repair_milestone_consistency()`（`gui/widgets_common.py`）が
両経路から共有される。呼び出し側の`db.undo_group`がまだ開いているうちに呼ぶ
規約にしてあり、再調整はきっかけの編集と同じ1エントリに合流する（＝1回のUndoで
全部戻る）。キャンセルされた場合は呼び出し側がきっかけの変更を取り消す——
同じ単位の中で差し引きゼロになるため、Undoエントリも積まれない。

タブ1の締切日欄は日付が1回の編集で何度も変わるため、確認は
`bind_undo_session(..., on_before_commit=...)`（Undo単位が閉じる直前に呼ばれる
フック）で編集の確定時に1回だけ行う。`on_session_end`ではUndo単位が既に
閉じており、そこでDBを変更するとUndoが2回に分かれてしまうため使えない。

なお、**マイルストーンの設定可能な日付を前後のマイルストーンで制限する**案は
採らなかった。マイルストーンには`sort_order`のような明示的な順序を持たせて
おらず順序は日付から導出されるため「前後」の定義が循環すること、無関係な
マイルストーン同士（並行する2プラットフォームのリリース等）が互いを拘束して
しまうこと、順序の入れ替えを伴う正当な計画変更ができなくなること、そして
何より**上記2の依存追加経路を塞げない**ことが理由。加えて、タブ3側の
「現在値を選択肢から外さない」防御（`gui/tab_jobs.py`）も併用し、既に不整合な
状態のファイルを開いた場合でもデータ損失だけは起こさないようにしている。

## Undo/Redo（DBスナップショット方式）

GUIで編集できる項目はすべてUndo/Redoで元に戻せる。個々の操作ごとに「逆操作」を
書く方式（コマンドパターン）は採らず、**操作の前後でDB全体のバイト列
スナップショット（`sqlite3.Connection.serialize()`）を取り、1エントリとして
積む**方式にしている。`sync_dependency_templates`やマイルストーンの前後整合
（`cascade_milestone_to_successors`/`enforce_milestone_floor`）のように連鎖的な
副作用を持つ操作が多く、逆操作を個別に書くと書き漏れの温床になるためで、
この方式なら連鎖もまとめて1回で正しく戻る。

- **記録の単位**: `ProjectDatabase`の変更系メソッドはすべて`@undoable("ラベル")`
  で包まれ、内部は`undo_group()`に委譲される。ネストした呼び出し（例:
  `add_job_dependency_link`→`sync_dependency_templates`）は外側のスナップショットに
  合流し、1エントリにまとまる。GUI側で複数のDB呼び出しにまたがる1操作
  （例: タスク追加→自動レイアウト）は、呼び出し側が`with db.undo_group(...)`で
  明示的にまとめる。**複数選択に対する一括操作（キャンバスでの複数ノード削除等）も
  1単位とする**——選択項目ごとにUndoが分かれると、1回のDeleteを取り消すのに
  複数回のUndoが必要になってしまうため。
- **連続した値編集はフォーカス単位でまとめる**: スピンボックスや日付欄は1回の編集で
  値が何度も変わるため、変更ごとにUndoが分かれると「▲を5回押したのに5回Undoしないと
  戻らない」ことになる。これらは`bind_undo_session()`（`gui/widgets_common.py`）で
  フォーカスの出入りを`db.begin_undo_group()`/`end_undo_group()`——複数のイベントに
  またがって開いたままにできるUndo単位——に繋ぎ、**フォーカスを得てから外れるまでを
  1エントリ**にする。値が結局変わらなければ何も記録しない。

  DBへの書き込み自体は従来どおり変更のたびに行う。「フォーカスが外れた時にまとめて
  書き込む」方式にしないのは、Ctrl+Sがフォーカスを移動させないため、値を変えた直後に
  保存すると変更前の値が保存されてしまうため。編集途中のままUndoや保存が行われた
  場合は、その時点で編集を1単位として確定させる。

  この方式は「編集中に入力欄が作り直されない」ことが前提になる。ジョブのタスク上書き表は
  従来、変更のたびに表全体を作り直していたため、スピンボックスごと差し替わって連続操作が
  できなかった。他の行へ影響が及ぶマイルストーンの変更時だけ作り直すよう改めている。
- **記録漏れの防止**: 2つの経路で担保する。(1) `_commit()`は`@undoable`/
  `undo_group`の外側から呼ばれると例外を送出する。(2) `tests/test_undo_redo.py`が、
  変更系の命名（`add_`/`update_`/`delete_`等）を持つ公開メソッドすべてに
  `@undoable`が付いていることを検査する。(1)は`self._conn.commit()`を直接呼ぶ
  実装をすり抜けるため、静的な検査である(2)を併用している。逆に(2)は命名規約から
  外れたメソッドを拾えないため、両方が必要。
- **前後対称のエントリ**: 1エントリは操作の「前」「後」両方のDBスナップショットと
  UI状態（選択・アクティブタブ）を持ち、Undoは前を、Redoは後を
  復元する。「後」のUI状態だけは、DB更新に続く表の作り直しと再選択まで終わってから
  取りたいため、`QTimer.singleShot(0, ...)`で現在のイベント処理の後に取得する。
  これにより、操作したタブとは別のタブに移ってからUndo/Redoしても、どちらも
  操作を行ったタブへ戻って当時の選択を復元する。
- **フォーカスは復元しない**: 復元するのは選択（表は行と列、キャンバスはノード・
  エッジ、アクティブタブ）までとし、フォーカスは動かさない。スピンボックス・
  日付欄・テキスト欄はいずれもCtrl+Zを自分のものとして横取りする
  （`QAbstractSpinBox`/`QLineEdit`が`ShortcutOverride`を受け取る）ため、Undoのたびに
  フォーカスをそれらへ戻すと、次のCtrl+Zがメニューまで届かず「Undoが効かなく
  なった」ように見える。なお表の現在セルを移動させるだけでも、その列にセル
  ウィジェットがあるとQtがそこへフォーカスを移すため、`restore_table_state()`は
  移ってしまったフォーカスを元の位置へ戻している。`QTreeWidget`（タブ1の
  チームツリー等）でセル位置にウィジェットを置いている場合も同じ現象が起きる
  ため、`gui/widgets_common.py`の`set_current_tree_item_keeping_focus()`が
  同じ考え方で選択変更後のフォーカスを元に戻す（`setCurrentItem()`を直接
  呼ぶ代わりに使う）。これを怠ると、フォーカスがセルウィジェット内に残った
  まま`begin_undo_group()`だけが開始され、対応する`focusOutEvent`が来ないまま
  DB接続だけ閉じられるような経路（アプリ終了時等）でクラッシュしうる。

  `set_current_tree_item_keeping_focus()`はこの往復のためにセルウィジェットへ
  一瞬フォーカスを入れてすぐ戻すため、`bind_undo_session()`の
  `focusInEvent`/`focusOutEvent`（`begin_undo_group`/`end_undo_group`）も
  素通りで発火する。`end_undo_group()`は「実際に内容が変わったか」を`bool`で
  返し、`_UndoSessionMixin.focusOutEvent`はこれが`True`の時だけ
  `on_session_end`コールバックを呼ぶ。こうしないと、値を何も変えていない
  プログラム的な選択変更のたびに、並べ替え用の再構築
  （例: `gui/tab_basic_info.py`の`_resort_team_capacity_changes_later`）が
  無条件に走ってしまい、選択・スクロール位置を失う（＝直前に選択・追加した
  行を見失う）。
- **キャンバスの選択はタブ切り替えでも維持する**: ノードグラフの選択・
  フォーカスと違い、ノード・依存関係の視覚的な選択状態は、Undo/Redoの対象で
  なくても失われるべきではない。ワークフロー選択のたびに`WorkflowGraphScene`を
  作り直す（`WorkflowsTab._on_selection_changed`）ため、何もしないと選択が
  消えてしまう。`WorkflowsTab._capture_canvas_selection`/
  `_restore_canvas_selection`を、Undo/Redo用の`capture_ui_state`/
  `restore_ui_state`と、通常のタブ切り替え時の`refresh_choices`の両方から
  共通で使うことで、どちらの経路でも選択を保つ。
- **適用中の書き込みの抑止**: Undo/Redoの適用は、スナップショットの復元に続けて
  アクティブなタブの`refresh_choices()`まで行う。この再読込がDBに書き込むことが
  あるため（`gui/tab_jobs.py`の`refresh_choices()`は`sync_dependency_templates()`を
  呼ぶ）、`db.suspend_undo_recording()`で囲み、ユーザーの新しい操作と誤認して
  Redoスタックを破棄してしまうことを防いでいる。
- **タブへの依存を持たない**: `MainWindow`は特定のタブ名を知らず、
  `refresh_choices()`と同じ規約で`capture_ui_state()`/`restore_ui_state(state)`を
  実装しているウィジェットだけを対象にする。新しいタブや、ガントチャートタブへの
  編集機能追加でも、この2メソッドを実装すれば自動的にUndo/Redoの選択復元に
  参加できる（未実装でもDB内容のUndo/Redo自体は機能する）。
- **未保存マークとの連動**: `is_dirty()`は単純な「変更したか」のフラグではなく、
  Undo履歴上の位置で判定する（`UndoManager.is_clean()`）。保存した時点のエントリを
  覚えておき、そこに戻っていれば保存済みと見なすため、変更→保存→変更→Undoで
  未保存マークが消える。スナップショット同士を比較する方法もあるが、変更のたびに
  DB全体を突き合わせることになるため、位置で見る方を選んでいる。
- **メモリ使用量**: 1エントリは操作前後2つのスナップショット（移行済みサンプルで
  1つ約124KB）を持つが、連続した操作では「直前のエントリの後」と「今回の前」が
  同じ内容になるため、同じbytesオブジェクトを共有させて実際の保持数を
  エントリ数+1に抑えている。上限は段数（100段）と合計バイト数（128MB）の
  両方で設ける——プロジェクトが大きくなるとスナップショット1つが大きくなり、
  段数だけでは使用量が読めないため。
- **割り切り**: 履歴はプロジェクトファイルを開く/新規作成するたびに破棄する
  （ファイルをまたいだUndoはしない）。保存では消えない。上限超過で保存時点の
  エントリが捨てられた場合は、以降「保存時と同じ内容か」を判定できなくなるため
  未保存扱いに倒す。

## リソースヒストグラム（`gui/resource_histogram.py`）

タブ1「基本情報設定」のチーム欄（ツリー表示）と連動する、チームの
同時ライン数の推移グラフ。計算部分を2段階に分けているのは、将来ガント
チャートタブで「実際のタスクスケジューリング結果（リソース使用状況）」を
表示する機能を追加する際に、描画ロジックをそのまま使い回せるようにするため
——現時点ではそちらは未実装。

- `compute_step_segments(breakpoints, range_start, range_end)` は、
  「日付→値」の変化点リストから階段関数の区間を作るだけの汎用関数で、
  チームの計画容量固有の知識を一切持たない。`team_capacity_breakpoints`
  （`teams.max_lines` + `team_capacity_changes`から変化点を作る、チーム容量
  専用の関数）とは意図的に分離してあり、将来「日ごとの実タスク稼働数」から
  同じ形の変化点リストを作る関数を追加すれば、`compute_step_segments`と
  `build_histogram_scene`（描画）はそのまま流用できる想定。
- 描画は`gui/gantt_view.py`と同じ、素のQGraphicsScene直接描画方式を踏襲する
  （プロット用ライブラリを新たに導入しない——`plotly`は`project_scheduler.py`の
  静的HTML書き出し専用で、アプリ内描画では使っていないという既存方針に合わせる）。
  ズーム・パン・A/Fキーは`gui/gantt_view.py`の`GanttGraphicsView`をそのまま
  継承し（`ResourceHistogramView`）、フィット処理だけ`gui/node_canvas.py`の
  `WorkflowGraphView.fit_all`と同じ自己完結パターンで追加する
  （ヒストグラムのバーは個別選択できない仕様のため、`fit_selected`は
  `fit_all`と同じ挙動にしている）。ヘッダー・左列を固定表示する
  `FrozenGanttPane`相当の仕組みは導入していない（表示期間・行数がガント
  チャートほど大きくならないため、v1では単一シーンで十分と判断した）。
- **表示期間の割り切り**: X軸の範囲は開発開始日から、既知のマイルストーン
  締切日・チームの容量変更点のうち最も遅い日付までとする
  （`histogram_axis_range`）。どちらも無いプロジェクトでは、期間の長さを
  決める材料が無いため開発開始日+90日をフォールバックとして使う。
  ユーザーが調整できる設定にはしていない。
- **未保存の座標を持たない**: ノードグラフの疑似ノード（`docs/architecture.md`
  「ノードビュー／テーブルビューの切り替えと依存テンプレートの疑似ノード」参照）
  と同様、ヒストグラムの見た目上の座標はDBに保存せず、表示のたびに計算し直す
  （保存すべき状態はチームの容量設定そのものであり、グラフの座標はその都度の
  派生表示でしかないため）。
- **チーム名ラベル**: `build_histogram_scene`は、系列（チーム）ごとに
  最も横幅が広い区間を選び、その中央にチーム名を1つだけ描画する（短い
  区間が並ぶ場合に文字が重なって読めなくなるのを避けるため）。位置合わせは
  `gui/gantt_view.py`の年ラベル（`_add_fixed_size_label` + `QFontMetrics`で
  幅を測って中央寄せ）と同じ、シーン座標に対する簡易な近似であり、ズーム後に
  再センタリングするような追従処理は行っていない（表示期間・行数の規模的に
  ガントチャート本体ほどの精度は不要と判断）。
- **タブ1側のレイアウト**: チームツリーとヒストグラムは、`gui/tab_basic_info.py`
  側で1つの`QGroupBox("チーム")`の中に`QSplitter`（既定比率3:7）で並べており、
  `resource_histogram.py`自体はどちらの枠に置かれるかを知らない（描画・計算
  ロジックとレイアウトを分離するという、このモジュールの既存方針のまま）。
- **上方向の余白**: 最も高いバーの上端が目盛りエリアの上端に接して窮屈に
  見えないよう、Y軸のスケールには`TOP_PADDING_ROWS`（既定1行分）を常に
  上乗せする（Y軸の目盛り自体は実際の最大値までしか描かないため、この余白に
  目盛り線は増えない）。
- **上余白はデータ量に応じて拡大し、ラベルはシーン最上部に固定する**
  （`_compute_top_margin`、`_LABEL_TOP`）: マイルストーンラベルは
  `ItemIgnoresTransformations`で常に一定ピクセル数のまま描かれるが、上余白
  （`TOP_MARGIN`）が固定値のままだと、全チーム積み上げの合計値が大きい
  （バーの縦幅が大きい）プロジェクトでは`fit_all()`が縦方向を大きく縮小する
  ため、縮小されないラベルに対し余白だけが縮み、実際の画面上でラベルが
  バーと重なって見えてしまう（回帰バグ）。上余白をバー全体の高さ
  （`TOP_PADDING_ROWS`込み）に対する一定割合（`TOP_MARGIN_MIN_FRACTION`、
  既定12%）を下限として確保することで、`fit_all()`後の表示倍率によらず、
  実際の画面上の余白がビューポート高さに対して概ね一定の割合を保つように
  している（縮小前のシーン座標での比率を保てば、縮小後の実ピクセルでの
  比率も保たれるため）。

  **ここで重要なのが、ラベル自体の位置**: 当初はラベルを`top_margin`の
  下端からの固定オフセット（例: `top_margin - 14`）に置いていたが、これでは
  `top_margin`をいくら大きくしても、ラベルからバーまでのシーン座標上の
  間隔は常に一定（`TOP_PADDING_ROWS * ROW_UNIT_HEIGHT`のみ）のままで、
  拡大した余白が全く活用されず、実際には何も改善しないという不具合があった
  （`top_margin`が動くとラベルの位置も一緒に追従してしまうため）。正しくは、
  ラベルをシーンの最上部近くの固定位置（`_LABEL_TOP`）に置くことで、
  `top_margin`が拡大するほどラベル・バー間の間隔（＝ほぼ`top_margin`
  そのもの）も連動して広がるようにする。マイルストーン・開発開始日の
  縦線も、ラベルと視覚的につながって見えるよう同じ`_LABEL_TOP`から
  描き始める。合計値が小さい既存のプロジェクトでは、この下限が既定値
  （`TOP_MARGIN`）を下回るため見た目は変わらない。
- **マイルストーンの縦線はバーより前面**: `gui/gantt_view.py`と同じ幅2の
  破線で描画し、`_grid_line`にzValue（`_MILESTONE_LINE_Z`）を渡してバー
  （既定のzValue=0）より手前に重ねる。バーの内側を横切っても線が隠れず、
  常にどのバーがどのマイルストーン日をまたぐか視認できるようにするため。
- **チーム選択の解除**: チームツリーで1件選択すると単独表示になるが、
  `QTreeWidget`は空欄クリックで選択（ハイライト）は解除されても
  `currentItem()`は古い項目を指したまま残る。ヒストグラムの表示切り替えは
  `currentItem()`ではなく実際の選択状態（`selectedItems()`）を見るように
  している。また、チーム欄（ツリー＋ツールバー、`BasicInfoTab._teams_panel`）
  の外をクリックした場合も選択を解除し、全チーム表示に戻す
  （`QApplication`単位のイベントフィルタ、`BasicInfoTab.eventFilter`）。
