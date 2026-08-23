# ProjectScheduler

リソース制約付きプロジェクトスケジューラー。プロジェクト/マイルストーン/
チーム/ワークフロー/ジョブ定義から、チームのライン数（同時並行キャパシティ）
と休業日を考慮した現実的なスケジュールを算出し、Mermaidガントチャート
（Markdown）と、サーバー不要でブラウザから直接開けるインタラクティブな
ガントチャート（単一HTMLファイル、Plotly製・チーム別色分け）として出力する。

入力データの作成には2通りの方法がある:

- **GUI（推奨）**: PySide6製のデスクトップアプリ（`gui/`）。IDを一切意識せず、
  名前のドロップダウン選択とノードグラフでの依存関係編集だけでデータを組み立て
  られる。保存形式はSQLite（`.pschedule`）で、GUIから直接ガントチャートを生成
  できる（Excelを経由しない）。詳しくは [GUIの使い方](#guiプロジェクトスケジューラーgui) を参照。
- **Excel + CLI**: 多シート構成のExcelを直接編集し、`project_scheduler.py`を
  コマンドラインから実行する（下記「使い方」）。

## セットアップ

```bash
pip install -r requirements.txt
```

## GUI（プロジェクトスケジューラーGUI）

```bash
python run_gui.py
```

起動したら「ファイル → 新規プロジェクト...」で `.pschedule` ファイルを新規作成
するか、「ファイル → プロジェクトを開く...」で既存のファイルを開く。
サンプルデータをすぐ試したい場合は、リポジトリに同梱の
[`data/Project_Schedule_Sample_GameDev_v22.pschedule`](data/Project_Schedule_Sample_GameDev_v22.pschedule)
を開く（既存のExcelサンプルを`scripts/migrate_excel_to_db.py`で変換したもの）。

3タブ（基本情報設定／ワークフロー設計／ジョブ）でデータを入力し、
`.pschedule`ファイルをウィンドウにドラッグ&ドロップして開くこともできる。
「ファイル → ガントチャートを生成...」から出力先フォルダを選べば、CLI版と
同じ Mermaid Markdown / インタラクティブHTML の2形式が書き出される。
保存は各操作のたびに自動で行われる（明示的な保存ボタンはない）。

詳細な要件・画面仕様・アーキテクチャ・DB設計は [`docs/`](docs/) を参照:

- [`docs/requirements.md`](docs/requirements.md) — 要件定義
- [`docs/screens.md`](docs/screens.md) — 画面仕様（各タブの操作・バリデーション）
- [`docs/architecture.md`](docs/architecture.md) — アーキテクチャ
- [`docs/db_design.md`](docs/db_design.md) — DB設計（`.pschedule`のテーブル定義）
- [`docs/packaging.md`](docs/packaging.md) — Windows `.exe` パッケージング手順

### Windows向け `.exe`

GitHub Actions（`.github/workflows/build-exe.yml`）が `windows-latest` 上で
自動ビルドする。Actionsの実行結果から `ProjectSchedulerGUI-windows`
Artifactをダウンロードすれば、Pythonのインストール無しで実行できる。
手元のWindows環境でビルドする手順も含め、詳細は
[`docs/packaging.md`](docs/packaging.md) を参照。

## 使い方（Excel + CLI）

```bash
python project_scheduler.py [Excelファイル] -o output/schedule_gantt.md --html-output output/schedule_gantt.html
```

引数を省略すると `data/Project_Schedule_Sample_GameDev_v22.xlsx`
（ゲーム開発案件のサンプルデータ）を使ってスケジューリングを実行する。

サンプルデータを実行した出力例は `samples/` 配下にコミット済み:

- [`samples/schedule_gantt.md`](samples/schedule_gantt.md) — Mermaidガントチャート
  （GitHub上でそのままレンダリングされる）
- [`samples/schedule_gantt.html`](samples/schedule_gantt.html) — インタラクティブHTML
  ガントチャート（ダウンロードしてブラウザで開く。GitHubのファイルビューでは
  ソースのまま表示される点に注意）

主なオプション:

- `--html-output`: インタラクティブなHTMLガントチャートの出力先
  （既定 `output/schedule_gantt.html`。空文字を指定すると出力しない）
- `--tick-interval`: Mermaidガントチャートの目盛り粒度（既定 `1week`）
- `--distribution-ratio`: 各タスクをASAP(0.0)〜ALAP(1.0)のどのあたりに
  配置するかの基準点（既定 `0.7`）
- `--highlight-resource-adjusted`: リソース制約により前倒しされたタスクを
  赤色（crit）表示する（Mermaid版のみ）

コードから直接呼び出す場合は `run_resource_constrained_scheduler()` を使う
（詳細は `project_scheduler.py` のdocstringを参照）。

### インタラクティブHTMLガントチャート（`--html-output`）

`output/schedule_gantt.html` はブラウザにドラッグ&ドロップするだけで開ける
単一ファイル（Plotly.jsを埋め込み済みでオフラインでも動作、サーバー不要）。

- **ワークフローごとに別々のガントチャートに分割**する（Mermaid版と同様）
- 各チャート内は**ジョブ単位で1行**にまとめる。ジョブ名は1回だけ表示し、
  時間的に重なるタスクがある場合だけ行（レーン）を追加する（重ならない
  タスクは同じ行に詰める）。ジョブの境界には横線を入れて区切る
- 各チャート内のジョブは、**作業開始が早い順に上から**並べる
- 各バー内にタスク名を表示し、**担当チームごとに色分け**する
- 画面上部の「チームで絞り込み」パネルでチームのチェックを外すと、その
  チームのタスクを全チャートから除外し、**レーンを詰め直して行の高さも
  縮める**（凡例クリックのように非表示分の余白が残ったままにはならない）
- バーにマウスを乗せるとジョブ名・タスク名・ワークフロー・優先度・開始/終了日・
  リソース調整の有無をツールチップ表示する
- プロジェクト開始日と各マイルストーンを縦の破線で表示する（表示期間は
  全チャート共通）

チーム色は固定8色のカテゴリカルパレット（色覚多様性シミュレーション下でも
隣接色を判別できるよう検証済みの配色）を `Teams` シートの行順に割り当てる。
9チーム目以降は無彩色にフォールドする（色は識別の補助であり、チーム名は
常に凡例・ツールチップのテキストでも確認できるため実用上問題ない）。

## 入力フォーマット

Excelブック内の各シート:

| シート | 必須 | 内容 |
| --- | --- | --- |
| `Project` | ○ | プロジェクト情報（1行）。`Start_Date` は全タスク共通の絶対下限日 |
| `Milestones` | ○ | マイルストーンID・締切日（`End_Date`） |
| `Teams` | ○ | チームID・同時並行可能ライン数（`Max_Lines`） |
| `Workflows` | ○ | ワークフロー単位のタスク定義（所要日数・依存関係・担当チーム） |
| `Jobs` | ○ | ジョブ（実際の制作物）とその優先度（`Priority`） |
| `Job_Tasks` | 任意 | ジョブ単位でのタスク上書き（無効化・日数上書き等） |
| `Holidays` | 任意 | 全社共通 or チーム別の休業日 |
| `External_Dependencies` | 任意 | ジョブ・ワークフローをまたぐタスク間依存 |
| `Workflow_Names` | 任意 | ガントチャート見出し用のワークフロー表示名 |

詳細な列定義はサンプルファイル `data/Project_Schedule_Sample_GameDev_v22.xlsx`
内の `README` シートを参照。

## スケジューリングの考え方

1. 依存関係のみを考慮した最速日程（ASAP）と、マイルストーンの締切から
   逆算した最遅日程（ALAP）を求め、各タスクの「動かせる幅（スラック）」を把握する。
2. `distribution_ratio` に応じてASAP〜ALAPの間に配置の基準点を置き、
   チームの空きライン数を考慮しながら日程を確定する。基準点に空きがなければ
   締切側へ自動的に探索範囲を広げるため、マイルストーンの締切には必ず間に合う。
3. 土日・日本の祝日（自動計算）・`Holidays` シートで指定した休業日は
   稼働日としてカウントしない。
4. 循環依存やリソース不足で締切に間に合わない場合はエラーとして検出する
   （`CircularDependencyError` / `ResourceOverflowError` 等）。

出力されるMarkdownには、ワークフロー別・チーム別2種類のMermaidガントチャートが
含まれ、いずれもプロジェクト開始日と各マイルストーンを◆マークで表示する。
