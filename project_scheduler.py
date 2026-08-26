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
7. Mermaidガントチャートの `title` 行を廃止（チャート上部にタイトルを表示しない）。
8. リソース制約による前倒しタスクの赤色強調（crit）表示は既定でOFFに変更。
   必要な場合のみ highlight_resource_adjusted=True で有効化する。
9. ワークフロー単位のガントチャートに加え、チーム単位のガントチャートも
   同じMarkdownファイル内に生成する（担当チームの稼働状況を横断的に見せる）。
10. ワークフローIDだけでは何の制作物か分かりづらいため、Excel側に任意の
    "Workflow_Names" シート（Workflow_ID / Workflow_Name）を追加できるように
    対応。指定があればガントチャートの見出しにその名前を使う（未指定時はID）。
    同様に Teams シートの Team_Name 列があればチーム別チャートの見出しに使う。

v6での変更点:
11. すべてのガントチャート（ワークフロー別・チーム別いずれも）の冒頭に
    「マイルストーン」セクションを追加し、プロジェクト開始日と各マイルストーン
    （Milestonesシート）を milestone（◆マーク）として表示するようにした。
12. 上記のマイルストーン群はプロジェクト全体で共通（同じID・同じ日付）なので、
    すべてのチャートに同じマイルストーンを含めることで、Mermaid側が自動計算する
    表示期間（軸の範囲）もチャート間で揃うようにした（横並び比較がしやすい）。

v7での変更点:
13. 従来の「ALAP（締切から逆算した最遅日程）でリソース平準化した後、依存元が
    終わり次第すぐ着手するASAP方向へ前倒しする」という2段階方式を廃止した。
    このASAP前倒しパスが、締切までまだ余裕があるタスクまで軒並みプロジェクト
    開始直後に詰め込んでしまい、非現実的な偏りを生む原因になっていたため。
14. 代わりに、各タスクの「依存関係のみを考慮した最速日程（ASAP）」と
    「締切から逆算した最遅日程（ALAP）」の両方を求め、その間（スラック）の
    どこに配置するかを distribution_ratio（既定0.5）で制御する方式にした。
    基準点にチームの空きが無い場合は締切側・着手可能日側の順に探索範囲を
    広げる。結果として、締切に間に合わせつつプロジェクト全体期間になるべく
    分散した日程になる（v8以降、それでも収まらない場合は締切を超過した日程を
    返し、超過日数を Deadline_Overrun_Days 列で報告する。下記16〜18を参照）。
15. ログ出力のレベル名（INFO/WARNING/ERROR等）を日本語（情報/警告/エラー等）に
    変更した。

v8での変更点（大規模プロジェクトへの対応）:
16. リソース平準化の処理順を、優先度付きの *前方向* トポロジカル順
    （_build_leveling_order）に修正した。v7で平準化を前進型に変えた際、
    逆方向Kahn順（優先度の高いものが先頭に来る）をそのまま reversed() して
    使っていたため、優先度の高いジョブほど *最後* にラインを確保することに
    なり、Priorityの効果が反転していた。
17. マイルストーンの締切に間に合わないタスクを ResourceOverflowError に
    せず、可能な限り早い日程へ配置したうえで Deadline_Overrun_Days 列
    （超過日数）として結果に返すようにした。1タスクの超過で全体の日程が
    まったく得られなくなる（＝何がどれだけ間に合わないのかも分からない）
    のを避けるため。ResourceOverflowError は、締切を無視しても置き場所が
    見つからない場合にのみ送出する。
18. 稼働日の判定と営業日の加減算を _WorkCalendar（チーム別の稼働日を
    序数添字の配列として事前計算）に置き換え、チームの使用ライン数も
    numpy配列で持つようにした。日付は内部では序数(int)のまま扱う。
    参照表の事前辞書化（_parse_tasks）と合わせて、16,000タスク規模で
    スケジューリング所要時間が約9分の1になっている。
