# ProjectScheduler

リソース制約付きプロジェクトスケジューラー。Excelで管理する
プロジェクト/マイルストーン/チーム/ワークフロー/ジョブ定義から、
チームのライン数（同時並行キャパシティ）と休業日を考慮した
現実的なスケジュールを算出し、Mermaidガントチャート（Markdown）と、
サーバー不要でブラウザから直接開けるインタラクティブなガントチャート
（単一HTMLファイル、Plotly製・チーム別色分け）として出力する。

## セットアップ

```bash
pip install -r requirements.txt
```

## 使い方

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

- **ジョブ単位で1行にまとめる**（Mermaid版のcompact表示と同じ考え方で、
  時間的に重ならないタスクは同じ行に詰め、重なるタスクだけ行を分ける）。
  タスク単位では行が増えすぎるため、縦方向を大幅に圧縮している
- 各バー内にタスク名を表示し、**担当チームごとに色分け**する
- 凡例をクリックするとそのチームのタスクだけ表示/非表示を切り替えられる
  （簡易フィルタリング）
- バーにマウスを乗せるとジョブ名・タスク名・ワークフロー・優先度・開始/終了日・
  リソース調整の有無をツールチップ表示する
- ズーム・パンで期間を絞り込める
- プロジェクト開始日と各マイルストーンを縦の破線で表示する

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
