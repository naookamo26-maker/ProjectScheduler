"""アプリのバージョン。

ここが唯一の定義で、次の3か所がこれを読む。バージョンを上げるときは、ここだけを変える。

- 画面: 「ヘルプ」→「バージョン情報」（gui/main.py）
- .exe: ファイルのプロパティに出るバージョン情報（packaging/ProjectSchedulerGUI.spec）
- 利用者ガイド: 表紙の対象バージョン（scripts/user_guide/build_pdf.py）

番号は「メジャー.マイナー.パッチ」の3つの数字で書く。
"""

APP_VERSION = "1.1.1"


def windows_version_tuple(version=APP_VERSION):
    """Windows の実行ファイルのバージョン情報に使う4つの数字（1.0.0 → (1, 0, 0, 0)）。"""
    parts = [int(p) for p in version.split(".")]
    return tuple((parts + [0, 0, 0, 0])[:4])