"""

import hashlib
import heapq
import logging
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


def _parse_tasks(df_teams, df_ms, df_wf, df_jobs, df_jtasks, df_extdeps):
    teams_dict = df_teams.set_index("Team_ID")["Max_Lines"].to_dict()
    ext_dep_map = _build_external_dep_map(df_extdeps)

    # ループ内で参照する表は、すべて先に素の辞書・リストへ落としておく
    # （pandasの行アクセスはタスク数が増えるとここが最も重くなるため）。
    ms_end_map = {}
    for ms_id, ms_row in df_ms.iterrows():
        ms_end_map[ms_id] = pd.to_datetime(ms_row.get("End_Date"))

    wf_tasks_by_id = {}
    for row in df_wf.to_dict("records"):
        wf_tasks_by_id.setdefault(row.get("Workflow_ID"), []).append(row)

    overrides_by_key = {}
    if df_jtasks is not None and not df_jtasks.empty:
        for key, row in zip(df_jtasks.index, df_jtasks.to_dict("records")):
            overrides_by_key[key] = row

    active_tasks = {}

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

            if str(override.get("Is_Active", "Y")).strip().upper() == "N":
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

            if task_ms not in ms_end_map:
                raise MissingMilestoneError(
                    f"タスク '{g_id}' が参照するマイルストーン '{task_ms}' が Milestones シートに見つかりません"
                )
            ms_end = ms_end_map[task_ms]
            if pd.isna(ms_end):
                raise MissingMilestoneError(
                    f"マイルストーン '{task_ms}'（タスク '{g_id}' が参照）の End_Date が空です"
                )

            internal_depends = t.get("Internal_Depends")
            int_deps = [
                f"{job_id}:{d.strip()}"
                for d in str(internal_depends).split(",")
                if d.strip() and pd.notna(internal_depends)
            ]
            ext_deps = ext_dep_map.get((job_id, t_id), [])

            active_tasks[g_id] = {
                "job_id": job_id, "job_name": job_name, "task_id": t_id,
                "task_name": t["Task_Name"], "days": days,
                "deps": int_deps + list(ext_deps), "milestone": task_ms,
                "team_id": team_id, "ms_end": ms_end, "ms_end_ord": ms_end.toordinal(),
                "priority": priority, "workflow_id": wf_id,
            }

    active_ids = set(active_tasks.keys())
    for g_id, t_data in active_tasks.items():
        dropped = [d for d in t_data["deps"] if d not in active_ids]
        if dropped:
            logger.warning(f"タスク '{g_id}' の依存先 {dropped} は存在しない、または非アクティブなため無視します")
        t_data["deps"] = [d for d in t_data["deps"] if d in active_ids]

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
    # tail[g] = g から終端タスクまでの最長所要日数（g 自身を含む）。
    # scheduling_order は後続タスクが先に並ぶため、この順で舐めれば
    # 後続の値が必ず先に確定している。
    tail = {}
    max_tail = 0
    for g_id in scheduling_order:
        succ_tail = max((tail[s] for s in successors[g_id]), default=0)
        value = active_tasks[g_id]["days"] + succ_tail
        tail[g_id] = value
        if value > max_tail:
            max_tail = value

    team_load = {}
    for t_info in active_tasks.values():
        team_load[t_info["team_id"]] = team_load.get(t_info["team_id"], 0) + t_info["days"]
    max_load = max(team_load.values(), default=0)

    ms_ends = [t["ms_end_ord"] for t in active_tasks.values()]
    start_ord = project_start.toordinal()
    lo = min([start_ord] + ms_ends) - (max_tail * _CALENDAR_MARGIN_FACTOR + _CALENDAR_MARGIN_DAYS)
    hi = max([start_ord] + ms_ends) + (max_load * _CALENDAR_MARGIN_FACTOR + _CALENDAR_MARGIN_DAYS)

    return _WorkCalendar(lo, hi, holidays_all, holidays_by_team, jp_holidays,
                         auto_exclude_weekends, auto_exclude_jp_holidays)


def _calc_raw_dates(active_tasks, successors, scheduling_order, cal):
    """リソース制約（チームのライン数）を無視した仮の理想日程（ALAP：締切から逆算した最遅日程）。
    休日はスキップする。戻り値は {g_id: (開始日の序数, 終了日の序数)}。"""
    raw_dates = {}
    for g_id in scheduling_order:
        t_info = active_tasks[g_id]
        succs = successors[g_id]
        if not succs:
            t_end = t_info["ms_end_ord"]
        else:
            t_end = min(min(raw_dates[s][0] for s in succs), t_info["ms_end_ord"])
        t_start = cal.business_start(t_end, t_info["days"], t_info["team_id"])
        if t_start is None:
            raise SchedulingError(
                f"タスク '{g_id}' の最遅日程を求められません。マイルストーンの締切が"
                f"早すぎるか、所要日数が長すぎる可能性があります。"
            )
        raw_dates[g_id] = (t_start, t_end)
    return raw_dates


def _calc_asap_dates(active_tasks, leveling_order, project_start_ord, cal):
    """
    リソース制約を無視した、依存関係のみを考慮した最速（ASAP）の理想日程。
    プロジェクト開始日・依存タスク（Internal/External Depends）の完了日のうち
    遅い方を起点に、できるだけ早く着手する前提で計算する。

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
        dep_ends = [asap_dates[d][1] for d in t_info["deps"] if d in asap_dates]
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

    Returns:
        (scheduled, adjusted) のタプル。
        scheduled: {g_id: (開始日の序数, 終了日の序数)}
        adjusted: {g_id: bool}。実際の配置が分散の基準点(target_start)からずれた
            場合（＝チームのライン数不足で動かさざるを得なかった場合）に True。
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

    job_shift_days = _calc_job_shift_days(active_tasks, asap_dates, raw_dates, distribution_ratio)

    for g_id in leveling_order:
        t_info = active_tasks[g_id]
        team_id = t_info["team_id"]
        days = t_info["days"]
        job_id = t_info.get("job_id", g_id)

        dep_ends = [scheduled[d][1] for d in t_info["deps"] if d in scheduled]
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

    return scheduled, adjusted


