"""
リソース制約付きプロジェクトスケジューラー（新フォーマット対応版）

対応フォーマット:
  Project (1行) / Milestones(End_Dateのみ) / Teams / Holidays /
  Workflows / Jobs(Priority列あり) / Job_Tasks(External_Depends廃止) /
  External_Dependencies(新設)

前バージョンからの主な変更点:
1. マイルストーン単位のStart_Dateを廃止し、Projectシートの単一Start_Dateを
   全タスク共通の絶対下限として使用。リソース不足で前倒しを続けてもこの日を
   超えられない場合は ResourceOverflowError にする（恣意的な日数上限は廃止）。
2. External_Depends（コロン区切り文字列）を廃止し、External_Dependencies
   シート（1依存=1行、列で構造化）に変更。入力ミスやパースミスを構造的に防止。
3. Jobs シートに Priority 列を追加。リソース競合時、優先度の高いジョブの
   タスクを先に処理して希望どおりの日程を確保し、優先度の低い方を押し出す
   （処理順は _build_leveling_order を参照）。
4. Holidays シート（全社共通日 or チーム別）を追加。休業日はそのチームの
   ライン数を実質0として扱い、その日をまたぐ配置を避ける。
5. スケジューリング順序を「後続タスクが先」というトポロジカル制約 +
   「同じ準備完了状態ならPriority優先」という優先度付きKahn法に統一。
   （前バージョンの raw日付ソートは、真のトポロジカル順序を保証しなかった）
6. 循環依存は明示的に検出してエラーにする。

v5での変更点:
7. ワークフローIDだけでは何の制作物か分かりづらいため、Excel側に任意の
   "Workflow_Names" シート（Workflow_ID / Workflow_Name）を追加できるように
   対応。指定があればガントチャートの見出しにその名前を使う（未指定時はID）。
   同様に Teams シートの Team_Name 列があればチーム別チャートの見出しに使う。

v6での変更点:
8. すべてのガントチャートに、プロジェクト開始日と各マイルストーン
   （Milestonesシート）を表示するようにした。
9. 上記のマイルストーン群はプロジェクト全体で共通（同じID・同じ日付）なので、
   すべてのチャートに同じマイルストーンを含めることで、表示期間（軸の範囲）も
   チャート間で揃うようにした（横並び比較がしやすい）。

v7での変更点:
10. 従来の「ALAP（締切から逆算した最遅日程）でリソース平準化した後、依存元が
    終わり次第すぐ着手するASAP方向へ前倒しする」という2段階方式を廃止した。
    このASAP前倒しパスが、締切までまだ余裕があるタスクまで軒並みプロジェクト
    開始直後に詰め込んでしまい、非現実的な偏りを生む原因になっていたため。
11. 代わりに、各タスクの「依存関係のみを考慮した最速日程（ASAP）」と
    「締切から逆算した最遅日程（ALAP）」の両方を求め、その間（スラック）の
    どこに配置するかを distribution_ratio（既定0.5）で制御する方式にした。
    基準点にチームの空きが無い場合は締切側・着手可能日側の順に探索範囲を
    広げる。結果として、締切に間に合わせつつプロジェクト全体期間になるべく
    分散した日程になる（v8以降、それでも収まらない場合は締切を超過した日程を
    返し、超過日数を Deadline_Overrun_Days 列で報告する。下記14を参照）。
12. ログ出力のレベル名（INFO/WARNING/ERROR等）を日本語（情報/警告/エラー等）に
    変更した。

v8での変更点（大規模プロジェクトへの対応）:
13. リソース平準化の処理順を、優先度付きの *前方向* トポロジカル順
    （_build_leveling_order）に修正した。v7で平準化を前進型に変えた際、
    逆方向Kahn順（優先度の高いものが先頭に来る）をそのまま reversed() して
    使っていたため、優先度の高いジョブほど *最後* にラインを確保することに
    なり、Priorityの効果が反転していた。
14. マイルストーンの締切に間に合わないタスクを ResourceOverflowError に
    せず、可能な限り早い日程へ配置したうえで Deadline_Overrun_Days 列
    （超過日数）として結果に返すようにした。1タスクの超過で全体の日程が
    まったく得られなくなる（＝何がどれだけ間に合わないのかも分からない）
    のを避けるため。ResourceOverflowError は、締切を無視しても置き場所が
    見つからない場合にのみ送出する。
15. 稼働日の判定と営業日の加減算を _WorkCalendar（チーム別の稼働日を
    序数添字の配列として事前計算）に置き換え、チームの使用ライン数も
    numpy配列で持つようにした。日付は内部では序数(int)のまま扱う。
    参照表の事前辞書化（_parse_tasks）と合わせて、16,000タスク規模で
    スケジューリング所要時間が約9分の1になっている。
16. Mermaidガントチャート（Markdown）の出力を廃止した。規模が大きくなると
    Mermaid側のレンダラーが持つ文字数上限に掛かって表示できず、実用に
    ならないため。出力はインタラクティブHTML（Plotly製）に一本化する。
17. マイルストーンの締切に間に合わないタスクを、HTMLガントチャートと
    GUIのガントチャートタブの両方で赤く太い枠線で強調するようにした。
"""

import hashlib
import heapq
import logging
import re
from datetime import date as date_cls, timedelta

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ログレベル名（INFO/WARNING/ERROR等）を日本語表示にする。
_LEVEL_NAME_JA = {
    "DEBUG": "デバッグ",
    "INFO": "情報",
    "WARNING": "警告",
    "ERROR": "エラー",
    "CRITICAL": "重大",
}
for _lvl, _ja in _LEVEL_NAME_JA.items():
    logging.addLevelName(getattr(logging, _lvl), _ja)


class SchedulingError(Exception):
    """スケジューリング処理全般の基底エラー"""


class MissingSheetOrColumnError(SchedulingError):
    pass


class MissingMilestoneError(SchedulingError):
    pass


class CircularDependencyError(SchedulingError):
    pass


class ResourceOverflowError(SchedulingError):
    pass


DEFAULT_LOW_PRIORITY = 999  # Priority未指定タスクのフォールバック（最後に処理＝押し出されやすい）

# マイルストーン未指定のジョブのフォールバック（一番遅いマイルストーンの締切と
# みなす。マイルストーンが1件も無いプロジェクトでは、開発開始日からこの年数後を
# 仮の締切とみなす）。
MISSING_MILESTONE_FALLBACK_YEARS = 5

#: 依存関係の種別。FS = Finish-to-Start（先行の完了後に開始）、
#: SS = Start-to-Start（先行の開始に合わせて開始）。
DEPENDENCY_KINDS = ("FS", "SS")

#: 種別・ラグを指定しない依存関係の意味（＝この機能が入る前の唯一の挙動）。
DEFAULT_DEPENDENCY = ("FS", 0)

# Internal_Depends 列の1件ぶんの書式: "T_003" / "T_003(SS+2)" / "T_003(FS-1)"。
# 種別・ラグが既定（FS・0）の依存は括弧を付けず、従来とまったく同じ文字列に
# なるようにしている（旧Excel・既存の.pscheduleと相互に読み書きできる）。
_DEP_REF_RE = re.compile(
    r"^(?P<task>[^()\s]+)"
    r"(?:\(\s*(?P<kind>[A-Za-z]{2})\s*(?P<lag>[+-]\s*\d+)?\s*\))?$"
)


def format_dependency_ref(task_id, dep_type="FS", lag_days=0):
    """依存1件を Internal_Depends 列の文字列に変換する。"""
    dep_type = str(dep_type or "FS").upper()
    lag_days = int(lag_days or 0)
    if dep_type == "FS" and lag_days == 0:
        return str(task_id)
    if lag_days == 0:
        return f"{task_id}({dep_type})"
    return f"{task_id}({dep_type}{lag_days:+d})"


def parse_dependency_ref(text):
    """Internal_Depends 列の1件を (Task_ID, 種別, ラグ営業日) に分解する。

    括弧を伴わない従来どおりの書き方は (Task_ID, "FS", 0) になる。
    """
    m = _DEP_REF_RE.match(str(text).strip())
    if m is None:
        raise SchedulingError(
            f"依存関係の書き方 '{text}' を解釈できません"
            f"（'T_003' / 'T_003(SS+2)' / 'T_003(FS-1)' の形で指定してください）"
        )
    kind = (m.group("kind") or "FS").upper()
    if kind not in DEPENDENCY_KINDS:
        raise SchedulingError(
            f"依存関係の種別 '{kind}'（'{text}'）は不正です"
            f"（{' / '.join(DEPENDENCY_KINDS)} のいずれか）"
        )
    lag = m.group("lag")
    return m.group("task"), kind, int(lag.replace(" ", "")) if lag else 0

# チーム別色分け（Plotlyガントチャート／ガントチャートタブ／ワークフロー設計）
# 用の固定カテゴリカルパレット（12色、順序固定）。バー内に黒文字（#0b0b0b）を
# 重ねて表示する用途で使うため、彩度を抑え明度を高めにした「淡い配色」にして
# いる。並び替えたり途中に色を挿し込んだりしない（挿し込むと隣接色の分離が
# 崩れるため、増やす場合は末尾に追加する）。13チーム目以降は
# _TEAM_COLOR_OVERFLOW（無彩色）に折りたたむ。
#
# 前半8色（インデックス0-7）は色相を45度ずつ均等配置し、明度・彩度も
# 色ごとに変えることで、色覚多様性(CVD)シミュレーション下でも隣接色を
# なるべく判別できるようにしてある（ただし彩度を抑えている分、旧パレット
# （彩度100%・明度差が非常に大きかった）ほどのCVD耐性は無いため、色だけに
# 頼らず凡例・ホバーのチーム名表示と併用する前提が重要）。
# 後半4色（インデックス8-11）は、新たな色相を割り込ませると前半8色の分離が
# 崩れてしまうため、前半1-4番目と同じ色相のまま明度を下げた別トーンとして
# 追加してある（13チーム目以降での完全な無彩色フォールバックより先に、
# 「系統は近いが同一ではない」色を用意する段階を挟む狙い）。
_TEAM_COLOR_PALETTE = [
    "#dd9588", "#e8e2b0", "#a0da81", "#a8e6c1",
    "#90d3df", "#b8bdea", "#b779d8", "#e3a0c9",
    "#d05843", "#dacf6c", "#6fce3b", "#64d892",
]
_TEAM_COLOR_OVERFLOW = "#cbc9c2"


def _build_team_color_map(team_ids_in_order, team_name_map):
    """チームID（登場順・固定）から表示名 -> 色 の辞書を作る。"""
    color_map = {}
    for i, team_id in enumerate(team_ids_in_order):
        display = team_name_map.get(str(team_id), str(team_id))
        color = _TEAM_COLOR_PALETTE[i] if i < len(_TEAM_COLOR_PALETTE) else _TEAM_COLOR_OVERFLOW
        color_map.setdefault(display, color)
    return color_map


# ---------------------------------------------------------------------------
# 日本の祝日を自動計算する（Holidaysシートへの手入力なしで土日祝日を休業日にするため）
#
# 制約・既知の限界:
# - 春分の日・秋分の日は 1980〜2099 年の範囲で成立する近似式を使用（国立天文台公表の
#   計算式に基づく一般的な近似で、実際の官報確定日とまれにズレる可能性がある）
# - 2020年（オリンピック開催に伴う海の日・体育の日・山の日の特例移動）と
#   2021年（同様の特例）は未対応。対象期間にこれらの年を含む場合は要注意
# - 1999年以前の制度（成人の日が1/15固定 等、ハッピーマンデー導入前）は未対応
# ---------------------------------------------------------------------------

def _vernal_equinox_day(year):
    return int(20.8431 + 0.242194 * (year - 1980) - int((year - 1980) / 4))


def _autumnal_equinox_day(year):
    return int(23.2488 + 0.242194 * (year - 1980) - int((year - 1980) / 4))


def _nth_weekday(year, month, weekday, n):
    """指定月内の第n weekday（月曜=0）の date を返す"""
    d = date_cls(year, month, 1)
    add = (weekday - d.weekday()) % 7
    return date_cls(year, month, 1 + add + 7 * (n - 1))


def _fixed_and_moving_jp_holidays(year):
    """振替休日・国民の休日を適用する前の、その年の祝日 {date: 名称}"""
    h = {}
    h[date_cls(year, 1, 1)] = "元日"
    h[_nth_weekday(year, 1, 0, 2)] = "成人の日"
    h[date_cls(year, 2, 11)] = "建国記念の日"
    if year >= 2020:
        h[date_cls(year, 2, 23)] = "天皇誕生日"
    h[date_cls(year, 3, _vernal_equinox_day(year))] = "春分の日"
    h[date_cls(year, 4, 29)] = "昭和の日" if year >= 2007 else "みどりの日"
    h[date_cls(year, 5, 3)] = "憲法記念日"
    if year >= 2007:
        h[date_cls(year, 5, 4)] = "みどりの日"
    h[date_cls(year, 5, 5)] = "こどもの日"
    h[_nth_weekday(year, 7, 0, 3)] = "海の日"
    if year >= 2016:
        h[date_cls(year, 8, 11)] = "山の日"
    h[_nth_weekday(year, 9, 0, 3)] = "敬老の日"
    h[date_cls(year, 9, _autumnal_equinox_day(year))] = "秋分の日"
    h[_nth_weekday(year, 10, 0, 2)] = "スポーツの日" if year >= 2020 else "体育の日"
    h[date_cls(year, 11, 3)] = "文化の日"
    h[date_cls(year, 11, 23)] = "勤労感謝の日"
    return h


