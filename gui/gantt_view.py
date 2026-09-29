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

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPolygonF,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGridLayout,
    QLabel,
    QStyle,
    QStyleOptionGraphicsItem,
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
# 縦に縮小して行が文字より低くなったら、左列のジョブ名（と色見本）を全行そろって
# 縮小する。この倍率を下回るほど縮めないと収まらないときは、読めないので全行とも隠す。
_JOB_LABEL_MIN_SCALE = 0.6
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
# 日付の目盛りラベル同士の最小の間隔(px)。これより詰まるラベルは隠す（月初めが続く
# 軸の左端などで「8 9」のように重ならないように）。
_TICK_LABEL_GAP_PX = 4
# ホイール1ノッチ（angleDelta 120）あたりの拡大率。タッチパッドの細かいスクロール
# （1回あたり数単位）は、その量に比例して拡大する（1回ごとに1ノッチ分拡大すると、
# 指を少し動かしただけで何倍にもなっていた）。
_WHEEL_ZOOM_STEP = 1.15
_WHEEL_NOTCH = 120
# 横ホイール（チルト・横スワイプ）1ノッチあたりの横スクロール量(px)
_WHEEL_SCROLL_PX = 60
# 「全体表示のまま」とみなす、見えている範囲と内容の幅・高さの差(px)。全体表示の
# 縮尺でも、内容はビューポートより _SCALE_FLOOR_MARGIN_PX だけ大きくしてある。
_FIT_TOLERANCE_PX = 8

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
# 画面上のバーの幅か高さがこれ(px)に満たないときは、通常の黒い枠線を描かない。
# 大きな計画を全体表示すると、1pxの枠線がバーの塗りを覆ってチャートが真っ黒になり、
# チームの色分けが見えなくなっていた（締切超過などの強調枠はそのまま描く）。
_BORDER_MIN_PX = 5
# 選択中のバーの印（バーの内側に沿う青い枠。キャンバスは常に明るいのでライト／ダーク
# 共通）。以前は Qt 既定の細い点線だけで、全体表示ではほとんど見分けられなかった。
# バーが小さいときは枠がバー全体を埋め、バーが青く塗られて見える。
_SELECTION_COLOR = QColor("#1a5fd0")
_SELECTION_WIDTH = 2.5
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
# FrozenGanttPane.view_state() の戻り値。sx/sy は縮尺、h/v はスクロール量、fit_x/fit_y は
# 横／縦が全体表示のままか、left_day は左端の日付（序数。小数あり）、top_job/top_offset は
# 一番上に見えているジョブとその行の上端からのずれ（画面px）。
ViewState = namedtuple("ViewState", ["sx", "sy", "h", "v", "fit_x", "fit_y", "left_day", "top_job", "top_offset"])

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
# ジョブ間の依存の矢印（DependencyArrows）。キャンバスはテーマに関わらず常に明るい
# 背景（_PANE_BG）なので、色はライト／ダークで共通。
DEPENDENCY_OFF = "off"
DEPENDENCY_SELECTED = "selected"
DEPENDENCY_ALL = "all"
DEPENDENCY_MODES = (DEPENDENCY_OFF, DEPENDENCY_SELECTED, DEPENDENCY_ALL)
# 選んでいるタスクにつながらない依存（「すべて」のとき）。半透明・細めにして、
# 選んだタスクの矢印（青）や守られていない依存（赤）より目立たせない。点線は
# 「相手が絞り込みで隠れている」の意味に使っているので使わない
_DEP_COLOR = QColor(55, 65, 85, 110)
_DEP_FOCUS_COLOR = QColor("#1a5fd0")    # 選んでいるタスクにつながる依存
_DEP_BROKEN_COLOR = QColor("#d93025")   # 後のタスクが依存より前に始まっている
_DEP_WIDTH = 1.3
_DEP_FOCUS_WIDTH = 2.2
_DEP_STEP_PX = 6        # バーの端から横に出る長さ（画面px）
_DEP_HEAD_PX = 8        # 矢じりの長さ（画面px）
_DEP_DOT_PX = 7         # 相手が絞り込みで隠れているときの白い丸の直径（画面px）
# バー（締切超過のバーを含む）より前、タスク名より後ろ（線がタスク名の上を横切って
# 読めなくならないように）。確定位置の細線・ドラッグ中の影はさらに前。
_DEP_Z = (_OVERRUN_BAR_Z + _TASK_LABEL_Z) / 2
# 矢じりの向き（dependency_path が返す）
HEAD_RIGHT = "right"
HEAD_DOWN = "down"
HEAD_UP = "up"
# 確定済みのファイルで、まだ確定していないタスク（確定後に足したジョブ等）の斜線。
# 下端の細い帯（全体の3割）にはチームの色をそのまま残す。
_UNCONFIRMED_VEIL = QColor(255, 255, 255, 165)
_UNCONFIRMED_HATCH = QColor(110, 110, 110, 150)
_UNCONFIRMED_TEAM_BAND_RATIO = 0.3
# 変更案で、確定していた位置を示す細線（バーの下）。
_BASELINE_COLOR = QColor("#6f6f6f")
_BASELINE_HEIGHT = 3
# タスクの状態（進捗）。バーの右端を高さいっぱいで区切り、灰色の区画に白い記号を
# 描く（完了 ✔、進行中 ▶。未着手は区切らない）。チームの色は利用者が自由に
# 選べるようにする予定なので、区画は無彩色にする（赤・青・橙・黄は別の意味で
# 使っている）。バーの色・枠線・斜線（未確定）・確定位置の細線（バーの下）・📍
# （左上）のどれとも場所が重ならない。
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
_STATUS_SEGMENT_COLOR = QColor("#6e6e6e")
_STATUS_MARK_COLOR = QColor("#ffffff")
_STATUS_SEGMENT_PX = 16         # 記号を入れる区画の幅（画面px）
_STATUS_MARK_MIN_BAR_PX = 32    # バーがこの幅に満たなければ記号を省き、細い帯だけにする
_STATUS_MARK_MIN_HEIGHT_PX = 10  # バーがこの高さに満たなければ記号を省く
_STATUS_STRIP_PX = 4            # 記号を省いたときの帯の幅（画面px）
_STATUS_MIN_BAR_PX = 6          # バーがこの幅に満たなければ何も描かない


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
                 pinned=False, emphasized=False, emphasis_width=0, status=None):
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
        # タスクの状態（STATUS_IN_PROGRESS / STATUS_DONE / None＝未着手）
        self.status = status if status in (STATUS_IN_PROGRESS, STATUS_DONE) else None

    @property
    def key(self):
        return (self.job_key, self.task_key)

    def shows_pin(self, width_px, height_px):
        """画面上のバーの大きさで、📍の印を描くかどうか（ラベルの配置と揃える）。"""
        return self.pinned and width_px >= _PIN_MARKER_PX and height_px >= _PIN_MARKER_PX

    def status_segment_px(self, width_px, height_px):
        """画面上のバーの大きさで、状態の区画をどの幅（画面px）で描くか（0 なら描かない）。
        ラベルの配置と揃える。"""
        if self.status is None or width_px < _STATUS_MIN_BAR_PX:
            return 0
        if width_px >= _STATUS_MARK_MIN_BAR_PX and height_px >= _STATUS_MARK_MIN_HEIGHT_PX:
            return _STATUS_SEGMENT_PX
        return _STATUS_STRIP_PX

    def _paint_status(self, painter, rect, border_drawn):
        """状態の区画（画面座標で描く）。バーの丸い角に沿って切り取り、区切りの線と
        記号を描き、枠線を区画の上から描き直す（区画で枠線が途切れないように）。"""
        seg_px = self.status_segment_px(rect.width(), rect.height())
        if seg_px == 0:
            return
        device_path = painter.worldTransform().map(self.path())
        seg = QRectF(rect.right() - seg_px, rect.top(), seg_px, rect.height())
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.Antialiasing, True)
        clip = QPainterPath()
        clip.addRect(seg.adjusted(0, -1, 1, 1))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(_STATUS_SEGMENT_COLOR))
        painter.drawPath(device_path.intersected(clip))
        if border_drawn:
            divider = QPen(_NORMAL_BORDER_COLOR, 1)
            divider.setCosmetic(True)
            painter.setPen(divider)
            painter.drawLine(QPointF(seg.left(), seg.top()), QPointF(seg.left(), seg.bottom()))
            painter.save()
            painter.setClipPath(clip)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(self.pen())
            painter.drawPath(device_path)
            painter.restore()
        if seg_px == _STATUS_SEGMENT_PX:
            c = seg.center()
            painter.setBrush(Qt.NoBrush)
            if self.status == STATUS_DONE:
                mark = QPen(_STATUS_MARK_COLOR, 2)
                mark.setCapStyle(Qt.RoundCap)
                mark.setJoinStyle(Qt.RoundJoin)
                painter.setPen(mark)
                painter.drawPolyline(QPolygonF([
                    QPointF(c.x() - 4, c.y()), QPointF(c.x() - 1.2, c.y() + 3), QPointF(c.x() + 4, c.y() - 3.5),
                ]))
            else:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(_STATUS_MARK_COLOR))
                painter.drawPolygon(QPolygonF([
                    QPointF(c.x() - 3, c.y() - 4), QPointF(c.x() - 3, c.y() + 4), QPointF(c.x() + 4, c.y()),
                ]))
        painter.restore()

    def set_highlighted(self, value):
        if self.highlighted != value:
            self.highlighted = value
            self.update()

    def paint(self, painter, option, widget=None):
        # 選択の印は Qt 既定の細い点線ではなく、下で青い枠として描く
        plain = QStyleOptionGraphicsItem(option)
        plain.state &= ~QStyle.State_Selected
        rect = painter.worldTransform().mapRect(self.bar_rect)
        border_drawn = self.emphasized or not (rect.width() < _BORDER_MIN_PX or rect.height() < _BORDER_MIN_PX)
        if not border_drawn:
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.brush())
            painter.drawPath(self.path())
            painter.restore()
        else:
            super().paint(painter, plain, widget)
        selected = self.isSelected()
        if not (self.pinned or self.emphasized or self.highlighted or self.unconfirmed or selected
                or self.status):
            return
        painter.save()
        painter.resetTransform()
        if self.unconfirmed:
            veiled = QRectF(rect)
            veiled.setHeight(rect.height() * (1 - _UNCONFIRMED_TEAM_BAND_RATIO))
            painter.fillRect(veiled, _UNCONFIRMED_VEIL)
            painter.fillRect(veiled, QBrush(_UNCONFIRMED_HATCH, Qt.BDiagPattern))
        painter.restore()
        # 状態の区画は斜線（未確定）の上、強調・選択の枠の下に描く
        self._paint_status(painter, rect, border_drawn)
        painter.save()
        painter.resetTransform()
        painter.setBrush(Qt.NoBrush)
        if self.emphasized:
            inset = self._emphasis_width / 2 + _EMPHASIS_INNER_WIDTH / 2
            inner = rect.adjusted(inset, inset, -inset, -inset)
            if inner.width() > 2 and inner.height() > 2:
                painter.setPen(QPen(_EMPHASIS_INNER_COLOR, _EMPHASIS_INNER_WIDTH))
                painter.drawRect(inner)
        # 選択の枠はバーの縁、動いたバーの強調（黄色）はその内側に描く（両方のときも見える）
        edge = 0.0
        if selected:
            inset = min(_SELECTION_WIDTH / 2, rect.width() / 4, rect.height() / 4)
            painter.setPen(QPen(_SELECTION_COLOR, min(_SELECTION_WIDTH, rect.width() / 2, rect.height() / 2)))
            painter.drawRect(rect.adjusted(inset, inset, -inset, -inset))
            edge = _SELECTION_WIDTH
        if self.highlighted:
            inset = edge + _MOVED_HIGHLIGHT_WIDTH / 2
            highlight = rect.adjusted(inset, inset, -inset, -inset)
            if highlight.width() > 0 and highlight.height() > 0:
                painter.setPen(QPen(_MOVED_HIGHLIGHT_COLOR, _MOVED_HIGHLIGHT_WIDTH))
                painter.drawRect(highlight)
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


