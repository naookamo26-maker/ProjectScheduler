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

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGridLayout,
    QToolTip,
    QWidget,
)

from gui.gantt_edit import to_date
from i18n import tr

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
# 見出しのマイルストーン名（「今日」を含む）同士の最小の間隔(px)
_MILESTONE_LABEL_GAP_PX = 6
# 見出しのラベルが重なるときに残す優先度（大きいほど優先）。締切（マイルストーン）が
# 最も大事で、次に「今日」。開発開始日はチャートの左端にあり、線だけでも分かる。
_LABEL_PRIORITY_MILESTONE = 2
_LABEL_PRIORITY_TODAY = 1
_LABEL_PRIORITY_PROJECT_START = 0
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
# _center_task_labelsのビューポート判定に見込む余白(px)。ちょうど画面端で
# 出入りするラベルがパンのたびに省略表示とフル表示を行き来してちらつかない
# ようにする（実際に見えている範囲より少しだけ広く処理対象にする）。
_TASK_LABEL_VIEWPORT_MARGIN_PX = 64

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

# -- タスクの編集（docs/roadmap.md §9）用の見た目 ----------------------------------
# 手動ピン（開始固定日）の印。バー左上に置く、📍と同じ形のピン（赤い丸の頭と針。
# 画面上で一定の大きさ）。絵文字の文字として描くと、フォントの有無で□になる環境が
# あり、この大きさでは潰れて読めないため、図形として描く。
_PIN_HEAD_COLOR = QColor("#d93025")
_PIN_HEAD_BORDER = QColor("#7a1712")
_PIN_NEEDLE_COLOR = QColor("#3c4043")
_PIN_HEAD_RADIUS_PX = 4.5
_PIN_NEEDLE_PX = 5
_PIN_MARKER_PX = 12  # この大きさ（幅・高さ）に満たないバーには描かない
# 📍を描いたバーでは、タスク名をこの幅（画面px）だけ右に避けて置く（重ならないように）
_PIN_LABEL_RESERVE_PX = 3 + _PIN_HEAD_RADIUS_PX * 2 + 3
# 📍の下端（バー上端からの画面px）。バーの縦に余裕があり、中央に置いた文字がこれより
# 下に収まるなら、横に避けずに中央のまま置く（拡大時に無駄に折り返さないように）。
_PIN_BOTTOM_PX = 2 + _PIN_HEAD_RADIUS_PX * 2 + _PIN_NEEDLE_PX + 1
# 強調枠（締切超過・固定どおりに置けない）の内側に入れる白い枠。バーの塗りが枠と
# 同系色（カットシーンの赤いバー等）でも、枠が塗りに埋もれないようにする。
_EMPHASIS_INNER_COLOR = QColor("#ffffff")
_EMPHASIS_INNER_WIDTH = 1.5
# 編集の結果として動いたバーの一時的な強調（黄色の内枠）。
_MOVED_HIGHLIGHT_COLOR = QColor(255, 196, 0, 230)
_MOVED_HIGHLIGHT_WIDTH = 3
# ドラッグ中の影。半透明の塗り＋濃い色の破線の枠にし、他のバーより前面に描く
# （前面に出すだけだと実体のバーと見分けがつかないため、下のバーが透けて見えるようにする）。
_GHOST_FILL_ALPHA = 115
_GHOST_BORDER_COLOR = QColor("#1f3b8c")
_GHOST_Z = 100
# バーの右端をこの幅（画面px）以内で掴むと、移動ではなく期間の伸縮になる。
_RESIZE_HANDLE_PX = 6
# 確定済みのファイルで、まだ確定していないタスク（確定後に足したジョブ等）の斜線。
# 下端の細い帯（全体の3割）にはチームの色をそのまま残す。
_UNCONFIRMED_VEIL = QColor(255, 255, 255, 165)
_UNCONFIRMED_HATCH = QColor(110, 110, 110, 150)
_UNCONFIRMED_TEAM_BAND_RATIO = 0.3
# 変更案で、確定していた位置を示す細線（バーの下）。
_BASELINE_COLOR = QColor("#6f6f6f")
_BASELINE_HEIGHT = 3