def generate_jp_holidays(start_year, end_year):
    """
    start_year〜end_year（両端含む）の日本の祝日を、振替休日・国民の休日の
    適用まで含めて計算し、pd.Timestamp の set で返す。
    """
    base = {}
    # 振替休日が年をまたぐケースに備えて前後1年分を含めて計算する
    for y in range(start_year - 1, end_year + 2):
        base.update(_fixed_and_moving_jp_holidays(y))

    holiday_set = set(base.keys())

    # 振替休日: 日曜にあたる祝日の翌日以降で最初の非祝日を休日にする
    for d in sorted(base.keys()):
        if d.weekday() == 6:  # Sunday
            nd = d + timedelta(days=1)
            while nd in holiday_set:
                nd += timedelta(days=1)
            holiday_set.add(nd)

    # 国民の休日: 前後を祝日に挟まれた非祝日（日曜を除く）を休日にする
    range_start = date_cls(start_year - 1, 1, 1)
    range_end = date_cls(end_year + 1, 12, 31)
    to_add = set()
    day = range_start
    while day <= range_end:
        if day not in holiday_set and day.weekday() != 6:
            if (day - timedelta(days=1)) in holiday_set and (day + timedelta(days=1)) in holiday_set:
                to_add.add(day)
        day += timedelta(days=1)
    holiday_set |= to_add

    return {pd.Timestamp(d) for d in holiday_set if start_year <= d.year <= end_year}


def _require_columns(df, required_cols, sheet_name):
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise MissingSheetOrColumnError(
            f"シート '{sheet_name}' に必須列が不足しています: {missing}"
        )


def _load_data_from_frames(df_project, df_teams, df_ms, df_wf, df_jobs,
                            df_jtasks=None, df_holidays=None, df_extdeps=None,
                            df_wf_names=None, df_team_capacity=None):
    """
    既に読み込み済みのDataFrame群を検証・整形する（列チェック・インデックス設定・
    任意データの既定値補完）。Excel由来（_load_data経由）・DB由来（GUIの
    gui/gantt_generator.py経由）を問わない共通の入口。

    df_project/df_teams/df_ms/df_wf/df_jobs は必須。
    df_jtasks/df_holidays/df_extdeps/df_wf_names/df_team_capacity は、Noneなら
    「対応するデータがそもそも存在しない」ことを表し既定の空DataFrameを使う。
    DataFrame（0行でも可）を渡した場合は「存在する」ことを表し、Excelで対応
    シートが存在する場合と同じ列検証を行う（Excel側の「シートが存在するか
    どうか」と1対1に対応する）。
    """
    _require_columns(df_project, ["Project_ID", "Project_Name", "Start_Date"], "Project")
    _require_columns(df_teams, ["Team_ID", "Max_Lines"], "Teams")
    _require_columns(df_ms, ["Milestone_ID", "End_Date"], "Milestones")
    _require_columns(df_wf, ["Workflow_ID", "Task_ID", "Task_Name", "Default_Days"], "Workflows")
    _require_columns(df_jobs, ["Job_ID", "Job_Name", "Workflow_ID", "Priority"], "Jobs")

    if df_project.empty:
        raise MissingSheetOrColumnError("Project シートが空です（1行必要）")
    if len(df_project) > 1:
        logger.warning("Project シートに複数行あります。1行目のみ使用します")

    df_ms = df_ms.set_index("Milestone_ID")

    if df_jtasks is not None:
        _require_columns(df_jtasks, ["Job_ID", "Task_ID"], "Job_Tasks")
        df_jtasks = df_jtasks.set_index(["Job_ID", "Task_ID"])
    else:
        df_jtasks = pd.DataFrame()

    if df_holidays is not None:
        if not df_holidays.empty:
            _require_columns(df_holidays, ["Date"], "Holidays")
    else:
        df_holidays = pd.DataFrame(columns=["Date", "Team_ID"])

    if df_extdeps is not None:
        if not df_extdeps.empty:
            _require_columns(
                df_extdeps,
                ["Job_ID", "Task_ID", "Depends_On_Job_ID", "Depends_On_Task_ID"],
                "External_Dependencies",
            )
    else:
        df_extdeps = pd.DataFrame(columns=["Job_ID", "Task_ID", "Depends_On_Job_ID", "Depends_On_Task_ID"])

    # Workflow_Names（任意）: Workflow_ID だけでは何の制作物か分かりづらいので、
    # ガントチャートの見出しに使う表示名を別途定義できるようにする。
    if df_wf_names is not None:
        if not df_wf_names.empty:
            _require_columns(df_wf_names, ["Workflow_ID", "Workflow_Name"], "Workflow_Names")
    else:
        df_wf_names = pd.DataFrame(columns=["Workflow_ID", "Workflow_Name"])

    # Team_Capacity_Changes（任意）: チームの同時ライン数（Teams.Max_Lines）を
    # 開発開始日からの既定値としつつ、途中の日付から変動させたい場合の変更点。
    if df_team_capacity is not None:
        if not df_team_capacity.empty:
            _require_columns(
                df_team_capacity, ["Team_ID", "Start_Date", "Lines"], "Team_Capacity_Changes"
            )
    else:
        df_team_capacity = pd.DataFrame(columns=["Team_ID", "Start_Date", "Lines"])

    return (df_project, df_teams, df_ms, df_wf, df_jobs, df_jtasks, df_holidays, df_extdeps,
            df_wf_names, df_team_capacity)


def _load_data(excel_file):
    """Excelファイル（またはExcel形式のバイト列/バッファ）を読み込み、
    _load_data_from_frames に検証・整形を委譲する。"""
    try:
        xls = pd.ExcelFile(excel_file)
    except Exception as e:
        raise MissingSheetOrColumnError(f"Excelファイルを開けませんでした: {excel_file} ({e})")

    for required_sheet in ["Project", "Teams", "Milestones", "Workflows", "Jobs"]:
        if required_sheet not in xls.sheet_names:
            raise MissingSheetOrColumnError(f"必須シート '{required_sheet}' が見つかりません")

    df_project = pd.read_excel(xls, sheet_name="Project")
    df_teams = pd.read_excel(xls, sheet_name="Teams")
    df_ms = pd.read_excel(xls, sheet_name="Milestones")
    df_wf = pd.read_excel(xls, sheet_name="Workflows")
    df_jobs = pd.read_excel(xls, sheet_name="Jobs")

    df_jtasks = pd.read_excel(xls, sheet_name="Job_Tasks") if "Job_Tasks" in xls.sheet_names else None
    df_holidays = pd.read_excel(xls, sheet_name="Holidays") if "Holidays" in xls.sheet_names else None
    df_extdeps = (
        pd.read_excel(xls, sheet_name="External_Dependencies")
        if "External_Dependencies" in xls.sheet_names else None
    )
    df_wf_names = pd.read_excel(xls, sheet_name="Workflow_Names") if "Workflow_Names" in xls.sheet_names else None
    df_team_capacity = (
        pd.read_excel(xls, sheet_name="Team_Capacity_Changes")
        if "Team_Capacity_Changes" in xls.sheet_names else None
    )
    return _load_data_from_frames(df_project, df_teams, df_ms, df_wf, df_jobs,
                                   df_jtasks, df_holidays, df_extdeps, df_wf_names,
                                   df_team_capacity)


def _load_project_start(df_project):
    start = pd.to_datetime(df_project.iloc[0]["Start_Date"])
    if pd.isna(start):
        raise MissingSheetOrColumnError("Project シートの Start_Date が空です")
    return start


def _load_holidays(df_holidays):
    """休業日を (全社共通の set, チーム別 dict[team_id -> set]) に変換する"""
    holidays_all = set()
    holidays_by_team = {}
    for _, row in df_holidays.iterrows():
        d = pd.to_datetime(row.get("Date"))
        if pd.isna(d):
            continue
        team = row.get("Team_ID")
        if not pd.notna(team) or str(team).strip() == "":
            holidays_all.add(d)
        else:
            holidays_by_team.setdefault(str(team).strip(), set()).add(d)
    return holidays_all, holidays_by_team


def _build_external_dep_map(df_extdeps):
    """(Job_ID, Task_ID) -> ["Depends_On_Job_ID:Depends_On_Task_ID", ...] のマップを作る"""
    dep_map = {}
    for _, row in df_extdeps.iterrows():
        job_id, task_id = row.get("Job_ID"), row.get("Task_ID")
        dep_job, dep_task = row.get("Depends_On_Job_ID"), row.get("Depends_On_Task_ID")
        if not (pd.notna(job_id) and pd.notna(task_id) and pd.notna(dep_job) and pd.notna(dep_task)):
            logger.warning(f"External_Dependencies に不完全な行があります（スキップ）: {row.to_dict()}")
            continue
        key = (job_id, task_id)
        dep_map.setdefault(key, []).append(f"{dep_job}:{dep_task}")
    return dep_map


# ---------------------------------------------------------------------------
# 無効化されたタスク（Is_Active = N）をまたぐ依存の橋渡し
#
# ジョブ単位でタスクを1つ外すと、そのタスクを経由していた依存の鎖に穴が空く。
# 穴の前後を繋ぎ直さないと、後続タスクは先行タスクの完了を待たずに着手できる
# ことになり、外したタスクとは無関係な工程まで一斉に前倒しされてしまう
# （A→B→C の B を外すと、C が A を待たなくなる）。実務上これはほぼ確実に
# 意図と違うため、外したタスクは「所要0日で素通しする穴」とみなし、その
# 上流と下流を直接繋ぐ。
#
# 種別・ラグの合成: 穴になったタスク B が所要0日なら B の開始日＝完了日なので、
#   X --(k1, l1)--> B --(k2, l2)--> C
# のとき C の下限は
#   anchor(B, k2) + l2 = B.開始 + l2 = anchor(X, k1) + l1 + l2
# となる（k2 が FS でも SS でも B の開始＝完了なので同じ）。よって合成後は
# 「種別は上流側の k1 を引き継ぎ、ラグは足し合わせる」。穴が連続している
# 場合も同じ規則を繰り返し適用すればよい。
# ---------------------------------------------------------------------------

def _task_dependency_refs(job_id, wf_task_row, ext_dep_map):
    """1タスクの依存を (依存先g_id, 種別, ラグ) のリストに揃えて返す。

    ワークフロー内の依存（Internal_Depends、種別・ラグ付き）と、ジョブをまたぐ
    依存（External_Dependencies、現状は常に FS・ラグ0）を同じ形にして、
    橋渡し処理が両者を区別せずに扱えるようにする。
    """
    refs = []
    internal_depends = wf_task_row.get("Internal_Depends")
    if pd.notna(internal_depends):
        for ref in str(internal_depends).split(","):
            if not ref.strip():
                continue
            dep_task, dep_kind, dep_lag = parse_dependency_ref(ref)
            refs.append((f"{job_id}:{dep_task}", dep_kind, dep_lag))
    for ext_g_id in ext_dep_map.get((job_id, wf_task_row["Task_ID"]), []):
        refs.append((ext_g_id, *DEFAULT_DEPENDENCY))
    return refs


def _tighter_dependency(a, b):
    """同じ依存先へ複数の経路で辿り着いた場合に、制約として厳しい方を返す。

    FS は先行タスクの「完了」、SS は「開始」を起点にするため、同じラグなら
    FS の方が必ず遅い（＝厳しい）。同じ種別ならラグが大きい方が厳しい。
    """
    a_kind, a_lag = a
    b_kind, b_lag = b
    if a_kind != b_kind:
        return a if a_kind == "FS" else b
    return a if a_lag >= b_lag else b


def _merge_dependency(resolved, dep_g_id, spec):
    """resolved（{依存先g_id: (種別, ラグ)}）へ1件を、厳しい方を残して畳み込む。"""
    current = resolved.get(dep_g_id)
    resolved[dep_g_id] = spec if current is None else _tighter_dependency(current, spec)


def _resolve_inactive_ref(dep_g_id, active_ids, inactive_deps, cache, path):
    """無効化されたタスク dep_g_id を、実際に待つべきアクティブなタスクの集合へ
    展開する。戻り値は ({アクティブなg_id: (種別, ラグ)}, 見つからなかった依存先)。

    ラグは dep_g_id 自身までの累積で、呼び出し側が「穴より下流のラグ」を
    足し込む。上流も無効化されていれば再帰的に辿る（穴が連続する場合）。

    無効化されたタスクだけで閉じた循環は、その経路を打ち切って解消する
    （アクティブなタスク間の循環は _build_scheduling_order が別途検出する。
    ここで例外にすると、既に無効化して使っていないタスクのせいで
    スケジューリング全体が止まってしまう）。
    """
    if dep_g_id in cache:
        return cache[dep_g_id]
    if dep_g_id in path:
        return {}, frozenset()

    resolved = {}
    missing = set()
    for up_id, kind, lag in inactive_deps.get(dep_g_id, []):
        if up_id in active_ids:
            _merge_dependency(resolved, up_id, (kind, lag))
        elif up_id in inactive_deps:
            up_resolved, up_missing = _resolve_inactive_ref(
                up_id, active_ids, inactive_deps, cache, path | {dep_g_id}
            )
            for a_id, (a_kind, a_lag) in up_resolved.items():
                _merge_dependency(resolved, a_id, (a_kind, a_lag + lag))
            missing |= up_missing
        else:
            missing.add(up_id)

    result = (resolved, frozenset(missing))
    cache[dep_g_id] = result
    return result


def _bridge_inactive_deps(g_id, refs, active_ids, inactive_deps, cache):
    """1タスクぶんの依存を、無効化されたタスクを飛ばした形に解決する。

    Returns: (解決後の {依存先g_id: (種別, ラグ)}, 橋渡しの内訳, 見つからなかった依存先)
    """
    resolved = {}
    bridged = {}
    missing = set()
    for dep_g_id, kind, lag in refs:
        if dep_g_id in active_ids:
            candidates = {dep_g_id: (kind, lag)}
        elif dep_g_id in inactive_deps:
            upstream, up_missing = _resolve_inactive_ref(
                dep_g_id, active_ids, inactive_deps, cache, frozenset()
            )
            candidates = {a: (k, l + lag) for a, (k, l) in upstream.items()}
            missing |= up_missing
            bridged[dep_g_id] = sorted(candidates)
        else:
            missing.add(dep_g_id)
            continue
        for a_id, spec in candidates.items():
            # 穴を通って自分自身へ戻る経路（循環）は無視する。
            if a_id != g_id:
                _merge_dependency(resolved, a_id, spec)
    return resolved, bridged, missing


