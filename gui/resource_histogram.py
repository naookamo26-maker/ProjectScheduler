"""
階段関数の区間化・複数系列の積み上げ用ユーティリティ（Qt非依存の純粋関数）。

タブ1「基本情報設定」にあったリソースヒストグラム（チームの計画上限
「同時ライン数」の推移を表示する機能）はプロジェクト分析タブへの機能移管に
伴い廃止した（`docs/project_analysis_tab_design.md`参照）。ただしこの3関数
自体はチーム容量に限らない汎用の階段関数処理であり、プロジェクト分析タブの
「チーム別サマリー」（`gui/team_summary_view.py`）の積み上げグラフ・
設定上限の破線にそのまま使い回せるため、Qt非依存の純粋関数としてここに残す。
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


def value_at(breakpoints, d):
    """breakpoints: [(date, value), ...]。日付dの時点で有効な値
    （dより後にしか変化点が無ければ0）。積み上げ描画が、系列ごとに異なる
    変化点の集合を共通の区切り（shared_boundaries）へ揃えて評価するために使う。"""
    value = 0
    for bd, bv in sorted(breakpoints):
        if bd <= d:
            value = bv
        else:
            break
    return value


def shared_boundaries(breakpoints_by_key, range_start, range_end):
    """複数系列（breakpoints_by_key: {key: [(date, value), ...]}）を積み上げて
    描画するために、range内で「どれか1系列でも値が変わりうる」日付をすべて
    集めた、共通の区切り日付リスト（range_start, range_endを含む昇順）を返す。

    区切りの総数は系列内の変化点の「重複除去後の日付数」で決まる——同じ日に
    複数のタスクが開始・終了しても、その日の変化点は1つに畳み込まれる
    （`gui/summary_metrics.team_concurrency_steps` の diff+cumsum参照）ため、
    タスク数ではなく実際の日数のオーダーに収まる。"""
    dates = {range_start, range_end}
    for bps in breakpoints_by_key.values():
        for d, _v in bps:
            if range_start < d <= range_end:
                dates.add(d)
    return sorted(dates)