def _dependency_pen(color, width, dashed=False):
    pen = QPen(color, width)
    pen.setCosmetic(True)
    pen.setJoinStyle(Qt.RoundJoin)
    if dashed:
        pen.setStyle(Qt.DashLine)
    return pen


def _passive(item, z):
    """矢印の部品はマウス操作を受け取らない（クリック・範囲選択・バーのツールチップは
    下のバーに届く）。"""
    item.setAcceptedMouseButtons(Qt.NoButton)
    item.setAcceptHoverEvents(False)
    item.setZValue(z)
    return item


def dependency_path(a, b, step_x):
    """前のバー a の右端の中央から、後のバー b へ向かう矢印の線。
    Returns: (QPainterPath, 矢じりの先の位置, 矢じりの向き HEAD_RIGHT/HEAD_DOWN/HEAD_UP)。

    a, b はバーの本来の形（輪郭線を含まない bar_rect）。step_x はバーの端から横に出る
    長さ（シーン座標）。
    - 後のバーが十分右にあれば「出る横線 → 縦線 → 入る横線」の2回だけ折れ、b の左端の
      中央に横から入る。
    - 後のバーが前のバーの終わりのすぐ後（依存でいちばん多い、終わった翌日に始まる
      場合）や、終わりより前から始まっているときは、横から入る余地が無い。そのときは
      前のバーの右から出て、b の上端（b が上にあれば下端）へ縦に入る。以前は b の手前へ
      戻る小さな S 字の折れになり、矢じりが線より長く窮屈に見えていた。
    - b の幅が足りず縦にも入れないときだけ、中間の横線を後のバーの段のすぐ上（下）の
      隙間の中央に通して、b の左端へ戻る（バーの枠線と重ならない）。"""
    ya, yb = a.center().y(), b.center().y()
    path = QPainterPath(QPointF(a.right(), ya))
    if b.left() - a.right() >= 2 * step_x:
        x1 = a.right() + step_x
        path.lineTo(x1, ya)
        path.lineTo(x1, yb)
        path.lineTo(b.left(), yb)
        return path, QPointF(b.left(), yb), HEAD_RIGHT
    x_v = max(a.right(), b.left()) + step_x
    if ya != yb and x_v <= b.right() - step_x / 2:
        edge, head = (b.top(), HEAD_DOWN) if yb > ya else (b.bottom(), HEAD_UP)
        path.lineTo(x_v, ya)
        path.lineTo(x_v, edge)
        return path, QPointF(x_v, edge), head
    x1 = a.right() + step_x
    gap = b.top() - BAR_MARGIN if yb >= ya else b.bottom() + BAR_MARGIN
    x2 = b.left() - step_x
    path.lineTo(x1, ya)
    path.lineTo(x1, gap)
    path.lineTo(x2, gap)
    path.lineTo(x2, yb)
    path.lineTo(b.left(), yb)
    return path, QPointF(b.left(), yb), HEAD_RIGHT