def _parse_tasks(df_teams, df_ms, df_wf, df_jobs, df_jtasks, df_extdeps, project_start):
    teams_dict = df_teams.set_index("Team_ID")["Max_Lines"].to_dict()
    ext_dep_map = _build_external_dep_map(df_extdeps)

    # ループ内で参照する表は、すべて先に素の辞書・リストへ落としておく
    # （pandasの行アクセスはタスク数が増えるとここが最も重くなるため）。
    ms_end_map = {}
    for ms_id, ms_row in df_ms.iterrows():
        ms_end_map[ms_id] = pd.to_datetime(ms_row.get("End_Date"))

    # マイルストーン未指定のジョブのフォールバック先（一番締切が遅い
    # マイルストーン）。マイルストーンが1件も定義されていない、または
    # 全件End_Dateが空の場合はNone（呼び出し側で開発開始日+5年を使う）。
    _valid_ms_ends = {mid: end for mid, end in ms_end_map.items() if pd.notna(end)}
    latest_ms_id = max(_valid_ms_ends, key=lambda mid: _valid_ms_ends[mid]) if _valid_ms_ends else None
    fallback_ms_end = (
        project_start + pd.DateOffset(years=MISSING_MILESTONE_FALLBACK_YEARS)
    )

    wf_tasks_by_id = {}
    for row in df_wf.to_dict("records"):
        wf_tasks_by_id.setdefault(row.get("Workflow_ID"), []).append(row)

    overrides_by_key = {}
    if df_jtasks is not None and not df_jtasks.empty:
        for key, row in zip(df_jtasks.index, df_jtasks.to_dict("records")):
            overrides_by_key[key] = row

    active_tasks = {}
    # 依存の解決は全ジョブを読み終えてから行う（後ろのジョブのタスクを
    # 参照する依存があるため）。無効化されたタスクも、その依存だけは
    # 覚えておく——前後を繋ぎ直す「穴」として使う（_bridge_inactive_deps 参照）。
    raw_deps = {}
    inactive_deps = {}

    for job in df_jobs.to_dict("records"):
        job_id, job_name = job["Job_ID"], job["Job_Name"]
        wf_id = job["Workflow_ID"]
        job_default_ms = job.get("Default_Milestone_ID", "")

        priority = job.get("Priority")
        if not pd.notna(priority):
            logger.warning(f"Job '{job_id}' の Priority が未指定です。既定値 {DEFAULT_LOW_PRIORITY}（最低優先）を使用します")
            priority = DEFAULT_LOW_PRIORITY
        else:
            priority = float(priority)

        wf_tasks = wf_tasks_by_id.get(wf_id, [])
        if not wf_tasks:
            logger.warning(f"Job '{job_id}' の Workflow_ID '{wf_id}' に該当するタスクが Workflows に見つかりません")

        for t in wf_tasks:
            t_id = t["Task_ID"]
            g_id = f"{job_id}:{t_id}"

            override = overrides_by_key.get((job_id, t_id), {})

            # 依存は、このタスクが無効化されていても先に読んでおく。無効化された
            # タスクは依存の鎖に空いた「所要0日の穴」として扱い、その上流と下流を
            # 繋ぎ直すため（_bridge_inactive_deps 参照）。
            deps = _task_dependency_refs(job_id, t, ext_dep_map)

            if str(override.get("Is_Active", "Y")).strip().upper() == "N":
                inactive_deps[g_id] = deps
                continue

            override_days = override.get("Override_Days")
            days = int(override_days) if pd.notna(override_days) else int(t["Default_Days"])
            if days <= 0:
                raise SchedulingError(f"タスク '{g_id}' の所要日数が不正です（{days}日）")

            task_ms = override.get("Milestone_ID")
            if not pd.notna(task_ms) or str(task_ms).strip() == "":
                task_ms = job_default_ms

            team_id = override.get("Team_ID")
            if not pd.notna(team_id) or str(team_id).strip() == "":
                team_id = t.get("Team_ID", "")

            if team_id not in teams_dict:
                logger.warning(
                    f"タスク '{g_id}' の担当チーム '{team_id}' は Teams シートに未定義です。"
                    f"ライン制限なし（無制限）として扱います。"
                )

            if not pd.notna(task_ms) or str(task_ms).strip() == "":
                # マイルストーン未指定: 一番締切が遅いマイルストーンに合わせて
                # 扱う（間に合わないジョブとして誤って埋もれるより、後ろ倒し
                # 気味に見積もる方が安全）。マイルストーンが1件も無い
                # プロジェクトでは、開発開始日から
                # MISSING_MILESTONE_FALLBACK_YEARS 年後を仮の締切とみなす。
                if latest_ms_id is not None:
                    task_ms = latest_ms_id
                    ms_end = ms_end_map[task_ms]
                    logger.info(
                        f"タスク '{g_id}' にマイルストーンが設定されていないため、"
                        f"最も締切が遅いマイルストーン '{task_ms}'（{ms_end.date()}）を仮の締切として扱います"
                    )
                else:
                    ms_end = fallback_ms_end
                    logger.info(
                        f"タスク '{g_id}' にマイルストーンが設定されておらず、プロジェクトにも"
                        f"マイルストーンが1件も無いため、開発開始日から"
                        f"{MISSING_MILESTONE_FALLBACK_YEARS}年後（{ms_end.date()}）を仮の締切として扱います"
                    )
            elif task_ms not in ms_end_map:
                raise MissingMilestoneError(
                    f"タスク '{g_id}' が参照するマイルストーン '{task_ms}' が Milestones シートに見つかりません"
                )
            else:
                ms_end = ms_end_map[task_ms]
                if pd.isna(ms_end):
                    raise MissingMilestoneError(
                        f"マイルストーン '{task_ms}'（タスク '{g_id}' が参照）の End_Date が空です"
                    )

            raw_deps[g_id] = deps

            # 開始固定日（実績確定・外部都合のピン留め）。読めない日付を黙って
            # 無視すると「固定が無かったこと」になり、意図と違う日程が静かに
            # 出てしまうため、他の日付列同様エラーにする。
            start_pin_raw = override.get("Start_Pin_Date")
            start_pin_ord = None
            if pd.notna(start_pin_raw) and str(start_pin_raw).strip() != "":
                start_pin = pd.to_datetime(start_pin_raw, errors="coerce")
                if pd.isna(start_pin):
                    raise SchedulingError(
                        f"タスク '{g_id}' の開始固定日 '{start_pin_raw}' を解釈できません"
                    )
                start_pin_ord = start_pin.toordinal()

            active_tasks[g_id] = {
                "start_pin_ord": start_pin_ord,
                "job_id": job_id, "job_name": job_name, "task_id": t_id,
                "task_name": t["Task_Name"], "days": days,
                # deps / dep_specs は全ジョブを読み終えてから
                # （無効化されたタスクを飛ばして）確定させる。
                "deps": [], "dep_specs": {},
                "milestone": task_ms,
                "team_id": team_id, "ms_end": ms_end, "ms_end_ord": ms_end.toordinal(),
                "priority": priority, "workflow_id": wf_id,
            }

    active_ids = set(active_tasks.keys())
    bridge_cache = {}
    bridged_count = 0
    for g_id, t_data in active_tasks.items():
        resolved, bridged, missing = _bridge_inactive_deps(
            g_id, raw_deps[g_id], active_ids, inactive_deps, bridge_cache
        )
        if missing:
            logger.warning(
                f"タスク '{g_id}' の依存先 {sorted(missing)} は存在しないため無視します"
            )
        for hole_id, upstream in bridged.items():
            bridged_count += 1
            if upstream:
                logger.info(
                    f"タスク '{g_id}' の依存先 '{hole_id}' は無効化されているため、"
                    f"その先行タスク {upstream} への依存に繋ぎ替えます"
                )
            else:
                logger.info(
                    f"タスク '{g_id}' の依存先 '{hole_id}' は無効化されており、"
                    f"さらに手前に待つべきタスクが無いため依存を解除します"
                )
        # 既定（FS・ラグ0）以外の依存だけを dep_specs に載せる。ラグを使って
        # いないプロジェクトでは空のままになり、日程計算のループが従来と
        # まったく同じ経路を通る（大規模データでの速度を落とさないため）。
        t_data["deps"] = list(resolved)
        t_data["dep_specs"] = {
            d: spec for d, spec in resolved.items() if spec != DEFAULT_DEPENDENCY
        }

    if bridged_count:
        logger.info(
            f"無効化されたタスクをまたぐ依存 {bridged_count} 件を、"
            f"その先行タスクへ繋ぎ替えました"
        )

    return teams_dict, active_tasks, active_ids


def _build_scheduling_order(active_tasks, active_ids):
    """
    「後続タスクは必ず先」というトポロジカル制約を満たしつつ、
    複数タスクが同時に準備完了となった場合は Priority（小さいほど優先）で
    処理順を決める、優先度付き逆方向Kahnアルゴリズム。

    戻り値: (successors辞書, スケジューリング順のg_idリスト)
    循環依存があれば CircularDependencyError を送出する。
    """
    successors = {g_id: [] for g_id in active_ids}
    for g_id, t_data in active_tasks.items():
        for dep in t_data["deps"]:
            successors[dep].append(g_id)

    # 逆方向グラフ（task -> dep）での入次数 = 後続タスク数
    remaining_successors = {g_id: len(successors[g_id]) for g_id in active_ids}

    def sort_key(g_id):
        t = active_tasks[g_id]
        # Priority昇順（小さいほど先）、同値ならMilestone締切が遅い方を先に処理、
        # さらに同値ならg_idで安定化
        return (t["priority"], -t["ms_end"].value, g_id)

    heap = [(*sort_key(g_id), g_id) for g_id in active_ids if remaining_successors[g_id] == 0]
    heapq.heapify(heap)

    scheduling_order = []
    while heap:
        *_key, g_id = heapq.heappop(heap)
        scheduling_order.append(g_id)
        for dep in active_tasks[g_id]["deps"]:
            remaining_successors[dep] -= 1
            if remaining_successors[dep] == 0:
                heapq.heappush(heap, (*sort_key(dep), dep))

    if len(scheduling_order) != len(active_ids):
        remaining = active_ids - set(scheduling_order)
        raise CircularDependencyError(
            f"循環依存が検出されました。関係するタスク: {sorted(remaining)}"
        )

    return successors, scheduling_order


def _build_leveling_order(active_tasks, active_ids, successors):
    """
    リソース平準化で「先に空きラインを確保する」順を決める、優先度付き
    *前方向* Kahnアルゴリズム。依存元（predecessor）がすべて確定したタスクの
    中から、Priorityが小さい（＝優先度が高い）ものを先に取り出す。

    _run_leveling は前進型（predecessor -> successor）で、先に処理したタスクが
    先にチームのラインを予約する。したがって「優先度の高いジョブが希望どおりの
    日程を取り、低い方が押し出される」という意図を満たすには、平準化の処理順
    そのものが優先度昇順である必要がある。

    _build_scheduling_order（後続タスクを先に並べる逆方向Kahn）の結果を
    reversed() しただけではこの性質は得られない——逆方向Kahnは優先度の高い
    タスクを列の *先頭* に置くため、反転すると優先度の高いタスクほど *最後* に
    処理され、ラインの確保順が優先度と逆になってしまう。

    循環依存は _build_scheduling_order が先に検出するため、ここでは扱わない。
    """
    remaining_deps = {g_id: len(active_tasks[g_id]["deps"]) for g_id in active_ids}

    def sort_key(g_id):
        t = active_tasks[g_id]
        # Priority昇順（小さいほど先）、同値ならMilestone締切が早い方（＝より
        # 切迫している方）を先に処理し、さらに同値ならg_idで安定化する。
        return (t["priority"], t["ms_end"].value, g_id)

    heap = [(*sort_key(g_id), g_id) for g_id in active_ids if remaining_deps[g_id] == 0]
    heapq.heapify(heap)

    leveling_order = []
    while heap:
        *_key, g_id = heapq.heappop(heap)
        leveling_order.append(g_id)
        for succ in successors[g_id]:
            remaining_deps[succ] -= 1
            if remaining_deps[succ] == 0:
                heapq.heappush(heap, (*sort_key(succ), succ))

    return leveling_order


# 稼働日カレンダーとして事前計算しておく範囲の余裕（営業日→暦日の換算に使う
# 係数と、それとは別に前後へ足す固定日数）。土日だけでも営業日1日あたり暦日
# 1.4日、休業日の多いチームではさらに膨らむため、係数は余裕を持たせてある。
_CALENDAR_MARGIN_FACTOR = 3
_CALENDAR_MARGIN_DAYS = 400

# チームが Teams シートに未定義（＝ライン数無制限）の場合に使う実効ライン数。
# 「常に空きがある」と同じ意味になる十分大きな値。
_UNLIMITED_LINES = np.iinfo(np.int32).max


