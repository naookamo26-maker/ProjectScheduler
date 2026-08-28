"""
タブ2「ワークフロー設計」用のノードグラフエディタ。

タスクを矩形ノードとしてキャンバス上に配置し、ノードの出力アンカーから
別ノードの入力アンカーへドラッグすることで依存関係（Internal_Depends）を
視覚的に作成できるようにする。エッジ追加のたびに、追加先ノードから追加元
ノードへ既存の依存をたどって到達できるか（＝循環依存になるか）をBFSで
事前チェックし、循環になる場合は追加を拒否する。

project_scheduler.py の _build_scheduling_order() が採用しているトポロジカル
ソート（優先度付き逆方向Kahn法、循環をCircularDependencyErrorとして事後検出）
と同じ「依存グラフに閉路がないこと」を保証する考え方を参考にしているが、
キャンバス編集時点ではジョブをまたいだ g_id データ構造がまだ存在しないため、
ワークフロー内の単純な整数グラフに対する専用の事前チェックとして別実装する。
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPainterPathStroker, QPen, QPolygonF
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QMenu,
    QMessageBox,
)

from gui.db import MAX_LAG_DAYS, DuplicateNameError, ProjectDatabaseError
from gui.widgets_common import (
    NoWheelComboBox,
    NoWheelListWidget,
    NoWheelSpinBox,
    confirm_and_repair_milestone_consistency,
)

# エッジのクリック判定の太さ（見た目の線は2px）と、種別・ラグのラベルの見た目。
EDGE_HIT_WIDTH = 14
EDGE_LABEL_BG_COLOR = "#faf9f5"
EDGE_LABEL_PADDING = 3
EDGE_LABEL_GAP = 4

NODE_WIDTH = 170
NODE_HEIGHT = 64
ANCHOR_RADIUS = 8
# ノード矩形の角丸半径。gui/gantt_view.py のタスクバーと同様、隣接する
# 要素との境目を視認しやすくする狙い。
NODE_CORNER_RADIUS = 6

# タスク間の依存線（グレー実線）や、チームカラー（project_scheduler.py の
# _TEAM_COLOR_PALETTE、いずれも彩度を抑えたパステル調）と紛れないよう、
# 依存テンプレート（他ワークフローへの依存）の疑似ノード・接続線は彩度の高い
# マゼンタ系の点線にする。
TEMPLATE_EDGE_COLOR = "#c2158c"
TEMPLATE_NODE_FILL_COLOR = "#fbe6f4"
TEMPLATE_NODE_HEADER_HEIGHT = 20
# タスクノードはチーム名・日数を含め3行分の高さ（NODE_HEIGHT）を要するが、
# 疑似ノードは見出しバーの下に依存先ワークフロー名・依存先タスク名の2行
# だけで足りるため、タスクと同じ高さを流用せず専用の余白詰めの高さにする。
TEMPLATE_NODE_CONTENT_HEIGHT = 44
TEMPLATE_NODE_HEIGHT = TEMPLATE_NODE_HEADER_HEIGHT + TEMPLATE_NODE_CONTENT_HEIGHT
# 依存テンプレートの疑似ノードは、タスク同士の縦方向の並び（アクティブな
# タスクの流れ）を邪魔しないよう、タスクの整列とは別扱いで、タスク群の
# 上端よりさらに上に余白を空けて配置する（compute_combined_layout参照）。
TEMPLATE_LAYOUT_MARGIN = NODE_HEIGHT + 60

# ワークフロー全体の最短完了日数を示す目盛り（｜←-- 最短N日 --→｜）の見た目。
# gui/gantt_view.py の開発開始日の縦線と同じ色（構造的な基準線という
# 位置づけを揃えるため。チーム色・依存テンプレートの配色とは別系統にする）。
DURATION_MARKER_COLOR = "#52514e"
# ノード・依存テンプレートの疑似ノードの邪魔にならないよう、現在配置されて
# いる最上段のさらに上に空ける余白(px)。
DURATION_MARKER_MARGIN = 40
DURATION_MARKER_TICK_HEIGHT = 12
DURATION_MARKER_ARROW_SIZE = 8
# 矢印の先端を目盛り線の端（｜）そのものではなく、少しだけ内側に置く
# ギャップ(px)。先端が｜と完全に重なると矢印の形が潰れて見えるため。
DURATION_MARKER_ARROW_INSET = 3
# ラベル（"最短N日"）と、その両側の線分との間隔(px)。
DURATION_MARKER_LABEL_GAP = 6

_ADD_TEAM_SENTINEL = "__add_new_team__"


def compute_task_depths(tasks, dependencies):
    """タスク・依存関係一覧から、依存の深さ（predecessorを持たないタスク=0、
    以降predecessorの深さの最大+1）を計算する（純粋関数、Qt/DB非依存）。
    ノードグラフの自動整列（compute_auto_layout/compute_combined_layout）と、
    テーブルビューでのタスクの並び順（上流→下流）の両方から共有する。

    tasks: [{"id": ...}, ...]
    dependencies: [{"predecessor_task_id": ..., "successor_task_id": ...}, ...]
    Returns: {task_id: depth}
    """
    task_ids = [t["id"] for t in tasks]
    preds = {tid: [] for tid in task_ids}
    for d in dependencies:
        p, s = d["predecessor_task_id"], d["successor_task_id"]
        if p in preds and s in preds:
            preds[s].append(p)

    depth = {}

    def calc_depth(tid, path):
        if tid in depth:
            return depth[tid]
        if tid in path:
            return 0  # 循環がある場合の保険（通常はキャンバス側で事前に防止される）
        if not preds[tid]:
            depth[tid] = 0
        else:
            depth[tid] = max(calc_depth(p, path | {tid}) for p in preds[tid]) + 1
        return depth[tid]

    for tid in task_ids:
        calc_depth(tid, set())
    return depth


def compute_workflow_min_duration(tasks, dependencies):
    """タスク・依存関係一覧から、ワークフロー全体を完了するまでの最短日数
    （純粋関数、Qt/DB非依存）を計算する。

    内部依存（Internal_Depends）の種別（FS/SS）とラグだけで決まる下限で、
    project_scheduler.py の実際のスケジューリング結果とは異なる——チームの
    同時ライン数（リソース制約）・休業日・他ジョブとの競合は一切考慮しない、
    「このワークフローだけを、資源制約なしで最速で流したら何日か」という
    設計時の目安。開発開始日を0日目とする起算のため、暦日換算になる
    （休業日を考慮しないため、project_scheduler.py の営業日ベースの計算
    とはこの点でも異なる）。

    tasks: [{"id": ..., "default_days": ...}, ...]
    dependencies: [{"predecessor_task_id": ..., "successor_task_id": ...,
                     "dep_type": "FS"/"SS", "lag_days": int}, ...]
    Returns: 全タスクが完了するまでの最短日数（タスクが1件も無ければ0）。
    """
    task_days = {t["id"]: t["default_days"] for t in tasks}
    preds = {tid: [] for tid in task_days}
    for d in dependencies:
        p, s = d["predecessor_task_id"], d["successor_task_id"]
        if p in task_days and s in task_days:
            preds[s].append((p, d.get("dep_type", "FS"), d.get("lag_days", 0)))

    start = {}
    finish = {}

    def calc(tid, path):
        if tid in finish:
            return finish[tid]
        if tid in path or not preds[tid]:
            start[tid] = 0  # pathに含まれる=循環がある場合の保険（compute_task_depthsと同じ考え方）
        else:
            bounds = []
            for pred_id, dep_type, lag_days in preds[tid]:
                calc(pred_id, path | {tid})
                anchor = start[pred_id] if (dep_type or "FS").upper() == "SS" else finish[pred_id]
                bounds.append(anchor + int(lag_days or 0))
            start[tid] = max(0, max(bounds))
        finish[tid] = start[tid] + task_days[tid]
        return finish[tid]

    for tid in task_days:
        calc(tid, set())
    return max(finish.values()) if finish else 0


def compute_auto_layout(tasks, dependencies, gap_x=NODE_WIDTH + 60, gap_y=NODE_HEIGHT + 30):
    """タスクの依存の深さでレイヤー分けし、左→右に深さ順、各レイヤー内は
    上→下（名前順）に並べる座標を計算する。

    新規ワークフローの初期表示や、既存Excelからの移行データ（座標情報を
    持たない）に初期レイアウトを与えるために使う。
    Returns: {task_id: (x, y)}
    """
    depth = compute_task_depths(tasks, dependencies)
    layers = {}
    for t in tasks:
        layers.setdefault(depth[t["id"]], []).append(t)

    positions = {}
    for layer_depth, layer_tasks in layers.items():
        for i, t in enumerate(sorted(layer_tasks, key=lambda t: t["name"])):
            positions[t["id"]] = (layer_depth * gap_x, i * gap_y)
    return positions


def compute_combined_layout(tasks, dependencies, templates, gap_x=NODE_WIDTH + 60, gap_y=NODE_HEIGHT + 30,
                             template_margin=TEMPLATE_LAYOUT_MARGIN):
    """タスクの自動整列に、依存テンプレート（他ワークフローへの依存）の
    疑似ノードを重ねて配置する。

    タスク同士の縦方向の並び（アクティブなタスクの流れを見やすくするための
    並び）にテンプレートが割り込むと視認性が落ちるため、**まずタスクだけで
    `compute_auto_layout` により整列し、テンプレートの有無で結果が変わらない
    ようにする**。テンプレートの配置自体は `compute_template_positions` に委譲する
    （タスクの座標が「自動整列し直した直後の値」か「DB保存済みの値（手動で
    ドラッグしたものを含む）」かによらず、同じロジックで対象タスクの近くに
    配置できるようにするため——`WorkflowGraphScene.reload`参照）。

    テンプレートの座標はDBに保存しない（保存用カラムを持たないため）。
    タスクや依存関係・テンプレートが変わるたび、この関数で毎回計算し直す
    という割り切り（詳細はdocs/architecture.md参照）。

    戻り値のキーは、タスクは workflow_task_id（int）のまま、テンプレートは
    ("template", template_id) というタプルにし、テーブル同士のIDが衝突
    しても混同しないようにする。
    Returns: {task_id または ("template", template_id): (x, y)}
    """
    positions = dict(compute_auto_layout(tasks, dependencies, gap_x, gap_y))
    positions.update(compute_template_positions(positions, templates, gap_x, gap_y, template_margin))
    return positions


def compute_template_positions(task_positions, templates, gap_x=NODE_WIDTH + 60, gap_y=NODE_HEIGHT + 30,
                                template_margin=TEMPLATE_LAYOUT_MARGIN):
    """task_positions: {workflow_task_id: (x, y)}（実際に配置されている座標。
    `compute_auto_layout`が計算し直した直後の値でも、DB保存済みの値
    ——手動でドラッグしたものを含む——でもよい）。

    各テンプレートを、対象タスク（workflow_task_id）の1つ上流の列（同じx）に
    揃え、縦方向はタスク群の最上段よりさらに `template_margin` 分上の帯に
    配置する。同じx（同じ対象タスクを指す複数テンプレート、または
    たまたま同じxのタスクを指す複数テンプレート）はgap_y間隔で積み上げ、
    重ならないようにする。対象タスクの座標が渡されていない（データ不整合）
    テンプレートは除外する。

    Returns: {("template", template_id): (x, y)}
    """
    if not templates or not task_positions:
        return {}
    min_task_y = min(y for _x, y in task_positions.values())
    band_top = min_task_y - template_margin

    columns = {}
    for tpl in templates:
        target_pos = task_positions.get(tpl["workflow_task_id"])
        if target_pos is None:
            continue
        x = target_pos[0] - gap_x
        label = f'{tpl["depends_on_workflow_name"]} / {tpl["depends_on_task_name"]}'
        columns.setdefault(x, []).append((label, tpl["id"]))

    positions = {}
    for x, entries in columns.items():
        for i, (_label, template_id) in enumerate(sorted(entries, key=lambda e: e[0])):
            positions[("template", template_id)] = (x, band_top - i * gap_y)
    return positions


def team_color_map(teams):
    """teams: db.list_teams() の戻り値。{team_id: hex色} を返す
    （project_scheduler.py の _TEAM_COLOR_PALETTE をそのまま再利用し、
    Gantt出力と配色を統一する）。"""
    from project_scheduler import _TEAM_COLOR_OVERFLOW, _TEAM_COLOR_PALETTE

    colors = {}
    for i, t in enumerate(teams):
        colors[t["id"]] = _TEAM_COLOR_PALETTE[i] if i < len(_TEAM_COLOR_PALETTE) else _TEAM_COLOR_OVERFLOW
    return colors


class AnchorItem(QGraphicsEllipseItem):
    def __init__(self, role, parent):
        super().__init__(-ANCHOR_RADIUS, -ANCHOR_RADIUS, ANCHOR_RADIUS * 2, ANCHOR_RADIUS * 2, parent)
        self.role = role  # "input" | "output"
        self.setBrush(QBrush(QColor("#52514e")))
        self.setPen(QPen(Qt.NoPen))
        self.setAcceptedMouseButtons(Qt.NoButton)  # クリックはView側でitemAtにより処理する


class TaskNodeItem(QGraphicsPathItem):
    def __init__(self, workflow_task_id, name, team_name, color_hex, days):
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT), NODE_CORNER_RADIUS, NODE_CORNER_RADIUS)
        super().__init__(path)
        self.workflow_task_id = workflow_task_id
        self.edges = []  # 接続中のEdgeItem一覧（移動時の再描画用）
        # このタスクを対象とする依存テンプレート（他ワークフローへの依存）の
        # 疑似ノードから伸びるEdgeItem一覧。scene.edges/delete_edgeが前提とする
        # 「両端とも実タスクノード」という扱いに混ぜられないため、self.edgesとは
        # 別リストに分けて持つ（_add_template_scene_item/_remove_template_scene_item参照）。
        self.incoming_template_edges = []

        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setBrush(QBrush(QColor(color_hex)))
        self.setPen(QPen(QColor("#0b0b0b"), 1))

        name_text = QGraphicsSimpleTextItem(name, self)
        font = name_text.font()
        font.setBold(True)
        name_text.setFont(font)
        name_text.setPos(8, 4)

        team_text = QGraphicsSimpleTextItem(team_name, self)
        team_text.setPos(8, 24)

        days_text = QGraphicsSimpleTextItem(f"{days}日", self)
        days_text.setPos(8, 44)

        self.input_anchor = AnchorItem("input", self)
        self.input_anchor.setPos(0, NODE_HEIGHT / 2)
        self.output_anchor = AnchorItem("output", self)
        self.output_anchor.setPos(NODE_WIDTH, NODE_HEIGHT / 2)

    def update_labels(self, name, team_name, days):
        items = [c for c in self.childItems() if isinstance(c, QGraphicsSimpleTextItem)]
        items[0].setText(name)
        items[1].setText(team_name)
        items[2].setText(f"{days}日")

    def set_color(self, color_hex):
        self.setBrush(QBrush(QColor(color_hex)))

    def output_anchor_scene_pos(self):
        return self.mapToScene(self.output_anchor.pos())

    def input_anchor_scene_pos(self):
        return self.mapToScene(self.input_anchor.pos())

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for edge in self.edges:
                edge.update_path()
            for edge in self.incoming_template_edges:
                edge.update_path()
        return super().itemChange(change, value)


class TemplateDependencyNodeItem(QGraphicsPathItem):
    """依存テンプレート（他ワークフローへの依存）1件を表す疑似ノード。

    タスクノード（角丸・チーム色・ドラッグ移動可・接続アンカー付き）とは
    一目で区別できるよう、角丸なしの破線枠・上部に「依存テンプレート」の
    見出しバーを持つデザインにする。ドラッグ移動も接続アンカーも持たせない
    （他ワークフローのタスクへドラッグ接続する操作自体が存在しないため）。
    座標はauto_arrangeが毎回計算し直すだけでDBには保存しない
    （WorkflowGraphScene参照）。"""

    def __init__(self, template_id, target_task_id, workflow_name, task_name):
        path = QPainterPath()
        path.addRect(QRectF(0, 0, NODE_WIDTH, TEMPLATE_NODE_HEIGHT))
        super().__init__(path)
        self.template_id = template_id
        self.target_task_id = target_task_id

        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setBrush(QBrush(QColor(TEMPLATE_NODE_FILL_COLOR)))
        self.setPen(QPen(QColor(TEMPLATE_EDGE_COLOR), 1.5, Qt.DashLine))

        header = QGraphicsRectItem(0, 0, NODE_WIDTH, TEMPLATE_NODE_HEADER_HEIGHT, self)
        header.setBrush(QBrush(QColor(TEMPLATE_EDGE_COLOR)))
        header.setPen(QPen(Qt.NoPen))

        header_text = QGraphicsSimpleTextItem("依存テンプレート", self)
        header_font = header_text.font()
        header_font.setBold(True)
        header_font.setPointSize(max(header_font.pointSize() - 1, 6))
        header_text.setFont(header_font)
        header_text.setBrush(QBrush(QColor("#ffffff")))
        header_text.setPos(6, 3)

        workflow_text = QGraphicsSimpleTextItem(workflow_name, self)
        font = workflow_text.font()
        font.setItalic(True)
        workflow_text.setFont(font)
        workflow_text.setPos(8, TEMPLATE_NODE_HEADER_HEIGHT + 4)

        task_text = QGraphicsSimpleTextItem(task_name, self)
        task_text.setPos(8, TEMPLATE_NODE_HEADER_HEIGHT + 24)

    def update_labels(self, workflow_name, task_name):
        # 子要素は見出し（"依存テンプレート"、固定文言で変更不要）・
        # 依存先ワークフロー名・依存先タスク名の順。
        items = [c for c in self.childItems() if isinstance(c, QGraphicsSimpleTextItem)]
        items[1].setText(workflow_name)
        items[2].setText(task_name)

    def output_anchor_scene_pos(self):
        return self.mapToScene(QPointF(NODE_WIDTH, TEMPLATE_NODE_HEIGHT / 2))


def _arrow_polygon(tip, direction, size=9):
    import math

    length = math.hypot(direction.x(), direction.y())
    if length == 0:
        return QPolygonF([tip, tip, tip])
    ux, uy = direction.x() / length, direction.y() / length
    back = QPointF(tip.x() - ux * size, tip.y() - uy * size)
    perp = QPointF(-uy, ux)
    left = QPointF(back.x() + perp.x() * size * 0.5, back.y() + perp.y() * size * 0.5)
    right = QPointF(back.x() - perp.x() * size * 0.5, back.y() - perp.y() * size * 0.5)
    return QPolygonF([tip, left, right])


def format_dependency_kind(dep_type, lag_days):
    """依存関係の種別・ラグを画面表示用の短い文字列にする。
    既定（FS・ラグ0）は「指定なし」を意味するので空文字を返す
    ——ほぼすべてのエッジが既定である以上、既定にラベルを出すと
    キャンバスがラベルで埋まって、例外である方が目立たなくなる。"""
    dep_type = (dep_type or "FS").upper()
    lag_days = int(lag_days or 0)
    if dep_type == "FS" and lag_days == 0:
        return ""
    return dep_type if lag_days == 0 else f"{dep_type}{lag_days:+d}"


class EdgeItem(QGraphicsPathItem):
    def __init__(self, dependency_id, pred_node, succ_node, color="#52514e", line_style=Qt.SolidLine,
                 dep_type="FS", lag_days=0):
        super().__init__()
        self.dependency_id = dependency_id
        self.pred_node = pred_node
        self.succ_node = succ_node
        self.dep_type = dep_type
        self.lag_days = lag_days
        self.setPen(QPen(QColor(color), 2, line_style))
        self.setBrush(Qt.NoBrush)
        self.setZValue(-1)
        self.arrow_item = QGraphicsPolygonItem(self)
        self.arrow_item.setBrush(QBrush(QColor(color)))
        self.arrow_item.setPen(QPen(Qt.NoPen))
        # 種別・ラグのラベル。既定（FS・0）のときは非表示にするため、
        # 背景の下敷きごとまとめて隠せるよう別アイテムに分けている。
        self.label_bg = QGraphicsRectItem(self)
        self.label_bg.setBrush(QBrush(QColor(EDGE_LABEL_BG_COLOR)))
        self.label_bg.setPen(QPen(QColor(color), 1))
        self.label_item = QGraphicsSimpleTextItem(self.label_bg)
        self.label_item.setBrush(QBrush(QColor(color)))
        self.set_dependency_kind(dep_type, lag_days)

    def set_dependency_kind(self, dep_type, lag_days):
        self.dep_type = (dep_type or "FS").upper()
        self.lag_days = int(lag_days or 0)
        text = format_dependency_kind(self.dep_type, self.lag_days)
        self.label_item.setText(text)
        self.label_bg.setVisible(bool(text))
        self.update_path()

    def shape(self):
        """クリック判定を線の太さ（2px）から広げる。ベジェ曲線をペンの
        太さのままで当てるのは実用にならないため。"""
        stroker = QPainterPathStroker()
        stroker.setWidth(EDGE_HIT_WIDTH)
        return stroker.createStroke(self.path())

    def boundingRect(self):
        # Qtはヒット判定の前に boundingRect で絞り込むため、shape() だけを
        # 広げても矩形の外側は当たらない。同じ幅ぶん広げて揃える。
        margin = EDGE_HIT_WIDTH / 2
        return super().boundingRect().adjusted(-margin, -margin, margin, margin)

    def update_path(self):
        start = self.pred_node.output_anchor_scene_pos()
        end = self.succ_node.input_anchor_scene_pos()
        dx = max(abs(end.x() - start.x()) * 0.5, 40)
        c1 = QPointF(start.x() + dx, start.y())
        c2 = QPointF(end.x() - dx, end.y())
        path = QPainterPath(start)
        path.cubicTo(c1, c2, end)
        self.setPath(path)
        # 矢印は接続線の末端（入力ポート付近）だとアンカーの丸と重なって
        # 分かりづらいため、線の中央（曲線上の進行方向）に表示する。
        mid = path.pointAtPercent(0.5)
        ahead = path.pointAtPercent(0.51)
        direction = ahead - mid
        self.arrow_item.setPolygon(_arrow_polygon(mid, direction))
        self._update_label_pos(mid)

    def _update_label_pos(self, mid):
        """ラベルは矢印と重ならないよう線の少し上に置く。"""
        if not self.label_bg.isVisible():
            return
        rect = self.label_item.boundingRect()
        pad = EDGE_LABEL_PADDING
        self.label_bg.setRect(0, 0, rect.width() + pad * 2, rect.height() + pad * 2)
        self.label_item.setPos(pad, pad)
        self.label_bg.setPos(
            mid.x() - (rect.width() + pad * 2) / 2,
            mid.y() - (rect.height() + pad * 2) - EDGE_LABEL_GAP,
        )


class WorkflowGraphScene(QGraphicsScene):
    def __init__(self, db, workflow_id, parent_widget, on_changed=None):
        super().__init__(parent_widget)
        self.db = db
        self.workflow_id = workflow_id
        self.parent_widget = parent_widget
        # GUI側（gui/tab_workflows.py）へ「タスク・依存関係・依存テンプレートの
        # いずれかが変わった」ことを通知するフック。テーブルビューの再表示に使う
        # （引数なしで呼ばれるcallable、またはNone）。
        self.on_changed = on_changed
        self.nodes = {}  # workflow_task_id -> TaskNodeItem
        self.edges = {}  # dependency_id -> EdgeItem
        self.adjacency = {}  # predecessor_task_id -> [successor_task_id, ...]
        self.template_nodes = {}  # template_id -> TemplateDependencyNodeItem
        self.template_edges = {}  # template_id -> EdgeItem
        self._duration_marker_items = []  # 最短完了日数の目盛り（線・矢印・ラベル）
        self.reload()

    def reload(self):
        self.clear()
        self.nodes.clear()
        self.edges.clear()
        self.adjacency.clear()
        self.template_nodes.clear()
        self.template_edges.clear()
        # self.clear() で以前の目盛りアイテムも破棄済みのため、参照を捨てておく
        # （_refresh_duration_marker が誤って破棄済みアイテムに触れないように）。
        self._duration_marker_items = []

        colors = team_color_map(self.db.list_teams())
        tasks = self.db.list_workflow_tasks(self.workflow_id)
        deps = self.db.list_task_dependencies(self.workflow_id)
        templates = self.db.list_dependency_templates(self.workflow_id)
        # 座標はDBに保存しない（ドラッグは保存されない一時的な並べ替えとして
        # のみ許可する。auto_arrangeと同じ理由・同じ関数、docs/architecture.md
        # 参照）ため、開くたびに依存の深さに基づくレイアウトを計算し直す。
        positions = compute_combined_layout(tasks, deps, templates)

        for t in tasks:
            node = TaskNodeItem(
                t["id"], t["name"], t["team_name"], colors.get(t["team_id"], "#cbc9c2"),
                t["default_days"],
            )
            x, y = positions.get(t["id"], (0, 0))
            node.setPos(x, y)
            self.addItem(node)
            self.nodes[t["id"]] = node

        for d in deps:
            pred = self.nodes.get(d["predecessor_task_id"])
            succ = self.nodes.get(d["successor_task_id"])
            if pred is None or succ is None:
                continue
            edge = EdgeItem(d["id"], pred, succ,
                            dep_type=d["dep_type"], lag_days=d["lag_days"])
            self.addItem(edge)
            self.edges[d["id"]] = edge
            pred.edges.append(edge)
            succ.edges.append(edge)
            self.adjacency.setdefault(d["predecessor_task_id"], []).append(d["successor_task_id"])

        for tpl in templates:
            self._add_template_scene_item(tpl)
            node = self.template_nodes.get(tpl["id"])
            pos = positions.get(("template", tpl["id"]))
            if node is None or pos is None:
                continue
            node.setPos(*pos)
            self.template_edges[tpl["id"]].update_path()

        self._refresh_duration_marker(tasks, deps, positions)
        self._update_scene_rect()

        if self.on_changed:
            self.on_changed()

    def _update_scene_rect(self):
        """全ノード・疑似ノードが収まるよう、シーンの矩形を配置後の内容に
        合わせて広げ直す。

        固定サイズ（旧: setSceneRect(-2000, -2000, 4000, 4000)）のままだと、
        ワークフローが横に長くなって実際のノード配置がその範囲をはみ出した
        場合、ビューのスクロール可能範囲（＝パンやfit_allで到達できる範囲）が
        QGraphicsViewの仕様上シーン矩形に固定されてしまい、右端のノードまで
        表示・パンできなくなる。ドラッグで自由に動かせる余白は残しつつ、
        レイアウトが変わるたび（reload/auto_arrange）に実際の内容を包む
        矩形へ広げ直す。"""
        margin = 400
        bounds = self.itemsBoundingRect().adjusted(-margin, -margin, margin, margin)
        base = QRectF(-2000, -2000, 4000, 4000)
        self.setSceneRect(bounds.united(base))

    # -- 依存テンプレート（他ワークフローへの依存）の疑似ノード ------------------------

    def _add_template_scene_item(self, tpl):
        """list_dependency_templates() の1行から疑似ノード・接続線を作り、
        シーンに追加する（reload/add/updateで共有）。対象タスクが見つからない
        場合（データ不整合）は何もしない。"""
        target_node = self.nodes.get(tpl["workflow_task_id"])
        if target_node is None:
            return
        node = TemplateDependencyNodeItem(
            tpl["id"], tpl["workflow_task_id"],
            tpl["depends_on_workflow_name"], tpl["depends_on_task_name"],
        )
        self.addItem(node)
        self.template_nodes[tpl["id"]] = node
        edge = EdgeItem(tpl["id"], node, target_node, color=TEMPLATE_EDGE_COLOR, line_style=Qt.DashLine)
        self.addItem(edge)
        self.template_edges[tpl["id"]] = edge
        # 対象タスクノードが動いた際にこの線も追従できるよう、対象ノード側にも
        # 登録しておく（TaskNodeItem.itemChange参照）。
        target_node.incoming_template_edges.append(edge)

    def _remove_template_scene_item(self, template_id):
        edge = self.template_edges.pop(template_id, None)
        if edge is not None:
            if edge in edge.succ_node.incoming_template_edges:
                edge.succ_node.incoming_template_edges.remove(edge)
            self.removeItem(edge)
        node = self.template_nodes.pop(template_id, None)
        if node is not None:
            self.removeItem(node)

    def add_dependency_template_node(self, workflow_task_id, depends_on_workflow_id, depends_on_workflow_task_id):
        with self.db.undo_group("依存テンプレートを追加"):
            template_id = self.db.add_dependency_template(
                self.workflow_id, workflow_task_id, depends_on_workflow_id, depends_on_workflow_task_id,
            )
            tpl = next(t for t in self.db.list_dependency_templates(self.workflow_id) if t["id"] == template_id)
            self._add_template_scene_item(tpl)
            self.auto_arrange()

    def update_dependency_template_node(self, template_id, workflow_task_id,
                                         depends_on_workflow_id, depends_on_workflow_task_id):
        with self.db.undo_group("依存テンプレートを変更"):
            self.db.update_dependency_template(
                template_id, workflow_task_id, depends_on_workflow_id, depends_on_workflow_task_id,
            )
            # 対象タスクや依存先が変わりうるため、疑似ノードは作り直す。
            self._remove_template_scene_item(template_id)
            tpl = next(t for t in self.db.list_dependency_templates(self.workflow_id) if t["id"] == template_id)
            self._add_template_scene_item(tpl)
            self.auto_arrange()

    def delete_dependency_template_node(self, template_id):
        with self.db.undo_group("依存テンプレートを削除"):
            self.db.delete_dependency_template(template_id)
            self._remove_template_scene_item(template_id)
            self.auto_arrange()

    def _would_create_cycle(self, pred_id, succ_id):
        if pred_id == succ_id:
            return True
        stack = [succ_id]
        seen = set()
        while stack:
            node_id = stack.pop()
            if node_id == pred_id:
                return True
            if node_id in seen:
                continue
            seen.add(node_id)
            stack.extend(self.adjacency.get(node_id, []))
        return False

    def try_add_edge(self, pred_node, succ_node):
        pred_id, succ_id = pred_node.workflow_task_id, succ_node.workflow_task_id
        if pred_id == succ_id:
            return
        if succ_id in self.adjacency.get(pred_id, []):
            return  # 既に存在する依存
        if self._would_create_cycle(pred_id, succ_id):
            QMessageBox.warning(
                self.parent_widget, "循環依存",
                "この依存関係を追加すると循環依存になるため、追加できません。",
            )
            return
        with self.db.undo_group("依存関係を追加"):
            try:
                dep_id = self.db.add_task_dependency(self.workflow_id, pred_id, succ_id)
            except ProjectDatabaseError as e:
                QMessageBox.warning(self.parent_widget, "エラー", str(e))
                return
            # 依存関係が増えると「先行タスクの実効マイルストーン <= 後続タスクの
            # 実効マイルストーン」の判定対象が増えるため、マイルストーンの日付を
            # 触っていなくてもジョブ側の整合性が崩れうる。まだUndo単位が開いて
            # いるここで確認・再調整しておく（キャンセルなら依存の追加ごと取り消す
            # ——同じ単位の中で差し引きゼロになり、Undoエントリも積まれない）。
            if not confirm_and_repair_milestone_consistency(
                self.db, self.parent_widget, "この依存関係の追加",
            ):
                self.db.delete_task_dependency(dep_id)
                return
            edge = EdgeItem(dep_id, pred_node, succ_node)
            self.addItem(edge)
            self.edges[dep_id] = edge
            pred_node.edges.append(edge)
            succ_node.edges.append(edge)
            self.adjacency.setdefault(pred_id, []).append(succ_id)
            self.auto_arrange()

    def update_edge_kind(self, dependency_id, dep_type, lag_days):
        """依存関係の種別（FS/SS）とラグ（営業日）を変更する。

        依存の向きは変えないため循環依存の再検査は不要。マイルストーンの
        整合性（先行タスクの実効マイルストーン <= 後続）も、依存の有無が
        変わらない以上ここでは崩れない。
        """
        edge = self.edges.get(dependency_id)
        if edge is None:
            return
        try:
            self.db.update_task_dependency(dependency_id, dep_type, lag_days)
        except ProjectDatabaseError as e:
            QMessageBox.warning(self.parent_widget, "エラー", str(e))
            return
        edge.set_dependency_kind(dep_type, lag_days)
        if self.on_changed:
            self.on_changed()

    def delete_edge(self, edge):
        with self.db.undo_group("依存関係を削除"):
            self.db.delete_task_dependency(edge.dependency_id)
            pred_id, succ_id = edge.pred_node.workflow_task_id, edge.succ_node.workflow_task_id
            if succ_id in self.adjacency.get(pred_id, []):
                self.adjacency[pred_id].remove(succ_id)
            edge.pred_node.edges.remove(edge)
            edge.succ_node.edges.remove(edge)
            del self.edges[edge.dependency_id]
            self.removeItem(edge)
            self.auto_arrange()

    def delete_node(self, node):
        if not self.confirm_delete_nodes([node]):
            return
        self._delete_node_unconfirmed(node)

    def confirm_delete_nodes(self, nodes):
        """削除しようとしているタスク群について、他所から参照されている分が
        あれば確認ダイアログを1回だけ出す。複数選択してまとめて削除する場合
        （gui/node_canvas.py の _delete_selected）でも、ノードの数だけダイアログが
        繰り返し出ないよう、対象ノード全体の被参照件数をまとめて確認する。

        文面はノード数に応じて単数形/複数形を切り替える——1件なら「このタスクは」、
        複数件なら「選択した3件のタスクは」のように、対象がどれなのか誤解の
        無いようにする。"""
        if not nodes:
            return True
        # ノード同士の依存関係（task_dependencies）は両端でそれぞれ数えられるため、
        # 選択範囲内の依存はここで多重に計上されうるが、これは「削除して問題
        # ないか」を大まかに伝えるための件数であり、厳密な重複排除はしない
        # （削除自体は対象ノードすべてで行われるため、実害は無い）。
        usage = sum(self.db.workflow_task_usage_count(n.workflow_task_id) for n in nodes)
        if usage <= 0:
            return True
        if len(nodes) == 1:
            message = (
                f"このタスクは {usage} 件のジョブ設定/依存関係から参照されています。"
                "削除すると、それらの参照も削除されます。続行しますか？"
            )
        else:
            message = (
                f"選択した{len(nodes)}件のタスクは、合計{usage}件のジョブ設定/依存関係"
                "から参照されています。削除すると、それらの参照も削除されます。"
                "続行しますか？"
            )
        reply = QMessageBox.question(
            self.parent_widget, "削除の確認", message,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _delete_node_unconfirmed(self, node):
        """確認ダイアログを経ずにノード1件を削除する内部処理。確認は呼び出し側
        （delete_node、または複数選択時はまとめて1回）の責務とする。

        削除するタスクの直前・直後のタスクは、削除後も前後関係が保たれるよう
        橋渡しの依存関係で繋ぎ直す（例: A→B→Cという流れでBを削除すると、
        A→Cになる）。分岐がある場合は先行タスク全体×後続タスク全体の
        組み合わせすべてに橋渡しする。元の依存グラフに閉路が無い前提であれば、
        この橋渡しが新たな閉路を生むことはない——削除するタスクが先行タスクから
        後続タスクへ至る経路上にあった以上、橋渡し先は元々到達可能だったため。
        橋渡し先の依存が既に存在する場合（他の経路で既に繋がっている場合）は
        try_add_edge が重複させずスキップする。

        複数選択でまとめて削除する場合（_delete_selected）も、1件ずつ順に
        この処理を通すことで連鎖的な橋渡しが正しく働く。例えばA→B→C→D→Eから
        B・Dをまとめて削除すると、Bの処理でA→Cが繋がり、続くDの処理はその
        時点の後続関係（C→E）を見るため、最終的にA→C→Eになる。

        このタスクを対象とする依存テンプレート（workflow_task_id側）は、
        DBスキーマのON DELETE CASCADEによりdelete_workflow_task内で自動的に
        削除される。DB側はUndoスナップショットに含まれるため特別な対応は
        不要だが、キャンバス上の疑似ノード・接続線はQtの管理下にあり自動的には
        消えないため、ここで明示的に取り除く。"""
        pred_nodes = [edge.pred_node for edge in node.edges if edge.succ_node is node]
        succ_nodes = [edge.succ_node for edge in node.edges if edge.pred_node is node]
        attached_template_ids = [
            template_id for template_id, tnode in self.template_nodes.items()
            if tnode.target_task_id == node.workflow_task_id
        ]
        with self.db.undo_group("タスクを削除"):
            for edge in list(node.edges):
                self.delete_edge(edge)
            self.db.delete_workflow_task(node.workflow_task_id)
            for template_id in attached_template_ids:
                self._remove_template_scene_item(template_id)
            del self.nodes[node.workflow_task_id]
            self.removeItem(node)
            for pred_node in pred_nodes:
                for succ_node in succ_nodes:
                    self.try_add_edge(pred_node, succ_node)
            self.auto_arrange()

    def add_task(self, name, team_id, days, x, y):
        with self.db.undo_group(f"タスク「{name}」を追加"):
            task_id = self.db.add_workflow_task(self.workflow_id, name, team_id, days)
            colors = team_color_map(self.db.list_teams())
            team = next(t for t in self.db.list_teams() if t["id"] == team_id)
            node = TaskNodeItem(task_id, name, team["name"], colors.get(team_id, "#cbc9c2"), days)
            node.setPos(x, y)
            self.addItem(node)
            self.nodes[task_id] = node
            self.auto_arrange()
        return node

    def auto_arrange(self):
        """ノード情報（タスクの追加・編集・削除、依存関係・依存テンプレートの
        追加・削除）が変わるたびに呼び出し、依存の深さに基づく自動レイアウトへ
        全ノード・疑似ノードを整列し直す（compute_combined_layout、
        gui/node_canvas.py冒頭参照）。

        座標はDBに保存しない——タスク・依存テンプレートの疑似ノードいずれも
        同じ扱いで、その場でQt側の位置を更新するだけにとどめる。手動で
        ドラッグした位置は、次に何か編集する・ワークフローを開き直すと
        この自動レイアウトに揃う（一時的な並べ替えとしてのみ有効）。"""
        tasks = self.db.list_workflow_tasks(self.workflow_id)
        deps = self.db.list_task_dependencies(self.workflow_id)
        templates = self.db.list_dependency_templates(self.workflow_id)
        positions = compute_combined_layout(tasks, deps, templates)
        for key, (x, y) in positions.items():
            if isinstance(key, tuple):
                _kind, template_id = key
                node = self.template_nodes.get(template_id)
                if node is None:
                    continue
                node.setPos(x, y)
                edge = self.template_edges.get(template_id)
                if edge is not None:
                    edge.update_path()
            else:
                node = self.nodes.get(key)
                if node is None:
                    continue
                node.setPos(x, y)
        self._refresh_duration_marker(tasks, deps, positions)
        self._update_scene_rect()
        if self.on_changed:
            self.on_changed()

    # -- ワークフロー全体の最短完了日数の目盛り ----------------------------------------
    #
    # ｜←-- 最短N日 --→｜ のイメージで、キャンバス上部（現在配置されている
    # 全ノード・疑似ノードの最上段よりさらに上）に表示する。ドラッグ中の
    # 一時的な位置には追従しない——座標そのものが「自動整列し直すたびに
    # compute_combined_layoutへ揃う一時的な値」という既存の割り切り
    # （auto_arrange参照）と同じ扱いにして良い、付随的な注記情報のため。

    def _remove_duration_marker(self):
        for item in self._duration_marker_items:
            self.removeItem(item)
        self._duration_marker_items = []

    def _refresh_duration_marker(self, tasks, deps, positions):
        self._remove_duration_marker()
        task_positions = [positions[t["id"]] for t in tasks if t["id"] in positions]
        if not task_positions:
            return

        x_left = min(x for x, _y in task_positions)
        x_right = max(x for x, _y in task_positions) + NODE_WIDTH
        marker_y = min(y for _x, y in positions.values()) - DURATION_MARKER_MARGIN

        color = QColor(DURATION_MARKER_COLOR)
        pen = QPen(color, 1.5)
        items = []

        label_item = QGraphicsSimpleTextItem(f"最短{compute_workflow_min_duration(tasks, deps)}日")
        font = label_item.font()
        font.setBold(True)
        label_item.setFont(font)
        label_item.setBrush(QBrush(color))
        label_rect = label_item.boundingRect()
        center_x = (x_left + x_right) / 2
        label_half = label_rect.width() / 2 + DURATION_MARKER_LABEL_GAP
        label_item.setPos(center_x - label_rect.width() / 2, marker_y - label_rect.height() / 2)
        self.addItem(label_item)
        items.append(label_item)

        # ラベルの両側に線分を引く（ラベルの方が目盛りの全幅より広い場合は
        # 線分を省き、目盛り線・矢印だけにする）。
        if x_right - x_left > label_half * 2:
            left_line = self.addLine(x_left, marker_y, center_x - label_half, marker_y, pen)
            right_line = self.addLine(center_x + label_half, marker_y, x_right, marker_y, pen)
            items += [left_line, right_line]

        tick_half = DURATION_MARKER_TICK_HEIGHT / 2
        items.append(self.addLine(x_left, marker_y - tick_half, x_left, marker_y + tick_half, pen))
        items.append(self.addLine(x_right, marker_y - tick_half, x_right, marker_y + tick_half, pen))

        for tip_x, direction in ((x_left + DURATION_MARKER_ARROW_INSET, QPointF(-1, 0)),
                                  (x_right - DURATION_MARKER_ARROW_INSET, QPointF(1, 0))):
            arrow = QGraphicsPolygonItem(
                _arrow_polygon(QPointF(tip_x, marker_y), direction, DURATION_MARKER_ARROW_SIZE)
            )
            arrow.setBrush(QBrush(color))
            arrow.setPen(QPen(Qt.NoPen))
            self.addItem(arrow)
            items.append(arrow)

        for item in items:
            item.setZValue(-1)
        self._duration_marker_items = items

    def refresh_colors(self):
        """チームマスタが変わった際、既存ノードの色を再計算する。"""
        colors = team_color_map(self.db.list_teams())
        for t in self.db.list_workflow_tasks(self.workflow_id):
            node = self.nodes.get(t["id"])
            if node:
                node.set_color(colors.get(t["team_id"], "#cbc9c2"))
                node.update_labels(t["name"], t["team_name"], t["default_days"])
        if self.on_changed:
            self.on_changed()


class TaskNodeEditDialog(QDialog):
    """タスクの追加・編集用モーダルダイアログ。チーム未登録時や、既存チームに
    無い担当を割り当てたい場合に備え、コンボの末尾から即席でチームを追加できる。"""

    def __init__(self, db, title, name="", team_id=None, days=1,
                 workflow_id=None, task_id=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(title)

        form = QFormLayout(self)

        self.name_edit = QLineEdit(name)
        form.addRow("タスク名", self.name_edit)

        self.team_combo = NoWheelComboBox()
        self._reload_teams(team_id)
        self.team_combo.activated.connect(self._on_team_activated)
        form.addRow("担当チーム", self.team_combo)

        self.days_spin = NoWheelSpinBox()
        self.days_spin.setRange(1, 9999)
        self.days_spin.setValue(days)
        form.addRow("所要日数", self.days_spin)

        self.predecessor_list = NoWheelListWidget()
        self.predecessor_list.setSelectionMode(NoWheelListWidget.MultiSelection)
        self.predecessor_list.setMaximumHeight(120)
        if workflow_id is not None:
            deps = db.list_task_dependencies(workflow_id)
            current_preds = (
                {d["predecessor_task_id"] for d in deps if d["successor_task_id"] == task_id}
                if task_id is not None else set()
            )
            for t in db.list_workflow_tasks(workflow_id):
                if task_id is not None and t["id"] == task_id:
                    continue  # 自分自身は先行タスクにできない
                item = QListWidgetItem(t["name"])
                item.setData(Qt.UserRole, t["id"])
                self.predecessor_list.addItem(item)
                if t["id"] in current_preds:
                    item.setSelected(True)
        form.addRow("先行タスク（このタスクの前に完了が必要）", self.predecessor_list)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _reload_teams(self, select_team_id=None):
        self.team_combo.clear()
        for t in self.db.list_teams():
            self.team_combo.addItem(t["name"], t["id"])
        self.team_combo.addItem("＋ 新しいチームを追加...", _ADD_TEAM_SENTINEL)
        if select_team_id is not None:
            idx = self.team_combo.findData(select_team_id)
            if idx >= 0:
                self.team_combo.setCurrentIndex(idx)

    def _on_team_activated(self, index):
        if self.team_combo.itemData(index) != _ADD_TEAM_SENTINEL:
            return
        name, ok = QInputDialog.getText(self, "新しいチーム", "チーム名:")
        if not ok or not name.strip():
            self._reload_teams()
            return
        lines, ok = QInputDialog.getInt(self, "新しいチーム", "同時ライン数:", 1, 1, 999)
        if not ok:
            self._reload_teams()
            return
        try:
            new_id = self.db.add_team(name.strip(), lines)
        except DuplicateNameError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            self._reload_teams()
            return
        self._reload_teams(select_team_id=new_id)

    def values(self):
        return self.name_edit.text().strip(), self.team_combo.currentData(), self.days_spin.value()

    def selected_predecessor_ids(self):
        return {item.data(Qt.UserRole) for item in self.predecessor_list.selectedItems()}


class DependencyKindDialog(QDialog):
    """依存関係（ワークフロー内の1本のエッジ）の種別とラグを編集するダイアログ。

    先行/後続の組み合わせ自体は変更させない。向きを変えるのは「別の依存関係」
    であり循環依存の再検査が要るため、既存の依存を消して引き直す操作
    （＝キャンバス上のドラッグ）に任せる。
    """

    def __init__(self, pred_name, succ_name, dep_type="FS", lag_days=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("依存関係を編集")

        form = QFormLayout(self)
        form.addRow("依存関係", QLabel(f"{pred_name} → {succ_name}"))

        self.kind_combo = NoWheelComboBox()
        self.kind_combo.addItem("完了 → 開始（FS）", "FS")
        self.kind_combo.addItem("開始 → 開始（SS）", "SS")
        idx = self.kind_combo.findData((dep_type or "FS").upper())
        self.kind_combo.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("種別", self.kind_combo)

        self.lag_spin = NoWheelSpinBox()
        self.lag_spin.setRange(-MAX_LAG_DAYS, MAX_LAG_DAYS)
        self.lag_spin.setValue(int(lag_days or 0))
        self.lag_spin.setSuffix(" 営業日")
        form.addRow("ラグ", self.lag_spin)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #6b6a66;")
        form.addRow("", self.hint)
        self.kind_combo.currentIndexChanged.connect(self._update_hint)
        self.lag_spin.valueChanged.connect(self._update_hint)
        self._update_hint()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _update_hint(self):
        """設定した内容を日本語の一文で言い直す。FS/SS・正負のラグの
        組み合わせは意味を取り違えやすいので、記号のまま確定させない。"""
        kind = self.kind_combo.currentData()
        lag = self.lag_spin.value()
        anchor = "先行タスクの完了後" if kind == "FS" else "先行タスクの開始と同時"
        if lag == 0:
            text = f"{anchor}に後続タスクを開始する。"
        elif lag > 0:
            base = "完了後" if kind == "FS" else "開始から"
            text = f"先行タスクの{base} {lag} 営業日空けてから後続タスクを開始する。"
        else:
            base = "完了" if kind == "FS" else "開始"
            text = f"先行タスクの{base}より {abs(lag)} 営業日早く後続タスクを開始できる。"
        self.hint.setText(text)

    def values(self):
        return self.kind_combo.currentData(), self.lag_spin.value()


class DependencyTemplateDialog(QDialog):
    """依存テンプレート（ワークフローペア単位の既定タスク対応）の追加・編集ダイアログ。
    「このワークフローのタスク」は現在選択中のワークフロー内のタスクに固定し、
    依存先ワークフロー→依存先タスクをカスケードのドロップダウンで選ばせる
    （追加・編集のいずれも同じ3つのドロップダウンから後から選び直せる）。

    ノードビュー（右クリックメニュー・疑似ノードの編集）とテーブルビュー
    （依存テンプレート欄）の両方から共通で使う。"""

    def __init__(self, db, workflow_id, parent=None, initial=None):
        super().__init__(parent)
        self.db = db
        self.workflow_id = workflow_id
        self.setWindowTitle("依存テンプレートを編集" if initial else "依存テンプレートを追加")

        form = QFormLayout(self)

        self.task_combo = NoWheelComboBox()
        for t in db.list_workflow_tasks(workflow_id):
            self.task_combo.addItem(t["name"], t["id"])
        form.addRow("このワークフローのタスク", self.task_combo)

        self.target_workflow_combo = NoWheelComboBox()
        for wf in db.list_workflows():
            if wf["id"] != workflow_id:
                self.target_workflow_combo.addItem(wf["name"], wf["id"])
        self.target_workflow_combo.currentIndexChanged.connect(self._reload_target_tasks)

        self.target_task_combo = NoWheelComboBox()

        form.addRow("依存先ワークフロー", self.target_workflow_combo)
        form.addRow("依存先タスク", self.target_task_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        if initial is not None:
            task_id, target_workflow_id, target_task_id = initial
            idx = self.task_combo.findData(task_id)
            if idx >= 0:
                self.task_combo.setCurrentIndex(idx)
            idx = self.target_workflow_combo.findData(target_workflow_id)
            if idx >= 0:
                self.target_workflow_combo.setCurrentIndex(idx)
        self._reload_target_tasks()
        if initial is not None:
            idx = self.target_task_combo.findData(initial[2])
            if idx >= 0:
                self.target_task_combo.setCurrentIndex(idx)

    def _reload_target_tasks(self):
        self.target_task_combo.clear()
        target_workflow_id = self.target_workflow_combo.currentData()
        if target_workflow_id is None:
            return
        for t in self.db.list_workflow_tasks(target_workflow_id):
            self.target_task_combo.addItem(t["name"], t["id"])

    def values(self):
        return (
            self.task_combo.currentData(),
            self.target_workflow_combo.currentData(),
            self.target_task_combo.currentData(),
        )


# -- ダイアログを開いてDB/シーンへ反映する共通ロジック ---------------------------------
#
# ノードビュー（gui/node_canvas.py の WorkflowGraphView、右クリックメニュー）と
# テーブルビュー（gui/tab_workflows.py の CrudSection、＋追加/編集...ボタン）の
# 両方から同じ関数を呼び、挙動が2箇所で食い違わないようにする。


def add_task_via_dialog(scene, parent, x=0.0, y=0.0):
    db = scene.db
    if not db.list_teams():
        QMessageBox.information(
            parent, "チーム未登録",
            "先にチームを1つ以上登録してください（このダイアログからも追加できます）。",
        )
    dialog = TaskNodeEditDialog(db, "タスクを追加", workflow_id=scene.workflow_id)
    if dialog.exec() != QDialog.Accepted:
        return
    name, team_id, days = dialog.values()
    if not name or team_id is None or team_id == _ADD_TEAM_SENTINEL:
        QMessageBox.warning(parent, "入力エラー", "タスク名とチームを指定してください。")
        return
    with db.undo_group(f"タスク「{name}」を追加"):
        node = scene.add_task(name, team_id, days, x, y)
        apply_predecessors(scene, node, dialog.selected_predecessor_ids())


def edit_task_via_dialog(scene, parent, node):
    db = scene.db
    current = next(t for t in db.list_workflow_tasks(scene.workflow_id) if t["id"] == node.workflow_task_id)
    dialog = TaskNodeEditDialog(
        db, "タスクを編集", name=current["name"], team_id=current["team_id"],
        days=current["default_days"], workflow_id=scene.workflow_id,
        task_id=node.workflow_task_id,
    )
    if dialog.exec() != QDialog.Accepted:
        return
    name, team_id, days = dialog.values()
    if not name or team_id is None or team_id == _ADD_TEAM_SENTINEL:
        QMessageBox.warning(parent, "入力エラー", "タスク名とチームを指定してください。")
        return
    with db.undo_group(f"タスク「{name}」を編集"):
        try:
            db.update_workflow_task(node.workflow_task_id, name, team_id, days)
        except DuplicateNameError as e:
            QMessageBox.warning(parent, "変更できません", str(e))
            return
        colors = team_color_map(db.list_teams())
        team = next(t for t in db.list_teams() if t["id"] == team_id)
        node.update_labels(name, team["name"], days)
        node.set_color(colors.get(team_id, "#cbc9c2"))
        apply_predecessors(scene, node, dialog.selected_predecessor_ids())
        scene.auto_arrange()


def apply_predecessors(scene, node, predecessor_task_ids):
    """タスク追加・編集ダイアログで選択された先行タスク集合を、実際の
    task_dependencies行に反映する（追加分・削除分の差分のみ処理）。
    循環依存になる追加は scene.try_add_edge が警告して拒否する。"""
    current_pred_ids = {
        edge.pred_node.workflow_task_id for edge in node.edges if edge.succ_node is node
    }
    for pred_id in predecessor_task_ids - current_pred_ids:
        pred_node = scene.nodes.get(pred_id)
        if pred_node is not None:
            scene.try_add_edge(pred_node, node)
    for pred_id in current_pred_ids - predecessor_task_ids:
        edge = next(
            (e for e in node.edges
             if e.succ_node is node and e.pred_node.workflow_task_id == pred_id),
            None,
        )
        if edge is not None:
            scene.delete_edge(edge)


def edit_edge_via_dialog(scene, parent, edge):
    db = scene.db
    current = db.get_task_dependency(edge.dependency_id)
    if current is None:
        return
    names = {t["id"]: t["name"] for t in db.list_workflow_tasks(scene.workflow_id)}
    dialog = DependencyKindDialog(
        names.get(current["predecessor_task_id"], "?"),
        names.get(current["successor_task_id"], "?"),
        current["dep_type"], current["lag_days"], parent,
    )
    if dialog.exec() != QDialog.Accepted:
        return
    dep_type, lag_days = dialog.values()
    if (dep_type, lag_days) == (current["dep_type"], current["lag_days"]):
        return  # 変更なし。Undo履歴に空のエントリを積まない
    scene.update_edge_kind(edge.dependency_id, dep_type, lag_days)


def add_template_via_dialog(scene, parent):
    db = scene.db
    if not db.list_workflow_tasks(scene.workflow_id):
        QMessageBox.information(parent, "タスク未登録", "先にこのワークフローにタスクを1つ以上追加してください。")
        return
    other_workflows = [w for w in db.list_workflows() if w["id"] != scene.workflow_id]
    if not other_workflows:
        QMessageBox.information(parent, "依存先ワークフローがありません", "他のワークフローを先に作成してください。")
        return
    dialog = DependencyTemplateDialog(db, scene.workflow_id, parent)
    if dialog.exec() != QDialog.Accepted:
        return
    workflow_task_id, target_workflow_id, target_task_id = dialog.values()
    if None in (workflow_task_id, target_workflow_id, target_task_id):
        QMessageBox.warning(parent, "入力エラー", "すべての項目を選択してください。")
        return
    try:
        scene.add_dependency_template_node(workflow_task_id, target_workflow_id, target_task_id)
    except ProjectDatabaseError as e:
        QMessageBox.warning(parent, "追加できません", str(e))


def edit_template_via_dialog(scene, parent, template_id):
    db = scene.db
    current = next(t for t in db.list_dependency_templates(scene.workflow_id) if t["id"] == template_id)
    dialog = DependencyTemplateDialog(
        db, scene.workflow_id, parent,
        initial=(
            current["workflow_task_id"],
            current["depends_on_workflow_id"],
            current["depends_on_workflow_task_id"],
        ),
    )
    if dialog.exec() != QDialog.Accepted:
        return
    workflow_task_id, target_workflow_id, target_task_id = dialog.values()
    if None in (workflow_task_id, target_workflow_id, target_task_id):
        QMessageBox.warning(parent, "入力エラー", "すべての項目を選択してください。")
        return
    try:
        scene.update_dependency_template_node(template_id, workflow_task_id, target_workflow_id, target_task_id)
    except ProjectDatabaseError as e:
        QMessageBox.warning(parent, "変更できません", str(e))


class WorkflowGraphView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(self.renderHints())
        self.setDragMode(QGraphicsView.RubberBandDrag)
        # 文字色は常に黒固定（QGraphicsSimpleTextItemの既定）で描画しているため、
        # OSがダークモードだと既定の（ダークな）ビュー背景に文字が埋もれて
        # 読めなくなる。この独自キャンバスはOSのテーマに関わらず常に明るい
        # 背景で描くようにし、文字色との組み合わせを固定して視認性を保つ。
        self.setBackgroundBrush(QBrush(QColor("#fdfcf9")))
        # QGraphicsViewは既定でacceptDrops()がTrueになっており、プロジェクト
        # ファイル（.pschedule）をこのビュー上にドラッグ&ドロップしても
        # シーンが受け取らないまま素通りせず、MainWindow.dropEvent（ウィンドウ
        # 全体でのファイルオープン）まで伝播しない。このビュー自体はファイルの
        # ドロップを扱わないため、明示的に無効化してMainWindow側へ委ねる。
        self.setAcceptDrops(False)
        self._connecting_from = None
        self._temp_edge = None
        self._panning = False
        self._pan_last_pos = None

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def fit_all(self):
        """シーン上の全ノードが収まるように表示を合わせる（Aキー、初期表示時）。"""
        if self.scene() is None:
            return
        rect = self.scene().itemsBoundingRect()
        if rect.isEmpty():
            return
        margin = 40
        rect = rect.adjusted(-margin, -margin, margin, margin)
        self.fitInView(rect, Qt.KeepAspectRatio)

    def fit_selected(self):
        """選択中のノードだけが収まるように表示を合わせる（Fキー）。
        選択が無ければ全体表示にフォールバックする。"""
        if self.scene() is None:
            return
        selected = self.scene().selectedItems()
        if not selected:
            self.fit_all()
            return
        rect = QRectF()
        for item in selected:
            rect = rect.united(item.sceneBoundingRect())
        margin = 60
        rect = rect.adjusted(-margin, -margin, margin, margin)
        self.fitInView(rect, Qt.KeepAspectRatio)

    def mouseDoubleClickEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        item = self.scene().itemAt(scene_pos, self.transform()) if self.scene() else None
        node, template_node = self._resolve_hit(item)
        if node is not None:
            self._edit_node(node)
            event.accept()
            return
        if template_node is not None:
            edit_template_via_dialog(self.scene(), self, template_node.template_id)
            event.accept()
            return
        edge = self._resolve_edge_hit(item)
        if edge is not None:
            edit_edge_via_dialog(self.scene(), self, edge)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            # 中ボタンドラッグでキャンバスを平行移動（パン）できるようにする。
            # ノードのクリック/選択やルバーバンド選択（左ボタン）とは独立した操作。
            self._panning = True
            self._pan_last_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        scene_pos = self.mapToScene(event.pos())
        item = self.scene().itemAt(scene_pos, self.transform()) if self.scene() else None
        if isinstance(item, AnchorItem) and item.role == "output":
            self._connecting_from = item.parentItem()
            self._temp_edge = QGraphicsPathItem()
            self._temp_edge.setPen(QPen(QColor("#2a78d6"), 2, Qt.DashLine))
            self._temp_edge.setZValue(10)
            self.scene().addItem(self._temp_edge)
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
        if self._connecting_from is not None:
            start = self._connecting_from.output_anchor_scene_pos()
            end = self.mapToScene(event.pos())
            path = QPainterPath(start)
            path.lineTo(end)
            self._temp_edge.setPath(path)
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
        if self._connecting_from is not None:
            scene_pos = self.mapToScene(event.pos())
            # ドラッグ中の仮の破線（_temp_edge）はリリース位置ちょうどに終端を持つ
            # 上、zValueも高いため、隠さずにitemAtを呼ぶと自分自身がヒットして
            # しまい、本来の接続先ノード/アンカーを検出できない。判定前に必ず隠す。
            self._temp_edge.hide()
            item = self.scene().itemAt(scene_pos, self.transform())
            target_node = None
            if isinstance(item, AnchorItem):
                target_node = item.parentItem()
            elif isinstance(item, TaskNodeItem):
                target_node = item
            elif item is not None and isinstance(item.parentItem(), TaskNodeItem):
                target_node = item.parentItem()
            if target_node is not None and target_node is not self._connecting_from:
                self.scene().try_add_edge(self._connecting_from, target_node)
            self.scene().removeItem(self._temp_edge)
            self._temp_edge = None
            self._connecting_from = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self._delete_selected()
            event.accept()
            return
        if event.key() == Qt.Key_A:
            self.fit_all()
            event.accept()
            return
        if event.key() == Qt.Key_F:
            self.fit_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    def _delete_selected(self):
        """選択中のタスク・依存関係・依存テンプレート疑似ノードをまとめて削除する。

        複数選択に対する一括操作は1つのUndo単位にまとめる（選択項目ごとに
        Undoが分かれると、1回のDeleteを取り消すのに複数回のUndoが必要に
        なってしまう）。種類を問わず、選択されていたものは常に1つのUndo単位に
        まとめるため、ラベルは組み合わせ爆発を避けて「選択した項目を削除」に
        統一する。

        タスクを削除するとそのタスクに繋がる依存関係・依存テンプレートも
        一緒に消えるため、タスクを先に処理し、依存関係・依存テンプレートは
        「まだ残っているもの」だけを削除する（同時選択時の二重削除を防ぐ）。

        参照されているタスクが選択に含まれる場合の確認ダイアログは、ノードの
        数だけ繰り返さず、選択範囲全体でまとめて1回だけ出す
        （WorkflowGraphScene.confirm_delete_nodes 参照）。依存テンプレート
        疑似ノードの削除は、テーブルビュー側の削除と同様に無確認のままとし、
        この確認ダイアログの要否・件数には影響させない。"""
        scene = self.scene()
        if scene is None:
            return
        selected = list(scene.selectedItems())
        nodes = [i for i in selected if isinstance(i, TaskNodeItem)]
        edges = [i for i in selected if isinstance(i, EdgeItem)]
        template_nodes = [i for i in selected if isinstance(i, TemplateDependencyNodeItem)]
        if not nodes and not edges and not template_nodes:
            return
        if not scene.confirm_delete_nodes(nodes):
            return
        with scene.db.undo_group("選択した項目を削除"):
            for node in nodes:
                if node.workflow_task_id in scene.nodes:
                    scene._delete_node_unconfirmed(node)
            for tnode in template_nodes:
                if tnode.template_id in scene.template_nodes:
                    scene.delete_dependency_template_node(tnode.template_id)
            for edge in edges:
                if edge.dependency_id in scene.edges:
                    scene.delete_edge(edge)

    def _resolve_hit(self, item):
        """クリック/ダブルクリック位置のアイテムから、タスクノード・依存
        テンプレート疑似ノードのどちらか（またはどちらでもない）を判定する。
        両ノードとも子要素（ラベル等）をクリックした場合を考慮し、親を辿る。"""
        if isinstance(item, TaskNodeItem):
            return item, None
        if isinstance(item, TemplateDependencyNodeItem):
            return None, item
        if item is not None and isinstance(item.parentItem(), TaskNodeItem):
            return item.parentItem(), None
        if item is not None and isinstance(item.parentItem(), TemplateDependencyNodeItem):
            return None, item.parentItem()
        return None, None

    def _resolve_edge_hit(self, item):
        """ワークフロー内の依存関係のエッジを判定する。矢印・種別ラベルなど
        子要素をクリックした場合に備えて親を辿る（依存テンプレートのエッジは
        疑似ノード側で編集するため、ここでは対象外にする）。"""
        while item is not None:
            if isinstance(item, EdgeItem):
                scene = self.scene()
                if scene is not None and scene.edges.get(item.dependency_id) is item:
                    return item
                return None
            item = item.parentItem()
        return None

    def contextMenuEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        item = self.scene().itemAt(scene_pos, self.transform()) if self.scene() else None
        node, template_node = self._resolve_hit(item)
        edge = self._resolve_edge_hit(item)

        menu = QMenu(self)
        if node is not None:
            edit_action = menu.addAction("編集...")
            delete_action = menu.addAction("削除")
            chosen = menu.exec(event.globalPos())
            if chosen == edit_action:
                self._edit_node(node)
            elif chosen == delete_action:
                self.scene().delete_node(node)
        elif template_node is not None:
            edit_action = menu.addAction("編集...")
            delete_action = menu.addAction("削除")
            chosen = menu.exec(event.globalPos())
            if chosen == edit_action:
                edit_template_via_dialog(self.scene(), self, template_node.template_id)
            elif chosen == delete_action:
                self.scene().delete_dependency_template_node(template_node.template_id)
        elif edge is not None:
            edit_action = menu.addAction("種別・ラグを編集...")
            delete_action = menu.addAction("削除")
            chosen = menu.exec(event.globalPos())
            if chosen == edit_action:
                edit_edge_via_dialog(self.scene(), self, edge)
            elif chosen == delete_action:
                self.scene().delete_edge(edge)
        else:
            add_task_action = menu.addAction("タスクを追加...")
            add_template_action = menu.addAction("依存テンプレートを追加...")
            chosen = menu.exec(event.globalPos())
            if chosen == add_task_action:
                self._add_task_at(scene_pos)
            elif chosen == add_template_action:
                add_template_via_dialog(self.scene(), self)

    def _add_task_at(self, scene_pos):
        add_task_via_dialog(self.scene(), self, scene_pos.x(), scene_pos.y())

    def _edit_node(self, node):
        edit_task_via_dialog(self.scene(), self, node)
