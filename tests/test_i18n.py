"""
多言語対応（docs/roadmap.md §11）の訳の抜けを検出するテスト。

ソースから翻訳の対象（i18n.py の冒頭に書いた抽出規則）を集め、各言語の辞書
（locales/*.json）と突き合わせる。文言を足したり変えたりしたのに訳を更新して
いなければ、ここで失敗する（「文言を変えたコミットで4言語すべての訳を更新する」
という CLAUDE.md の作業ルールを機械的に守らせる）。
"""

import ast
import json
import re
import string
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import i18n  # noqa: E402

# 分類: core（pytestのみ。ソースを読むだけで、PySide6・pandasは要らない）
pytestmark = pytest.mark.core

SOURCE_FILES = sorted(
    [ROOT / "project_scheduler.py", ROOT / "i18n.py"] + list((ROOT / "gui").glob("*.py"))
)
TRANSLATED_LANGUAGES = [code for code, _label in i18n.LANGUAGES if code != i18n.SOURCE_LANGUAGE]


# 翻訳の対象として集める呼び出し（関数名 → 対象の引数の位置）
_MARKERS = {
    "tr": 0, "N_": 0, "undoable": 0, "undo_group": 0, "begin_undo_group": 0, "bind_undo_session": 2,
}
# 翻訳しない呼び出し（ログ・コンソール出力。GUI には出ない）。ログは logger.warning(…)
# のようにロガーのメソッドとして呼んだものだけ（QMessageBox.warning(…) は画面に出るので対象）
_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
_LOGGERS = {"logger", "logging", "log", "_logger"}
# 翻訳しない定数（言語の選択肢は、それぞれの言語での呼び名のまま出す。SQL・ログ・HTML
# テンプレートの日本語はコメントとログだけ。HTMLの文言は _html_texts() で訳して差し込む）
_EXEMPT_ASSIGNMENTS = {"LANGUAGES", "_SCHEMA_SQL", "_LEVEL_NAME_JA", "_PLOTLY_GANTT_HTML_TEMPLATE"}
# 翻訳しない関数（日本の祝日の名前は日付の計算のためだけに持ち、画面に出さない）
_EXEMPT_FUNCTIONS = {"_fixed_and_moving_jp_holidays"}
_JA = re.compile(r"[぀-ヿ㐀-鿿＀-￯]")


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _scan(path):
    """(翻訳の対象の原文の集合, 包んでいない日本語の文字列の [(行, 文字列)])"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    marked, marked_ids, exempt_ids = set(), set(), _docstring_ids(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in _MARKERS and len(node.args) > _MARKERS[name]:
            arg = node.args[_MARKERS[name]]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                marked.add(arg.value)
                marked_ids.add(id(arg))
        func = node.func
        is_log = (isinstance(func, ast.Name) and func.id == "print") or (
            isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS
            and isinstance(func.value, ast.Name) and func.value.id in _LOGGERS
        )
        if is_log:
            for sub in ast.walk(node):
                exempt_ids.add(id(sub))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in _EXEMPT_FUNCTIONS:
            for sub in ast.walk(node):
                exempt_ids.add(id(sub))
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _EXEMPT_ASSIGNMENTS for t in node.targets):
            for sub in ast.walk(node):
                exempt_ids.add(id(sub))
    bare = []
    for node in ast.walk(tree):
        if id(node) in marked_ids or id(node) in exempt_ids:
            continue
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
            if _JA.search(text):
                bare.append((node.lineno, text))
            for v in node.values:
                exempt_ids.add(id(v))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _JA.search(node.value):
            bare.append((node.lineno, node.value))
    return marked, bare


def _source_strings():
    strings = set()
    for path in SOURCE_FILES:
        strings |= _scan(path)[0]
    return strings


def _placeholders(text):
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}


def _catalog(language):
    return json.loads((ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("language", TRANSLATED_LANGUAGES)
def test_every_marked_string_is_translated(language):
    catalog = _catalog(language)
    missing = sorted(s for s in _source_strings() if not catalog.get(s))
    assert not missing, f"{language} の訳がありません: {missing[:20]}（全{len(missing)}件）"


@pytest.mark.parametrize("language", TRANSLATED_LANGUAGES)
def test_no_stale_translations(language):
    stale = sorted(set(_catalog(language)) - _source_strings())
    assert not stale, f"{language} にソースから消えた原文の訳が残っています: {stale[:20]}"


@pytest.mark.parametrize("language", TRANSLATED_LANGUAGES)
def test_placeholders_match(language):
    wrong = [
        (src, dst) for src, dst in _catalog(language).items()
        if _placeholders(src) != _placeholders(dst)
    ]
    assert not wrong, f"{language} の差し込み（{{名前}}）が原文と食い違っています: {wrong[:10]}"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_no_bare_japanese(path):
    """tr() 等で包んでいない日本語の文字列（docstring・ログ・対象外の定数を除く）が無い。"""
    bare = _scan(path)[1]
    assert not bare, f"{path.name} に tr() で包んでいない日本語の文字列があります: {bare[:20]}"


def test_tr_falls_back_to_the_source_and_formats():
    i18n.set_language("ja")
    assert i18n.tr("{n}件", n=3) == "3件"
    assert i18n.N_("原文") == "原文"


def test_set_language_loads_the_catalog_and_ignores_unknown_languages():
    try:
        i18n.set_language("en")
        assert i18n.current_language() == "en"
        i18n.set_language("xx")
        assert i18n.current_language() == "ja"
    finally:
        i18n.set_language("ja")


def _tr_calls_evaluated_at_import(path):
    """モジュール直下・クラス直下・既定引数で呼んでいる tr()（表示言語を決める前の
    import 時に評価されて、日本語のまま固まってしまう）。定数には N_() を使い、
    表示するときに tr(定数) で訳す。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr"):
            continue
        child, parent = node, parents.get(node)
        at_import = True
        while parent is not None:
            if isinstance(parent, ast.arguments):
                break  # 既定引数
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and child is not parent.args:
                at_import = child in parent.decorator_list if hasattr(parent, "decorator_list") else False
                break
            child, parent = parent, parents.get(parent)
        if at_import:
            found.append(node.lineno)
    return found


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_tr_is_not_evaluated_at_import_time(path):
    lines = _tr_calls_evaluated_at_import(path)
    assert not lines, f"{path.name} の {lines} 行目の tr() は import 時に評価されます（N_() を使う）"