def _wrap_label(text, width):
    """
    Mermaidのgantt task labelは現状 <br/> や \\n による改行に対応していない
    （2023年時点でMermaid本体の未解決issue）。将来的なレンダラー側の対応や、
    表示環境によっては効く場合もあるためベストエフォートで <br/> を挿入する。
    効かない場合でも、Mermaidはバーからテキストがはみ出す形で全文表示するため
    テキスト自体が読めなくなることはない。
    """
    if not width or len(text) <= width:
        return text
    chunks = [text[i:i + width] for i in range(0, len(text), width)]
    return "<br/>".join(chunks)


def _generate_mermaid_gantt_blocks(result_df, group_col, group_name_map, tick_interval,
                                    label_wrap_width, highlight_resource_adjusted, id_prefix,
                                    milestone_markers=None):
    """
    result_df を group_col（"Workflow_ID" または "Team_ID"）でグルーピングし、
    グループごとに1つのMermaid ganttブロックを生成する（見出し行のリストを返す）。

    各ブロックの先頭に「マイルストーン」セクションを差し込み、続けて Job_ID で
    セクション分けしたタスクを並べる。重ならないタスクは displayMode: compact
    により同じ行にまとめられる。

    milestone_markers: [(id, label, date), ...] のリスト。全ブロックに同じものを
    差し込むことで、Mermaidが自動計算する表示期間（軸の範囲）をブロック間で
    揃える役割も兼ねる（比較しやすくするため）。
    """
    milestone_markers = milestone_markers or []
    lines = []
    weekday_line = ["    weekday monday"] if "week" in tick_interval else []

    group_order = result_df.groupby(group_col)["Start_Date"].min().sort_values().index.tolist()
    for group_id in group_order:
        group_df = result_df[result_df[group_col] == group_id]
        display_name = group_name_map.get(str(group_id), str(group_id))
        lines.append(f"## {display_name}")
        lines.append("")
        lines.append("```mermaid")
        lines.append("---")
        lines.append("displayMode: compact")
        lines.append("---")
        lines.append("gantt")
        lines.append("    dateFormat YYYY-MM-DD")
        lines.append(f"    tickInterval {tick_interval}")
        lines += weekday_line
        lines.append("")

        used_ids = set()

        def _unique_id(raw_id):
            uid = "".join(c if c.isalnum() else "_" for c in raw_id)
            base_id, i = uid, 2
            while uid in used_ids:
                uid = f"{base_id}_{i}"
                i += 1
            used_ids.add(uid)
            return uid

        if milestone_markers:
            # 全ブロック共通のマイルストーン群を先頭セクションとして表示する。
            # プロジェクト全体で同じ日付集合を含めることで、チャート間の
            # 表示期間（軸の範囲）が揃う。
            lines.append("    section マイルストーン")
            for ms_id, ms_label, ms_date in milestone_markers:
                m_id = _unique_id(f"{id_prefix}_MS_{ms_id}")
                m_label = str(ms_label).replace(":", "-")
                m_date = ms_date.strftime("%Y-%m-%d")
                lines.append(f"    {m_label} :milestone, {m_id}, {m_date}, 0d")
            lines.append("")

        job_order = group_df.groupby("Job_ID")["Start_Date"].min().sort_values().index.tolist()
        for job_id in job_order:
            job_group = group_df[group_df["Job_ID"] == job_id].sort_values("Start_Date")
            job_name = str(job_group.iloc[0]["Job_Name"]).replace(":", "-")
            lines.append(f"    section {job_name}")
            for _, r in job_group.iterrows():
                task_id = _unique_id(f"{id_prefix}_{r['Job_ID']}_{r['Task_ID']}")
                status = "crit, " if (highlight_resource_adjusted and r["Resource_Adjusted"]) else ""
                task_label = _wrap_label(str(r["Task_Name"]).replace(":", "-"), label_wrap_width)
                start = r["Start_Date"].strftime("%Y-%m-%d")
                end = r["End_Date"].strftime("%Y-%m-%d")
                lines.append(f"    {task_label} :{status}{task_id}, {start}, {end}")
            lines.append("")

        lines.append("```")
        lines.append("")

    return lines


