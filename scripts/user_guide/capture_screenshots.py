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
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from gui.app_settings import AppSettings  # noqa: E402
from gui.db import ProjectDatabase  # noqa: E402
from gui.main import MainWindow, apply_language  # noqa: E402
from gui.node_canvas import TaskNodeEditDialog  # noqa: E402
from gui.options_dialog import OptionsDialog  # noqa: E402
from gui.replan_dialog import ReplanDialog  # noqa: E402
import gui.plan_actions  # noqa: E402
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


def _select_workflow(name, view=0):
    """ワークフロー設計タブで、指定したワークフローを選び、ビュー（0=ノード、1=テーブル）を切り替える。"""
    def setup(app, window):
        window.tabs.setCurrentIndex(TAB_WORKFLOWS)
        tab = window.tab_workflows
        items = tab.workflow_list.findItems(name, Qt.MatchExactly)
        if not items:
            raise RuntimeError(f"ワークフローが見つかりません: {name}")
        tab.workflow_list.setCurrentItem(items[0])
        tab.view_tabs.setCurrentIndex(view)
        _settle(app)
        if view == 0:
            tab.view.fit_all()
    return setup


def _job_with_dependencies(name):
    """ジョブ作成タブで、指定したジョブを選び、依存先ジョブのツリーを展開する。"""
    select = _select_job(name)

    def setup(app, window):
        select(app, window)
        _settle(app)
        window.tab_jobs.dep_tree.expandAll()
    return setup


def _analysis_section(index):
    """プロジェクト分析タブのサブタブ（0=マイルストーン、1=チーム、2=ワークフロー）を開く。"""
    show = _show_tab(TAB_ANALYSIS, needs_schedule=True)

    def setup(app, window):
        show(app, window)
        window.tab_analysis.section_tabs.setCurrentIndex(index)
    return setup


# -- 5章 ガントチャートの図解 ------------------------------------------------------
def _bar_legend(tmp):
    """バーの見た目を1種類ずつ並べた小さなプロジェクト。ジョブ名がそのまま見た目の説明になる。"""
    path = Path(tmp) / "バーの見た目.pschedule"
    if path.exists():
        path.unlink()
    db = ProjectDatabase.create_new(str(path))
    db.set_project("バーの見た目", "2026-10-05")
    db.set_distribution_ratio(0.0)
    deadline = db.add_milestone("締切", "2026-11-13", "")
    early = db.add_milestone("早い締切", "2026-10-14", "")
    planner = db.add_team("プランナー", None)
    art = db.add_team("アート", None)
    wf = db.add_workflow("制作")
    spec = db.add_workflow_task(wf, "仕様作成", planner, 3)
    make = db.add_workflow_task(wf, "制作", art, 5)
    check = db.add_workflow_task(wf, "確認", planner, 2)
    db.add_task_dependency(wf, spec, make)
    db.add_task_dependency(wf, make, check)
    normal = db.add_job("通常のタスク", wf, deadline, 1)
    late = db.add_job("締切に間に合わない", wf, early, 2)
    pinned = db.add_job("開始日を固定", wf, deadline, 3)
    broken = db.add_job("固定どおりに置けない", wf, deadline, 4)
    db.upsert_job_task_override(pinned, spec, start_pin_date="2026-10-12")
    # 仕様作成（10/05〜10/07）が終わる前の日付に固定する
    db.upsert_job_task_override(broken, make, start_pin_date="2026-10-06")
    db.save()
    db.close()
    return path


_bar_legend.key = ("bar_legend",)


def _bar_key(db, job_name, task_name):
    """ガントのバーのキー（gui/gantt_generator.py の Job_ID / Task_ID と同じ書式）。"""
    job = next(j for j in db.list_jobs() if j["name"] == job_name)
    task = next(t for t in db.list_workflow_tasks(job["workflow_id"]) if t["name"] == task_name)
    return (f"JOB_{job['id']:03d}", f"T_{task['id']:03d}")


def _gantt_drag(job_name, task_name, days):
    """Shift を押しながらバーをドラッグしている途中の場面。撮った後にボタンを離す
    （離すと編集が確定し、日程が組み直される。次の場面 _gantt_after_edit で結果を撮る）。"""
    show = _show_tab(TAB_GANTT, needs_schedule=True)

    def setup(app, window):
        show(app, window)
        tab = window.tab_gantt
        key = _bar_key(window.db, job_name, task_name)
        tab.view.select_keys([key])
        _settle(app)
        bar = tab.view.bars()[key]
        body = tab.view.body
        viewport = body.viewport()
        center = bar.bar_rect.center()
        start = body.mapFromScene(center)
        # ガントのシーン座標は1暦日＝10
        end = body.mapFromScene(center.x() + days * 10, center.y())
        QTest.mousePress(viewport, Qt.LeftButton, Qt.ShiftModifier, start)
        move = QMouseEvent(QEvent.MouseMove, QPointF(end), QPointF(viewport.mapToGlobal(end)),
                           Qt.NoButton, Qt.LeftButton, Qt.ShiftModifier)
        QApplication.sendEvent(viewport, move)

        def release():
            QTest.mouseRelease(viewport, Qt.LeftButton, Qt.ShiftModifier, end)
            app.processEvents()
        return release
    return setup


