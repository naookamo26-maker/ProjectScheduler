"""
タブ2「ワークフロー設計」。

左側にワークフロー一覧（追加・名前変更・削除）、右側に選択中のワークフローを
「ノードビュー」（gui/node_canvas.py のキャンバス）と「テーブルビュー」
（タスク表＋依存テンプレート表）で切り替えて編集できる画面を表示する。
どちらのビューも同じ WorkflowGraphScene を経由してDBを変更するため、
一方で編集した内容はもう一方にも即座に反映される（on_changed フック）。
"""

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.db import DuplicateNameError, ReferencedEntityError
from gui.node_canvas import (
    WorkflowGraphScene,
    WorkflowGraphView,
    add_task_via_dialog,
    add_template_via_dialog,
    compute_task_depths,
    edit_task_via_dialog,
    edit_template_via_dialog,
)
from gui.widgets_common import (
    CrudSection,
    NoWheelListWidget,
    auto_size_columns,
    capture_table_state,
    confirm_or_block_delete,
    keep_selection_visible,
    restore_table_state,
    row_id,
    set_row_id,
)

_HELP_TEXT = (
    "左のリストからワークフローを選択するか、「＋追加」で新規作成してください。\n\n"
    "ノードビューでキャンバスを右クリックすると、その位置にタスクや\n"
    "依存テンプレートを追加できます。タスク右端の丸（出力）から別タスク\n"
    "左端の丸（入力）へドラッグすると、依存関係（先に完了すべきタスク →\n"
    "後続タスク）を作成できます。循環する依存関係は自動的に拒否されます。\n\n"
    "タブ右上の「テーブルビュー」に切り替えると、同じ内容を一覧性の高い\n"
    "表形式で編集できます。"
)


class WorkflowsTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_scene = None
        self.current_workflow_id = None

        layout = QHBoxLayout(self)

        left = QWidget()
        left.setMaximumWidth(240)
        left_layout = QVBoxLayout(left)

        toolbar = QHBoxLayout()
        add_btn = QPushButton("＋追加")
        add_btn.clicked.connect(self._add_workflow)
        rename_btn = QPushButton("名前変更")
        rename_btn.clicked.connect(self._rename_workflow)
        delete_btn = QPushButton("－削除")
        delete_btn.clicked.connect(self._delete_workflow)
        toolbar.addWidget(add_btn)
        toolbar.addWidget(rename_btn)
        toolbar.addWidget(delete_btn)
        left_layout.addLayout(toolbar)

        self.workflow_list = NoWheelListWidget()
        self.workflow_list.currentItemChanged.connect(self._on_selection_changed)
        # ドラッグで上下の表示順を入れ替えられるようにする（並び順はDBの
        # workflows.sort_orderに保存し、次回開いた時も同じ順序で表示する）。
        self.workflow_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.workflow_list.model().rowsMoved.connect(self._on_workflow_reordered)
        keep_selection_visible(self.workflow_list)
        left_layout.addWidget(self.workflow_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.view = WorkflowGraphView()

        # ノードビュー／テーブルビューの切り替え（既定はノードビュー）。
        # ワークフローを切り替えても選んでいたビューはそのまま維持される
        # （QTabWidget自体は作り直さず、中身だけ差し替えるため）。
        self.view_tabs = QTabWidget()
        self.view_tabs.addTab(self.view, "ノードビュー")

        table_view = QWidget()
        table_view_layout = QVBoxLayout(table_view)
        table_view_layout.setContentsMargins(0, 0, 0, 0)

        self.task_section = CrudSection(
            "タスク一覧（上流→下流の順に表示）",
            ["タスク名", "担当チーム", "所要日数", "先行タスク"],
            on_add=self._add_task_row, on_delete=self._delete_task_row,
            on_edit=self._edit_task_row,
        )
        table_view_layout.addWidget(self.task_section, 2)

        self.template_section = CrudSection(
            "依存テンプレート（このワークフローが他のワークフローに依存する場合の既定タスク対応）",
            ["このワークフローのタスク", "依存先ワークフロー", "依存先タスク"],
            on_add=self._add_template, on_delete=self._delete_template,
            on_edit=self._edit_template,
        )
        table_view_layout.addWidget(self.template_section, 1)

        self.view_tabs.addTab(table_view, "テーブルビュー")

        self.empty_label = QLabel(_HELP_TEXT)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignCenter)

        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self.empty_label)
        self.right_stack.addWidget(self.view_tabs)
        right_layout.addWidget(self.right_stack, 1)

        layout.addWidget(left)
        layout.addWidget(right, 1)

        self.refresh_workflows()

    def refresh_workflows(self, select_id=None):
        self.workflow_list.blockSignals(True)
        self.workflow_list.clear()
        selected_item = None
        for wf in self.db.list_workflows():
            item = QListWidgetItem(wf["name"])
            item.setData(Qt.UserRole, wf["id"])
            self.workflow_list.addItem(item)
            if select_id == wf["id"]:
                selected_item = item
        self.workflow_list.blockSignals(False)

        if selected_item is not None:
            self.workflow_list.setCurrentItem(selected_item)
        elif self.workflow_list.count() > 0:
            self.workflow_list.setCurrentRow(0)
        else:
            self.workflow_list.setCurrentRow(-1)
        self._on_selection_changed(self.workflow_list.currentItem(), None)

    def _on_workflow_reordered(self, *_args):
        """ワークフロー一覧をドラッグで並び替えた直後（QListWidgetModelの
        rowsMoved）に呼ばれ、新しい表示順をDBに保存する。"""
        ordered_ids = [
            self.workflow_list.item(i).data(Qt.UserRole)
            for i in range(self.workflow_list.count())
        ]
        self.db.reorder_workflows(ordered_ids)

    def _on_selection_changed(self, current, _previous):
        if current is None:
            self.right_stack.setCurrentWidget(self.empty_label)
            self.current_scene = None
            self.current_workflow_id = None
            self.task_section.setEnabled(False)
            self.task_section.table.setRowCount(0)
            self.template_section.setEnabled(False)
            self.template_section.table.setRowCount(0)
            return
        workflow_id = current.data(Qt.UserRole)
        self.current_workflow_id = workflow_id
        self.current_scene = WorkflowGraphScene(self.db, workflow_id, self, on_changed=self._on_scene_changed)
        self.view.setScene(self.current_scene)
        self.right_stack.setCurrentWidget(self.view_tabs)
        # シーン切替直後はビューポートサイズが未確定な場合があるため、
        # レイアウト確定後（次のイベントループ）にフィットさせる。
        QTimer.singleShot(0, self.view.fit_all)
        self.task_section.setEnabled(True)
        self.template_section.setEnabled(True)
        self.refresh_task_table()
        self.refresh_templates()

    def _on_scene_changed(self):
        """WorkflowGraphSceneがタスク・依存関係・依存テンプレートのいずれかを
        変更した際に呼ばれる（ノードビュー・テーブルビューどちらの操作からでも
        経由するため、これだけで両方のテーブルが常に最新化される）。"""
        self.refresh_task_table()
        self.refresh_templates()

    # -- タスク一覧（テーブルビュー） -------------------------------------------------

    def refresh_task_table(self):
        table = self.task_section.table
        table.setRowCount(0)
        if self.current_workflow_id is None:
            return
        tasks = self.db.list_workflow_tasks(self.current_workflow_id)
        deps = self.db.list_task_dependencies(self.current_workflow_id)
        depth = compute_task_depths(tasks, deps)
        name_by_id = {t["id"]: t["name"] for t in tasks}
        preds_by_task = {}
        for d in deps:
            preds_by_task.setdefault(d["successor_task_id"], []).append(d["predecessor_task_id"])

        # ノードビューの自動整列（依存の深さ→同じ深さ内は名前順）と同じ順序で、
        # 上流→下流を上→下に並べる。
        ordered = sorted(tasks, key=lambda t: (depth[t["id"]], t["name"]))
        for t in ordered:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(t["name"]))
            table.setItem(row, 1, QTableWidgetItem(t["team_name"]))
            table.setItem(row, 2, QTableWidgetItem(f'{t["default_days"]}日'))
            pred_names = "、".join(
                sorted(name_by_id[p] for p in preds_by_task.get(t["id"], []) if p in name_by_id)
            )
            table.setItem(row, 3, QTableWidgetItem(pred_names))
            for col in range(4):
                table.item(row, col).setFlags(table.item(row, col).flags() & ~Qt.ItemIsEditable)
            set_row_id(table, row, t["id"])
        auto_size_columns(table)

    def _add_task_row(self):
        if self.current_scene is None:
            return
        add_task_via_dialog(self.current_scene, self)

    def _edit_task_row(self, row):
        if self.current_scene is None:
            return
        task_id = row_id(self.task_section.table, row)
        node = self.current_scene.nodes.get(task_id)
        if node is None:
            return
        edit_task_via_dialog(self.current_scene, self, node)

    def _delete_task_row(self, row):
        if self.current_scene is None:
            return
        task_id = row_id(self.task_section.table, row)
        node = self.current_scene.nodes.get(task_id)
        if node is None:
            return
        self.current_scene.delete_node(node)

    # -- 依存テンプレート ----------------------------------------------------------

    def refresh_templates(self):
        table = self.template_section.table
        table.setRowCount(0)
        if self.current_workflow_id is None:
            return
        for tpl in self.db.list_dependency_templates(self.current_workflow_id):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(tpl["task_name"]))
            table.setItem(row, 1, QTableWidgetItem(tpl["depends_on_workflow_name"]))
            table.setItem(row, 2, QTableWidgetItem(tpl["depends_on_task_name"]))
            for col in range(3):
                table.item(row, col).setFlags(table.item(row, col).flags() & ~Qt.ItemIsEditable)
            set_row_id(table, row, tpl["id"])
        auto_size_columns(table)

    def _add_template(self):
        if self.current_scene is None:
            return
        add_template_via_dialog(self.current_scene, self)

    def _edit_template(self, row):
        if self.current_scene is None:
            return
        template_id = row_id(self.template_section.table, row)
        edit_template_via_dialog(self.current_scene, self, template_id)

    def _delete_template(self, row):
        if self.current_scene is None:
            return
        template_id = row_id(self.template_section.table, row)
        self.current_scene.delete_dependency_template_node(template_id)

    def _add_workflow(self):
        name, ok = QInputDialog.getText(self, "ワークフローを追加", "ワークフロー名:")
        if not ok or not name.strip():
            return
        try:
            new_id = self.db.add_workflow(name.strip())
        except DuplicateNameError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return
        self.refresh_workflows(select_id=new_id)

    def _rename_workflow(self):
        item = self.workflow_list.currentItem()
        if item is None:
            return
        wf_id = item.data(Qt.UserRole)
        name, ok = QInputDialog.getText(self, "ワークフロー名を変更", "ワークフロー名:", text=item.text())
        if not ok or not name.strip():
            return
        try:
            self.db.rename_workflow(wf_id, name.strip())
        except DuplicateNameError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            return
        self.refresh_workflows(select_id=wf_id)

    def _delete_workflow(self):
        item = self.workflow_list.currentItem()
        if item is None:
            return
        wf_id = item.data(Qt.UserRole)
        count = self.db.workflow_usage_count(wf_id)
        if not confirm_or_block_delete(self, count, "このワークフロー", hard_block=True):
            return
        try:
            self.db.delete_workflow(wf_id)
        except ReferencedEntityError as e:
            QMessageBox.warning(self, "削除できません", str(e))
            return
        self.refresh_workflows()

    def refresh_team_choices(self):
        """チームマスタ変更時、表示中キャンバス・タスク表の色/表示を再計算する。"""
        if self.current_scene is not None:
            self.current_scene.refresh_colors()

    def refresh_choices(self):
        """MainWindowのタブ切り替え時フックから呼ばれる共通インターフェース。
        選択中のワークフローを維持したまま一覧・キャンバスを最新化する。

        キャンバス上のノード・依存関係・依存テンプレート疑似ノードの選択
        （フォーカスではなく、視覚的な選択状態）は、_on_selection_changed が
        ワークフロー選択のたびに WorkflowGraphScene を作り直すため、何も
        しないと消えてしまう。単に別のタブへ移って戻ってきただけでも選択が
        消えるのは不便なので、Undo/Redoでの選択復元（capture_ui_state/
        restore_ui_state）と同じヘルパーを使い、作り直し前後で維持する。"""
        current = self.workflow_list.currentItem()
        select_id = current.data(Qt.UserRole) if current else None
        canvas_selection = self._capture_canvas_selection()
        self.refresh_workflows(select_id=select_id)
        self._restore_canvas_selection(canvas_selection)

    # -- キャンバスの選択（ノード・依存関係・依存テンプレート疑似ノード） -----------------
    #
    # ワークフロー選択のたびにWorkflowGraphSceneを作り直すため、選択状態は
    # 明示的に持ち回らないと消える。Undo/Redoでの復元（下記）と、単なる
    # タブの往復（refresh_choices、上記）の両方から使う共通ロジック。

    def _capture_canvas_selection(self):
        if self.current_scene is None:
            return None
        return {
            "selected_task_ids": [
                task_id for task_id, node in self.current_scene.nodes.items()
                if node.isSelected()
            ],
            "selected_dep_ids": [
                dep_id for dep_id, edge in self.current_scene.edges.items()
                if edge.isSelected()
            ],
            "selected_template_ids": [
                template_id for template_id, node in self.current_scene.template_nodes.items()
                if node.isSelected()
            ],
        }

    def _restore_canvas_selection(self, canvas_state):
        if self.current_scene is None or not canvas_state:
            return
        for task_id in canvas_state.get("selected_task_ids", []):
            node = self.current_scene.nodes.get(task_id)
            if node is not None:
                node.setSelected(True)
        for dep_id in canvas_state.get("selected_dep_ids", []):
            edge = self.current_scene.edges.get(dep_id)
            if edge is not None:
                edge.setSelected(True)
        for template_id in canvas_state.get("selected_template_ids", []):
            node = self.current_scene.template_nodes.get(template_id)
            if node is not None:
                node.setSelected(True)

    # -- Undo/Redo用の選択状態 -------------------------------------------------------

    def capture_ui_state(self):
        current_item = self.workflow_list.currentItem()
        return {
            "workflow_id": current_item.data(Qt.UserRole) if current_item else None,
            "template": capture_table_state(self.template_section.table),
            "canvas": self._capture_canvas_selection(),
        }

    def restore_ui_state(self, state):
        if not state:
            return
        self.refresh_workflows(select_id=state.get("workflow_id"))
        self._restore_canvas_selection(state.get("canvas"))
        restore_table_state(self.template_section.table, state.get("template"))
