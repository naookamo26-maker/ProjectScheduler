"""
タブ1「基本情報設定」。

プロジェクト名・開始日、マイルストーン、チーム、休業日を1画面にまとめる。
変更はメモリ上のDBに即時反映されるが、ファイルへの書き込みはCtrl+S等の
明示的な保存を待つ（gui/main.py参照）。

セル編集ウィジェット（QComboBox/QDateEdit）のシグナルは、行インデックスでは
なく DBの整数ID（entity_id）をクロージャで捕まえて紐づける——行の並び替えや
他行の削除で見た目上の行番号がずれても、対象のDB行を取り違えないようにする
ため（QTableWidgetItemのテキスト編集は item.row() が常に最新の行を指すので
そのままで安全）。
"""

from datetime import date, timedelta

from PySide6.QtCore import QDate, QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.db import DuplicateNameError, ReferencedEntityError
from gui.widgets_common import (
    CrudSection,
    NoWheelDateEdit,
    OptionalSpinBox,
    auto_size_columns,
    bind_undo_session,
    capture_table_state,
    confirm_and_repair_milestone_consistency,
    confirm_or_block_delete,
    keep_selection_visible,
    make_fk_combo,
    restore_table_state,
    row_id,
    select_row_by_id,
    set_current_tree_item_keeping_focus,
    set_row_id,
    unique_default_name,
)

# チームの同時ライン数入力欄の上限（gui/tab_basic_info.py 全体で共通）。
_MAX_LINES = 999
_LINES_UNSET_TEXT = "指定なし"


def _to_qdate(iso_str):
    d = QDate.fromString(iso_str, "yyyy-MM-dd")
    return d if d.isValid() else QDate.currentDate()


def _to_iso(qdate):
    return qdate.toString("yyyy-MM-dd")


class AddMilestoneDialog(QDialog):
    """マイルストーン追加ダイアログ。名前と締切日を入力してから追加する。"""

    def __init__(self, default_name, default_date, parent=None):
        super().__init__(parent)
        self.setWindowTitle("マイルストーンを追加")

        form = QFormLayout(self)

        self.name_edit = QLineEdit(default_name)
        form.addRow("マイルストーン名", self.name_edit)

        self.date_edit = NoWheelDateEdit(default_date)
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        form.addRow("締切日", self.date_edit)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)

    def values(self):
        return self.name_edit.text(), _to_iso(self.date_edit.date())


