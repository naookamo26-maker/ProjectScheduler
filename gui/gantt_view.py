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
固定」と同じ考え方）。

ヘッダー・左列・本体は「担当範囲の項目しか持たない」別々の QGraphicsScene
として構築する（build_gantt_scenes）。当初は1つのシーンを3つのビューで
共有し、各ビューの setSceneRect() で見せる範囲を絞る方式にしていたが、
QGraphicsView は setSceneRect() だけでは実際の描画をクリップしないため、
ズームアウトしてビューポートがその範囲より広くなるとQtが中身を中央寄せし、
範囲外にある他ペイン用のアイテム（ヘッダーの日付や左列の項目名など）が
そのまま透けて見えてしまう問題があった。ペインごとに最初から別シーンへ
分けて、担当外の項目をそもそも作らないことで、この問題を構造的に防ぐ。

本体の変換／スクロール位置の変化に追従して、ヘッダーは横方向、左列は
縦方向のみ同期する（交差方向の縮尺は常に1.0固定）。
"""

from collections import namedtuple
from datetime import date, timedelta

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGridLayout,
    QWidget,
)

DAY_WIDTH = 10
ROW_HEIGHT = 26
BAR_MARGIN = 3
# タスクバーの角丸半径（シーン座標）。隣接するバー同士が隙間なく接している
# ときでも、角が丸まっていることで境目を視認しやすくする。
BAR_CORNER_RADIUS = 3
LEFT_MARGIN = 190
# 左列のジョブ名の左に置く、ワークフロー識別用の色スペース（幅・文字との間隔）。
# 複数ワークフローのジョブが混在して並ぶ表示でも、どのワークフローのジョブか
# 一目で分かるようにするため。
_JOB_SWATCH_WIDTH = 10
_JOB_SWATCH_GAP = 4
# ヘッダーは上から (1)マイルストーン名 (2)年 (3)月日 の3段構成のため、
# 目盛り1段のみだった頃より高さが必要。
TOP_MARGIN = 58
JOB_GAP = 6
AXIS_MARGIN_DAYS = 3
# 日単位の補助線を表示し始める、画面上の1週間ぶんの幅(px)のしきい値。
# これを下回る（＝十分に拡大していない）間は補助線を出さない。1週間の
# 目盛り間隔が十分に広がって初めて意味を持つ情報のため。
_DAY_GRID_MIN_WEEK_PX = 140
# 日ごとの日付ラベル（日単位の補助線に添える数字）を表示し始める、画面上の
# 1日ぶんの幅(px)のしきい値。日単位の補助線が出るズームよりさらに拡大しないと
# 数字を並べる余白が確保できないため、_DAY_GRID_MIN_WEEK_PXより厳しい値にする。
_DAY_LABEL_MIN_DAY_PX = 40
# 週の目盛り（月初め以外）を補助線としてすら出さなくする、画面上の1か月
# ぶんの幅(px)のしきい値。平均月日数(30.44日)で近似する。これを下回るほど
# 縮小すると、補助線を残すこと自体が可視性を下げるため完全に隠す。
_WEEK_AUX_HIDE_MAX_MONTH_PX = 60
_DAYS_PER_MONTH_AVG = 30.44
# ヘッダー内の各段のY位置（TOP_MARGINからの差分。値が大きいほど上）。
_MILESTONE_LABEL_OFFSET = 56
_YEAR_LABEL_OFFSET = 38
_TICK_LABEL_OFFSET = 22
_GRID_TOP_OFFSET = 10
# 年の区切り線を、年ラベルの文字の高さから上下にどれだけ広げるか(px)。
_YEAR_DIVIDER_PADDING = 3
# ヘッダー／左列ペインの表示専用の余白。ペイン境界の罫線が、隣接する本体側の
# 続きと見た目上つながって見えるよう、担当範囲の少し先まで描いておく分。
_PANE_PADDING = 10
# 全体表示（フィット）時のズーム下限を計算する際、丸め誤差で隙間が生まれない
# よう、内容をビューポートよりわずかに大きく保つための安全マージン(px)。
_SCALE_FLOOR_MARGIN_PX = 4
# タスクバー内ラベル用。バーの左右・上下に対する表示可能幅・高さの余白(px)。
_TASK_LABEL_H_MARGIN_PX = 6
_TASK_LABEL_V_MARGIN_PX = 2
# 2行表示に切り替える際、1行目と2行目の間に最低限見込む余白(px)。
_TASK_LABEL_LINE_GAP_PX = 2

_GRID_COLOR = QColor("#e1e0d9")
# 日単位の補助線・間引かれた週の目盛り線用。主線（_GRID_COLOR）よりさらに
# 薄くし、常時表示しても本体のバーの視認性を落とさないようにする。
_AUX_GRID_COLOR = QColor("#eeede6")
# 年の変わり目の区切り線。月・週の目盛りより意味が大きい区切りのため、
# 主線（_GRID_COLOR）より少しはっきりした色にする。
_YEAR_GRID_COLOR = QColor("#c9c7bd")
_MILESTONE_COLOR = QColor("#c0392b")
_PROJECT_START_COLOR = QColor("#52514e")
# 「今日」の縦線。マイルストーン（赤破線）・開発開始日（濃灰破線）と見分けが
# つくよう、実線・別系統の色（青）にする。
_TODAY_LINE_COLOR = QColor("#1a73e8")
# 休業日の日付ラベル（日単位の個別表示時のみ）。マイルストーンと同じ赤系だが、
# 別の要素であることが分かるよう独立した定数にしてある。
_HOLIDAY_LABEL_COLOR = QColor("#c0392b")
_DEFAULT_BAR_COLOR = "#cbc9c2"
# 1行飛ばしのジョブ行の背景（半透明の黒を薄く重ねるだけなので、背景色
# （_PANE_BG）を変えても常に「少し暗い」効果になり色を合わせ直す必要がない）。
_ROW_STRIPE_COLOR = QColor(0, 0, 0, 12)
# マイルストーンの締切に間に合わないタスクの強調。塗りつぶしはチーム／ワーク
# フローの色分けをそのまま残したいので、枠線だけを赤く太くして重ねて表す。
_OVERRUN_BORDER_COLOR = QColor("#c5221f")
_OVERRUN_BORDER_WIDTH = 3
# 開始固定日を満たせなかったタスク。締切超過（赤の実線）と区別できるよう、
# 別の色＋破線にする。件数だけを状況表示に出しても「どれが」が分からないため、
# バー側にも印を付けて特定できるようにする。
_CONSTRAINT_BORDER_COLOR = QColor("#b26a00")
_CONSTRAINT_BORDER_WIDTH = 3
_NORMAL_BORDER_COLOR = QColor("#0b0b0b")
_NORMAL_BORDER_WIDTH = 1
# 本体シーンの重ね順: 1行飛ばしの行背景(-5) < 日単位の補助線(-3) <
# 週の目盛り・区切り線(-2〜-1) < 通常のバー(0) < 締切超過のバー(1) <
# タスク名ラベル(2)
_ROW_STRIPE_Z = -5
_OVERRUN_BAR_Z = 1
_TASK_LABEL_Z = 2
_PANE_BG = QColor("#fdfcf9")

# build_gantt_scenes() の戻り値。header/column/body はそれぞれの担当分だけの
# 項目を持つ独立した QGraphicsScene（FrozenGanttPane.setScene()参照）。
GanttScenes = namedtuple("GanttScenes", ["header", "column", "body"])


class GanttGraphicsView(QGraphicsView):
    """本体ペイン。ホイールでズーム、中ボタンドラッグでパン
    （gui/node_canvas.py の WorkflowGraphView と同じ操作感）。ガントチャートは
    時間軸（横）と行数（縦）の縮尺を別々に調整したいことが多いため、
    Ctrlを押しながらのホイールで横方向のみ、Shiftを押しながらのホイールで
    縦方向のみ、修飾キーなしなら従来通り両方向を拡縮する。

    変換／スクロール位置が変わるたびに transformChanged を発火し、
    FrozenGanttPane がヘッダー・左列ペインを追従させる。タスクバーは
    選択可能（ラバーバンド選択・クリック選択）にしてあり、Aキーで全体表示、
    Fキーで選択中のタスクへズームする（gui/node_canvas.py の
    WorkflowGraphView と同じキー操作）。"""

    transformChanged = Signal()
    fitAllRequested = Signal()
    fitSelectedRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(self.renderHints())
        # 文字色は常に黒固定（QGraphicsSimpleTextItemの既定）で描画しているため、
        # OSがダークモードだと既定の（ダークな）ビュー背景に文字が埋もれて
        # 読めなくなる。この独自キャンバスはOSのテーマに関わらず常に明るい
        # 背景で描くようにし、文字色との組み合わせを固定して視認性を保つ。
        self.setBackgroundBrush(QBrush(_PANE_BG))
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self._panning = False
        self._pan_last_pos = None
        h_bar = self.horizontalScrollBar()
        v_bar = self.verticalScrollBar()
        h_bar.valueChanged.connect(self.transformChanged)
        v_bar.valueChanged.connect(self.transformChanged)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_A:
            self.fitAllRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key_F:
            self.fitSelectedRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        modifiers = event.modifiers()
        if modifiers & Qt.ControlModifier:
            self.scale(factor, 1.0)
        elif modifiers & Qt.ShiftModifier:
            self.scale(1.0, factor)
        else:
            self.scale(factor, factor)
        self._clamp_scale()
        self.transformChanged.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._clamp_scale():
            self.transformChanged.emit()

    def _clamp_scale(self):
        """全体表示（フィット時）の縮尺より外側へはズームアウトできないよう
        下限を設ける。本体の内容（gantt_body_rect）が常にビューポート以上の
        大きさを保つようにし、それより縮小してもチャート全体は既に見えている
        ため実用上ズームアウトする意味がない。戻り値: 実際に縮尺を補正したか。"""
        scene = self.scene()
        if scene is None:
            return False
        rect = getattr(scene, "gantt_body_rect", None)
        if rect is None or rect.isEmpty():
            return False
        viewport_size = self.viewport().size()
        transform = self.transform()
        sx, sy = transform.m11(), transform.m22()
        # fitInView等の丸め誤差でちょうど等倍（隙間ゼロ）を狙うと、わずかな
        # 誤差で隙間が生まれてしまうことがあるため、数ピクセル分だけ内容を
        # ビューポートより意図的に大きくしておき、隙間が生じる余地を無くす。
        min_sx = (viewport_size.width() + _SCALE_FLOOR_MARGIN_PX) / rect.width() if rect.width() > 0 else sx
        min_sy = (viewport_size.height() + _SCALE_FLOOR_MARGIN_PX) / rect.height() if rect.height() > 0 else sy
        new_sx = max(sx, min_sx)
        new_sy = max(sy, min_sy)
        if new_sx == sx and new_sy == sy:
            return False
        self.setTransform(QTransform().scale(new_sx, new_sy))
        return True

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
    直接使っていた旧 GanttGraphicsView の代わりにこれを配置する。

    setScene() には単一の QGraphicsScene ではなく build_gantt_scenes() が
    返す GanttScenes（header/column/body の3シーン）を渡す。"""

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
        self.body.fitAllRequested.connect(self.fit_all)
        self.body.fitSelectedRequested.connect(self.fit_selected)

    def setScene(self, scenes):
        if scenes is None:
            self.header.setScene(None)
            self.column.setScene(None)
            self.body.setScene(None)
            return
        self.header.setScene(scenes.header)
        self.column.setScene(scenes.column)
        self.body.setScene(scenes.body)
        header_rect = getattr(scenes.header, "gantt_header_rect", None)
        column_rect = getattr(scenes.column, "gantt_column_rect", None)
        body_rect = getattr(scenes.body, "gantt_body_rect", None)
        if header_rect is not None:
            self.header.setSceneRect(header_rect)
        if column_rect is not None:
            self.column.setSceneRect(column_rect)
        if body_rect is not None:
            self.body.setSceneRect(body_rect)
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
        self.body._clamp_scale()
        self._sync_panes()

    def fit_selected(self):
        """選択中のタスクバーだけが収まるようにズームを合わせる（Fキー）。
        選択が無ければ全体表示にフォールバックする
        （gui/node_canvas.py の fit_selected() と同じ考え方）。"""
        scene = self.body.scene()
        if scene is None:
            return
        selected = scene.selectedItems()
        if not selected:
            self.fit_all()
            return
        rect = QRectF()
        for item in selected:
            rect = rect.united(item.sceneBoundingRect())
        margin = 20
        rect = rect.adjusted(-margin, -margin, margin, margin)
        self.body.fitInView(rect, Qt.IgnoreAspectRatio)
        self.body._clamp_scale()
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

        self._update_axis_density(sx)
        self._center_milestone_labels(sx)
        self._center_tick_labels(sx)
        self._center_task_labels(sx, sy)

    def _update_axis_density(self, sx):
        """現在の横方向の拡縮率（sx）に応じて、日付軸の見せ方を切り替える。

        拡大方向（詳細を見せる）:
        - 1週間の目盛り間隔が_DAY_GRID_MIN_WEEK_PX以上に広がったら、日単位の
          補助線を表示する。
        - さらに1日の幅が_DAY_LABEL_MIN_DAY_PX以上に広がったら、日ごとの
          日付ラベル（日の数字のみ。休業日は赤字）も表示する。

        縮小方向（間引いて可視性を確保する）:
        - 1週間の目盛り間隔が"MM/DD"ラベルの幅より狭くなったら（compact）、
          月が変わった直後の週の目盛り（月初めの線）以外は、線を補助線化して
          ラベルを消す。月初めの線のラベルは年の行と重複しないよう月の数字
          だけに簡略化する（例:「03/02」→「3」）。
        - さらに1か月の幅が_WEEK_AUX_HIDE_MAX_MONTH_PXを下回ったら、月初め
          以外の週の目盛りは補助線としてもうるさいため完全に隠す。

        それ以外（通常時）は、全ての週の目盛りに主線と"MM/DD"ラベルを表示する
        （build_gantt_scenesが作った直後の状態）。

        シーンは作り直さず、あらかじめ用意済みの線・ラベルの表示/非表示・
        ペイン・文言だけを切り替える。"""
        scene = self.header.scene()
        if scene is None or sx <= 0:
            return
        day_px = DAY_WIDTH * sx
        week_px = day_px * 7
        month_px = day_px * _DAYS_PER_MONTH_AVG
        show_day_grid = week_px >= _DAY_GRID_MIN_WEEK_PX
        show_day_labels = show_day_grid and day_px >= _DAY_LABEL_MIN_DAY_PX
        label_min_px = getattr(scene, "gantt_week_label_min_px", 0)
        compact = week_px < label_min_px
        hide_week_aux = compact and month_px < _WEEK_AUX_HIDE_MAX_MONTH_PX

        for header_line, body_line, label in getattr(scene, "gantt_day_ticks", []):
            header_line.setVisible(show_day_grid)
            body_line.setVisible(show_day_grid)
            label.setVisible(show_day_labels)

        for header_line, body_line, label, is_month_boundary, full_text in \
                getattr(scene, "gantt_week_ticks", []):
            if compact and not is_month_boundary:
                visible = not hide_week_aux
                header_line.setVisible(visible)
                body_line.setVisible(visible)
                header_line.setPen(QPen(_AUX_GRID_COLOR, 1))
                body_line.setPen(QPen(_AUX_GRID_COLOR, 1))
                label.setVisible(False)
                continue
            header_line.setVisible(True)
            body_line.setVisible(True)
            header_line.setPen(QPen(_GRID_COLOR, 1))
            body_line.setPen(QPen(_GRID_COLOR, 1))
            label.setVisible(True)
            if compact:
                text = full_text.split("/", 1)[0].lstrip("0") or "0"
            else:
                text = full_text
            if label.text() != text:
                label.setText(text)

    def _center_task_labels(self, sx, sy):
        """タスクバー内のラベルを、そのバーの中心に揃え続ける。
        _center_milestone_labelsと同じ理由で、バー中心からのオフセットは
        現在の拡縮率(sx, sy)で割ってシーン座標に変換する必要がある。

        ラベルはItemIgnoresTransformationsで常に等倍（一定ピクセル数）で
        描画される一方、バー自体はズームすると画面上のサイズが変わるため、
        ズームアウトするとラベルがバーの外へはみ出してしまう。そこで、
        現在の拡縮率から実際に使える画面上の幅・高さを求め、
        - 幅が足りなければ省略（…）表示にする
        - それでも表示に値する幅すら無ければ非表示にする
        - 縦方向にもう1行分の余裕があれば2行に分けて表示する
        よう、その都度テキストを組み立て直す。"""
        scene = self.body.scene()
        if scene is None or sx <= 0 or sy <= 0:
            return
        # 全タスクラベルが同じフォント（task_font）を共有しているのが通常なので、
        # QFontMetricsをラベルごとに作り直さず使い回す（数百〜数千件のラベルを
        # 毎フレーム処理するため、地味だが効く最適化）。
        metrics_cache = {}
        for label, center_x, center_y, bar_width, bar_height, full_text, font in \
                getattr(scene, "gantt_task_labels", []):
            metrics = metrics_cache.get(id(font))
            if metrics is None:
                metrics = QFontMetrics(font)
                metrics_cache[id(font)] = metrics
            line_height = metrics.height()
            avail_w = bar_width * sx - _TASK_LABEL_H_MARGIN_PX
            avail_h = bar_height * sy - _TASK_LABEL_V_MARGIN_PX

            if avail_w < metrics.averageCharWidth() or avail_h < line_height:
                label.setVisible(False)
                continue

            single_line = metrics.elidedText(full_text, Qt.ElideRight, int(avail_w))
            if not single_line or single_line == "…":
                label.setVisible(False)
                continue

            if single_line != full_text and avail_h >= line_height * 2 + _TASK_LABEL_LINE_GAP_PX:
                line1, line2 = _wrap_two_lines(full_text, metrics, avail_w)
                text = f"{line1}\n{line2}" if line2 else line1
            else:
                text = single_line

            label.setVisible(True)
            if label.text() != text:
                label.setText(text)
            width_px = label.boundingRect().width()
            height_px = label.boundingRect().height()
            label.setPos(center_x - (width_px / 2) / sx, center_y - (height_px / 2) / sy)

    def _center_milestone_labels(self, sx):
        """マイルストーンラベルを、その縦線を中心に左右均等になるよう配置
        し直す。ItemIgnoresTransformationsを立てた項目のsetPos()はシーン座標
        系のままなので、画面上で「中央揃え」を保つオフセット（ラベル幅の半分）
        は、現在の横方向の拡縮率(sx)で割ってシーン座標に変換する必要がある。
        また、軸の両端付近では中央揃えのままだとラベルがチャート外へはみ出す
        ため、ヘッダーの表示範囲（gantt_header_rect）に収まるようクランプする。"""
        scene = self.header.scene()
        if scene is None or sx <= 0:
            return
        header_rect = getattr(scene, "gantt_header_rect", None)
        for label, line_x in getattr(scene, "gantt_milestone_labels", []):
            width_px = label.boundingRect().width()
            anchor = line_x - (width_px / 2) / sx
            if header_rect is not None:
                min_anchor = header_rect.left()
                max_anchor = max(min_anchor, header_rect.right() - width_px / sx)
                anchor = max(min_anchor, min(anchor, max_anchor))
            label.setPos(anchor, label.y())

    def _center_tick_labels(self, sx):
        """日付目盛りラベルを、その縦線を中心に左右均等になるよう配置し直す。
        _center_milestone_labelsと同じ理由で、中央揃えのオフセット（ラベル幅の
        半分）は現在の横方向の拡縮率(sx)で割ってシーン座標に変換する必要がある。"""
        scene = self.header.scene()
        if scene is None or sx <= 0:
            return
        for label, line_x in getattr(scene, "gantt_tick_labels", []):
            width_px = label.boundingRect().width()
            label.setPos(line_x - (width_px / 2) / sx, label.y())


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