class _WorkCalendar:
    """
    チーム別の稼働日を、date.toordinal()（＝1日1増える整数）を添字とする配列
    として事前計算し、営業日の加減算・空き判定を定数時間の添字計算で済ませる
    ためのカレンダー。

    従来は (date, team_id) -> bool の休日判定関数を1日ずつ呼びながら日付を
    進めていたが、タスク数が増えると「営業日でN日進む」「空きラインを探して
    1日ずつずらす」処理が実行時間の大半を占めるようになる。そこでチームごとに
    次の3つを先に作り、ループを添字計算に置き換えている。

      work[i] : 添字 i（＝序数 base + i）が稼働日か（bool配列）
      cum[i]  : 添字 i より前（[0, i)）にある稼働日の数
      nth[k]  : k番目（0起点）の稼働日の添字

    これにより
      「稼働日 s から営業日 days 日分の終了日（exclusive）」= nth[cum[s] + days - 1] + 1
      「終了日 e（exclusive）から遡って営業日 days 日分の開始日」= nth[cum[e] - days]
    がいずれも O(1) で求まる。

    チーム別休業日（Holidaysシートでチームを指定した行）を持たないチームは
    稼働日パターンが完全に同じになるため、1つの共通カレンダーを共有する
    （チーム数に比例したメモリ・構築時間を避けるため）。

    日付はすべて序数（int）でやり取りする。pd.Timestamp との相互変換は
    スケジューリングの入口と出口だけで行い、内部のループには持ち込まない。
    """

    def __init__(self, lo_ord, hi_ord, holidays_all, holidays_by_team, jp_holidays,
                 auto_exclude_weekends, auto_exclude_jp_holidays):
        self.base = int(lo_ord)
        self.size = int(hi_ord) - self.base + 1
        if self.size <= 0:
            raise SchedulingError("稼働日カレンダーの範囲が不正です")

        common = np.ones(self.size, dtype=bool)
        if auto_exclude_weekends:
            # 序数1 = 0001-01-01 は月曜日。したがって (序数 - 1) % 7 が
            # 0=月〜6=日 に対応し、5・6 が土日になる。
            weekday = (self.base + np.arange(self.size) - 1) % 7
            common &= weekday < 5
        if auto_exclude_jp_holidays:
            self._mark_holidays(common, jp_holidays)
        self._mark_holidays(common, holidays_all)

        self._common = self._build(common)
        self._by_team = {}
        for team_id, days in holidays_by_team.items():
            work = common.copy()
            self._mark_holidays(work, days)
            self._by_team[team_id] = self._build(work)

    def _mark_holidays(self, work, dates):
        for d in dates:
            i = d.toordinal() - self.base
            if 0 <= i < self.size:
                work[i] = False

    def _build(self, work):
        cum = np.zeros(self.size + 1, dtype=np.int32)
        cum[1:] = np.cumsum(work, dtype=np.int32)
        nth = np.flatnonzero(work).astype(np.int32)
        if nth.size == 0:
            raise SchedulingError(
                "対象期間内に稼働日が1日もありません。休業日の設定を見直してください。"
            )
        return work, cum, nth

    def _cal(self, team_id):
        return self._by_team.get(team_id, self._common)

    def work_mask(self, team_id):
        """チームの稼働日フラグ配列（添字は 序数 - base）。"""
        return self._cal(team_id)[0]

    def _index(self, ordinal):
        i = int(ordinal) - self.base
        if not 0 <= i < self.size:
            raise SchedulingError(
                f"日付 {pd.Timestamp.fromordinal(int(ordinal)).date()} が"
                f"事前計算した稼働日カレンダーの範囲外です"
            )
        return i

    def is_working_day(self, ordinal, team_id):
        return bool(self._cal(team_id)[0][self._index(ordinal)])

    def next_working_day(self, ordinal, team_id):
        """ordinal 以降で最初の稼働日の序数。カレンダー範囲を越える場合は None。"""
        i = int(ordinal) - self.base
        if i < 0:
            i = 0
        if i >= self.size:
            return None
        _work, cum, nth = self._cal(team_id)
        k = int(cum[i])
        if k >= nth.size:
            return None
        return self.base + int(nth[k])

    def prev_working_day(self, ordinal, team_id):
        """ordinal 以前で最後の稼働日の序数。存在しなければ None。"""
        i = int(ordinal) - self.base
        if i < 0:
            return None
        if i >= self.size:
            i = self.size - 1
        _work, cum, nth = self._cal(team_id)
        # cum[i + 1] は「添字 i まで（i を含む）の稼働日数」
        k = int(cum[i + 1]) - 1
        if k < 0:
            return None
        return self.base + int(nth[k])

    @property
    def last_ordinal(self):
        """カレンダーが覆う最後の日の序数。"""
        return self.base + self.size - 1

    def business_end(self, start_ord, days, team_id):
        """start_ord（稼働日であること）から営業日 days 日分の終了日（exclusive）。
        カレンダーの範囲内に稼働日が足りない場合は None。"""
        _work, cum, nth = self._cal(team_id)
        k = int(cum[self._index(start_ord)]) + days - 1
        if k >= nth.size:
            return None
        return self.base + int(nth[k]) + 1

    def business_start(self, end_ord, days, team_id):
        """end_ord（exclusive）から遡って営業日 days 日分の開始日。足りなければ None。"""
        _work, cum, nth = self._cal(team_id)
        k = int(cum[self._index(end_ord)]) - days
        if k < 0:
            return None
        return self.base + int(nth[k])

    def shift_working_days(self, ordinal, days, team_id):
        """境界としての序数 ordinal を、稼働日換算で days 日ぶん前後へ動かす
        （依存関係のラグの加算に使う）。範囲外になる場合は None。

        - days > 0 : ordinal 以降の最初の稼働日から数えて days 稼働日ぶん後ろ。
          「先行タスクの完了後、稼働日で days 日空けてから」という意味になる。
        - days < 0 : ordinal から遡って |days| 稼働日ぶん前（リード）。

        暦日ではなく稼働日で数えるのは、所要日数・休業日の扱いが他のすべての
        計算で営業日ベースだからで、ここだけ暦日にすると「2日空ける」が
        週末をまたぐかどうかで意味を変えてしまう。
        """
        if days == 0:
            return int(ordinal)
        if days > 0:
            start = self.next_working_day(ordinal, team_id)
            if start is None:
                return None
            return self.business_end(start, days, team_id)
        return self.business_start(ordinal, -days, team_id)

    def build_capacity(self, team_id, team_capacity_schedule):
        """チームの「実効ライン数」を日ごとに並べた配列を作る（添字は 序数 - base）。

        非稼働日は _UNLIMITED_LINES を入れて必ず空きがある扱いにする。こうすると
        空き判定が「使用量 < 実効ライン数」の一括比較だけで済み、休日を読み飛ばす
        分岐をループから追い出せる（休日には使用量を加算しないため、非稼働日の
        値がいくつであっても結果に影響しない）。Teams シートに未定義のチームも
        同じ値を使い、従来どおり「ライン制限なし」として扱う。
        """
        work = self.work_mask(team_id)
        periods = team_capacity_schedule.get(team_id)
        if periods is None:
            return np.full(self.size, _UNLIMITED_LINES, dtype=np.int32)
        starts = np.array([p[0].toordinal() for p in periods], dtype=np.int64)
        values = np.array([p[1] for p in periods], dtype=np.int32)
        days = self.base + np.arange(self.size, dtype=np.int64)
        # 「適用開始日がその日以下である最後の変更点」を引く。どの変更点よりも
        # 前の日付は、先頭の値（Teams シートの Max_Lines）にフォールバックする。
        idx = np.maximum(np.searchsorted(starts, days, side="right") - 1, 0)
        return np.where(work, values[idx], _UNLIMITED_LINES).astype(np.int32)


def _dep_spec(active_tasks, succ_id, pred_id):
    """依存 pred -> succ の (種別, ラグ)。指定が無ければ既定（FS・0）。"""
    return active_tasks[succ_id]["dep_specs"].get(pred_id, DEFAULT_DEPENDENCY)


def _dep_lower_bounds(t_info, dates, cal, team_id, g_id):
    """依存関係から決まる「このタスクが開始できる最も早い日」の候補を列挙する。

    dates は {g_id: (開始序数, 終了序数exclusive)}（ASAP計算中なら理想日程、
    平準化中なら確定済みの実際の日程）。dep_specs が空の場合＝ラグも
    Start-to-Start も使っていない場合は、従来どおり依存元の終了日をそのまま
    返す高速な経路を通る。
    """
    deps = t_info["deps"]
    specs = t_info["dep_specs"]
    if not specs:
        return [dates[d][1] for d in deps if d in dates]

    bounds = []
    for d in deps:
        placed = dates.get(d)
        if placed is None:
            continue
        kind, lag = specs.get(d, DEFAULT_DEPENDENCY)
        anchor = placed[0] if kind == "SS" else placed[1]
        bound = cal.shift_working_days(anchor, lag, team_id)
        if bound is None:
            raise SchedulingError(
                f"タスク '{g_id}' の依存 '{d}'（{kind}{lag:+d}日）を反映した"
                f"着手可能日が稼働日カレンダーの範囲を超えました。ラグの値を"
                f"見直してください。"
            )
        bounds.append(bound)
    return bounds


def _pinned_start(t_info, cal, team_id):
    """開始固定日（start_pin_ord）があれば序数（稼働日に丸め済み）。無ければ None。

    指定日が休業日なら次の稼働日へ送る。ここでエラーにしないのは、休業日の
    設定はあとから変わりうる一方、固定の意図（この日から着手する）は変わらない
    ため——「置けない」ではなく「置いた結果がこうなった」を返すほうが情報が多い。
    """
    pin = t_info["start_pin_ord"]
    return None if pin is None else cal.next_working_day(pin, team_id)


def _build_work_calendar(active_tasks, successors, scheduling_order, project_start,
                         holidays_all, holidays_by_team, jp_holidays,
                         auto_exclude_weekends, auto_exclude_jp_holidays):
    """スケジューリングに必要な期間をすべて覆う _WorkCalendar を組み立てる。

    必要な範囲は次の2方向に伸びうるので、あらかじめ上限を見積もって確保する。

    - 過去方向: ALAP（締切からの逆算）は、そのタスクから終端タスクまでの
      最長の鎖の長さだけ締切より前へ遡る。
    - 未来方向: リソース不足で後ろへずれる場合でも、1チームの全タスクを
      1ラインで直列に並べた長さを超えることはない。

    いずれも営業日での見積りなので、暦日に換算する分の余裕を掛けて確保する。
    """
    # tail[g] = g から終端タスクまでの最長所要日数（g 自身と、途中の依存ラグを含む）。
    # lag_tail[g] = 同じ鎖のうち、ラグだけを積んだ長さ。ラグを使っていない
    # プロジェクトでは常に0になり、確保する範囲は従来と1日も変わらない。
    # scheduling_order は後続タスクが先に並ぶため、この順で舐めれば
    # 後続の値が必ず先に確定している。
    tail = {}
    lag_tail = {}
    max_tail = 0
    max_lag_tail = 0
    for g_id in scheduling_order:
        succ_tail = 0
        succ_lag_tail = 0
        for s in successors[g_id]:
            # 負のラグ（リード）は鎖を縮める方向なので、範囲の見積りでは0として扱う。
            lag = max(_dep_spec(active_tasks, s, g_id)[1], 0)
            succ_tail = max(succ_tail, tail[s] + lag)
            succ_lag_tail = max(succ_lag_tail, lag_tail[s] + lag)
        value = active_tasks[g_id]["days"] + succ_tail
        tail[g_id] = value
        lag_tail[g_id] = succ_lag_tail
        if value > max_tail:
            max_tail = value
        if succ_lag_tail > max_lag_tail:
            max_lag_tail = succ_lag_tail

    team_load = {}
    for t_info in active_tasks.values():
        team_load[t_info["team_id"]] = team_load.get(t_info["team_id"], 0) + t_info["days"]
    max_load = max(team_load.values(), default=0)

    ms_ends = [t["ms_end_ord"] for t in active_tasks.values()]
    # 開始固定日はプロジェクト開始日より前にも、どのマイルストーンより後ろにも
    # 置きうる（実績の確定・外部都合のピン留め）。範囲に含めておかないと
    # 「カレンダーの範囲外です」で止まる。
    pin_ords = [
        t["start_pin_ord"] for t in active_tasks.values() if t["start_pin_ord"] is not None
    ]
    start_ord = project_start.toordinal()
    anchors = [start_ord] + ms_ends + pin_ords
    lo = min(anchors) - (max_tail * _CALENDAR_MARGIN_FACTOR + _CALENDAR_MARGIN_DAYS)
    hi = max(anchors) + (
        (max_load + max_lag_tail) * _CALENDAR_MARGIN_FACTOR + _CALENDAR_MARGIN_DAYS
    )

    return _WorkCalendar(lo, hi, holidays_all, holidays_by_team, jp_holidays,
                         auto_exclude_weekends, auto_exclude_jp_holidays)


def _calc_raw_dates(active_tasks, successors, scheduling_order, cal):
    """リソース制約（チームのライン数）を無視した仮の理想日程（ALAP：締切から逆算した最遅日程）。
    休日はスキップする。戻り値は {g_id: (開始日の序数, 終了日の序数)}。"""
    raw_dates = {}
    for g_id in scheduling_order:
        t_info = active_tasks[g_id]
        team_id = t_info["team_id"]
        days = t_info["days"]

        # 固定（START_ON）タスクは最遅日程も固定日そのもの。後続タスクの都合で
        # 前後させる余地は無いので、ここで確定して次へ進む。
        pin = _pinned_start(t_info, cal, team_id)
        if pin is not None:
            pin_end = cal.business_end(pin, days, team_id)
            if pin_end is None:
                raise SchedulingError(
                    f"タスク '{g_id}' の固定開始日から所要日数ぶんの稼働日を確保できません"
                )
            raw_dates[g_id] = (pin, pin_end)
            continue

        # 上限は「自分の締切」と「各後続タスクの最遅開始から逆算した日」の最小値。
        t_end = t_info["ms_end_ord"]
        for s in successors[g_id]:
            kind, lag = _dep_spec(active_tasks, s, g_id)
            succ_start = raw_dates[s][0]
            if kind == "SS":
                # SSは「開始」同士の制約なので、まず後続の最遅開始からラグを
                # 戻して自分の最遅開始を求め、そこから所要日数ぶん進めて
                # 終了側の上限に直す。
                start_cap = cal.shift_working_days(succ_start, -lag, team_id)
                if start_cap is not None:
                    start_cap = cal.prev_working_day(start_cap, team_id)
                cap = None if start_cap is None else cal.business_end(start_cap, days, team_id)
            else:
                cap = cal.shift_working_days(succ_start, -lag, team_id)
            if cap is None:
                raise SchedulingError(
                    f"タスク '{g_id}' の最遅日程を求められません（後続 '{s}' への"
                    f"依存 {kind}{lag:+d}日 が稼働日カレンダーの範囲を超えました）"
                )
            if cap < t_end:
                t_end = cap

        t_start = cal.business_start(t_end, days, team_id)
        if t_start is None:
            raise SchedulingError(
                f"タスク '{g_id}' の最遅日程を求められません。マイルストーンの締切が"
                f"早すぎるか、所要日数が長すぎる可能性があります。"
            )
        raw_dates[g_id] = (t_start, t_end)
    return raw_dates


