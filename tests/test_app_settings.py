"""
オプション設定（gui/app_settings.py）とオプションダイアログ（gui/options_dialog.py）の
テスト。QSettings / QLocale / QDialog を使うため PySide6 が必要。

設定ファイルは各テストの tmp_path に作り、利用者の実設定には触れない
（ルートの conftest.py でも既定の保存先を一時フォルダへ向けている）。
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6")

from PySide6.QtCore import QLocale, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import app_settings as app_settings_module  # noqa: E402
from gui.app_settings import AppSettings, default_settings_path, system_language  # noqa: E402

# 分類: gui（PySide6 が必要）
pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def settings_path(tmp_path):
    return str(tmp_path / "ProjectScheduler.ini")


def test_defaults_are_returned_when_nothing_is_saved(settings_path):
    s = AppSettings(settings_path)
    assert s.get("undo_memory_limit_mb") == 128
    assert s.get("gantt_drag_modifier") == "shift"
    assert s.get("full_replan_threshold_percent") == 20
    assert s.get("moved_bar_highlight_seconds") == 0
    assert s.get("language") in app_settings_module.SUPPORTED_LANGUAGES
    assert s.undo_memory_limit_bytes() == 128 * 1024 * 1024


def test_saved_values_survive_reopening(settings_path):
    s = AppSettings(settings_path)
    s.set("undo_memory_limit_mb", 256)
    s.set("gantt_drag_modifier", "alt")
    s.set("language", "vi")
    s.sync()

    reopened = AppSettings(settings_path)
    assert reopened.get("undo_memory_limit_mb") == 256
    assert reopened.get("gantt_drag_modifier") == "alt"
    assert reopened.get("language") == "vi"


def test_broken_values_in_the_file_fall_back_to_defaults(settings_path):
    """ini は手で編集できるので、型違い・範囲外・選択肢外の値でも起動できること。"""
    raw = QSettings(settings_path, QSettings.IniFormat)
    raw.setValue("general/undo_memory_limit_mb", "たくさん")
    raw.setValue("plan/full_replan_threshold_percent", 500)
    raw.setValue("gantt/drag_modifier", "meta")
    raw.setValue("general/language", "fr")
    raw.sync()

    s = AppSettings(settings_path)
    assert s.get("undo_memory_limit_mb") == 128
    assert s.get("full_replan_threshold_percent") == 20
    assert s.get("gantt_drag_modifier") == "shift"
    assert s.get("language") == s.default("language")


@pytest.mark.parametrize(
    "name, value",
    [
        ("undo_memory_limit_mb", 8),
        ("undo_memory_limit_mb", 99999),
        ("undo_memory_limit_mb", "128"),
        ("undo_memory_limit_mb", True),
        ("gantt_drag_modifier", "meta"),
        ("language", "zh_TW"),
    ],
)
def test_setting_an_invalid_value_is_rejected(settings_path, name, value):
    s = AppSettings(settings_path)
    with pytest.raises(ValueError):
        s.set(name, value)


def test_reset_to_defaults_clears_saved_values(settings_path):
    s = AppSettings(settings_path)
    s.set("undo_memory_limit_mb", 512)
    s.sync()
    s.reset_to_defaults()
    s.sync()
    assert AppSettings(settings_path).get("undo_memory_limit_mb") == 128


@pytest.mark.parametrize(
    "locale_name, expected",
    [
        ("ja_JP", "ja"),
        ("en_US", "en"),
        ("vi_VN", "vi"),
        ("zh_CN", "zh_CN"),
        ("zh_Hans_SG", "zh_CN"),
        ("zh_TW", "en"),  # 繁体字は対応外
        ("fr_FR", "en"),  # 対応4言語以外は英語
    ],
)
def test_system_language_maps_os_locale_to_supported_languages(locale_name, expected):
    assert system_language(QLocale(locale_name)) == expected


def test_settings_path_prefers_environment_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_SCHEDULER_SETTINGS", str(tmp_path / "x.ini"))
    assert default_settings_path() == str(tmp_path / "x.ini")


def test_frozen_exe_saves_next_to_the_exe_when_writable(monkeypatch, tmp_path):
    monkeypatch.delenv("PROJECT_SCHEDULER_SETTINGS", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "ProjectSchedulerGUI.exe"))
    assert default_settings_path() == os.path.join(str(tmp_path), "ProjectScheduler.ini")


def test_frozen_exe_falls_back_to_user_folder_when_not_writable(monkeypatch, tmp_path):
    monkeypatch.delenv("PROJECT_SCHEDULER_SETTINGS", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "ProjectSchedulerGUI.exe"))
    monkeypatch.setattr(app_settings_module, "_is_writable_dir", lambda path: False)
    monkeypatch.setattr(app_settings_module, "_user_config_dir", lambda: str(tmp_path / "user"))
    assert default_settings_path() == os.path.join(str(tmp_path / "user"), "ProjectScheduler.ini")


def test_development_run_uses_user_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("PROJECT_SCHEDULER_SETTINGS", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(app_settings_module, "_user_config_dir", lambda: str(tmp_path / "user"))
    assert default_settings_path() == os.path.join(str(tmp_path / "user"), "ProjectScheduler.ini")


def test_options_dialog_saves_on_ok_only(qapp, settings_path):
    from gui.options_dialog import OptionsDialog

    s = AppSettings(settings_path)
    dialog = OptionsDialog(s)
    dialog.undo_memory_spin.setValue(64)
    dialog.reject()
    assert AppSettings(settings_path).get("undo_memory_limit_mb") == 128

    dialog = OptionsDialog(s)
    dialog.undo_memory_spin.setValue(64)
    dialog.accept()
    assert AppSettings(settings_path).get("undo_memory_limit_mb") == 64


def test_options_dialog_reset_button_restores_defaults_on_screen(qapp, settings_path):
    from gui.options_dialog import OptionsDialog

    s = AppSettings(settings_path)
    s.set("undo_memory_limit_mb", 512)
    dialog = OptionsDialog(s)
    assert dialog.undo_memory_spin.value() == 512
    dialog.reset_button.click()
    assert dialog.undo_memory_spin.value() == 128
    # 「既定値に戻す」だけでは保存しない（OK で保存）
    assert s.get("undo_memory_limit_mb") == 512


def test_main_window_applies_undo_memory_limit(qapp, settings_path):
    """起動時の値でUndo履歴の上限が決まり、オプション変更後は実行中の履歴にも反映される。"""
    import shiboken6

    from gui.db import ProjectDatabase
    from gui.main import MainWindow

    s = AppSettings(settings_path)
    s.set("undo_memory_limit_mb", 64)
    w = MainWindow(app_settings=s)
    try:
        w._open_database(ProjectDatabase.create_new())
        assert w.undo_manager._max_total_bytes == 64 * 1024 * 1024

        s.set("undo_memory_limit_mb", 32)
        w._apply_app_settings()
        assert w.undo_manager._max_total_bytes == 32 * 1024 * 1024
    finally:
        w._shutdown_schedule_cache()
        w.db.on_change = None
        w.db.undo_manager = None
        w.db.close()
        shiboken6.delete(w)
        qapp.processEvents()