def _wrap_two_lines(text, metrics, max_width):
    """textを2行に分ける。1行目は max_width に収まる範囲で貪欲に文字を
    詰め、残りを2行目として max_width に収まるよう省略する
    （スペース区切りの無い日本語のタスク名でも自然に折り返せるよう、
    単語単位ではなく文字単位で詰める）。"""
    fit_len = 1
    for i in range(1, len(text) + 1):
        if metrics.horizontalAdvance(text[:i]) > max_width:
            break
        fit_len = i
    line1 = text[:fit_len]
    remainder = text[fit_len:]
    line2 = metrics.elidedText(remainder, Qt.ElideRight, int(max_width)) if remainder else ""
    return line1, line2


def _add_fixed_size_label(scene, text, font, pos, brush=None, z_value=None):
    """日付・マイルストーン・項目名のラベル用。ItemIgnoresTransformationsを
    立てることで、ヘッダー／左列ペインの拡縮（本体に追従する軸方向の縮尺）に
    よらず常に一定の文字サイズ・縦横比で表示されるようにする（親ビューの
    変換を無視して等倍描画される）。位置(pos)はシーン座標のまま指定でき、
    描画時にその位置へマッピングされる。"""
    label = QGraphicsSimpleTextItem(text)
    label.setFont(font)
    if brush is not None:
        label.setBrush(brush)
    label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
    label.setPos(*pos)
    if z_value is not None:
        label.setZValue(z_value)
    scene.addItem(label)
    return label


