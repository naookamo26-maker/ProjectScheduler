"""
タブ3「ジョブ作成」。

左右2分割（QSplitter、既定50/50でユーザーがドラッグ調整可）。
- 左: ジョブ一覧（名前・ワークフロー・既定マイルストーン・優先度・ジョブ タグ）を
  縦全体に表示。上部の「絞り込み」は折りたたみ可能なセクション（既定は
  折りたたみ状態。常時展開だと縦幅を取りすぎるため）で、開くとワークフロー
  ／マイルストーン／ジョブ タグ／タスク タグの4段が現れる（いずれも
  チェックボックスのOR条件で、4つの間はAND条件。一時的に表示件数を絞る
  だけでデータは削除されない。タスク タグは、そのタグを持つタスクを
  1つでも含むジョブを表示する）。ジョブ一覧の「タグ」列（列見出しは冗長さを
  避けて単に「タグ」とする——テーブルの文脈からジョブ タグと分かるため。
  絞り込み側の「ジョブ タグ」「タスク タグ」は同じセクションに並ぶため
  区別が要る）はStretchで残り幅を吸収し、パネル幅にかかわらず横スクロール
  なしで5列すべてが収まるようにしている。「複製」ボタンで選択中のジョブを
  タスク上書き・依存先ジョブごと複製できる（このジョブに依存している側は
  複製先へ引き継がない。db.duplicate_job参照）。ジョブ タグは単純な
  カンマ区切りのテキスト入力（例:「緊急, 顧客A」）で、保存時に前後の
  空白除去・重複排除・「, 」区切りへの正規化を行う（gui/db.py の
  normalize_tags 参照。タスク タグも同じ仕様・同じ正規化関数を使う）。
- 右: 上下2分割（QSplitter）で、選択中ジョブのタスク上書き表と依存先ジョブを
  縦に並べる。
  - タスク上書き表: 選択ジョブが使うワークフローのタスク一覧をそのまま
    自動的に表示し（手入力不要）、既定から外れる項目（無効化・日数上書き・
    マイルストーン上書き・チーム上書き・タスク タグ）だけを編集する。
    既定日数・既定チームは、上書き用のスピンボックス/コンボボックスの
    特殊表示（「既定（n日）」「（既定: 値）」）に統合し、専用の列は持たない。
    タスク タグはジョブ タグ列と同じ、常に編集可能なテキスト入力の列。
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

from datetime import date, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
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

from gui.db import InvalidNameError, ProjectDatabaseError, normalize_name, normalize_tags, parse_tags
from gui.gantt_edit import WorkDayCalendar, format_entity_id
from gui.gantt_generator import build_display
from gui.plan_confirmation import (
    DRAFT,
    JOB_CHANGED,
    JOB_CONFIRMED,
    JOB_PARTIAL,
    JOB_UNCONFIRMED,
    UNCONFIRMED,
    PlanState,
    job_plan_summary,
)
from gui.widgets_common import (
    ChoiceFilterGroup,
    CollapsibleSection,
    CrudSection,
    DefaultAwareSpinBox,
    NoWheelComboBox,
    NoWheelListWidget,
    NoWheelSpinBox,
    OptionalDateEdit,
    apply_with_milestone_repair,
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
from i18n import N_, tr


# セルウィジェットを用意しておく、可視範囲の上下の余分な行数。スクロール
# のたびに作り直す行を減らしつつ、素早くスクロールしてもテキスト表示のまま
# の行が見えないだけの余裕を持たせる。
_VISIBLE_ROW_MARGIN = 12

# 既定マイルストーン未設定の表示（コンボの空欄と、ウィジェットが無い行の
# テキスト表示とで文言を揃える必要があるため定数にしてある）。
_BLANK_MILESTONE_LABEL = N_("（未設定）")
_UNSPECIFIED_PRIORITY_LABEL = N_("（未指定）")

# ジョブ一覧の絞り込み（マイルストーン／ジョブ タグ／タスク タグ）で
# 「該当が無いジョブ」をまとめるための擬似キー。実在のID・タグ文字列と
# 衝突しないよう None を使う（ジョブ タグ・タスク タグの絞り込みは別々の
# ChoiceFilterGroupなので、同じ値を使っても両者のキー空間は混ざらない）。
_NO_MILESTONE_FILTER_KEY = None
_NO_TAG_FILTER_KEY = None

# 進行中・完了（実績の日付で置く状態）。タスク上書き表では、日程に効かない欄
# （進行中・完了の開始固定日、完了の日数・チーム）を編集できなくする
_STARTED = ("in_progress", "done")
_STARTED_LOCK_TIP = N_("進行中・完了のタスクは実績の日付で置くので、この欄は使いません"
                       "（実績の日付は、ガントチャートのタスクの編集ウィンドウで直せます）。")
_STARTED_ACTIVE_LOCK_TIP = N_("進行中・完了のタスクは無効にできません"
                              "（実行しないことにするときは、先に状態を未着手に戻してください）。")
_INACTIVE_STATUS_LOCK_TIP = N_("無効のタスクは進行中・完了にできません（先に有効にしてください）。")
_NO_JOB_TAG_FILTER_LABEL = N_("（ジョブ タグなし）")
_NO_TASK_TAG_FILTER_LABEL = N_("（タスク タグなし）")

# 計画の確定（docs/roadmap.md §8-9）の列。一度も確定していないファイルでは隠す
# （全部が「未確定」になるだけで情報が無いため）。既存の列番号を変えないよう末尾に置く。
_JOB_PLAN_COLUMN = 5
_OVERRIDE_PLAN_COLUMN = 8
_PLAN_CHANGED_COLOR = "#c62828"
_PLAN_QUIET_COLOR = "#8a939c"  # 確定済み（大半の行）は目立たせない
_UNCONFIRMED_BRUSH_COLOR = QColor("#cdd3da")
_JOB_PLAN_LABELS = {
    JOB_CONFIRMED: N_("確定"),
    JOB_CHANGED: N_("変更あり"),
    JOB_PARTIAL: N_("一部未確定"),
    JOB_UNCONFIRMED: N_("未確定"),
}
# 並び替え・絞り込みの順（手を付けるべきものが先）
_JOB_PLAN_ORDER = [JOB_CHANGED, JOB_UNCONFIRMED, JOB_PARTIAL, JOB_CONFIRMED]


def _unconfirmed_brush():
    """確定行の無いものの背景（ガントの未確定のバーと同じ斜線）。"""
    return QBrush(_UNCONFIRMED_BRUSH_COLOR, Qt.BDiagPattern)


def _job_plan_text(kind, changed, unconfirmed):
    if kind == JOB_CHANGED:
        text = tr("変更 {changed}", changed=changed)
        return text + (tr("・未確定 {unconfirmed}", unconfirmed=unconfirmed) if unconfirmed else "")
    if kind == JOB_PARTIAL:
        return tr("一部未確定 {unconfirmed}", unconfirmed=unconfirmed)
    return tr(_JOB_PLAN_LABELS[kind])


def _fmt_confirmed(row, team_names):
    """確定行の表示（開始〜終了・日数・チーム）。終了は含む日で出す。"""
    start = row["start_date"]
    end = (date.fromisoformat(row["end_date"]) - timedelta(days=1)).isoformat()
    if end[:4] == start[:4]:
        end = end[5:]  # 同じ年なら年を省く
    team = team_names.get(row["team_id"], "")
    # 列幅を取りすぎないよう2段にする（1段目: 期間、2段目: 日数・チーム）
    return tr("{start}〜{end}\n{days}日  {team}", start=start, end=end, days=row['days'], team=team).rstrip()


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
        self.setWindowTitle(tr("依存先ジョブを追加"))
        self.resize(360, 420)

        self._existing = {link["depends_on_job_id"] for link in db.list_job_dependency_links(job_id)}
        self._candidates = [j for j in db.list_jobs() if j["id"] != job_id and j["id"] not in self._existing]

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.workflow_filter_combo = NoWheelComboBox()
        self.workflow_filter_combo.addItem(tr("（すべてのワークフロー）"), None)
        for wf in db.list_workflows():
            self.workflow_filter_combo.addItem(wf["name"], wf["id"])
        self.workflow_filter_combo.currentIndexChanged.connect(self._reload_job_list)
        form.addRow(tr("ワークフローで絞り込み"), self.workflow_filter_combo)
        layout.addLayout(form)

        layout.addWidget(QLabel(tr("依存先ジョブ")))
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
            item = QListWidgetItem(tr("{name}（{workflow_name}）", name=j["name"], workflow_name=j["workflow_name"]))
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
        self.setWindowTitle(tr("タスク対応を編集") if initial else tr("タスク対応を追加"))
        form = QFormLayout(self)

        self.dep_task_combo = NoWheelComboBox()
        for t in db.list_workflow_tasks(target_job["workflow_id"]):
            self.dep_task_combo.addItem(t["name"], t["id"])
        form.addRow(tr("依存先の先行タスク（{name}）", name=target_job['name']), self.dep_task_combo)

        self.task_combo = NoWheelComboBox()
        for t in db.list_workflow_tasks(job["workflow_id"]):
            self.task_combo.addItem(t["name"], t["id"])
        form.addRow(tr("本ジョブの後続タスク（{name}）", name=job['name']), self.task_combo)

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
    3: lambda j: (j["priority"] is None, j["priority"] or 0),
    4: lambda j: j["tags"],
    _JOB_PLAN_COLUMN: lambda j: j.get("_plan_order", 0),
}


class JobsTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_job_id = None
        # 開始固定日を編集したタスク（入力を終えたときに休業日かを見る。_snap_pin_to_working_day）
        self._pins_edited = set()
        self._sort_column = 0
        self._sort_ascending = True
        # セルウィジェットの遅延生成用（モジュール冒頭の説明を参照）
        self._materialized_rows = set()   # 実体化済みの行番号
        self._job_by_id = {}              # job_id -> 直近に読み込んだジョブの内容
        self._row_by_job_id = {}          # job_id -> 行番号
        self._workflow_options = []       # [(id, 表示名), ...]
        self._milestone_options = []

        layout = QVBoxLayout(self)

        # ワークフロー／マイルストーン／ジョブ タグ／タスク タグの4つの絞り込み
        # （いずれもOR条件のチェックボックス一覧で、4つの間はAND条件で
        # 組み合わせる）。常時展開だと縦幅を取りすぎるため、まとめて1つの
        # 折りたたみセクションに収める。
        self.filters_section = CollapsibleSection(tr("絞り込み"))
        layout.addWidget(self.filters_section)

        self.workflow_filter = ChoiceFilterGroup(tr("ワークフロー"))
        self.workflow_filter.changed.connect(lambda: self.refresh_jobs(select_id=self.current_job_id))
        self.filters_section.content_layout.addWidget(self.workflow_filter)

        self.milestone_filter = ChoiceFilterGroup(tr("マイルストーン"))
        self.milestone_filter.changed.connect(lambda: self.refresh_jobs(select_id=self.current_job_id))
        self.filters_section.content_layout.addWidget(self.milestone_filter)

        self.tag_filter = ChoiceFilterGroup(tr("ジョブ タグ"))
        self.tag_filter.changed.connect(lambda: self.refresh_jobs(select_id=self.current_job_id))
        self.filters_section.content_layout.addWidget(self.tag_filter)

        # タスク タグ（job_task_overrides.tags）での絞り込み。そのタグを持つ
        # タスクを1つでも含むジョブを表示する（gui/db.pyのlist_all_job_task_overrides
        # から全ジョブ分をまとめて引き、job_id単位のタグ集合に潰して使う。
        # _task_tag_map参照）。
        self.task_tag_filter = ChoiceFilterGroup(tr("タスク タグ"))
        self.task_tag_filter.changed.connect(lambda: self.refresh_jobs(select_id=self.current_job_id))
        self.filters_section.content_layout.addWidget(self.task_tag_filter)

        # 確定状態での絞り込み（確定後に足したジョブ・変更のあるジョブを探す）。
        # 一度も確定していないファイルでは隠す
        self.plan_filter = ChoiceFilterGroup(tr("確定状態"))
        self.plan_filter.rebuild([(k, tr(_JOB_PLAN_LABELS[k])) for k in _JOB_PLAN_ORDER])
        self.plan_filter.changed.connect(lambda: self.refresh_jobs(select_id=self.current_job_id))
        self.filters_section.content_layout.addWidget(self.plan_filter)
        self._plan_state = None

        self.jobs_section = CrudSection(
            tr("ジョブ"), [tr("ジョブ名"), tr("ワークフロー"), tr("既定マイルストーン"), tr("優先度"), tr("タグ"), tr("確定")],
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
        # 「タグ」列（ジョブ タグ。内容の長さが最も変動する）に残り幅を
        # 吸収させ、パネル幅にかかわらず横スクロールなしで5列すべてが
        # 収まるようにする。
        jobs_header.setSectionResizeMode(4, QHeaderView.Stretch)
        # 「確定」列は末尾の列番号のまま、見た目だけジョブ名の隣へ出す（右端だと
        # 横スクロールしないと見えないため）
        jobs_header.moveSection(jobs_header.visualIndex(_JOB_PLAN_COLUMN), 1)
        # スクロールや表示領域の変化に追従して、見えている行にだけ
        # セルウィジェットを用意する。
        jobs_scrollbar = self.jobs_section.table.verticalScrollBar()
        jobs_scrollbar.valueChanged.connect(self._sync_job_row_widgets)
        jobs_scrollbar.rangeChanged.connect(lambda _min, _max: self._sync_job_row_widgets())

        override_group = QGroupBox(tr("タスク上書き（選択中のジョブ）"))
        override_layout = QVBoxLayout(override_group)

        self.override_table = QTableWidget(0, 9)
        self.override_table.setHorizontalHeaderLabels(
            [tr("タスク名"), tr("有効"), tr("日数"), tr("マイルストーン"), tr("チーム"), tr("開始固定日"), tr("状態"), tr("タグ"),
             tr("確定日程")]
        )
        self.override_table.verticalHeader().setVisible(False)
        # 「確定日程」列も同様に、見た目だけタスク名の隣へ出す
        override_header = self.override_table.horizontalHeader()
        override_header.moveSection(override_header.visualIndex(_OVERRIDE_PLAN_COLUMN), 1)
        self.override_table.setSelectionMode(QTableWidget.NoSelection)
        # タスク タグ列は、ジョブ タグ列（ジョブ一覧）と同じくカンマ区切りの
        # テキスト入力（専用ウィジェットを持たない、常に編集可能なitem）にする。
        self.override_table.itemChanged.connect(self._on_override_tag_text_changed)
        override_layout.addWidget(self.override_table)

        dep_group = QGroupBox(tr("依存先ジョブ（展開してタスク単位の対応を確認・編集）"))
        dep_group_layout = QVBoxLayout(dep_group)

        dep_toolbar = QHBoxLayout()
        add_link_btn = QPushButton(tr("＋ 依存先ジョブ"))
        add_link_btn.clicked.connect(self._add_dependency_link)
        del_link_btn = QPushButton(tr("－ 依存先ジョブ"))
        del_link_btn.clicked.connect(self._delete_selected_dependency_link)
        dep_toolbar.addWidget(add_link_btn)
        dep_toolbar.addWidget(del_link_btn)
        dep_toolbar.addSpacing(16)
        add_pair_btn = QPushButton(tr("＋ タスク対応"))
        add_pair_btn.clicked.connect(self._add_selected_task_pair)
        del_pair_btn = QPushButton(tr("－ タスク対応"))
        del_pair_btn.clicked.connect(self._delete_selected_task_pair)
        dep_toolbar.addWidget(add_pair_btn)
        dep_toolbar.addWidget(del_pair_btn)
        dep_toolbar.addStretch(1)
        dep_group_layout.addLayout(dep_toolbar)

        self.dep_tree = QTreeWidget()
        self.dep_tree.setColumnCount(3)
        self.dep_tree.setHeaderLabels([tr("依存先ジョブ ／ タスク対応（先行→後続）"), tr("有効"), tr("種別")])
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

    # -- ワークフロー／マイルストーン／ジョブ タグ／タスク タグの絞り込み ---------------------

    def _job_tag_keys(self, job):
        """絞り込み判定に使う、ジョブが持つジョブ タグのキー集合。ジョブ タグが
        1つも無いジョブは擬似キー _NO_TAG_FILTER_KEY（＝「（ジョブ タグなし）」）
        を持つ扱いにする。"""
        tags = parse_tags(job["tags"])
        return set(tags) if tags else {_NO_TAG_FILTER_KEY}

    def _task_tag_map(self):
        """job_id -> そのジョブの全タスクが持つタスク タグの集合。
        db.list_all_job_task_overrides() で全ジョブ分を1回のクエリでまとめて
        引き、job_id単位のタグ集合に潰す（タスク タグの絞り込みはジョブ単位で
        「いずれかのタスクがそのタグを持つか」を見るため、タスク側の内訳は
        絞り込みでは使わない）。"""
        mapping = {}
        for o in self.db.list_all_job_task_overrides():
            tags = parse_tags(o["tags"])
            if tags:
                mapping.setdefault(o["job_id"], set()).update(tags)
        return mapping

    def _job_task_tag_keys(self, job_id, task_tag_map):
        """絞り込み判定に使う、ジョブが持つタスク タグのキー集合。タスク タグを
        持つタスクが1つも無いジョブは擬似キー _NO_TAG_FILTER_KEY
        （＝「（タスク タグなし）」）を持つ扱いにする。"""
        tags = task_tag_map.get(job_id)
        return set(tags) if tags else {_NO_TAG_FILTER_KEY}

    def _rebuild_filters(self, jobs, task_tag_map):
        """ワークフロー／マイルストーン／ジョブ タグ／タスク タグの絞り込み用
        チェックボックスを、現在のDBの内容（渡された絞り込み前のジョブ一覧）に
        合わせて再構築する。既存のチェック状態はキーで可能な限り維持し、
        新規キーは既定で表示にする。"""
        self.workflow_filter.rebuild([(w["id"], w["name"]) for w in self.db.list_workflows()])
        self.milestone_filter.rebuild(
            [(m["id"], m["name"]) for m in self.db.list_milestones()]
            + [(_NO_MILESTONE_FILTER_KEY, tr(_BLANK_MILESTONE_LABEL))]
        )
        self._rebuild_tag_filter(jobs)
        self._rebuild_task_tag_filter(jobs, task_tag_map)

    def _rebuild_tag_filter(self, jobs):
        """「ジョブ タグ」の絞り込みチェックボックスだけを再構築する。ジョブ タグは
        ジョブ一覧の中で自由記述のため、インライン編集のたびにこれだけを
        呼び直して、タブを切り替えなくても絞り込みの選択肢に反映されるように
        する（_write_job参照）。"""
        tag_keys = set()
        for job in jobs:
            tag_keys.update(self._job_tag_keys(job))
        has_no_tag = _NO_TAG_FILTER_KEY in tag_keys
        tag_keys.discard(_NO_TAG_FILTER_KEY)
        tag_items = [(tag, tag) for tag in sorted(tag_keys)]
        if has_no_tag:
            tag_items.append((_NO_TAG_FILTER_KEY, tr(_NO_JOB_TAG_FILTER_LABEL)))
        self.tag_filter.rebuild(tag_items)

    def _rebuild_task_tag_filter(self, jobs, task_tag_map):
        """「タスク タグ」の絞り込みチェックボックスだけを再構築する
        （_rebuild_tag_filterと同じ理由。_on_override_changed参照）。"""
        task_tag_keys = set()
        for job in jobs:
            task_tag_keys.update(self._job_task_tag_keys(job["id"], task_tag_map))
        has_no_task_tag = _NO_TAG_FILTER_KEY in task_tag_keys
        task_tag_keys.discard(_NO_TAG_FILTER_KEY)
        task_tag_items = [(tag, tag) for tag in sorted(task_tag_keys)]
        if has_no_task_tag:
            task_tag_items.append((_NO_TAG_FILTER_KEY, tr(_NO_TASK_TAG_FILTER_LABEL)))
        self.task_tag_filter.rebuild(task_tag_items)

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
        task_tag_map = self._task_tag_map()
        self._rebuild_filters(all_jobs, task_tag_map)
        visible_workflow_ids = self.workflow_filter.visible_keys()
        visible_milestone_keys = self.milestone_filter.visible_keys()
        visible_tag_keys = self.tag_filter.visible_keys()
        visible_task_tag_keys = self.task_tag_filter.visible_keys()
        self._plan_state = PlanState(self.db)
        plan_on = self._plan_state.status != UNCONFIRMED
        self.plan_filter.setVisible(plan_on)
        plan_summary = job_plan_summary(self._plan_state) if plan_on else {}
        visible_plan_keys = self.plan_filter.visible_keys()
        for job in all_jobs:
            kind = plan_summary.get(job["id"], (JOB_CONFIRMED, 0, 0))[0]
            job["_plan"] = plan_summary.get(job["id"], (JOB_CONFIRMED, 0, 0))
            job["_plan_order"] = _JOB_PLAN_ORDER.index(kind)

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
            and self._job_task_tag_keys(job["id"], task_tag_map) & visible_task_tag_keys
            and (not plan_on or job["_plan"][0] in visible_plan_keys)
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
                milestone_names.get(job["default_milestone_id"], tr(_BLANK_MILESTONE_LABEL))))
            table.setItem(row, 3, _readonly_item(
                str(job["priority"]) if job["priority"] is not None else tr(_UNSPECIFIED_PRIORITY_LABEL)))
            # ジョブ タグはシンプルなテキスト入力（カンマ区切り）のため、ワーク
            # フロー／マイルストーン／優先度と違って専用ウィジェットを持たず、
            # 常に編集可能なitemとして表示する（ジョブ名列と同じ扱い）。
            table.setItem(row, 4, QTableWidgetItem(job["tags"]))
            table.setItem(row, _JOB_PLAN_COLUMN, self._job_plan_item(job["_plan"]))
            if job["id"] == select_id:
                select_row = row
        table.blockSignals(False)
        table.setColumnHidden(_JOB_PLAN_COLUMN, not plan_on)

        auto_size_columns(table)
        self._fit_job_plan_column()
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
                                  allow_blank=True, blank_label=tr(_BLANK_MILESTONE_LABEL))
        ms_combo.currentIndexChanged.connect(
            lambda _idx, jid=job_id: self._on_job_field_changed(jid)
        )
        table.setCellWidget(row, 2, ms_combo)

        priority_spin = NoWheelSpinBox()
        priority_spin.setRange(0, 999)
        priority_spin.setSpecialValueText(tr(_UNSPECIFIED_PRIORITY_LABEL))
        priority_spin.setValue(job["priority"] if job["priority"] is not None else 0)
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
                self, tr("ワークフロー未登録"),
                tr("先に「ワークフロー設計」タブでワークフローを1つ以上作成してください。"),
            )
            return
        existing = {j["name"] for j in self.db.list_jobs()}
        name = unique_default_name(existing, tr("新しいジョブ"))
        workflow_id = self.db.list_workflows()[0]["id"]
        try:
            new_id = self.db.add_job(name, workflow_id, None, None)
        except InvalidNameError as e:
            QMessageBox.warning(self, tr("追加できません"), str(e))
            return
        self.refresh_jobs(select_id=new_id)

    def _delete_job(self, row):
        job_id = row_id(self.jobs_section.table, row)
        count = self.db.job_usage_count(job_id)
        if not confirm_or_block_delete(self, count, tr("このジョブ"), hard_block=False):
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
        if priority_widget is not None:
            raw_priority = priority_widget.value()
            priority = None if raw_priority == 0 else raw_priority
        else:
            priority = job["priority"]
        # ジョブ タグ列はウィジェットを持たない（常にitemとしてのみ存在する）
        # ため、名前列と同様にテキストをそのまま読む。
        raw_tags = tags_override if tags_override is not None else table.item(row, 4).text()
        tags = normalize_tags(raw_tags)

        def change():
            self.db.update_job(job_id, name, workflow_id, milestone_id, priority, tags)

        workflow_changed = workflow_id != job["workflow_id"]
        try:
            name = normalize_name(name)
            if workflow_changed or milestone_id != job["default_milestone_id"]:
                # 既定マイルストーン・ワークフローの変更は、タスク上書きを触らずに
                # 「先行タスクの締切 <= 後続タスクの締切」を崩しうる（上書きの無い
                # タスクの締切が変わる／元に戻したワークフローの古い上書きが効き
                # 直す）。締切日の変更と同じく、確認して再調整する。
                applied = apply_with_milestone_repair(
                    self.db, self, tr("ジョブの変更"),
                    tr("ジョブ「{name}」を変更", name=name), change,
                )
            else:
                change()
                applied = True
        except InvalidNameError as e:
            QMessageBox.warning(self, tr("変更できません"), str(e))
            self.refresh_jobs(select_id=job_id)
            return
        if not applied:
            # 取り消したので、コンボ等の表示を元の値に戻す
            self.refresh_jobs(select_id=job_id)
            return
        # 名前の前後の空白は落として保存しているので、表示もそろえる
        name_item = table.item(row, 0)
        if name_item is not None and name_item.text() != name:
            table.blockSignals(True)
            name_item.setText(name)
            table.blockSignals(False)
        # タグを変えた場合、絞り込みの選択肢（タグ一覧のチェックボックス）に
        # タブを切り替えなくても反映されるよう、その場で作り直す。
        if tags != job["tags"]:
            self._rebuild_tag_filter(self.db.list_jobs())
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
            if workflow_changed:
                # ワークフローを変えると、依存先ジョブのタスク対応が作り直される
                # （テンプレート由来の分は新しい組み合わせで展開し直し、旧ワーク
                # フローのタスクを指す手動の分は消える。gui/db.py の update_job）
                self._refresh_dependencies()

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
        # 「確定日程」列の表示・非表示は行が無くても合わせる（未確定のプロジェクトで、
        # ジョブを選ぶ前にこの列の見出しだけが出ていた）
        self._update_override_plan_marks()

    def _refresh_overrides(self):
        table = self.override_table
        table.blockSignals(True)
        table.setRowCount(0)
        all_milestones = self.db.list_milestones()
        team_options = [(t["id"], t["name"]) for t in self.db.list_teams()]

        if self.current_job_id is None:
            table.blockSignals(False)
            self._update_override_plan_marks()  # 行が無くても列の表示を合わせる
            return
        job = next(j for j in self.db.list_jobs() if j["id"] == self.current_job_id)
        default_ms_label = job["milestone_name"] or tr("未設定")

        tasks = self.db.list_job_tasks_with_overrides(self.current_job_id)
        self._override_default_team = {r["workflow_task_id"]: r["default_team_id"] for r in tasks}
        for r in tasks:
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
            days_spin.setSuffix(tr("日"))
            days_spin.setSpecialValueText(tr("既定（{default_days}日）", default_days=r['default_days']))
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
                    milestone_options.insert(0, (current_ms["id"], tr("{name}（要調整）", name=current_ms["name"])))
            ms_combo = make_fk_combo(
                milestone_options, r["override_milestone_id"], allow_blank=True,
                blank_label=tr("（既定: {default_ms_label}）", default_ms_label=default_ms_label),
            )
            ms_combo.currentIndexChanged.connect(
                lambda _idx, tid=r["workflow_task_id"]:
                self._on_override_changed(tid, milestone_changed=True)
            )
            table.setCellWidget(row, 3, ms_combo)

            # 既定チームも同様に、専用列ではなくコンボの未選択時ラベルに埋め込む。
            team_combo = make_fk_combo(
                team_options, r["override_team_id"], allow_blank=True,
                blank_label=tr("（既定: {default_team_name}）", default_team_name=r['default_team_name']),
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
                lambda _date, tid=r["workflow_task_id"]: self._on_pin_changed(tid)
            )
            # 入力を終えたとき（フォーカスが外れる直前。同じUndo単位の中）に、休業日なら
            # 次の稼働日へ直す。入力中に直すと、日付を打っている途中で書き換わってしまう
            bind_undo_session(
                pin_edit, self.db, "タスクの開始固定日を変更",
                on_before_commit=lambda tid=r["workflow_task_id"]: self._snap_pin_to_working_day(tid),
            )
            table.setCellWidget(row, 5, pin_edit)

            # 実際の進捗（ユーザーが手動で記録する。日付からの推測ではない
            # ——プロジェクト分析タブの「タスクの状態」はこの値を集計する。
            # gui/db.py の job_task_overrides.status: NULL＝未着手）。
            status_combo = make_fk_combo(
                [("in_progress", tr("進行中")), ("done", tr("完了"))], r["status"],
                allow_blank=True, blank_label=tr("未着手"),
            )
            status_combo.currentIndexChanged.connect(
                lambda _idx, tid=r["workflow_task_id"]: self._on_override_changed(tid)
            )
            table.setCellWidget(row, 6, status_combo)

            # タスク タグ。ジョブ タグ列（ジョブ一覧）と同様、専用ウィジェットを
            # 持たない常に編集可能なitemとして表示する（itemChangedは
            # __init__で_on_override_tag_text_changedに一括で繋いである）。
            table.setItem(row, 7, QTableWidgetItem(r["tags"]))
            table.setItem(row, _OVERRIDE_PLAN_COLUMN, _readonly_item(""))
        table.blockSignals(False)
        # 「タグ」列（最後の列）はジョブ一覧と同様、内容幅に関わらず表の
        # 右側に残る余白をすべて使う。
        auto_size_columns(table, min_width=50, stretch_last=True)
        table.setColumnWidth(1, 44)  # 「有効」列はチェックボックスのみなので詰める
        self._update_override_plan_marks()
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

    def _on_override_tag_text_changed(self, item):
        """タスク上書き表の「タグ」列（7列目、タスク タグ）の編集完了時に
        呼ばれる。他の列（チェックボックス・コンボ・スピンボックス等）は
        セルウィジェットのシグナルで直接 _on_override_changed に繋いでいるが、
        タスク タグ列はジョブ一覧の「タグ」列と同じくウィジェットを持たない
        常時編集可能なitemのため、テーブル全体のitemChangedを購読してここで
        列を判定する。"""
        if item.column() != 7:
            return
        workflow_task_id = row_id(self.override_table, item.row())
        if workflow_task_id is None:
            return
        self._on_override_changed(workflow_task_id)

    def _on_pin_changed(self, workflow_task_id):
        self._pins_edited.add(workflow_task_id)
        self._on_override_changed(workflow_task_id)

    def _snap_pin_to_working_day(self, workflow_task_id):
        """開始固定日を編集し終えたとき、休業日なら次の稼働日へ直して知らせる。

        スケジューラも休業日の固定日は次の稼働日から始めるので、欄の値とガントの
        バーの位置をそろえる（以前は何も言わずに翌稼働日から始まっていた）。"""
        if workflow_task_id not in self._pins_edited or self.current_job_id is None:
            return
        self._pins_edited.discard(workflow_task_id)
        row = next((r for r in self.db.list_job_tasks_with_overrides(self.current_job_id)
                    if r["workflow_task_id"] == workflow_task_id), None)
        if row is None or not row["start_pin_date"] or row["status"] in _STARTED:
            return
        day = date.fromisoformat(row["start_pin_date"])
        team = row["override_team_id"] or row["default_team_id"]
        calendar = WorkDayCalendar.from_display(build_display(self.db))
        working = calendar.next_working(day, format_entity_id("TEAM", team))
        if working == day:
            return
        self.db.update_job_task_override_fields(
            self.current_job_id, workflow_task_id, start_pin_date=working.isoformat()
        )
        table = self.override_table
        for r in range(table.rowCount()):
            if row_id(table, r) == workflow_task_id:
                edit = table.cellWidget(r, 5)
                edit.blockSignals(True)
                edit.set_value(working.isoformat())
                edit.blockSignals(False)
        message = tr("{day} は休業日のため、次の稼働日 {working} を開始固定日にしました。", day=day, working=working)
        # フォーカスが移る途中なので、ダイアログは今のイベント処理の後に出す
        QTimer.singleShot(0, lambda: QMessageBox.information(self, tr("開始固定日"), message))

    def _apply_started_locks(self):
        """状態と食い違う欄を編集できなくする。

        - 進行中・完了のタスク: 日程に効かない欄（進行中・完了は開始固定日、完了は
          日数・チームも）。書いても実績の日付で置くので何も起きず、未着手に戻した瞬間に
          まとめて効いてタスクが動いていたため
        - 進行中・完了で有効なタスク: 「有効」（無効にできない）。実行中・実行済みの
          タスクを「実行しない」にするのは矛盾する（先に未着手に戻す）
        - 無効で未着手のタスク: 「状態」（進行中・完了にできない。先に有効にする）
        既に食い違っている行（無効なのに進行中等）は、有効に戻す・未着手に戻すことは
        できるようにしておく（身動きが取れなくならないように）。"""
        table = self.override_table
        for row in range(table.rowCount()):
            status_combo = table.cellWidget(row, 6)
            active_check = table.cellWidget(row, 1)
            if status_combo is None or active_check is None:
                continue
            status = status_combo.currentData()
            active = active_check.isChecked()
            for column, locked, tip in (
                (5, status in _STARTED, _STARTED_LOCK_TIP),
                (2, status == "done", _STARTED_LOCK_TIP),
                (4, status == "done", _STARTED_LOCK_TIP),
                (1, status in _STARTED and active, _STARTED_ACTIVE_LOCK_TIP),
                (6, not active and status is None, _INACTIVE_STATUS_LOCK_TIP),
            ):
                widget = table.cellWidget(row, column)
                tip = tr(tip)
                widget.setEnabled(not locked)
                if locked:
                    if widget.toolTip() != tip:
                        widget.setProperty("unlockedToolTip", widget.toolTip())
                        widget.setToolTip(tip)
                elif widget.toolTip() == tip:
                    widget.setToolTip(widget.property("unlockedToolTip") or "")

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
                status = table.cellWidget(row, 6).currentData()
                # タスク タグ列はジョブ タグ列と同様にウィジェットを持たない
                # ため、名前列と同じくitemのテキストをそのまま読む。
                tags_item = table.item(row, 7)
                tags = normalize_tags(tags_item.text() if tags_item is not None else "")

                with self.db.undo_group("タスク上書きを変更"):
                    if (not is_active or override_days is not None or milestone_id is not None
                            or team_id is not None or start_pin_date is not None or tags
                            or status is not None):
                        self.db.upsert_job_task_override(
                            self.current_job_id, workflow_task_id, is_active=is_active,
                            override_days=override_days, milestone_id=milestone_id, team_id=team_id,
                            start_pin_date=start_pin_date, tags=tags, status=status,
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
                # ユーザーが入力したカンマ区切りの表記ゆれ（空白の有無等）を
                # 正規化した表示へ書き戻す（ジョブ タグ列のtags_overrideと同じ
                # 考え方）。milestone_changed分岐でテーブルを作り直す場合も
                # 二度手間にはならず、作り直さない分岐（日数・チーム・有効・
                # タグのみの変更）では必須になる。
                if tags_item is not None and tags_item.text() != tags:
                    table.blockSignals(True)
                    tags_item.setText(tags)
                    table.blockSignals(False)
                # タグを変えた場合、絞り込みの選択肢（タグ一覧のチェックボックス）に
                # タブを切り替えなくても反映されるよう、その場で作り直す
                # （_write_job の同名の対処と同じ理由）。
                self._rebuild_task_tag_filter(self.db.list_jobs(), self._task_tag_map())
                if raised or changed:
                    messages = []
                    if raised:
                        messages.append(
                            tr("このタスクのマイルストーンが先行タスクより早かったため、"
                            "先行タスクに合わせて自動的に引き上げました。")
                        )
                    if changed:
                        messages.append(
                            tr("後継タスク{n_changed}件のマイルストーンが、変更後のマイルストーンより早かったため、整合性を保つよう自動的に合わせました。", n_changed=len(changed))
                        )
                    QMessageBox.information(self, tr("マイルストーンを自動調整しました"), "\n".join(messages))
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
                    self._refresh_plan_marks()
                    self._ensure_job_selection()
                return

    # -- 計画の確定の表示（docs/roadmap.md §8-9） ------------------------------------

    def _job_plan_item(self, plan):
        kind, changed, unconfirmed = plan
        item = _readonly_item(_job_plan_text(kind, changed, unconfirmed))
        if kind == JOB_UNCONFIRMED:
            item.setBackground(_unconfirmed_brush())
        elif kind == JOB_CHANGED:
            item.setForeground(QColor(_PLAN_CHANGED_COLOR))
        elif kind == JOB_CONFIRMED:
            item.setForeground(QColor(_PLAN_QUIET_COLOR))
        return item

    def _fit_job_plan_column(self):
        """「確定」列は中身（「確定」「変更 2」等の短い文字）に合わせて詰める
        （他の列と同じ最小幅だと、ほぼ全行が「確定」なのに幅を取りすぎるため）。"""
        table = self.jobs_section.table
        table.resizeColumnToContents(_JOB_PLAN_COLUMN)
        table.setColumnWidth(_JOB_PLAN_COLUMN, max(table.columnWidth(_JOB_PLAN_COLUMN), 44))

    def _refresh_plan_marks(self):
        """編集の後、表を作り直さずに確定の表示だけを更新する（スピンボックスの
        連続操作中にウィジェットを差し替えないため。_on_override_changed 参照）。"""
        self._plan_state = PlanState(self.db)
        if self._plan_state.status != UNCONFIRMED:
            summary = job_plan_summary(self._plan_state)
            table = self.jobs_section.table
            table.blockSignals(True)
            for job_id, row in self._row_by_job_id.items():
                table.setItem(row, _JOB_PLAN_COLUMN,
                              self._job_plan_item(summary.get(job_id, (JOB_CONFIRMED, 0, 0))))
            table.blockSignals(False)
            self._fit_job_plan_column()
        self._update_override_plan_marks()

    def _update_override_plan_marks(self):
        """タスク上書き表の「確定日程」列と、確定時から変わった日数・チームの赤文字。
        あわせて、進行中・完了のタスクで使わない欄を編集できなくする（_apply_started_locks。
        赤文字の説明より後に当てる）。"""
        try:
            self._update_override_plan_marks_inner()
        finally:
            self._apply_started_locks()

    def _update_override_plan_marks_inner(self):
        table = self.override_table
        state = self._plan_state if self._plan_state is not None else PlanState(self.db)
        plan_on = state.status != UNCONFIRMED
        table.setColumnHidden(_OVERRIDE_PLAN_COLUMN, not plan_on)
        if not plan_on or self.current_job_id is None:
            return
        team_names = {t["id"]: t["name"] for t in self.db.list_teams()}
        red = f"color: {_PLAN_CHANGED_COLOR};"
        table.blockSignals(True)
        for row in range(table.rowCount()):
            key = (self.current_job_id, row_id(table, row))
            confirmed = state.confirmed.get(key)
            item = table.item(row, _OVERRIDE_PLAN_COLUMN)
            days_spin = table.cellWidget(row, 2)
            team_combo = table.cellWidget(row, 4)
            for widget in (days_spin, team_combo):
                widget.setStyleSheet("")
                widget.setToolTip("")
            item.setForeground(QBrush())
            item.setBackground(QBrush())
            item.setToolTip("")
            if confirmed is None:
                item.setText(tr("未確定"))
                item.setBackground(_unconfirmed_brush())
                continue
            item.setText(_fmt_confirmed(confirmed, team_names))
            # 2段を今の行の高さ（入力欄の高さ）に収めるため、この列だけ文字を小さくする
            font = item.font()
            font.setPointSizeF(max(table.font().pointSizeF() - 2, 6))
            item.setFont(font)
            if state.status != DRAFT or key not in state.changed:
                continue
            # 変更案: 確定時から変わった入力を赤文字にし、確定値と今の値を並べる。
            # 編集は禁止しない
            reasons = []
            days_now = days_spin.value() or days_spin.default_value
            if days_now != confirmed["days"]:
                tip = tr("確定: {days}日 → 変更案: {days_now}日", days=confirmed['days'], days_now=days_now)
                days_spin.setStyleSheet(red)
                days_spin.setToolTip(tip)
                reasons.append(tip)
            team_now = team_combo.currentData() or self._default_team_id(row)
            if confirmed["team_id"] is not None and team_now != confirmed["team_id"]:
                tip = (tr(
                    "確定: {before} → 変更案: {after}",
                    before=team_names.get(confirmed["team_id"], ""), after=team_names.get(team_now, ""),
                ))
                team_combo.setStyleSheet(red)
                team_combo.setToolTip(tip)
                reasons.append(tip)
            if not table.cellWidget(row, 1).isChecked():
                reasons.append(tr("確定後に無効にしました"))
            if key in state.draft_moves:
                reasons.append(tr("ガントで移動: 開始 {start_date} → {value}", start_date=confirmed['start_date'], value=state.draft_moves[key]))
            fact = state.facts.get(key)
            if fact is not None and fact["start_date"] != confirmed["start_date"]:
                reasons.append(tr("実績の開始日: {start_date} → {value}", start_date=confirmed['start_date'], value=fact["start_date"]))
            elif fact is not None and key not in state.in_progress and fact["end_date"] != confirmed["end_date"]:
                reasons.append(tr("実績の終了日が確定と違います"))
            if not reasons:
                reasons.append(tr("確定後に開始固定日・依存などが変わりました"))
            item.setForeground(QColor(_PLAN_CHANGED_COLOR))
            item.setToolTip("\n".join(reasons))
        table.blockSignals(False)
        table.resizeColumnToContents(_OVERRIDE_PLAN_COLUMN)

    def _default_team_id(self, row):
        """その行のタスクの既定チーム（上書きしていないときの実効チーム）。"""
        return self._override_default_team.get(row_id(self.override_table, row))

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
                    tree.setItemWidget(child, 2, QLabel(tr("自動") if is_auto else tr("手動")))
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
            QMessageBox.information(self, tr("ジョブ未選択"), tr("先にジョブを選択してください。"))
            return
        others = [j for j in self.db.list_jobs() if j["id"] != self.current_job_id]
        if not others:
            QMessageBox.information(self, tr("依存先ジョブがありません"), tr("他のジョブを先に作成してください。"))
            return
        dialog = JobDependencyLinkDialog(self.db, self.current_job_id, self)
        if dialog.exec() != QDialog.Accepted:
            return
        depends_on_job_ids = dialog.values()
        if not depends_on_job_ids:
            QMessageBox.warning(self, tr("入力エラー"), tr("依存先ジョブを1つ以上選択してください。"))
            return
        errors = []
        with self.db.undo_group("依存先ジョブを追加"):
            for depends_on_job_id in depends_on_job_ids:
                try:
                    self.db.add_job_dependency_link(self.current_job_id, depends_on_job_id)
                except ProjectDatabaseError as e:
                    errors.append(str(e))
        if errors:
            QMessageBox.warning(self, tr("一部追加できませんでした"), "\n".join(errors))
        self._refresh_dependencies()

    def _delete_selected_dependency_link(self):
        _item, data = self._selected_link()
        if data is None:
            QMessageBox.information(self, tr("依存先ジョブ未選択"), tr("削除する依存先ジョブを選択してください。"))
            return
        self.db.delete_job_dependency_link(data["link_id"])
        self._refresh_dependencies()

    def _add_selected_task_pair(self):
        _item, data = self._selected_link()
        if data is None:
            QMessageBox.information(
                self, tr("依存先ジョブ未選択"), tr("タスク対応を追加する依存先ジョブを選択してください。")
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
            QMessageBox.warning(self, tr("入力エラー"), tr("すべての項目を選択してください。"))
            return
        try:
            self.db.add_external_dependency(self.current_job_id, task_id, target_job_id, dep_task_id)
        except ProjectDatabaseError as e:
            QMessageBox.warning(self, tr("追加できません"), str(e))
            return
        self._refresh_dependencies()

    def _delete_selected_task_pair(self):
        item = self.dep_tree.currentItem()
        data = item.data(0, Qt.UserRole) if item is not None else None
        if data is None or data["kind"] != "pair":
            QMessageBox.information(
                self, tr("タスク対応未選択"), tr("削除するタスク対応（依存先ジョブの子項目）を選択してください。")
            )
            return
        if data["auto"]:
            QMessageBox.information(
                self, tr("削除できません"),
                tr("自動生成された対応です。不要な場合は「有効」のチェックを外してください。"),
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
