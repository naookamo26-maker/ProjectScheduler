"""
タブ1「基本情報設定」のリソースヒストグラム用の計算・描画部品。

現時点ではチームの「計画上の同時ライン数」（`teams.max_lines` +
`team_capacity_changes`）の推移だけを表示する。実際のタスクスケジューリング
結果（リソース使用状況）を表示する用途にも将来使い回せるよう、計算部分は
2段階に分けてある。

- `compute_step_segments`: 純粋な階段関数の区間化。「日付→値」の変化点の
  リストから、値が変わるたびに区切った連続区間を作るだけの汎用関数で、
  チームの計画容量に限らず、将来ガントチャートタブで実際のタスク使用数
  （日ごとの稼働タスク数）を表示したくなった場合もそのまま使い回せる。
- `team_capacity_breakpoints`: チームの`max_lines`と`team_capacity_changes`
  から、上記の汎用関数に渡す「変化点」リストを作る、チーム容量専用の
  データ整形関数。

描画（`build_histogram_scene`）は `gui/gantt_view.py` と同じ、素の
QGraphicsScene直接描画方式を踏襲する（軸・目盛りラベルの考え方も同様）。
ズーム・パン・A/Fキーでのフィットは `gui/gantt_view.py` の
`GanttGraphicsView` をそのまま再利用し、フィット処理だけ
`gui/node_canvas.py` の `WorkflowGraphView.fit_all/fit_selected` と同じ
自己完結パターンで `ResourceHistogramView` に追加する。
"""

from datetime import date, timedelta

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsSimpleTextItem

from gui.gantt_view import GanttGraphicsView

DAY_WIDTH = 6
ROW_UNIT_HEIGHT = 22
LEFT_MARGIN = 50
TOP_MARGIN = 36
# 最も高いバーの上端と目盛りエリア上端(TOP_MARGIN)が接してしまうと窮屈な
# 見た目になるため、最大値の上にさらに1行分の空白（見出し・ラベル用の
# 余白ではなく、純粋な視覚的な余白）を確保する。
TOP_PADDING_ROWS = 1
# 上余白（TOP_MARGIN）の実効値の下限をバー全体の高さに対する割合で決める。
# 詳細は _compute_top_margin 参照。
TOP_MARGIN_MIN_FRACTION = 0.12
_GRID_COLOR = QColor("#e1e0d9")
_AXIS_TEXT_COLOR = QColor("#52514e")
_MILESTONE_COLOR = QColor("#c0392b")
_PROJECT_START_COLOR = QColor("#52514e")
_PANE_BG = QColor("#fdfcf9")
_MILESTONE_LINE_Z = 10  # バー（既定のzValue=0）より前面に描画する
# マイルストーン・開発開始日のラベルと、その縦線の描き始めのY座標。
# top_marginの下端からの固定オフセットではなく、シーンの最上部近くに固定
# することで、top_marginがデータ量に応じて拡大した際、ラベル・バー間の
# 間隔（実質的にtop_marginそのもの）もきちんと拡大されるようにする
# （_compute_top_margin参照）。
_LABEL_TOP = 4

_AXIS_MARGIN_DAYS = 3
# マイルストーン・変動点のどちらも無いプロジェクトでは表示範囲を決める材料が
# 無いため、開発開始日から一定期間だけを既定の表示窓とする。将来的な実績値
# 表示（タスクの実際のスケジュール結果）ではこのフォールバックは使われない
# 想定（結果があれば必ず日付範囲が定まるため）。
_DEFAULT_WINDOW_DAYS = 90


# -- 純粋関数（Qt非依存） -----------------------------------------------------------

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


def team_capacity_breakpoints(team, capacity_changes, project_start_date):
    """team: {"max_lines": int, ...}（gui/db.pyのlist_teams()の1件）。
    capacity_changes: db.list_team_capacity_changes(team_id)の戻り値。
    project_start_date: ISO日付文字列（開発開始日）。
    Returns: compute_step_segments にそのまま渡せる [(date, value), ...]。"""
    points = [(date.fromisoformat(project_start_date), team["max_lines"])]
    for c in capacity_changes:
        points.append((date.fromisoformat(c["start_date"]), c["lines"]))
    return sorted(points)


def histogram_axis_range(project_start_date, milestones, all_capacity_changes):
    """表示するX軸範囲 (range_start, range_end) を決める。

    range_start は開発開始日そのもの。range_end は、既知のマイルストーン
    締切日・チームの容量変更点のうち最も遅い日付（それらが1件も無い場合は
    開発開始日 + 90日）に、見た目の余白（_AXIS_MARGIN_DAYS）を足したもの。
    これは「タスクの実スケジュール結果が無い段階では、表示すべき期間の
    長さを確定させる材料が無い」ための割り切りであり、ユーザー設定はしない
    （詳細はdocs/architecture.md参照）。"""
    start = date.fromisoformat(project_start_date)
    candidates = [date.fromisoformat(m["end_date"]) for m in milestones]
    candidates += [date.fromisoformat(c["start_date"]) for c in all_capacity_changes]
    end = max(candidates) if candidates else start + timedelta(days=_DEFAULT_WINDOW_DAYS)
    end = max(end, start)
    return start, end + timedelta(days=_AXIS_MARGIN_DAYS)


