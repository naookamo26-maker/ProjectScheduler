"""
タブ3「ジョブ作成」。

左右2分割（QSplitter、既定50/50でユーザーがドラッグ調整可）。
- 左: ジョブ一覧（名前・ワークフロー・既定マイルストーン・優先度）を縦全体に表示。
  「既定マイルストーン」列はStretchで残り幅を吸収し、パネル幅にかかわらず
  横スクロールなしで4列すべてが収まるようにしている。
- 右: 上下2分割（QSplitter）で、選択中ジョブのタスク上書き表と依存先ジョブを
  縦に並べる。
  - タスク上書き表: 選択ジョブが使うワークフローのタスク一覧をそのまま
    自動的に表示し（手入力不要）、既定から外れる項目（無効化・日数上書き・
    マイルストーン上書き・チーム上書き）だけを編集する。既定日数・既定
    チームは、上書き用のスピンボックス/コンボボックスの特殊表示
    （「既定（n日）」「（既定: 値）」）に統合し、専用の列は持たない。
    既定値のままの行はDBに保存しない（差分のみ保持、gui/db.py参照）。
  - 依存先ジョブ: ツリー表示で1セクションに統合。トップレベルが依存先
    ジョブ、その子がタスク単位の対応（依存先の先行タスク→本ジョブの
    後続タスク）。別ウィンドウにすると一覧性が悪いため、ツリーをその場で
    展開したまま編集できるようにしている。ワークフロー設計タブの依存
    テンプレートに対応があれば自動的にタスク対応が展開される
    （gui/db.py の sync_dependency_templates 参照。既に手動で同じ対応が
    追加済みの場合は重複させず自動生成扱いに変換する）。テンプレートの
    追加・編集・削除、依存先ジョブの追加、ジョブのワークフロー再割当ての
    いずれからもこの同期が呼ばれるほか、このタブがアクティブになった際にも
    念のため呼び直す（refresh_choices参照）。
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.db import DuplicateNameError, ProjectDatabaseError
from gui.widgets_common import (
    CrudSection,
    DefaultAwareSpinBox,
    NoWheelComboBox,
    NoWheelListWidget,
    NoWheelSpinBox,
    auto_size_columns,
    confirm_or_block_delete,
    keep_selection_visible,
    make_fk_combo,
    row_id,
    set_row_id,
    unique_default_name,
)


def _readonly_item(text):
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


class JobDependencyLinkDialog(QDialog):
    """「依存先ジョブ」を選ぶダイアログ。ワークフローで絞り込んでから、
    候補ジョブを複数選択してまとめて追加できる。タスク単位の対応はワークフロー
    設計タブの依存テンプレートから自動展開されるため、ここではジョブを選ぶだけでよい。"""

    def __init__(self, db, job_id, parent=None):
        super().__init__(parent)
        self.db = db
        self.job_id = job_id
        self.setWindowTitle("依存先ジョブを追加")
        self.resize(360, 420)

        self._existing = {link["depends_on_job_id"] for link in db.list_job_dependency_links(job_id)}
        self._candidates = [j for j in db.list_jobs() if j["id"] != job_id and j["id"] not in self._existing]

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.workflow_filter_combo = NoWheelComboBox()
        self.workflow_filter_combo.addItem("（すべてのワークフロー）", None)
        for wf in db.list_workflows():
            self.workflow_filter_combo.addItem(wf["name"], wf["id"])
        self.workflow_filter_combo.currentIndexChanged.connect(self._reload_job_list)
        form.addRow("ワークフローで絞り込み", self.workflow_filter_combo)
        layout.addLayout(form)

        layout.addWidget(QLabel("依存先ジョブ"))
        self.job_list = NoWheelListWidget()
        self.job_list.setSelectionMode(NoWheelListWidget.ExtendedSelection)
        layout.addWidget(self.job_list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._reload_job_list()

    def _reload_job_list(self):
        workflow_id = self.workflow_filter_combo.currentData()
        self.job_list.clear()
        for j in self._candidates:
            if workflow_id is not None and j["workflow_id"] != workflow_id:
                continue
            item = QListWidgetItem(f'{j["name"]}（{j["workflow_name"]}）')
            item.setData(Qt.UserRole, j["id"])
            self.job_list.addItem(item)

    def values(self):
        return [item.data(Qt.UserRole) for item in self.job_list.selectedItems()]


class TaskPairDialog(QDialog):
    """依存先ジョブとの、タスク単位の対応（依存先の先行タスク→本ジョブの
    後続タスク）を1件選ぶダイアログ。依存先ジョブ自体は呼び出し側（依存先
    ジョブ一覧で選択済みのリンク）で固定されているため、ここでは2つの
    タスクを選ぶだけでよい。"""

    def __init__(self, db, job, target_job, parent=None, initial=None):
        super().__init__(parent)
        self.setWindowTitle("タスク対応を編集" if initial else "タスク対応を追加")
        form = QFormLayout(self)

        self.dep_task_combo = NoWheelComboBox()
        for t in db.list_workflow_tasks(target_job["workflow_id"]):
            self.dep_task_combo.addItem(t["name"], t["id"])
        form.addRow(f"依存先の先行タスク（{target_job['name']}）", self.dep_task_combo)

        self.task_combo = NoWheelComboBox()
        for t in db.list_workflow_tasks(job["workflow_id"]):
            self.task_combo.addItem(t["name"], t["id"])
        form.addRow(f"本ジョブの後続タスク（{job['name']}）", self.task_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        if initial is not None:
            dep_task_id, task_id = initial
            idx = self.dep_task_combo.findData(dep_task_id)
            if idx >= 0:
                self.dep_task_combo.setCurrentIndex(idx)
            idx = self.task_combo.findData(task_id)
            if idx >= 0:
                self.task_combo.setCurrentIndex(idx)

    def values(self):
        """(このジョブのタスクID, 依存先タスクID) の順で返す
        （db.add_external_dependency の引数順に合わせる）。"""
        return self.task_combo.currentData(), self.dep_task_combo.currentData()


_JOB_SORT_KEYS = {
    0: lambda j: j["name"],
    1: lambda j: j["workflow_name"],
    2: lambda j: (j["milestone_name"] is None, j["milestone_name"] or ""),
    3: lambda j: j["priority"],
}


class JobsTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_job_id = None
        self._workflow_checks = {}  # workflow_id -> QCheckBox（ジョブ一覧の絞り込み用）
        self._sort_column = 0
        self._sort_ascending = True

        layout = QVBoxLayout(self)

        filter_group = QGroupBox("ワークフローで絞り込み")
        self.filter_layout = QHBoxLayout(filter_group)
        select_all_btn = QPushButton("すべて表示")
        select_all_btn.clicked.connect(lambda: self._set_all_filters(True))
        select_none_btn = QPushButton("すべて解除")
        select_none_btn.clicked.connect(lambda: self._set_all_filters(False))
        self.filter_layout.addWidget(select_all_btn)
        self.filter_layout.addWidget(select_none_btn)
        self.filter_layout.addSpacing(16)
        self.filter_checks_layout = QHBoxLayout()
        self.filter_layout.addLayout(self.filter_checks_layout)
        self.filter_layout.addStretch(1)
        layout.addWidget(filter_group)

        self.jobs_section = CrudSection(
            "ジョブ", ["ジョブ名", "ワークフロー", "既定マイルストーン", "優先度"],
            on_add=self._add_job, on_delete=self._delete_job,
        )
        self.jobs_section.table.itemChanged.connect(self._on_job_name_changed)
        self.jobs_section.table.currentCellChanged.connect(self._on_job_selection_changed)
        # 列見出しをクリックするとその列で並び替えられる（同じ列を再クリックで昇順/降順切替）。
        jobs_header = self.jobs_section.table.horizontalHeader()
        jobs_header.setSectionsClickable(True)
        jobs_header.setSortIndicatorShown(True)
        jobs_header.setSortIndicator(self._sort_column, Qt.AscendingOrder)
        jobs_header.sectionClicked.connect(self._on_job_header_clicked)
        # 「既定マイルストーン」列（内容の長さが最も変動する）に残り幅を吸収させ、
        # パネル幅にかかわらず横スクロールなしで4列すべてが収まるようにする。
        jobs_header.setSectionResizeMode(2, QHeaderView.Stretch)

        override_group = QGroupBox("タスク上書き（選択中のジョブ）")
        override_layout = QVBoxLayout(override_group)
        self.override_table = QTableWidget(0, 5)
        self.override_table.setHorizontalHeaderLabels(
            ["タスク名", "有効", "日数", "マイルストーン", "チーム"]
        )
        self.override_table.verticalHeader().setVisible(False)
        self.override_table.setSelectionMode(QTableWidget.NoSelection)
        override_layout.addWidget(self.override_table)

        dep_group = QGroupBox("依存先ジョブ（展開してタスク単位の対応を確認・編集）")
        dep_group_layout = QVBoxLayout(dep_group)

        dep_toolbar = QHBoxLayout()
        add_link_btn = QPushButton("＋ 依存先ジョブ")
        add_link_btn.clicked.connect(self._add_dependency_link)
        del_link_btn = QPushButton("－ 依存先ジョブ")
        del_link_btn.clicked.connect(self._delete_selected_dependency_link)
        dep_toolbar.addWidget(add_link_btn)
        dep_toolbar.addWidget(del_link_btn)
        dep_toolbar.addSpacing(16)
        add_pair_btn = QPushButton("＋ タスク対応")
        add_pair_btn.clicked.connect(self._add_selected_task_pair)
        del_pair_btn = QPushButton("－ タスク対応")
        del_pair_btn.clicked.connect(self._delete_selected_task_pair)
        dep_toolbar.addWidget(add_pair_btn)
        dep_toolbar.addWidget(del_pair_btn)
        dep_toolbar.addStretch(1)
        dep_group_layout.addLayout(dep_toolbar)

        self.dep_tree = QTreeWidget()
        self.dep_tree.setColumnCount(3)
        self.dep_tree.setHeaderLabels(["依存先ジョブ ／ タスク対応（先行→後続）", "有効", "種別"])
        self.dep_tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.dep_tree.itemDoubleClicked.connect(self._on_dep_tree_double_clicked)
        keep_selection_visible(self.dep_tree)
        dep_group_layout.addWidget(self.dep_tree)

        # 右側: タスク上書きと依存先ジョブを縦に並べる（ユーザーがドラッグで
        # 配分を調整できるようQSplitterを使う）。
        right_splitter = QSplitter(Qt.Vertical)
        right_splitter.addWidget(override_group)
        right_splitter.addWidget(dep_group)
        right_splitter.setStretchFactor(0, 1)
        right_splitter.setStretchFactor(1, 1)

        # 左: ジョブ一覧を縦全体に、右: 上記2つを縦に並べたもの。幅は指定通り50/50。
        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.addWidget(self.jobs_section)
        self.main_splitter.addWidget(right_splitter)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 1)
        layout.addWidget(self.main_splitter, 1)
        # コンストラクタ時点（実際のウィジェット幅が確定する前）にsetSizes()を
        # 呼んでも比率が反映されない（表示後の最初のレイアウトで上書きされる）
        # ため、レイアウト確定後（次のイベントループ）に改めて設定し直す。
        QTimer.singleShot(0, self._apply_initial_splitter_sizes)

        self.refresh_jobs()

    def _apply_initial_splitter_sizes(self):
        total = self.main_splitter.width()
        if total > 0:
            self.main_splitter.setSizes([total // 2, total - total // 2])

    # -- ワークフロー絞り込み ------------------------------------------------------

    def _rebuild_workflow_filter(self):
        """ワークフロー一覧に合わせて絞り込み用チェックボックスを再構築する。
        既存のチェック状態はワークフロー名で可能な限り維持し、新規ワークフローは
        既定で表示（チェック済み）にする。"""
        previous_checked = {
            wf_id for wf_id, cb in self._workflow_checks.items() if cb.isChecked()
        }
        previous_unchecked = {
            wf_id for wf_id, cb in self._workflow_checks.items() if not cb.isChecked()
        }
        while self.filter_checks_layout.count():
            item = self.filter_checks_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._workflow_checks = {}
        for wf in self.db.list_workflows():
            checked = wf["id"] not in previous_unchecked or wf["id"] in previous_checked
            checkbox = QCheckBox(wf["name"])
            checkbox.setChecked(checked)  # connect前に設定し、構築時のstateChangedを発火させない
            checkbox.stateChanged.connect(lambda _state: self.refresh_jobs(select_id=self.current_job_id))
            self.filter_checks_layout.addWidget(checkbox)
            self._workflow_checks[wf["id"]] = checkbox

    def _set_all_filters(self, checked):
        # 一括変更中に途中でrefresh_jobs（＝チェックボックス再構築）が走ると
        # ループ中のウィジェットが差し替わってしまうため、シグナルを止めてから
        # 最後にまとめて一度だけ反映する。
        for checkbox in self._workflow_checks.values():
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)
        self.refresh_jobs(select_id=self.current_job_id)

    def _visible_workflow_ids(self):
        return {wf_id for wf_id, cb in self._workflow_checks.items() if cb.isChecked()}

    # -- ジョブ一覧 --------------------------------------------------------------

    def _on_job_header_clicked(self, column):
        if column not in _JOB_SORT_KEYS:
            return
        if column == self._sort_column:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = column
            self._sort_ascending = True
        self.jobs_section.table.horizontalHeader().setSortIndicator(
            self._sort_column, Qt.AscendingOrder if self._sort_ascending else Qt.DescendingOrder
        )
        self.refresh_jobs(select_id=self.current_job_id)

    def refresh_jobs(self, select_id=None):
        self._rebuild_workflow_filter()
        visible_workflow_ids = self._visible_workflow_ids()

        table = self.jobs_section.table
        table.blockSignals(True)
        table.setRowCount(0)
        workflow_options = [(w["id"], w["name"]) for w in self.db.list_workflows()]
        milestone_options = [(m["id"], m["name"]) for m in self.db.list_milestones()]
        select_row = -1
        sort_key = _JOB_SORT_KEYS[self._sort_column]
        jobs = sorted(self.db.list_jobs(), key=sort_key, reverse=not self._sort_ascending)
        for job in jobs:
            if job["workflow_id"] not in visible_workflow_ids:
                continue
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(job["name"]))
            set_row_id(table, row, job["id"])
            if job["id"] == select_id:
                select_row = row

            wf_combo = make_fk_combo(workflow_options, job["workflow_id"])
            wf_combo.currentIndexChanged.connect(
                lambda _idx, jid=job["id"]: self._on_job_field_changed(jid)
            )
            table.setCellWidget(row, 1, wf_combo)

            ms_combo = make_fk_combo(milestone_options, job["default_milestone_id"], allow_blank=True)
            ms_combo.currentIndexChanged.connect(
                lambda _idx, jid=job["id"]: self._on_job_field_changed(jid)
            )
            table.setCellWidget(row, 2, ms_combo)

            priority_spin = NoWheelSpinBox()
            priority_spin.setRange(1, 999)
            priority_spin.setValue(job["priority"])
            priority_spin.valueChanged.connect(
                lambda _val, jid=job["id"]: self._on_job_field_changed(jid)
            )
            table.setCellWidget(row, 3, priority_spin)
        table.blockSignals(False)
        auto_size_columns(table)

        if select_row >= 0:
            table.setCurrentCell(select_row, 0)
        else:
            self._on_job_selection_changed(table.currentRow(), 0, -1, -1)

    def _add_job(self):
        if not self.db.list_workflows():
            QMessageBox.information(
                self, "ワークフロー未登録",
                "先に「ワークフロー設計」タブでワークフローを1つ以上作成してください。",
            )
            return
        existing = {j["name"] for j in self.db.list_jobs()}
        name = unique_default_name(existing, "新しいジョブ")
        workflow_id = self.db.list_workflows()[0]["id"]
        try:
            new_id = self.db.add_job(name, workflow_id, None, 100)
        except DuplicateNameError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return
        self.refresh_jobs(select_id=new_id)

    def _delete_job(self, row):
        job_id = row_id(self.jobs_section.table, row)
        count = self.db.job_usage_count(job_id)
        if not confirm_or_block_delete(self, count, "このジョブ", hard_block=False):
            return
        self.db.delete_job(job_id)
        self.refresh_jobs()

    def _on_job_name_changed(self, item):
        if item.column() != 0:
            return
        job_id = row_id(self.jobs_section.table, item.row())
        if job_id is None:
            return
        self._write_job(job_id, name_override=item.text())

    def _on_job_field_changed(self, job_id):
        self._write_job(job_id)

    def _write_job(self, job_id, name_override=None):
        table = self.jobs_section.table
        for row in range(table.rowCount()):
            if row_id(table, row) == job_id:
                name = name_override if name_override is not None else table.item(row, 0).text()
                workflow_id = table.cellWidget(row, 1).currentData()
                milestone_id = table.cellWidget(row, 2).currentData()
                priority = table.cellWidget(row, 3).value()
                try:
                    self.db.update_job(job_id, name, workflow_id, milestone_id, priority)
                except DuplicateNameError as e:
                    QMessageBox.warning(self, "変更できません", str(e))
                    self.refresh_jobs(select_id=job_id)
                    return
                if job_id == self.current_job_id:
                    self._refresh_overrides()
                return

    # -- タスク上書き ------------------------------------------------------------

    def _on_job_selection_changed(self, current_row, _current_col, _prev_row, _prev_col):
        if current_row is not None and current_row >= 0:
            self.current_job_id = row_id(self.jobs_section.table, current_row)
            self._refresh_overrides()
            self._refresh_dependencies()
            return
        # current_row < 0: ジョブ一覧の「現在セル」が本当に無くなった
        # （ジョブが削除された・絞り込みで非表示になった等）のか、タスク
        # 上書き欄のセルウィジェットへフォーカスが移る際のQtの内部挙動による
        # 一時的な現在セル解除なのかを区別する。後者はよく起こり、放置すると
        # 選択中ジョブの情報（current_job_id・タスク上書き欄・依存先ジョブ欄）
        # が意図せず消えてしまう。current_job_id がまだジョブ一覧に存在する
        # なら一時的な解除とみなし、状態は保持したまま該当行を選び直す
        # （setCurrentCellにより本メソッドが再度呼ばれ、上のブロックで
        # 通常通り更新される）。
        if self.current_job_id is not None:
            table = self.jobs_section.table
            for row in range(table.rowCount()):
                if row_id(table, row) == self.current_job_id:
                    table.setCurrentCell(row, max(table.currentColumn(), 0))
                    return
        self.current_job_id = None
        self.override_table.setRowCount(0)
        self.dep_tree.clear()

    def _refresh_overrides(self):
        table = self.override_table
        table.blockSignals(True)
        table.setRowCount(0)
        all_milestones = self.db.list_milestones()
        team_options = [(t["id"], t["name"]) for t in self.db.list_teams()]

        if self.current_job_id is None:
            table.blockSignals(False)
            return
        job = next(j for j in self.db.list_jobs() if j["id"] == self.current_job_id)
        default_ms_label = job["milestone_name"] or "未設定"

        for r in self.db.list_job_tasks_with_overrides(self.current_job_id):
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, _readonly_item(r["task_name"]))
            set_row_id(table, row, r["workflow_task_id"])

            active_checkbox = QCheckBox()
            active_checkbox.setChecked(True if r["is_active"] is None else bool(r["is_active"]))
            active_checkbox.stateChanged.connect(
                lambda _state, tid=r["workflow_task_id"]: self._on_override_changed(tid)
            )
            table.setCellWidget(row, 1, active_checkbox)

            # 既定日数は専用の列を持たず、上書きしていない状態（0＝特殊値）での
            # 表示テキストに埋め込む（「既定日数」列を廃止して列数を圧縮するため）。
            # 0（既定）の状態から▲▼で増減すると、既定値を起点に動く
            # （DefaultAwareSpinBox。0自体は「既定を使用」という意味のまま）。
            days_spin = DefaultAwareSpinBox(r["default_days"])
            days_spin.setRange(0, 9999)
            days_spin.setSuffix("日")
            days_spin.setSpecialValueText(f"既定（{r['default_days']}日）")
            days_spin.setValue(r["override_days"] or 0)
            days_spin.valueChanged.connect(
                lambda _val, tid=r["workflow_task_id"]: self._on_override_changed(tid)
            )
            table.setCellWidget(row, 2, days_spin)

            # 先行タスク（同一ワークフロー内）の実効マイルストーンより早い締切の
            # マイルストーンは、後継タスクが前倒しになってしまうため選択肢から外す。
            min_end_date = self.db.minimum_milestone_end_date(
                self.current_job_id, r["workflow_task_id"]
            )
            milestone_options = [
                (m["id"], m["name"]) for m in all_milestones
                if min_end_date is None or m["end_date"] >= min_end_date
            ]
            ms_combo = make_fk_combo(
                milestone_options, r["override_milestone_id"], allow_blank=True,
                blank_label=f"（既定: {default_ms_label}）",
            )
            ms_combo.currentIndexChanged.connect(
                lambda _idx, tid=r["workflow_task_id"]: self._on_override_changed(tid)
            )
            table.setCellWidget(row, 3, ms_combo)

            # 既定チームも同様に、専用列ではなくコンボの未選択時ラベルに埋め込む。
            team_combo = make_fk_combo(
                team_options, r["override_team_id"], allow_blank=True,
                blank_label=f"（既定: {r['default_team_name']}）",
            )
            team_combo.currentIndexChanged.connect(
                lambda _idx, tid=r["workflow_task_id"]: self._on_override_changed(tid)
            )
            table.setCellWidget(row, 4, team_combo)
        table.blockSignals(False)
        auto_size_columns(table, min_width=50)
        table.setColumnWidth(1, 44)  # 「有効」列はチェックボックスのみなので詰める
        self._ensure_job_selection()

    def _ensure_job_selection(self):
        """タスク上書き欄のセルウィジェット（スピンボックス/コンボ）をクリックして
        すぐ離す操作をした直後、そのウィジェットが破棄・再構築される
        （_refresh_overrides）のに伴うフォーカス遷移の影響で、ジョブ一覧側の
        選択（現在セル）が意図せず解除されてしまうことがある（左ボタンを
        押したまま値を変える分には発生しない＝クリックの解放タイミングに
        起因する）。current_job_id自体は保持されるため実害は無いが、見た目上
        「ジョブの選択が消えた」ように見えて紛らわしいため、対応する行を
        明示的に選び直す。"""
        if self.current_job_id is None:
            return
        table = self.jobs_section.table
        for row in range(table.rowCount()):
            if row_id(table, row) == self.current_job_id and table.currentRow() != row:
                table.setCurrentCell(row, max(table.currentColumn(), 0))
                return

    def _on_override_changed(self, workflow_task_id):
        table = self.override_table
        for row in range(table.rowCount()):
            if row_id(table, row) == workflow_task_id:
                is_active = table.cellWidget(row, 1).isChecked()
                days_val = table.cellWidget(row, 2).value()
                override_days = days_val if days_val > 0 else None
                milestone_id = table.cellWidget(row, 3).currentData()
                team_id = table.cellWidget(row, 4).currentData()

                if not is_active or override_days is not None or milestone_id is not None or team_id is not None:
                    self.db.upsert_job_task_override(
                        self.current_job_id, workflow_task_id, is_active=is_active,
                        override_days=override_days, milestone_id=milestone_id, team_id=team_id,
                    )
                else:
                    self.db.clear_job_task_override(self.current_job_id, workflow_task_id)

                # このタスク自身が、先行タスクの実効マイルストーンより早くなって
                # しまった場合（既定に戻した結果、前倒しの矛盾が生じた場合を含む）、
                # 先行タスクに合わせて自動的に引き上げる。
                raised = self.db.enforce_milestone_floor(self.current_job_id, workflow_task_id)
                # マイルストーンを変更した場合、後継タスク（同一ワークフロー内、
                # transitively）が前のタスクより早いマイルストーンのままだと
                # 前倒しの矛盾が生じるため、必要なら自動的に繰り下げる。
                changed = self.db.cascade_milestone_to_successors(
                    self.current_job_id, workflow_task_id
                )
                if raised or changed:
                    messages = []
                    if raised:
                        messages.append(
                            "このタスクのマイルストーンが先行タスクより早かったため、"
                            "先行タスクに合わせて自動的に引き上げました。"
                        )
                    if changed:
                        messages.append(
                            f"後継タスク{len(changed)}件のマイルストーンが、変更後のマイルストーン"
                            "より早かったため、整合性を保つよう自動的に合わせました。"
                        )
                    QMessageBox.information(self, "マイルストーンを自動調整しました", "\n".join(messages))
                # このタスク自身の上書き入力に加え、後継タスクの選択肢・表示も
                # 変わりうるため、テーブル全体を作り直す（シグナル発火元セルの
                # ウィジェットを直接コールバック内で破棄しないよう次のイベント
                # ループへ遅延させる）。
                QTimer.singleShot(0, self._refresh_overrides)
                return

    # -- 依存ジョブ（ツリー表示） ----------------------------------------------------
    #
    # 別ウィンドウのダイアログだと依存先ジョブとタスク対応を同時に見渡せず
    # 一覧性が悪いため、その場で展開できるツリーで表示・編集する。
    # トップレベル項目 = 依存先ジョブ（job_dependency_links 1件）
    # 子項目           = タスク対応（job_external_dependencies 1件）
    # 各アイテムのUserRoleデータに種別（"link" / "pair"）とIDを保持する。

    def _refresh_dependencies(self):
        tree = self.dep_tree
        tree.blockSignals(True)
        tree.clear()
        if self.current_job_id is not None:
            all_pairs = self.db.list_external_dependencies(job_id=self.current_job_id)
            for link in self.db.list_job_dependency_links(self.current_job_id):
                top = QTreeWidgetItem([link["depends_on_job_name"], "", ""])
                top.setData(0, Qt.UserRole, {
                    "kind": "link", "link_id": link["id"], "target_job_id": link["depends_on_job_id"],
                })
                tree.addTopLevelItem(top)

                pairs = [d for d in all_pairs if d["depends_on_job_id"] == link["depends_on_job_id"]]
                for d in pairs:
                    is_auto = d["source_link_id"] is not None
                    child = QTreeWidgetItem([f'{d["depends_on_task_name"]} → {d["task_name"]}', "", ""])
                    child.setData(0, Qt.UserRole, {"kind": "pair", "dep_id": d["id"], "auto": is_auto})
                    top.addChild(child)

                    active_checkbox = QCheckBox()
                    active_checkbox.setChecked(bool(d["is_active"]))
                    active_checkbox.stateChanged.connect(
                        lambda _state, cb=active_checkbox, dep_id=d["id"]:
                        self.db.set_external_dependency_active(dep_id, cb.isChecked())
                    )
                    tree.setItemWidget(child, 1, active_checkbox)
                    tree.setItemWidget(child, 2, QLabel("自動" if is_auto else "手動"))
                # 一覧性を優先し、常に展開した状態で表示する。
                top.setExpanded(True)
        tree.blockSignals(False)
        for col in range(3):
            tree.resizeColumnToContents(col)

    def _selected_link(self):
        """現在の選択項目から、それが属する「依存先ジョブ」トップレベル項目と
        そのUserRoleデータを返す（子のタスク対応が選ばれていれば親を辿る）。
        何も選択されていなければ (None, None)。"""
        item = self.dep_tree.currentItem()
        if item is None:
            return None, None
        data = item.data(0, Qt.UserRole)
        if data is not None and data["kind"] == "pair":
            item = item.parent()
            data = item.data(0, Qt.UserRole) if item else None
        return item, data

    def _add_dependency_link(self):
        if self.current_job_id is None:
            QMessageBox.information(self, "ジョブ未選択", "先にジョブを選択してください。")
            return
        others = [j for j in self.db.list_jobs() if j["id"] != self.current_job_id]
        if not others:
            QMessageBox.information(self, "依存先ジョブがありません", "他のジョブを先に作成してください。")
            return
        dialog = JobDependencyLinkDialog(self.db, self.current_job_id, self)
        if dialog.exec() != QDialog.Accepted:
            return
        depends_on_job_ids = dialog.values()
        if not depends_on_job_ids:
            QMessageBox.warning(self, "入力エラー", "依存先ジョブを1つ以上選択してください。")
            return
        errors = []
        for depends_on_job_id in depends_on_job_ids:
            try:
                self.db.add_job_dependency_link(self.current_job_id, depends_on_job_id)
            except ProjectDatabaseError as e:
                errors.append(str(e))
        if errors:
            QMessageBox.warning(self, "一部追加できませんでした", "\n".join(errors))
        self._refresh_dependencies()

    def _delete_selected_dependency_link(self):
        _item, data = self._selected_link()
        if data is None:
            QMessageBox.information(self, "依存先ジョブ未選択", "削除する依存先ジョブを選択してください。")
            return
        self.db.delete_job_dependency_link(data["link_id"])
        self._refresh_dependencies()

    def _add_selected_task_pair(self):
        _item, data = self._selected_link()
        if data is None:
            QMessageBox.information(
                self, "依存先ジョブ未選択", "タスク対応を追加する依存先ジョブを選択してください。"
            )
            return
        self._add_task_pair(data["target_job_id"])

    def _add_task_pair(self, target_job_id):
        job = next(j for j in self.db.list_jobs() if j["id"] == self.current_job_id)
        target_job = next(j for j in self.db.list_jobs() if j["id"] == target_job_id)
        dialog = TaskPairDialog(self.db, job, target_job, self)
        if dialog.exec() != QDialog.Accepted:
            return
        task_id, dep_task_id = dialog.values()
        if None in (task_id, dep_task_id):
            QMessageBox.warning(self, "入力エラー", "すべての項目を選択してください。")
            return
        try:
            self.db.add_external_dependency(self.current_job_id, task_id, target_job_id, dep_task_id)
        except ProjectDatabaseError as e:
            QMessageBox.warning(self, "追加できません", str(e))
            return
        self._refresh_dependencies()

    def _delete_selected_task_pair(self):
        item = self.dep_tree.currentItem()
        data = item.data(0, Qt.UserRole) if item is not None else None
        if data is None or data["kind"] != "pair":
            QMessageBox.information(
                self, "タスク対応未選択", "削除するタスク対応（依存先ジョブの子項目）を選択してください。"
            )
            return
        if data["auto"]:
            QMessageBox.information(
                self, "削除できません",
                "自動生成された対応です。不要な場合は「有効」のチェックを外してください。",
            )
            return
        self.db.delete_external_dependency(data["dep_id"])
        self._refresh_dependencies()

    def _on_dep_tree_double_clicked(self, item, _column):
        data = item.data(0, Qt.UserRole)
        if data is not None and data["kind"] == "link":
            self._add_task_pair(data["target_job_id"])

    # -- 他タブからの通知 ----------------------------------------------------------

    def refresh_choices(self):
        """ワークフロー・マイルストーン・チームの変更を反映する。

        依存テンプレートと依存先ジョブのタスク対応は、変更が起こりうる
        DB操作（gui/db.py の add_job_dependency_link 等）側で都度同期して
        いるため本来ここで呼ぶ必要は無いはずだが、想定外の経路でズレる
        場合に備え、このタブがアクティブになるたびにも念のため同期し直す。"""
        self.db.sync_dependency_templates()
        self.refresh_jobs(select_id=self.current_job_id)
        if self.current_job_id is not None:
            self._refresh_dependencies()
