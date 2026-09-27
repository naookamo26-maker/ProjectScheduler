"""
起動用アイコン（assets/icon/app_icon.ico）が、.exe・ウィンドウの両方で使える形で
揃っていることを確かめる。原画は assets/icon/*.svg、生成は scripts/build_icon.py。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6")
pytest.importorskip("pandas")

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app_version import APP_VERSION  # noqa: E402
from gui.app_settings import AppSettings  # noqa: E402
from gui.main import MainWindow, app_icon_path  # noqa: E402

pytestmark = pytest.mark.gui

# Windows がタスクバー・エクスプローラー・高DPIで使い分けるサイズ
REQUIRED_SIZES = {16, 24, 32, 48, 256}


def test_icon_file_is_bundled_path():
    path = app_icon_path()
    assert path.is_file()
    # spec の datas・icon= と同じ場所を指していること
    spec = (Path(__file__).resolve().parent.parent / "packaging" / "ProjectSchedulerGUI.spec").read_text(encoding="utf-8")
    assert '"assets", "icon", "app_icon.ico"' in spec


def test_icon_loads_with_all_required_sizes():
    QApplication.instance() or QApplication([])
    icon = QIcon(str(app_icon_path()))
    assert not icon.isNull()
    sizes = {s.width() for s in icon.availableSizes()}
    assert REQUIRED_SIZES <= sizes


def test_help_menu_shows_the_version(tmp_path):
    """「ヘルプ」→「バージョン情報」で、アプリ名とバージョンを確かめられる。"""
    QApplication.instance() or QApplication([])
    window = MainWindow(app_settings=AppSettings(str(tmp_path / "settings.ini")))
    try:
        menus = {a.text(): a.menu() for a in window.menuBar().actions()}
        help_menu = menus["ヘルプ(&H)"]
        assert [a.text() for a in help_menu.actions()] == ["バージョン情報(&A)..."]
        box = window.about_box()
        assert box.windowTitle() == "バージョン情報"
        assert "プロジェクトスケジューラー" in box.text()
        assert box.informativeText() == f"バージョン {APP_VERSION}"
        assert not box.iconPixmap().isNull()
    finally:
        window.close()
