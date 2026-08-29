"""
プロジェクト分析タブ「チーム別サマリー」の描画（QGraphicsScene直接描画）。

`gui/gantt_view.py`・旧`gui/resource_histogram.py`のヒストグラム描画と同じ、
素のQGraphicsScene直接描画方式を踏襲する（軸・目盛りラベルの考え方も同様。
docs/project_analysis_tab_design.md §5参照）。ズーム・パン・A/Fキーでの
フィットは`gui/gantt_view.py`の`GanttGraphicsView`をそのまま再利用し
（旧`ResourceHistogramView`と同じ考え方）、フィット処理だけ
`TeamSummaryChartView`に自己完結で追加する。

積み上げグラフの描画区間数は「全チームの変化点の日付数（重複除去後）」の
オーダーに収まる（`gui/resource_histogram.shared_boundaries`参照）。同じ日に
複数のタスクが開始・終了しても変化点は1つに畳み込まれるため、タスク数
そのものではなく実際の日数のオーダーになり、タスク数の多い大規模
プロジェクトでも描画コストが跳ね上がらない。
"""

from datetime import timedelta

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsSimpleTextItem

from gui.gantt_view import GanttGraphicsView
from gui.resource_histogram import compute_step_segments, shared_boundaries, value_at
from project_scheduler import _UNLIMITED_LINES

DAY_WIDTH = 4
ROW_UNIT_HEIGHT = 18
LEFT_MARGIN = 50
TOP_MARGIN = 36
# 最も高いバー（積み上げ合計／同時タスク数・上限のいずれか大きい方）の上端と
# 目盛りエリア上端が接しないよう、最大値の上にさらに1行分の空白を確保する
# （gui/resource_histogram.py の旧ヒストグラム描画と同じ考え方）。
TOP_PADDING_ROWS = 1
TOP_MARGIN_MIN_FRACTION = 0.12
_GRID_COLOR = QColor("#e1e0d9")
_AXIS_TEXT_COLOR = QColor("#52514e")
_MILESTONE_COLOR = QColor("#c0392b")
_PROJECT_START_COLOR = QColor("#52514e")
_PANE_BG = QColor("#fdfcf9")
_MILESTONE_LINE_Z = 10
_LABEL_TOP = 4
_CAPACITY_LINE_COLOR = QColor("#0b0b0b")
_CAPACITY_LINE_Z = 5


def _tick_step_days(total_days):
    if total_days > 365:
        return 28
    if total_days > 120:
        return 14
    return 7


def _compute_top_margin(bar_height_range):
    return max(TOP_MARGIN, bar_height_range * TOP_MARGIN_MIN_FRACTION)


def _grid_line(x1, y1, x2, y2, color=None, dashed=False, width=1, z_value=-1):
    pen = QPen(color or _GRID_COLOR, width, Qt.DashLine if dashed else Qt.SolidLine)
    line = QGraphicsPathItem()
    path = QPainterPath(QPointF(x1, y1))
    path.lineTo(QPointF(x2, y2))
    line.setPath(path)
    line.setPen(pen)
    line.setZValue(z_value)
    return line


def _add_label(scene, text, font, pos, brush=None):
    label = QGraphicsSimpleTextItem(text)
    label.setFont(font)
    if brush is not None:
        label.setBrush(brush)
    label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
    label.setPos(*pos)
    scene.addItem(label)
    return label