def _calc_asap_dates(active_tasks, leveling_order, project_start_ord, cal):
    """
    リソース制約を無視した、依存関係と開始固定日のみを考慮した最速（ASAP）の
    理想日程。プロジェクト開始日と、各依存タスクから決まる着手可能日（FSなら
    依存元の完了日、SSなら依存元の開始日。いずれも依存関係に設定されたラグを
    営業日で加算する）のうち、最も遅い日を起点にする。開始固定日（実績確定・
    外部都合のピン留め）が付いたタスクはその日そのものを使う。

    _calc_raw_dates（ALAP＝締切から逆算した最遅日程）とセットで使うことで、
    各タスクの「動かせる幅（スラック）」＝ ASAP〜ALAP の範囲が分かる。
    戻り値は {g_id: (開始日の序数, 終了日の序数)}。
    """
    asap_dates = {}
    # 依存元（predecessor）が先に確定している必要があるため、
    # leveling_order（predecessorが先）で処理する。
    for g_id in leveling_order:
        t_info = active_tasks[g_id]
        team_id = t_info["team_id"]
        t_start = _pinned_start(t_info, cal, team_id)
        if t_start is None:
            dep_ends = _dep_lower_bounds(t_info, asap_dates, cal, team_id, g_id)
            t_start = cal.next_working_day(max([project_start_ord] + dep_ends), team_id)
        t_end = None if t_start is None else cal.business_end(t_start, t_info["days"], team_id)
        if t_end is None:
            raise SchedulingError(
                f"タスク '{g_id}' の最速日程を求められません。休業日の設定、"
                f"または所要日数を見直してください。"
            )
        asap_dates[g_id] = (t_start, t_end)
    return asap_dates


def _build_team_capacity_schedule(df_teams, df_team_capacity):
    """チームごとの同時ライン数を、日付で変動しうる区分定数関数として表す
    {Team_ID: [(適用開始日, ライン数), ...]}（開始日昇順）を組み立てる。

    先頭要素は常に Teams シートの Max_Lines を「いつまでも遡って適用される
    既定値」として含む（pd.Timestamp.min始まり）ため、開発開始日を含む
    どの日付を問い合わせても必ず何らかの値が見つかる。Team_Capacity_Changes
    （任意）に登録された変更点があれば、その後ろに開始日昇順で追加する。"""
    schedule = {}
    for team_id, row in df_teams.set_index("Team_ID").iterrows():
        schedule[team_id] = [(pd.Timestamp.min, int(row["Max_Lines"]))]

    for _, row in df_team_capacity.iterrows():
        team_id = row.get("Team_ID")
        start = pd.to_datetime(row.get("Start_Date"))
        lines = row.get("Lines")
        if not pd.notna(team_id) or pd.isna(start) or not pd.notna(lines):
            logger.warning(f"Team_Capacity_Changes に不完全な行があります（スキップ）: {row.to_dict()}")
            continue
        schedule.setdefault(team_id, [(pd.Timestamp.min, int(lines))])
        schedule[team_id].append((start, int(lines)))

    for team_id in schedule:
        schedule[team_id].sort(key=lambda period: period[0])
    return schedule


def _job_ratio_jitter(job_id, amplitude=0.5):
    """
    同じワークフロー・同じマイルストーンのジョブは理想シフト量がほぼ重なるため、
    ジョブ単位で決定的な微小オフセットを distribution_ratio に加える。

    このオフセットは意図的に優先度と無関係にしてある。優先度は「競合したときに
    どちらが希望の日程を取るか」で表現するものであり（_build_leveling_order）、
    配置の基準点そのものを優先度順にずらすと、低優先度のジョブがまとめて締切側へ
    寄って負荷の山を作り、全体の遅延を増やすだけで高優先度のジョブは早くならない
    （合成データでの実測: 締切超過の合計日数が約1.9倍に悪化し、高優先度帯の
    超過はむしろ微増した）。分散はあくまで負荷の平準化のための仕組みとして、
    優先度とは独立に散らす。
    """
    h = int(hashlib.md5(job_id.encode("utf-8")).hexdigest(), 16)
    return ((h % 1000) / 1000.0 - 0.5) * amplitude


def _calc_job_shift_days(active_tasks, asap_dates, raw_dates, distribution_ratio):
    """ジョブ単位で「鎖全体をどれだけ後ろにずらすか」を一度だけ決める。

    各タスクを個別に [ASAP, ALAP] 内で独立にずらすと、鎖の前段（例: デザイン）が
    自分の広い枠の中で大きく後ろに動いた分だけ、後段タスクの実際の下限
    （＝前工程の実際の終了日）も連鎖的に押し下げられ続け、鎖の終盤で余裕が
    ゼロになってしまう（雪だるま式のシフト）。ジョブ内で最もタイトな経路
    （クリティカルパス）のスラック幅を基準に、ジョブ全体へ同一のシフト量を
    適用することでこれを防ぐ。
    """
    if not 0.0 < distribution_ratio < 1.0:
        return {}

    job_tasks = {}
    for g_id, t_info in active_tasks.items():
        job_tasks.setdefault(t_info.get("job_id", g_id), []).append(g_id)

    job_shift_days = {}
    for job_id, g_ids in job_tasks.items():
        min_slack = min(raw_dates[g][0] - asap_dates[g][0] for g in g_ids)
        min_slack = max(0, min_slack)
        effective_ratio = min(1.0, max(0.0, distribution_ratio + _job_ratio_jitter(job_id)))
        job_shift_days[job_id] = round(min_slack * effective_ratio)
    return job_shift_days


def _run_leveling(active_tasks, leveling_order, team_capacity_schedule, project_start_ord,
                   cal, asap_dates, raw_dates, distribution_ratio=1.0):
    """
    リソース制約（チームのライン数・休日）を考慮して各タスクの日程を確定する。

    predecessor（依存元）を先に確定させる順（leveling_order、優先度の高いものが
    先に来る前方向トポロジカル順）で処理する「前進（ASAP方向）型」のリソース
    平準化。先に処理したタスクが先にチームのラインを予約するため、この順序が
    そのまま「優先度の高いジョブが希望どおりの日程を取る」挙動になる。各タスクの下限は「依存タスクの実際の
    終了日」、上限は「そのタスク自身の締切から逆算した最遅日程（raw_dates、鎖全体の
    残り所要日数を織り込み済みの静的な値）」とする。

    前進型にしている理由: 後続タスクを先に確定させる方式（締切からの逆算＝ALAP）だと、
    分散のために後続タスクを早めに動かした分だけ、前工程の締切（上限）も連鎖的に
    早まり続けてしまい、鎖が長い/枝分かれが多いワークフローで雪だるま式に前倒しされて
    ResourceOverflowError になりやすい。前進型なら「前工程が早く終わるほど後工程の
    自由度が増える」向きにしか作用しないため、この問題が起きない。

    distribution_ratio（0.0〜1.0）で、各タスクを自身の [ASAP開始, 締切から逆算した
    最遅開始] の範囲内のどこに配置するかの基準点を調整する:
      - 0.0: 依存関係が満たされ次第すぐ着手（最速側）
      - 1.0: 締切ギリギリまで待つ（最遅側、従来のALAP的挙動）
      - 0.7（既定）: 締切寄り7割の位置を基準にする ＝ 締切に間に合わせつつ
        全体期間をなるべく広く使って分散させる（0.5だと前半に偏りやすい）

    基準点で空きが無い場合は、まず基準点から締切側（後ろ）へ、それでも無ければ
    基準点から着手可能日側（前）へと探索範囲を広げるため、間に合う日程が
    存在する限りは必ず見つかる。

    日付はすべて序数（int）で扱う。チームの使用ライン数も、序数を添字とする
    numpy配列（`usage`）として持ち、空き判定は「使用量 < 実効ライン数」の
    一括比較で行う（_WorkCalendar.build_capacity 参照）。

開始固定日（実績確定・外部都合のピン留め）はパス1で先にラインを予約する
    （下記）。**固定タスクを先に予約する2パス構成にしている。** 単純にトポロジカル順の
    1パスで回すと、固定タスクの番が来たときには既にその時間帯のラインが他の
    タスクで埋まっていて置けない。固定は「動かせない入力」なのだから、
    探索対象より先にラインを取らせるのが正しい順序になる。

    Returns:
        (scheduled, adjusted, overbooked_pins) のタプル。
        scheduled: {g_id: (開始日の序数, 終了日の序数)}
        adjusted: {g_id: bool}。実際の配置が分散の基準点(target_start)からずれた
            場合（＝チームのライン数不足で動かさざるを得なかった場合）に True。
            固定タスクは「言われた場所」にあるので常に False。
        overbooked_pins: 固定した結果、チームのライン数を超えて予約することに
            なったタスクの g_id の集合（診断結果として返すためのもの。
            固定を動かして辻褄を合わせることはしない）。
    """
    scheduled = {}
    adjusted = {}
    usage = {}
    caps = {}

    def team_arrays(team_id):
        arrays = usage.get(team_id)
        if arrays is None:
            arrays = usage[team_id] = np.zeros(cal.size, dtype=np.int32)
            caps[team_id] = cal.build_capacity(team_id, team_capacity_schedule)
        return arrays, caps[team_id]

    def find_forward(team_id, from_ord, limit_ord, days):
        """[from_ord, limit_ord] の範囲で days 営業日分の空きラインが取れる
        最も早い開始日を (開始, 終了exclusive) で返す。無ければ None。"""
        used, cap = team_arrays(team_id)
        start_ord = cal.next_working_day(from_ord, team_id)
        while start_ord is not None and start_ord <= limit_ord:
            end_ord = cal.business_end(start_ord, days, team_id)
            if end_ord is None:
                return None
            lo, hi = start_ord - cal.base, end_ord - cal.base
            blocked = np.flatnonzero(used[lo:hi] >= cap[lo:hi])
            if blocked.size == 0:
                return start_ord, end_ord
            # 窓の中で最後に埋まっていた日を跨ぐところまで開始日を進める。
            # その日を含む窓はどう置いても空かないため、1日ずつずらす場合と
            # 結果は変わらないまま、空きの無い区間をまとめて読み飛ばせる。
            start_ord = cal.next_working_day(cal.base + lo + int(blocked[-1]) + 1, team_id)
        return None

    def find_backward(team_id, from_ord, floor_ord, days):
        """[floor_ord, from_ord] の範囲で days 営業日分の空きラインが取れる
        最も遅い開始日を (開始, 終了exclusive) で返す。無ければ None。"""
        used, cap = team_arrays(team_id)
        start_ord = cal.prev_working_day(from_ord, team_id)
        while start_ord is not None and start_ord >= floor_ord:
            end_ord = cal.business_end(start_ord, days, team_id)
            if end_ord is None:
                return None
            lo, hi = start_ord - cal.base, end_ord - cal.base
            blocked = np.flatnonzero(used[lo:hi] >= cap[lo:hi])
            if blocked.size == 0:
                return start_ord, end_ord
            # 窓が「最初に埋まっていた日」を含まないようにするには、終了日
            # (exclusive) がその日以下でなければならない。そこまで一気に遡る。
            first_blocked = cal.base + lo + int(blocked[0])
            start_ord = cal.business_start(first_blocked, days, team_id)
        return None

    def book(team_id, start_ord, end_ord):
        used, _cap = team_arrays(team_id)
        lo, hi = start_ord - cal.base, end_ord - cal.base
        used[lo:hi] += cal.work_mask(team_id)[lo:hi]

    def is_overbooked(team_id, start_ord, end_ord):
        """[start_ord, end_ord) の稼働日に、既に空きラインが無い日があるか。"""
        used, cap = team_arrays(team_id)
        lo, hi = start_ord - cal.base, end_ord - cal.base
        return bool(np.any(used[lo:hi] >= cap[lo:hi]))

    job_shift_days = _calc_job_shift_days(active_tasks, asap_dates, raw_dates, distribution_ratio)

    # === パス1: 固定（START_ON）タスクを先に予約する ==============================
    # トポロジカル順の1パスだけで回すと、固定タスクの番が来たときには既にその
    # 時間帯のラインが他のタスクで埋まっていて置けない。固定は「動かせない入力」
    # なのだから、探索対象より先にラインを取らせるのが正しい順序になる。
    # （この2パス構成は、実績を反映した再計画——完了タスクを実績日で固定して
    # 残りを平準化する——でそのまま必要になるものと同じ形。）
    overbooked_pins = set()
    for g_id in leveling_order:
        t_info = active_tasks[g_id]
        team_id = t_info["team_id"]
        pin = _pinned_start(t_info, cal, team_id)
        if pin is None:
            continue
        end_ord = cal.business_end(pin, t_info["days"], team_id)
        if end_ord is None:
            raise SchedulingError(
                f"タスク '{g_id}' の固定開始日 "
                f"{pd.Timestamp.fromordinal(pin).date()} から所要日数ぶんの"
                f"稼働日を確保できません"
            )
        # 固定同士がぶつかる（同じチーム・同じ期間に固定が集中する）場合は、
        # 片方を動かして辻褄を合わせることはしない——固定は入力であり、
        # 動かした時点で入力を書き換えたことになるため。ライン数を超えたまま
        # 予約し、超過を診断結果として返す。
        if is_overbooked(team_id, pin, end_ord):
            overbooked_pins.add(g_id)
        book(team_id, pin, end_ord)
        scheduled[g_id] = (pin, end_ord)
        # 固定タスクは「言われた場所」に置かれているので、リソース制約による
        # ずれ（Resource_Adjusted）ではない。
        adjusted[g_id] = False

    # === パス2: 残りをリソース平準化する ==========================================
    for g_id in leveling_order:
        if g_id in scheduled:
            continue  # パス1で固定済み
        t_info = active_tasks[g_id]
        team_id = t_info["team_id"]
        days = t_info["days"]
        job_id = t_info.get("job_id", g_id)

        dep_ends = _dep_lower_bounds(t_info, scheduled, cal, team_id, g_id)
        earliest_start = cal.next_working_day(max([project_start_ord] + dep_ends), team_id)
        if earliest_start is None:
            raise SchedulingError(f"タスク '{g_id}' の着手可能日を求められません")

        # 締切から逆算した、このタスク自身の最遅開始日（鎖全体の残り所要日数を
        # 織り込み済みの静的な値）。依存元の実際の終了が想定より遅れた場合に
        # 備えて、下限（earliest_start）を下回らないようクリップする。
        latest_start = max(raw_dates[g_id][0], earliest_start)

        if distribution_ratio >= 1.0:
            target_start = latest_start
        elif distribution_ratio <= 0.0 or job_id not in job_shift_days:
            target_start = earliest_start
        else:
            # ジョブ単位で決めた一律のシフト量を、このタスクのASAP開始日に加える
            # （鎖全体が同じ量だけ後ろにずれるだけなので、内部の間隔は保たれる）。
            static_target = asap_dates[g_id][0] + job_shift_days[job_id]
            # 実際の依存元完了（earliest_start）が静的な想定より遅れていた場合は
            # そちらを優先する（安全側のクリップ）。上限は締切から逆算した最遅開始日。
            target_start = min(max(static_target, earliest_start), latest_start)
        target_start = cal.next_working_day(target_start, team_id)
        if target_start is None:
            raise SchedulingError(f"タスク '{g_id}' の配置基準日を求められません")
        # 上限が非稼働日の場合、その日を開始日とする窓は「次の稼働日を開始日と
        # する窓」と全く同じ期間を指す。稼働日に丸めておくことで、開始日が
        # 土日祝に記録されるのを防ぎつつ探索範囲は変えずに済む。
        # 上限以降に稼働日が1日も無い場合（カレンダー末尾に達した場合）は、
        # 前方向の探索を空にせずカレンダー末尾まで許す——ここで打ち切っても
        # 後段のパス4が同じ範囲を探すことになり、結果は変わらないため。
        limit_start = cal.next_working_day(latest_start, team_id)
        if limit_start is None:
            limit_start = cal.last_ordinal

        # 1) target_start を起点に、締切側（後ろ）へ向かって空きを探す
        placed = find_forward(team_id, target_start, limit_start, days)

        # 2) 見つからなければ target_start より前（着手可能日側）にも空きを探す
        if placed is None:
            placed = find_backward(team_id, target_start - 1, earliest_start, days)

        # 3) それでも見つからなければ、このタスク自身の締切（ms_end）まで
        #    探索範囲を広げる（依存元の実際の終了が想定より遅れた場合の保険）
        hard_cap_start = cal.business_start(t_info["ms_end_ord"], days, team_id)
        if placed is None and hard_cap_start is not None:
            placed = find_forward(team_id, limit_start + 1, hard_cap_start, days)

        # 4) 締切までに収まらない場合でも、可能な限り早い日程に置く（締切超過は
        #    エラーではなく結果として返す）。ここで例外にしてしまうと、1タスクが
        #    間に合わないだけでプロジェクト全体の日程が一切得られなくなり、
        #    「何が・どれだけ間に合っていないのか」を確認することすらできない。
        #    どれだけ超過したかは Deadline_Overrun_Days 列として返す。
        if placed is None:
            resume_from = earliest_start if hard_cap_start is None else hard_cap_start + 1
            placed = find_forward(team_id, max(resume_from, earliest_start),
                                   cal.last_ordinal, days)

        if placed is None:
            raise ResourceOverflowError(
                f"タスク '{g_id}'（チーム '{team_id}'）を配置できる日程が見つかりません。"
                f"チームのライン数、休業日の設定、または所要日数を見直してください。"
            )

        start_ord, end_ord = placed
        book(team_id, start_ord, end_ord)
        scheduled[g_id] = (start_ord, end_ord)
        # 実際の配置が「分散の基準点(target_start)」からずれた場合は、
        # チームのライン数不足（リソース制約）によって動かさざるを得なかったことを示す
        adjusted[g_id] = (start_ord != target_start)

    return scheduled, adjusted, overbooked_pins