def build_gantt_scenes(df, display, color_by="team"):
    """df: result_df を表示対象（1ワークフロー分、または1チーム分）に絞り込んだ
    もの。display: compute_schedule()の2番目の戻り値。ヘッダー／左列／本体
    それぞれの担当分の項目だけを持つ3つの QGraphicsScene を GanttScenes に
    まとめて返す（絞り込んだ結果が空ならNoneを返す）。

    color_by: "team"（既定、ワークフロー別表示用——同じワークフロー内で担当
    チームを見分けたい）または "workflow"（チーム別表示用——1チームに
    絞り込まれている代わりに、どのワークフローの仕事かを見分けたい）。

    マイルストーンの締切に間に合わないタスク（Deadline_Overrun_Days > 0）は、
    塗りつぶしの色（＝チーム／ワークフローの識別）はそのままに、枠線を赤く
    太くして強調する。色分けの軸を潰さずに「間に合っていない」を重ねて
    表せるため。開始固定日を満たせなかったタスク（Constraint_Violation）も
    同じ考え方で、別の色の破線の枠にする（両方に該当する場合は締切超過を
    優先。枠線は1本しか引けないため）。

    3つのシーンは同じ座標系（LEFT_MARGIN/TOP_MARGIN起点、x_of()による日付
    ->x座標変換）を共有しているが、日付軸・マイルストーン・目盛り線の縦線
    などペインをまたいで見える要素は、各ペインが実際に描く区間だけを別々の
    アイテムとして両方のシーンに（境界がつながって見えるよう少し重ねて）
    追加している。"""
    if df.empty:
        return None

    milestone_markers = display.get("milestone_markers") or []
    axis_start = min(df["Start_Date"].min(), *(m[2] for m in milestone_markers)) if milestone_markers \
        else df["Start_Date"].min()
    axis_end = max(df["End_Date"].max(), *(m[2] for m in milestone_markers)) if milestone_markers \
        else df["End_Date"].max()
    axis_start = axis_start - timedelta(days=AXIS_MARGIN_DAYS)
    axis_end = axis_end + timedelta(days=AXIS_MARGIN_DAYS)

    def x_of(date):
        return LEFT_MARGIN + (date - axis_start).days * DAY_WIDTH

    header_scene = QGraphicsScene()
    column_scene = QGraphicsScene()
    body_scene = QGraphicsScene()

    task_font = QFont()
    task_font.setPointSize(9)
    job_font = QFont()
    job_font.setPointSize(9)
    job_font.setBold(True)

    # -- ジョブごとにレーン詰め、Y座標を決める --------------------------------------
    job_order = df.groupby("Job_ID")["Start_Date"].min().sort_values().index.tolist()
    y_cursor = TOP_MARGIN
    job_blocks = []  # (job_id, job_name, workflow_id, y_top, y_bottom, [(task_row, lane), ...])
    for job_id in job_order:
        job_rows = df[df["Job_ID"] == job_id].sort_values("Start_Date")
        tasks = [{"start": r["Start_Date"], "end": r["End_Date"]} for _, r in job_rows.iterrows()]
        lanes, lane_count = _pack_lanes(tasks)
        y_top = y_cursor
        y_bottom = y_top + lane_count * ROW_HEIGHT
        job_blocks.append((job_id, str(job_rows.iloc[0]["Job_Name"]), job_rows.iloc[0]["Workflow_ID"],
                            y_top, y_bottom, list(zip(job_rows.to_dict("records"), lanes))))
        y_cursor = y_bottom + JOB_GAP

    chart_bottom = y_cursor
    chart_right = x_of(axis_end)
    # ヘッダーの縦線・左列の横線は、隣接する本体側の続きと見た目がつながる
    # よう、それぞれのペイン境界のさらに少し先（_PANE_PADDING分）まで描く。
    header_stub_bottom = TOP_MARGIN + _PANE_PADDING
    column_stub_right = LEFT_MARGIN + _PANE_PADDING

    # -- 日付軸（週単位の目盛り＋日単位の補助線。密度はズーム量に応じて動的に
    #    切り替える） ------------------------------------------------------------
    # 目盛りラベルは年をまたいでも「YYYY-MM-DD」を毎回繰り返すと横に長く冗長なため、
    # 月日のみを目盛りごとに、年は表示範囲に含まれる年ごとに上段中央へ1回だけ表示する
    # 2段構成にする。いずれもヘッダー専用（本体には描かない）。
    #
    # 週の目盛り線・日の補助線はここで表示範囲全体ぶん一度だけ作っておき、
    # 実際にどれを見せるか（主線／補助線／ラベルの有無や文言）は現在の拡縮率
    # （sx）に応じて FrozenGanttPane._update_axis_density が_sync_panesの
    # たびに切り替える（シーンを作り直すコストを避けるため）。
    #
    # 週の目盛りのうち、月が変わった直後の1本（is_month_boundary）は「月初めの
    # 線」として扱い、間隔が詰まったズーム時にもラベルと主線の見た目を残す
    # （他の週の目盛りは補助線に格下げしてラベルを消す）。
    header_scene.gantt_tick_labels = []  # 中央揃え用（_center_tick_labels）。週・日のラベルが対象。
    header_scene.gantt_week_ticks = []   # (header_line, body_line, label, is_month_boundary, full_text)
    header_scene.gantt_day_ticks = []    # (header_line, body_line, label)

    week_dates = set()
    tick_date = axis_start + timedelta(days=(7 - axis_start.weekday()) % 7)  # 最初の目盛りを月曜に揃える
    prev_month = None
    while tick_date <= axis_end:
        week_dates.add(tick_date)
        x = x_of(tick_date)
        is_month_boundary = (tick_date.year, tick_date.month) != prev_month
        prev_month = (tick_date.year, tick_date.month)
        header_line = header_scene.addLine(x, TOP_MARGIN - _GRID_TOP_OFFSET, x, header_stub_bottom,
                                            QPen(_GRID_COLOR, 1))
        header_line.setZValue(-2)
        body_line = body_scene.addLine(x, TOP_MARGIN, x, chart_bottom, QPen(_GRID_COLOR, 1))
        body_line.setZValue(-2)
        full_text = tick_date.strftime("%m/%d")
        label = _add_fixed_size_label(
            header_scene, full_text, task_font, (x, TOP_MARGIN - _TICK_LABEL_OFFSET),
        )
        header_scene.gantt_tick_labels.append((label, x))
        header_scene.gantt_week_ticks.append((header_line, body_line, label, is_month_boundary, full_text))
        tick_date += timedelta(days=7)

    # 表示中のチーム（対象コンボ・凡例チェックボックスの絞り込み後にdfへ実際に
    # 残っているチーム）に関係する休業日だけを、日付ラベルの赤字対象にする
    # （関係の無い他チームの休業日まで赤くすると誤解を招くため）。
    relevant_team_ids = set(df["Team_ID"].unique()) if "Team_ID" in df.columns else set()
    holiday_dates = set(display.get("common_holiday_dates") or ())
    holidays_by_team = display.get("holidays_by_team") or {}
    for team_id in relevant_team_ids:
        holiday_dates.update(holidays_by_team.get(team_id, ()))

    # 日単位の補助線（週の目盛りと重なる日は除く）と、日ごとの日付ラベル
    # （日の数字のみ。休業日は赤字）。既定ではどちらも非表示にしておき、
    # 十分に拡大された時だけ _update_axis_density が表示する（ラベルは
    # 補助線よりさらに拡大しないと出さない——十分な余白が要るため）。
    day_cursor = axis_start
    while day_cursor <= axis_end:
        if day_cursor not in week_dates:
            x = x_of(day_cursor)
            header_line = header_scene.addLine(x, TOP_MARGIN - _GRID_TOP_OFFSET, x, header_stub_bottom,
                                                QPen(_AUX_GRID_COLOR, 1))
            header_line.setZValue(-3)
            header_line.setVisible(False)
            body_line = body_scene.addLine(x, TOP_MARGIN, x, chart_bottom, QPen(_AUX_GRID_COLOR, 1))
            body_line.setZValue(-3)
            body_line.setVisible(False)
            is_holiday = day_cursor.date() in holiday_dates
            label = _add_fixed_size_label(
                header_scene, str(day_cursor.day), task_font, (x, TOP_MARGIN - _TICK_LABEL_OFFSET),
                brush=QBrush(_HOLIDAY_LABEL_COLOR) if is_holiday else None,
            )
            label.setVisible(False)
            header_scene.gantt_tick_labels.append((label, x))
            header_scene.gantt_day_ticks.append((header_line, body_line, label))
        day_cursor += timedelta(days=1)

    # 週の目盛りラベル（"MM/DD"、幅5文字ぶん）が重ならずに収まる最小の週間隔
    # （画面px）。これを下回ったら _update_axis_density が間引きモードに切り替える。
    header_scene.gantt_week_label_min_px = QFontMetrics(task_font).horizontalAdvance("00/00") * 1.4

    year_font = QFont(task_font)
    year_font.setBold(True)
    year_metrics = QFontMetrics(year_font)
    year_cursor = axis_start.replace(month=1, day=1)
    while year_cursor <= axis_end:
        year_end = year_cursor.replace(month=12, day=31)
        span_start = max(axis_start, year_cursor)
        span_end = min(axis_end, year_end)
        center_x = (x_of(span_start) + x_of(span_end)) / 2
        text = f"{year_cursor.year}年"
        text_width = year_metrics.horizontalAdvance(text)
        _add_fixed_size_label(
            header_scene, text, year_font,
            (center_x - text_width / 2, TOP_MARGIN - _YEAR_LABEL_OFFSET),
        )
        if year_cursor > axis_start:
            # 年の変わり目に区切り線を引く（軸の左端そのものは境目ではないため
            # 引かない）。チャート全体（本体・他の行）には伸ばさず、年のラベルを
            # 書いている行の高さだけに収める（「2026 | 2027」のように、年ラベル
            # 同士の区切りとして見せるため）。ズーム量に関わらず常に表示する
            # （月・週の目盛りと違い間引く対象ではない）。
            x = x_of(year_cursor)
            row_top = TOP_MARGIN - _YEAR_LABEL_OFFSET - _YEAR_DIVIDER_PADDING
            row_bottom = TOP_MARGIN - _YEAR_LABEL_OFFSET + year_metrics.height() + _YEAR_DIVIDER_PADDING
            header_line = header_scene.addLine(x, row_top, x, row_bottom, QPen(_YEAR_GRID_COLOR, 1))
            header_line.setZValue(-1)
        year_cursor = year_cursor.replace(year=year_cursor.year + 1)

    # -- マイルストーン（プロジェクト開始日含む）を縦線で表示 ------------------------
    # ラベルは「◆」を付けず、縦線を中心に左右均等に配置する（線がどのマイル
    # ストーンを指しているか一目でわかり、線の片側だけに伸びるより見やすい）。
    # ItemIgnoresTransformationsを立てた項目はsetPos自体はシーン座標のまま
    # ズームの影響を受けるため、「中央揃え」を維持するオフセットは実際の表示
    # 倍率が分かるタイミング（FrozenGanttPane._sync_panes）でしか正しく計算
    # できない。ここでは対象を後で拾えるよう参照だけ残しておく。
    header_scene.gantt_milestone_labels = []
    for marker_id, label_text, marker_date in milestone_markers:
        x = x_of(marker_date)
        color = _PROJECT_START_COLOR if marker_id == "PROJECT_START" else _MILESTONE_COLOR
        pen = QPen(color, 2, Qt.DashLine)
        # コズメティックペンにし、太さ(2px)がズーム（本体の拡縮率）の影響を
        # 受けず常に画面上で一定になるようにする（既定では線の太さもシーン
        # 座標として拡縮されるため、ズームアウトすると細く、ズームインすると
        # 太くなってしまう）。
        pen.setCosmetic(True)
        header_line = header_scene.addLine(x, TOP_MARGIN - _GRID_TOP_OFFSET, x, header_stub_bottom, pen)
        header_line.setZValue(-1)
        body_line = body_scene.addLine(x, TOP_MARGIN, x, chart_bottom, pen)
        body_line.setZValue(-1)
        label = _add_fixed_size_label(
            header_scene, label_text, job_font,
            (x, TOP_MARGIN - _MILESTONE_LABEL_OFFSET),
            brush=QBrush(color), z_value=2,
        )
        header_scene.gantt_milestone_labels.append((label, x))

    # -- 「今日」を縦線で表示（表示範囲に含まれる場合のみ） --------------------------
    # マイルストーンとは違い対象データから独立した「現在時刻」由来の情報のため、
    # 表示範囲（axis_start/axis_end）を決める対象には含めない——含めてしまうと、
    # 過去または未来だけのプロジェクトで「今日」を無理に表示範囲へ押し込むために
    # 軸全体が不自然に伸びてしまう。範囲外なら単に描かない。
    today = axis_start + (date.today() - axis_start.date())
    if axis_start <= today <= axis_end:
        x = x_of(today)
        pen = QPen(_TODAY_LINE_COLOR, 2)
        pen.setCosmetic(True)  # マイルストーンと同じ理由で、太さをズームに依らず一定にする
        header_line = header_scene.addLine(x, TOP_MARGIN - _GRID_TOP_OFFSET, x, header_stub_bottom, pen)
        header_line.setZValue(-1)
        body_line = body_scene.addLine(x, TOP_MARGIN, x, chart_bottom, pen)
        body_line.setZValue(-1)
        label = _add_fixed_size_label(
            header_scene, "今日", job_font,
            (x, TOP_MARGIN - _MILESTONE_LABEL_OFFSET),
            brush=QBrush(_TODAY_LINE_COLOR), z_value=2,
        )
        header_scene.gantt_milestone_labels.append((label, x))

    # -- ジョブ／タスクのバーを描画 ---------------------------------------------------
    team_names = display.get("team_names") or {}
    workflow_names = display.get("workflow_names") or {}
    workflow_colors = display.get("workflow_colors") or {}
    if color_by == "workflow":
        color_map, color_key = (display.get("workflow_colors") or {}), "Workflow_ID"
    else:
        color_map, color_key = (display.get("team_colors") or {}), "Team_ID"

    # タスクバー内のラベルは、拡縮の縦横比が違う（IgnoreAspectRatioで独立に
    # 拡縮する）と文字が歪んで見えるため、日付・マイルストーン・項目名と
    # 同様にItemIgnoresTransformationsで常に等倍（一定ピクセル数）描画する。
    # ただしそのままだと、バーを画面上で小さく縮小した際にラベルがバーから
    # はみ出してしまう。バーの中心揃えに加えて、現在の表示倍率でのバーの
    # 実サイズに応じた省略表示・非表示・2行化も、実際の表示倍率が分かる
    # タイミング（FrozenGanttPane._sync_panes）でしか正しく計算できないため、
    # ここでは元のタスク名とバーサイズ（シーン座標）を後で拾えるよう
    # 参照だけ残しておく。
    body_scene.gantt_task_labels = []

    job_metrics = QFontMetrics(job_font)
    swatch_height = job_metrics.height()
    for row_index, (job_id, job_name, job_workflow_id, y_top, y_bottom, task_lane_pairs) \
            in enumerate(job_blocks):
        if row_index % 2 == 1:
            # 1行飛ばしでジョブ行の背面を薄い灰色にし、行の視認性を上げる
            # （どのバーがどのジョブの行に属するか目で追いやすくするため）。
            # 半透明にしてあるのは、背景色（_PANE_BG）が変わっても常に
            #「少し暗くする」効果になり、色を合わせ直す必要がないため。
            column_stripe = column_scene.addRect(
                0, y_top, column_stub_right, y_bottom - y_top, QPen(Qt.NoPen), QBrush(_ROW_STRIPE_COLOR),
            )
            column_stripe.setZValue(_ROW_STRIPE_Z)
            body_stripe = body_scene.addRect(
                LEFT_MARGIN, y_top, chart_right - LEFT_MARGIN, y_bottom - y_top,
                QPen(Qt.NoPen), QBrush(_ROW_STRIPE_COLOR),
            )
            body_stripe.setZValue(_ROW_STRIPE_Z)

        # ワークフロー識別用の色スペース。ItemIgnoresTransformationsを立てて
        # ジョブ名ラベルと同様に常に一定の画面サイズで表示する（縦にズームしても
        # 太さが変わらないようにするため）。
        swatch_color = QColor(workflow_colors.get(job_workflow_id, _DEFAULT_BAR_COLOR))
        swatch = column_scene.addRect(
            0, 0, _JOB_SWATCH_WIDTH, swatch_height,
            QPen(_NORMAL_BORDER_COLOR, _NORMAL_BORDER_WIDTH), QBrush(swatch_color),
        )
        swatch.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        swatch.setPos(4, (y_top + y_bottom) / 2 - swatch_height / 2)
        swatch.setToolTip(f"ワークフロー: {workflow_names.get(job_workflow_id, job_workflow_id)}")

        _add_fixed_size_label(
            column_scene,
            _elide_text(job_name, job_font, LEFT_MARGIN - 12 - _JOB_SWATCH_WIDTH - _JOB_SWATCH_GAP),
            job_font,
            (4 + _JOB_SWATCH_WIDTH + _JOB_SWATCH_GAP, (y_top + y_bottom) / 2 - 8),
        )

        for r, lane in task_lane_pairs:
            y = y_top + lane * ROW_HEIGHT
            start_x = x_of(r["Start_Date"])
            end_x = x_of(r["End_Date"])
            width = max(end_x - start_x, 2)
            color_hex = color_map.get(r[color_key], _DEFAULT_BAR_COLOR)

            bar_height = ROW_HEIGHT - BAR_MARGIN * 2
            bar_path = QPainterPath()
            bar_path.addRoundedRect(
                QRectF(start_x, y + BAR_MARGIN, width, bar_height),
                BAR_CORNER_RADIUS, BAR_CORNER_RADIUS,
            )
            overrun_days = int(r.get("Deadline_Overrun_Days", 0) or 0)
            constraint_violation = str(r.get("Constraint_Violation") or "")

            rect = QGraphicsPathItem(bar_path)
            rect.setBrush(QBrush(QColor(color_hex)))
            if overrun_days > 0:
                # 両方に該当する場合は締切超過（赤の実線）を優先する。枠線は
                # 1本しか引けないため、より重い「間に合っていない」を採る
                # （どちらに該当しているかはツールチップに両方出る）。
                border_pen = QPen(_OVERRUN_BORDER_COLOR, _OVERRUN_BORDER_WIDTH)
            elif constraint_violation:
                border_pen = QPen(_CONSTRAINT_BORDER_COLOR, _CONSTRAINT_BORDER_WIDTH, Qt.DashLine)
            else:
                border_pen = QPen(_NORMAL_BORDER_COLOR, _NORMAL_BORDER_WIDTH)
            # コズメティックペイン（常に一定の画面上の太さで描く）にしないと、
            # 横縦で異なる拡縮率（IgnoreAspectRatio）のもとでは、枠線の太さが
            # 辺の向きによって（横縁は縦方向の拡縮率、縦縁は横方向の拡縮率の
            # 影響を受けて）不揃いに見えてしまう。締切超過の強調は「太さ」で
            # 表すため、拡縮によらず一定であることが特に重要になる。
            border_pen.setCosmetic(True)
            rect.setPen(border_pen)
            # 赤枠が隣のバーやジョブ区切り線に隠れないよう、超過タスクだけ手前に
            # 重ねる（枠線はバーの輪郭の内外にまたがって描かれるため）。
            if overrun_days > 0 or constraint_violation:
                rect.setZValue(_OVERRUN_BAR_Z)
            rect.setFlag(QGraphicsItem.ItemIsSelectable, True)
            team_name = team_names.get(r["Team_ID"], str(r["Team_ID"]))
            workflow_name = workflow_names.get(r["Workflow_ID"], str(r["Workflow_ID"]))
            rect.setToolTip(
                f'{job_name} / {r["Task_Name"]}\n'
                f'ワークフロー: {workflow_name}\n'
                f'チーム: {team_name}\n'
                f'{r["Start_Date"].strftime("%Y-%m-%d")} 〜 {r["End_Date"].strftime("%Y-%m-%d")}'
                + (f'\n⚠ マイルストーンの締切を{overrun_days}日超過' if overrun_days > 0 else "")
                + (f'\n⚠ {constraint_violation}' if constraint_violation else "")
                + ("\n※リソース制約により前倒し" if r["Resource_Adjusted"] else "")
            )
            body_scene.addItem(rect)

            task_name = str(r["Task_Name"])
            if task_name:
                center_x = start_x + width / 2
                center_y = y + BAR_MARGIN + bar_height / 2
                # 表示内容（省略・非表示・2行化）はバーの実際の画面上サイズ
                # （拡縮率次第で変わる）に応じてFrozenGanttPane._sync_panesが
                # その都度決めるため、ここでは仮の位置に元のタスク名をそのまま
                # 置いておくだけにする。
                task_label = _add_fixed_size_label(
                    body_scene, task_name, task_font,
                    (center_x, center_y),
                    # バー（既定0、締切超過は赤枠を隣に隠されないよう1）より
                    # 必ず前面に置く。同じ z だと描画順しだいでバーがラベルを
                    # 覆ってしまう。
                    z_value=_TASK_LABEL_Z,
                )
                body_scene.gantt_task_labels.append(
                    (task_label, center_x, center_y, width, bar_height, task_name, task_font)
                )

        column_boundary = column_scene.addLine(0, y_bottom + JOB_GAP / 2, column_stub_right, y_bottom + JOB_GAP / 2,
                                                 QPen(_GRID_COLOR, 1))
        column_boundary.setZValue(-2)
        body_boundary = body_scene.addLine(LEFT_MARGIN, y_bottom + JOB_GAP / 2, chart_right, y_bottom + JOB_GAP / 2,
                                            QPen(_GRID_COLOR, 1))
        body_boundary.setZValue(-2)

    # -- 各ペインの担当範囲（スクロールバー可動域の基準にする矩形） -------------------
    # ヘッダー・左列は本体との共有軸（ヘッダーなら横、左列なら縦）の範囲・原点を
    # 本体と揃えることで、GraphicsView間のスクロールバー可動域を一致させ、
    # スクロール位置をそのままコピーするだけでズレなく同期できるようにする。
    header_scene.gantt_header_rect = QRectF(
        LEFT_MARGIN, 0, chart_right - LEFT_MARGIN + 20, header_stub_bottom,
    )
    column_scene.gantt_column_rect = QRectF(
        0, TOP_MARGIN, column_stub_right, chart_bottom - TOP_MARGIN + 20,
    )
    body_scene.gantt_body_rect = QRectF(
        LEFT_MARGIN, TOP_MARGIN, chart_right - LEFT_MARGIN + 20, chart_bottom - TOP_MARGIN + 20,
    )
    return GanttScenes(header=header_scene, column=column_scene, body=body_scene)