def dependency_stub_path(rect, outgoing, step_x):
    """相手が絞り込みで隠れているときの L 字の線と、白い丸を置く位置。バーの横から
    出入りし、縦線はバーのすぐ上の段の隙間の中央で止める（上のジョブに入らない）。"""
    y = rect.center().y()
    gap = rect.top() - BAR_MARGIN
    if outgoing:
        x = rect.right() + step_x
        path = QPainterPath(QPointF(rect.right(), y))
        path.lineTo(x, y)
        path.lineTo(x, gap)
    else:
        x = rect.left() - step_x
        path = QPainterPath(QPointF(x, gap))
        path.lineTo(x, y)
        path.lineTo(rect.left(), y)
    return path, QPointF(x, gap)


class _DependencyLayer(QGraphicsItem):
    """依存の矢印（線と矢じり）をまとめて描く部品。形（shape）を持たないので、
    クリック・範囲選択・ツールチップの対象にならない（下のバーに届く）。
    矢じりは画面上で一定の大きさで描く。"""

    def __init__(self):
        super().__init__()
        self.arrows = []
        self._bounds = QRectF()
        self.setAcceptedMouseButtons(Qt.NoButton)
        self.setAcceptHoverEvents(False)
        self.setZValue(_DEP_Z)
        # paint() で exposedRect（描き直す範囲）を使うため
        self.setFlag(QGraphicsItem.ItemUsesExtendedStyleOption)

    def set_arrows(self, arrows, margin_x, margin_y):
        """margin_x/y: 矢じり・線の太さが線の外にはみ出すぶん（シーン座標。縮尺しだい）。

        描き直すのは、前回から変わった矢印の範囲だけにする（選択を変えて色が変わった
        数本のために、チャート全体を描き直さない。大きな計画で選択が重くなるため）。"""
        bounds = QRectF()
        for path, *_rest in arrows:
            bounds = bounds.united(path.boundingRect())
        bounds = bounds.adjusted(-margin_x, -margin_y, margin_x, margin_y) if arrows else QRectF()

        def signature(arrow):
            path, color, width, dashed, head, direction = arrow
            r = path.boundingRect()
            return (r.x(), r.y(), r.width(), r.height(), path.elementCount(), color.rgba(), width, dashed,
                    None if head is None else (head.x(), head.y()), direction)

        before = {signature(a): a for a in self.arrows}
        after = {signature(a): a for a in arrows}
        changed = [before[k] for k in before.keys() - after.keys()] + [after[k] for k in after.keys() - before.keys()]
        self.arrows = arrows
        if bounds != self._bounds:
            self.prepareGeometryChange()
            self._bounds = bounds
            self.update()
            return
        for path, *_rest in changed:
            self.update(path.boundingRect().adjusted(-margin_x, -margin_y, margin_x, margin_y))

    def boundingRect(self):
        return self._bounds

    def shape(self):
        return QPainterPath()

    def paint(self, painter, option, widget=None):
        # 線は縦・横だけなのでアンチエイリアスは要らない（数百本あると描画が重くなる）。
        # 画面に見えている範囲（exposedRect）に掛からない線は描かない。
        exposed = option.exposedRect
        half, length = _DEP_HEAD_PX / 2, _DEP_HEAD_PX
        # 矢じり（先端が原点）。向きごとに、根元の2点を先端の反対側に置く
        shapes = {
            HEAD_RIGHT: QPolygonF([QPointF(0, 0), QPointF(-length, -half), QPointF(-length, half)]),
            HEAD_DOWN: QPolygonF([QPointF(0, 0), QPointF(-half, -length), QPointF(half, -length)]),
            HEAD_UP: QPolygonF([QPointF(0, 0), QPointF(-half, length), QPointF(half, length)]),
        }
        painter.setBrush(Qt.NoBrush)
        heads = []
        for path, color, width, dashed, head_at, direction in self.arrows:
            if not path.boundingRect().adjusted(-1, -1, 1, 1).intersects(exposed):
                continue
            painter.setPen(_dependency_pen(color, width, dashed))
            painter.drawPath(path)
            if head_at is not None:
                heads.append((painter.worldTransform().map(head_at), color, shapes[direction]))
        if not heads:
            return
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        for device, color, shape in heads:
            painter.setBrush(QBrush(color))
            painter.drawPolygon(shape.translated(device))
        painter.restore()


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
        # ボタンを押していない間の移動も受け取り、伸縮できるバーの右端でカーソルを↔にする
        self.viewport().setMouseTracking(True)
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
        # ジョブ間の依存の矢印（set_dependencies）。[(前のキー, 後のキー, 守られていないか)]
        self._dep_links = []
        self._dep_names = {}   # キー -> 「ジョブ名 / タスク名」（隠れた相手のツールチップ）
        self._dep_mode = DEPENDENCY_SELECTED
        self._dep_items = []    # 相手が隠れているときの白い丸
        self._dep_layer = None  # 線と矢じりをまとめて描く部品（_DependencyLayer）
        self._dep_scene = None  # 上の部品を載せたシーン（作り直した後は古い部品に触らない）
        self._dep_scale = None
        h_bar = self.horizontalScrollBar()
        v_bar = self.verticalScrollBar()
        h_bar.valueChanged.connect(self.transformChanged)
        v_bar.valueChanged.connect(self.transformChanged)
        # 矢印の横に出る長さ・矢じりは画面上の大きさで決めるので、縮尺が変わったら描き直す
        # （スクロールだけなら描き直さない）
        self.transformChanged.connect(self._on_transform_for_dependencies)

    def keyPressEvent(self, event):
        key, ctrl = event.key(), bool(event.modifiers() & Qt.ControlModifier)
        if key in (Qt.Key_Shift, Qt.Key_Alt):
            # 指定キーを押した時点で、右端の上にあればカーソルを↔にする
            self._update_hover_cursor()
        if key == Qt.Key_Escape:
            # ドラッグ中なら取りやめ（何も書き込まない）、そうでなければ選択を外す
            if self._drag is not None:
                self._cancel_drag()
            elif self.scene() is not None:
                self.scene().clearSelection()
            event.accept()
            return
        if key == Qt.Key_A and ctrl:
            # Ctrl+A はすべてのタスクを選ぶ（A だけなら全体表示）。1本ずつ選ぶと選択の変更の
            # 通知がバーの数だけ出て（大きな計画では1万回以上）、そのたびに依存の矢印を描き
            # 直すので、範囲選択と同じく1回の通知で済ませる（選べる部品はバーだけ）
            scene = self.scene()
            if scene is not None:
                area = QPainterPath()
                area.addRect(scene.itemsBoundingRect())
                scene.setSelectionArea(area, Qt.ReplaceSelection, Qt.IntersectsItemShape)
            event.accept()
            return
        if key == Qt.Key_A:
            self.fitAllRequested.emit()
            event.accept()
            return
        if key == Qt.Key_F:
            self.fitSelectedRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        """ホイールで拡大縮小する（Ctrl＝横のみ、Shift＝縦のみ）。マウスの位置を中心に
        拡大し、拡大率は回した量に比例させる（タッチパッドの細かいスクロールでも急に
        何倍にもならないように）。横ホイール（チルト・横スワイプ）は横スクロールにする
        （以前は縦の回転量0を「縮小」と扱い、横に払うと縮小していた）。"""
        dx, dy = event.angleDelta().x(), event.angleDelta().y()
        modifiers = event.modifiers()
        if dy == 0 and dx != 0 and modifiers & Qt.ShiftModifier:
            # macOS などは Shift＋ホイールを横ホイールに変えて送ってくる。縦の拡大縮小として扱う
            dx, dy = 0, dx
        if abs(dx) > abs(dy):
            pixels = event.pixelDelta().x() or round(dx / _WHEEL_NOTCH * _WHEEL_SCROLL_PX)
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - pixels)
            event.accept()
            return
        if dy == 0:
            event.accept()
            return
        factor = _WHEEL_ZOOM_STEP ** (dy / _WHEEL_NOTCH)
        if modifiers & Qt.ControlModifier:
            fx, fy = factor, 1.0
        elif modifiers & Qt.ShiftModifier:
            fx, fy = 1.0, factor
        else:
            fx, fy = factor, factor
        self.zoom_at(event.position().toPoint(), fx, fy)
        event.accept()

    def zoom_at(self, view_pos, fx, fy):
        """view_pos（ビューポート座標）の下にある日付・行を動かさずに拡大縮小する。"""
        anchor = self.mapToScene(view_pos)
        self.scale(fx, fy)
        self._clamp_scale()
        moved = self.mapFromScene(anchor) - view_pos
        h_bar, v_bar = self.horizontalScrollBar(), self.verticalScrollBar()
        h_bar.setValue(h_bar.value() + moved.x())
        v_bar.setValue(v_bar.value() + moved.y())
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

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key_Shift, Qt.Key_Alt):
            self._update_hover_cursor()
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_last_pos = event.pos()
            self._set_cursor(Qt.ClosedHandCursor)
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
            if not self._drag["started"]:
                moved = event.position().toPoint() - self._drag["press_pos"]
                if moved.manhattanLength() < QApplication.startDragDistance():
                    event.accept()
                    return
                self._begin_drag()
            self._update_drag(event)
            event.accept()
            return
        if event.buttons() == Qt.NoButton:
            self._update_hover_cursor(event)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self._pan_last_pos = None
            self._update_hover_cursor(event)
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._drag is not None:
            if self._drag["started"]:
                self._finish_drag()
            else:
                # 動かさずに離した（Shift＋クリック）: 選択を外さずにそのバーを選択に加える
                anchor = self._drag["anchor"]
                self._drag = None
                anchor.setSelected(True)
            self._update_hover_cursor(event)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        self._update_hover_cursor(event)

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

    # -- ジョブ間の依存の矢印 -----------------------------------------------------------

    def set_dependencies(self, links, names):
        """links: [(前のキー, 後のキー, 守られていないか)]（スケジューリング結果の
        attrs["job_links"]）。names: キー -> 「ジョブ名 / タスク名」。シーンを差し替えた
        後に呼ぶ（矢印はシーンに載るので、シーンごとに描き直す）。"""
        self._dep_links = list(links)
        self._dep_names = dict(names)
        self.refresh_dependencies()

    def set_dependency_mode(self, mode):
        self._dep_mode = mode if mode in DEPENDENCY_MODES else DEPENDENCY_SELECTED
        self.refresh_dependencies()

    def dependency_mode(self):
        return self._dep_mode

    def dependency_items(self):
        """相手が隠れているときの白い丸（テスト・確認用）。"""
        return list(self._dep_items)

    def _on_transform_for_dependencies(self):
        scale = (self.transform().m11(), self.transform().m22())
        if scale != self._dep_scale:
            self.refresh_dependencies()

    def refresh_dependencies(self, overrides=None):
        """矢印を描き直す。overrides: {キー: QRectF} はドラッグ中の影の位置（その位置から
        矢印を出す。動かしているバーに矢印が置いて行かれないように）。

        矢印の線と矢じりは1つの部品（_DependencyLayer）がまとめて描く。依存が数百件
        あっても、選択の変更・拡大縮小のたびに部品を作り直さずに済むように（部品を
        1本ずつ作ると、大きな計画で選択を変えるたびに1秒以上かかった）。相手が
        隠れているときの白い丸だけは、ツールチップを出すため個別の部品にする。"""
        scene = self.scene()
        if scene is not self._dep_scene:
            # 作り直したシーンでは、古い部品（古いシーンと一緒に消える）に触らない
            self._dep_items = []
            self._dep_layer = None
            self._dep_scene = scene
        for item in self._dep_items:
            scene.removeItem(item)
        self._dep_items = []
        self._dep_scale = (self.transform().m11(), self.transform().m22())
        if scene is None:
            return
        if self._dep_layer is None:
            self._dep_layer = _DependencyLayer()
            scene.addItem(self._dep_layer)
        arrows = []
        if self._dep_mode != DEPENDENCY_OFF and self._dep_links:
            arrows = self._dependency_arrows(scene, overrides or {})
        sx, sy = (abs(v) or 1.0 for v in self._dep_scale)
        self._dep_layer.set_arrows(arrows, 2 * _DEP_HEAD_PX / sx, 2 * _DEP_HEAD_PX / sy)

    def _dependency_arrows(self, scene, overrides):
        """描く矢印の一覧 [(線, 色, 太さ, 破線か, 矢じりの位置 or None, 矢じりの向き)]。目立たせたいもの
        （選んでいるタスクにつながる・守られていない）を後に並べて前面に描く。"""
        bars = getattr(scene, "gantt_bars", {})
        selected = {bar.key for bar in _task_bars(scene)}
        step_x = _DEP_STEP_PX / (self._dep_scale[0] or 1.0)

        def rect_of(key):
            override = overrides.get(key)
            return override if override is not None else bars[key].bar_rect

        normal, emphasized = [], []
        for pred, succ, broken in self._dep_links:
            focused = pred in selected or succ in selected
            if self._dep_mode == DEPENDENCY_SELECTED and not focused:
                continue
            if pred not in bars and succ not in bars:
                continue
            if broken:
                color, width = _DEP_BROKEN_COLOR, _DEP_FOCUS_WIDTH + 0.4
            elif focused:
                color, width = _DEP_FOCUS_COLOR, _DEP_FOCUS_WIDTH
            else:
                color, width = _DEP_COLOR, _DEP_WIDTH
            target = emphasized if focused or broken else normal
            if pred in bars and succ in bars:
                path, head, direction = dependency_path(rect_of(pred), rect_of(succ), step_x)
                target.append((path, color, width, False, head, direction))
                continue
            outgoing = pred in bars
            key, other = (pred, succ) if outgoing else (succ, pred)
            rect = rect_of(key)
            path, dot_at = dependency_stub_path(rect, outgoing, step_x)
            head = None if outgoing else QPointF(rect.left(), rect.center().y())
            target.append((path, color, width, True, head, HEAD_RIGHT))
            name = self._dep_names.get(other, "")
            tip = (tr("このタスクを待っているタスク: {name}（絞り込みで非表示）", name=name) if outgoing
                   else tr("このタスクが待っているタスク: {name}（絞り込みで非表示）", name=name))
            self._add_dep_dot(scene, dot_at, color, width, _DEP_Z + 1, tip)
        return normal + emphasized

    def dependency_arrows(self):
        """今描いている矢印（テスト・確認用）。[(線, 色, 太さ, 破線か, 矢じりの位置, 矢じりの向き)]"""
        return list(self._dep_layer.arrows) if self._dep_layer is not None else []

    def _add_dep_dot(self, scene, point, color, width, z, tooltip):
        r = _DEP_DOT_PX / 2
        item = QGraphicsEllipseItem(QRectF(-r, -r, 2 * r, 2 * r))
        item.setPen(_dependency_pen(color, width))
        item.setBrush(QBrush(QColor("white")))
        item.setPos(point)
        item.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        item.setToolTip(tooltip)
        _passive(item, z)
        scene.addItem(item)
        self._dep_items.append(item)

    # -- ドラッグでの移動・伸縮 -------------------------------------------------------

    def _bar_at(self, view_pos):
        for item in self.items(view_pos):
            if isinstance(item, TaskBarItem):
                return item
        return None

    def _resize_handle_at(self, view_pos, modifiers):
        """指定キー（既定 Shift）を押していて、バーの右端（_RESIZE_HANDLE_PX 以内）に
        view_pos があれば、そのバーを返す（掴むと期間の伸縮。カーソルは↔になる）。"""
        if not self.edit_enabled or self.calendar is None:
            return None
        if not modifiers & self.drag_modifier:
            return None
        # 右端に隣のバーが接していると、その位置で一番上にあるのは隣のバーのことがある。
        # 前後の幅にあるバーをすべて見て、選んでいるバーの右端を優先する
        area = QRect(view_pos.x() - _RESIZE_HANDLE_PX, view_pos.y(), 2 * _RESIZE_HANDLE_PX + 1, 1)
        candidates = []
        for item in self.items(area):
            if not isinstance(item, TaskBarItem):
                continue
            right_edge = self.mapFromScene(item.bar_rect.topRight()).x()
            if abs(view_pos.x() - right_edge) <= _RESIZE_HANDLE_PX:
                candidates.append((not item.isSelected(), abs(view_pos.x() - right_edge), item))
        if not candidates:
            return None
        return min(candidates, key=lambda c: c[:2])[2]

    def _set_cursor(self, shape):
        """チャート部分のカーソルを変える（None で既定の矢印に戻す）。"""
        if shape is None:
            self.viewport().unsetCursor()
        else:
            self.viewport().setCursor(shape)

    def _update_hover_cursor(self, event=None):
        """ボタンを押していない間、伸縮できるバーの右端の上では↔にする。event が無い
        とき（キーを押した・離したとき）は、いまのマウスの位置とキーの状態で決める。"""
        if self._drag is not None or self._panning:
            return
        if event is None:
            pos = self.viewport().mapFromGlobal(QCursor.pos())
            modifiers = QApplication.keyboardModifiers()
        else:
            pos, modifiers = event.position().toPoint(), event.modifiers()
        on_handle = self._resize_handle_at(pos, modifiers) is not None
        self._set_cursor(Qt.SizeHorCursor if on_handle else None)

    def _try_start_drag(self, event):
        """指定キー（既定 Shift）を押しながらバーを押したら、ドラッグの準備をする。
        キーを押していなければ何もしない（クリックは従来どおり選択だけ）。右端（カーソルが
        ↔になる所）を押したら期間の伸縮になる。

        選択はまだ変えない。実際に動かし始めた時（_begin_drag）に、選ばれていないバーなら
        そのバーだけを選んで動かす。動かさずに離したら（Shift＋クリック）、今の選択を
        外さずにそのバーを選択に加える（以前は押した瞬間に選択を外していたので、複数
        選んでから Shift＋クリックすると1件に戻っていた）。"""
        if not self.edit_enabled or self.calendar is None:
            return False
        pos = event.position().toPoint()
        if not (event.modifiers() & self.drag_modifier):
            return False
        resize_bar = self._resize_handle_at(pos, event.modifiers())
        anchor = resize_bar or self._bar_at(pos)
        if anchor is None:
            return False
        self._drag = {
            "anchor": anchor,
            "started": False,
            "press_pos": pos,
            "add_to_selection": bool(event.modifiers() & Qt.ControlModifier),
            "resize": resize_bar is not None,
            "bars": [],
            "ghosts": {},
            "press_x": self.mapToScene(pos).x(),
            "days": {},
            "shift": 0,
            "new_days": None,
            "targets": {},
        }
        return True

    def _begin_drag(self):
        """押したまま動かし始めた。動かすバーを決め、影を置く。"""
        drag = self._drag
        anchor = drag["anchor"]
        scene = self.scene()
        if not anchor.isSelected():
            if not drag["add_to_selection"]:
                scene.clearSelection()
            anchor.setSelected(True)
        bars = [anchor] if drag["resize"] else _task_bars(scene)
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
            drag["ghosts"][bar.key] = ghost
        drag["bars"] = bars
        # 各バーの長さ（営業日）。移動してもこの日数を保つ
        drag["days"] = {b.key: max(1, self.calendar.count(b.start, b.end, b.team_key)) for b in bars}
        drag["started"] = True
        self._set_cursor(Qt.SizeHorCursor if drag["resize"] else Qt.ClosedHandCursor)

    def _cancel_drag(self):
        """ドラッグを取りやめる（Esc）。影を消し、何も書き込まない。"""
        drag, self._drag = self._drag, None
        if drag is None:
            return
        scene = self.scene()
        for ghost in drag["ghosts"].values():
            if scene is not None:
                scene.removeItem(ghost)
        self.refresh_dependencies()
        QToolTip.hideText()
        self._set_cursor(None)

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
            self._refresh_dependencies_for_ghosts(drag)
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
            self._refresh_dependencies_for_ghosts(drag)
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

    def _refresh_dependencies_for_ghosts(self, drag):
        """ドラッグ中は、動かしているバーの矢印を影の位置から出す。"""
        overrides = {key: ghost.path().boundingRect() for key, ghost in drag["ghosts"].items()}
        self.refresh_dependencies(overrides)

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
        # 離した直後は元の位置に戻す（書き込み→再計算の後、新しい位置で描き直される）
        self.refresh_dependencies()
        QToolTip.hideText()
        self._set_cursor(None)
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
        # クリックしてもフォーカスを受け取らず、本体に渡す（ジョブ名をクリックして
        # ジョブを選んだ直後に、F（選択へズーム）・A（全体表示）が効くように）
        self.setFocusPolicy(Qt.NoFocus)
        self.body_view = None  # FrozenGanttPane が本体（GanttGraphicsView）を入れる

    def wheelEvent(self, event):
        """見出し・左列の上でのホイールも、本体の上と同じ操作（拡大縮小・横スクロール）に
        する。以前はこのペインだけがスクロールして、左列のジョブ名が本体の行とずれていた。
        拡大の中心は、マウスの位置に対応する本体の位置（本体の外なら端）にする。"""
        body = self.body_view
        if body is None:
            super().wheelEvent(event)
            return
        viewport = body.viewport()
        pos = viewport.mapFromGlobal(event.globalPosition().toPoint())
        pos = QPoint(min(max(pos.x(), 0), viewport.width() - 1), min(max(pos.y(), 0), viewport.height() - 1))
        forwarded = QWheelEvent(QPointF(pos), event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                                event.buttons(), event.modifiers(), event.phase(), event.inverted())
        QApplication.sendEvent(viewport, forwarded)
        event.accept()

    def mousePressEvent(self, event):
        self._focus_body()
        super().mousePressEvent(event)

    def _focus_body(self):
        if self.body_view is not None:
            self.body_view.setFocus(Qt.MouseFocusReason)


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
        self._focus_body()
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
        self.header.body_view = self.body
        self.column.body_view = self.body

        # チャートが無いとき（絞り込みで0件・スケジューリングできない）も、本体を見出し・
        # 左列と同じ明るい背景で塗る。シーンが無いと QGraphicsView は背景（backgroundBrush）を
        # 描かずパレットの色で塗るため、ダークモードでは本体だけ暗くなり、明るい見出し・
        # 左列とL字に食い違っていた。
        viewport = self.body.viewport()
        viewport.setAutoFillBackground(True)
        pal = viewport.palette()
        pal.setColor(QPalette.Base, _PANE_BG)
        pal.setColor(QPalette.Window, _PANE_BG)
        viewport.setPalette(pal)
        # チャートが無い理由（set_placeholder）。キャンバスは常に明るいので文字色も固定。
        self.placeholder = QLabel(viewport)
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet(
            f"QLabel {{ color: {_PROJECT_START_COLOR.name()}; background: transparent; }}"
        )
        self.placeholder.setVisible(False)

        self.body.transformChanged.connect(self._sync_panes)
        # 本体の表示部分の大きさ・位置は、ウィンドウの大きさやスクロールバーの
        # 表示／非表示で変わる。そのたびにヘッダー・左列を合わせ直す。
        self.body.viewport().installEventFilter(self)
        self.body.fitAllRequested.connect(self.fit_all)
        self.body.fitSelectedRequested.connect(self.fit_selected)
        self.column.jobClicked.connect(self._select_job)

    def setScene(self, scenes):
        # シーンを差し替えるとドラッグ中のバー・影は古いシーンと一緒に消えるので、取りやめる
        self.body._cancel_drag()
        # 本体のスクロールバーは、チャートがある間は常に出し、無いときは出さない。
        # 全体表示の縮尺は内容がビューポートよりわずかに大きい境目にある（_clamp_scale）ので、
        # 必要に応じて出し入れすると、作り直しの途中で一瞬消えてビューポートが広がり、
        # その幅で縮尺を合わせた後にまた現れて、全体表示からずれていた。
        policy = Qt.ScrollBarAlwaysOff if scenes is None else Qt.ScrollBarAlwaysOn
        self.body.setHorizontalScrollBarPolicy(policy)
        self.body.setVerticalScrollBarPolicy(policy)
        if scenes is None:
            self.header.setScene(None)
            self.column.setScene(None)
            self.body.setScene(None)
            # 前のチャートのスクロール範囲（setSceneRect）を消す
            for view in (self.header, self.column, self.body):
                view.setSceneRect(QRectF())
            self._update_placeholder()
            return
        self.header.setScene(scenes.header)
        self.column.setScene(scenes.column)
        self.body.setScene(scenes.body)
        # 選んでいるタスクにつながる矢印を出す（依存の矢印の「選択中のみ」）
        scenes.body.selectionChanged.connect(self.body.refresh_dependencies)
        header_rect = getattr(scenes.header, "gantt_header_rect", None)
        column_rect = getattr(scenes.column, "gantt_column_rect", None)
        body_rect = getattr(scenes.body, "gantt_body_rect", None)
        if header_rect is not None:
            self.header.setSceneRect(header_rect)
        if column_rect is not None:
            self.column.setSceneRect(column_rect)
        if body_rect is not None:
            self.body.setSceneRect(body_rect)
        self._update_placeholder()
        self._sync_panes()

    def set_placeholder(self, text):
        """チャートが無いときに本体の中央に出す文言（チャートがあるときは出さない）。"""
        self.placeholder.setText(text)
        self._update_placeholder()

    def _update_placeholder(self):
        shown = self.body.scene() is None and bool(self.placeholder.text())
        if shown:
            viewport = self.body.viewport()
            self.placeholder.setGeometry(viewport.rect().adjusted(24, 24, -24, -24))
        self.placeholder.setVisible(shown)

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
        """ジョブ名のクリックで、そのジョブの全タスクを選ぶ。Ctrl＋クリック（add）なら今の
        選択に加える。そのジョブのタスクがすべて選ばれていれば、Ctrl＋クリックで外す。"""
        scene = self.body.scene()
        if scene is None:
            return
        job_bars = [bar for key, bar in self.bars().items() if key[0] == job_key]
        if add and job_bars and all(bar.isSelected() for bar in job_bars):
            for bar in job_bars:
                bar.setSelected(False)
            return
        if not add:
            scene.clearSelection()
        for bar in job_bars:
            bar.setSelected(True)
        self.body.setFocus(Qt.MouseFocusReason)

    def view_state(self):
        """本体の表示位置。restore_view_state() で戻す。

        スクロール量だけでなく、左端の日付・一番上に見えているジョブと、縦横それぞれが
        全体表示のままか、を覚える。作り直したチャートでは、軸の範囲（表示中のタスク
        しだいで左端の日付が変わる）や行の顔ぶれ（絞り込み）が変わるため、スクロール量を
        そのまま戻すと別の日付・別のジョブが出ていた。全体表示のままだった向きは、作り
        直した後も全体表示にする（絞り込みを外して行が増えても、全体が収まるように）。"""
        body = self.body
        transform = body.transform()
        sx, sy = transform.m11(), transform.m22()
        h, v = body.horizontalScrollBar().value(), body.verticalScrollBar().value()
        scene = body.scene()
        rect = getattr(scene, "gantt_body_rect", None) if scene is not None else None
        if rect is None or sx <= 0 or sy <= 0:
            return ViewState(sx, sy, h, v, True, True, None, None, 0.0)
        visible = body.mapToScene(body.viewport().rect()).boundingRect()
        fit_x = visible.width() >= rect.width() - _FIT_TOLERANCE_PX / sx
        fit_y = visible.height() >= rect.height() - _FIT_TOLERANCE_PX / sy
        left_day = scene.gantt_axis_start.toordinal() + (visible.left() - LEFT_MARGIN) / DAY_WIDTH
        top_job, top_offset = None, 0.0
        column_scene = self.column.scene()
        for y_top, y_bottom, job_key in getattr(column_scene, "gantt_job_rows", ()):
            if y_bottom > visible.top():
                top_job, top_offset = job_key, (visible.top() - y_top) * sy
                break
        return ViewState(sx, sy, h, v, fit_x, fit_y, left_day, top_job, top_offset)

    def restore_view_state(self, state):
        body = self.body
        scene = body.scene()
        rect = getattr(scene, "gantt_body_rect", None) if scene is not None else None
        sx, sy = state.sx, state.sy
        if rect is not None and not rect.isEmpty():
            viewport = body.viewport().size()
            # 全体表示のままだった向きは、作り直したチャートの全体が収まる縮尺にする。
            # _clamp_scale と同じく内容をビューポートよりわずかに大きくしておく（ちょうどの
            # 大きさにすると一瞬スクロールバーが消えてビューポートが広がり、その幅で補正
            # した後にスクロールバーが戻って、全体表示からずれていた）
            if state.fit_x:
                sx = (viewport.width() + _SCALE_FLOOR_MARGIN_PX) / rect.width()
            if state.fit_y:
                sy = (viewport.height() + _SCALE_FLOOR_MARGIN_PX) / rect.height()
        body.setTransform(QTransform().scale(sx, sy))
        body._clamp_scale()
        h_bar, v_bar = body.horizontalScrollBar(), body.verticalScrollBar()
        if rect is None or state.left_day is None:
            h_bar.setValue(state.h)
            v_bar.setValue(state.v)
            self._sync_panes()
            return
        sx, sy = body.transform().m11(), body.transform().m22()
        if state.fit_x:
            h_bar.setValue(h_bar.minimum())
        else:
            x = LEFT_MARGIN + (state.left_day - scene.gantt_axis_start.toordinal()) * DAY_WIDTH
            h_bar.setValue(h_bar.value() + round((x - body.mapToScene(0, 0).x()) * sx))
        rows = {job: y_top for y_top, _y_bottom, job in getattr(self.column.scene(), "gantt_job_rows", ())}
        if state.fit_y:
            v_bar.setValue(v_bar.minimum())
        elif state.top_job in rows:
            y = rows[state.top_job] + state.top_offset / sy
            v_bar.setValue(v_bar.value() + round((y - body.mapToScene(0, 0).y()) * sy))
        else:
            v_bar.setValue(state.v)
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
            self._update_placeholder()
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
        self._place_year_labels(sx)
        self._center_task_labels(sx, sy)
        self._layout_job_labels(sy)

    def _layout_job_labels(self, sy):
        """左列のジョブ名（と色見本）を、その行の中央に揃える。

        ジョブ名は等倍（一定ピクセル数）で描く一方、行の高さは縦の拡縮率（sy）で
        変わる。全体表示などで縦に縮小すると行が文字より低くなり、名前同士が
        重なって読めなくなる。そのときは全行の名前を同じ倍率で縮小し、それでも
        _JOB_LABEL_MIN_SCALE を下回るほど低いなら全行とも隠す（拡大すれば現れる）。

        以前は上から順に、直前に表示した名前と重なるものだけを隠していた。行の高さ
        （レーンの数）はジョブごとに違うため、名前の出る行と出ない行が不規則に混ざり、
        見た目が分かりにくかった。"""
        scene = self.column.scene()
        if scene is None or sy <= 0:
            return
        entries = getattr(scene, "gantt_job_labels", [])
        if not entries:
            return
        height_px = max(label.boundingRect().height() for label, _swatch, _y in entries)
        # 隣り合う行の中央の間隔が、名前を重ならずに置ける高さの上限
        pitch_px = min(
            ((b[2] - a[2]) * sy for a, b in zip(entries, entries[1:])), default=height_px,
        )
        scale = min(1.0, pitch_px / height_px) if height_px > 0 else 1.0
        visible = scale >= _JOB_LABEL_MIN_SCALE
        for label, swatch, center_y in entries:
            label.setVisible(visible)
            swatch.setVisible(visible)
            if not visible:
                continue
            for item, item_h in ((label, label.boundingRect().height()), (swatch, swatch.rect().height())):
                if item.scale() != scale:
                    item.setScale(scale)
                item.setPos(item.x(), center_y - (item_h * scale / 2) / sy)

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
            # 状態の区画（右端）を描くバーは、その分だけ右を空ける
            if bar is not None:
                status_px = bar.status_segment_px(bar_width * sx, bar_h_px)
                avail_w -= status_px
                shift_px -= status_px / 2

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
        header_rect = getattr(scene, "gantt_header_rect", None)
        left_limit = header_rect.left() if header_rect is not None else float("-inf")
        shown = []
        for label, line_x in getattr(scene, "gantt_tick_labels", []):
            width = label.boundingRect().width() / sx
            # 軸の左端の目盛りは、中央揃えのままだと左半分が切れる。見出しの範囲に収める
            left = max(line_x - width / 2, left_limit)
            label.setPos(left, label.y())
            if label.isVisible():
                shown.append((left, left + width, label))
        # 月初めが続く軸の左端などで、ラベル同士が重ならないようにする。右から順に置き、
        # 右隣と重なるものを隠す（左端の目盛りは月の途中から始まることが多いので、右の
        # 丸ごと1か月ぶんのラベルを残す）
        gap = _TICK_LABEL_GAP_PX / sx
        next_left = float("inf")
        for left, right, label in sorted(shown, key=lambda e: e[0], reverse=True):
            if right + gap > next_left:
                label.setVisible(False)
                continue
            next_left = left

    def _place_year_labels(self, sx):
        """年のラベルを、その年のうち今見えている部分の中央に置く（年の外にははみ出さない。
        見えている部分がラベルより狭ければ、年の端に寄せる）。以前は年の中央に固定して
        いたため、拡大して年の途中を見ていると年がどこにも出ず、何年か分からなかった。"""
        scene = self.header.scene()
        if scene is None or sx <= 0:
            return
        visible = self.header.mapToScene(self.header.viewport().rect()).boundingRect()
        for label, span_left, span_right in getattr(scene, "gantt_year_labels", []):
            width = label.boundingRect().width() / sx
            if span_right - span_left < width:
                # 縮小して年の幅がラベルより狭い（年をまたぐ短い端など）: 年の幅に収まらない
                label.setVisible(False)
                continue
            left, right = max(span_left, visible.left()), min(span_right, visible.right())
            center = (left + right) / 2 if right > left else (span_left + span_right) / 2
            x = min(max(center - width / 2, span_left), span_right - width)
            label.setVisible(True)
            label.setPos(x, label.y())


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