def _new_scene_with_axes(max_value, milestone_markers, project_start, range_start, range_end):
    """軸・グリッド線・マイルストーン線だけを描いた空のシーンを組み立てて、
    (scene, x_of, chart_bottom, chart_right) を返す。呼び出し側はこの上に
    バー・線を追加する。"""
    scene = QGraphicsScene()
    total_days = max((range_end - range_start).days, 1)

    def x_of(d):
        return LEFT_MARGIN + (d - range_start).days * DAY_WIDTH

    bar_height_range = (max(max_value, 1) + TOP_PADDING_ROWS) * ROW_UNIT_HEIGHT
    top_margin = _compute_top_margin(bar_height_range)
    chart_bottom = top_margin + bar_height_range
    chart_right = x_of(range_end)

    axis_font = QFont()
    axis_font.setPointSize(8)

    # Y軸（同時タスク数の目盛り）
    y_step = max(1, round(max_value / 5)) if max_value > 5 else 1
    y = 0
    while y <= max_value:
        gy = chart_bottom - y * ROW_UNIT_HEIGHT
        scene.addItem(_grid_line(LEFT_MARGIN, gy, chart_right, gy))
        _add_label(scene, str(y), axis_font, (4, gy - 7), QBrush(_AXIS_TEXT_COLOR))
        y += y_step

    # X軸（日付目盛り）
    step = _tick_step_days(total_days)
    tick_date = range_start + timedelta(days=(7 - range_start.weekday()) % 7)
    while tick_date <= range_end:
        x = x_of(tick_date)
        scene.addItem(_grid_line(x, top_margin, x, chart_bottom))
        _add_label(scene, tick_date.strftime("%m/%d"), axis_font, (x - 14, chart_bottom + 4))
        tick_date += timedelta(days=step)

    # 開発開始日・マイルストーンの縦線（gui/gantt_view.py と同じ見た目）
    milestone_font = QFont(axis_font)
    milestone_font.setBold(True)
    if project_start is not None and range_start <= project_start <= range_end:
        x = x_of(project_start)
        scene.addItem(_grid_line(
            x, _LABEL_TOP, x, chart_bottom, _PROJECT_START_COLOR,
            dashed=True, width=2, z_value=_MILESTONE_LINE_Z,
        ))
    for _mid, mlabel, mdate in milestone_markers:
        if not (range_start <= mdate <= range_end):
            continue
        x = x_of(mdate)
        scene.addItem(_grid_line(
            x, _LABEL_TOP, x, chart_bottom, _MILESTONE_COLOR,
            dashed=True, width=2, z_value=_MILESTONE_LINE_Z,
        ))
        _add_label(scene, mlabel, milestone_font, (x + 2, _LABEL_TOP), QBrush(_MILESTONE_COLOR))

    scene.setBackgroundBrush(QBrush(_PANE_BG))
    return scene, x_of, chart_bottom, chart_right


def build_team_stacked_scene(concurrency_by_team, color_map, labels_by_team,
                              milestone_markers, project_start, range_start, range_end):
    """全チームの同時タスク数を積み上げた面グラフ。

    concurrency_by_team: {Team_ID: [(date, 同時タスク数), ...]}
    （`gui/summary_metrics.team_concurrency_steps()` をチームごとに呼んだもの）。
    color_map/labels_by_team: `gui/gantt_generator.build_display()` の
    team_colors/team_names をそのまま渡せる。"""
    boundaries = shared_boundaries(concurrency_by_team, range_start, range_end)
    max_value = 0
    band_items = []
    for i in range(len(boundaries) - 1):
        seg_start, seg_end = boundaries[i], boundaries[i + 1]
        bottom = 0
        for team_id, breakpoints in concurrency_by_team.items():
            value = value_at(breakpoints, seg_start)
            if value > 0:
                band_items.append((seg_start, seg_end, bottom, bottom + value, team_id))
            bottom += value
        max_value = max(max_value, bottom)

    scene, x_of, chart_bottom, _chart_right = _new_scene_with_axes(
        max_value, milestone_markers, project_start, range_start, range_end,
    )

    # チーム名は帯の中に描き込まない。積み上げグラフは区間数が日数のオーダー
    # まで増えうるため、A/Fキーでのフィット後に大きく縮小された表示（多チーム・
    # 長期間のプロジェクトほど縮小率が上がる）では、ItemIgnoresTransformations
    # で画面上一定サイズを保つラベル文字がその縮小に追従せず、狭い帯に対して
    # 相対的に巨大化して重なり合ってしまう。どの色がどのチームかは、
    # 呼び出し側（gui/tab_analysis.py）がチャートの外に置く凡例と、この
    # バーのツールチップで補う（docs/project_analysis_tab_design.md §7-6）。
    for seg_start, seg_end, lo, hi, team_id in band_items:
        x1, x2 = x_of(seg_start), x_of(seg_end)
        y_top = chart_bottom - hi * ROW_UNIT_HEIGHT
        y_bottom = chart_bottom - lo * ROW_UNIT_HEIGHT
        path = QPainterPath()
        path.addRect(QRectF(x1, y_top, max(x2 - x1, 1), y_bottom - y_top))
        rect = QGraphicsPathItem(path)
        rect.setBrush(QBrush(QColor(color_map.get(team_id, "#cbc9c2"))))
        rect.setPen(QPen(QColor("#0b0b0b"), 1))
        name = labels_by_team.get(team_id, team_id)
        rect.setToolTip(
            f"{name}\n{seg_start.strftime('%Y-%m-%d')} 〜 {seg_end.strftime('%Y-%m-%d')}\n同時 {hi - lo} 本"
        )
        scene.addItem(rect)

    scene.team_summary_max_value = max_value
    return scene