def _gantt_after_edit(app, window):
    """編集が確定して日程が組み直された後の場面（動いたバーが黄色の枠で強調される）。"""
    tab = window.tab_gantt
    if not _wait(app, lambda: tab.cache.is_fresh() and tab._pending_edit is None, timeout=60.0):
        raise RuntimeError("編集後の再計算が終わりませんでした")
    _settle(app, 0.5)


def _gantt_editor(job_name, task_name):
    """バーを選んで編集ウィンドウを開いた場面（編集ウィンドウだけを撮る）。"""
    show = _show_tab(TAB_GANTT, needs_schedule=True)

    def setup(app, window):
        show(app, window)
        tab = window.tab_gantt
        tab.view.select_keys([_bar_key(window.db, job_name, task_name)])
        _settle(app)
        tab.open_editor()
        return tab._editor
    return setup


# -- 6章 計画の確定と再計画 ------------------------------------------------------
# 状態帯に確定した日時が写るので、撮る日によって画像が変わらないよう固定する
gui.plan_actions._now = lambda: "2026-10-02T17:00:00"
# 全面再計画のダイアログで「今日」として使う日（ダイアログの件数の表示に効く）
PLAN_TODAY = date(2026, 11, 2)


def _plan_sample(tmp):
    """6章用に、新作タイトルのサンプルを別のコピーとして開く（ほかの章の場面と混ざらないように）。"""
    path = Path(tmp) / "新作タイトルの制作.pschedule"
    shutil.copy(NEW_TITLE, path)
    return path


_plan_sample.key = ("plan",)


def _wait_schedule(app, window):
    cache = window.schedule_cache
    cache.ensure_fresh()
    if not _wait(app, cache.is_fresh, timeout=60.0):
        raise RuntimeError("スケジューリングの計算が終わりませんでした")


def _plan_confirm(app, window):
    """ガントチャートタブで「確定する」を押した後の場面。"""
    window.tabs.setCurrentIndex(TAB_GANTT)
    _wait_schedule(app, window)
    window.tab_gantt.run_plan_action("confirm")
    _settle(app)
    _wait_schedule(app, window)


def _plan_draft(app, window):
    """確定の後に日程に関わる編集をして、変更案になった場面（影響の小さい変更）。"""
    db = window.db
    jobs = {j["name"]: j for j in db.list_jobs()}
    tasks = {t["name"]: t["id"] for t in db.list_workflow_tasks(jobs["魔王"]["workflow_id"])}
    # 定例ミーティングで進み具合を反映した（状態の更新だけでは変更案にならない）
    for job in ("主人公", "ヒロイン"):
        db.update_job_task_override_fields(jobs[job]["id"], tasks["設定・仕様"], status="done")
    db.update_job_task_override_fields(jobs["主人公"]["id"], tasks["コンセプトアート"], status="in_progress")
    # 魔王の組み込みが延びることになった（10日 → 15日）。プログラマーはライン数が
    # 「指定なし」なので、ほかのタスクとの取り合いが起きず、影響は後続だけにとどまる
    db.update_job_task_override_fields(jobs["魔王"]["id"], tasks["組み込み"], override_days=15)
    # 確定の後にジョブを追加した（このジョブのタスクは未確定になる）
    master = next(m["id"] for m in db.list_milestones() if m["name"] == "マスターアップ")
    db.add_job("村人", jobs["魔王"]["workflow_id"], master, 5, "サブ")
    window.tabs.setCurrentIndex(TAB_GANTT)
    _settle(app)
    _wait_schedule(app, window)


def _plan_draft_large(app, window):
    """影響の大きい変更。モーションチームのラインが確定済みのタスクで埋まっているので、
    延ばしたタスクが空きを探して大きく後ろへずれる（全面再計画を使う理由の説明に使う）。"""
    db = window.db
    jobs = {j["name"]: j for j in db.list_jobs()}
    tasks = {t["name"]: t["id"] for t in db.list_workflow_tasks(jobs["魔王"]["workflow_id"])}
    db.update_job_task_override_fields(jobs["魔王"]["id"], tasks["モーション制作"], override_days=30)
    window.tabs.setCurrentIndex(TAB_GANTT)
    _settle(app)
    _wait_schedule(app, window)


def _zoom_to_bar(job_name, task_name, before_days, after_days):
    """ガントチャートで、指定したバーの前後（営業日ではなく暦日）を拡大して表示する。
    細い線や斜線など、全体表示では小さすぎて見えない表示を見せるのに使う。"""
    def setup(app, window):
        window.tabs.setCurrentIndex(TAB_GANTT)
        _wait_schedule(app, window)
        _settle(app)
        tab = window.tab_gantt
        bar = tab.view.bars()[_bar_key(window.db, job_name, task_name)]
        # ガントのシーン座標は1暦日＝10
        rect = bar.sceneBoundingRect().adjusted(-before_days * 10, -70, after_days * 10, 70)
        tab.view.body.fitInView(rect)
        tab.view._sync_panes()
        _settle(app)
    return setup


