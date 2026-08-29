"""
プロジェクト分析タブ「チーム別サマリー」の描画（QGraphicsScene直接描画）。

`gui/gantt_view.py`・旧`gui/resource_histogram.py`のヒストグラム描画と同じ、
素のQGraphicsScene直接描画方式を踏襲する（軸・目盛りラベルの考え方も同様。
docs/project_analysis_tab_design.md §5参照）。ズーム・パン・A/Fキーでの
フィットは`gui/gantt_view.py`の`GanttGraphicsView`をそのまま再利用し
（旧`ResourceHistogramView`と同じ考え方）、フィット処理だけ
`TeamSummaryChartView`に自己完結で追加する。

**X軸は週次**（1点＝1週）。日次の変化点をそのまま描くと営業日数ぶんの
ギザギザになって読めないため、`gui/summary_metrics` 側で週次へ集計してから、
点と点を直線で結ぶ。月次だと数か月しかない短いプロジェクトでは点が数個しか
並ばず形が読めないので、週を単位にしている。描画する点の数は週数
（サンプルの大規模プロジェクトで約130週）に収まるので、タスク数が増えても
描画コストは変わらない。

集計は用途で使い分ける。

- 全体グラフ（`build_team_stacked_scene`）: `weekly_peak_breakdown_by_team()`
  ——週ごとの合計が実在した同時タスク数になる内訳。積み上げるため、
  合計が意味を持つ必要がある。
- 詳細グラフ（`build_team_detail_scene`）: `weekly_concurrency_by_team()`
  ——そのチーム単独の週内最大。自チームの上限と突き合わせる図なので、
  他チームの都合で選んだ日の値ではなく自分のピークを見せる。
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsSimpleTextItem

from gui.gantt_view import GanttGraphicsView

# 1週あたりの横幅と、値域（0〜最大値）に割り当てる縦幅（いずれもシーン座標）。
# 縦は「1本＝固定px」ではなく最大値に対する比率でスケールする——チームによって
# ピークの桁が違う（サンプルでは5本〜23本）ため、固定の行高だとピークの小さい
# チームの線が軸に張り付いて読めなくなる。
WEEK_WIDTH = 9
CHART_HEIGHT = 230
LEFT_MARGIN = 52
TOP_MARGIN = 18
BOTTOM_MARGIN = 26

_GRID_COLOR = QColor("#e1e0d9")
_AXIS_TEXT_COLOR = QColor("#52514e")
_MILESTONE_COLOR = QColor("#c0392b")
_PROJECT_START_COLOR = QColor("#52514e")
_PANE_BG = QColor("#fdfcf9")
_MILESTONE_LINE_Z = 10
_LABEL_TOP = 4
_CAPACITY_LINE_COLOR = QColor("#0b0b0b")
_CAPACITY_LINE_Z = 5
# 設定上限の破線の太さ。設計案モックアップの stroke-width:1.5 / dasharray:"4 3"
# に近い見た目にする（Qtの DashLine はダッシュ長を線幅の倍数で決めるため、
# 太くするほどダッシュも粗くなる——細めにして破線らしさを出す）。
_CAPACITY_LINE_WIDTH = 1.5
_PEAK_MARKER_COLOR = QColor("#52514e")
_LINE_WIDTH = 2
_DETAIL_FILL_ALPHA = 150
# 積み上げた帯の区切り線（背景色の細い線）。隣り合う帯の色が近くても境目が分かる。
# 週次は点の間隔が狭いので、月次のときより細くしないと帯が線で埋まってしまう。
_BAND_SEPARATOR_WIDTH = 0.6

# X軸ラベルの最大本数。ラベルはItemIgnoresTransformationsで画面上一定サイズの
# ため、フィットで縮小されるほど間隔が詰まって重なる。週の数だけ出すことは
# できないので、この本数に収まるよう間引く。
_MAX_AXIS_LABELS = 12


def _y_tick_step(max_value):
    """Y軸の目盛り間隔（グリッド線がおよそ5本になるように選ぶ）。"""
    if max_value <= 5:
        return 1
    raw = max_value / 5
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if raw <= step:
            return step
    return 2000


def _axis_labels(weeks):
    """X軸に出す [(週インデックス, ラベル文字列), ...]。

    月の変わり目の週に「YYYY/MM」を置くのを基本にする（週次の点は多いので
    全部にラベルは出せず、月の頭が目印として一番読みやすい）。月の変わり目が
    3つ以下しか無い短いプロジェクトでは月の頭だけでは目印が足りないので、
    一定間隔で「MM/DD」を入れる。いずれも `_MAX_AXIS_LABELS` 本に収まるよう
    間引く。"""
    if not weeks:
        return []
    month_starts = [
        i for i, week in enumerate(weeks)
        if i == 0 or week.month != weeks[i - 1].month
    ]
    if len(month_starts) >= 4:
        stride = max(1, math.ceil(len(month_starts) / _MAX_AXIS_LABELS))
        return [(i, weeks[i].strftime("%Y/%m")) for i in month_starts[::stride]]
    stride = max(1, math.ceil(len(weeks) / _MAX_AXIS_LABELS))
    return [(i, weeks[i].strftime("%m/%d")) for i in range(0, len(weeks), stride)]


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


class _WeekAxis:
    """週次X軸と、値→Y座標の対応。weeks は各週の月曜（`pd.Timestamp`）の昇順
    リスト（`gui/summary_metrics.week_starts()` の戻り値）。"""

    def __init__(self, weeks, max_value):
        self.weeks = weeks
        self.max_value = max(max_value, 1)
        self._first_monday = weeks[0] if weeks else None
        self.chart_top = TOP_MARGIN
        self.chart_bottom = TOP_MARGIN + CHART_HEIGHT
        self.chart_right = LEFT_MARGIN + max(len(weeks) - 1, 1) * WEEK_WIDTH

    def x(self, week_index):
        return LEFT_MARGIN + week_index * WEEK_WIDTH

    def y(self, value):
        return self.chart_bottom - value / self.max_value * CHART_HEIGHT

    def x_of_date(self, ts):
        """日付を週インデックス（＋週内の曜日ぶんの端数）でX座標にする
        （マイルストーンの縦線を週の途中に正しく置くため）。範囲外なら None。"""
        if self._first_monday is None:
            return None
        offset_days = (ts.normalize() - self._first_monday).days
        if offset_days < 0 or offset_days > (len(self.weeks) - 1) * 7 + 6:
            return None
        return self.x(offset_days / 7)


def _new_scene_with_axes(weeks, max_value, milestone_markers, project_start):
    """週次の軸・グリッド線・マイルストーン線だけを描いた空のシーンと、
    その座標変換（_WeekAxis）を返す。呼び出し側はこの上に線・面を追加する。"""
    scene = QGraphicsScene()
    axis = _WeekAxis(weeks, max_value)

    axis_font = QFont()
    axis_font.setPointSize(8)

    # Y軸（同時タスク数の目盛り）
    step = _y_tick_step(axis.max_value)
    value = 0
    while value <= axis.max_value:
        y = axis.y(value)
        scene.addItem(_grid_line(LEFT_MARGIN, y, axis.chart_right, y))
        _add_label(scene, f"{value:,}", axis_font, (4, y - 7), QBrush(_AXIS_TEXT_COLOR))
        value += step

    # X軸（週ラベル）。ラベルを出す週にだけ縦のグリッド線も添える。
    for i, text in _axis_labels(weeks):
        x = axis.x(i)
        scene.addItem(_grid_line(x, axis.chart_top, x, axis.chart_bottom))
        _add_label(
            scene, text, axis_font, (x - 16, axis.chart_bottom + 4), QBrush(_AXIS_TEXT_COLOR),
        )

    # 開発開始日・マイルストーンの縦線（gui/gantt_view.py と同じ見た目）
    milestone_font = QFont(axis_font)
    milestone_font.setBold(True)
    if project_start is not None:
        x = axis.x_of_date(project_start)
        if x is not None:
            scene.addItem(_grid_line(
                x, _LABEL_TOP, x, axis.chart_bottom, _PROJECT_START_COLOR,
                dashed=True, width=2, z_value=_MILESTONE_LINE_Z,
            ))
    for _mid, mlabel, mdate in milestone_markers:
        x = axis.x_of_date(mdate)
        if x is None:
            continue
        scene.addItem(_grid_line(
            x, _LABEL_TOP, x, axis.chart_bottom, _MILESTONE_COLOR,
            dashed=True, width=2, z_value=_MILESTONE_LINE_Z,
        ))
        _add_label(scene, mlabel, milestone_font, (x + 2, _LABEL_TOP), QBrush(_MILESTONE_COLOR))

    scene.setBackgroundBrush(QBrush(_PANE_BG))
    return scene, axis


def _merge_runs(values):
    """[値, ...] を、同じ値が連続する区間 [(開始index, 終了index, 値), ...] に
    まとめる（None の要素は区間を作らず、そこで区間を切る）。"""
    runs = []
    start = None
    for i, value in enumerate(values):
        if start is not None and (value is None or values[start] != value):
            runs.append((start, i - 1, values[start]))
            start = None
        if value is not None and start is None:
            start = i
    if start is not None:
        runs.append((start, len(values) - 1, values[start]))
    return runs


def _hit_area(axis, week_index, tooltip):
    """1週ぶんの透明な当たり判定（その週の値をツールチップで読ませるため）。"""
    item = QGraphicsPathItem()
    path = QPainterPath()
    path.addRect(QRectF(
        axis.x(week_index) - WEEK_WIDTH / 2, axis.chart_top, WEEK_WIDTH, CHART_HEIGHT,
    ))
    item.setPath(path)
    item.setPen(QPen(Qt.NoPen))
    item.setBrush(QBrush(Qt.transparent))
    item.setToolTip(tooltip)
    return item


def _polyline_path(axis, values):
    """週次の値の列を、点と点を直線で結んだパスにする（階段状にはしない
    ——週次に集計済みで1点＝1週のため、階段にすると本来無い「週末に急に
    変わる」段差を描いてしまう）。"""
    path = QPainterPath(QPointF(axis.x(0), axis.y(values[0])))
    for i, value in enumerate(values[1:], start=1):
        path.lineTo(axis.x(i), axis.y(value))
    return path


def _fmt_week(week_start):
    return week_start.strftime("%Y/%m/%d") + " の週"


def build_team_stacked_scene(weeks, breakdown_by_team, totals, color_map, labels_by_team,
                              milestone_markers, project_start):
    """全チームの同時タスク数を積み上げた面グラフ（週次・チーム色で塗り分け）。

    breakdown_by_team / totals: `gui/summary_metrics.weekly_peak_breakdown_by_team()`
    の戻り値。**週ごとの合計が実在した同時タスク数になる**内訳を積む
    （チームごとの週内最大を単純に足すと、実際には同時に起きていない高さの山に
    なってしまう。設計案§3参照）。
    color_map/labels_by_team: `gui/gantt_generator.build_display()` の
    team_colors/team_names をそのまま渡せる。

    全チーム合算のピークには縦の目印と件数ラベルを添える。totals の最大値が
    そのままプロジェクト全体のピーク（＝KPIタイルの「同時タスク数のピーク」）
    なので、別途受け取らずここで求める。"""
    if not weeks:
        return QGraphicsScene()

    max_value = max(totals, default=0)
    scene, axis = _new_scene_with_axes(weeks, max_value, milestone_markers, project_start)

    # チーム名は帯の中に描き込まない。帯が薄い区間では文字が帯からはみ出して
    # 重なり、フィットで縮小されるほど読めなくなるため、どの色がどのチームかは
    # 呼び出し側（gui/tab_analysis.py）がチャートの外に置く凡例と、帯の
    # ツールチップで補う（docs/project_analysis_tab_design.md §7-6）。
    #
    # 積む順は逆順（最後のチームが一番下、最初のチームが一番上）。凡例の
    # 並び順と積み上げの上下が一致し、凡例の先頭＝グラフの一番上になる。
    base = [0] * len(weeks)
    for team_id in reversed(list(breakdown_by_team)):
        values = breakdown_by_team[team_id]
        if not values:
            continue
        if max(values) <= 0:
            continue  # 値が全て0なのでbaseは変わらない
        top = [b + v for b, v in zip(base, values)]
        # 上端を左→右、下端を右→左に辿って帯を閉じる。
        path = QPainterPath(QPointF(axis.x(0), axis.y(top[0])))
        for i, value in enumerate(top[1:], start=1):
            path.lineTo(axis.x(i), axis.y(value))
        for i in range(len(weeks) - 1, -1, -1):
            path.lineTo(axis.x(i), axis.y(base[i]))
        path.closeSubpath()

        band = QGraphicsPathItem(path)
        band.setBrush(QBrush(QColor(color_map.get(team_id, "#cbc9c2"))))
        # 区切りは背景色の細い線。隣り合う帯の色が近くても境目が分かる。
        band.setPen(QPen(_PANE_BG, _BAND_SEPARATOR_WIDTH))
        name = labels_by_team.get(team_id, team_id)
        band.setToolTip(f"{name}\nピーク {max(values)} 本")
        scene.addItem(band)
        base = top

    # 週ごとの合計を出す透明な当たり判定（帯のツールチップはチーム単位のため、
    # 「その週に全体で何本走っているか」はこちらで読ませる）。
    for i, week_start in enumerate(weeks):
        scene.addItem(_hit_area(
            axis, i, f"{_fmt_week(week_start)}\n同時タスク数 合計 {totals[i]:,} 本",
        ))

    if max_value > 0:
        peak_x = axis.x(totals.index(max_value))
        scene.addItem(_grid_line(
            peak_x, axis.chart_top, peak_x, axis.chart_bottom, _PEAK_MARKER_COLOR,
            dashed=True, width=1, z_value=_MILESTONE_LINE_Z,
        ))
        peak_font = QFont()
        peak_font.setPointSize(8)
        peak_font.setBold(True)
        _add_label(
            scene, f"全チーム合計のピーク {max_value:,} 本", peak_font,
            (peak_x + 3, axis.chart_top + 2), QBrush(_PEAK_MARKER_COLOR),
        )

    scene.team_summary_max_value = max_value
    return scene


def build_team_detail_scene(weeks, values, capacity_values, color,
                             milestone_markers, project_start):
    """選択した1チームの同時タスク数（塗り＋折れ線）と設定上限（破線）。週次。

    values: `weekly_concurrency_by_team()` の該当チームぶん。
    capacity_values: `gui/summary_metrics.weekly_capacity()` の戻り値
    （上限「指定なし」の週は None）。Noneの週には破線を描かない——線が無い
    こと自体が「まだ人数を決めていない」ことの表示になる
    （docs/project_analysis_tab_design.md §2-3）。"""
    if not weeks or not values:
        return QGraphicsScene()

    finite_caps = [cap for cap in capacity_values if cap is not None]
    max_value = max(values + finite_caps + [0])
    scene, axis = _new_scene_with_axes(weeks, max_value, milestone_markers, project_start)

    # 塗り（折れ線の下）＋その上に折れ線本体、の2重ね。
    area_path = _polyline_path(axis, values)
    area_path.lineTo(axis.x(len(values) - 1), axis.chart_bottom)
    area_path.lineTo(axis.x(0), axis.chart_bottom)
    area_path.closeSubpath()
    fill_color = QColor(color)
    fill_color.setAlpha(_DETAIL_FILL_ALPHA)
    area_item = QGraphicsPathItem(area_path)
    area_item.setBrush(QBrush(fill_color))
    area_item.setPen(QPen(Qt.NoPen))
    scene.addItem(area_item)

    line_item = QGraphicsPathItem(_polyline_path(axis, values))
    line_item.setPen(QPen(QColor(color), _LINE_WIDTH))
    scene.addItem(line_item)

    # 各週に、その週の値（と上限）を出す透明な当たり判定を重ねる。
    for i, week_start in enumerate(weeks):
        cap = capacity_values[i] if i < len(capacity_values) else None
        cap_text = f"\n上限 {cap} 本" if cap is not None else "\n上限 指定なし"
        scene.addItem(_hit_area(
            axis, i, f"{_fmt_week(week_start)}\n同時 {values[i]} 本{cap_text}",
        ))

    # 設定上限の破線（階段状の水平線）。
    #
    # 同じ値が続く週は**1本の長い線にまとめてから**描く。週ごとに
    # WEEK_WIDTH(=9px)の細切れで描くと、Qtの DashLine はダッシュ長を線幅の
    # 倍数で決めるため1ダッシュも入りきらず、実線にしか見えなくなる。
    for start, end, cap in _merge_runs(capacity_values):
        y = axis.y(cap)
        scene.addItem(_grid_line(
            axis.x(start) - WEEK_WIDTH / 2, y, axis.x(end) + WEEK_WIDTH / 2, y,
            _CAPACITY_LINE_COLOR, dashed=True, width=_CAPACITY_LINE_WIDTH,
            z_value=_CAPACITY_LINE_Z,
        ))

    scene.team_summary_max_value = max_value
    return scene


class TeamSummaryChartView(GanttGraphicsView):
    """`GanttGraphicsView`（ホイールズーム・中ボタンパン・A/Fキー）をそのまま
    再利用し、フィット処理だけを自己完結で追加する（旧
    `gui/resource_histogram.ResourceHistogramView`と同じ考え方。線は個別
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