def build_team_detail_scene(concurrency_steps, capacity_periods, color,
                             milestone_markers, project_start, range_start, range_end):
    """選択した1チームの同時タスク数（塗り）と設定上限（破線）。

    concurrency_steps: `gui/summary_metrics.team_concurrency_steps(result_df, team_id)`。
    capacity_periods: `gui/gantt_generator.build_display()` の
    team_capacity_schedule[team_id]（{Team_ID: [(適用開始日, ライン数), ...]}）。
    上限が「指定なし」（`project_scheduler._UNLIMITED_LINES`）の区間は破線を
    描かない——線が無いこと自体が「まだ人数を決めていない」ことの表示になる
    （docs/project_analysis_tab_design.md §2-3）。"""
    concurrency_segments = compute_step_segments(concurrency_steps, range_start, range_end)
    cap_segments = compute_step_segments(capacity_periods, range_start, range_end)
    finite_caps = [v for _s, _e, v in cap_segments if v != _UNLIMITED_LINES]
    max_value = max([v for _s, _e, v in concurrency_segments] + finite_caps + [0])

    scene, x_of, chart_bottom, _chart_right = _new_scene_with_axes(
        max_value, milestone_markers, project_start, range_start, range_end,
    )

    for seg_start, seg_end, value in concurrency_segments:
        x1, x2 = x_of(seg_start), x_of(seg_end)
        y_top = chart_bottom - value * ROW_UNIT_HEIGHT
        path = QPainterPath()
        path.addRect(QRectF(x1, y_top, max(x2 - x1, 1), chart_bottom - y_top))
        rect = QGraphicsPathItem(path)
        rect.setBrush(QBrush(QColor(color)))
        rect.setPen(QPen(QColor("#0b0b0b"), 1))
        rect.setToolTip(
            f"{seg_start.strftime('%Y-%m-%d')} 〜 {seg_end.strftime('%Y-%m-%d')}\n同時 {value} 本"
        )
        scene.addItem(rect)

    for seg_start, seg_end, cap in cap_segments:
        if cap == _UNLIMITED_LINES:
            continue
        x1, x2 = x_of(seg_start), x_of(seg_end)
        y = chart_bottom - cap * ROW_UNIT_HEIGHT
        line = _grid_line(
            x1, y, x2, y, _CAPACITY_LINE_COLOR, dashed=True, width=2, z_value=_CAPACITY_LINE_Z,
        )
        line.setToolTip(
            f"上限 {cap} 本（{seg_start.strftime('%Y-%m-%d')} 〜 {seg_end.strftime('%Y-%m-%d')}）"
        )
        scene.addItem(line)

    scene.team_summary_max_value = max_value
    return scene


class TeamSummaryChartView(GanttGraphicsView):
    """`GanttGraphicsView`（ホイールズーム・中ボタンパン・A/Fキー）をそのまま
    再利用し、フィット処理だけを自己完結で追加する（旧
    `gui/resource_histogram.ResourceHistogramView`と同じ考え方。バーは個別
    選択できる仕様にしていないため、fit_selectedはfit_allと同じ挙動にする）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fitAllRequested.connect(self.fit_all)
        self.fitSelectedRequested.connect(self.fit_all)

    def fit_all(self):
        if self.scene() is None:
            return
        rect = self.scene().itemsBoundingRect()
        if rect.isEmpty():
            return
        margin = 16
        rect = rect.adjusted(-margin, -margin, margin, margin)
        self.fitInView(rect, Qt.IgnoreAspectRatio)
