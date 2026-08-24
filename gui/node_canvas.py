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
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen, QPolygonF
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
    QLineEdit,
    QListWidgetItem,
    QMenu,
    QMessageBox,
)

from gui.db import DuplicateNameError, ProjectDatabaseError
from gui.widgets_common import NoWheelComboBox, NoWheelListWidget, NoWheelSpinBox

NODE_WIDTH = 170
NODE_HEIGHT = 64
ANCHOR_RADIUS = 8

_ADD_TEAM_SENTINEL = "__add_new_team__"


def compute_auto_layout(tasks, dependencies, gap_x=NODE_WIDTH + 60, gap_y=NODE_HEIGHT + 30):
    """タスク・依存関係一覧から、依存の深さ（predecessorを持たないタスク=0、
    以降predecessorの深さの最大+1）でレイヤー分けし、左→右に深さ順、各レイヤー
    内は上→下に並べる座標を計算する（純粋関数、Qt/DB非依存）。

    新規ワークフローの初期表示や、既存Excelからの移行データ（座標情報を
    持たない）に初期レイアウトを与えるために使う。

    tasks: [{"id": ...}, ...]
    dependencies: [{"predecessor_task_id": ..., "successor_task_id": ...}, ...]
    Returns: {task_id: (x, y)}
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

    layers = {}
    for t in tasks:
        layers.setdefault(depth[t["id"]], []).append(t)

    positions = {}
    for layer_depth, layer_tasks in layers.items():
        for i, t in enumerate(sorted(layer_tasks, key=lambda t: t["name"])):
            positions[t["id"]] = (layer_depth * gap_x, i * gap_y)
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


class TaskNodeItem(QGraphicsRectItem):
    def __init__(self, workflow_task_id, name, team_name, color_hex, days, on_moved):
        super().__init__(0, 0, NODE_WIDTH, NODE_HEIGHT)
        self.workflow_task_id = workflow_task_id
        self.on_moved = on_moved
        self.edges = []  # 接続中のEdgeItem一覧（移動時の再描画用）

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
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.on_moved(self.workflow_task_id, self.pos().x(), self.pos().y())


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


class EdgeItem(QGraphicsPathItem):
    def __init__(self, dependency_id, pred_node, succ_node):
        super().__init__()
        self.dependency_id = dependency_id
        self.pred_node = pred_node
        self.succ_node = succ_node
        self.setPen(QPen(QColor("#52514e"), 2))
        self.setBrush(Qt.NoBrush)
        self.setZValue(-1)
        self.arrow_item = QGraphicsPolygonItem(self)
        self.arrow_item.setBrush(QBrush(QColor("#52514e")))
        self.arrow_item.setPen(QPen(Qt.NoPen))
        self.update_path()

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


class WorkflowGraphScene(QGraphicsScene):
    def __init__(self, db, workflow_id, parent_widget):
        super().__init__(parent_widget)
        self.db = db
        self.workflow_id = workflow_id
        self.parent_widget = parent_widget
        self.nodes = {}  # workflow_task_id -> TaskNodeItem
        self.edges = {}  # dependency_id -> EdgeItem
        self.adjacency = {}  # predecessor_task_id -> [successor_task_id, ...]
        self.setSceneRect(-2000, -2000, 4000, 4000)
        self.reload()

    def reload(self):
        self.clear()
        self.nodes.clear()
        self.edges.clear()
        self.adjacency.clear()

        colors = team_color_map(self.db.list_teams())
        tasks = self.db.list_workflow_tasks(self.workflow_id)
        for t in tasks:
            node = TaskNodeItem(
                t["id"], t["name"], t["team_name"], colors.get(t["team_id"], "#898781"),
                t["default_days"], on_moved=self._on_node_moved,
            )
            node.setPos(t["canvas_x"], t["canvas_y"])
            self.addItem(node)
            self.nodes[t["id"]] = node

        deps = self.db.list_task_dependencies(self.workflow_id)
        for d in deps:
            pred = self.nodes.get(d["predecessor_task_id"])
            succ = self.nodes.get(d["successor_task_id"])
            if pred is None or succ is None:
                continue
            edge = EdgeItem(d["id"], pred, succ)
            self.addItem(edge)
            self.edges[d["id"]] = edge
            pred.edges.append(edge)
            succ.edges.append(edge)
            self.adjacency.setdefault(d["predecessor_task_id"], []).append(d["successor_task_id"])

    def _on_node_moved(self, workflow_task_id, x, y):
        self.db.update_task_position(workflow_task_id, x, y)

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
        try:
            dep_id = self.db.add_task_dependency(self.workflow_id, pred_id, succ_id)
        except ProjectDatabaseError as e:
            QMessageBox.warning(self.parent_widget, "エラー", str(e))
            return
        edge = EdgeItem(dep_id, pred_node, succ_node)
        self.addItem(edge)
        self.edges[dep_id] = edge
        pred_node.edges.append(edge)
        succ_node.edges.append(edge)
        self.adjacency.setdefault(pred_id, []).append(succ_id)
        self.auto_arrange()

    def delete_edge(self, edge):
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
        usage = self.db.workflow_task_usage_count(node.workflow_task_id)
        if usage > 0:
            reply = QMessageBox.question(
                self.parent_widget, "削除の確認",
                f"このタスクは {usage} 件のジョブ設定/依存関係から参照されています。"
                "削除すると、それらの参照も削除されます。続行しますか？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        for edge in list(node.edges):
            self.delete_edge(edge)
        self.db.delete_workflow_task(node.workflow_task_id)
        del self.nodes[node.workflow_task_id]
        self.removeItem(node)
        self.auto_arrange()

    def add_task(self, name, team_id, days, x, y):
        task_id = self.db.add_workflow_task(self.workflow_id, name, team_id, days, x, y)
        colors = team_color_map(self.db.list_teams())
        team = next(t for t in self.db.list_teams() if t["id"] == team_id)
        node = TaskNodeItem(task_id, name, team["name"], colors.get(team_id, "#898781"),
                             days, on_moved=self._on_node_moved)
        node.setPos(x, y)
        self.addItem(node)
        self.nodes[task_id] = node
        self.auto_arrange()
        return node

    def auto_arrange(self):
        """ノード情報（タスクの追加・編集・削除、依存関係の追加・削除）が
        変わるたびに呼び出し、依存の深さに基づく自動レイアウトへ整列し直す
        （compute_auto_layout、gui/node_canvas.py冒頭参照）。手動でドラッグした
        位置は、次に何か編集するとリセットされる。"""
        tasks = self.db.list_workflow_tasks(self.workflow_id)
        deps = self.db.list_task_dependencies(self.workflow_id)
        positions = compute_auto_layout(tasks, deps)
        for task_id, (x, y) in positions.items():
            node = self.nodes.get(task_id)
            if node is None:
                continue
            node.setPos(x, y)
            self.db.update_task_position(task_id, x, y)

    def refresh_colors(self):
        """チームマスタが変わった際、既存ノードの色を再計算する。"""
        colors = team_color_map(self.db.list_teams())
        for t in self.db.list_workflow_tasks(self.workflow_id):
            node = self.nodes.get(t["id"])
            if node:
                node.set_color(colors.get(t["team_id"], "#898781"))
                node.update_labels(t["name"], t["team_name"], t["default_days"])


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


class WorkflowGraphView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(self.renderHints())
        self.setDragMode(QGraphicsView.RubberBandDrag)
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
        node = None
        if isinstance(item, TaskNodeItem):
            node = item
        elif item is not None and isinstance(item.parentItem(), TaskNodeItem):
            node = item.parentItem()
        if node is not None:
            self._edit_node(node)
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
            for item in list(self.scene().selectedItems()):
                if isinstance(item, TaskNodeItem):
                    self.scene().delete_node(item)
                elif isinstance(item, EdgeItem):
                    self.scene().delete_edge(item)
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

    def contextMenuEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        item = self.scene().itemAt(scene_pos, self.transform()) if self.scene() else None
        node = None
        if isinstance(item, TaskNodeItem):
            node = item
        elif item is not None and isinstance(item.parentItem(), TaskNodeItem):
            node = item.parentItem()

        menu = QMenu(self)
        if node is not None:
            edit_action = menu.addAction("編集...")
            delete_action = menu.addAction("削除")
            chosen = menu.exec(event.globalPos())
            if chosen == edit_action:
                self._edit_node(node)
            elif chosen == delete_action:
                self.scene().delete_node(node)
        else:
            add_action = menu.addAction("タスクを追加...")
            chosen = menu.exec(event.globalPos())
            if chosen == add_action:
                self._add_task_at(scene_pos)

    def _add_task_at(self, scene_pos):
        db = self.scene().db
        if not db.list_teams():
            QMessageBox.information(
                self, "チーム未登録",
                "先にチームを1つ以上登録してください（このダイアログからも追加できます）。",
            )
        dialog = TaskNodeEditDialog(db, "タスクを追加", workflow_id=self.scene().workflow_id)
        if dialog.exec() != QDialog.Accepted:
            return
        name, team_id, days = dialog.values()
        if not name or team_id is None or team_id == _ADD_TEAM_SENTINEL:
            QMessageBox.warning(self, "入力エラー", "タスク名とチームを指定してください。")
            return
        node = self.scene().add_task(name, team_id, days, scene_pos.x(), scene_pos.y())
        self._apply_predecessors(node, dialog.selected_predecessor_ids())

    def _edit_node(self, node):
        db = self.scene().db
        current = next(t for t in db.list_workflow_tasks(self.scene().workflow_id)
                        if t["id"] == node.workflow_task_id)
        dialog = TaskNodeEditDialog(
            db, "タスクを編集", name=current["name"], team_id=current["team_id"],
            days=current["default_days"], workflow_id=self.scene().workflow_id,
            task_id=node.workflow_task_id,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        name, team_id, days = dialog.values()
        if not name or team_id is None or team_id == _ADD_TEAM_SENTINEL:
            QMessageBox.warning(self, "入力エラー", "タスク名とチームを指定してください。")
            return
        try:
            db.update_workflow_task(node.workflow_task_id, name, team_id, days)
        except DuplicateNameError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            return
        colors = team_color_map(db.list_teams())
        team = next(t for t in db.list_teams() if t["id"] == team_id)
        node.update_labels(name, team["name"], days)
        node.set_color(colors.get(team_id, "#898781"))
        self._apply_predecessors(node, dialog.selected_predecessor_ids())
        self.scene().auto_arrange()

    def _apply_predecessors(self, node, predecessor_task_ids):
        """タスク編集ダイアログで選択された先行タスク集合を、実際の
        task_dependencies行に反映する（追加分・削除分の差分のみ処理）。
        循環依存になる追加は scene.try_add_edge が警告して拒否する。"""
        scene = self.scene()
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