def _generate_mermaid_gantt(result_df, project_name, tick_interval="1week", label_wrap_width=14,
                             workflow_name_map=None, team_name_map=None,
                             highlight_resource_adjusted=False, milestone_markers=None):
    """
    スケジュール結果のDataFrameから、Mermaid記法のガントチャートを
    含んだMarkdown文字列を生成する。

    - まず「ワークフロー別」セクションで、ワークフロー（キャラクター/背景/
      カットシーン等）ごとにガントチャートのMermaidブロックを分割する。
      続けて「チーム別」セクションで、担当チームごとにも同様に分割する
      （1ファイル内に複数の```mermaid```ブロック）。
    - 見出しには Workflow_Name / Team_Name（任意の表示名）があればそれを使い、
      なければ ID をそのまま使う。
    - 各ブロックの先頭には「マイルストーン」セクションを差し込み、プロジェクト
      開始日と各マイルストーン（Milestonesシート）を milestone（◆マーク）として
      表示する。全ブロックに同じマイルストーン集合を含めることで、Mermaidが
      自動計算する表示期間（軸の範囲）がブロック間で揃い、比較しやすくなる。
    - 各ブロック内はJobごとにセクション分けする（1ジョブ=1系統の流れとして
      タスクを追いやすい）
    - displayMode: compact を有効化し、同じセクション（Job）内で重ならない
      タスクは自動的に同じ行へ詰めて縦の長さを抑える
      （例: Internal_Dependsで並行着手できるタスク同士が重ならなければ1行にまとまる）
    - tick_interval で目盛りの粒度を指定できる（例: "1week", "2week", "1month"）
    - highlight_resource_adjusted=True の場合のみ、リソース制約により前倒しされた
      タスク（Resource_Adjusted=True）を crit（赤色強調）にする（既定はOFF）
    - label_wrap_width 文字を超えるタスク名は <br/> でベストエフォートに折り返す
      （Mermaid側の対応状況によっては効かない場合がある。詳細は _wrap_label 参照）
    - チャート自体にタイトル（title行）は付けない。プロジェクト名は
      Markdown冒頭の見出しにのみ表示する。
    """
    workflow_name_map = workflow_name_map or {}
    team_name_map = team_name_map or {}
    milestone_markers = milestone_markers or []

    lines = [
        f"# {project_name} スケジュール",
        "",
        "ワークフロー別・チーム別の2種類のガントチャートを掲載している。",
        "各チャート内はJobごとにセクション分けしたうえで、重ならないタスクは",
        "同じ行にまとめて表示している。",
        "◆マークはマイルストーン（プロジェクト開始日・各締切日）を示す。",
        "すべてのチャートに同じマイルストーンを含めているため、表示期間は",
        "チャート間で揃っている（比較しやすいように統一）。",
    ]
    if highlight_resource_adjusted:
        lines += [
            "赤色（crit）表示は、リソース制約（チームのライン数不足）により、",
            "本来の理想日程より前倒しされたタスクを示す。",
        ]
    lines.append("")

    if result_df.empty:
        lines += ["（タスクなし）"]
        return "\n".join(lines)

    lines.append("# ワークフロー別")
    lines.append("")
    lines += _generate_mermaid_gantt_blocks(
        result_df, "Workflow_ID", workflow_name_map, tick_interval,
        label_wrap_width, highlight_resource_adjusted, id_prefix="WF",
        milestone_markers=milestone_markers,
    )

    lines.append("# チーム別")
    lines.append("")
    lines += _generate_mermaid_gantt_blocks(
        result_df, "Team_ID", team_name_map, tick_interval,
        label_wrap_width, highlight_resource_adjusted, id_prefix="TEAM",
        milestone_markers=milestone_markers,
    )

    return "\n".join(lines)