def _plan_replanned(app, window):
    """全面再計画を実行した後の場面（結果は変更案として表示される）。"""
    base = PLAN_TODAY.isoformat()
    window.db.start_full_replan(base, base)
    window.tabs.setCurrentIndex(TAB_GANTT)
    _settle(app)
    _wait_schedule(app, window)
    _settle(app)
    # 直前の場面で拡大した表示を、全体表示（A キー）に戻す
    window.tab_gantt.view.fit_all()
    _settle(app)


def _replan_dialog(app, window):
    dialog = ReplanDialog(window.db, today=PLAN_TODAY, parent=window)
    return dialog


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
# 場面を作る関数がウィジェット（ダイアログ等）を返したときは、ウィンドウではなくそれを撮り、
# 撮った後に閉じる。関数を返したときは、ウィンドウを撮った後にその関数を呼ぶ（ドラッグの
# ボタンを離す等の後始末）。4つ目に (x, y, 幅, 高さ) を書くと、撮った画像のその範囲だけを残す
# （細かい部分を紙面で大きく見せたいとき）。
SHOTS = [
    ("startup", None, lambda app, window: None),
    ("options_dialog", None, _options_dialog),
    ("basic_info_tab", NEW_TITLE, _show_tab(TAB_BASIC_INFO)),
    ("workflows_tab", NEW_TITLE, _show_tab(TAB_WORKFLOWS)),
    ("jobs_tab", NEW_TITLE, _show_tab(TAB_JOBS)),
    ("gantt_tab", NEW_TITLE, _show_tab(TAB_GANTT, needs_schedule=True)),
    ("analysis_tab", NEW_TITLE, _analysis_section(0)),
    ("analysis_team", NEW_TITLE, _analysis_section(1)),
    ("workflows_table", NEW_TITLE, _select_workflow("キャラクター制作", view=1)),
    ("workflows_cutscene", NEW_TITLE, _select_workflow("カットシーン制作", view=0)),
    ("jobs_opening", NEW_TITLE, _job_with_dependencies("オープニング")),
    ("update_jobs_tab", UPDATE, _select_job("新キャラクター")),
    ("update_gantt_tab", UPDATE, _show_tab(TAB_GANTT, needs_schedule=True)),
    ("tutorial_1_basic_info", _tutorial(1), _show_tab(TAB_BASIC_INFO)),
    ("tutorial_2_task_dialog", _tutorial(1.5), _task_dialog),
    ("tutorial_2_workflow", _tutorial(2), _show_tab(TAB_WORKFLOWS)),
    ("tutorial_3_jobs", _tutorial(3), _show_tab(TAB_JOBS)),
    ("tutorial_4_gantt", _tutorial(4), _show_tab(TAB_GANTT, needs_schedule=True)),
    # 5章 ガントチャートの編集（チュートリアル手順4の状態を使う）
    # 影がほかのバーと重ならないよう、行の最後のタスクを右の空いている所へ動かす
    ("gantt_drag", _tutorial(4), _gantt_drag("新キャラクター", "確認", 10), (0, 180, 900, 200)),
    ("gantt_after_edit", _tutorial(4), _gantt_after_edit),
    ("gantt_editor", _tutorial(4), _gantt_editor("新アイテム", "アート制作")),
    ("tutorial_5_gantt", _tutorial(5), _show_tab(TAB_GANTT, needs_schedule=True)),
    ("gantt_legend", _bar_legend, _show_tab(TAB_GANTT, needs_schedule=True)),
    # 6章 計画の確定と再計画（同じコピーに対して、この順に操作を重ねる）
    ("plan_confirmed", _plan_sample, _plan_confirm),
    ("plan_draft", _plan_sample, _plan_draft),
    ("plan_draft_changed", _plan_sample, _zoom_to_bar("魔王", "組み込み", 40, 40)),
    ("plan_draft_new", _plan_sample, _zoom_to_bar("村人", "モデル制作", 50, 60)),
    ("plan_draft_jobs", _plan_sample, _select_job("魔王")),
    ("plan_draft_large", _plan_sample, _plan_draft_large),
    ("plan_draft_large_zoom", _plan_sample, _zoom_to_bar("魔王", "モーション制作", 130, 70)),
    ("plan_replan_dialog", _plan_sample, _replan_dialog),
    ("plan_replanned", _plan_sample, _plan_replanned),
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
            for name, sample, setup, *crop in SHOTS:
                if only and name not in only:
                    continue
                key = getattr(sample, "key", sample)
                if sample is not None and key != opened:
                    _open_sample(app, window, sample, tmp)
                    opened = key
                result = setup(app, window)
                dialog = result if isinstance(result, QWidget) else None
                after = result if callable(result) and dialog is None else None
                if dialog is not None:
                    dialog.show()
                _settle(app)
                path = out_dir / f"{name}.png"
                target = dialog if dialog is not None else window
                pixmap = target.grab()
                if crop:
                    pixmap = pixmap.copy(QRect(*crop[0]))
                ok = pixmap.save(str(path))
                if dialog is not None:
                    dialog.close()
                if after is not None:
                    after()
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
