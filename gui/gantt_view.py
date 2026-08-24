"""
「ガントチャート」タブ（gui/tab_gantt.py）用の独自描画部品（段階2〜3）。

HTMLに頼らず、gui/node_canvas.py と同じQGraphicsView/QGraphicsSceneベースで
ツール内に直接バーチャートを描画する。データは gui/gantt_generator.py の
compute_schedule() が返す result_df（1ワークフロー分に絞り込み済み）と
display（チーム色・表示名・マイルストーン一覧）をそのまま使う。

レーン詰め（同じジョブ内で時間的に重ならないタスクは同じ行にまとめる）は
project_scheduler.py の export_plotly_gantt が持つ考え方を踏襲している。

段階3: 日付ヘッダー／マイルストーン行と、左の項目名列を、本体の拡縮・パン
操作から見切れないよう画面上に固定表示する（表計算ソフトの「ウィンドウ枠の
固定」と同じ考え方）。ヘッダー・左列・本体は同じ QGraphicsScene を共有する
別々の QGraphicsView で、本体の変換／スクロール位置の変化に追従して
ヘッダーは横方向、左列は縦方向のみ同期する（交差方向の縮尺は常に1.0固定）。
"""

from datetime import timedelta

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPen, QTransform
from PySide6.QtWidgets import (
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGridLayout,
    QWidget,
)

DAY_WIDTH = 10
ROW_HEIGHT = 26
BAR_MARGIN = 3
LEFT_MARGIN = 190
TOP_MARGIN = 46
JOB_GAP = 6
AXIS_MARGIN_DAYS = 3
# ヘッダー／左列ペインの表示専用の余白（罫線がペイン端で見切れないように）。
_PANE_PADDING = 10

_ADJUSTED_BORDER = QColor("#c0392b")
_GRID_COLOR = QColor("#e1e0d9")
_MILESTONE_COLOR = QColor("#c0392b")
_PROJECT_START_COLOR = QColor("#52514e")
_DEFAULT_BAR_COLOR = "#898781"
_PANE_BG = QColor("#fdfcf9")


