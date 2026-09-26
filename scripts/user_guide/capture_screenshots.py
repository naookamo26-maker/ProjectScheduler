"""利用者向けドキュメント（docs/user_guide/）に載せる画面を自動で撮影する。

同梱のサンプルプロジェクトを開いた状態で、SHOTS に並べた場面を順に撮り、
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

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui.app_settings import AppSettings  # noqa: E402
from gui.db import ProjectDatabase  # noqa: E402
from gui.main import MainWindow, apply_language  # noqa: E402

SAMPLE = ROOT / "data" / "Project_Schedule_Sample_GameDev_v22.pschedule"
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


# (画像の名前, 場面を作る関数)。撮りたい場面はここに足す。
SHOTS = [
    ("basic_info_tab", _show_tab(TAB_BASIC_INFO)),
    ("workflows_tab", _show_tab(TAB_WORKFLOWS)),
    ("jobs_tab", _show_tab(TAB_JOBS)),
    ("gantt_tab", _show_tab(TAB_GANTT, needs_schedule=True)),
    ("analysis_tab", _show_tab(TAB_ANALYSIS, needs_schedule=True)),
]


def capture(language, only=None):
    out_dir = OUT_ROOT / language
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    apply_language(app, language)

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / SAMPLE.name
        shutil.copy(SAMPLE, project)
        window = MainWindow(app_settings=AppSettings(str(Path(tmp) / "settings.ini")))
        window.resize(*WINDOW_SIZE)
        window.show()
        window._open_database(ProjectDatabase.open_existing(str(project)))
        # ステータスバーには開いたファイルのパスが出る。一時フォルダのパスが
        # 写り込まないよう、ファイル名だけにする。
        window.statusBar().showMessage(window.statusBar().currentMessage().replace(str(project), project.name))
        _settle(app)

        saved = []
        try:
            for name, setup in SHOTS:
                if only and name not in only:
                    continue
                setup(app, window)
                _settle(app)
                path = out_dir / f"{name}.png"
                if not window.grab().save(str(path)):
                    raise RuntimeError(f"画像を保存できませんでした: {path}")
                saved.append(path)
                print(f"saved {path.relative_to(ROOT)}")
        finally:
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