class TaskBarItem(QGraphicsPathItem):
    """ガントのタスクバー。どのジョブのどのタスクかを持ち、ガント上での編集
    （選択・ドラッグ・編集ウィンドウ）の対象を特定できるようにする。

    key は計算結果の文字列IDの組 (Job_ID, Task_ID)。start/end は datetime.date
    で、end は exclusive（計算結果の End_Date と同じ）。

    手動ピンの印・強調枠の白い内枠・動いたバーの強調は、ズームに関わらず
    画面上で一定の大きさにしたいので、paint() の中で画面座標に切り替えて描く。
    いずれもバーの内側に収まるように描く（バーの外にはみ出すと boundingRect の
    外になり、再描画で消え残るため）。"""

    def __init__(self, path, bar_rect, job_key, task_key, team_key, start, end,
                 pinned=False, emphasized=False, emphasis_width=0):
        super().__init__(path)
        self.bar_rect = bar_rect
        self.job_key = job_key
        self.task_key = task_key
        self.team_key = team_key
        self.start = start
        self.end = end
        self.pinned = pinned
        self.emphasized = emphasized
        self._emphasis_width = emphasis_width
        self.highlighted = False
        # 確定済みのファイルで、まだ確定行を持たないタスク（§8-9）
        self.unconfirmed = False

    @property
    def key(self):
        return (self.job_key, self.task_key)

    def shows_pin(self, width_px, height_px):
        """画面上のバーの大きさで、📍の印を描くかどうか（ラベルの配置と揃える）。"""
        return self.pinned and width_px >= _PIN_MARKER_PX and height_px >= _PIN_MARKER_PX

    def set_highlighted(self, value):
        if self.highlighted != value:
            self.highlighted = value
            self.update()

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if not (self.pinned or self.emphasized or self.highlighted or self.unconfirmed):
            return
        painter.save()
        rect = painter.worldTransform().mapRect(self.bar_rect)
        painter.resetTransform()
        if self.unconfirmed:
            veiled = QRectF(rect)
            veiled.setHeight(rect.height() * (1 - _UNCONFIRMED_TEAM_BAND_RATIO))
            painter.fillRect(veiled, _UNCONFIRMED_VEIL)
            painter.fillRect(veiled, QBrush(_UNCONFIRMED_HATCH, Qt.BDiagPattern))
        painter.setBrush(Qt.NoBrush)
        if self.emphasized:
            inset = self._emphasis_width / 2 + _EMPHASIS_INNER_WIDTH / 2
            inner = rect.adjusted(inset, inset, -inset, -inset)
            if inner.width() > 2 and inner.height() > 2:
                painter.setPen(QPen(_EMPHASIS_INNER_COLOR, _EMPHASIS_INNER_WIDTH))
                painter.drawRect(inner)
        if self.highlighted:
            inset = _MOVED_HIGHLIGHT_WIDTH / 2
            painter.setPen(QPen(_MOVED_HIGHLIGHT_COLOR, _MOVED_HIGHLIGHT_WIDTH))
            painter.drawRect(rect.adjusted(inset, inset, -inset, -inset))
        if self.shows_pin(rect.width(), rect.height()):
            cx = rect.left() + 3 + _PIN_HEAD_RADIUS_PX
            cy = rect.top() + 2 + _PIN_HEAD_RADIUS_PX
            painter.setRenderHint(QPainter.Antialiasing, True)
            needle = QPen(_PIN_NEEDLE_COLOR, 1.6)
            needle.setCapStyle(Qt.RoundCap)
            painter.setPen(needle)
            painter.drawLine(QPointF(cx, cy), QPointF(cx, cy + _PIN_HEAD_RADIUS_PX + _PIN_NEEDLE_PX))
            painter.setPen(QPen(_PIN_HEAD_BORDER, 1))
            painter.setBrush(QBrush(_PIN_HEAD_COLOR))
            painter.drawEllipse(QPointF(cx, cy), _PIN_HEAD_RADIUS_PX, _PIN_HEAD_RADIUS_PX)
        painter.restore()


def set_bar_baseline(scene, bar, start, end):
    """変更案で動いたバーの下に、確定していた位置（start〜end, end は exclusive）を
    細線で重ねる。他のバーより前面に描く（§8-9）。"""
    x0 = _scene_x_of(scene, start)
    x1 = _scene_x_of(scene, end)
    item = scene.addRect(
        QRectF(x0, bar.bar_rect.bottom() + 1, max(x1 - x0, 2), _BASELINE_HEIGHT),
        QPen(Qt.NoPen), QBrush(_BASELINE_COLOR),
    )
    item.setZValue(_GHOST_Z - 1)
    return item


def _scene_x_of(scene, d):
    """本体シーンの x 座標（build_gantt_scenes の x_of と同じ変換）。"""
    return LEFT_MARGIN + (d - scene.gantt_axis_start).days * DAY_WIDTH