class GanttGraphicsView(QGraphicsView):
    """本体ペイン。ホイールでズーム、中ボタンドラッグでパン
    （gui/node_canvas.py の WorkflowGraphView と同じ操作感）。ガントチャートは
    時間軸（横）と行数（縦）の縮尺を別々に調整したいことが多いため、
    Ctrlを押しながらのホイールで横方向のみ、Shiftを押しながらのホイールで
    縦方向のみ、修飾キーなしなら従来通り両方向を拡縮する。

    変換／スクロール位置が変わるたびに transformChanged を発火し、
    FrozenGanttPane がヘッダー・左列ペインを追従させる。"""

    transformChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(self.renderHints())
        # 文字色は常に黒固定（QGraphicsSimpleTextItemの既定）で描画しているため、
        # OSがダークモードだと既定の（ダークな）ビュー背景に文字が埋もれて
        # 読めなくなる。この独自キャンバスはOSのテーマに関わらず常に明るい
        # 背景で描くようにし、文字色との組み合わせを固定して視認性を保つ。
        self.setBackgroundBrush(QBrush(_PANE_BG))
        self._panning = False
        self._pan_last_pos = None
        h_bar = self.horizontalScrollBar()
        v_bar = self.verticalScrollBar()
        h_bar.valueChanged.connect(self.transformChanged)
        v_bar.valueChanged.connect(self.transformChanged)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        modifiers = event.modifiers()
        if modifiers & Qt.ControlModifier:
            self.scale(factor, 1.0)
        elif modifiers & Qt.ShiftModifier:
            self.scale(1.0, factor)
        else:
            self.scale(factor, factor)
        self.transformChanged.emit()

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_last_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.pos() - self._pan_last_pos
            self._pan_last_pos = event.pos()
            h_bar = self.horizontalScrollBar()
            v_bar = self.verticalScrollBar()
            h_bar.setValue(h_bar.value() - delta.x())
            v_bar.setValue(v_bar.value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self._pan_last_pos = None
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _FrozenPaneView(QGraphicsView):
    """ヘッダー／左列ペイン共通の基底クラス。表示専用（ユーザー操作は
    受け付けず、本体ペインの変換／スクロールに追従するだけ）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(self.renderHints())
        self.setBackgroundBrush(QBrush(_PANE_BG))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setInteractive(False)
        self.setFrameShape(QGraphicsView.NoFrame)


class GanttHeaderView(_FrozenPaneView):
    """日付軸・マイルストーンの行。本体と横方向の縮尺／スクロール位置だけ
    同期し、縦方向は常に等倍で固定表示する。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(TOP_MARGIN + _PANE_PADDING)


class GanttColumnView(_FrozenPaneView):
    """項目名（ジョブ名）の列。本体と縦方向の縮尺／スクロール位置だけ同期し、
    横方向は常に等倍で固定表示する。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(LEFT_MARGIN + _PANE_PADDING)


class FrozenGanttPane(QWidget):
    """コーナー／ヘッダー／左列／本体の4分割レイアウトをまとめて管理する
    コンポジットウィジェット。gui/tab_gantt.py からは本体ペインだけを
    直接使っていた旧 GanttGraphicsView の代わりにこれを配置する。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.body = GanttGraphicsView()
        self.header = GanttHeaderView()
        self.column = GanttColumnView()

        corner = QWidget()
        corner.setFixedSize(LEFT_MARGIN + _PANE_PADDING, TOP_MARGIN + _PANE_PADDING)
        corner.setAutoFillBackground(True)
        pal = corner.palette()
        pal.setColor(corner.backgroundRole(), _PANE_BG)
        corner.setPalette(pal)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
        grid.addWidget(corner, 0, 0)
        grid.addWidget(self.header, 0, 1)
        grid.addWidget(self.column, 1, 0)
        grid.addWidget(self.body, 1, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)

        self.body.transformChanged.connect(self._sync_panes)

    def setScene(self, scene):
        self.body.setScene(scene)
        self.header.setScene(scene)
        self.column.setScene(scene)
        if scene is None:
            return
        body_rect = getattr(scene, "gantt_body_rect", None)
        header_rect = getattr(scene, "gantt_header_rect", None)
        column_rect = getattr(scene, "gantt_column_rect", None)
        if body_rect is not None:
            self.body.setSceneRect(body_rect)
        if header_rect is not None:
            self.header.setSceneRect(header_rect)
        if column_rect is not None:
            self.column.setSceneRect(column_rect)
        self._sync_panes()

    def scene(self):
        return self.body.scene()

    def fit_all(self):
        """本体ペインの内容（ヘッダー行・左列を除いたチャート本体）が
        ちょうど収まるようにズームを合わせ、ヘッダー・左列ペインもそれに
        追従させる。gui/node_canvas.py の fit_all() と同じ考え方。"""
        scene = self.body.scene()
        if scene is None:
            return
        rect = getattr(scene, "gantt_body_rect", None)
        if rect is None or rect.isEmpty():
            return
        # ガントチャートは横（時間軸）と縦（行数）で必要な縮尺が大きく異なる
        # ことが多い。KeepAspectRatioだと縦横比を保つために片方が余ってしまう
        # ため、IgnoreAspectRatioで縦横それぞれ独立にビューいっぱいへ広げる。
        self.body.fitInView(rect, Qt.IgnoreAspectRatio)
        self._sync_panes()

    def _sync_panes(self):
        body_transform = self.body.transform()
        sx, sy = body_transform.m11(), body_transform.m22()

        header_transform = QTransform()
        header_transform.scale(sx, 1.0)
        self.header.setTransform(header_transform)
        self.header.horizontalScrollBar().setValue(self.body.horizontalScrollBar().value())

        column_transform = QTransform()
        column_transform.scale(1.0, sy)
        self.column.setTransform(column_transform)
        self.column.verticalScrollBar().setValue(self.body.verticalScrollBar().value())


def _pack_lanes(tasks):
    """tasks: 開始日昇順に並んだタスク（各要素は 'start'/'end' キーを持つ辞書）。
    時間的に重ならないタスクは同じレーンに詰め、各タスクにレーン番号（0始まり）を
    割り当てる。Returns: (レーン番号のリスト, 総レーン数)。"""
    lane_end_dates = []
    lanes = []
    for t in tasks:
        placed = False
        for lane_idx, end_date in enumerate(lane_end_dates):
            if t["start"] >= end_date:
                lane_end_dates[lane_idx] = t["end"]
                lanes.append(lane_idx)
                placed = True
                break
        if not placed:
            lane_end_dates.append(t["end"])
            lanes.append(len(lane_end_dates) - 1)
    return lanes, len(lane_end_dates)


def _elide_text(text, font, max_width):
    metrics = QFontMetrics(font)
    return metrics.elidedText(text, Qt.ElideRight, int(max_width))


def build_gantt_scene(df, display, color_by="team"):
    """df: result_df を表示対象（1ワークフロー分、または1チーム分）に絞り込んだ
    もの。display: compute_schedule()の2番目の戻り値。QGraphicsScene を組み立てて
    返す（絞り込んだ結果が空ならNoneを返す）。

    color_by: "team"（既定、ワークフロー別表示用——同じワークフロー内で担当
    チームを見分けたい）または "workflow"（チーム別表示用——1チームに
    絞り込まれている代わりに、どのワークフローの仕事かを見分けたい）。

    返すシーンには、FrozenGanttPane が本体／ヘッダー／左列の3ペインへ
    それぞれ setSceneRect するための矩形を gantt_body_rect /
    gantt_header_rect / gantt_column_rect 属性として持たせる。"""
    if df.empty:
        return None

    milestone_markers = display.get("milestone_markers") or []
    axis_start = min(df["Start_Date"].min(), *(m[2] for m in milestone_markers)) if milestone_markers \
        else df["Start_Date"].min()
    axis_end = max(df["End_Date"].max(), *(m[2] for m in milestone_markers)) if milestone_markers \
        else df["End_Date"].max()
    axis_start = axis_start - timedelta(days=AXIS_MARGIN_DAYS)
    axis_end = axis_end + timedelta(days=AXIS_MARGIN_DAYS)
    total_days = max((axis_end - axis_start).days, 1)

    def x_of(date):
        return LEFT_MARGIN + (date - axis_start).days * DAY_WIDTH

    scene = QGraphicsScene()
    task_font = QFont()
    task_font.setPointSize(9)
    job_font = QFont()
    job_font.setPointSize(9)
    job_font.setBold(True)

    # -- ジョブごとにレーン詰め、Y座標を決める --------------------------------------
    job_order = df.groupby("Job_ID")["Start_Date"].min().sort_values().index.tolist()
    y_cursor = TOP_MARGIN
    job_blocks = []  # (job_id, job_name, y_top, y_bottom, [(task_row, lane), ...])
    for job_id in job_order:
        job_rows = df[df["Job_ID"] == job_id].sort_values("Start_Date")
        tasks = [{"start": r["Start_Date"], "end": r["End_Date"]} for _, r in job_rows.iterrows()]
        lanes, lane_count = _pack_lanes(tasks)
        y_top = y_cursor
        y_bottom = y_top + lane_count * ROW_HEIGHT
        job_blocks.append((job_id, str(job_rows.iloc[0]["Job_Name"]), y_top, y_bottom,
                            list(zip(job_rows.to_dict("records"), lanes))))
        y_cursor = y_bottom + JOB_GAP

    chart_bottom = y_cursor
    chart_right = x_of(axis_end)

    # -- 日付軸（週単位の目盛り、期間が長い場合は間引く） -----------------------------
    tick_step_days = 7
    if total_days > 365:
        tick_step_days = 28
    elif total_days > 120:
        tick_step_days = 14

    tick_date = axis_start
    # 最初の目盛りを月曜に揃える
    tick_date = tick_date + timedelta(days=(7 - tick_date.weekday()) % 7)
    while tick_date <= axis_end:
        x = x_of(tick_date)
        line = scene.addLine(x, TOP_MARGIN - 10, x, chart_bottom, QPen(_GRID_COLOR, 1))
        line.setZValue(-2)
        label = QGraphicsSimpleTextItem(tick_date.strftime("%Y-%m-%d"))
        label.setPos(x + 2, TOP_MARGIN - 26)
        label.setFont(task_font)
        scene.addItem(label)
        tick_date += timedelta(days=tick_step_days)

    # -- マイルストーン（プロジェクト開始日含む）を縦線で表示 ------------------------
    for marker_id, label_text, date in milestone_markers:
        x = x_of(date)
        color = _PROJECT_START_COLOR if marker_id == "PROJECT_START" else _MILESTONE_COLOR
        pen = QPen(color, 2, Qt.DashLine)
        line = scene.addLine(x, TOP_MARGIN - 10, x, chart_bottom, pen)
        line.setZValue(-1)
        label = QGraphicsSimpleTextItem(f"◆{label_text}")
        label.setFont(job_font)
        label.setBrush(QBrush(color))
        label.setPos(x + 3, TOP_MARGIN - 44)
        label.setZValue(2)
        scene.addItem(label)

    # -- ジョブ／タスクのバーを描画 ---------------------------------------------------
    team_names = display.get("team_names") or {}
    workflow_names = display.get("workflow_names") or {}
    if color_by == "workflow":
        color_map, color_key = (display.get("workflow_colors") or {}), "Workflow_ID"
    else:
        color_map, color_key = (display.get("team_colors") or {}), "Team_ID"

    for job_id, job_name, y_top, y_bottom, task_lane_pairs in job_blocks:
        job_label = QGraphicsSimpleTextItem(_elide_text(job_name, job_font, LEFT_MARGIN - 12))
        job_label.setFont(job_font)
        job_label.setPos(4, (y_top + y_bottom) / 2 - 8)
        scene.addItem(job_label)

        for r, lane in task_lane_pairs:
            y = y_top + lane * ROW_HEIGHT
            start_x = x_of(r["Start_Date"])
            end_x = x_of(r["End_Date"])
            width = max(end_x - start_x, 2)
            color_hex = color_map.get(r[color_key], _DEFAULT_BAR_COLOR)

            rect = QGraphicsRectItem(QRectF(start_x, y + BAR_MARGIN, width, ROW_HEIGHT - BAR_MARGIN * 2))
            rect.setBrush(QBrush(QColor(color_hex)))
            if r["Resource_Adjusted"]:
                rect.setPen(QPen(_ADJUSTED_BORDER, 2))
            else:
                rect.setPen(QPen(QColor("#0b0b0b"), 1))
            team_name = team_names.get(r["Team_ID"], str(r["Team_ID"]))
            workflow_name = workflow_names.get(r["Workflow_ID"], str(r["Workflow_ID"]))
            rect.setToolTip(
                f'{job_name} / {r["Task_Name"]}\n'
                f'ワークフロー: {workflow_name}\n'
                f'チーム: {team_name}\n'
                f'{r["Start_Date"].strftime("%Y-%m-%d")} 〜 {r["End_Date"].strftime("%Y-%m-%d")}'
                + ("\n※リソース制約により前倒し" if r["Resource_Adjusted"] else "")
            )
            scene.addItem(rect)

            text = _elide_text(str(r["Task_Name"]), task_font, width - 6)
            if text:
                task_label = QGraphicsSimpleTextItem(text)
                task_label.setFont(task_font)
                task_label.setPos(start_x + 3, y + BAR_MARGIN + 2)
                scene.addItem(task_label)

        boundary = scene.addLine(0, y_bottom + JOB_GAP / 2, chart_right, y_bottom + JOB_GAP / 2,
                                  QPen(_GRID_COLOR, 1))
        boundary.setZValue(-2)

    scene.setSceneRect(0, 0, chart_right + 20, chart_bottom + 20)

    # -- フリーズドペイン用の分割矩形 ------------------------------------------------
    # 本体: 左列・ヘッダー行を除いたチャート本体（タスクバー・目盛り線・境界線）。
    # ヘッダー: 本体と同じ横範囲、縦は日付軸・マイルストーン行のみ。
    # 左列: 本体と同じ縦範囲、横は項目名列のみ。
    # いずれも本体との共有軸（ヘッダーなら横、左列なら縦）の範囲・原点を本体と
    # 揃えることで、GraphicsView間のスクロールバー可動域を一致させ、
    # スクロール位置をそのままコピーするだけでズレなく同期できるようにする。
    scene.gantt_body_rect = QRectF(
        LEFT_MARGIN, TOP_MARGIN, chart_right - LEFT_MARGIN + 20, chart_bottom - TOP_MARGIN + 20,
    )
    scene.gantt_header_rect = QRectF(
        LEFT_MARGIN, 0, chart_right - LEFT_MARGIN + 20, TOP_MARGIN + _PANE_PADDING,
    )
    scene.gantt_column_rect = QRectF(
        0, TOP_MARGIN, LEFT_MARGIN + _PANE_PADDING, chart_bottom - TOP_MARGIN + 20,
    )
    return scene