class AddTeamDialog(QDialog):
    """チーム追加ダイアログ。名前と同時ライン数（既定値）を入力してから追加する。"""

    def __init__(self, default_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("チームを追加")

        form = QFormLayout(self)

        self.name_edit = QLineEdit(default_name)
        form.addRow("チーム名", self.name_edit)

        self.lines_spin = OptionalSpinBox(_MAX_LINES, _LINES_UNSET_TEXT)
        self.lines_spin.set_optional_value(None)  # 新規チームの既定は「指定なし」
        form.addRow("同時ライン数（開発開始日からの既定値）", self.lines_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.name_edit.text(), self.lines_spin.optional_value()


class AddCapacityChangeDialog(QDialog):
    """チームの同時ライン数の変動点を追加するダイアログ。適用開始日と、
    その日以降のライン数を入力してから追加する（値そのものは追加後も
    チームツリー上でインライン編集できる）。"""

    def __init__(self, default_date, default_lines, parent=None):
        super().__init__(parent)
        self.setWindowTitle("同時ライン数の変動点を追加")

        form = QFormLayout(self)

        self.date_edit = NoWheelDateEdit(default_date)
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        form.addRow("適用開始日", self.date_edit)

        self.lines_spin = OptionalSpinBox(_MAX_LINES, _LINES_UNSET_TEXT)
        self.lines_spin.set_optional_value(default_lines)
        form.addRow("同時ライン数", self.lines_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return _to_iso(self.date_edit.date()), self.lines_spin.optional_value()


class AddHolidayDialog(QDialog):
    """休業日追加ダイアログ。日付と対象チーム（未設定＝全チーム共通）を入力してから追加する。"""

    def __init__(self, team_options, default_date, parent=None):
        super().__init__(parent)
        self.setWindowTitle("休業日を追加")

        form = QFormLayout(self)

        self.date_edit = NoWheelDateEdit(default_date)
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        form.addRow("日付", self.date_edit)

        self.team_combo = make_fk_combo(team_options, allow_blank=True, blank_label="（全チーム共通）")
        form.addRow("対象チーム", self.team_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return _to_iso(self.date_edit.date()), self.team_combo.currentData()


class BasicInfoTab(QWidget):
    def __init__(self, db, on_teams_changed=None, on_jobs_changed=None, parent=None):
        super().__init__(parent)
        self.db = db
        # チーム一覧が変わったら他タブ（ワークフロー設計等）のコンボも更新したいので
        # 呼び出し元からコールバックを受け取る。
        self.on_teams_changed = on_teams_changed
        # マイルストーンの締切変更に伴いジョブ側のタスク上書きを再調整した場合、
        # ジョブタブの表示も更新してもらう必要がある。
        self.on_jobs_changed = on_jobs_changed
        # 締切日の編集セッション中に覚えておく変更前の値
        # （_repair_milestone_consistency_before_commit 参照）。
        self._milestone_date_before_edit = None

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)

        self.milestones_section = CrudSection(
            "マイルストーン", ["マイルストーン名", "締切日", "備考"],
            on_add=self._add_milestone, on_delete=self._delete_milestone,
        )
        self.milestones_section.table.itemChanged.connect(self._on_milestone_cell_changed)

        self.holidays_section = CrudSection(
            "休業日", ["日付", "対象チーム（未設定＝全チーム共通）", "備考"],
            on_add=self._add_holiday, on_delete=self._delete_holiday,
        )
        self.holidays_section.table.itemChanged.connect(self._on_holiday_note_changed)

        # 1段目: プロジェクト概要／休業日（上下）。2段目: マイルストーン／
        # チーム（左右5:5、`QSplitter` でユーザーがドラッグ調整可）。
        layout.addWidget(self._build_project_group())
        layout.addWidget(self.holidays_section)

        row2 = QSplitter(Qt.Horizontal)
        row2.addWidget(self.milestones_section)
        row2.addWidget(self._build_teams_group())
        row2.setStretchFactor(0, 5)
        row2.setStretchFactor(1, 5)
        layout.addWidget(row2, 1)
        self._row2_splitter = row2

        # コンストラクタ時点（実際のウィジェット幅が確定する前）にsetSizes()を
        # 呼んでも比率が反映されない（表示後の最初のレイアウトで上書きされる）
        # ため、レイアウト確定後（次のイベントループ）に改めて設定し直す
        # （gui/tab_jobs.py の _apply_initial_splitter_sizes と同じ理由）。
        QTimer.singleShot(0, self._apply_initial_splitter_sizes)

        self.refresh_all()

    # チームを選択した後、チーム欄（ツリー＋ツールバー）の外をクリックしたら
    # 選択を解除する。アプリ全体のマウスクリックを監視する必要があるため
    # QApplication単位のイベントフィルタで
    # 実装するが、これはアプリ内で発生する *すべての* イベントを一度Python側へ
    # 通すことになる。このタブが表示されている間だけ仕掛け、他のタブへ移ったら
    # 外す（例えばジョブ一覧を1,900行組み立てる間だけで55万回以上呼ばれ、
    # それだけで2秒以上を無駄にしていた）。
    def showEvent(self, event):
        super().showEvent(event)
        QApplication.instance().installEventFilter(self)

    def hideEvent(self, event):
        super().hideEvent(event)
        QApplication.instance().removeEventFilter(self)

    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.MouseButtonPress
            and self.teams_tree.isVisible()
            and self._selected_team_tree_item() is not None
            and isinstance(obj, QWidget)
            and not self._is_within_teams_panel(obj)
        ):
            self.teams_tree.clearSelection()
        return super().eventFilter(obj, event)

    def _is_within_teams_panel(self, widget):
        w = widget
        while w is not None:
            if w is self._teams_panel:
                return True
            w = w.parentWidget()
        return False

    def _apply_initial_splitter_sizes(self):
        total = self._row2_splitter.width()
        if total > 0:
            self._row2_splitter.setSizes([total // 2, total - total // 2])

    # -- プロジェクト概要 -----------------------------------------------------

    def _build_project_group(self):
        group = QGroupBox("プロジェクト概要")
        form = QFormLayout(group)

        self.project_name_edit = QLineEdit()
        self.project_name_edit.editingFinished.connect(self._on_project_changed)
        form.addRow("プロジェクト名", self.project_name_edit)

        self.start_date_edit = NoWheelDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.start_date_edit.dateChanged.connect(self._on_project_changed)
        bind_undo_session(self.start_date_edit, self.db, "開発開始日を変更")
        form.addRow("開発開始日", self.start_date_edit)

        return group

    def _on_project_changed(self):
        self.db.set_project(self.project_name_edit.text(), _to_iso(self.start_date_edit.date()))

    def refresh_project(self):
        proj = self.db.get_project()
        self.project_name_edit.blockSignals(True)
        self.start_date_edit.blockSignals(True)
        self.project_name_edit.setText(proj["project_name"])
        self.start_date_edit.setDate(_to_qdate(proj["start_date"] or date.today().isoformat()))
        self.project_name_edit.blockSignals(False)
        self.start_date_edit.blockSignals(False)

    # -- マイルストーン ---------------------------------------------------------

    def refresh_milestones(self):
        table = self.milestones_section.table
        table.blockSignals(True)
        table.setRowCount(0)
        for ms in self.db.list_milestones():
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(ms["name"]))
            set_row_id(table, row, ms["id"])
            date_edit = NoWheelDateEdit(_to_qdate(ms["end_date"]))
            date_edit.setCalendarPopup(True)
            date_edit.setDisplayFormat("yyyy-MM-dd")
            date_edit.dateChanged.connect(
                lambda qdate, eid=ms["id"]: self._on_milestone_date_changed(eid, qdate)
            )
            # 締切日順の並べ替えは編集中に行わず、編集が終わってから行う
            # （編集中に表を作り直すと、操作中の日付欄が破棄されてフォーカスが飛ぶ）。
            # 整合性の再調整は、Undo単位がまだ開いているうちに行う必要がある
            # （on_session_endでは単位が閉じた後になり、Undoが2回に分かれる）。
            bind_undo_session(
                date_edit, self.db, "マイルストーンの締切日を変更",
                on_session_end=self._resort_milestones_later,
                on_before_commit=self._repair_milestone_consistency_before_commit,
            )
            table.setCellWidget(row, 1, date_edit)
            table.setItem(row, 2, QTableWidgetItem(ms["note"]))
        table.blockSignals(False)
        auto_size_columns(table, stretch_last=True)

    def _add_milestone(self):
        existing = {ms["name"] for ms in self.db.list_milestones()}
        default_name = unique_default_name(existing, "新しいマイルストーン")
        dialog = AddMilestoneDialog(default_name, QDate.currentDate(), self)
        while True:
            if dialog.exec() != QDialog.Accepted:
                return
            name, end_date = dialog.values()
            try:
                new_id = self.db.add_milestone(name, end_date)
            except DuplicateNameError as e:
                QMessageBox.warning(self, "追加できません", str(e))
                continue
            break
        self.refresh_milestones()
        select_row_by_id(self.milestones_section.table, new_id)

    def _delete_milestone(self, row):
        table = self.milestones_section.table
        ms_id = row_id(table, row)
        count = self.db.milestone_usage_count(ms_id)
        if not confirm_or_block_delete(self, count, "このマイルストーン", hard_block=False):
            return
        self.db.delete_milestone(ms_id)
        self.refresh_milestones()

    def _on_milestone_cell_changed(self, item):
        if item.column() not in (0, 2):
            return
        table = self.milestones_section.table
        ms_id = row_id(table, item.row())
        if ms_id is None:
            return
        name = table.item(item.row(), 0).text()
        note = table.item(item.row(), 2).text()
        date_edit = table.cellWidget(item.row(), 1)
        try:
            self.db.update_milestone(ms_id, name, _to_iso(date_edit.date()), note)
        except DuplicateNameError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            self.refresh_milestones()
            return
        if item.column() == 0:
            # 名前は締切日に次ぐ第2ソートキー（db.list_milestones参照）のため、
            # 同じ締切日の別マイルストーンとの前後関係が変わりうる。表示順を
            # 締切日順に合わせ直す（このメソッド自体がitemChangedシグナル内から
            # 呼ばれているため、ウィジェットの再構築は次のイベントループへ遅延させる）。
            QTimer.singleShot(0, self.refresh_milestones)

    def _resort_milestones_later(self):
        """締切日順の表示を保つよう並べ直す。日付欄の編集が終わった時点で呼ぶ
        （このメソッド自体がフォーカス喪失の処理中から呼ばれるため、ウィジェットの
        再構築は次のイベントループへ遅延させる）。"""
        QTimer.singleShot(0, self.refresh_milestones)

    def _on_milestone_date_changed(self, milestone_id, qdate):
        table = self.milestones_section.table
        for row in range(table.rowCount()):
            if row_id(table, row) == milestone_id:
                name = table.item(row, 0).text()
                note = table.item(row, 2).text()
                previous = self.db.get_milestone(milestone_id)
                self.db.update_milestone(milestone_id, name, _to_iso(qdate), note)
                # 締切日が動くとマイルストーン同士の前後関係が入れ替わりうる。
                # 編集セッションの確定時（_repair_milestone_consistency_before_commit）に
                # まとめて検査するため、変更前の値だけ覚えておく（日付欄は1回の
                # 編集で何度も値が変わるので、検査・確認ダイアログは毎回は出さない）。
                if self._milestone_date_before_edit is None:
                    self._milestone_date_before_edit = (milestone_id, previous)
                return

    def _repair_milestone_consistency_before_commit(self):
        """マイルストーンの締切日の編集が確定する直前（Undo単位がまだ開いている
        うち）に呼ばれる。前後関係の入れ替わりでジョブ側のタスク上書きが
        「先行タスクより早い締切」になってしまう場合、確認の上で引き上げる。
        キャンセルされた場合は締切日の変更自体を元に戻す（同じUndo単位の中で
        差し引きゼロになるため、Undoエントリも積まれない）。"""
        edited = self._milestone_date_before_edit
        self._milestone_date_before_edit = None
        if edited is None:
            return
        if confirm_and_repair_milestone_consistency(
            self.db, self, "マイルストーンの締切日の変更",
        ):
            self._notify_jobs_changed()
            return

        milestone_id, previous = edited
        if previous is not None:
            self.db.update_milestone(
                milestone_id, previous["name"], previous["end_date"], previous["note"],
            )
        QTimer.singleShot(0, self.refresh_milestones)

    def _notify_jobs_changed(self):
        """ジョブ側のデータを書き換えたことを他タブへ伝える（タブ3が開いた
        ままでも表示が古くならないようにする）。"""
        if self.on_jobs_changed:
            self.on_jobs_changed()

    # -- チーム（ツリー: 上位＝チーム、子＝既定値＋期間中の変動点＝すべてインライン編集） ---

    def _build_teams_panel(self):
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)

        toolbar = QHBoxLayout()
        add_team_btn = QPushButton("＋ チーム")
        add_team_btn.clicked.connect(self._add_team)
        del_team_btn = QPushButton("－ チーム")
        del_team_btn.clicked.connect(self._delete_team_selected)
        toolbar.addWidget(add_team_btn)
        toolbar.addWidget(del_team_btn)
        toolbar.addSpacing(16)
        add_change_btn = QPushButton("＋ 変動点")
        add_change_btn.clicked.connect(self._add_capacity_change_selected)
        del_change_btn = QPushButton("－ 変動点")
        del_change_btn.clicked.connect(self._delete_capacity_change_selected)
        toolbar.addWidget(add_change_btn)
        toolbar.addWidget(del_change_btn)
        toolbar.addStretch(1)
        panel_layout.addLayout(toolbar)

        self.teams_tree = QTreeWidget()
        self.teams_tree.setColumnCount(2)
        self.teams_tree.setHeaderLabels(["チーム名 ／ 適用開始日", "ライン数"])
        # 列幅を1:1にする（両方をStretchにすると残り幅を均等に分け合う）。
        teams_header = self.teams_tree.header()
        teams_header.setSectionResizeMode(0, QHeaderView.Stretch)
        teams_header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.teams_tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.teams_tree.itemChanged.connect(self._on_team_name_changed)
        keep_selection_visible(self.teams_tree)
        panel_layout.addWidget(self.teams_tree)

        return panel

    def refresh_teams(self):
        tree = self.teams_tree
        tree.blockSignals(True)
        tree.clear()
        proj = self.db.get_project()
        start_date = proj["start_date"] or date.today().isoformat()
        for team in self.db.list_teams():
            top = QTreeWidgetItem([team["name"], ""])
            top.setFlags(top.flags() | Qt.ItemIsEditable)
            top.setData(0, Qt.UserRole, {"kind": "team", "team_id": team["id"]})
            tree.addTopLevelItem(top)

            # 開発開始日からの既定値は team_capacity_changes に実体を持たない
            # （teams.max_lines そのもの）ため、他の変動点と同じ見た目で表示する
            # 合成の子行として追加する（日付は開発開始日で固定・削除不可）。
            default_child = QTreeWidgetItem([start_date, ""])
            default_child.setFlags(default_child.flags() & ~Qt.ItemIsEditable)
            default_child.setData(0, Qt.UserRole, {"kind": "default_capacity", "team_id": team["id"]})
            top.addChild(default_child)

            default_spin = OptionalSpinBox(_MAX_LINES, _LINES_UNSET_TEXT)
            default_spin.set_optional_value(team["max_lines"])
            default_spin.valueChanged.connect(
                lambda _val, eid=team["id"], spin=default_spin:
                    self._on_team_lines_changed(eid, spin.optional_value())
            )
            bind_undo_session(default_spin, self.db, "チームの同時ライン数を変更")
            tree.setItemWidget(default_child, 1, default_spin)

            for c in self.db.list_team_capacity_changes(team["id"]):
                child = QTreeWidgetItem(["", ""])
                child.setFlags(child.flags() & ~Qt.ItemIsEditable)
                child.setData(0, Qt.UserRole, {
                    "kind": "capacity_change", "team_id": team["id"], "change_id": c["id"],
                })
                top.addChild(child)

                date_edit = NoWheelDateEdit(_to_qdate(c["start_date"]))
                date_edit.setCalendarPopup(True)
                date_edit.setDisplayFormat("yyyy-MM-dd")
                date_edit.dateChanged.connect(
                    lambda _qdate, cid=c["id"], it=child: self._on_team_capacity_change_edited(cid, it)
                )
                bind_undo_session(
                    date_edit, self.db, "同時ライン数の変更点を変更",
                    on_session_end=self._resort_team_capacity_changes_later,
                )
                tree.setItemWidget(child, 0, date_edit)

                lines_spin = OptionalSpinBox(_MAX_LINES, _LINES_UNSET_TEXT)
                lines_spin.set_optional_value(c["lines"])
                lines_spin.valueChanged.connect(
                    lambda _val, cid=c["id"], it=child: self._on_team_capacity_change_edited(cid, it)
                )
                bind_undo_session(lines_spin, self.db, "同時ライン数の変更点を変更")
                tree.setItemWidget(child, 1, lines_spin)
            top.setExpanded(True)
        tree.blockSignals(False)

    def _selected_team_tree_item(self):
        """teams_tree の実際の選択状態（ハイライト）を返す。currentItem()は
        空欄部分クリックで選択が解除されても値が残り続けてしまうため使わない
        （SingleSelectionのため高々1件）。"""
        selected = self.teams_tree.selectedItems()
        return selected[0] if selected else None

    def _resolve_team_item(self, item):
        """選択中の項目（チーム自身、またはその子＝既定値／容量変更点）から、
        対象のチーム項目とそのUserRoleデータを返す（gui/tab_jobs.py の
        _selected_link と同じ「子は親へ辿る」考え方）。未選択なら (None, None)。"""
        if item is None:
            return None, None
        data = item.data(0, Qt.UserRole)
        if data is not None and data.get("kind") in ("capacity_change", "default_capacity"):
            item = item.parent()
            data = item.data(0, Qt.UserRole) if item else None
        return item, data

    def _find_team_tree_item(self, team_id):
        for i in range(self.teams_tree.topLevelItemCount()):
            top = self.teams_tree.topLevelItem(i)
            data = top.data(0, Qt.UserRole)
            if data is not None and data.get("team_id") == team_id:
                return top
        return None

    def _select_team_tree_item(self, team_id):
        item = self._find_team_tree_item(team_id)
        if item is not None:
            set_current_tree_item_keeping_focus(self.teams_tree, item)

    def _add_capacity_change_selected(self):
        _item, data = self._resolve_team_item(self._selected_team_tree_item())
        if data is None:
            QMessageBox.information(self, "追加", "変動点を追加するチームを選択してください。")
            return
        team_id = data["team_id"]
        team = next((t for t in self.db.list_teams() if t["id"] == team_id), None)
        if team is None:
            return
        proj = self.db.get_project()
        project_start = proj["start_date"] or date.today().isoformat()
        existing = self.db.list_team_capacity_changes(team_id)
        existing_dates = {c["start_date"] for c in existing}
        existing_dates.add(project_start)
        # 既定日は、既存の変更点のうち最も遅い日付の翌日（無ければ開発開始日の翌日）を提案する。
        if existing:
            base = _to_qdate(existing[-1]["start_date"]).addDays(1)
        else:
            base = _to_qdate(project_start).addDays(1)
        while _to_iso(base) in existing_dates:
            base = base.addDays(1)

        dialog = AddCapacityChangeDialog(base, team["max_lines"], self)
        while True:
            if dialog.exec() != QDialog.Accepted:
                return
            start_date, lines = dialog.values()
            try:
                new_id = self.db.add_team_capacity_change(team_id, start_date, lines)
            except DuplicateNameError as e:
                QMessageBox.warning(self, "追加できません", str(e))
                continue
            break
        self.refresh_teams()
        self._select_capacity_change_item(team_id, new_id)

    def _delete_capacity_change_selected(self):
        item = self._selected_team_tree_item()
        data = item.data(0, Qt.UserRole) if item is not None else None
        if data is None or data.get("kind") != "capacity_change":
            QMessageBox.information(
                self, "削除", "削除する変動点を選択してください（既定値の行は削除できません）。"
            )
            return
        team_id = data["team_id"]
        self.db.delete_team_capacity_change(data["change_id"])
        self.refresh_teams()
        self._select_team_tree_item(team_id)

    def _select_capacity_change_item(self, team_id, change_id):
        top = self._find_team_tree_item(team_id)
        if top is None:
            return
        for j in range(top.childCount()):
            child = top.child(j)
            data = child.data(0, Qt.UserRole)
            if data is not None and data.get("kind") == "capacity_change" and data.get("change_id") == change_id:
                set_current_tree_item_keeping_focus(self.teams_tree, child)
                return

    def _on_team_capacity_change_edited(self, change_id, item):
        date_edit = self.teams_tree.itemWidget(item, 0)
        lines_spin = self.teams_tree.itemWidget(item, 1)
        if date_edit is None or lines_spin is None:
            return
        try:
            self.db.update_team_capacity_change(
                change_id, _to_iso(date_edit.date()), lines_spin.optional_value()
            )
        except DuplicateNameError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            self.refresh_teams()
            return

    def _resort_team_capacity_changes_later(self):
        """適用開始日を変更すると並び順が変わりうるため、編集セッション
        （フォーカスが外れたタイミング）の終わりに並べ直す（編集中に作り
        直すとウィジェットが破棄されフォーカスが飛んでしまうため）。並べ
        直すとツリーが作り直され選択・スクロール位置が失われるため、
        編集していた項目を選び直して見えている位置へ戻し、見失わないように
        する（対象が特定できない場合のみ、元のスクロール位置をそのまま戻す）。"""
        selected = self._selected_team_tree_item()
        saved_data = selected.data(0, Qt.UserRole) if selected is not None else None
        scrollbar = self.teams_tree.verticalScrollBar()
        scroll_value = scrollbar.value()

        def _resort():
            self.refresh_teams()
            if saved_data is not None:
                self._select_team_tree_item_by_data(saved_data)
            else:
                scrollbar.setValue(scroll_value)

        QTimer.singleShot(0, _resort)

    def _add_team(self):
        existing = {t["name"] for t in self.db.list_teams()}
        default_name = unique_default_name(existing, "新しいチーム")
        dialog = AddTeamDialog(default_name, self)
        while True:
            if dialog.exec() != QDialog.Accepted:
                return
            name, max_lines = dialog.values()
            try:
                new_id = self.db.add_team(name, max_lines)
            except DuplicateNameError as e:
                QMessageBox.warning(self, "追加できません", str(e))
                continue
            break
        self.refresh_teams()
        self._select_team_tree_item(new_id)
        self._notify_teams_changed()

    def _delete_team_selected(self):
        _item, data = self._resolve_team_item(self._selected_team_tree_item())
        if data is None:
            QMessageBox.information(self, "削除", "削除するチームを選択してください。")
            return
        team_id = data["team_id"]
        count = self.db.team_usage_count(team_id)
        if not confirm_or_block_delete(self, count, "このチーム", hard_block=True):
            return
        try:
            self.db.delete_team(team_id)
        except ReferencedEntityError as e:
            QMessageBox.warning(self, "削除できません", str(e))
            return
        self.refresh_teams()
        self._notify_teams_changed()

    def _on_team_name_changed(self, item, column):
        if column != 0:
            return
        data = item.data(0, Qt.UserRole)
        if data is None or data.get("kind") != "team":
            return
        team_id = data["team_id"]
        team = next((t for t in self.db.list_teams() if t["id"] == team_id), None)
        if team is None:
            return
        try:
            self.db.update_team(team_id, item.text(0), team["max_lines"])
        except DuplicateNameError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            self.refresh_teams()
            return
        self._notify_teams_changed()

    def _on_team_lines_changed(self, team_id, value):
        top = self._find_team_tree_item(team_id)
        if top is None:
            return
        self.db.update_team(team_id, top.text(0), value)

    def _notify_teams_changed(self):
        if self.on_teams_changed:
            self.on_teams_changed()

    # -- チーム（枠） ---------------------------------------------------------------

    def _build_teams_group(self):
        group = QGroupBox("チーム")
        group_layout = QVBoxLayout(group)
        self._teams_panel = self._build_teams_panel()
        group_layout.addWidget(self._teams_panel)
        return group

    # -- 休業日 -------------------------------------------------------------------

    def refresh_holidays(self):
        table = self.holidays_section.table
        table.blockSignals(True)
        table.setRowCount(0)
        team_options = [(t["id"], t["name"]) for t in self.db.list_teams()]
        for hol in self.db.list_holidays():
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(""))  # 日付列はウィジェット表示のみ、テキストは未使用
            set_row_id(table, row, hol["id"])

            date_edit = NoWheelDateEdit(_to_qdate(hol["date"]))
            date_edit.setCalendarPopup(True)
            date_edit.setDisplayFormat("yyyy-MM-dd")
            date_edit.dateChanged.connect(
                lambda qdate, eid=hol["id"]: self._on_holiday_changed(eid)
            )
            bind_undo_session(date_edit, self.db, "休業日の日付を変更")
            table.setCellWidget(row, 0, date_edit)

            combo = make_fk_combo(team_options, hol["team_id"], allow_blank=True, blank_label="（全チーム共通）")
            combo.currentIndexChanged.connect(
                lambda idx, eid=hol["id"]: self._on_holiday_changed(eid)
            )
            table.setCellWidget(row, 1, combo)

            table.setItem(row, 2, QTableWidgetItem(hol["note"]))
        table.blockSignals(False)
        auto_size_columns(table, stretch_last=True)

    def _add_holiday(self):
        # 「追加」連打で全チーム共通・同日の重複エラーが出ないよう、空いている日付を
        # 既定値として提案する（既定は全チーム共通＝チーム未設定。ダイアログ側で
        # 変更可能なので、実際に重複した場合はダイアログ内で警告して再入力させる）。
        existing = {h["date"] for h in self.db.list_holidays() if h["team_id"] is None}
        d = date.today()
        while d.isoformat() in existing:
            d += timedelta(days=1)
        team_options = [(t["id"], t["name"]) for t in self.db.list_teams()]
        dialog = AddHolidayDialog(team_options, _to_qdate(d.isoformat()), self)
        while True:
            if dialog.exec() != QDialog.Accepted:
                return
            hol_date, team_id = dialog.values()
            try:
                new_id = self.db.add_holiday(hol_date, team_id)
            except DuplicateNameError as e:
                QMessageBox.warning(self, "追加できません", str(e))
                continue
            break
        self.refresh_holidays()
        select_row_by_id(self.holidays_section.table, new_id)

    def _delete_holiday(self, row):
        table = self.holidays_section.table
        hol_id = row_id(table, row)
        self.db.delete_holiday(hol_id)
        self.refresh_holidays()

    def _on_holiday_changed(self, holiday_id):
        table = self.holidays_section.table
        for row in range(table.rowCount()):
            if row_id(table, row) == holiday_id:
                date_edit = table.cellWidget(row, 0)
                combo = table.cellWidget(row, 1)
                note = table.item(row, 2).text()
                try:
                    self.db.update_holiday(holiday_id, _to_iso(date_edit.date()), combo.currentData(), note)
                except DuplicateNameError as e:
                    QMessageBox.warning(self, "変更できません", str(e))
                    self.refresh_holidays()
                return

    def _on_holiday_note_changed(self, item):
        if item.column() != 2:
            return
        table = self.holidays_section.table
        hol_id = row_id(table, item.row())
        if hol_id is None:
            return
        date_edit = table.cellWidget(item.row(), 0)
        combo = table.cellWidget(item.row(), 1)
        try:
            self.db.update_holiday(hol_id, _to_iso(date_edit.date()), combo.currentData(), item.text())
        except DuplicateNameError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            self.refresh_holidays()

    # -- 全体再読み込み ----------------------------------------------------------

    def refresh_all(self):
        self.refresh_project()
        self.refresh_milestones()
        self.refresh_teams()
        self.refresh_holidays()

    def refresh_choices(self):
        """他タブの変更（現状なし）に合わせて表示を更新する共通インターフェース。
        MainWindowのタブ切り替え時フックから呼ばれる。"""
        self.refresh_all()

    # -- Undo/Redo用の選択・フォーカス状態 -------------------------------------------

    def capture_ui_state(self):
        team_item = self.teams_tree.currentItem()
        return {
            "milestones": capture_table_state(self.milestones_section.table),
            "team_selected": team_item.data(0, Qt.UserRole) if team_item else None,
            "holidays": capture_table_state(self.holidays_section.table),
        }

    def restore_ui_state(self, state):
        if not state:
            return
        restore_table_state(self.milestones_section.table, state.get("milestones"))
        team_data = state.get("team_selected")
        if team_data is not None:
            self._select_team_tree_item_by_data(team_data)
        restore_table_state(self.holidays_section.table, state.get("holidays"))

    def _select_team_tree_item_by_data(self, data):
        """capture_ui_state が記録したUserRoleデータ（チーム自身、または
        その子＝容量変更点のどちらか）と一致する項目を選び直す
        （gui/tab_jobs.py の _select_dep_tree_item と同じ考え方）。"""
        for i in range(self.teams_tree.topLevelItemCount()):
            top = self.teams_tree.topLevelItem(i)
            if top.data(0, Qt.UserRole) == data:
                set_current_tree_item_keeping_focus(self.teams_tree, top)
                return
            for j in range(top.childCount()):
                child = top.child(j)
                if child.data(0, Qt.UserRole) == data:
                    set_current_tree_item_keeping_focus(self.teams_tree, child)
                    return