def export_mermaid_gantt(result_df, output_path, project_name="プロジェクトスケジュール",
                          tick_interval="1week", label_wrap_width=14,
                          workflow_name_map=None, team_name_map=None,
                          highlight_resource_adjusted=False, milestone_markers=None):
    """result_df（run_resource_constrained_schedulerの戻り値）からMermaidガントチャートの
    Markdownファイルを書き出す（ワークフロー別・チーム別それぞれに分割し、
    各チャート内はJobごとにセクション分け、compact表示）。

    workflow_name_map / team_name_map: {ID: 表示名} の辞書。省略時はIDをそのまま表示する。
    highlight_resource_adjusted: Trueならリソース制約による前倒しタスクを赤色（crit）表示する（既定False）。
    milestone_markers: [(id, label, date), ...] のリスト。各チャートの先頭に
        マイルストーンとして表示し、全チャート共通で含めることで表示期間を揃える。
    """
    content = _generate_mermaid_gantt(
        result_df, project_name, tick_interval=tick_interval,
        label_wrap_width=label_wrap_width,
        workflow_name_map=workflow_name_map, team_name_map=team_name_map,
        highlight_resource_adjusted=highlight_resource_adjusted,
        milestone_markers=milestone_markers,
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"Mermaidガントチャートを書き出しました: {output_path}")
    return output_path


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
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="subtitle">
  ジョブ単位で1行にまとめ（時間が重なるタスクだけ行を分ける）、作業開始が早い順に上から並べている。
  チームのチェックを外すとそのチームのタスクを非表示にし、行の高さも詰めて再描画する。
</div>
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
  return "<b>" + t.job_name + " ＞ " + t.task_name + "</b><br>" +
    "ワークフロー: " + t.workflow_name + "<br>" +
    "チーム: " + t.team_name + "<br>" +
    "優先度: " + t.priority + "<br>" +
    "開始: " + t.start + " ／ 終了: " + t.end + "<br>" +
    "リソース調整: " + adj;
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
    marker: { color: colors },
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

function renderAll() {
  WORKFLOWS.forEach(renderWorkflow);
}

