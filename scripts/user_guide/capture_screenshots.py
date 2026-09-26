"""利用者向けドキュメント（docs/user_guide/）に載せる画面を自動で撮影する。

ドキュメント用のサンプル（scripts/user_guide/generate_guide_samples.py が生成）を
開いた状態で、SHOTS に並べた場面を順に撮り、
docs/user_guide/images/<言語>/<名前>.png に保存する。画面が変わったら
このスクリプトを実行し直すだけで、ドキュメントの画像がすべて最新になる。

    python scripts/user_guide/capture_screenshots.py            # 日本語
    python scripts/user_guide/capture_screenshots.py --lang en  # 英語の画面
    python scripts/user_guide/capture_screenshots.py --only gantt_tab

画面を表示しない（offscreen）で動くので、CIやリモート環境でも実行できる。
サンプルファイルは一時フォルダへコピーしてから開くため、元のファイルは変わらない。
利用者の設定（オプション）も一時ファイルを使い、実際の設定には触れない。
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from gui.app_settings import AppSettings  # noqa: E402
from gui.db import ProjectDatabase  # noqa: E402
from gui.main import MainWindow, apply_language  # noqa: E402
from gui.node_canvas import TaskNodeEditDialog  # noqa: E402
from gui.options_dialog import OptionsDialog  # noqa: E402
from i18n import tr  # noqa: E402

# ドキュメント用のサンプル（活用例の2つの題材。generate_guide_samples.py）
NEW_TITLE = ROOT / "data" / "Guide_Sample_NewTitle.pschedule"
UPDATE = ROOT / "data" / "Guide_Sample_Update.pschedule"
OUT_ROOT = ROOT / "docs" / "user_guide" / "images"
# A4の紙面で画面の文字が読める大きさになるよう、実際の既定サイズより小さめに撮る
WINDOW_SIZE = (1280, 800)

# タブの並び（gui/main.py の _rebuild_tabs と同じ順）
TAB_BASIC_INFO, TAB_WORKFLOWS, TAB_JOBS, TAB_GANTT, TAB_ANALYSIS = range(5)


def _wait(app, predicate, timeout=30.0):
    """predicate が真になるまでイベントを回す。スケジューリングは別スレッドで
    計算されるため、ガントチャート・分析タブはこれで完了を待つ。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _settle(app, seconds=0.3):
    """レイアウトの確定・遅延描画（次のイベントループでの fit 等）を済ませる。"""
    _wait(app, lambda: False, timeout=seconds)


def _show_tab(index, needs_schedule=False):
    def setup(app, window):
        window.tabs.setCurrentIndex(index)
        if needs_schedule:
            cache = window.schedule_cache
            cache.ensure_fresh()
            if not _wait(app, cache.is_fresh, timeout=60.0):
                raise RuntimeError("スケジューリングの計算が終わりませんでした")
    return setup


def _select_job(name):
    """ジョブ作成タブで、指定した名前のジョブを選んだ状態にする。"""
    def setup(app, window):
        window.tabs.setCurrentIndex(TAB_JOBS)
        table = window.tab_jobs.jobs_section.table
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and item.text() == name:
                table.selectRow(row)
                return
        raise RuntimeError(f"ジョブが見つかりません: {name}")
    return setup


# -- 3章 クイックスタート②のチュートリアル ----------------------------------------
# 本文（docs/user_guide/ja/03_quickstart.md）の手順と同じ内容を、手順 step まで
# 進めた状態のプロジェクトを作る。本文の手順を変えたら、ここも合わせて変える。
TUTORIAL_NAME = "はじめてのアップデート"


def _tutorial(step):
    def build(tmp):
        path = Path(tmp) / f"{TUTORIAL_NAME}.pschedule"
        if path.exists():
            path.unlink()
        db = ProjectDatabase.create_new(str(path))
        # 手順1: 基本情報
        db.set_project(TUTORIAL_NAME, "2026-10-05")
        db.add_milestone("配信", "2026-11-13", "")
        planner = db.add_team("プランナー", 1)
        art = db.add_team("アート", 2)
        if step >= 1.5:  # 手順2: ワークフロー（1.5 はタスクを1件だけ追加した途中の状態）
            wf = db.add_workflow("追加コンテンツ制作")
            spec = db.add_workflow_task(wf, "仕様作成", planner, 3)
        if step >= 2 and step != 1.5:
            artwork = db.add_workflow_task(wf, "アート制作", art, 5)
            check = db.add_workflow_task(wf, "確認", planner, 2)
            db.add_task_dependency(wf, spec, artwork)
            db.add_task_dependency(wf, artwork, check)
        if step >= 3:  # 手順3: ジョブ
            milestone = db.list_milestones()[0]["id"]
            for name, priority in (("新キャラクター", 1), ("新ステージ", 2), ("新アイテム", 3)):
                db.add_job(name, wf, milestone, priority)
        if step >= 4:  # 手順4: 配置コントロールを「最速」にする
            db.set_distribution_ratio(0.0)
        if step >= 5:  # 手順5: 条件を変えてみる
            db.update_team(planner, "プランナー", 2)
        db.save()
        db.close()
        return path
    build.key = ("tutorial", step)
    return build


