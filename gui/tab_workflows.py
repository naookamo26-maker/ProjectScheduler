"""
タブ2「ワークフロー設計」。

左側にワークフロー一覧（追加・名前変更・削除）、右側に選択中のワークフローの
ノードグラフキャンバス（gui/node_canvas.py）を表示する。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui.db import DuplicateNameError, ReferencedEntityError
from gui.node_canvas import WorkflowGraphScene, WorkflowGraphView
from gui.widgets_common import confirm_or_block_delete

_HELP_TEXT = (
    "左のリストからワークフローを選択するか、「＋追加」で新規作成してください。\n\n"
    "キャンバス上で右クリックすると、その位置にタスクを追加できます。\n"
    "タスク右端の丸（出力）から別タスク左端の丸（入力）へドラッグすると、\n"
    "依存関係（先に完了すべきタスク → 後続タスク）を作成できます。\n"
    "循環する依存関係は自動的に拒否されます。"
)


class WorkflowsTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_scene = None

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

        self.workflow_list = QListWidget()
        self.workflow_list.currentItemChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self.workflow_list)

        self.view = WorkflowGraphView()

        self.empty_label = QLabel(_HELP_TEXT)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignCenter)

        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self.empty_label)
        self.right_stack.addWidget(self.view)

        layout.addWidget(left)
        layout.addWidget(self.right_stack, 1)

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

    def _on_selection_changed(self, current, _previous):
        if current is None:
            self.right_stack.setCurrentWidget(self.empty_label)
            self.current_scene = None
            return
        workflow_id = current.data(Qt.UserRole)
        self.current_scene = WorkflowGraphScene(self.db, workflow_id, self)
        self.view.setScene(self.current_scene)
        self.right_stack.setCurrentWidget(self.view)

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
        """チームマスタ変更時、表示中キャンバスの色・ラベルを再計算する。"""
        if self.current_scene is not None:
            self.current_scene.refresh_colors()
