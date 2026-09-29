"""
ガントチャートタブの行（ジョブ）の並び順。

分類（全体／ワークフロー別／依存のつながり）と並び（開始日・終了日・マイルストーン・優先度）、
昇順／降順を組み合わせて、ジョブの並びを決める。画面（gui/tab_gantt.py）から
切り離し、スケジューリング結果の DataFrame だけで計算できるようにしてある。

並びが変わるのは、並び順を選び直したときと「並べ直す」ボタンを押したときだけ。
編集の後は、対象のジョブを見失わないよう前回の並びを保つ（keep_order）。
"""

import re

import pandas as pd

GROUP_ALL = "all"
GROUP_WORKFLOW = "workflow"
GROUP_DEPENDENCY = "dependency"
GROUPINGS = (GROUP_ALL, GROUP_WORKFLOW, GROUP_DEPENDENCY)

ORDER_START = "start"
ORDER_END = "end"
ORDER_MILESTONE = "milestone"
ORDER_PRIORITY = "priority"
ORDERS = (ORDER_START, ORDER_END, ORDER_MILESTONE, ORDER_PRIORITY)


def _job_facts(df, display):
    """ジョブごとの (開始, 終了, 締切 or None, 優先度, ワークフロー)。"""
    deadlines = {mid: when for mid, _name, when in (display or {}).get("milestone_markers", ())}
    # ジョブごとの集計は groupby の集計でまとめて行う（ジョブごとに DataFrame を取り出すと、
    # 大きな計画で並べ替えだけで数秒かかる）
    grouped = df.groupby("Job_ID", sort=False)
    starts = grouped["Start_Date"].min()
    ends = grouped["End_Date"].max()
    # タスクごとにマイルストーンを上書きできるので、ジョブ全体が終わるべき
    # 締切＝タスクの締切のうち最も遅いものをジョブの締切とする
    job_deadlines = df["Milestone_ID"].map(deadlines).groupby(df["Job_ID"], sort=False).max()
    firsts = df.drop_duplicates("Job_ID").set_index("Job_ID")
    facts = {}
    for job_id, first in zip(firsts.index, firsts.itertuples(index=False)):
        deadline = job_deadlines.get(job_id)
        facts[job_id] = {
            "start": starts[job_id],
            "end": ends[job_id],
            "deadline": None if pd.isna(deadline) else deadline,
            # 未設定の優先度は、スケジューラが最低優先（project_scheduler.DEFAULT_LOW_PRIORITY）
            # として扱った値がそのまま入っている。並びでも同じく最低優先として扱う
            "priority": first.Priority if pd.notna(first.Priority) else float("inf"),
            "workflow": first.Workflow_ID,
        }
    return facts


def _creation_key(job_id):
    """ジョブを作った順（Job_ID の番号順。JOB_999 の次の JOB_1000 も番号で比べる）。"""
    match = re.search(r"(\d+)$", str(job_id))
    return (int(match.group(1)) if match else float("inf"), str(job_id))


def _sort_key(order):
    if order == ORDER_END:
        return lambda f: (f["end"],)
    if order == ORDER_MILESTONE:
        # 同じマイルストーンの中は、締切に間に合うかを見やすいよう終了日順
        return lambda f: (f["deadline"], f["end"])
    if order == ORDER_PRIORITY:
        return lambda f: (f["priority"], f["start"])
    return lambda f: (f["start"],)


def sort_job_ids(df, display, grouping=GROUP_ALL, order=ORDER_START, descending=False, job_links=()):
    """df（スケジューリング結果。絞り込み後でよい）に含まれるジョブの並びを返す。

    - 同じ値のジョブはジョブを作った順（計算し直すたびに入れ替わらないように）。
      降順でも作った順のまま。この機能を入れる前の開始日順も、同じ開始日のジョブを
      作った順に並べていたので、既定（全体・開始日順）の表示は以前と変わらない。
    - マイルストーン順で締切の無いジョブは、昇順・降順とも最後。
    - ワークフロー別では、ワークフロー設計タブの並び順（display["workflow_names"]
      の順）でまとめ、その中を上の並びで並べる。ワークフロー同士の順は降順でも変えない。
    - 依存のつながりでは、ジョブをまたぐ依存（job_links: [((前のJob_ID, …), (後のJob_ID, …), …)]。
      スケジューリング結果の attrs["job_links"]）でつながったジョブをまとめ、依存される
      ジョブのすぐ下に依存するジョブを並べる（_order_by_dependency）。
    """
    if df is None or df.empty:
        return []
    facts = _job_facts(df, display)
    key = _sort_key(order)

    def ordered(job_ids):
        # 作った順に並べてから主キーで安定ソートする（reverse=True でも安定なので、
        # 同じ値のジョブは降順でも作った順に残る）
        created = sorted(job_ids, key=_creation_key)
        if order == ORDER_MILESTONE:
            undated = [j for j in created if facts[j]["deadline"] is None]
            created = [j for j in created if facts[j]["deadline"] is not None]
        else:
            undated = []
        return sorted(created, key=lambda j: key(facts[j]), reverse=descending) + undated

    if grouping == GROUP_DEPENDENCY:
        return _order_by_dependency(ordered(list(facts)), job_links)
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


def _order_by_dependency(ordered_jobs, job_links):
    """依存される側のジョブのすぐ下に、依存する側のジョブを並べる。

    ordered_jobs（選んだ並び順で並べた全ジョブ）の順に、依存先を持たないジョブから
    置いていき、置いたジョブに依存するジョブをその直下へ続ける（深さ優先）。複数の
    ジョブに依存するジョブは、依存先がすべて置かれた時点で最後の依存先の下に置く。
    同じ親の下の兄弟、まとまり同士の順は ordered_jobs の順。ジョブ単位では依存が
    輪になることがある（A のタスク → B、B の別のタスク → A）ので、輪の中で置けずに
    残ったジョブは ordered_jobs の順に置く。依存が長く続いても落ちないよう再帰は使わない。
    """
    present = set(ordered_jobs)
    rank = {job_id: i for i, job_id in enumerate(ordered_jobs)}
    children, parents = {}, {}
    for (pred, *_p), (succ, *_s), *_rest in job_links:
        if pred == succ or pred not in present or succ not in present:
            continue
        children.setdefault(pred, set()).add(succ)
        parents.setdefault(succ, set()).add(pred)

    result, placed = [], set()

    def place_from(root):
        stack = [root]
        while stack:
            job_id = stack.pop()
            if job_id in placed:
                continue
            placed.add(job_id)
            result.append(job_id)
            ready = [c for c in children.get(job_id, ())
                     if c not in placed and parents[c] <= placed]
            # 先に置くものほど後から積む（スタックなので逆順）
            stack.extend(sorted(ready, key=rank.get, reverse=True))

    for job_id in ordered_jobs:
        if job_id not in placed and parents.get(job_id, set()) <= placed:
            place_from(job_id)
    for job_id in ordered_jobs:  # 輪になっていて置けなかったもの
        if job_id not in placed:
            place_from(job_id)
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