def _task_dialog(app, window):
    """ワークフロー設計タブの「タスクを追加」ダイアログ（入力途中の状態）。"""
    window.tabs.setCurrentIndex(TAB_WORKFLOWS)
    db = window.db
    wf = db.list_workflows()[0]["id"]
    art = next(t["id"] for t in db.list_teams() if t["name"] == "アート")
    dialog = TaskNodeEditDialog(db, tr("タスクを追加"), name="アート制作", team_id=art, days=5,
                                workflow_id=wf, parent=window)
    for i in range(dialog.predecessor_list.count()):
        item = dialog.predecessor_list.item(i)
        item.setSelected(item.text() == "仕様作成")
    return dialog


def _options_dialog(app, window):
    dialog = OptionsDialog(window.app_settings, window)
    # 保存先の表示に一時フォルダのパスが写り込まないよう、ファイル名だけにする
    for label in dialog.findChildren(QLabel):
        if window.app_settings.path in label.text():
            label.setText(label.text().replace(window.app_settings.path, Path(window.app_settings.path).name))
    return dialog


# (画像の名前, 開くサンプル, 場面を作る関数)。撮りたい場面はここに足す。
# 開くサンプルは、ファイルのパス・一時フォルダを受けてパスを返す関数（_tutorial）・
# None（何も開いていない起動直後。先頭に置く）のいずれか。
# 場面を作る関数がダイアログを返したときは、ウィンドウではなくそのダイアログを撮る。
SHOTS = [
    ("startup", None, lambda app, window: None),
    ("options_dialog", None, _options_dialog),
    ("basic_info_tab", NEW_TITLE, _show_tab(TAB_BASIC_INFO)),
    ("workflows_tab", NEW_TITLE, _show_tab(TAB_WORKFLOWS)),
    ("jobs_tab", NEW_TITLE, _show_tab(TAB_JOBS)),
    ("gantt_tab", NEW_TITLE, _show_tab(TAB_GANTT, needs_schedule=True)),
    ("analysis_tab", NEW_TITLE, _show_tab(TAB_ANALYSIS, needs_schedule=True)),
    ("update_jobs_tab", UPDATE, _select_job("新キャラクター")),
    ("update_gantt_tab", UPDATE, _show_tab(TAB_GANTT, needs_schedule=True)),
    ("tutorial_1_basic_info", _tutorial(1), _show_tab(TAB_BASIC_INFO)),
    ("tutorial_2_task_dialog", _tutorial(1.5), _task_dialog),
    ("tutorial_2_workflow", _tutorial(2), _show_tab(TAB_WORKFLOWS)),
    ("tutorial_3_jobs", _tutorial(3), _show_tab(TAB_JOBS)),
    ("tutorial_4_gantt", _tutorial(4), _show_tab(TAB_GANTT, needs_schedule=True)),
    ("tutorial_5_gantt", _tutorial(5), _show_tab(TAB_GANTT, needs_schedule=True)),
]


def _open_sample(app, window, sample, tmp):
    """サンプルを一時フォルダへコピーして開く（元のファイルは変えない）。"""
    if callable(sample):
        project = sample(tmp)
    else:
        project = Path(tmp) / sample.name
        shutil.copy(sample, project)
    window._open_database(ProjectDatabase.open_existing(str(project)))
    # ステータスバーには開いたファイルのパスが出る。一時フォルダのパスが
    # 写り込まないよう、ファイル名だけにする。
    window.statusBar().showMessage(window.statusBar().currentMessage().replace(str(project), project.name))
    _settle(app)


def capture(language, only=None):
    out_dir = OUT_ROOT / language
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    apply_language(app, language)

    with tempfile.TemporaryDirectory() as tmp:
        settings = AppSettings(str(Path(tmp) / "settings.ini"))
        # オプション画面の表示言語が撮影環境のOSの言語にならないよう、撮る言語にそろえる
        settings.set("language", language)
        window = MainWindow(app_settings=settings)
        window.resize(*WINDOW_SIZE)
        window.show()

        saved = []
        opened = None
        try:
            for name, sample, setup in SHOTS:
                if only and name not in only:
                    continue
                key = getattr(sample, "key", sample)
                if sample is not None and key != opened:
                    _open_sample(app, window, sample, tmp)
                    opened = key
                dialog = setup(app, window)
                if dialog is not None:
                    dialog.show()
                _settle(app)
                path = out_dir / f"{name}.png"
                target = dialog if dialog is not None else window
                ok = target.grab().save(str(path))
                if dialog is not None:
                    dialog.close()
                if not ok:
                    raise RuntimeError(f"画像を保存できませんでした: {path}")
                saved.append(path)
                print(f"saved {path.relative_to(ROOT)}")
        finally:
            if window.db is not None:
                window._shutdown_schedule_cache()
                window.db.on_change = None
                window.db.undo_manager = None
                window.db.close()
            window.hide()
    return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lang", default="ja", help="表示言語（ja / en / vi / zh_CN）")
    parser.add_argument("--only", nargs="*", help="この名前の場面だけ撮る")
    args = parser.parse_args()
    capture(args.lang, set(args.only) if args.only else None)


if __name__ == "__main__":
    main()
