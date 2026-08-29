"""
階段関数の区間化（`compute_step_segments`）。

タブ1「基本情報設定」にあったリソースヒストグラム（チームの計画上限
「同時ライン数」の推移を表示する機能）はプロジェクト分析タブへの機能移管に
伴い廃止した（`docs/project_analysis_tab_design.md`参照）。ただしこの関数
自体はチーム容量に限らない汎用の階段関数区間化であり、プロジェクト分析
タブの「上限に張り付いた日数」の集計
（`gui/summary_metrics._team_pinned_days`——同時タスク数と設定上限という
2つの階段関数を共通の区切りへ揃えて突き合わせる）でそのまま使い回せるため、
Qt非依存の純粋関数としてここに残す。
"""


def compute_step_segments(breakpoints, range_start, range_end):
    """breakpoints: [(date, value), ...]（順不同可、同日重複は最後の値が勝つ）。
    [range_start, range_end] の範囲に階段関数を区切った
    [(seg_start, seg_end, value), ...] を返す（区間はすべて閉区間の
    range内に収まる）。range_start時点で有効な変化点が1つも無ければ
    （＝range_startより後にしか変化点が無ければ）、その手前は「値が
    定まらない」として区間を作らない。range_start > range_end、または
    range内に有効な値が一度も無ければ空リストを返す。"""
    if range_start > range_end:
        return []
    dedup = {}
    for d, v in breakpoints:
        dedup[d] = v
    sorted_points = sorted(dedup.items())

    active_value = None
    remaining = []
    for d, v in sorted_points:
        if d <= range_start:
            active_value = v
        else:
            remaining.append((d, v))

    segments = []
    cursor = range_start
    current_value = active_value
    for d, v in remaining:
        if d > range_end:
            break
        if current_value is not None and d > cursor:
            segments.append((cursor, d, current_value))
        cursor, current_value = d, v
    if current_value is not None and cursor <= range_end:
        segments.append((cursor, range_end, current_value))
    return segments