def _task_bars(scene):
    return [item for item in scene.selectedItems() if isinstance(item, TaskBarItem)] \
        if scene is not None else []


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
    # -- タスクの編集（docs/roadmap.md §9）。実際のDB書き込みは gui/tab_gantt.py が行う --
    # [(key, 新しい開始日), ...] と、ずらした営業日数
    moveRequested = Signal(object, int)
    # key と新しい日数（営業日）
    resizeRequested = Signal(object, int)
    # ダブルクリック（編集ウィンドウを開く）
    editRequested = Signal()
    # 右クリック（画面座標）。対象のバーは選択済みにしてから発火する
    contextMenuRequested = Signal(object)
    # 本体での左クリック（動いたバーの一時的な強調を「次の操作まで」で消すため）
    pressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(self.renderHints())
        # 文字色は常に黒固定（QGraphicsSimpleTextItemの既定）で描画しているため、
        # OSがダークモードだと既定の（ダークな）ビュー背景に文字が埋もれて
        # 読めなくなる。この独自キャンバスはOSのテーマに関わらず常に明るい
        # 背景で描くようにし、文字色との組み合わせを固定して視認性を保つ。
        self.setBackgroundBrush(QBrush(_PANE_BG))
        self.setDragMode(QGraphicsView.RubberBandDrag)
        # QGraphicsViewは既定でacceptDrops()がTrueになっており、プロジェクト
        # ファイル（.pschedule）をこのビュー上にドラッグ&ドロップしても
        # シーンが受け取らないまま素通りせず、MainWindow.dropEvent（ウィンドウ
        # 全体でのファイルオープン）まで伝播しない。このビュー自体はファイルの
        # ドロップを扱わないため、明示的に無効化してMainWindow側へ委ねる。
        self.setAcceptDrops(False)
        self._panning = False
        self._pan_last_pos = None
        # 編集の設定（gui/tab_gantt.py が設定する）。edit_enabled が偽の間は
        # 従来どおり表示専用として振る舞う。
        self.edit_enabled = False
        self.drag_modifier = Qt.ShiftModifier
        self.calendar = None            # gui/gantt_edit.WorkDayCalendar
        self.move_hint_provider = None  # (key, 新しい開始日) -> 注意書き or None
        self._drag = None
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
        if event.button() == Qt.RightButton:
            # QGraphicsView は右ボタンの押下でもラバーバンド選択を始めて選択を解除する
            # ため、複数選択してから右クリックすると1つしか残らなかった。右クリックでの
            # 選択の扱いは contextMenuEvent に任せ、押下はここで止める。
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self.pressed.emit()
            if self._try_start_drag(event):
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
        if self._drag is not None:
            self._update_drag(event)
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
        if event.button() == Qt.LeftButton and self._drag is not None:
            self._finish_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.edit_enabled and event.button() == Qt.LeftButton:
            item = self._bar_at(event.position().toPoint())
            if item is not None:
                if not item.isSelected():
                    self.scene().clearSelection()
                    item.setSelected(True)
                self.editRequested.emit()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        if not self.edit_enabled or self.scene() is None:
            super().contextMenuEvent(event)
            return
        # QContextMenuEvent はマウスのイベント（QSinglePointEvent）と違い position() を
        # 持たない（pos() だけ）。position() と書くと右クリックのたびに例外になり、
        # メニューが一度も開かなかった。
        item = self._bar_at(event.pos())
        if item is not None and not item.isSelected():
            self.scene().clearSelection()
            item.setSelected(True)
        self.contextMenuRequested.emit(event.globalPos())
        event.accept()

    # -- ドラッグでの移動・伸縮 -------------------------------------------------------

    def _bar_at(self, view_pos):
        for item in self.items(view_pos):
            if isinstance(item, TaskBarItem):
                return item
        return None

    def _try_start_drag(self, event):
        """指定キー（既定 Shift）を押しながらバーを掴んだら、ドラッグを始める。
        キーを押していなければ何もしない（クリックは従来どおり選択だけ）。"""
        if not self.edit_enabled or self.calendar is None:
            return False
        if not (event.modifiers() & self.drag_modifier):
            return False
        anchor = self._bar_at(event.position().toPoint())
        if anchor is None:
            return False
        scene = self.scene()
        if not anchor.isSelected():
            if not (event.modifiers() & Qt.ControlModifier):
                scene.clearSelection()
            anchor.setSelected(True)
        right_edge = self.mapFromScene(anchor.bar_rect.topRight()).x()
        resize = abs(event.position().toPoint().x() - right_edge) <= _RESIZE_HANDLE_PX
        bars = [anchor] if resize else _task_bars(scene)
        ghosts = {}
        for bar in bars:
            color = QColor(bar.brush().color())
            color.setAlpha(_GHOST_FILL_ALPHA)
            ghost = QGraphicsPathItem(bar.path())
            ghost.setBrush(QBrush(color))
            pen = QPen(_GHOST_BORDER_COLOR, 1.6, Qt.DashLine)
            pen.setCosmetic(True)
            ghost.setPen(pen)
            ghost.setZValue(_GHOST_Z)
            scene.addItem(ghost)
            ghosts[bar.key] = ghost
        self._drag = {
            "anchor": anchor,
            "resize": resize,
            "bars": bars,
            "ghosts": ghosts,
            "press_x": self.mapToScene(event.position().toPoint()).x(),
            # 各バーの長さ（営業日）。移動してもこの日数を保つ
            "days": {b.key: max(1, self.calendar.count(b.start, b.end, b.team_key)) for b in bars},
            "shift": 0,
            "new_days": None,
            "targets": {},
        }
        self.setCursor(Qt.SizeHorCursor if resize else Qt.ClosedHandCursor)
        return True

    def _update_drag(self, event):
        drag = self._drag
        cal = self.calendar
        anchor = drag["anchor"]
        scene = self.scene()
        dx_days = round((self.mapToScene(event.position().toPoint()).x() - drag["press_x"]) / DAY_WIDTH)
        if drag["resize"]:
            last_day = anchor.end - timedelta(days=1) + timedelta(days=dx_days)
            new_days = max(1, cal.count(anchor.start, last_day + timedelta(days=1), anchor.team_key))
            drag["new_days"] = new_days
            new_end = cal.end_exclusive(anchor.start, new_days, anchor.team_key)
            self._place_ghost(drag["ghosts"][anchor.key], anchor, anchor.start, new_end)
            text = tr("{new_days}営業日（{end:%m/%d} まで）", new_days=new_days, end=new_end - timedelta(days=1))
        else:
            shift = cal.diff(anchor.start, anchor.start + timedelta(days=dx_days), anchor.team_key)
            drag["shift"] = shift
            targets = {}
            for bar in drag["bars"]:
                new_start = cal.shift(bar.start, shift, bar.team_key)
                new_end = cal.end_exclusive(new_start, drag["days"][bar.key], bar.team_key)
                targets[bar.key] = new_start
                self._place_ghost(drag["ghosts"][bar.key], bar, new_start, new_end)
            drag["targets"] = targets
            sign = "+" if shift >= 0 else "−"
            if len(drag["bars"]) == 1:
                text = tr(
                    "{start:%m/%d} → {new_start:%m/%d}（{sign}{days}営業日）",
                    start=anchor.start, new_start=targets[anchor.key], sign=sign, days=abs(shift),
                )
            else:
                text = tr("{n}件を {sign}{days}営業日", n=len(drag["bars"]), sign=sign, days=abs(shift))
            if self.move_hint_provider is not None:
                hints = [h for h in (self.move_hint_provider(k, d) for k, d in targets.items()) if h]
                if hints:
                    text += "\n⚠ " + hints[0] + (tr("（ほか{n}件）", n=len(hints) - 1) if len(hints) > 1 else "")
        QToolTip.showText(event.globalPosition().toPoint(), text, self)
        scene.update()

    def _place_ghost(self, ghost, bar, new_start, new_end):
        scene = self.scene()
        x0 = _scene_x_of(scene, new_start)
        x1 = _scene_x_of(scene, new_end)
        rect = QRectF(x0, bar.bar_rect.top(), max(x1 - x0, 2), bar.bar_rect.height())
        path = QPainterPath()
        path.addRoundedRect(rect, BAR_CORNER_RADIUS, BAR_CORNER_RADIUS)
        ghost.setPath(path)

    def _finish_drag(self):
        drag, self._drag = self._drag, None
        scene = self.scene()
        for ghost in drag["ghosts"].values():
            scene.removeItem(ghost)
        QToolTip.hideText()
        self.setCursor(Qt.ArrowCursor)
        anchor = drag["anchor"]
        if drag["resize"]:
            original = drag["days"][anchor.key]
            if drag["new_days"] is not None and drag["new_days"] != original:
                self.resizeRequested.emit(anchor.key, drag["new_days"])
        elif drag["shift"] != 0:
            self.moveRequested.emit(sorted(drag["targets"].items()), drag["shift"])


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
        # 本体の枠・スクロールバーに合わせて表示部分の外に余白を取る
        # （FrozenGanttPane._align_frozen_viewports）。余白も同じ背景色で塗る。
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), _PANE_BG)
        self.setPalette(pal)
        # GanttGraphicsView と同じ理由（ファイルドロップをMainWindowへ
        # 伝播させるため）で無効化する。
        self.setAcceptDrops(False)


