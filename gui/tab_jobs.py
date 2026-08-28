"""
タブ3「ジョブ作成」。

左右2分割（QSplitter、既定50/50でユーザーがドラッグ調整可）。
- 左: ジョブ一覧（名前・ワークフロー・既定マイルストーン・優先度・タグ）を
  縦全体に表示。上部の「絞り込み」は折りたたみ可能なセクション（既定は
  折りたたみ状態。常時展開だと縦幅を取りすぎるため）で、開くとワークフロー
  ／マイルストーン／タグの3段が現れる（いずれもチェックボックスのOR条件で、
  3つの間はAND条件。一時的に表示件数を絞るだけでデータは削除されない）。
  「タグ」列はStretchで残り幅を吸収し、パネル幅にかかわらず横スクロール
  なしで5列すべてが収まるようにしている。「複製」ボタンで選択中のジョブを
  タスク上書き・依存先ジョブごと複製できる（このジョブに依存している側は
  複製先へ引き継がない。db.duplicate_job参照）。タグは単純なカンマ区切りの
  テキスト入力（例:「緊急, 顧客A」）で、保存時に前後の空白除去・重複排除・
  「, 」区切りへの正規化を行う（gui/db.py の normalize_tags 参照）。
- 右: 上下2分割（QSplitter）で、選択中ジョブのタスク上書き表と依存先ジョブを
  縦に並べる。
  - タスク上書き表: 選択ジョブが使うワークフローのタスク一覧をそのまま
    自動的に表示し（手入力不要）、既定から外れる項目（無効化・日数上書き・
    マイルストーン上書き・チーム上書き）だけを編集する。既定日数・既定
    チームは、上書き用のスピンボックス/コンボボックスの特殊表示
    （「既定（n日）」「（既定: 値）」）に統合し、専用の列は持たない。
    既定値のままの行はDBに保存しない（差分のみ保持、gui/db.py参照）。
    ジョブ一覧のセルウィジェット（ワークフロー・マイルストーンのコンボ、
    優先度のスピンボックス）は、**画面に見えている行の分だけ**実体化する
    （後述の「セルウィジェットの遅延生成」）。
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

## セルウィジェットの遅延生成

ジョブ一覧は行数がそのままジョブ数になるため、大規模プロジェクトでは
数千行になりうる。全行に QComboBox / QSpinBox を実体として置くと、タブを
開くだけで行数に比例した時間ウィンドウが固まる（1,916ジョブで約19秒。
内訳は insertRow の繰り返し7.2秒、コンボの項目追加4.4秒、
setCellWidget 1.5秒）。そこで次の2点で行数への依存を切っている。

1. 行は `setRowCount()` で一度に確保する（1行ずつ `insertRow()` すると
   その都度ジオメトリ再計算が走り、行数の増加に対して急激に遅くなる）。
2. セルウィジェットは**可視範囲（＋上下マージン）の行にだけ**作り、範囲外
   へ出た行からは取り外す（`_sync_job_row_widgets`）。範囲外の行は、同じ
   内容を読み取り専用のテキストとして表示しておく。スクロールに追従して
   入れ替えるため、ユーザーからは常にウィジェットが並んで見える。

編集中の行（セルウィジェットがフォーカスを持っている行）は、範囲外へ出ても
取り外さない。`bind_undo_session` はフォーカスの出入りでUndo単位を開閉する
ため、単位を開いたままウィジェットを破棄すると閉じられなくなるため。
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
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

from gui.db import DuplicateNameError, ProjectDatabaseError, normalize_tags, parse_job_tags
from gui.widgets_common import (
    ChoiceFilterGroup,
    CollapsibleSection,
    CrudSection,
    DefaultAwareSpinBox,
    NoWheelComboBox,
    NoWheelListWidget,
    NoWheelSpinBox,
    OptionalDateEdit,
    auto_size_columns,
    bind_undo_session,
    capture_table_state,
    confirm_or_block_delete,
    keep_selection_visible,
    make_fk_combo,
    restore_table_state,
    row_id,
    set_row_id,
    unique_default_name,
)


# セルウィジェットを用意しておく、可視範囲の上下の余分な行数。スクロール
# のたびに作り直す行を減らしつつ、素早くスクロールしてもテキスト表示のまま
# の行が見えないだけの余裕を持たせる。
_VISIBLE_ROW_MARGIN = 12

# 既定マイルストーン未設定の表示（コンボの空欄と、ウィジェットが無い行の
# テキスト表示とで文言を揃える必要があるため定数にしてある）。
_BLANK_MILESTONE_LABEL = "（未設定）"

# ジョブ一覧の絞り込み（マイルストーン／タグ）で「該当が無いジョブ」を
# まとめるための擬似キー。実在のID・タグ文字列と衝突しないよう None を使う。
_NO_MILESTONE_FILTER_KEY = None
_NO_TAG_FILTER_KEY = None
_NO_TAG_FILTER_LABEL = "（タグなし）"

# コンボボックスの▼やスピンボックスの▲▼のぶん、テキスト幅より少し広くする
# （列幅は読み取り専用テキストの幅を基準に自動調整されるため）。
_CELL_WIDGET_EXTRA_WIDTH = 34

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
    4: lambda j: j["tags"],
}


class JobsTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_job_id = None
        self._sort_column = 0
        self._sort_ascending = True
        # セルウィジェットの遅延生成用（モジュール冒頭の説明を参照）
        self._materialized_rows = set()   # 実体化済みの行番号
        self._job_by_id = {}              # job_id -> 直近に読み込んだジョブの内容
        self._row_by_job_id = {}          # job_id -> 行番号
        self._workflow_options = []       # [(id, 表示名), ...]
        self._milestone_options = []

        layout = QVBoxLayout(self)

        # ワークフロー／マイルストーン／タグの3つの絞り込み（いずれもOR条件の
        # チェックボックス一覧で、3つの間はAND条件で組み合わせる）。常時展開だと
        # 縦幅を取りすぎるため、まとめて1つの折りたたみセクションに収める。
        self.filters_section = CollapsibleSection("絞り込み")
        layout.addWidget(self.filters_section)

        self.workflow_filter = ChoiceFilterGroup("ワークフロー")
        self.workflow_filter.changed.connect(lambda: self.refresh_jobs(select_id=self.current_job_id))
        self.filters_section.content_layout.addWidget(self.workflow_filter)

        self.milestone_filter = ChoiceFilterGroup("マイルストーン")
        self.milestone_filter.changed.connect(lambda: self.refresh_jobs(select_id=self.current_job_id))
        self.filters_section.content_layout.addWidget(self.milestone_filter)

        self.tag_filter = ChoiceFilterGroup("タグ")
        self.tag_filter.changed.connect(lambda: self.refresh_jobs(select_id=self.current_job_id))
        self.filters_section.content_layout.addWidget(self.tag_filter)

        self.jobs_section = CrudSection(
            "ジョブ", ["ジョブ名", "ワークフロー", "既定マイルストーン", "優先度", "タグ"],
            on_add=self._add_job, on_delete=self._delete_job, on_duplicate=self._duplicate_job,
        )
        self.jobs_section.table.itemChanged.connect(self._on_job_cell_text_changed)
        self.jobs_section.table.currentCellChanged.connect(self._on_job_selection_changed)
        # 列見出しをクリックするとその列で並び替えられる（同じ列を再クリックで昇順/降順切替）。
        jobs_header = self.jobs_section.table.horizontalHeader()
        jobs_header.setSectionsClickable(True)
        jobs_header.setSortIndicatorShown(True)
        jobs_header.setSortIndicator(self._sort_column, Qt.AscendingOrder)
        jobs_header.sectionClicked.connect(self._on_job_header_clicked)
        # 「タグ」列（内容の長さが最も変動する）に残り幅を吸収させ、パネル幅に
        # かかわらず横スクロールなしで5列すべてが収まるようにする。
        jobs_header.setSectionResizeMode(4, QHeaderView.Stretch)
        # スクロールや表示領域の変化に追従して、見えている行にだけ
        # セルウィジェットを用意する。
        jobs_scrollbar = self.jobs_section.table.verticalScrollBar()
        jobs_scrollbar.valueChanged.connect(self._sync_job_row_widgets)
        jobs_scrollbar.rangeChanged.connect(lambda _min, _max: self._sync_job_row_widgets())

        override_group = QGroupBox("タスク上書き（選択中のジョブ）")
        override_layout = QVBoxLayout(override_group)

        self.override_table = QTableWidget(0, 6)
        self.override_table.setHorizontalHeaderLabels(
            ["タスク名", "有効", "日数", "マイルストーン", "チーム", "開始固定日"]
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

    # -- ワークフロー／マイルストーン／タグの絞り込み ------------------------------------

    def _job_tag_keys(self, job):
        """絞り込み判定に使う、ジョブが持つタグのキー集合。タグが1つも無い
        ジョブは擬似キー _NO_TAG_FILTER_KEY（＝「（タグなし）」）を持つ扱いにする。"""
        tags = parse_job_tags(job["tags"])
        return set(tags) if tags else {_NO_TAG_FILTER_KEY}

    def _rebuild_filters(self, jobs):
        """ワークフロー／マイルストーン／タグの絞り込み用チェックボックスを、
        現在のDBの内容（渡された絞り込み前のジョブ一覧）に合わせて再構築する。
        既存のチェック状態はキーで可能な限り維持し、新規キーは既定で表示にする。"""
        self.workflow_filter.rebuild([(w["id"], w["name"]) for w in self.db.list_workflows()])
        self.milestone_filter.rebuild(
            [(m["id"], m["name"]) for m in self.db.list_milestones()]
            + [(_NO_MILESTONE_FILTER_KEY, _BLANK_MILESTONE_LABEL)]
        )
        tag_keys = set()
        for job in jobs:
            tag_keys.update(self._job_tag_keys(job))
        has_no_tag = _NO_TAG_FILTER_KEY in tag_keys
        tag_keys.discard(_NO_TAG_FILTER_KEY)
        tag_items = [(tag, tag) for tag in sorted(tag_keys)]
        if has_no_tag:
            tag_items.append((_NO_TAG_FILTER_KEY, _NO_TAG_FILTER_LABEL))
        self.tag_filter.rebuild(tag_items)

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
        all_jobs = self.db.list_jobs()
        self._rebuild_filters(all_jobs)
        visible_workflow_ids = self.workflow_filter.visible_keys()
        visible_milestone_keys = self.milestone_filter.visible_keys()
        visible_tag_keys = self.tag_filter.visible_keys()

        table = self.jobs_section.table
        table.blockSignals(True)
        table.setRowCount(0)  # 既存のセルウィジェットもここで破棄される
        self._materialized_rows.clear()

        self._workflow_options = [(w["id"], w["name"]) for w in self.db.list_workflows()]
        self._milestone_options = [(m["id"], m["name"]) for m in self.db.list_milestones()]
        workflow_names = dict(self._workflow_options)
        milestone_names = dict(self._milestone_options)

        sort_key = _JOB_SORT_KEYS[self._sort_column]
        jobs = [
            job for job in sorted(all_jobs, key=sort_key, reverse=not self._sort_ascending)
            if job["workflow_id"] in visible_workflow_ids
            and job["default_milestone_id"] in visible_milestone_keys
            and self._job_tag_keys(job) & visible_tag_keys
        ]
        self._job_by_id = {job["id"]: job for job in jobs}
        self._row_by_job_id = {job["id"]: row for row, job in enumerate(jobs)}

        # 1行ずつ insertRow() すると、その都度ジオメトリの再計算が走って行数の
        # 増加に対して急激に遅くなる（1,900行で約7秒）。先に行数を確定させる。
        table.setRowCount(len(jobs))
        select_row = -1
        for row, job in enumerate(jobs):
            table.setItem(row, 0, QTableWidgetItem(job["name"]))
            set_row_id(table, row, job["id"])
            # セルウィジェットは可視範囲の行にだけ後から載せる。それ以外の行は
            # 同じ内容を読み取り専用のテキストとして見せておく。
            table.setItem(row, 1, _readonly_item(workflow_names.get(job["workflow_id"], "")))
            table.setItem(row, 2, _readonly_item(
                milestone_names.get(job["default_milestone_id"], _BLANK_MILESTONE_LABEL)))
            table.setItem(row, 3, _readonly_item(str(job["priority"])))
            # タグはシンプルなテキスト入力（カンマ区切り）のため、ワークフロー
            # ／マイルストーン／優先度と違って専用ウィジェットを持たず、常に
            # 編集可能なitemとして表示する（ジョブ名列と同じ扱い）。
            table.setItem(row, 4, QTableWidgetItem(job["tags"]))
            if job["id"] == select_id:
                select_row = row
        table.blockSignals(False)

        auto_size_columns(table)
        for column in (1, 2, 3):
            table.setColumnWidth(column, table.columnWidth(column) + _CELL_WIDGET_EXTRA_WIDTH)
        self._sync_job_row_widgets()

        if select_row >= 0:
            table.setCurrentCell(select_row, 0)
        else:
            self._on_job_selection_changed(table.currentRow(), 0, -1, -1)

    # -- セルウィジェットの遅延生成（モジュール冒頭の説明を参照） -----------------------

    def _visible_job_row_range(self):
        """セルウィジェットを用意しておく行の範囲（両端を含む）を返す。
        行が1つも無ければ空の範囲 (0, -1) を返す。"""
        table = self.jobs_section.table
        count = table.rowCount()
        if count == 0:
            return 0, -1
        first = table.rowAt(0)
        if first < 0:  # 先頭が余白（行が画面より少ない等）なら0行目から
            first = 0
        last = table.rowAt(max(0, table.viewport().height() - 1))
        if last < 0:  # 最終行より下が余白なら最後の行まで
            last = count - 1
        return max(0, first - _VISIBLE_ROW_MARGIN), min(count - 1, last + _VISIBLE_ROW_MARGIN)

    def _sync_job_row_widgets(self):
        """可視範囲の行にセルウィジェットを用意し、範囲外の行からは取り外す。"""
        first, last = self._visible_job_row_range()
        wanted = set(range(first, last + 1))
        for row in sorted(self._materialized_rows - wanted):
            self._release_job_row_widgets(row)
        for row in sorted(wanted - self._materialized_rows):
            self._build_job_row_widgets(row)

    def _build_job_row_widgets(self, row):
        """1行分のセルウィジェット（コンボ2つとスピンボックス）を作って載せる。"""
        table = self.jobs_section.table
        job_id = row_id(table, row)
        job = self._job_by_id.get(job_id)
        if job is None:
            return

        wf_combo = make_fk_combo(self._workflow_options, job["workflow_id"])
        wf_combo.currentIndexChanged.connect(
            lambda _idx, jid=job_id: self._on_job_field_changed(jid)
        )
        table.setCellWidget(row, 1, wf_combo)

        ms_combo = make_fk_combo(self._milestone_options, job["default_milestone_id"],
                                  allow_blank=True, blank_label=_BLANK_MILESTONE_LABEL)
        ms_combo.currentIndexChanged.connect(
            lambda _idx, jid=job_id: self._on_job_field_changed(jid)
        )
        table.setCellWidget(row, 2, ms_combo)

        priority_spin = NoWheelSpinBox()
        priority_spin.setRange(1, 999)
        priority_spin.setValue(job["priority"])
        priority_spin.valueChanged.connect(
            lambda _val, jid=job_id: self._on_job_field_changed(jid)
        )
        bind_undo_session(priority_spin, self.db, "ジョブの優先度を変更")
        table.setCellWidget(row, 3, priority_spin)

        # セルウィジェットの下に読み取り専用テキストのitemが残ったままだと、
        # ウィジェットの背景越しに文字が二重に見えてしまう（幅・高さが
        # ぴったり一致しないため隙間から透ける）。ウィジェットを載せた列は
        # itemのテキストを空にしておく（取り外し時に_release_job_row_widgets
        # が改めてテキストを書き戻す）。
        table.blockSignals(True)
        for column in (1, 2, 3):
            item = table.item(row, column)
            if item is not None:
                item.setText("")
        table.blockSignals(False)

        self._materialized_rows.add(row)

    def _release_job_row_widgets(self, row):
        """1行分のセルウィジェットを取り外し、同じ内容をテキスト表示に戻す。"""
        if self._job_row_has_focus(row):
            # 編集中の行は残す。bind_undo_session はフォーカスの出入りでUndo単位を
            # 開閉するため、開いたまま破棄すると単位を閉じられなくなる。
            return
        table = self.jobs_section.table
        table.blockSignals(True)
        for column, text in (
            (1, self._cell_widget_text(row, 1)),
            (2, self._cell_widget_text(row, 2)),
            (3, self._cell_widget_text(row, 3)),
        ):
            if text is not None:
                table.setItem(row, column, _readonly_item(text))
            table.removeCellWidget(row, column)  # ウィジェットはここで破棄される
        table.blockSignals(False)
        self._materialized_rows.discard(row)

    def _cell_widget_text(self, row, column):
        """取り外す直前のセルウィジェットが表示している文字列（無ければNone）。

        DBやキャッシュではなくウィジェット自身から取るのは、編集直後で
        まだ書き戻し前という状態でも、見た目が食い違わないようにするため。"""
        widget = self.jobs_section.table.cellWidget(row, column)
        if widget is None:
            return None
        if isinstance(widget, NoWheelSpinBox):
            return str(widget.value())
        return widget.currentText()

    def _job_row_has_focus(self, row):
        """その行のセルウィジェット（またはその子）が入力フォーカスを持っているか。"""
        focus = QApplication.focusWidget()
        if focus is None:
            return False
        table = self.jobs_section.table
        for column in (1, 2, 3):
            widget = table.cellWidget(row, column)
            if widget is not None and (widget is focus or widget.isAncestorOf(focus)):
                return True
        return False

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

    def _duplicate_job(self, row):
        job_id = row_id(self.jobs_section.table, row)
        new_id = self.db.duplicate_job(job_id)
        self.refresh_jobs(select_id=new_id)

    def _on_job_cell_text_changed(self, item):
        column = item.column()
        if column not in (0, 4):
            return
        job_id = row_id(self.jobs_section.table, item.row())
        if job_id is None:
            return
        if column == 0:
            self._write_job(job_id, name_override=item.text())
        else:
            self._write_job(job_id, tags_override=item.text())

    def _on_job_field_changed(self, job_id):
        self._write_job(job_id)

    def _write_job(self, job_id, name_override=None, tags_override=None):
        table = self.jobs_section.table
        row = self._row_by_job_id.get(job_id)
        job = self._job_by_id.get(job_id)
        if row is None or job is None or row >= table.rowCount():
            return

        name = name_override if name_override is not None else table.item(row, 0).text()
        # セルウィジェットは可視範囲の行にしか無い（モジュール冒頭の説明を参照）。
        # 無い行はユーザーが今その値を編集したわけではないので、直近に読み込んだ
        # 値をそのまま書き戻す。
        workflow_widget = table.cellWidget(row, 1)
        milestone_widget = table.cellWidget(row, 2)
        priority_widget = table.cellWidget(row, 3)
        workflow_id = (workflow_widget.currentData() if workflow_widget is not None
                       else job["workflow_id"])
        milestone_id = (milestone_widget.currentData() if milestone_widget is not None
                        else job["default_milestone_id"])
        priority = priority_widget.value() if priority_widget is not None else job["priority"]
        # タグ列はウィジェットを持たない（常にitemとしてのみ存在する）ため、
        # 名前列と同様にテキストをそのまま読む。
        raw_tags = tags_override if tags_override is not None else table.item(row, 4).text()
        tags = normalize_tags(raw_tags)

        try:
            self.db.update_job(job_id, name, workflow_id, milestone_id, priority, tags)
        except DuplicateNameError as e:
            QMessageBox.warning(self, "変更できません", str(e))
            self.refresh_jobs(select_id=job_id)
            return
        # ユーザーが入力したカンマ区切りの表記ゆれ（空白の有無等）を正規化した
        # 表示へ書き戻す（例外はitemChangedを再度発火させないようblockSignalsする）。
        tags_item = table.item(row, 4)
        if tags_item is not None and tags_item.text() != tags:
            table.blockSignals(True)
            tags_item.setText(tags)
            table.blockSignals(False)
        # 行が可視範囲から外れて再び戻ってきたときに、古い値でウィジェットを
        # 作り直さないようキャッシュも更新しておく。
        self._job_by_id[job_id] = dict(
            job, name=name, workflow_id=workflow_id,
            default_milestone_id=milestone_id, priority=priority, tags=tags,
        )
        if job_id == self.current_job_id:
            self._refresh_overrides()

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
            bind_undo_session(days_spin, self.db, "タスクの日数上書きを変更")
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
            # 既に設定済みの値は、下限を下回っていても必ず選択肢に残す。
            # 外してしまうとコンボが「（既定）」にフォールバックし、DBの実際の
            # 値と違うものを表示したうえ、同じ行の別の欄を触った瞬間に
            # _on_override_changed がその表示値（None）を書き戻して上書きを
            # 無言で消してしまう。整合性が崩れた状態（マイルストーンの締切変更や
            # 依存関係の追加で後から起こりうる）でも、まず現状を正しく見せる。
            current_ms_id = r["override_milestone_id"]
            if current_ms_id is not None and not any(i == current_ms_id for i, _n in milestone_options):
                current_ms = next((m for m in all_milestones if m["id"] == current_ms_id), None)
                if current_ms is not None:
                    milestone_options.insert(0, (current_ms["id"], f'{current_ms["name"]}（要調整）'))
            ms_combo = make_fk_combo(
                milestone_options, r["override_milestone_id"], allow_blank=True,
                blank_label=f"（既定: {default_ms_label}）",
            )
            ms_combo.currentIndexChanged.connect(
                lambda _idx, tid=r["workflow_task_id"]:
                self._on_override_changed(tid, milestone_changed=True)
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

            # 開始固定日（実績確定・外部都合のピン留め）。DefaultAwareSpinBoxと
            # 同じ発想の1列ウィジェット——未設定を表す特殊値（OptionalDateEdit
            # 参照）から日付を選ぶと固定になり、Deleteキーで固定なしに戻せる。
            pin_edit = OptionalDateEdit()
            pin_edit.set_value(r["start_pin_date"])
            pin_edit.dateChanged.connect(
                lambda _date, tid=r["workflow_task_id"]: self._on_override_changed(tid)
            )
            bind_undo_session(pin_edit, self.db, "タスクの開始固定日を変更")
            table.setCellWidget(row, 5, pin_edit)
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

    def _on_override_changed(self, workflow_task_id, milestone_changed=False):
        table = self.override_table
        for row in range(table.rowCount()):
            if row_id(table, row) == workflow_task_id:
                is_active = table.cellWidget(row, 1).isChecked()
                days_val = table.cellWidget(row, 2).value()
                override_days = days_val if days_val > 0 else None
                milestone_id = table.cellWidget(row, 3).currentData()
                team_id = table.cellWidget(row, 4).currentData()
                start_pin_date = table.cellWidget(row, 5).value()

                with self.db.undo_group("タスク上書きを変更"):
                    if (not is_active or override_days is not None or milestone_id is not None
                            or team_id is not None or start_pin_date is not None):
                        self.db.upsert_job_task_override(
                            self.current_job_id, workflow_task_id, is_active=is_active,
                            override_days=override_days, milestone_id=milestone_id, team_id=team_id,
                            start_pin_date=start_pin_date,
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
                # マイルストーンを変えた場合は、後継タスクの選択肢や自動調整の
                # 結果が他の行にも及ぶため、テーブル全体を作り直す（シグナル
                # 発火元セルのウィジェットを直接コールバック内で破棄しないよう
                # 次のイベントループへ遅延させる）。
                #
                # 一方、日数・チーム・有効の変更は他の行に影響しない。ここで
                # 作り直すと、スピンボックスを▲で連続操作している最中に
                # ウィジェットごと差し替わってフォーカスが飛んでしまい、
                # 続けて操作できないうえ、フォーカス単位でまとめている
                # Undoの区切りも途切れてしまう（gui/widgets_common.py の
                # bind_undo_session 参照）。
                if milestone_changed or raised or changed:
                    QTimer.singleShot(0, self._refresh_overrides)
                else:
                    self._ensure_job_selection()
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
        with self.db.undo_group("依存先ジョブを追加"):
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

    # -- Undo/Redo用の選択・フォーカス状態 -------------------------------------------

    def capture_ui_state(self):
        dep_item = self.dep_tree.currentItem()
        return {
            "jobs": capture_table_state(self.jobs_section.table),
            "overrides": capture_table_state(self.override_table),
            "dep_selected": dep_item.data(0, Qt.UserRole) if dep_item else None,
        }

    def restore_ui_state(self, state):
        if not state:
            return
        restore_table_state(self.jobs_section.table, state.get("jobs"))
        restore_table_state(self.override_table, state.get("overrides"))
        dep_data = state.get("dep_selected")
        if dep_data is not None:
            self._select_dep_tree_item(dep_data)

    def _select_dep_tree_item(self, data):
        for i in range(self.dep_tree.topLevelItemCount()):
            top = self.dep_tree.topLevelItem(i)
            if top.data(0, Qt.UserRole) == data:
                self.dep_tree.setCurrentItem(top)
                return
            for j in range(top.childCount()):
                child = top.child(j)
                if child.data(0, Qt.UserRole) == data:
                    self.dep_tree.setCurrentItem(child)
                    return