buildFilterPanel();
buildWorkflowSections();
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

    - **ワークフローごとに別々のガントチャートに分割**する（Mermaid版と同様）。
    - 各チャート内は「ジョブ単位で1行」にまとめる。ジョブ名は1回だけ表示し、
      時間的に重なるタスクがある場合だけレーン（行）を追加する（重ならない
      タスクは同じ行に詰める、Mermaidのcompact表示と同じ考え方）。ジョブの
      境界には横線を入れて区切る。バー内にはタスク名を表示する。
    - 各チャート内のジョブは、そのジョブの最初のタスクの開始日が早い順に
      上から並べる。
    - 画面上部の「チームで絞り込み」パネルでチームのチェックを外すと、その
      チームのタスクを全チャートから除外し、**レーンを詰め直して行の高さも
      縮める**（Plotlyの凡例クリックによる表示/非表示とは異なり、非表示分の
      余白が残らない）。チェックボックスの色見本がチーム別配色を兼ねる。
    - バーにマウスを乗せるとジョブ名・タスク名・ワークフロー・優先度・
      開始/終了日・リソース調整有無を表示する。
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
                                        mermaid_output_path=None,
                                        mermaid_tick_interval="1week",
                                        mermaid_label_wrap_width=14,
                                        plotly_output_path=None,
                                        project_name=None,
                                        highlight_resource_adjusted=False,
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
        mermaid_output_path: 指定すると、その日程を Mermaid ガントチャート形式の
            Markdown ファイルとして書き出す（例: "schedule_gantt.md"）。
            ワークフローごとにガントチャートを分割し、各チャート内はJobごとに
            セクション分けしたうえで、displayMode: compact で重ならないタスクは
            同じ行に詰めて表示する。省略時はファイル出力を行わない。
        mermaid_tick_interval: Mermaidガントチャートの目盛り粒度（例: "1day", "1week",
            "2week", "1month"）。既定は "1week"。
        mermaid_label_wrap_width: この文字数を超えるタスク名は <br/> でベストエフォートに
            折り返す（Mermaid側のレンダラー対応状況によっては効かない場合がある）。
        plotly_output_path: 指定すると、サーバー不要でブラウザから直接開ける
            インタラクティブなガントチャート（単一HTMLファイル、Plotly製）を
            書き出す（例: "schedule_gantt.html"）。チーム別に色分けし、凡例
            クリックでチーム単位の表示/非表示切り替え（簡易フィルタリング）が
            できる。省略時はファイル出力を行わない。
        project_name: Markdown冒頭の見出しに使うプロジェクト名。
            省略時は Project シートの Project_Name を使う。
        highlight_resource_adjusted: True の場合、リソース制約により前倒しされた
            タスク（Resource_Adjusted=True）を crit（赤色強調）表示する。
            既定は False（赤色表示なし）。
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

    生成されるガントチャートには、プロジェクト開始日と各マイルストーン
    （Milestonesシート）が「マイルストーン」セクションに milestone（◆）として
    自動的に含まれる。すべてのチャートに同じマイルストーン集合を含めるため、
    Mermaidが自動計算する表示期間（軸の範囲）もチャート間で揃う。

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
        mermaid_output_path=mermaid_output_path,
        mermaid_tick_interval=mermaid_tick_interval,
        mermaid_label_wrap_width=mermaid_label_wrap_width,
        plotly_output_path=plotly_output_path,
        project_name=project_name,
        highlight_resource_adjusted=highlight_resource_adjusted,
        distribution_ratio=distribution_ratio,
    )


