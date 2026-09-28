"""
ガントチャートタブの行（ジョブ）の並び順（gui/gantt_row_order.py）のテスト。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pd = pytest.importorskip("pandas")

from gui.gantt_row_order import (  # noqa: E402
    GROUP_ALL,
    GROUP_WORKFLOW,
    ORDER_END,
    ORDER_MILESTONE,
    ORDER_PRIORITY,
    ORDER_START,
    keep_order,
    sort_job_ids,
)

# 分類: scheduler（スケジューリング結果の DataFrame を扱うので pandas が要る）
pytestmark = pytest.mark.scheduler

_DISPLAY = {
    # ワークフロー設計タブの並び順（WF_002 が先）
    "workflow_names": {"WF_002": "背景", "WF_001": "キャラ"},
    "milestone_markers": [
        ("PROJECT_START", "開始", pd.Timestamp("2026-01-01")),
        ("MS_001", "アルファ", pd.Timestamp("2026-06-30")),
        ("MS_002", "ベータ", pd.Timestamp("2026-12-31")),
    ],
}


def _df(jobs):
    """jobs: [(Job_ID, 名前, ワークフロー, 優先度, マイルストーン, 開始, 終了), ...]（1ジョブ1タスク）。"""
    return pd.DataFrame([
        {"Job_ID": j, "Task_ID": "T_001", "Job_Name": name, "Workflow_ID": wf, "Priority": prio,
         "Milestone_ID": ms, "Start_Date": pd.Timestamp(start), "End_Date": pd.Timestamp(end)}
        for j, name, wf, prio, ms, start, end in jobs
    ])


# 名前（Z〜W）は作った順（番号）と逆にしてあり、同点が名前ではなく作った順で決まることを確かめる
JOBS = _df([
    ("JOB_A", "Z", "WF_001", 3, "MS_002", "2026-02-01", "2026-05-01"),
    ("JOB_B", "Y", "WF_002", 1, "MS_001", "2026-03-01", "2026-04-01"),
    ("JOB_C", "X", "WF_001", 2, "", "2026-01-15", "2026-06-01"),
    ("JOB_D", "W", "WF_002", 999, "MS_001", "2026-03-01", "2026-03-20"),
])


@pytest.mark.parametrize("order, descending, expected", [
    # 開始日が同じ B と D は、作った順（降順でも作った順のまま）
    (ORDER_START, False, ["JOB_C", "JOB_A", "JOB_B", "JOB_D"]),
    (ORDER_START, True, ["JOB_B", "JOB_D", "JOB_A", "JOB_C"]),
    (ORDER_END, False, ["JOB_D", "JOB_B", "JOB_A", "JOB_C"]),
    (ORDER_END, True, ["JOB_C", "JOB_A", "JOB_B", "JOB_D"]),
    # 同じマイルストーンの中は終了日順。締切の無いジョブ（C）は昇順・降順とも最後
    (ORDER_MILESTONE, False, ["JOB_D", "JOB_B", "JOB_A", "JOB_C"]),
    (ORDER_MILESTONE, True, ["JOB_A", "JOB_B", "JOB_D", "JOB_C"]),
    # 優先度は小さいほど高い。未設定（最低優先の 999）は昇順で最後
    (ORDER_PRIORITY, False, ["JOB_B", "JOB_C", "JOB_A", "JOB_D"]),
    (ORDER_PRIORITY, True, ["JOB_D", "JOB_A", "JOB_C", "JOB_B"]),
])
def test_orders_over_all_jobs(order, descending, expected):
    assert sort_job_ids(JOBS, _DISPLAY, GROUP_ALL, order, descending) == expected


def test_grouping_by_workflow_follows_the_workflow_design_order():
    """ワークフロー設計タブの並び順でまとめ、その中を選んだ並びで並べる。
    降順にしてもワークフロー同士の順は変えない。"""
    assert sort_job_ids(JOBS, _DISPLAY, GROUP_WORKFLOW, ORDER_START) == ["JOB_B", "JOB_D", "JOB_C", "JOB_A"]
    assert sort_job_ids(JOBS, _DISPLAY, GROUP_WORKFLOW, ORDER_END, True) == ["JOB_B", "JOB_D", "JOB_C", "JOB_A"]


def test_a_job_deadline_is_the_latest_among_its_tasks():
    """タスクごとにマイルストーンを上書きできるので、ジョブの締切は最も遅いタスクの締切。"""
    df = pd.concat([
        JOBS[JOBS["Job_ID"] != "JOB_A"],
        _df([("JOB_A", "A", "WF_001", 3, "MS_001", "2026-02-01", "2026-05-01")]),
        _df([("JOB_A", "Z", "WF_001", 3, "", "2026-02-01", "2026-05-01")]),
    ])
    assert sort_job_ids(df, _DISPLAY, GROUP_ALL, ORDER_MILESTONE) == ["JOB_D", "JOB_B", "JOB_A", "JOB_C"]


def test_keep_order_keeps_known_rows_and_inserts_new_ones_next_to_their_neighbour():
    previous = ["JOB_B", "JOB_A", "JOB_C"]
    # 編集で JOB_A の開始が一番早くなっても、前回の並びを保つ
    assert keep_order(previous, ["JOB_A", "JOB_B", "JOB_C"]) == previous
    # 消えたジョブは除き、新しいジョブは今の並びで直前のジョブの後ろへ
    assert keep_order(previous, ["JOB_B", "JOB_D", "JOB_A"]) == ["JOB_B", "JOB_D", "JOB_A"]
    assert keep_order(previous, ["JOB_E", "JOB_C"]) == ["JOB_E", "JOB_C"]
    assert keep_order([], ["JOB_A", "JOB_B"]) == ["JOB_A", "JOB_B"]


def test_ties_follow_the_creation_order_by_number():
    """同じ開始日のジョブは作った順。番号は数として比べる（JOB_1000 は JOB_999 の後）。"""
    df = _df([
        ("JOB_1000", "A", "WF_001", 1, "", "2026-02-01", "2026-03-01"),
        ("JOB_999", "B", "WF_001", 1, "", "2026-02-01", "2026-03-01"),
        ("JOB_002", "C", "WF_001", 1, "", "2026-02-01", "2026-03-01"),
    ])
    assert sort_job_ids(df, _DISPLAY) == ["JOB_002", "JOB_999", "JOB_1000"]