def build_gantt_scenes(df, display, color_by="team", job_order=None):
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
    追加している。

    job_order: ジョブ（行）の並び（Job_ID のリスト。gui/gantt_row_order.py）。省略時は
    ジョブの最も早い開始日の順。"""
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
    task_status = display.get("task_status") or {}

    task_font = QFont()
    task_font.setPointSize(9)
    job_font = QFont()
    job_font.setPointSize(9)
    job_font.setBold(True)

    # -- ジョブごとにレーン詰め、Y座標を決める --------------------------------------
    # 行は最初に1回だけ辞書にして、ジョブごとのまとめ・並べ替えは Python 側で行う
    # （ジョブごとに DataFrame を絞り込んで iterrows すると、ジョブ数×行数の手間が
    # かかり、大きな計画（1,916ジョブ・16,230行）で描画に10秒かかっていた）。
    rows_by_job = {}
    for r in df.to_dict("records"):
        rows_by_job.setdefault(r["Job_ID"], []).append(r)
    if job_order is None:
        job_order = df.groupby("Job_ID")["Start_Date"].min().sort_values().index.tolist()
    else:
        job_order = [job_id for job_id in job_order if job_id in rows_by_job]
    y_cursor = TOP_MARGIN
    job_blocks = []  # (job_id, job_name, workflow_id, y_top, y_bottom, [(task_row, lane), ...])
    for job_id in job_order:
        job_rows = sorted(rows_by_job[job_id], key=lambda r: r["Start_Date"])
        lanes, lane_count = _pack_lanes([{"start": r["Start_Date"], "end": r["End_Date"]} for r in job_rows])
        y_top = y_cursor
        y_bottom = y_top + lane_count * ROW_HEIGHT
        job_blocks.append((job_id, str(job_rows[0]["Job_Name"]), job_rows[0]["Workflow_ID"],
                            y_top, y_bottom, list(zip(job_rows, lanes))))
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
    # 年のラベルは、その年のうち画面に見えている部分の中央に置く（拡大して年の途中を
    # 見ているときも年が分かるように）。位置は拡縮率しだいなので
    # FrozenGanttPane._place_year_labels が _sync_panes のたびに決める。
    header_scene.gantt_year_labels = []  # (label, 年の左端x, 年の右端x)
    year_cursor = axis_start.replace(month=1, day=1)
    while year_cursor <= axis_end:
        year_end = year_cursor.replace(month=12, day=31)
        span_start = max(axis_start, year_cursor)
        span_end = min(axis_end, year_end + timedelta(days=1))
        center_x = (x_of(span_start) + x_of(span_end)) / 2
        text = tr("{year}年", year=year_cursor.year)
        text_width = year_metrics.horizontalAdvance(text)
        year_label = _add_fixed_size_label(
            header_scene, text, year_font,
            (center_x - text_width / 2, TOP_MARGIN - _YEAR_LABEL_OFFSET),
        )
        header_scene.gantt_year_labels.append((year_label, x_of(span_start), x_of(span_end)))
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
        # 列の幅に収まらず省略したジョブ名も、マウスを乗せれば全体を読めるようにする
        job_label.setToolTip(job_name)
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
                status=task_status.get(key),
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
                + {STATUS_DONE: tr("\n✔ 完了"), STATUS_IN_PROGRESS: tr("\n▶ 進行中")}.get(rect.status, "")
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