class GanttHeaderView(_FrozenPaneView):
    """日付軸・マイルストーンの行。本体と横方向の縮尺／スクロール位置だけ
    同期し、縦方向は常に等倍で固定表示する。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(TOP_MARGIN + _PANE_PADDING)


class GanttColumnView(_FrozenPaneView):
    """項目名（ジョブ名）の列。本体と縦方向の縮尺／スクロール位置だけ同期し、
    横方向は常に等倍で固定表示する。

    ジョブ名をクリックすると jobClicked（ジョブの文字列ID、Ctrlを押していたか）を
    発火する（そのジョブの全タスクを選ぶため。docs/roadmap.md §9）。表示専用の
    ペインなので setInteractive(False) のままにし、シーンへは渡さずここで拾う。"""

    jobClicked = Signal(object, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(LEFT_MARGIN + _PANE_PADDING)

    def mousePressEvent(self, event):
        scene = self.scene()
        if event.button() == Qt.LeftButton and scene is not None:
            y = self.mapToScene(event.position().toPoint()).y()
            for y_top, y_bottom, job_key in getattr(scene, "gantt_job_rows", ()):
                if y_top <= y < y_bottom:
                    self.jobClicked.emit(job_key, bool(event.modifiers() & Qt.ControlModifier))
                    event.accept()
                    return
        super().mousePressEvent(event)


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
        # 本体の表示部分の大きさ・位置は、ウィンドウの大きさやスクロールバーの
        # 表示／非表示で変わる。そのたびにヘッダー・左列を合わせ直す。
        self.body.viewport().installEventFilter(self)
        self.body.fitAllRequested.connect(self.fit_all)
        self.body.fitSelectedRequested.connect(self.fit_selected)
        self.column.jobClicked.connect(self._select_job)

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

    # -- 選択と表示位置（編集後の再計算・Undo/Redo をまたいで保つ） -----------------

    def bars(self):
        """{(Job_ID, Task_ID): TaskBarItem}。シーンが無ければ空。"""
        scene = self.body.scene()
        return getattr(scene, "gantt_bars", {}) if scene is not None else {}

    def selected_keys(self):
        return [bar.key for bar in _task_bars(self.body.scene())]

    def select_keys(self, keys, ensure_visible=False):
        """keys のバーを選択する（表示されていないものは無視）。"""
        scene = self.body.scene()
        if scene is None:
            return
        bars = self.bars()
        scene.clearSelection()
        chosen = [bars[k] for k in keys if k in bars]
        for bar in chosen:
            bar.setSelected(True)
        if ensure_visible and chosen:
            rect = QRectF()
            for bar in chosen:
                rect = rect.united(bar.sceneBoundingRect())
            self.body.ensureVisible(rect, 40, 40)

    def _select_job(self, job_key, add):
        scene = self.body.scene()
        if scene is None:
            return
        if not add:
            scene.clearSelection()
        for key, bar in self.bars().items():
            if key[0] == job_key:
                bar.setSelected(True)

    def view_state(self):
        """本体の縮尺とスクロール位置。restore_view_state() で戻す。"""
        transform = self.body.transform()
        return (transform.m11(), transform.m22(),
                self.body.horizontalScrollBar().value(), self.body.verticalScrollBar().value())

    def restore_view_state(self, state):
        sx, sy, h, v = state
        self.body.setTransform(QTransform().scale(sx, sy))
        self.body._clamp_scale()
        self.body.horizontalScrollBar().setValue(h)
        self.body.verticalScrollBar().setValue(v)
        self._sync_panes()

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

    def eventFilter(self, obj, event):
        if obj is self.body.viewport() and event.type() in (QEvent.Resize, QEvent.Move):
            self._sync_panes()
        return super().eventFilter(obj, event)

    def _align_frozen_viewports(self):
        """ヘッダーの表示部分を本体の表示部分と同じ横位置・横幅に、左列の表示部分を
        同じ縦位置・縦幅にそろえる。本体には枠とスクロールバーがあるため、そのままでは
        ヘッダー・左列の方が広く、スクロールできる範囲が食い違う。すると端まで
        スクロールしたときに、本体だけがスクロールバーの幅だけ先へ進み、日付・
        マイルストーンの線や行の位置がずれる。"""
        viewport = self.body.viewport()
        origin = viewport.mapTo(self, QPoint(0, 0))
        header = self.header.geometry()
        left = origin.x() - header.x()
        right = header.x() + header.width() - (origin.x() + viewport.width())
        self.header.setViewportMargins(max(0, left), 0, max(0, right), 0)
        column = self.column.geometry()
        top = origin.y() - column.y()
        bottom = column.y() + column.height() - (origin.y() + viewport.height())
        self.column.setViewportMargins(0, max(0, top), 0, max(0, bottom))

    def _sync_panes(self):
        self._align_frozen_viewports()
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
        self._layout_job_labels(sy)

    def _layout_job_labels(self, sy):
        """左列のジョブ名（と色見本）を、その行の中央に揃え、上の名前と重なるものを
        隠す。

        ジョブ名は等倍（一定ピクセル数）で描く一方、行の高さは縦の拡縮率（sy）で
        変わる。全体表示などで縦に縮小すると行が文字より低くなり、名前同士が
        重なって読めなくなっていた。上から順に、直前に表示した名前と重なるものを
        隠す（拡大すれば、すべての名前が現れる）——日付軸の目盛りを縮小時に
        間引くのと同じ考え方（_update_axis_density）。"""
        scene = self.column.scene()
        if scene is None or sy <= 0:
            return
        next_free_px = None
        for label, swatch, center_y in getattr(scene, "gantt_job_labels", []):
            height_px = label.boundingRect().height()
            top_px = center_y * sy - height_px / 2
            visible = next_free_px is None or top_px >= next_free_px
            label.setVisible(visible)
            swatch.setVisible(visible)
            if not visible:
                continue
            next_free_px = top_px + height_px
            label.setPos(label.x(), center_y - (height_px / 2) / sy)
            swatch.setPos(swatch.x(), center_y - (swatch.rect().height() / 2) / sy)

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
                aux_pen = QPen(_AUX_GRID_COLOR, 1)
                aux_pen.setCosmetic(True)  # ズーム（水平方向の拡縮）で線が太くならないようにする
                header_line.setPen(aux_pen)
                body_line.setPen(aux_pen)
                label.setVisible(False)
                continue
            header_line.setVisible(True)
            body_line.setVisible(True)
            main_pen = QPen(_GRID_COLOR, 1)
            main_pen.setCosmetic(True)  # ズーム（水平方向の拡縮）で線が太くならないようにする
            header_line.setPen(main_pen)
            body_line.setPen(main_pen)
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
        # 現在ビューポートに映っている範囲（シーン座標、余白付き）。この外に
        # あるラベルは省略・2行化などの本文処理をせず、非表示化だけして
        # 早期に打ち切る。ここをスキップしないと、大規模プロジェクト
        # （数千〜数万タスク）でラベルが読めるほど拡大した際、画面外の
        # タスクぶんまで含めた全件のテキスト整形をパン・ズームのたびに
        # 行うことになり、操作が重くなる（バウンディングボックスの比較
        # 自体は全件行っても軽いため、判定はこの後も全ラベル分ループする）。
        visible_rect = self.body.mapToScene(self.body.viewport().rect()).boundingRect()
        margin_x = _TASK_LABEL_VIEWPORT_MARGIN_PX / sx
        margin_y = _TASK_LABEL_VIEWPORT_MARGIN_PX / sy
        visible_left = visible_rect.left() - margin_x
        visible_right = visible_rect.right() + margin_x
        visible_top = visible_rect.top() - margin_y
        visible_bottom = visible_rect.bottom() + margin_y
        # 全タスクラベルが同じフォント（task_font）を共有しているのが通常なので、
        # QFontMetricsをラベルごとに作り直さず使い回す（画面内に映っている分
        # だけとはいえ、数百件規模になりうるため、地味だが効く最適化）。
        metrics_cache = {}
        for label, center_x, center_y, bar_width, bar_height, full_text, font, bar in \
                getattr(scene, "gantt_task_labels", []):
            half_w, half_h = bar_width / 2, bar_height / 2
            if (center_x + half_w < visible_left or center_x - half_w > visible_right
                    or center_y + half_h < visible_top or center_y - half_h > visible_bottom):
                if label.isVisible():
                    label.setVisible(False)
                continue

            metrics = metrics_cache.get(id(font))
            if metrics is None:
                metrics = QFontMetrics(font)
                metrics_cache[id(font)] = metrics
            line_height = metrics.height()
            avail_w = bar_width * sx - _TASK_LABEL_H_MARGIN_PX
            avail_h = bar_height * sy - _TASK_LABEL_V_MARGIN_PX
            # 📍を描くバーは、その分だけ左を空けて残りの幅の中央に置く
            # （印と文字が重ならないように）。
            shift_px = 0
            bar_h_px = bar_height * sy
            if bar is not None and bar.shows_pin(bar_width * sx, bar_h_px):
                # 2行になっても📍の下に収まるほど縦に余裕があれば、避けない
                text_h_max = line_height * 2 + _TASK_LABEL_LINE_GAP_PX
                if (bar_h_px - text_h_max) / 2 < _PIN_BOTTOM_PX:
                    avail_w -= _PIN_LABEL_RESERVE_PX
                    shift_px = _PIN_LABEL_RESERVE_PX / 2

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
            label.setPos(center_x + (shift_px - width_px / 2) / sx, center_y - (height_px / 2) / sy)

    def _center_milestone_labels(self, sx):
        """マイルストーン（開発開始日・「今日」を含む）のラベルを、その縦線を中心に
        左右均等になるよう配置し直す。ItemIgnoresTransformationsを立てた項目の
        setPos()はシーン座標系のままなので、画面上で「中央揃え」を保つオフセット
        （ラベル幅の半分）は、現在の横方向の拡縮率(sx)で割ってシーン座標に変換する
        必要がある。

        あわせて次の2つを避ける。
        - はみ出し: 軸の両端付近では中央揃えのままだとラベルがチャート外へ
          はみ出すため、ヘッダーの表示範囲（gantt_header_rect）に収める。画面に
          一部でもかかるラベルは、今見えている範囲にも収める——全体表示でも
          本体は数十ピクセル横にスクロールできる（_clamp_scale）ため、チャート
          全体の範囲に収めるだけでは右端のラベルが切れて見えていた。
        - 重なり: 締切の近いマイルストーン同士や、開発開始日と「今日」は同じ高さに
          並べると文字が重なって読めない（「プロジ今日ト開始」のように）。優先度の
          高いもの（マイルストーン → 今日 → 開発開始日）から置き、既に置いたものと
          重なるラベルは隣へずらす。ずらすと自分の縦線を指さなくなる（線がラベルの
          幅から外れる）なら隠す（拡大すれば現れる）。"""
        scene = self.header.scene()
        if scene is None or sx <= 0:
            return
        header_rect = getattr(scene, "gantt_header_rect", None)
        visible = self.header.mapToScene(self.header.viewport().rect()).boundingRect()
        gap = _MILESTONE_LABEL_GAP_PX / sx
        epsilon = 0.5 / sx
        placed = []  # [(左端, 右端)]（シーン座標）

        def overlaps(left, right):
            return any(left < p_right + gap and right + gap > p_left for p_left, p_right in placed)

        entries = sorted(getattr(scene, "gantt_milestone_labels", []), key=lambda e: (-e[2], e[1]))
        for label, line_x, _priority in entries:
            width = label.boundingRect().width() / sx
            preferred = line_x - width / 2
            low, high = (header_rect.left(), header_rect.right()) if header_rect is not None \
                else (float("-inf"), float("inf"))
            preferred = max(low, min(preferred, max(low, high - width)))
            on_screen = preferred < visible.right() and preferred + width > visible.left()
            if on_screen:
                low, high = max(low, visible.left()), min(high, visible.right())
                preferred = max(low, min(preferred, max(low, high - width)))
            candidates = [preferred]
            for p_left, p_right in placed:
                candidates += [p_right + gap, p_left - gap - width]
            chosen = None
            for left in sorted(candidates, key=lambda c: abs(c - preferred)):
                if left < low - epsilon or left + width > high + epsilon:
                    continue
                if left != preferred and not left - epsilon <= line_x <= left + width + epsilon:
                    continue
                if not overlaps(left, left + width):
                    chosen = left
                    break
            if chosen is None:
                label.setVisible(False)
                continue
            label.setVisible(True)
            label.setPos(chosen, label.y())
            placed.append((chosen, chosen + width))

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
    # ガント上での編集（ドラッグ位置⇔日付の変換、キーからバーを引く）用
    body_scene.gantt_axis_start = to_date(axis_start)
    body_scene.gantt_bars = {}
    column_scene.gantt_job_rows = []  # (y_top, y_bottom, Job_ID) ジョブ名クリックでの選択用
    start_pins = display.get("start_pins") or {}

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
        week_pen = QPen(_GRID_COLOR, 1)
        week_pen.setCosmetic(True)  # ズーム（水平方向の拡縮）で線が太くならないようにする
        header_line = header_scene.addLine(x, TOP_MARGIN - _GRID_TOP_OFFSET, x, header_stub_bottom, week_pen)
        header_line.setZValue(-2)
        body_line = body_scene.addLine(x, TOP_MARGIN, x, chart_bottom, week_pen)
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
            day_pen = QPen(_AUX_GRID_COLOR, 1)
            day_pen.setCosmetic(True)  # ズーム（水平方向の拡縮）で線が太くならないようにする
            header_line = header_scene.addLine(x, TOP_MARGIN - _GRID_TOP_OFFSET, x, header_stub_bottom, day_pen)
            header_line.setZValue(-3)
            header_line.setVisible(False)
            body_line = body_scene.addLine(x, TOP_MARGIN, x, chart_bottom, day_pen)
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
        text = tr("{year}年", year=year_cursor.year)
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
            year_pen = QPen(_YEAR_GRID_COLOR, 1)
            year_pen.setCosmetic(True)  # ズーム（水平方向の拡縮）で線が太くならないようにする
            header_line = header_scene.addLine(x, row_top, x, row_bottom, year_pen)
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
        # 重なるときにどれを残すかの優先度（FrozenGanttPane._center_milestone_labels）
        priority = _LABEL_PRIORITY_PROJECT_START if marker_id == "PROJECT_START" else _LABEL_PRIORITY_MILESTONE
        header_scene.gantt_milestone_labels.append((label, x, priority))

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
            header_scene, tr("今日"), job_font,
            (x, TOP_MARGIN - _MILESTONE_LABEL_OFFSET),
            brush=QBrush(_TODAY_LINE_COLOR), z_value=2,
        )
        header_scene.gantt_milestone_labels.append((label, x, _LABEL_PRIORITY_TODAY))

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
    column_scene.gantt_job_labels = []
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
        swatch.setToolTip(tr("ワークフロー: {workflow}", workflow=workflow_names.get(job_workflow_id, job_workflow_id)))

        job_label = _add_fixed_size_label(
            column_scene,
            _elide_text(job_name, job_font, LEFT_MARGIN - 12 - _JOB_SWATCH_WIDTH - _JOB_SWATCH_GAP),
            job_font,
            (4 + _JOB_SWATCH_WIDTH + _JOB_SWATCH_GAP, (y_top + y_bottom) / 2 - 8),
        )
        # 縦に縮小して行が文字より低くなると、等倍描画のジョブ名同士が重なって
        # 読めなくなる。表示倍率が分かるタイミング（FrozenGanttPane._sync_panes）で
        # 行の中央へ揃え直し、重なる分を間引くため、参照を残しておく。
        column_scene.gantt_job_labels.append((job_label, swatch, (y_top + y_bottom) / 2))

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

            # Task_ID を持たない表（テスト等で手組みしたもの）も描けるようにする
            key = (r["Job_ID"], r.get("Task_ID"))
            emphasis_width = 0
            if overrun_days > 0:
                emphasis_width = _OVERRUN_BORDER_WIDTH
            elif constraint_violation:
                emphasis_width = _CONSTRAINT_BORDER_WIDTH
            rect = TaskBarItem(
                bar_path,
                QRectF(start_x, y + BAR_MARGIN, width, bar_height),
                job_key=key[0], task_key=key[1], team_key=r["Team_ID"],
                start=to_date(r["Start_Date"]), end=to_date(r["End_Date"]),
                pinned=key in start_pins,
                emphasized=emphasis_width > 0, emphasis_width=emphasis_width,
            )
            body_scene.gantt_bars[key] = rect
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
                tr(
                    "{job} / {task}\nワークフロー: {workflow}\nチーム: {team}\n{start} 〜 {end}",
                    job=job_name, task=r["Task_Name"], workflow=workflow_name, team=team_name,
                    start=r["Start_Date"].strftime("%Y-%m-%d"), end=r["End_Date"].strftime("%Y-%m-%d"),
                )
                + (tr("\n⚠ マイルストーンの締切を{overrun_days}日超過", overrun_days=overrun_days) if overrun_days > 0 else "")
                + (f'\n⚠ {constraint_violation}' if constraint_violation else "")
                + (tr("\n※リソース制約により前倒し") if r["Resource_Adjusted"] else "")
                + (tr("\n📍 開始固定日: {date:%Y-%m-%d}", date=start_pins[key]) if key in start_pins else "")
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
                    (task_label, center_x, center_y, width, bar_height, task_name, task_font, rect)
                )

        column_scene.gantt_job_rows.append((y_top - JOB_GAP / 2, y_bottom + JOB_GAP / 2, job_id))
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
