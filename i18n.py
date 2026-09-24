"""
多言語対応（docs/roadmap.md §11）。画面・DB層・スケジューラ・出力ファイルで共通に
使う、Qt に依存しない小さな翻訳の仕組み。

    from i18n import tr
    tr("ジョブを追加")                       # → "Add job"（英語のとき）
    tr("{n}件のタスク", n=3)                 # 差し込みは str.format の名前付き引数

- 原文（ソース上の文字列）は日本語のまま書き、それを辞書のキーにする。訳は
  `locales/<言語>.json` に {原文: 訳} で持つ。訳が無ければ原文をそのまま返す
  （訳の抜けは tests/test_i18n.py が検出する）
- 表示言語は起動時に1回だけ set_language() で決める（切り替えは再起動で反映）。
  既定は日本語で、テストも日本語で動く
- 翻訳の対象として集める文字列（tests/test_i18n.py の抽出規則）:
  `tr("…")`、`N_("…")`（その場では訳さず、後で tr(変数) で訳す定数に付ける印）、
  `@undoable("…")` と `undo_group("…")` のラベル（表示するときに訳す）
- 日付は全言語で 2026-09-24 形式に統一する（言語ごとの書式に切り替えない）
"""

import json
import sys
from pathlib import Path

# (コード, その言語での呼び名)。オプション画面の選択肢の順
LANGUAGES = (
    ("ja", "日本語"),
    ("en", "English"),
    ("vi", "Tiếng Việt"),
    ("zh_CN", "简体中文"),
)
SOURCE_LANGUAGE = "ja"

_language = SOURCE_LANGUAGE
_catalog = {}


def locales_dir():
    """辞書の置き場所。PyInstaller の1ファイル exe では、起動時に展開される
    一時フォルダ（sys._MEIPASS）の下に同梱している（packaging/ の spec）。"""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent
    return root / "locales"


def load_catalog(language):
    """{原文: 訳}。日本語（原文）なら空。"""
    if language == SOURCE_LANGUAGE:
        return {}
    path = locales_dir() / f"{language}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def set_language(language):
    """表示言語を決める。対応していない言語・辞書が読めない場合は日本語のまま。"""
    global _language, _catalog
    if language not in dict(LANGUAGES):
        language = SOURCE_LANGUAGE
    try:
        catalog = load_catalog(language)
    except (OSError, ValueError):
        language, catalog = SOURCE_LANGUAGE, {}
    _language, _catalog = language, catalog


def current_language():
    return _language


def tr(text, /, **kwargs):
    """原文 text を今の表示言語に訳す。kwargs は訳文の {名前} に差し込む
    （text は位置専用なので、差し込みの名前に text を使ってもぶつからない）。"""
    translated = _catalog.get(text) or text
    return translated.format(**kwargs) if kwargs else translated


def N_(text, /):
    """翻訳の対象だと印を付けるだけで、その場では訳さない（定数の表など、
    表示するときに tr(変数) で訳すもの）。"""
    return text
