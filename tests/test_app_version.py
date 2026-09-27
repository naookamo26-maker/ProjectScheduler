"""
アプリのバージョン（app_version.py）が、画面・.exe・利用者ガイドで同じ番号として
使える形になっていることを確かめる。
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app_version import APP_VERSION, windows_version_tuple  # noqa: E402

pytestmark = pytest.mark.core


def test_version_is_three_numbers():
    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION)


def test_windows_version_tuple_has_four_numbers():
    assert windows_version_tuple("1.0.0") == (1, 0, 0, 0)
    assert windows_version_tuple("2.13.4") == (2, 13, 4, 0)
    assert windows_version_tuple() == tuple(int(p) for p in APP_VERSION.split(".")) + (0,)


def test_spec_and_user_guide_read_the_single_definition():
    # 番号を書き写さず、app_version.py から読むこと（上げ忘れ・食い違いを防ぐ）
    spec = (ROOT / "packaging" / "ProjectSchedulerGUI.spec").read_text(encoding="utf-8")
    assert "from app_version import" in spec
    assert "version=VERSION_FILE" in spec
    guide = (ROOT / "scripts" / "user_guide" / "build_pdf.py").read_text(encoding="utf-8")
    assert "from app_version import APP_VERSION" in guide
