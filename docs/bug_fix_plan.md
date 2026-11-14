# 不具合・問題点と修正計画（2026-11-14 時点の調査メモ）

調査は途中で打ち切った。**GUIのウィジェット層（各タブ・ガント描画）はコードを読めていない。**
下記は「確認できたこと」と「未確認の疑い」を分けて書く。

## 調査したこと

| 対象 | 方法 | 結果 |
| --- | --- | --- |
| 既存テスト | `python -m pytest`（core 162 / scheduler 211 / gui 269） | scheduler の1件だけ失敗（P1）。ほかは全件成功 |
| スケジューラ本体 | ランダムなプロジェクト約900件で、依存・ライン数・稼働日・日数・決定性を検査 | 違反・異常終了なし |
| 計画の確定・変更案・破棄 | ランダムな編集列 約740件で、確定直後の再計算の一致・破棄後の復元を検査 | 不一致・異常終了なし |
| 極端な値（日数9999、1752年、2199年） | pandas 2.2 / 3.0 の両方で実行 | 異常終了なし |
| DB層の入力検証 | 境界値を直接APIに渡す | 検証漏れあり（P3） |
| 静的解析（ruff） | 本体コード | 重大な指摘なし |

## 確認できた問題

### P1. テストが実行日に依存して失敗する（確認済み）

- `tests/test_plan_confirmation.py::test_editing_a_task_moves_only_it_and_its_successors` が今日（2026-11-14）失敗する。
- 原因: `gui/plan_actions.py:25` の `_now()`（実際の現在時刻）が確定日時になり、`gui/gantt_generator.py:506` でそれが置き直すタスクの下限日になる。テストのデータは2026年の固定日付なので、実行日によって結果が変わる。
- 確定日時を 2026-04-01 / 2026-10-06 / 2027-03-01 に固定すると 373件すべて成功することを確認した。
- 修正: `tests/` に `gui.plan_actions._now` を固定日時へ差し替える autouse フィクスチャを置く（`conftest.py`）。GUIテスト側で同じ経路を使う箇所も同じ値に固定する。
- 検証: 確定日時を複数の日付に変えて `pytest -m scheduler` が通ること。
- アプリ本体の変更ではないので、バージョンは上げない。

### P2. 循環GCを止めたまま戻していない（事実は確認済み、影響の大きさは未確定）

- `gui/schedule_cache.py:42` の `gc.disable()` がアプリ全体に効き、`gc.collect()` を呼ぶ箇所はどこにも無い。
- 測定: サンプル（GameDev）を12回開き直すと RSS が 369MB（9回目）→ 436MB（12回目）と増え続けた。ただし最後に `gc.collect()` しても回収は2112個で RSS は減らなかったので、**増加の主因が循環参照だとは確認できていない**（別の保持経路の可能性がある）。
- 修正案: (1) 計算スレッドが動いていない時点（`ScheduleCache._on_worker_thread_finished` の後、スレッド一覧が空のとき）にGUIスレッドで `gc.collect()` する。(2) 開き直しでメモリが増える原因を別途 `tracemalloc` 等で特定する。
- 検証: 上の開き直しの測定を再実行して RSS が頭打ちになること。`pytest -m gui`。パッチを1つ上げる。

### P3. DB層の日付・数値の検証漏れ（確認済み。GUIからは通常届かない）

GUIの入力欄が範囲を絞っているため、手で作ったファイルやAPI直接呼び出しでだけ起きる。

- 日付を検証しないメソッド: `set_project`、`add/update_milestone`、`add/update_holiday`、`add/update_team_capacity_change`（`gui/db.py`）。
  - 不正な文字列（`"xx"` 等）は文字列比較で「2199-12-31 より後です」という的外れなエラーになる（`gui/gantt_generator.py:56` `_dates_out_of_range`）。
  - 休業日 `"2026-4-7"` は `build_display` の `date.fromisoformat` で `ValueError`。
  - 下限の検査が無い: 開発開始日 `0001-01-01` は `ValueError`、マイルストーン `1600-01-01` は `OverflowError`。
- CHECK制約の違反（ライン数 -1、日数 0、優先度 -1 など）が `DuplicateNameError`（「名前は既に使用されています」）として報告される（`gui/db.py:681` ほか、`IntegrityError` を一律に重複名へ変換している）。
- 優先度に文字列、日数に小数（1.5）を受け付ける。優先度 `"abc"` は計算時に `ValueError`。
- 修正: 既存の `_parse_iso_date` を上記メソッドに通し、下限（例 1900-01-01）も `validate_for_generation` に足す。`IntegrityError` は UNIQUE 違反のときだけ重複名にし、ほかは値の範囲のエラーにする。文言を足すので `locales/` 3言語も同じコミットで更新。
- 検証: `tests/test_db_robustness.py` に各ケースを足し、`pytest -m "not gui"`（DB層の変更なので上位まで）。パッチを1つ上げる。

### P4. 細かい点（コードを読んで確認）

- `gui/main.py:140`、`gui/main.py:427`: 拡張子の判定が大文字小文字を区別する。Windowsで `FOO.PSCHEDULE` をドロップしても開けず、保存では `foo.PSCHEDULE.pschedule` になる。→ `.lower()` で比べる。
- `gui/main.py:819` `main()`: `sys.excepthook` もログファイルも無い。`.exe` は `console=False` なので、スロット内の想定外の例外は利用者にも開発者にも見えない。→ 例外をログファイルに書き、ダイアログで知らせる。
- `gui/main.py:491`: HTML出力は常に `schedule_gantt.html` で、既存ファイルを確認なしに上書きする。→ 上書き確認、またはファイル名を選ばせる（仕様の判断が要る）。
- `project_scheduler.py:99`: import 時に `logging.basicConfig` を呼び、ルートロガーを設定してしまう。→ 起動側（`gui/main.py`）へ移す。
- `.github/workflows/`: テストを回すCIが無い（ビルドだけ）。P1のような失敗に気付けない。→ `pytest` を回すワークフローを足す。
- `CLAUDE.md` の行数の記載が古い（`gui/db.py` 1550行→実際2772行、`project_scheduler.py` 1839行→2597行、`gui/node_canvas.py` 1209行→1580行）。

## 未確認（疑いだけ。次に調べる）

- 計算が失敗したとき `ScheduleCache._set_error` が `updated` を出し、タブ側の `_on_cache_updated` が `ensure_fresh()` を呼び直して同じ計算を繰り返さないか（`gui/tab_gantt.py:604-607`、`gui/tab_analysis.py:393-410` を未読）。
- 追い越された計算スレッドは止めずに最後まで走る（`ScheduleCache._start_worker`）。大きな計画で続けて編集したときの積み上がり。
- 日数9999のタスクで終了日が2877年になる計画を、ガント描画・HTML出力が扱えるか。
- 同じファイルを2つのウィンドウで開いたとき、後から保存した方が黙って上書きする（ロックも変更検知も無い）。

## 進め方（提案）

1. P1（テストだけ。すぐ直せる）
2. P4のCI追加（以後の失敗を検知できるようにする）
3. P3 → P4の残り（まとめて1つのPR、パッチを1つ上げる）
4. P2（原因の特定が先）と「未確認」の調査