_PLOTLY_GANTT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    margin: 0; padding: 24px; background: #fcfcfb; color: #0b0b0b;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .subtitle { color: #52514e; font-size: 13px; margin-bottom: 18px; }
  #filter-panel {
    border: 1px solid #e1e0d9; border-radius: 8px; padding: 12px 16px;
    margin-bottom: 22px; background: #fff;
  }
  #filter-panel .filter-title { font-weight: 600; margin-bottom: 8px; font-size: 13px; }
  #filter-panel .filter-actions { margin-bottom: 10px; }
  #filter-panel .filter-actions button {
    font-size: 12px; margin-right: 8px; padding: 4px 10px;
    border: 1px solid #c3c2b7; border-radius: 4px; background: #fff; cursor: pointer;
  }
  #filter-panel .filter-actions button:hover { background: #f0efec; }
  .team-list { display: flex; flex-wrap: wrap; gap: 8px 18px; }
  .team-item { display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; user-select: none; }
  .swatch { width: 12px; height: 12px; border-radius: 2px; display: inline-block; flex: none; }
  .workflow-section { margin-bottom: 30px; }
  .workflow-section h2 { font-size: 16px; margin: 0 0 2px; }
  .workflow-meta { color: #898781; font-size: 12px; margin-bottom: 6px; }
  .empty-note { color: #898781; font-size: 13px; padding: 16px 0; }
  #overrun-note {
    border: 1px solid #c5221f; border-left-width: 5px; border-radius: 6px;
    padding: 10px 14px; margin-bottom: 18px; background: #fdf3f2;
    color: #7c1512; font-size: 13px;
  }
  #overrun-note .swatch {
    display: inline-block; width: 22px; height: 12px; vertical-align: -1px;
    background: #e8e2b0; border: 3px solid #c5221f; margin: 0 4px;
  }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="subtitle">
  ジョブ単位で1行にまとめ（時間が重なるタスクだけ行を分ける）、作業開始が早い順に上から並べている。
  チームのチェックを外すとそのチームのタスクを非表示にし、行の高さも詰めて再描画する。
</div>
<div id="overrun-note"></div>
<div id="filter-panel">
  <div class="filter-title">チームで絞り込み</div>
</div>
<div id="charts"></div>
<script>__PLOTLY_JS__</script>
<script>
const TASKS = __TASKS_JSON__;
const TEAMS = __TEAMS_JSON__;
const WORKFLOWS = __WORKFLOWS_JSON__;
const MILESTONES = __MILESTONES_JSON__;

const ROW_HEIGHT = 26;
const BAR_BORDER_COLOR = '#0b0b0b';
const BAR_BORDER_WIDTH = 0.5;
// 締切超過タスクの強調（赤く太い枠線）。塗りつぶしの色ではなく枠線を変える
// ことで、チーム別の色分けを保ったまま重ねて表現できる。
const OVERRUN_BORDER_COLOR = '#c5221f';
const OVERRUN_BORDER_WIDTH = 3;

function teamColor(teamId) {
  const t = TEAMS.find(function (t) { return t.id === teamId; });
  return t ? t.color : "#cbc9c2";
}

function topMargin() {
  var maxLen = 0;
  MILESTONES.forEach(function (m) { maxLen = Math.max(maxLen, m.label.length); });
  return 40 + maxLen * 9;
}

function overallDateRange() {
  var dates = [];
  TASKS.forEach(function (t) { dates.push(t.start, t.end); });
  MILESTONES.forEach(function (m) { dates.push(m.date); });
  dates.sort();
  var pad = function (d, days) {
    var dt = new Date(d + "T00:00:00");
    dt.setDate(dt.getDate() + days);
    return dt.toISOString().slice(0, 10);
  };
  return [pad(dates[0], -3), pad(dates[dates.length - 1], 3)];
}
const X_RANGE = overallDateRange();

function packLanes(tasks) {
  var sorted = tasks.slice().sort(function (a, b) { return a.start < b.start ? -1 : a.start > b.start ? 1 : 0; });
  var laneEnds = [];
  var assignments = [];
  sorted.forEach(function (t) {
    var lane = -1;
    for (var i = 0; i < laneEnds.length; i++) {
      if (t.start >= laneEnds[i]) { laneEnds[i] = t.end; lane = i; break; }
    }
    if (lane === -1) { laneEnds.push(t.end); lane = laneEnds.length - 1; }
    assignments.push({ task: t, lane: lane });
  });
  return { assignments: assignments, laneCount: laneEnds.length };
}

function groupBy(arr, keyFn) {
  var m = new Map();
  arr.forEach(function (item) {
    var k = keyFn(item);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(item);
  });
  return m;
}

function hoverText(t) {
  var adj = t.adjusted ? "あり（リソース制約）" : "なし";
  var overrun = t.overrun > 0
    ? "<br><b>締切超過: " + t.overrun + "日</b>"
    : "";
  return "<b>" + t.job_name + " ＞ " + t.task_name + "</b><br>" +
    "ワークフロー: " + t.workflow_name + "<br>" +
    "チーム: " + t.team_name + "<br>" +
    "優先度: " + t.priority + "<br>" +
    "開始: " + t.start + " ／ 終了: " + t.end + "<br>" +
    "リソース調整: " + adj + overrun;
}

function getSelectedTeamIds() {
  var ids = new Set();
  document.querySelectorAll('#filter-panel input[type=checkbox]').forEach(function (cb) {
    if (cb.checked) ids.add(cb.dataset.teamId);
  });
  return ids;
}

function buildFilterPanel() {
  var panel = document.getElementById('filter-panel');

  var actions = document.createElement('div');
  actions.className = 'filter-actions';
  var allBtn = document.createElement('button');
  allBtn.textContent = 'すべて表示';
  allBtn.onclick = function () { setAll(true); };
  var noneBtn = document.createElement('button');
  noneBtn.textContent = 'すべて非表示';
  noneBtn.onclick = function () { setAll(false); };
  actions.appendChild(allBtn);
  actions.appendChild(noneBtn);
  panel.appendChild(actions);

  var list = document.createElement('div');
  list.className = 'team-list';
  TEAMS.forEach(function (team) {
    var label = document.createElement('label');
    label.className = 'team-item';
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = true;
    cb.dataset.teamId = team.id;
    cb.addEventListener('change', renderAll);
    var swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.background = team.color;
    label.appendChild(cb);
    label.appendChild(swatch);
    label.appendChild(document.createTextNode(team.name));
    list.appendChild(label);
  });
  panel.appendChild(list);
}

function setAll(checked) {
  document.querySelectorAll('#filter-panel input[type=checkbox]').forEach(function (cb) { cb.checked = checked; });
  renderAll();
}

function buildWorkflowSections() {
  var root = document.getElementById('charts');
  WORKFLOWS.forEach(function (wf) {
    var section = document.createElement('div');
    section.className = 'workflow-section';
    var h2 = document.createElement('h2');
    h2.textContent = wf.name;
    section.appendChild(h2);
    var meta = document.createElement('div');
    meta.className = 'workflow-meta';
    meta.id = 'meta-' + wf.id;
    section.appendChild(meta);
    var div = document.createElement('div');
    div.id = 'chart-' + wf.id;
    section.appendChild(div);
    root.appendChild(section);
  });
}

function renderWorkflow(wf) {
  var container = document.getElementById('chart-' + wf.id);
  var meta = document.getElementById('meta-' + wf.id);
  var selected = getSelectedTeamIds();
  var tasks = TASKS.filter(function (t) { return t.workflow_id === wf.id && selected.has(t.team_id); });

  if (tasks.length === 0) {
    Plotly.purge(container);
    container.innerHTML = '<div class="empty-note">表示するタスクがありません（フィルタ条件に一致するタスクなし）</div>';
    meta.textContent = '';
    return;
  }

  var byJob = groupBy(tasks, function (t) { return t.job_id; });
  var jobEntries = [];
  byJob.forEach(function (jobTasks, jobId) {
    var minStart = jobTasks.reduce(function (m, t) { return t.start < m ? t.start : m; }, jobTasks[0].start);
    var packed = packLanes(jobTasks);
    jobEntries.push({
      jobId: jobId, jobName: jobTasks[0].job_name, minStart: minStart,
      assignments: packed.assignments, laneCount: packed.laneCount,
    });
  });
  jobEntries.sort(function (a, b) {
    if (a.minStart !== b.minStart) return a.minStart < b.minStart ? -1 : 1;
    return a.jobName.localeCompare(b.jobName);
  });

  meta.textContent = jobEntries.length + ' ジョブ ／ ' + tasks.length + ' タスク';

  var cursor = 0;
  var tickvals = [], ticktext = [], separators = [];
  var bases = [], xs = [], ys = [], colors = [], texts = [], hovertexts = [];
  // 締切を超過したタスクだけ、枠線を赤く太くして目立たせる（塗りはチーム色の
  // ままにして、どのチームの作業かは引き続き分かるようにする）。
  var lineColors = [], lineWidths = [];

  jobEntries.forEach(function (job, jobIdx) {
    if (jobIdx > 0) separators.push(cursor - 0.5);
    var blockStart = cursor;
    job.assignments.forEach(function (a) {
      var t = a.task;
      var rowIndex = blockStart + a.lane;
      var startMs = new Date(t.start + "T00:00:00").getTime();
      var endMs = new Date(t.end + "T00:00:00").getTime();
      bases.push(t.start);
      xs.push(endMs - startMs);
      ys.push(rowIndex);
      colors.push(teamColor(t.team_id));
      lineColors.push(t.overrun > 0 ? OVERRUN_BORDER_COLOR : BAR_BORDER_COLOR);
      lineWidths.push(t.overrun > 0 ? OVERRUN_BORDER_WIDTH : BAR_BORDER_WIDTH);
      texts.push(t.task_name);
      hovertexts.push(hoverText(t));
    });
    tickvals.push(blockStart + (job.laneCount - 1) / 2);
    ticktext.push(job.jobName);
    cursor += job.laneCount;
  });

  var totalRows = cursor;
  var height = Math.max(140, totalRows * ROW_HEIGHT + topMargin() + 50);

  var trace = {
    type: 'bar', orientation: 'h',
    base: bases, x: xs, y: ys,
    marker: { color: colors, line: { color: lineColors, width: lineWidths } },
    text: texts, textposition: 'inside', insidetextanchor: 'start',
    textfont: { size: 11, color: '#0b0b0b' },
    constraintext: 'both',
    hovertext: hovertexts, hoverinfo: 'text',
    width: 0.7,
  };

  var shapes = MILESTONES.map(function (m) {
    return {
      type: 'line', xref: 'x', x0: m.date, x1: m.date, yref: 'paper', y0: 0, y1: 1,
      line: { color: '#52514e', width: 1.5, dash: 'dash' },
    };
  });
  separators.forEach(function (y) {
    shapes.push({
      type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: y, y1: y,
      line: { color: '#e1e0d9', width: 1 },
    });
  });

  var annotations = MILESTONES.map(function (m) {
    return {
      x: m.date, y: 1, xref: 'x', yref: 'paper', text: '◆' + m.label,
      showarrow: false, textangle: -90, xanchor: 'left', yanchor: 'bottom',
      font: { size: 11, color: '#52514e' },
    };
  });

  var layout = {
    height: height,
    margin: { l: 10, r: 10, t: topMargin(), b: 30 },
    xaxis: { type: 'date', range: X_RANGE, gridcolor: '#e1e0d9' },
    yaxis: {
      range: [totalRows - 0.5, -0.5],
      tickmode: 'array', tickvals: tickvals, ticktext: ticktext,
      automargin: true, showgrid: false, zeroline: false,
    },
    shapes: shapes, annotations: annotations,
    showlegend: false,
    plot_bgcolor: '#fcfcfb', paper_bgcolor: '#fcfcfb',
    font: { family: 'system-ui, -apple-system, "Segoe UI", sans-serif', color: '#0b0b0b' },
  };

  Plotly.react(container, [trace], layout, { displaylogo: false, responsive: true });
}

