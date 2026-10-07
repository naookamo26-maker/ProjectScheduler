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
        ("gantt_drag_modifier", "ctrl"),
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


def test_bar_label_options_round_trip_and_fall_back_when_broken(settings_path):
    """ガントのバーに出す項目（真偽値）は ini に保存でき、壊れた値は既定に戻る。"""
    s = AppSettings(settings_path)
    assert s.get("gantt_bar_show_name") is True
    assert s.get("gantt_bar_show_days") is True
    assert s.get("gantt_bar_show_team") is False
    assert s.get("gantt_bar_day_count") == "work"
    s.set("gantt_bar_show_days", False)
    s.set("gantt_bar_show_team", True)
    s.set("gantt_bar_day_count", "calendar")
    s.sync()

    reopened = AppSettings(settings_path)
    assert reopened.get("gantt_bar_show_days") is False
    assert reopened.get("gantt_bar_show_team") is True
    assert reopened.get("gantt_bar_day_count") == "calendar"

    raw = QSettings(settings_path, QSettings.IniFormat)
    raw.setValue("gantt/bar_show_days", "たぶん")
    raw.setValue("gantt/bar_day_count", "weeks")
    raw.sync()
    broken = AppSettings(settings_path)
    assert broken.get("gantt_bar_show_days") is True
    assert broken.get("gantt_bar_day_count") == "work"
    with pytest.raises(ValueError):
        broken.set("gantt_bar_show_days", 1)


def test_options_dialog_saves_bar_label_items(qapp, settings_path):
    from gui.options_dialog import OptionsDialog
    from gui.tab_gantt import bar_label_options

    s = AppSettings(settings_path)
    dialog = OptionsDialog(s)
    dialog.bar_label_checks["gantt_bar_show_slack"].setChecked(True)
    dialog.bar_label_checks["gantt_bar_show_name"].setChecked(False)
    dialog._select_data(dialog.bar_day_count_combo, "calendar")
    dialog.accept()

    options = bar_label_options(AppSettings(settings_path))
    assert options.show_name is False
    assert options.extras == ("days", "slack")
    assert options.day_count == "calendar"

    dialog = OptionsDialog(s)
    dialog.reset_button.click()
    assert dialog.bar_label_checks["gantt_bar_show_name"].isChecked()
    assert not dialog.bar_label_checks["gantt_bar_show_slack"].isChecked()
    assert dialog.bar_day_count_combo.currentData() == "work"


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


def test_options_dialog_lets_the_user_choose_the_display_language(qapp, settings_path):
    from gui.options_dialog import OptionsDialog

    s = AppSettings(settings_path)
    dialog = OptionsDialog(s)
    labels = [dialog.language_combo.itemText(i) for i in range(dialog.language_combo.count())]
    assert labels == ["日本語", "English", "Tiếng Việt", "简体中文"]

    dialog._select_language("zh_CN")
    dialog.accept()
    assert AppSettings(settings_path).get("language") == "zh_CN"

    dialog = OptionsDialog(AppSettings(settings_path))
    assert dialog.selected_language() == "zh_CN"
    dialog.reset_button.click()
    assert dialog.selected_language() == s.default("language")


def test_options_dialog_saves_gantt_drag_key_and_highlight_time(qapp, settings_path):
    from gui.options_dialog import OptionsDialog

    s = AppSettings(settings_path)
    dialog = OptionsDialog(s)
    assert [dialog.drag_modifier_combo.itemData(i) for i in range(dialog.drag_modifier_combo.count())] \
        == ["shift", "alt"]
    assert dialog.highlight_spin.text() == "次の操作まで"
    dialog._select_data(dialog.drag_modifier_combo, "alt")
    dialog.highlight_spin.setValue(5)
    dialog.accept()

    saved = AppSettings(settings_path)
    assert saved.get("gantt_drag_modifier") == "alt"
    assert saved.get("moved_bar_highlight_seconds") == 5
