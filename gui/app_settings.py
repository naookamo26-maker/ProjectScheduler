"""
利用者（PC）ごとのオプション設定の読み書き（docs/roadmap.md §10）。

プロジェクトファイル（.pschedule）ではなく、使っているPC側に保存する。
プロジェクトを共有する相手の操作感（ドラッグのキー等）を変えないため。
設定はプロジェクトの内容ではないので、Undo/Redoの対象にもしない。

保存先は ini ファイル（QSettings の IniFormat）で、場所は
default_settings_path() が決める。

- .exe（PyInstaller で凍結済み）: exe と同じフォルダ。フォルダごとコピーすれば
  設定も持ち運べる。そこに書き込めない場合（Program Files に置いた等）は
  利用者ごとのフォルダ（QStandardPaths.AppConfigLocation）へ逃がす
- 開発時（python run_gui.py）: 利用者ごとのフォルダ。リポジトリ直下に
  ini を作らないため
- 環境変数 PROJECT_SCHEDULER_SETTINGS があればそのパス（テスト用。
  ルートの conftest.py が一時フォルダを指定し、利用者の実設定を汚さない）

項目は今後も増える前提なので、キー・既定値・取りうる値は OPTIONS に集め、
各所に既定値を散らさない。値の読み書きは get() / set() を通す。
"""

import os
import sys
import tempfile
from dataclasses import dataclass

from PySide6.QtCore import QLocale, QSettings, QStandardPaths

from i18n import LANGUAGES

SETTINGS_FILE_NAME = "ProjectScheduler.ini"
SETTINGS_PATH_ENV = "PROJECT_SCHEDULER_SETTINGS"

# 表示言語（docs/roadmap.md §11）。値は QLocale の名前の形に合わせる。一覧は i18n.py
SUPPORTED_LANGUAGES = tuple(code for code, _label in LANGUAGES)
FALLBACK_LANGUAGE = "en"

# ガントのバーをドラッグするときに押すキー（docs/roadmap.md §9）。Ctrl は
# 複数選択（Ctrl＋クリック）に使うため選べない。
DRAG_MODIFIERS = ("shift", "alt")


def system_language(locale=None):
    """OSの言語から、対応4言語のどれを既定にするかを決める。

    中国語は簡体字（zh_CN / zh_Hans_*）だけを対応言語とみなし、繁体字の
    地域（zh_TW 等）は対応外として英語にする。
    """
    locale = locale or QLocale.system()
    lang = locale.language()
    if lang == QLocale.Language.Japanese:
        return "ja"
    if lang == QLocale.Language.Vietnamese:
        return "vi"
    if lang == QLocale.Language.Chinese and locale.script() != QLocale.Script.TraditionalChineseScript:
        if locale.script() == QLocale.Script.SimplifiedChineseScript or locale.territory() in (
            QLocale.Territory.China, QLocale.Territory.Singapore,
        ):
            return "zh_CN"
    if lang == QLocale.Language.English:
        return "en"
    return FALLBACK_LANGUAGE


@dataclass(frozen=True)
class _Option:
    key: str
    default: object  # 値、または引数なしで既定値を返す callable
    kind: type
    choices: tuple = None
    minimum: int = None
    maximum: int = None

    def default_value(self):
        return self.default() if callable(self.default) else self.default

    def is_valid(self, value):
        # bool は int の派生型なので明示的に弾く（真偽値の項目は今のところ無い）
        if isinstance(value, bool) or not isinstance(value, self.kind):
            return False
        if self.choices is not None and value not in self.choices:
            return False
        if self.minimum is not None and value < self.minimum:
            return False
        if self.maximum is not None and value > self.maximum:
            return False
        return True


OPTIONS = {
    # 表示言語。既定はOSの言語（対応4言語以外なら英語）。§11 で使う。
    "language": _Option("general/language", system_language, str, choices=SUPPORTED_LANGUAGES),
    # Undoに使うメモリの上限（MB）。gui/undo_manager.py の合計バイト数の上限になる。
    "undo_memory_limit_mb": _Option(
        "general/undo_memory_limit_mb", 128, int, minimum=16, maximum=4096,
    ),
    # ガントのバーをドラッグするキー。§9 で使う。
    "gantt_drag_modifier": _Option("gantt/drag_modifier", "shift", str, choices=DRAG_MODIFIERS),
    # 動いたバーを強調する時間（秒）。0 は「次の操作まで」。§9-3 で使う。
    "moved_bar_highlight_seconds": _Option(
        "gantt/moved_bar_highlight_seconds", 0, int, minimum=0, maximum=60,
    ),
    # 全面再計画を案内する影響範囲の割合（%）。§8-7 で使う。
    "full_replan_threshold_percent": _Option(
        "plan/full_replan_threshold_percent", 20, int, minimum=1, maximum=100,
    ),
}


def _is_writable_dir(path):
    """実際にファイルを作って確かめる。Windows の Program Files では
    os.access() が書き込み可と答えても、実際には書けないことがあるため。"""
    try:
        fd, probe = tempfile.mkstemp(dir=path, prefix=".write_test_")
    except OSError:
        return False
    os.close(fd)
    try:
        os.remove(probe)
    except OSError:
        pass
    return True


def _user_config_dir():
    return QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)


def default_settings_path():
    """設定ファイルの保存先を決める（冒頭のdocstring参照）。"""
    override = os.environ.get(SETTINGS_PATH_ENV)
    if override:
        return override
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if _is_writable_dir(exe_dir):
            return os.path.join(exe_dir, SETTINGS_FILE_NAME)
    return os.path.join(_user_config_dir(), SETTINGS_FILE_NAME)


class AppSettings:
    """オプション設定の読み書き。値の型・範囲は OPTIONS で検証する。"""

    def __init__(self, path=None):
        self.path = path or default_settings_path()
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._qs = QSettings(self.path, QSettings.IniFormat)

    def get(self, name):
        """保存値を返す。未保存、または壊れた値（型違い・範囲外）なら既定値。
        ini は手で編集できるため、壊れた値で起動できなくならないようにする。"""
        option = OPTIONS[name]
        raw = self._qs.value(option.key)
        if raw is None:
            return option.default_value()
        value = raw
        if option.kind is int:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return option.default_value()
        elif option.kind is str:
            value = str(raw)
        return value if option.is_valid(value) else option.default_value()

    def set(self, name, value):
        option = OPTIONS[name]
        if not option.is_valid(value):
            raise ValueError(f"{name} に設定できない値です: {value!r}")
        self._qs.setValue(option.key, value)

    def default(self, name):
        return OPTIONS[name].default_value()

    def reset_to_defaults(self):
        """保存値をすべて消す（以後の get() は既定値を返す）。"""
        self._qs.clear()

    def sync(self):
        """ファイルへ書き出す。書き込みに失敗したら OSError。"""
        self._qs.sync()
        if self._qs.status() != QSettings.NoError:
            raise OSError(f"設定を保存できませんでした: {self.path}")

    def get_ui_state(self, name):
        """オプションではない画面の状態（編集ウィンドウの位置など）。検証しない。"""
        return self._qs.value(f"ui/{name}")

    def set_ui_state(self, name, value):
        self._qs.setValue(f"ui/{name}", value)

    def undo_memory_limit_bytes(self):
        return self.get("undo_memory_limit_mb") * 1024 * 1024