function buildOverrunNote() {
  // 締切超過は例外ではなく結果として返ってくるため、件数を明示しないと
  // 赤枠に気付かないまま見過ごされてしまう。件数が0なら何も表示しない。
  var note = document.getElementById('overrun-note');
  var overruns = TASKS.filter(function (t) { return t.overrun > 0; });
  if (overruns.length === 0) {
    note.style.display = 'none';
    return;
  }
  var worst = overruns.reduce(function (m, t) { return Math.max(m, t.overrun); }, 0);
  note.innerHTML =
    '<b>マイルストーンの締切に間に合わないタスクが ' + overruns.length + ' 件あります'
    + '（最大 ' + worst + ' 日超過）。</b><br>'
    + '該当タスクは<span class="swatch"></span>のように<b>赤く太い枠線</b>で表示しています'
    + '（バーにマウスを乗せると超過日数を確認できます）。'
    + 'チームのライン数・依存関係・締切のいずれかを見直してください。';
}

function renderAll() {
  WORKFLOWS.forEach(renderWorkflow);
}

buildFilterPanel();
buildWorkflowSections();
buildOverrunNote();
renderAll();
</script>
</body>
</html>
"""


def export_plotly_gantt(result_df, output_path, project_name="プロジェクトスケジュール",
                         team_name_map=None, workflow_name_map=None, team_order=None,
                         milestone_markers=None):
    """result_df（run_resource_constrained_schedulerの戻り値）から、サーバー不要で
    ブラウザで直接開けるインタラクティブなガントチャート（単一HTMLファイル、
    Plotly製）を書き出す。

    - **ワークフローごとに別々のガントチャートに分割**する。
    - 各チャート内は「ジョブ単位で1行」にまとめる。ジョブ名は1回だけ表示し、
      時間的に重なるタスクがある場合だけレーン（行）を追加する（重ならない
      タスクは同じ行に詰める）。ジョブの
      境界には横線を入れて区切る。バー内にはタスク名を表示する。
    - 各チャート内のジョブは、そのジョブの最初のタスクの開始日が早い順に
      上から並べる。
    - 画面上部の「チームで絞り込み」パネルでチームのチェックを外すと、その
      チームのタスクを全チャートから除外し、**レーンを詰め直して行の高さも
      縮める**（Plotlyの凡例クリックによる表示/非表示とは異なり、非表示分の
      余白が残らない）。チェックボックスの色見本がチーム別配色を兼ねる。
    - バーにマウスを乗せるとジョブ名・タスク名・ワークフロー・優先度・
      開始/終了日・リソース調整有無・締切超過日数を表示する。
    - **マイルストーンの締切に間に合わないタスク**（Deadline_Overrun_Days > 0）は、
      塗りつぶしはチーム色のまま、**枠線を赤く太く**して強調する。件数と最大
      超過日数はチャート上部に赤い枠で併記する。
    - プロジェクト開始日と各マイルストーン（milestone_markers）を縦の破線として
      全チャート共通で重ねる（表示期間もチャート間で揃える）。
    - チーム色は固定12色のカテゴリカルパレット（team_order の登場順に割り当て）。
      13チーム目以降は無彩色にフォールドする（色は識別の補助であり、チーム名は
      常にチェックボックス・ホバーのテキストでも確認できる）。

    実データはHTML内にJSON埋め込みし、レーンパッキングや再描画はブラウザ側の
    JavaScriptで行う（フィルタ変更のたびにPython側で再生成する必要がない）。

    team_name_map / workflow_name_map: {ID: 表示名} の辞書。省略時はIDをそのまま表示する。
    team_order: 色を割り当てる順序を決めるチームIDのリスト（例: Teamsシートの行順）。
        省略時は result_df 内の初出順を使う。
    milestone_markers: [(id, label, date), ...] のリスト。

    Returns:
        書き出したファイルパス。
    """
    import json as _json
    from html import escape as _esc

    try:
        import plotly.offline as pyo
    except ImportError as e:
        raise ImportError(
            "export_plotly_gantt には plotly が必要です。`pip install plotly` を"
            "実行するか、requirements.txt から依存関係をインストールしてください。"
        ) from e

    team_name_map = team_name_map or {}
    workflow_name_map = workflow_name_map or {}
    milestone_markers = milestone_markers or []

    if result_df.empty:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"<html><body><p>{_esc(project_name)}: タスクなし</p></body></html>")
        logger.info(f"Plotlyガントチャートを書き出しました（タスクなし）: {output_path}")
        return output_path

    if team_order is None:
        team_order = list(dict.fromkeys(result_df["Team_ID"].tolist()))
    # team_order に無いチームIDが result_df 側にだけ存在する場合に備えて末尾に補う
    team_order = list(dict.fromkeys(list(team_order) + result_df["Team_ID"].tolist()))
    team_color_map = _build_team_color_map(team_order, team_name_map)

    teams_json = [
        {"id": str(tid), "name": team_name_map.get(str(tid), str(tid)),
         "color": team_color_map.get(team_name_map.get(str(tid), str(tid)), _TEAM_COLOR_OVERFLOW)}
        for tid in team_order
    ]

    workflow_ids_in_order = list(dict.fromkeys(result_df["Workflow_ID"].tolist()))
    workflows_json = [
        {"id": str(wid), "name": workflow_name_map.get(str(wid), str(wid))}
        for wid in workflow_ids_in_order
    ]

    tasks_json = [
        {
            "job_id": str(r["Job_ID"]),
            "job_name": str(r["Job_Name"]),
            "workflow_id": str(r["Workflow_ID"]),
            "workflow_name": workflow_name_map.get(str(r["Workflow_ID"]), str(r["Workflow_ID"])),
            "task_name": str(r["Task_Name"]),
            "team_id": str(r["Team_ID"]),
            "team_name": team_name_map.get(str(r["Team_ID"]), str(r["Team_ID"])),
            "priority": r["Priority"],
            "start": r["Start_Date"].strftime("%Y-%m-%d"),
            "end": r["End_Date"].strftime("%Y-%m-%d"),
            "adjusted": bool(r["Resource_Adjusted"]),
            "overrun": int(r.get("Deadline_Overrun_Days", 0) or 0),
        }
        for _, r in result_df.iterrows()
    ]

    milestones_json = [
        {"id": str(mid), "label": str(mlabel), "date": mdate.strftime("%Y-%m-%d")}
        for mid, mlabel, mdate in milestone_markers
    ]

    title = f"{project_name} スケジュール"
    html_out = (
        _PLOTLY_GANTT_HTML_TEMPLATE
        .replace("__TITLE__", _esc(title))
        .replace("__PLOTLY_JS__", pyo.get_plotlyjs())
        .replace("__TASKS_JSON__", _json.dumps(tasks_json, ensure_ascii=False))
        .replace("__TEAMS_JSON__", _json.dumps(teams_json, ensure_ascii=False))
        .replace("__WORKFLOWS_JSON__", _json.dumps(workflows_json, ensure_ascii=False))
        .replace("__MILESTONES_JSON__", _json.dumps(milestones_json, ensure_ascii=False))
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_out)
    logger.info(f"Plotlyガントチャートを書き出しました: {output_path}")
    return output_path


def run_resource_constrained_scheduler(excel_file, verbose=True,
                                        auto_exclude_weekends=True,
                                        auto_exclude_jp_holidays=True,
                                        plotly_output_path=None,
                                        project_name=None,
                                        distribution_ratio=0.7):
    """
    リソース制約付きスケジューリングを実行し、結果を DataFrame で返す。

    スケジューリングの考え方:
    1. 依存関係のみを考慮した最速日程（ASAP）と、締切（マイルストーン）から
       逆算した最遅日程（ALAP）の両方を計算し、各タスクが「動かせる幅
       （スラック）」を把握する。
    2. distribution_ratio に応じて、ASAP〜ALAPの間に探索の基準点を置き、
       そこを起点にチームの空きラインを探す（見つからなければ締切側へも
       探索範囲を広げる）。これにより、マイルストーンには間に合わせつつ、
       特定の時期にタスクが偏らないよう全体期間に分散させる。

    Args:
        auto_exclude_weekends: True の場合、土日を全チーム共通の休業日として自動的に除外する。
        auto_exclude_jp_holidays: True の場合、日本の祝日（振替休日含む）を自動的に除外する。
            土日・祝日以外の休業日（年末年始休業、チーム独自の研修日等）は、
            従来通り Holidays シートに明記する。
        plotly_output_path: 指定すると、サーバー不要でブラウザから直接開ける
            インタラクティブなガントチャート（単一HTMLファイル、Plotly製）を
            書き出す（例: "schedule_gantt.html"）。チーム別に色分けし、凡例
            クリックでチーム単位の表示/非表示切り替え（簡易フィルタリング）が
            できる。省略時はファイル出力を行わない。
        project_name: HTML冒頭の見出しに使うプロジェクト名。
            省略時は Project シートの Project_Name を使う。
        distribution_ratio: 0.0〜1.0。各タスクをASAP（最速）〜ALAP（締切ギリギリ）の
            どのあたりに配置するかの基準点。
              - 0.0: 依存関係が満たされ次第すぐ着手（従来のASAP前倒しに近い、前に詰まりやすい）
              - 1.0: 締切から逆算した最遅日程を基準にする（締切ギリギリに偏りやすい）
              - 0.7（既定）: 締切寄り7割の位置を基準にし、締切に間に合わせながら
                全体期間をなるべく広く使って分散させる
            いずれの値でも、締切に間に合う日程が存在する限りは間に合わせる
            （基準点で空きが無い場合は締切側まで自動的に探索範囲を広げるため）。
            リソースが足りず間に合わない場合は、可能な限り早い日程に配置した
            うえで Deadline_Overrun_Days 列に超過日数を入れて返す。

    生成されるガントチャートには、プロジェクト開始日と各マイルストーンが
    縦の破線として自動的に含まれる。すべてのチャートに同じマイルストーン集合を
    含めるため、表示期間（軸の範囲）もチャート間で揃う。

    戻り値の DataFrame は、各タスクの Start_Date / End_Date に加えて
    Deadline_Overrun_Days 列（マイルストーンの締切をどれだけ超過したか。
    0なら間に合っている）と Milestone_ID 列を持つ。

    Raises:
        MissingSheetOrColumnError: シート/列が不足している場合
        MissingMilestoneError: マイルストーン参照が不正な場合
        CircularDependencyError: 循環依存がある場合
        ResourceOverflowError: 締切を無視しても配置できる日程が見つからない場合
        SchedulingError: その他のスケジューリング不整合（稼働日が1日も無い等）
    """
    frames = _load_data(excel_file)
    return _run_scheduler_on_frames(
        *frames, verbose=verbose,
        auto_exclude_weekends=auto_exclude_weekends,
        auto_exclude_jp_holidays=auto_exclude_jp_holidays,
        plotly_output_path=plotly_output_path,
        project_name=project_name,
        distribution_ratio=distribution_ratio,
    )


def run_resource_constrained_scheduler_from_frames(df_project, df_teams, df_ms, df_wf, df_jobs,
                                                     df_jtasks=None, df_holidays=None,
                                                     df_extdeps=None, df_wf_names=None,
                                                     df_team_capacity=None,
                                                     verbose=True,
                                                     auto_exclude_weekends=True,
                                                     auto_exclude_jp_holidays=True,
                                                     plotly_output_path=None,
                                                     project_name=None,
                                                     distribution_ratio=0.7):
    """
    run_resource_constrained_scheduler() のDataFrame直接指定版。Excel読み込みを
    一切経由せず、既にDataFrameとして構築済みのデータ（例: GUIのSQLiteデータベース
    から組み立てたもの）から直接スケジューリングする。

    各引数は run_resource_constrained_scheduler() と同じ意味・既定値を持つ
    （df_project 以降 df_team_capacity までが Excel の各シートに相当するDataFrame、
    df_jtasks/df_holidays/df_extdeps/df_wf_names/df_team_capacity は None なら
    「データなし」を表す。df_team_capacity は Team_ID/Start_Date/Lines 列を持ち、
    チームの同時ライン数（Teams.Max_Lines）を途中の日付から変動させる場合の
    変更点を表す）。それ以外のキーワード引数・戻り値・送出しうる例外は
    run_resource_constrained_scheduler() のdocstringを参照。
    """
    frames = _load_data_from_frames(df_project, df_teams, df_ms, df_wf, df_jobs,
                                     df_jtasks, df_holidays, df_extdeps, df_wf_names,
                                     df_team_capacity)
    return _run_scheduler_on_frames(
        *frames, verbose=verbose,
        auto_exclude_weekends=auto_exclude_weekends,
        auto_exclude_jp_holidays=auto_exclude_jp_holidays,
        plotly_output_path=plotly_output_path,
        project_name=project_name,
        distribution_ratio=distribution_ratio,
    )


def _check_constraint_violations(active_tasks, scheduled, cal, overbooked_pins):
    """開始固定日が満たせなかったタスクを洗い出して {g_id: (超過日数, 説明)} で返す。

    **固定日そのものは必ず守られる**（パス1で無条件に予約するため）。破れうる
    のは「固定した結果、依存関係やライン数と両立しない」ほうであり、それを
    ここでエラーではなく結果として返す（締切超過と同じ方針）。ここで例外に
    すると、1つの固定が矛盾しているだけでプロジェクト全体の日程が一切得られ
    なくなり、「何が・どれだけ無理なのか」を確認することすらできなくなる。
    """
    violations = {}
    for g_id, (start_ord, end_ord) in scheduled.items():
        t_info = active_tasks[g_id]
        if t_info["start_pin_ord"] is None:
            continue
        team_id = t_info["team_id"]
        notes = []
        worst = 0

        def record(days, text):
            nonlocal worst
            worst = max(worst, days)
            notes.append(text)

        pin_label = _fmt_ord(t_info["start_pin_ord"])
        dep_floor = _dep_lower_bounds(t_info, scheduled, cal, team_id, g_id)
        if dep_floor:
            floor = cal.next_working_day(max(dep_floor), team_id)
            if floor is not None and start_ord < floor:
                record(floor - start_ord,
                       f"固定開始日（{pin_label}）が依存タスクの着手可能日（{_fmt_ord(floor)}）"
                       f"より{floor - start_ord}日早いです")
        if g_id in overbooked_pins:
            # ライン数の超過に「何日超過」に相当する量は無いので日数は0のまま。
            # 違反しているかどうかは説明文が空かどうかで判定する。
            record(0, f"固定開始日（{pin_label}）がチーム「{team_id}」のライン数を"
                      f"超えて予約されています")

        if notes:
            violations[g_id] = (worst, "／".join(notes))
    return violations


def _fmt_ord(ordinal):
    return pd.Timestamp.fromordinal(int(ordinal)).strftime("%Y-%m-%d")


def _warn_constraint_violations(result_df, max_listed=10):
    """満たせなかった日付制約を警告としてまとめて出す（締切超過と同じ扱い）。"""
    if result_df.empty or "Constraint_Violation" not in result_df.columns:
        return
    broken = result_df[result_df["Constraint_Violation"] != ""]
    if broken.empty:
        return
    worst = broken.sort_values("Constraint_Violation_Days", ascending=False)
    logger.warning(
        f"満たせなかった日付制約が {len(broken)} 件あります。制約の日付、依存関係、"
        f"チームのライン数のいずれかを見直してください。"
    )
    for _, r in worst.head(max_listed).iterrows():
        logger.warning(
            f"  {r['Job_Name']} > {r['Task_Name']}: {r['Constraint_Violation']}"
        )
    if len(worst) > max_listed:
        logger.warning(f"  ...ほか {len(worst) - max_listed} 件")


def _warn_deadline_overruns(result_df, max_listed=10):
    """マイルストーンの締切に間に合わなかったタスクを警告としてまとめて出す。

    締切超過は例外にせず結果として返す方針（_run_leveling のパス4を参照）なので、
    黙って通り過ぎないよう、ここで件数と代表例をログに残す。何件・どれだけ
    超過しているかは result_df の Deadline_Overrun_Days 列から常に確認できる。
    """
    if result_df.empty or "Deadline_Overrun_Days" not in result_df.columns:
        return
    overruns = result_df[result_df["Deadline_Overrun_Days"] > 0]
    if overruns.empty:
        return

    worst = overruns.sort_values("Deadline_Overrun_Days", ascending=False)
    logger.warning(
        f"マイルストーンの締切に間に合わないタスクが {len(overruns)} 件あります"
        f"（最大 {int(worst.iloc[0]['Deadline_Overrun_Days'])} 日超過）。"
        f"チームのライン数、依存関係、締切のいずれかを見直してください。"
    )
    for _, r in worst.head(max_listed).iterrows():
        logger.warning(
            f"  {int(r['Deadline_Overrun_Days'])}日超過: "
            f"[{r['Team_ID']}] {r['Job_Name']} > {r['Task_Name']} "
            f"（締切 {r['Milestone_ID']}、終了 {r['End_Date'].strftime('%Y-%m-%d')}）"
        )
    if len(worst) > max_listed:
        logger.warning(f"  ...ほか {len(worst) - max_listed} 件")


def _run_scheduler_on_frames(df_project, df_teams, df_ms, df_wf, df_jobs, df_jtasks,
                              df_holidays, df_extdeps, df_wf_names, df_team_capacity,
                              verbose=True,
                              auto_exclude_weekends=True, auto_exclude_jp_holidays=True,
                              plotly_output_path=None, project_name=None,
                              distribution_ratio=0.7):
    """run_resource_constrained_scheduler() / run_resource_constrained_scheduler_from_frames()
    が共有するスケジューリング本体（_load_data* による検証・整形済みのDataFrameを受け取る）。"""
    project_start = _load_project_start(df_project)
    if project_name is None:
        project_name = str(df_project.iloc[0].get("Project_Name", "プロジェクトスケジュール"))
    holidays_all, holidays_by_team = _load_holidays(df_holidays)

    # ガントチャート見出し用の表示名マップ（未指定なら ID をそのまま使う）
    workflow_name_map = {
        str(r["Workflow_ID"]): str(r["Workflow_Name"])
        for _, r in df_wf_names.iterrows()
        if pd.notna(r.get("Workflow_ID")) and pd.notna(r.get("Workflow_Name"))
    }
    team_name_map = {}
    if "Team_Name" in df_teams.columns:
        team_name_map = {
            str(r["Team_ID"]): str(r["Team_Name"])
            for _, r in df_teams.iterrows()
            if pd.notna(r.get("Team_ID")) and pd.notna(r.get("Team_Name"))
        }

    # ガントチャートに表示するマイルストーン群（プロジェクト開始日 + 各マイルストーン）。
    # 全チャートに同じ集合を差し込むことで、表示期間（軸の範囲）を
    # チャート間で揃える（比較しやすくするため）。
    milestone_markers = [("PROJECT_START", "プロジェクト開始", project_start)]
    for ms_id, ms_row in df_ms.iterrows():
        ms_name = ms_row.get("Milestone_Name")
        if not pd.notna(ms_name) or str(ms_name).strip() == "":
            ms_name = ms_id
        ms_end = pd.to_datetime(ms_row.get("End_Date"))
        if pd.notna(ms_end):
            milestone_markers.append((str(ms_id), str(ms_name), ms_end))
    milestone_markers.sort(key=lambda m: m[2])

    teams_dict, active_tasks, active_ids = _parse_tasks(
        df_teams, df_ms, df_wf, df_jobs, df_jtasks, df_extdeps, project_start)
    team_capacity_schedule = _build_team_capacity_schedule(df_teams, df_team_capacity)

    if not active_ids:
        logger.warning("アクティブなタスクがありません")
        result_df = pd.DataFrame(columns=[
            "Job_ID", "Task_ID", "Job_Name", "Task_Name", "Team_ID", "Priority",
            "Workflow_ID", "Milestone_ID", "Start_Date", "End_Date",
            "Resource_Adjusted", "Deadline_Overrun_Days",
            "Constraint_Violation_Days", "Constraint_Violation",
        ])
        if plotly_output_path:
            export_plotly_gantt(result_df, plotly_output_path, project_name,
                                 team_name_map=team_name_map,
                                 workflow_name_map=workflow_name_map,
                                 team_order=list(df_teams["Team_ID"]),
                                 milestone_markers=milestone_markers)
        return result_df

    jp_holidays = set()
    if auto_exclude_jp_holidays:
        year_start = project_start.year
        year_end = max([t["ms_end"].year for t in active_tasks.values()] + [year_start])
        jp_holidays = generate_jp_holidays(year_start, year_end)

    successors, scheduling_order = _build_scheduling_order(active_tasks, active_ids)

    # 稼働日の判定・営業日の加減算はスケジューリング中に最も多く呼ばれるため、
    # チーム別の稼働日を序数添字の配列として先に作り、以降は日付を序数(int)の
    # まま扱う（pd.Timestamp への復元は結果を組み立てる時だけ）。
    cal = _build_work_calendar(
        active_tasks, successors, scheduling_order, project_start,
        holidays_all, holidays_by_team, jp_holidays,
        auto_exclude_weekends, auto_exclude_jp_holidays,
    )
    project_start_ord = project_start.toordinal()

    leveling_order = _build_leveling_order(active_tasks, active_ids, successors)

    raw_dates = _calc_raw_dates(active_tasks, successors, scheduling_order, cal)
    asap_dates = _calc_asap_dates(active_tasks, leveling_order, project_start_ord, cal)
    scheduled, adjusted_flags, overbooked_pins = _run_leveling(
        active_tasks, leveling_order, team_capacity_schedule, project_start_ord, cal,
        asap_dates=asap_dates, raw_dates=raw_dates, distribution_ratio=distribution_ratio,
    )
    constraint_violations = _check_constraint_violations(
        active_tasks, scheduled, cal, overbooked_pins)

    rows = []
    for g_id, dates in scheduled.items():
        t = active_tasks[g_id]
        # 分散配置の基準点(target_start)からチームのライン数不足によりずれた
        # 場合に True になる（distribution_ratio による意図的な分散配置そのものは
        # 「調整あり」に含めない）
        resource_adjusted = adjusted_flags.get(g_id, False)
        violation_days, violation_note = constraint_violations.get(g_id, (0, ""))
        rows.append({
            "Job_ID": t["job_id"],
            "Task_ID": t["task_id"],
            "Job_Name": t["job_name"],
            "Task_Name": t["task_name"],
            "Team_ID": t["team_id"],
            "Priority": t["priority"],
            "Workflow_ID": t["workflow_id"],
            "Milestone_ID": t["milestone"],
            "Start_Date": pd.Timestamp.fromordinal(dates[0]),
            "End_Date": pd.Timestamp.fromordinal(dates[1]),
            "Resource_Adjusted": resource_adjusted,
            # マイルストーンの締切をどれだけ超過したか（暦日、0なら間に合っている）。
            # 終了日は exclusive なので、締切当日ちょうどに終わる場合は超過0になる。
            "Deadline_Overrun_Days": max(0, dates[1] - t["ms_end_ord"]),
            # 満たせなかった開始固定日（start_pin_ord）。固定は動かして辻褄を
            # 合わせず、矛盾は結果として返す（_check_constraint_violations 参照）。
            # 空文字なら固定は無い、または固定は問題なく守られている。
            "Constraint_Violation_Days": violation_days,
            "Constraint_Violation": violation_note,
        })

    result_df = pd.DataFrame(rows).sort_values(["Start_Date", "Job_ID", "Task_ID"]).reset_index(drop=True)

    _warn_deadline_overruns(result_df)
    _warn_constraint_violations(result_df)

    if verbose:
        print("=== リソース制約考慮スケジューリング結果 ===")
        for _, r in result_df.iterrows():
            mark = " ⚠️ [リソース制約により前倒し]" if r["Resource_Adjusted"] else ""
            if r["Constraint_Violation"]:
                mark += f" ⚠️ [{r['Constraint_Violation']}]"
            print(
                f"[{r['Team_ID']}] {r['Job_Name']} > {r['Task_Name']} "
                f"(優先度{r['Priority']}): "
                f"{r['Start_Date'].strftime('%Y-%m-%d')} ～ {r['End_Date'].strftime('%Y-%m-%d')}{mark}"
            )

    if plotly_output_path:
        export_plotly_gantt(result_df, plotly_output_path, project_name,
                             team_name_map=team_name_map,
                             workflow_name_map=workflow_name_map,
                             team_order=list(df_teams["Team_ID"]),
                             milestone_markers=milestone_markers)

    return result_df


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="リソース制約付きプロジェクトスケジューラー")
    parser.add_argument(
        "excel_file", nargs="?",
        default="data/Project_Schedule_Sample_GameDev_v22.xlsx",
        help="入力Excelファイルのパス（既定: サンプルデータ）",
    )
    parser.add_argument(
        "-o", "--output", "--html-output", dest="output",
        default="output/schedule_gantt.html",
        help="サーバー不要で開けるPlotly製インタラクティブガントチャート"
             "（チーム別色分け）の出力先HTMLパス。空文字を指定すると出力しない",
    )
    parser.add_argument(
        "--distribution-ratio", type=float, default=0.7,
        help="ASAP(0.0)〜ALAP(1.0)間の配置基準点（既定0.7）",
    )
    args = parser.parse_args()

    try:
        run_resource_constrained_scheduler(
            args.excel_file,
            plotly_output_path=args.output or None,
            distribution_ratio=args.distribution_ratio,
        )
    except SchedulingError as e:
        logger.error(f"スケジューリングに失敗しました: {e}")
        raise
