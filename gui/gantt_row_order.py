"""
ガントチャートタブの行（ジョブ）の並び順。

分類（全体／ワークフロー別）と並び（開始日・終了日・マイルストーン・優先度）、
昇順／降順を組み合わせて、ジョブの並びを決める。画面（gui/tab_gantt.py）から
切り離し、スケジューリング結果の DataFrame だけで計算できるようにしてある。

並びが変わるのは、並び順を選び直したときと「並べ直す」ボタンを押したときだけ。
編集の後は、対象のジョブを見失わないよう前回の並びを保つ（keep_order）。
"""

import pandas as pd

GROUP_ALL = "all"
GROUP_WORKFLOW = "workflow"
GROUPINGS = (GROUP_ALL, GROUP_WORKFLOW)

ORDER_START = "start"
ORDER_END = "end"
ORDER_MILESTONE = "milestone"
ORDER_PRIORITY = "priority"
ORDERS = (ORDER_START, ORDER_END, ORDER_MILESTONE, ORDER_PRIORITY)


def _job_facts(df, display):
    """ジョブごとの (開始, 終了, 締切 or None, 優先度, ワークフロー, 名前)。"""
    deadlines = {mid: when for mid, _name, when in (display or {}).get("milestone_markers", ())}
    facts = {}
    for job_id, rows in df.groupby("Job_ID", sort=False):
        # タスクごとにマイルストーンを上書きできるので、ジョブ全体が終わるべき
        # 締切＝タスクの締切のうち最も遅いものをジョブの締切とする
        job_deadlines = [deadlines[m] for m in rows["Milestone_ID"] if m in deadlines]
        first = rows.iloc[0]
        facts[job_id] = {
            "start": rows["Start_Date"].min(),
            "end": rows["End_Date"].max(),
            "deadline": max(job_deadlines) if job_deadlines else None,
            # 未設定の優先度は、スケジューラが最低優先（project_scheduler.DEFAULT_LOW_PRIORITY）
            # として扱った値がそのまま入っている。並びでも同じく最低優先として扱う
            "priority": first["Priority"] if pd.notna(first["Priority"]) else float("inf"),
            "workflow": first["Workflow_ID"],
            "name": str(first["Job_Name"]),
        }
    return facts


def _sort_key(order):
    if order == ORDER_END:
        return lambda f: (f["end"],)
    if order == ORDER_MILESTONE:
        # 同じマイルストーンの中は、締切に間に合うかを見やすいよう終了日順
        return lambda f: (f["deadline"], f["end"])
    if order == ORDER_PRIORITY:
        return lambda f: (f["priority"], f["start"])
    return lambda f: (f["start"],)


def sort_job_ids(df, display, grouping=GROUP_ALL, order=ORDER_START, descending=False):
    """df（スケジューリング結果。絞り込み後でよい）に含まれるジョブの並びを返す。

    - 同じ値のジョブはジョブ名の順（計算し直すたびに入れ替わらないように）。
      降順でもジョブ名は昇順のまま。
    - マイルストーン順で締切の無いジョブは、昇順・降順とも最後。
    - ワークフロー別では、ワークフロー設計タブの並び順（display["workflow_names"]
      の順）でまとめ、その中を上の並びで並べる。ワークフロー同士の順は降順でも変えない。
    """
    if df is None or df.empty:
        return []
    facts = _job_facts(df, display)
    key = _sort_key(order)

    def ordered(job_ids):
        # 名前順に並べてから主キーで安定ソートする（reverse=True でも安定なので、
        # 同じ値のジョブは降順でも名前の昇順に残る）
        by_name = sorted(job_ids, key=lambda j: (facts[j]["name"], j))
        if order == ORDER_MILESTONE:
            undated = [j for j in by_name if facts[j]["deadline"] is None]
            by_name = [j for j in by_name if facts[j]["deadline"] is not None]
        else:
            undated = []
        return sorted(by_name, key=lambda j: key(facts[j]), reverse=descending) + undated

    if grouping != GROUP_WORKFLOW:
        return ordered(list(facts))
    workflow_rank = {wf: i for i, wf in enumerate((display or {}).get("workflow_names", {}))}
    groups = {}
    for job_id, f in facts.items():
        groups.setdefault(f["workflow"], []).append(job_id)
    result = []
    for wf in sorted(groups, key=lambda w: (workflow_rank.get(w, len(workflow_rank)), str(w))):
        result.extend(ordered(groups[wf]))
    return result


def keep_order(previous, fresh):
    """前回の並び（previous）を保ったまま、今の顔ぶれ（fresh＝今の規則での並び）に合わせる。

    - 前回もあったジョブは前回の順のまま（編集で開始日が変わっても行は動かない）。
    - 消えたジョブは除く。
    - 新しく加わったジョブ（追加・絞り込みの解除）は、今の規則での並びで直前にある
      ジョブのすぐ後ろに差し込む（直前が無ければ先頭）。
    """
    present = set(fresh)
    result = [j for j in previous if j in present]
    placed = set(result)
    previous_in_fresh = None
    for job_id in fresh:
        if job_id not in placed:
            index = result.index(previous_in_fresh) + 1 if previous_in_fresh is not None else 0
            result.insert(index, job_id)
            placed.add(job_id)
        previous_in_fresh = job_id
    return result