def value_at(breakpoints, d):
    """breakpoints: [(date, value), ...]。日付d時点で有効な値（dより後の
    変化点しか無ければ0）。積み上げ描画（build_histogram_scene）が、
    チームごとに異なる変化点の集合を共通の区切りへ揃えて評価するために使う。"""
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
    集めた、共通の区切り日付リスト（range_start, range_endを含む昇順）を返す。"""
    dates = {range_start, range_end}
    for bps in breakpoints_by_key.values():
        for d, _v in bps:
            if range_start < d <= range_end:
                dates.add(d)
    return sorted(dates)


# -- 描画（QGraphicsScene） ---------------------------------------------------------

def _add_label(scene, text, font, pos, brush=None):
    label = QGraphicsSimpleTextItem(text)
    label.setFont(font)
    if brush is not None:
        label.setBrush(brush)
    label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
    label.setPos(*pos)
    scene.addItem(label)
    return label


def _tick_step_days(total_days):
    if total_days > 365:
        return 28
    if total_days > 120:
        return 14
    return 7


def _compute_top_margin(bar_height_range):
    """バー全体の高さ（TOP_PADDING_ROWS込み）から、上余白（シーン座標）を
    求める純粋関数。TOP_MARGINを固定値のまま使うと、fit_all()が縦方向を
    大きく縮小する（合計値が大きい）プロジェクトで、縮小されないマイル
    ストーンラベル（ItemIgnoresTransformations）が実際の画面上では
    バーと重なって見えてしまう。上余白をバー全体の高さの一定割合
    （TOP_MARGIN_MIN_FRACTION）を下限として確保することで、fit_all()後の
    表示倍率によらず、実際の画面上の余白がビューポート高さに対して
    概ね一定の割合を保つようにする（縮小前のシーン座標での比率を保てば、
    縮小後の実ピクセルでの比率も保たれるため）。"""
    return max(TOP_MARGIN, bar_height_range * TOP_MARGIN_MIN_FRACTION)


def build_histogram_scene(segments_by_key, mode, color_map, labels_by_key,
                           milestone_markers, project_start, range_start, range_end):
    """segments_by_key: {key: [(date, value), ...]}（"single"モードは1件だけ、
    "stacked"モードは複数）——値は team_capacity_breakpoints の生の変化点
    リストをそのまま渡す（区間化はこの関数の中で行う）。
    mode: "single"（1系列を単純な階段ヒストグラムで表示）または
    "stacked"（複数系列を積み上げ、合計と各系列の比率を同時に見せる）。
    color_map / labels_by_key: {key: 色 / 表示名}。labels_by_key の表示名は、
    バー内（系列ごとに最も横幅が広い区間の中央）にも表示され、ツールチップに
    頼らなくてもどの色がどのチームかが分かるようにする。
    milestone_markers: [(id, label, date), ...]（プロジェクト開始日を含めない。
    開始日は project_start で別途受け取り、専用の縦線として描く）。

    戻り値のQGraphicsSceneには、テスト・fit_all()から参照できるよう
    `histogram_mode`（"single"/"stacked"/"empty"）と `histogram_max_value`
    （Y軸のスケール基準にした最大値）を属性として持たせる
    （gui/gantt_view.py が gantt_body_rect 等をシーンに持たせる慣習を踏襲）。"""
    scene = QGraphicsScene()
    total_days = max((range_end - range_start).days, 1)

    def x_of(d):
        return LEFT_MARGIN + (d - range_start).days * DAY_WIDTH

    axis_font = QFont()
    axis_font.setPointSize(8)
    year_font = QFont(axis_font)
    year_font.setBold(True)

    if mode == "stacked":
        max_value = 0
        boundaries = shared_boundaries(segments_by_key, range_start, range_end)
        band_items = []
        for i in range(len(boundaries) - 1):
            seg_start, seg_end = boundaries[i], boundaries[i + 1]
            bottom = 0
            for key, breakpoints in segments_by_key.items():
                value = value_at(breakpoints, seg_start)
                if value > 0:
                    band_items.append((seg_start, seg_end, bottom, bottom + value, key))
                bottom += value
            max_value = max(max_value, bottom)
    else:
        key, breakpoints = next(iter(segments_by_key.items()), (None, []))
        segments = compute_step_segments(breakpoints, range_start, range_end)
        max_value = max((v for _s, _e, v in segments), default=0)
        band_items = [(s, e, 0, v, key) for s, e, v in segments]

    bar_height_range = (max(max_value, 1) + TOP_PADDING_ROWS) * ROW_UNIT_HEIGHT
    top_margin = _compute_top_margin(bar_height_range)
    chart_bottom = top_margin + bar_height_range
    chart_right = x_of(range_end)

    widest_band_by_key = {}
    for seg_start, seg_end, lo, hi, key in band_items:
        x1, x2 = x_of(seg_start), x_of(seg_end)
        y_top = chart_bottom - hi * ROW_UNIT_HEIGHT
        y_bottom = chart_bottom - lo * ROW_UNIT_HEIGHT
        path = QPainterPath()
        path.addRect(QRectF(x1, y_top, max(x2 - x1, 1), y_bottom - y_top))
        rect = QGraphicsPathItem(path)
        rect.setBrush(QBrush(QColor(color_map.get(key, "#cbc9c2"))))
        rect.setPen(QPen(QColor("#0b0b0b"), 1))
        label = labels_by_key.get(key, "")
        rect.setToolTip(f"{label}\n{seg_start.isoformat()} 〜 {seg_end.isoformat()}\n{hi - lo}ライン")
        scene.addItem(rect)

        # チーム名ラベルは、その系列の中で最も横幅が広い区間（＝最もラベルが
        # 収まりやすい区間）の中央に1つだけ表示する（区間ごとに表示すると、
        # 短い区間が並ぶ場合に文字が重なって読めなくなるため）。
        width = (seg_end - seg_start).days
        current = widest_band_by_key.get(key)
        if current is None or width > current[0]:
            widest_band_by_key[key] = (width, x1, x2, y_top, y_bottom)

    # -- チーム名ラベル（バー内） --------------------------------------------------
    team_label_font = QFont(axis_font)
    team_label_font.setBold(True)
    label_metrics = QFontMetrics(team_label_font)
    for key, (_width, x1, x2, y_top, y_bottom) in widest_band_by_key.items():
        text = labels_by_key.get(key, "")
        if not text:
            continue
        text_width = label_metrics.horizontalAdvance(text)
        cx = (x1 + x2) / 2
        cy = (y_top + y_bottom) / 2
        _add_label(
            scene, text, team_label_font,
            (cx - text_width / 2, cy - label_metrics.height() / 2),
            QBrush(QColor("#0b0b0b")),
        )

    # -- Y軸（ライン数の目盛り） --------------------------------------------------
    y_step = max(1, round(max_value / 5)) if max_value > 5 else 1
    y = 0
    while y <= max_value:
        gy = chart_bottom - y * ROW_UNIT_HEIGHT
        scene.addItem(_grid_line(LEFT_MARGIN, gy, chart_right, gy))
        _add_label(scene, str(y), axis_font, (4, gy - 7), QBrush(_AXIS_TEXT_COLOR))
        y += y_step

    # -- X軸（日付目盛り） ---------------------------------------------------------
    step = _tick_step_days(total_days)
    tick_date = range_start + timedelta(days=(7 - range_start.weekday()) % 7)
    while tick_date <= range_end:
        x = x_of(tick_date)
        scene.addItem(_grid_line(x, top_margin, x, chart_bottom))
        _add_label(scene, tick_date.strftime("%m/%d"), axis_font, (x - 14, chart_bottom + 4))
        tick_date += timedelta(days=step)

    # -- マイルストーン・開発開始日の縦線 -------------------------------------------
    # gui/gantt_view.py のマイルストーン線（幅2のダッシュ線）と同じ見た目にし、
    # バー（既定のzValue=0）より前面（_MILESTONE_LINE_Z）に描画することで、
    # バーに隠れず常に視認できるようにする。
    # ラベルは、上余白（top_margin）の下端から一定オフセットではなく、シーンの
    # 最上部近く（_LABEL_TOP）に固定で置く。top_marginが大きく育っても、
    # ラベルがバー側に張り付いたままでは意味が無く（top_marginがどれだけ
    # 大きくなっても、ラベルからバーまでの実際の間隔＝シーン座標の差は
    # 「top_margin - (top_margin - 14)」＝14で常に一定になってしまう）、
    # ラベル・バー間の間隔がtop_marginの拡大にきちんと連動するようにする。
    # 縦線もラベルに合わせてこの高さから描き始める（gui/gantt_view.pyの
    # マイルストーン線がヘッダー領域まで伸びているのと同じ考え方）。
    milestone_font = QFont(axis_font)
    milestone_font.setBold(True)
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
    scene.histogram_mode = mode if band_items else "empty"
    scene.histogram_max_value = max_value
    return scene


def _grid_line(x1, y1, x2, y2, color=None, dashed=False, width=1, z_value=-1):
    pen = QPen(color or _GRID_COLOR, width, Qt.DashLine if dashed else Qt.SolidLine)
    line = QGraphicsPathItem()
    path = QPainterPath(QPointF(x1, y1))
    path.lineTo(QPointF(x2, y2))
    line.setPath(path)
    line.setPen(pen)
    line.setZValue(z_value)
    return line


class ResourceHistogramView(GanttGraphicsView):
    """`gui/gantt_view.py` の `GanttGraphicsView`（ホイールズーム・中ボタン
    パン・A/Fキーのシグナル発火）をそのまま再利用し、フィット処理だけを
    自己完結で追加する（`gui/node_canvas.py` の `WorkflowGraphView.fit_all`/
    `fit_selected` と同じ考え方。ヒストグラムのバーは個別選択できる仕様に
    していないため、`fit_selected` は `fit_all` と同じ挙動にする）。"""

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