def run_resource_constrained_scheduler_from_frames(df_project, df_teams, df_ms, df_wf, df_jobs,
                                                     df_jtasks=None, df_holidays=None,
                                                     df_extdeps=None, df_wf_names=None,
                                                     df_team_capacity=None,
                                                     verbose=True,
                                                     auto_exclude_weekends=True,
                                                     auto_exclude_jp_holidays=True,
                                                     mermaid_output_path=None,
                                                     mermaid_tick_interval="1week",
                                                     mermaid_label_wrap_width=14,
                                                     plotly_output_path=None,
                                                     project_name=None,
                                                     highlight_resource_adjusted=False,
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
        mermaid_output_path=mermaid_output_path,
        mermaid_tick_interval=mermaid_tick_interval,
        mermaid_label_wrap_width=mermaid_label_wrap_width,
        plotly_output_path=plotly_output_path,
        project_name=project_name,
        highlight_resource_adjusted=highlight_resource_adjusted,
        distribution_ratio=distribution_ratio,
    )


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
                              mermaid_output_path=None, mermaid_tick_interval="1week",
                              mermaid_label_wrap_width=14, plotly_output_path=None,
                              project_name=None, highlight_resource_adjusted=False,
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
    # 全チャートに同じ集合を差し込むことで、Mermaidが自動計算する表示期間（軸の範囲）を
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

    teams_dict, active_tasks, active_ids = _parse_tasks(df_teams, df_ms, df_wf, df_jobs, df_jtasks, df_extdeps)
    team_capacity_schedule = _build_team_capacity_schedule(df_teams, df_team_capacity)

    if not active_ids:
        logger.warning("アクティブなタスクがありません")
        result_df = pd.DataFrame(columns=[
            "Job_ID", "Task_ID", "Job_Name", "Task_Name", "Team_ID", "Priority",
            "Workflow_ID", "Milestone_ID", "Start_Date", "End_Date",
            "Resource_Adjusted", "Deadline_Overrun_Days"
        ])
        if mermaid_output_path:
            export_mermaid_gantt(result_df, mermaid_output_path, project_name,
                                  tick_interval=mermaid_tick_interval,
                                  label_wrap_width=mermaid_label_wrap_width,
                                  workflow_name_map=workflow_name_map,
                                  team_name_map=team_name_map,
                                  highlight_resource_adjusted=highlight_resource_adjusted,
                                  milestone_markers=milestone_markers)
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
    scheduled, adjusted_flags = _run_leveling(
        active_tasks, leveling_order, team_capacity_schedule, project_start_ord, cal,
        asap_dates=asap_dates, raw_dates=raw_dates, distribution_ratio=distribution_ratio,
    )

    rows = []
    for g_id, dates in scheduled.items():
        t = active_tasks[g_id]
        # 分散配置の基準点(target_start)からチームのライン数不足によりずれた
        # 場合に True になる（distribution_ratio による意図的な分散配置そのものは
        # 「調整あり」に含めない）
        resource_adjusted = adjusted_flags.get(g_id, False)
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
        })

    result_df = pd.DataFrame(rows).sort_values(["Start_Date", "Job_ID", "Task_ID"]).reset_index(drop=True)

    _warn_deadline_overruns(result_df)

    if verbose:
        print("=== リソース制約考慮スケジューリング結果 ===")
        for _, r in result_df.iterrows():
            mark = " ⚠️ [リソース制約により前倒し]" if r["Resource_Adjusted"] else ""
            print(
                f"[{r['Team_ID']}] {r['Job_Name']} > {r['Task_Name']} "
                f"(優先度{r['Priority']}): "
                f"{r['Start_Date'].strftime('%Y-%m-%d')} ～ {r['End_Date'].strftime('%Y-%m-%d')}{mark}"
            )

    if mermaid_output_path:
        export_mermaid_gantt(result_df, mermaid_output_path, project_name,
                              tick_interval=mermaid_tick_interval,
                              label_wrap_width=mermaid_label_wrap_width,
                              workflow_name_map=workflow_name_map,
                              team_name_map=team_name_map,
                              highlight_resource_adjusted=highlight_resource_adjusted,
                              milestone_markers=milestone_markers)

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
        "-o", "--output", default="output/schedule_gantt.md",
        help="Mermaidガントチャートの出力先Markdownパス",
    )
    parser.add_argument(
        "--html-output", default="output/schedule_gantt.html",
        help="サーバー不要で開けるPlotly製インタラクティブガントチャート"
             "（チーム別色分け）の出力先HTMLパス。空文字を指定すると出力しない",
    )
    parser.add_argument(
        "--tick-interval", default="1week",
        help="ガントチャートの目盛り粒度（例: 1day, 1week, 2week, 1month）",
    )
    parser.add_argument(
        "--distribution-ratio", type=float, default=0.7,
        help="ASAP(0.0)〜ALAP(1.0)間の配置基準点（既定0.7）",
    )
    parser.add_argument(
        "--highlight-resource-adjusted", action="store_true",
        help="リソース制約により前倒しされたタスクを赤色（crit）表示する",
    )
    args = parser.parse_args()

    try:
        run_resource_constrained_scheduler(
            args.excel_file,
            mermaid_output_path=args.output,
            mermaid_tick_interval=args.tick_interval,
            plotly_output_path=args.html_output or None,
            distribution_ratio=args.distribution_ratio,
            highlight_resource_adjusted=args.highlight_resource_adjusted,
        )
    except SchedulingError as e:
        logger.error(f"スケジューリングに失敗しました: {e}")
        raise
