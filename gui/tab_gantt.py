"""
タブ4「ガントチャート」（段階2: QGraphicsSceneによる独自バーチャート描画）。

メニューの「ガントチャートを生成」（HTMLファイル出力）とは別に、
DBの現在の設定のまま素早くスケジューリング結果を確認するためのタブ。
このタブに切り替えるたびに自動的にスケジューリングを実行し直し（refresh_choices、
gui/main.py の _on_tab_changed から呼ばれる）、全ワークフロー・全チームの
ジョブをまとめてジョブ単位の行で表示する（gui/gantt_view.py 参照。
gui/node_canvas.py と同じQGraphicsView/QGraphicsSceneベースで、ホイール
ズーム・中ボタンパン対応）。

表示は常に全ジョブが対象で、バーの色はチーム別に塗り分ける。どのワークフロー
のジョブかは、左列のジョブ名の左に置く色スペースで見分ける（gui/gantt_view.py
の_JOB_SWATCH_WIDTH参照）。上部の「絞り込み」（折りたたみ式、gui/tab_jobs.py
と同じ構造）の中に、ワークフロー／チーム／ジョブ タグ／タスク タグの
チェックボックス（ワークフロー・チームはチャート本体と対応する色スペース
付き。タスク タグは、そのタグを持つタスクを1つでも含むジョブを表示する）、
ジョブ名の文字列検索、「間に合わないジョブのみ表示」をまとめてあり、一時的に
表示件数を絞り込める（絞り込みはあくまで表示上のもので、スケジューリング
自体はやり直さない）。

ジョブはそのジョブの最初のタスクの開始日が早い順。マイルストーンは縦線として
表示する。

タスクの編集（docs/roadmap.md §9）: バーを選んで、Shift（オプションで変更可）を
押しながらドラッグすると開始日の移動（右端なら期間の伸縮）、ダブルクリックで
編集ウィンドウ（gui/gantt_task_editor.py）、右クリックでメニュー。書き込み先は
タスク上書き（開始日は手動ピン＝start_pin_date、期間は日数上書き）で、1回の
操作が1つのUndo単位。書き込んだ後は再計算し、表示位置と選択を保ったまま、
他に動いたタスクの件数を状況表示に出して、そのバーを一時的に強調する。
"""

from datetime import date, timedelta

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from gui.app_settings import AppSettings
from gui.db import ProjectDatabaseError, parse_tags
from gui.gantt_edit import WorkDayCalendar, format_entity_id, parse_entity_id
from gui.gantt_task_editor import TaskEditWindow
from gui.gantt_view import FrozenGanttPane, build_gantt_scenes, set_bar_baseline
from gui.plan_actions import confirm_all, confirm_selected, selected_targets
from gui.plan_confirmation import CONFIRMED, DRAFT, UNCONFIRMED, PlanState, successor_map
from gui.replan_dialog import ReplanDialog
from gui.widgets_common import (
    ChoiceFilterGroup,
    CollapsibleSection,
    NoWheelSlider,
    NoWheelSpinBox,
    bind_undo_session,
)

# ジョブ タグ／タスク タグを1つも持たない場合にまとめる擬似キー
# （gui/tab_jobs.py と同じ考え方。2つの絞り込みは別々のChoiceFilterGroupの
# ため、同じ値を使ってもキー空間は混ざらない）。
_NO_TAG_FILTER_KEY = None
_NO_JOB_TAG_FILTER_LABEL = "（ジョブ タグなし）"
_NO_TASK_TAG_FILTER_LABEL = "（タスク タグなし）"

# 配置コントロール（画面上部、計算結果テキストの右隣に並べる操作パネル）の幅。
# タイトル・ラベル・スライダー・スピンボックスを縦に積まず1行に収めるぶん、
# 幅は広めに取る。
_PLACEMENT_CONTROL_WIDTH = 420

# ジョブ名検索は1文字入力するたびに絞り込みを走らせず、入力が止まってから
# まとめて反映する（デバウンス）。値はキー入力の間隔として自然に感じられる
# 程度（他の即時反映系UIとの一貫性より「打ち終わってから絞り込まれる」体感を優先）。
_SEARCH_DEBOUNCE_MS = 300


# オプション（gui/app_settings.py の gantt_drag_modifier）の値 → Qtの修飾キー
_DRAG_MODIFIER_KEYS = {"shift": Qt.ShiftModifier, "alt": Qt.AltModifier}


class GanttTab(QWidget):
    # 選択が変わった（状態帯の「選択した変更を確定」を押せるかが変わりうる）
    planSelectionChanged = Signal()
    def __init__(self, db, schedule_cache, app_settings=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.app_settings = app_settings or AppSettings()
        # -- タスクの編集（§9） --
        # 編集で書き込んだ後、次の再計算結果を描くときに使う（表示位置・選択を保ち、
        # 動いたバーを数える）。編集以外の再描画（絞り込み等）では None。
        self._pending_edit = None
        # Undo/Redo・編集の後に復元したい選択（キーの並び）。
        self._pending_selection = None
        self._pending_view_state = None
        self._moved_note = ""
        self._highlighted_keys = []
        self._calendar = None
        self._task_positions = {}   # {(Job_ID, Task_ID): (開始, 終了, タスク名)}
        self._predecessors = None   # {(Job_ID, Task_ID): [(先行キー, 種別)]}（遅延構築）
        self._editor = None
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self._clear_moved_highlight)
        # 結果（_result_df/_display）は ScheduleCache が一元管理する
        # （gui/schedule_cache.py 参照。プロジェクト分析タブと共有する）。
        # ここに持つ同名の属性は、cache の現在の内容を写した「表示用の
        # スナップショット」——このタブ自身の絞り込み・チャート描画ロジックは
        # 従来どおりこの2つを読むだけで済ませ、cache参照への書き換えを
        # 最小限にする。_sync_from_cache() でcacheの内容と合わせる。
        self._result_df = None
        self._display = None
        self.cache = schedule_cache
        self.cache.updated.connect(self._on_cache_updated)
        # 「配置コントロール」で調整する distribution_ratio（project_scheduler.py
        # 参照。各タスクを[ASAP, ALAP]のどのあたりに配置するかの基準点）。
        # プロジェクト設定としてDB（project.distribution_ratio）に保存する。
        # ここに持つのは表示用のキャッシュで、DB側が正——Undo/Redo等でDBの値が
        # 変わった場合は refresh_choices() の先頭で読み直して同期する。
        self._distribution_ratio = self.db.get_project()["distribution_ratio"]

        layout = QVBoxLayout(self)

        # ワークフロー／チーム／ジョブ タグ／タスク タグの4つの絞り込み
        # （いずれもOR条件のチェックボックス一覧で、4つの間はAND条件で
        # 組み合わせる。gui/tab_jobs.py の絞り込みと同じ構造・見た目）。
        self.filters_section = CollapsibleSection("絞り込み")
        layout.addWidget(self.filters_section)

        self.workflow_filter = ChoiceFilterGroup("ワークフロー")
        self.workflow_filter.changed.connect(self._refresh_chart)
        self.filters_section.content_layout.addWidget(self.workflow_filter)

        self.team_filter = ChoiceFilterGroup("チーム")
        self.team_filter.changed.connect(self._refresh_chart)
        self.filters_section.content_layout.addWidget(self.team_filter)

        self.tag_filter = ChoiceFilterGroup("ジョブ タグ")
        self.tag_filter.changed.connect(self._refresh_chart)
        self.filters_section.content_layout.addWidget(self.tag_filter)

        # タスク タグ（job_task_overrides.tags）での絞り込み。そのタグを持つ
        # タスクを1つでも含むジョブを表示する（gui/tab_jobs.py の
        # task_tag_filter と同じ考え方。display["job_task_tags"] は
        # gantt_generator.build_display() が組み立てる）。
        self.task_tag_filter = ChoiceFilterGroup("タスク タグ")
        self.task_tag_filter.changed.connect(self._refresh_chart)
        self.filters_section.content_layout.addWidget(self.task_tag_filter)

        # ジョブ名の文字列検索・「間に合わないジョブのみ表示」も、ワークフロー
        # ／チーム／ジョブ タグ／タスク タグと同じ「絞り込み」セクションにまとめる。
        search_toolbar = QHBoxLayout()
        search_toolbar.addWidget(QLabel("ジョブ名で絞り込み:"))
        self.search_edit = QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setPlaceholderText("ジョブ名の一部を入力")
        # 1文字入力するたびに絞り込みを走らせると、入力途中の文字列で毎回
        # 再描画されてしまう。入力が止まってからまとめて反映する（デバウンス）。
        self._search_debounce_timer = QTimer(self)
        self._search_debounce_timer.setSingleShot(True)
        self._search_debounce_timer.setInterval(_SEARCH_DEBOUNCE_MS)
        self._search_debounce_timer.timeout.connect(self._refresh_chart)
        self.search_edit.textChanged.connect(self._search_debounce_timer.start)
        # クリアボタンなど即座に反映したい操作もあるため、編集完了時
        # （Enter/フォーカスアウト）は待たずに即反映する。
        self.search_edit.editingFinished.connect(self._search_debounce_timer.stop)
        self.search_edit.editingFinished.connect(self._refresh_chart)
        # 20文字程度が入る幅に固定する（addWidget(..., 1)で親の幅いっぱいに
        # 伸びてしまうと、他の絞り込みチェックボックスと並べたときに長すぎるため）。
        search_edit_width = QFontMetrics(self.search_edit.font()).horizontalAdvance("あ" * 20) + 24
        self.search_edit.setFixedWidth(search_edit_width)
        search_toolbar.addWidget(self.search_edit)
        self.overrun_only_checkbox = QCheckBox("間に合わないジョブのみ表示")
        self.overrun_only_checkbox.stateChanged.connect(self._refresh_chart)
        search_toolbar.addWidget(self.overrun_only_checkbox)
        search_toolbar.addStretch(1)
        self.filters_section.content_layout.addLayout(search_toolbar)

        # 計算結果テキスト（status_label）と配置コントロールを同じ行に並べる。
        # 以前は配置コントロールをチャート本体（self.view）の右下にフローティング
        # 表示していたが、チャートのバー・グリッド線が透けて見えてしまい操作
        # 対象が見づらかったため、チャートへの重ね描画をやめて上部のテキストの
        # 横（右揃え）に置く（幅はフローティング時代と同じ _PLACEMENT_CONTROL_WIDTH）。
        top_row = QHBoxLayout()

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        top_row.addWidget(self.status_label, 1)

        # QGroupBoxのネイティブタイトルは枠線をまたぐ固定位置にしか描画できない
        # ため、タイトルは通常のQLabelとして枠内に置く（QGroupBoxはタイトル無しの
        # 単なる枠として使う）。
        self.placement_group = QGroupBox("")
        self.placement_group.setFixedWidth(_PLACEMENT_CONTROL_WIDTH)
        # ガントチャートの縦方向を圧迫しないよう、タイトル・ラベル・スライダー・
        # スピンボックスを縦に積まず1行に収める（そのぶん幅を確保する）。
        placement_layout = QHBoxLayout(self.placement_group)
        placement_layout.addWidget(QLabel("配置コントロール"))
        placement_layout.addWidget(QLabel("最速"))
        self.placement_slider = NoWheelSlider(Qt.Horizontal)
        self.placement_slider.setRange(0, 100)
        self.placement_slider.setToolTip(
            "各タスクを、依存関係が満たされ次第の最速開始～締切から逆算した"
            "最遅開始の範囲内のどこに配置するかの基準点（distribution_ratio）。"
        )
        placement_layout.addWidget(self.placement_slider, 1)
        placement_layout.addWidget(QLabel("ギリギリ"))
        self.placement_spinbox = NoWheelSpinBox()
        self.placement_spinbox.setRange(0, 100)
        self.placement_spinbox.setSuffix("%")
        self.placement_spinbox.setAlignment(Qt.AlignRight)
        # 数字入力中に1文字ごと反映されるのを防ぐ（Enter/フォーカスアウト、
        # または矢印ボタン操作でのみ valueChanged が飛ぶようにする）。
        self.placement_spinbox.setKeyboardTracking(False)
        # スピンボックスでの連続した増減（矢印連打・入力し直し）を、
        # フォーカスの出入り単位で1つのUndoにまとめる（docs/architecture.md
        # 「Undo/Redo」参照）。スライダーはドラッグ中DBに書き込まず離した時に
        # 1回だけ書き込むため、これ自体で既に1操作1Undoになっており不要。
        bind_undo_session(self.placement_spinbox, self.db, "配置コントロールを変更")
        placement_layout.addWidget(self.placement_spinbox)
        self._sync_placement_widgets(self._distribution_ratio)

        self.placement_slider.valueChanged.connect(self._on_placement_slider_value_changed)
        self.placement_slider.sliderReleased.connect(self._on_placement_slider_released)
        self.placement_spinbox.valueChanged.connect(self._on_placement_spinbox_value_changed)
        # 確定（＝DB書き込みと再計算）はここだけで行う。editingFinished は
        # Enterでの確定時とフォーカスを失った時の両方で飛ぶ
        # （_on_placement_spinbox_editing_finished 参照）。
        self.placement_spinbox.editingFinished.connect(
            self._on_placement_spinbox_editing_finished
        )
        top_row.addWidget(self.placement_group, 0, Qt.AlignRight)
        layout.addLayout(top_row)

        # 配置コントロールの外側——状況表示テキスト・枠の余白・タブの空き領域
        # ——をクリックしたときにも、数値入力欄のフォーカスが外れる（＝その
        # タイミングで確定・再計算が走る）ようにする。これらはいずれも既定では
        # フォーカスを受け取らないため、クリックしてもスピンボックスが
        # アクティブなままだった（フォーカスが外れるのはチャート本体を
        # クリックした場合だけだった）。クリックされた子ウィジェットが
        # フォーカスを受け取らないとき、Qtは祖先を辿ってクリックフォーカスを
        # 受け取れる最初のウィジェットへフォーカスを移すため、タブ自身に
        # ClickFocusを持たせるだけで上記すべてが解決する。
        self.setFocusPolicy(Qt.ClickFocus)

        self.view = FrozenGanttPane()
        layout.addWidget(self.view, 1)

        body = self.view.body
        body.moveRequested.connect(self._on_move_requested)
        body.resizeRequested.connect(self._on_resize_requested)
        body.editRequested.connect(self.open_editor)
        body.contextMenuRequested.connect(self._show_context_menu)
        body.pressed.connect(self._on_body_pressed)
        body.move_hint_provider = self._move_hint
        self.apply_app_settings()

    def apply_app_settings(self):
        """オプション（ドラッグのキー等）を反映する。起動時とオプション変更時に呼ぶ。"""
        self.view.body.drag_modifier = _DRAG_MODIFIER_KEYS[self.app_settings.get("gantt_drag_modifier")]

    def _sync_placement_widgets(self, ratio):
        """配置コントロールのスライダー・スピンボックスの表示をratioに合わせる
        （シグナルを止めて行う——ここからの再計算・DB書き込みは発生させない）。"""
        value = round(ratio * 100)
        self.placement_slider.blockSignals(True)
        self.placement_slider.setValue(value)
        self.placement_slider.blockSignals(False)
        self.placement_spinbox.blockSignals(True)
        self.placement_spinbox.setValue(value)
        self.placement_spinbox.blockSignals(False)

    def refresh_choices(self):
        """このタブに切り替わるたびに gui/main.py の _on_tab_changed から呼ばれ、
        現在のDB内容でスケジューリングを実行し直す。

        計算そのものは共有の ScheduleCache（gui/schedule_cache.py）に任せる。
        タスク数が増えるとスケジューリングは数秒かかるため、cache側で次の2つに
        よりUIが固まらないようにしている。

        1. 前回計算した時点からDBの内容が変わっていなければ再計算しない
           （db.revision で判定）。タブを行き来しただけで毎回計算し直すのを防ぐ。
           distribution_ratioの変更もDBへの書き込みを伴う（db.set_distribution_ratio）
           ためdb.revisionが進み、これだけで両方カバーできる。
        2. 計算本体はワーカースレッドで実行する。DBを読むのはGUIスレッド
           （build_frames）、計算だけ別スレッド、という分割にしている。計算中も
           画面は操作でき、途中で内容を変えれば新しい要求が古い要求を追い越す
           （古い結果は通し番号で判定して捨てる）。

        失敗した場合はダイアログを出さず、タブ内の status_label に表示するだけに
        留める。このメソッドはユーザーの明示的な操作ではなく「タブが表示される
        たび」「Undo/Redoで表示を作り直すたび」に自動的に呼ばれるため、
        ダイアログにすると、プロジェクトが未完成な間ずっと操作のたびに
        割り込むことになる（Undo/Redoのたびに無関係なダイアログが出るのを
        避けるための特別扱いが、gui/main.py 側で必要になっていた）。
        明示的な操作であるFileメニューの「ガントチャートを生成」は、従来どおり
        ダイアログでエラーを知らせる。"""
        # 配置コントロールはDBのproject.distribution_ratioが正。Undo/Redo等で
        # DB側の値がこのタブの知らないうちに変わっている場合があるため、
        # 毎回ここで読み直して表示（スライダー・スピンボックス）を合わせ直す。
        db_ratio = self.db.get_project()["distribution_ratio"]
        if db_ratio != self._distribution_ratio:
            self._distribution_ratio = db_ratio
            self._sync_placement_widgets(db_ratio)

        self.cache.ensure_fresh()
        self._sync_from_cache()

    def _on_cache_updated(self):
        """ScheduleCache の結果・エラーが更新されるたびに呼ばれる。

        自分が非表示の間は反映を後回しにする——次にこのタブへ切り替わった際、
        refresh_choices() の中で _sync_from_cache() が同期的に最新の内容を
        反映するため、ここで無駄にチャートを再構築する必要がない。"""
        if not self.isVisible():
            return
        self._sync_from_cache()

    def _sync_from_cache(self):
        """ScheduleCache の現在の状態（エラー／計算中／最新の結果）を
        このタブの表示へ反映する。"""
        if self.cache.error_message is not None:
            self._clear_chart_state(self.cache.error_message, is_error=True)
            return
        if not self.cache.is_fresh():
            self._set_status("スケジューリングを計算中です...")
            return
        self._result_df = self.cache.result_df
        self._display = self.cache.display
        self._apply_result()

    def _on_placement_slider_value_changed(self, value):
        """ドラッグ中は毎回ここが呼ばれる。スピンボックスの表示だけ追従させ、
        重いDB書き込み・再計算はスライダーを離すまで行わない
        （_on_placement_slider_released）。"""
        self.placement_spinbox.blockSignals(True)
        self.placement_spinbox.setValue(value)
        self.placement_spinbox.blockSignals(False)

    def _on_placement_slider_released(self):
        self._commit_distribution_ratio(self.placement_slider.value() / 100.0)

    def _on_placement_spinbox_value_changed(self, value):
        """▲▼ボタン・キー操作・入力の確定で値が変わるたびに呼ばれる。

        スライダーの表示だけ追従させ、DB書き込み・再計算はここでは行わない
        ——▲▼を押すたびに再計算していると重いうえ、Undoが1操作ずつ
        積み上がってしまうため（スライダーをドラッグ中に書き込まず、離した
        時に1回だけ書き込むのと同じ考え方）。実際の確定は
        _on_placement_spinbox_editing_finished に任せる。"""
        self.placement_slider.blockSignals(True)
        self.placement_slider.setValue(value)
        self.placement_slider.blockSignals(False)

    def _on_placement_spinbox_editing_finished(self):
        """スピンボックスの値を確定する。`editingFinished` は Enterでの確定時と
        フォーカスを失った時の両方で飛ぶため、▲▼で何度動かしても確定は
        「Enterを押した時」か「他をクリックしてアクティブでなくなった時」の
        1回だけになる。

        Enterで確定済みの値に対して、その後フォーカスが外れて再び呼ばれても
        _commit_distribution_ratio が値が変わっていないことを見て何もしない
        （＝非アクティブになった瞬間に同じ再計算が走り直さない）。

        フォーカスアウト経路では、bind_undo_session が開いたUndo単位がまだ
        閉じられていないうちに呼ばれる（gui/widgets_common.py の
        _UndoSessionMixin.focusOutEvent が super() を先に呼び、その中で
        editingFinished が飛ぶため）。したがってここでのDB書き込みは、
        一連の▲▼操作と同じ1つのUndoにまとまる。"""
        self._commit_distribution_ratio(self.placement_spinbox.value() / 100.0)

    def _commit_distribution_ratio(self, ratio):
        if ratio == self._distribution_ratio:
            # 実際には変わっていない（ドラッグして元の値に戻した、Enterで確定
            # した後にフォーカスが外れて再度呼ばれた等）。DB書き込みも再計算も
            # 行わない——ここで素通しすると、Undoに空のエントリが積まれたり、
            # 同じ内容の再計算が二重に走ったりする。
            return
        self._distribution_ratio = ratio
        self.db.set_distribution_ratio(ratio)
        self.refresh_choices()

    def _apply_result(self):
        """計算済みの結果でタブ内の表示（絞り込み選択肢・チャート）を作り直す。"""
        self._calendar = WorkDayCalendar.from_display(self._display)
        self._predecessors = None
        previous_positions = self._task_positions
        df = self._result_df
        self._task_positions = {
            (j, t): (s.date(), e.date(), n)
            for j, t, s, e, n in zip(df["Job_ID"], df["Task_ID"], df["Start_Date"],
                                     df["End_Date"], df["Task_Name"])
        }
        moved = []
        if self._pending_edit is not None:
            edited = self._pending_edit["keys"]
            moved = [
                key for key, pos in self._task_positions.items()
                if key not in edited and key in previous_positions
                and previous_positions[key][:2] != pos[:2]
            ]
            self._moved_note = f"他に{len(moved)}件のタスクが動きました。" if moved else ""
            if self._pending_edit.get("note"):
                self._moved_note += self._pending_edit["note"]
        else:
            self._moved_note = ""
        self._rebuild_filters()
        self._refresh_chart()
        self._pending_edit = None
        self._set_highlight(moved)
        message, is_error = self._result_summary()
        self._set_status(message + self._moved_note, is_error)
        if self._editor is not None and self._editor.isVisible():
            self._editor.reload()

    def _result_summary(self):
        """状況表示に出す文言と、エラー扱いにするかどうかを返す。

        締切に間に合わないタスクはエラーではなく結果として返ってくるため
        （project_scheduler.py の Deadline_Overrun_Days を参照）、件数を
        ここで明示しないと気付かないまま見過ごされてしまう。"""
        if self._result_df is None or self._result_df.empty:
            return "有効なタスクがありません。", False
        total = len(self._result_df)
        notes = []
        overruns = self._result_df[self._result_df["Deadline_Overrun_Days"] > 0]
        if not overruns.empty:
            worst = int(overruns["Deadline_Overrun_Days"].max())
            notes.append(
                f"うち{len(overruns)}件がマイルストーンの締切に間に合いません（最大{worst}日超過）。"
                f"チームのライン数・依存関係・締切を見直してください。"
            )
        # 満たせない開始固定日も、締切超過と同じく例外ではなく結果として返って
        # くる（固定を動かして辻褄を合わせず、矛盾はデータを書き換えて解消
        # しない）。ここで件数を出さないと気付けない。
        broken = self._result_df[self._result_df["Constraint_Violation"] != ""]
        if not broken.empty:
            notes.append(
                f"うち{len(broken)}件が開始固定日どおりに配置できません"
                f"（例: {broken.iloc[0]['Task_Name']} — {broken.iloc[0]['Constraint_Violation']}）。"
            )
        if not notes:
            return f"{total}件のタスクを生成しました。", False
        return f"{total}件のタスクを生成しました。" + "".join(notes), True

    def _set_status(self, message, is_error=False):
        """状況表示。エラーはダイアログを出さずここに表示するため、通常の
        メッセージと見分けが付くよう色を変える。"""
        self.status_label.setStyleSheet("color: #b3261e;" if is_error else "")
        self.status_label.setText(message)

    def _clear_chart_state(self, status_message, is_error=False):
        """スケジューリングに失敗した場合に、前回の生成結果（チャート・絞り込み
        選択肢）を全てクリアする。クリアしないと、直前まで表示していた古い
        結果が失敗後もそのまま残ってしまい、あたかも最新の内容であるかの
        ように誤解させてしまうため。"""
        self._result_df = None
        self._display = None
        self._pending_edit = None
        self._task_positions = {}
        self.view.setScene(None)
        self._rebuild_filters()
        self._set_status(status_message, is_error=is_error)

    # -- ワークフロー／チーム／ジョブ タグ／タスク タグの絞り込み ----------------------------

    def _job_tag_keys(self, job_id):
        """絞り込み判定に使う、ジョブが持つジョブ タグのキー集合。ジョブ タグが
        1つも無いジョブは擬似キー _NO_TAG_FILTER_KEY（＝「（ジョブ タグなし）」）
        を持つ扱いにする。"""
        job_tags = (self._display or {}).get("job_tags") or {}
        tags = parse_tags(job_tags.get(job_id, ""))
        return set(tags) if tags else {_NO_TAG_FILTER_KEY}

    def _job_task_tag_keys(self, job_id):
        """絞り込み判定に使う、ジョブが持つタスク タグのキー集合。タスク タグを
        持つタスクが1つも無いジョブは擬似キー _NO_TAG_FILTER_KEY
        （＝「（タスク タグなし）」）を持つ扱いにする。"""
        job_task_tags = (self._display or {}).get("job_task_tags") or {}
        tags = parse_tags(job_task_tags.get(job_id, ""))
        return set(tags) if tags else {_NO_TAG_FILTER_KEY}

    def _rebuild_filters(self):
        """ワークフロー／チーム／ジョブ タグ／タスク タグの絞り込み用
        チェックボックスを、直近の計算結果（全ジョブ）に合わせて再構築する。
        既存のチェック状態はキーで可能な限り維持し、新規キーは既定で表示に
        する（gui/tab_jobs.py と同じ考え方）。"""
        if self._result_df is None or not self._display:
            self.workflow_filter.rebuild([])
            self.team_filter.rebuild([])
            self.tag_filter.rebuild([])
            self.task_tag_filter.rebuild([])
            return

        workflow_names = self._display["workflow_names"]
        team_names = self._display["team_names"]
        workflow_ids = list(dict.fromkeys(self._result_df["Workflow_ID"].tolist()))
        team_ids = list(dict.fromkeys(self._result_df["Team_ID"].tolist()))
        # チェックボックスの左に色スペースを添える（左列のジョブ名の色スペース・
        # バーの色と対応付けられるようにするため）。
        self.workflow_filter.rebuild(
            [(wid, workflow_names.get(wid, wid)) for wid in workflow_ids],
            colors=self._display["workflow_colors"],
        )
        self.team_filter.rebuild(
            [(tid, team_names.get(tid, tid)) for tid in team_ids],
            colors=self._display["team_colors"],
        )

        job_ids = list(dict.fromkeys(self._result_df["Job_ID"].tolist()))

        tag_keys = set()
        has_no_tag = False
        for job_id in job_ids:
            keys = self._job_tag_keys(job_id)
            if keys == {_NO_TAG_FILTER_KEY}:
                has_no_tag = True
            else:
                tag_keys.update(keys)
        tag_items = [(tag, tag) for tag in sorted(tag_keys)]
        if has_no_tag:
            tag_items.append((_NO_TAG_FILTER_KEY, _NO_JOB_TAG_FILTER_LABEL))
        self.tag_filter.rebuild(tag_items)

        task_tag_keys = set()
        has_no_task_tag = False
        for job_id in job_ids:
            keys = self._job_task_tag_keys(job_id)
            if keys == {_NO_TAG_FILTER_KEY}:
                has_no_task_tag = True
            else:
                task_tag_keys.update(keys)
        task_tag_items = [(tag, tag) for tag in sorted(task_tag_keys)]
        if has_no_task_tag:
            task_tag_items.append((_NO_TAG_FILTER_KEY, _NO_TASK_TAG_FILTER_LABEL))
        self.task_tag_filter.rebuild(task_tag_items)

    def _refresh_chart(self):
        if self._result_df is None:
            self.view.setScene(None)
            return

        df = self._result_df
        df = df[df["Workflow_ID"].isin(self.workflow_filter.visible_keys())]
        df = df[df["Team_ID"].isin(self.team_filter.visible_keys())]

        search_text = self.search_edit.text().strip()
        if search_text:
            df = df[df["Job_Name"].str.contains(search_text, case=False, na=False, regex=False)]

        visible_tags = self.tag_filter.visible_keys()
        visible_task_tags = self.task_tag_filter.visible_keys()
        keep_job_ids = {
            job_id for job_id in df["Job_ID"].unique()
            if self._job_tag_keys(job_id) & visible_tags
            and self._job_task_tag_keys(job_id) & visible_task_tags
        }
        df = df[df["Job_ID"].isin(keep_job_ids)]

        if self.overrun_only_checkbox.isChecked():
            # 「間に合わない」かどうかはジョブ全体としての事実であり、他の
            # 絞り込み（ワークフロー／チーム等）で一部のタスクが隠れていても
            # 変わらないため、絞り込み前の全結果（self._result_df）から判定する。
            overrun_job_ids = set(
                self._result_df.loc[self._result_df["Deadline_Overrun_Days"] > 0, "Job_ID"]
            )
            df = df[df["Job_ID"].isin(overrun_job_ids)]

        selection = self._pending_selection
        view_state = self._pending_view_state
        if selection is None and self.view.scene() is not None:
            # 絞り込みの変更などで描き直すときも、表示中のバーの選択は保つ
            selection = self.view.selected_keys()
        self._pending_selection = None
        self._pending_view_state = None

        scenes = build_gantt_scenes(df, self._display, color_by="team")
        self.view.setScene(scenes)
        body = self.view.body
        body.edit_enabled = scenes is not None
        body.calendar = self._calendar
        if scenes is None:
            return
        scenes.body.selectionChanged.connect(self._on_selection_changed)
        self._decorate_plan(scenes.body)
        if view_state is not None:
            # 編集・Undo/Redo の後は、表示位置（縮尺・スクロール）を保つ。
            # 行の並び（ジョブの最早開始日順）が変わりうるので、選択したバーが
            # 見えるようにスクロールし直す。
            self.view.restore_view_state(view_state)
            self.view.select_keys(selection or [], ensure_visible=True)
            QTimer.singleShot(0, lambda: self._restore_view_after_layout(view_state, selection))
        else:
            if selection:
                self.view.select_keys(selection)
            # setScene直後はビューポートのジオメトリがまだ確定していないことが
            # あるため、次のイベントループでスケジュール全体が収まるようズームを
            # 合わせる（gui/node_canvas.py の fit_all() と同じ考え方）。
            QTimer.singleShot(0, self._fit_chart_view)

    def _fit_chart_view(self):
        self.view.fit_all()

    def _restore_view_after_layout(self, view_state, selection):
        if self.view.scene() is None:
            return
        self.view.restore_view_state(view_state)
        if selection:
            self.view.select_keys(selection, ensure_visible=True)

    # -- タスクの編集（docs/roadmap.md §9） -------------------------------------------

    def _job_and_task_ids(self, key):
        return parse_entity_id(key[0]), parse_entity_id(key[1])

    def _write_tasks(self, keys, label, write, note=""):
        """write() を1つのUndo単位で実行し、再計算を要求する。

        書き込みの前に、表示位置・選択・今の日程を覚えておき、次の再計算結果を
        描くときに使う（_apply_result / _refresh_chart）。"""
        self._pending_edit = {"keys": set(keys), "note": note}
        self._pending_selection = self.view.selected_keys() or list(keys)
        self._pending_view_state = self.view.view_state() if self.view.scene() is not None else None
        self._clear_moved_highlight()
        try:
            with self.db.undo_group(label):
                results = write()
        except ProjectDatabaseError as e:
            self._pending_edit = None
            self._pending_selection = None
            self._pending_view_state = None
            QMessageBox.warning(self, "タスクの編集", str(e))
            return
        adjusted = sum(
            (1 if r["milestone_raised"] else 0) + len(r["milestone_cascaded"])
            for r in (results or []) if r
        )
        if adjusted:
            self._pending_edit["note"] += f"マイルストーンの前後関係を保つため、{adjusted}件のマイルストーンを自動調整しました。"
        self.refresh_choices()

    def apply_task_fields(self, keys, label, fields):
        """keys の各タスクの上書き列を変える。fields は列→値の辞書、または
        編集ウィンドウの行（editor_task_rows の1要素）を受け取って辞書を返す関数。"""
        rows = {r["key"]: r for r in self.editor_task_rows(keys)} if callable(fields) else {}

        def write():
            results = []
            for key in keys:
                job_id, task_id = self._job_and_task_ids(key)
                values = fields(rows[key]) if callable(fields) else fields
                results.append(self.db.update_job_task_override_fields(job_id, task_id, **values))
            return results
        self._write_tasks(keys, label, write)

    def shift_tasks(self, keys, n):
        """keys の各タスクの開始日を、そのチームの営業日で n 日ずらして固定する。"""
        if self._calendar is None:
            return
        targets = []
        for key in keys:
            pos = self._task_positions.get(key)
            if pos is None:
                continue
            team_key = self._team_key(key)
            targets.append((key, self._calendar.shift(pos[0], n, team_key)))
        self._on_move_requested(targets, n)

    def _team_key(self, key):
        bar = self.view.bars().get(key)
        if bar is not None:
            return bar.team_key
        df = self._result_df
        match = df[(df["Job_ID"] == key[0]) & (df["Task_ID"] == key[1])]
        return match.iloc[0]["Team_ID"] if not match.empty else None

    def _on_move_requested(self, targets, shift):
        """ドラッグ（または編集ウィンドウの「ずらす」）で決まった新しい開始日を、
        手動ピン（開始固定日）として書き込む。"""
        if not targets:
            return
        keys = [key for key, _d in targets]

        if self.db.has_confirmation():
            # 確定済みのファイルでは、ドラッグした位置は変更案として記録する（手動ピン
            # にはしない。「変更を確定」で確定行に書き込まれる。§8-8）。
            def write():
                for key, new_start in targets:
                    self.db.set_draft_move(*self._job_and_task_ids(key), new_start.isoformat())
                return []
        else:
            def write():
                return [
                    self.db.update_job_task_override_fields(
                        *self._job_and_task_ids(key), start_pin_date=new_start.isoformat()
                    )
                    for key, new_start in targets
                ]
        label = "ガントでタスクを移動" if len(keys) == 1 else f"ガントで{len(keys)}件のタスクを移動"
        self._write_tasks(keys, label, write)

    def _on_resize_requested(self, key, new_days):
        """バー右端のドラッグで決まった期間（営業日）を、日数上書きとして書き込む。
        既定の日数と同じになったら上書きを外す（差分のみ保持）。"""
        rows = self.editor_task_rows([key])
        if not rows:
            return
        override = None if new_days == rows[0]["default_days"] else new_days
        job_id, task_id = self._job_and_task_ids(key)
        self._write_tasks(
            [key], "ガントでタスクの期間を変更",
            lambda: [self.db.update_job_task_override_fields(job_id, task_id, override_days=override)],
        )

    def _move_hint(self, key, new_start):
        """ドラッグ中の注意書き。先行タスクの完了（SSなら開始）より前に置こうと
        しているときだけ返す。拒否はしない（矛盾する固定もそのまま保存し、結果と
        して違反を返す既存の方針どおり）。

        誤った警告を出さないよう、確実に言える場合だけを見る: ラグが0以上の依存
        （ラグは開始をさらに遅らせる方向にしか働かない）で、先行タスクが今の結果に
        載っているもの。"""
        if self._predecessors is None:
            self._predecessors = self._build_predecessors()
        for pred_key, dep_type in self._predecessors.get(key, ()):
            pos = self._task_positions.get(pred_key)
            if pos is None:
                continue
            bound = pos[0] if dep_type == "SS" else pos[1]
            if new_start < bound:
                what = "開始" if dep_type == "SS" else "完了"
                return f"先行タスク「{pos[2]}」の{what}より前です"
        return None

    def _build_predecessors(self):
        preds = {}
        job_workflows = {j["id"]: j["workflow_id"] for j in self.db.list_jobs()}
        deps_by_workflow = {}
        for job_id, workflow_id in job_workflows.items():
            if workflow_id not in deps_by_workflow:
                deps_by_workflow[workflow_id] = [
                    d for d in self.db.list_task_dependencies(workflow_id) if d["lag_days"] >= 0
                ]
            job_key = format_entity_id("JOB", job_id)
            for d in deps_by_workflow[workflow_id]:
                preds.setdefault((job_key, format_entity_id("T", d["successor_task_id"])), []).append(
                    ((job_key, format_entity_id("T", d["predecessor_task_id"])), d["dep_type"])
                )
        for e in self.db.list_external_dependencies():
            if not e["is_active"]:
                continue
            preds.setdefault(
                (format_entity_id("JOB", e["job_id"]), format_entity_id("T", e["workflow_task_id"])), []
            ).append((
                (format_entity_id("JOB", e["depends_on_job_id"]),
                 format_entity_id("T", e["depends_on_workflow_task_id"])),
                "FS",
            ))
        return preds

    def editor_task_rows(self, keys):
        """編集ウィンドウ用に、各タスクの上書きの現在値と計算上の日程をまとめる。"""
        by_job = {}
        rows = []
        milestones = {m["id"]: m["name"] for m in self.db.list_milestones()}
        jobs = {j["id"]: j for j in self.db.list_jobs()}
        for key in keys:
            job_id, task_id = self._job_and_task_ids(key)
            job = jobs.get(job_id)
            if job is None:
                continue
            if job_id not in by_job:
                by_job[job_id] = {r["workflow_task_id"]: r for r in self.db.list_job_tasks_with_overrides(job_id)}
            r = by_job[job_id].get(task_id)
            if r is None:
                continue
            pos = self._task_positions.get(key)
            team_key = self._team_key(key) if pos is not None else None
            default_ms = milestones.get(job["default_milestone_id"], "未設定")
            rows.append({
                **r,
                "key": key,
                "job_name": job["name"],
                "default_milestone_name": default_ms,
                "start": pos[0] if pos else None,
                "last_day": (pos[1] - timedelta(days=1)) if pos else None,
                "working_days": self._calendar.count(pos[0], pos[1], team_key)
                if pos and self._calendar else None,
            })
        return rows

    def team_options(self):
        return [(t["id"], t["name"]) for t in self.db.list_teams()]

    def milestone_options(self):
        return [(m["id"], m["name"]) for m in self.db.list_milestones()]

    def open_editor(self):
        if self._editor is None:
            self._editor = TaskEditWindow(self)
        self._editor.set_keys(self.view.selected_keys())
        self._editor.show()
        self._editor.raise_()

    def _on_selection_changed(self):
        self.planSelectionChanged.emit()
        if self._editor is not None and self._editor.isVisible():
            self._editor.set_keys(self.view.selected_keys())

    def _show_context_menu(self, global_pos):
        keys = self.view.selected_keys()
        if not keys:
            return
        bars = self.view.bars()
        menu = QMenu(self)
        edit_action = menu.addAction("編集…")
        menu.addSeparator()
        pin_action = menu.addAction("開始日を固定")
        unpin_action = menu.addAction("固定を解除")
        unpin_action.setEnabled(any(bars[k].pinned for k in keys if k in bars))
        status_menu = menu.addMenu("状態")
        status_actions = {
            status_menu.addAction(label): value
            for value, label in ((None, "未着手"), ("in_progress", "進行中"), ("done", "完了"))
        }
        team_menu = menu.addMenu("チーム")
        team_actions = {team_menu.addAction("（既定を使用）"): None}
        team_menu.addSeparator()
        for team_id, name in self.team_options():
            team_actions[team_menu.addAction(name)] = team_id
        menu.addSeparator()
        disable_action = menu.addAction("無効にする")

        chosen = self._exec_menu(menu, global_pos)
        if chosen is None:
            return
        if chosen is edit_action:
            self.open_editor()
        elif chosen is pin_action:
            starts = {k: self._task_positions[k][0] for k in keys if k in self._task_positions}
            self.apply_task_fields(
                list(starts), "タスクの開始日を固定",
                lambda r: {"start_pin_date": starts[r["key"]].isoformat()},
            )
        elif chosen is unpin_action:
            self.apply_task_fields(keys, "タスクの開始日の固定を解除", {"start_pin_date": None})
        elif chosen in status_actions:
            self.apply_task_fields(keys, "タスクの状態を変更", {"status": status_actions[chosen]})
        elif chosen in team_actions:
            self.apply_task_fields(keys, "タスクのチームを変更", {"team_id": team_actions[chosen]})
        elif chosen is disable_action:
            self.apply_task_fields(keys, "タスクを無効にする", {"is_active": False})

    def _exec_menu(self, menu, global_pos):
        """メニューを出して選ばれたアクションを返す（テストで差し替えられるよう分けてある）。"""
        return menu.exec(global_pos)

    # -- 計画の確定（docs/roadmap.md §8） ----------------------------------------------

    def _confirmed_positions(self):
        """{(Job_ID, Task_ID): (確定の開始, 確定の終了)}（結果と同じ時点の確定行）。"""
        state = self.cache.plan_state
        if state is None or state.status == UNCONFIRMED:
            return {}
        return {
            (format_entity_id("JOB", j), format_entity_id("T", t)):
                (date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]))
            for (j, t), r in state.confirmed.items()
        }

    def _decorate_plan(self, scene):
        """確定済みのファイルで、まだ確定していないバーを斜線に、変更案で動いた
        バーの下に確定していた位置の細線を出す（§8-9）。"""
        confirmed = self._confirmed_positions()
        if not confirmed:
            return
        for key, bar in scene.gantt_bars.items():
            pos = confirmed.get(key)
            if pos is None:
                bar.unconfirmed = True
                bar.setToolTip(bar.toolTip() + "\n未確定")
                continue
            if pos != (bar.start, bar.end):
                set_bar_baseline(scene, bar, *pos)
                bar.setToolTip(
                    bar.toolTip()
                    + f"\n確定: {pos[0]:%m/%d}〜{pos[1] - timedelta(days=1):%m/%d}"
                    + f" → 変更案: {bar.start:%m/%d}〜{bar.end - timedelta(days=1):%m/%d}"
                )

    def plan_band_summary(self):
        """状態帯に出す (状態, 文言, 操作できるか)。文言は事実だけを短く書く。"""
        state = PlanState(self.db)
        ready = self.cache.is_fresh() and self.cache.error_message is None
        if state.status == UNCONFIRMED:
            return UNCONFIRMED, "", ready
        project = self.db.get_project()
        if state.status == CONFIRMED:
            if project["replanned_at"] and project["replan_base_date"]:
                base = date.fromisoformat(project["replan_base_date"])
                done = date.fromisoformat(project["replanned_at"][:10])
                return CONFIRMED, f"{base:%m/%d} から新計画（{done:%m/%d} 再計画）", ready
            return CONFIRMED, (project["confirmed_at"] or "")[:10], ready
        # 「変更」は確定済みのタスクへの変更（削除を含む）、「未確定」は確定後に
        # 足したタスク。同じタスクを両方に数えない。
        parts = []
        if state.pending_replan:
            base = date.fromisoformat(state.pending_replan[0])
            parts.append(f"全面再計画（{base:%m/%d} から）")
        edited = sum(1 for k in state.changed if k in state.confirmed)
        if edited:
            parts.append(f"変更 {edited}件")
        unconfirmed = len(state.changed) - edited
        if unconfirmed:
            parts.append(f"未確定のタスク {unconfirmed}件")
        if ready:
            moved, worst = self._draft_impact()
            if moved:
                sign = "+" if worst >= 0 else "−"
                parts.append(f"影響 {moved}タスク（最大 {sign}{abs(worst)}営業日）")
            guidance = self._replan_guidance(state)
            if guidance:
                parts.append(guidance)
        else:
            parts.append("計算中")
        if state.global_changed:
            parts.append("全体設定の変更（全面再計画で反映）")
        return DRAFT, " ｜ ".join(parts), ready

    def _replan_guidance(self, state):
        """影響範囲が全タスクの一定割合（オプション）を超えたら全面再計画を案内する
        （固定されたタスクの隙間に押し込むより収まりがよい可能性が高いため。§8-7）。"""
        info = self.cache.plan_info
        if state.pending_replan or not info or self._result_df is None or self._result_df.empty:
            return ""
        percent = 100 * len(info["released"]) / len(self._result_df)
        if percent <= self.app_settings.get("full_replan_threshold_percent"):
            return ""
        return f"影響が全体の {percent:.0f}%（全面再計画を検討）"

    def _draft_impact(self):
        """確定から日程が動いたタスクの数と、最大のずれ（営業日。符号付き）。"""
        confirmed = self._confirmed_positions()
        moved = 0
        worst = 0
        for key, pos in self._task_positions.items():
            before = confirmed.get(key)
            if before is None or before == pos[:2]:
                continue
            moved += 1
            shift = self._calendar.diff(before[0], pos[0], self._team_key(key)) if self._calendar else 0
            if abs(shift) > abs(worst):
                worst = shift
        return moved, worst

    def can_confirm_selected(self):
        """選んでいるタスクに、確定していない変更（またはその影響）があるか。"""
        state = self.cache.plan_state
        keys = self.view.selected_keys() if self.view.scene() is not None else []
        if state is None or state.status != DRAFT or not keys:
            return False
        if state.pending_replan:
            # 全面再計画は全体として組み直した結果なので、一部だけは確定できない
            return False
        ids = {self._job_and_task_ids(k) for k in keys}
        return bool(selected_targets(state, ids, successor_map(self.db)))

    def _ask_plan_action(self, title, text, details, ok_label):
        """取り消しの大きい操作の確認。ボタンは操作名と「キャンセル」（既定はキャンセル）。"""
        box = QMessageBox(QMessageBox.Question, title, text, parent=self)
        box.setInformativeText(details)
        ok = box.addButton(ok_label, QMessageBox.AcceptRole)
        cancel = box.addButton("キャンセル", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        # QMessageBox は幅が狭く、1行の説明が途中で折り返されるので広げる
        layout = box.layout()
        layout.addItem(QSpacerItem(560, 0, QSizePolicy.Minimum, QSizePolicy.Expanding),
                       layout.rowCount(), 0, 1, layout.columnCount())
        return self._exec_message_box(box) is ok

    def _exec_replan_dialog(self):
        """全面再計画ダイアログを出して、選んだ基準日を返す（キャンセルなら None。
        テストで差し替える）。"""
        dialog = ReplanDialog(self.db, parent=self)
        return dialog.base_date() if dialog.exec() == QDialog.Accepted else None

    def _exec_message_box(self, box):
        """確認ダイアログを出して、押されたボタンを返す（テストで差し替える）。"""
        box.exec()
        return box.clickedButton()

    def run_plan_action(self, action):
        """状態帯のボタンの処理。いずれも1回のUndoで戻せる。"""
        if action in ("confirm", "confirm_selected") and not (
                self.cache.is_fresh() and self.cache.result_df is not None):
            QMessageBox.information(self, "計画の確定", "計算が終わってから操作してください。")
            return
        try:
            if action == "confirm":
                confirm_all(self.db, self.cache.result_df)
            elif action == "confirm_selected":
                keys = {self._job_and_task_ids(k) for k in self.view.selected_keys()}
                if not keys:
                    QMessageBox.information(
                        self, "選択した変更を確定", "ガントチャートで、確定したいタスクを選んでください。"
                    )
                    return
                count = confirm_selected(self.db, self.cache.result_df, keys, successor_map(self.db))
                if count == 0:
                    QMessageBox.information(
                        self, "選択した変更を確定", "選んだタスクには、確定していない変更がありません。"
                    )
                    return
            elif action == "replan":
                base = self._exec_replan_dialog()
                if base is None:
                    return
                self.db.start_full_replan(base.isoformat(), date.today().isoformat())
            elif action == "discard":
                if not self._ask_plan_action(
                    "変更を破棄", "変更案を破棄して、最後に確定した日程に戻しますか？",
                    "消えるもの: 確定後の変更（ドラッグで動かした位置を含む）\n"
                    "残るもの: タスクの進捗の更新",
                    "変更を破棄",
                ):
                    return
                self.db.discard_draft()
            elif action == "clear":
                if not self._ask_plan_action(
                    "未確定に戻す", "プロジェクト全体を未確定に戻しますか？",
                    "消えるもの: 未着手のタスクの確定日程、変更案（ドラッグで動かした位置を含む）\n"
                    "残るもの: 進行中・完了のタスクの日程、ジョブ・タスクの設定、手動ピン\n\n"
                    "戻した後は、未着手のタスクを確定前と同じように計算し直します。",
                    "未確定に戻す",
                ):
                    return
                self.db.clear_confirmation()
        except ProjectDatabaseError as e:
            QMessageBox.warning(self, "計画の確定", str(e))
            return
        self.refresh_choices()

    # -- 動いたバーの一時的な強調 -------------------------------------------------------

    def _set_highlight(self, keys):
        self._clear_moved_highlight()
        bars = self.view.bars()
        self._highlighted_keys = [k for k in keys if k in bars]
        for key in self._highlighted_keys:
            bars[key].set_highlighted(True)
        seconds = self.app_settings.get("moved_bar_highlight_seconds")
        if self._highlighted_keys and seconds > 0:
            self._highlight_timer.start(seconds * 1000)

    def _clear_moved_highlight(self):
        self._highlight_timer.stop()
        bars = self.view.bars()
        for key in self._highlighted_keys:
            if key in bars:
                bars[key].set_highlighted(False)
        self._highlighted_keys = []

    def _on_body_pressed(self):
        # 既定（0秒＝次の操作まで）のときは、次のクリックで強調を消す
        if self.app_settings.get("moved_bar_highlight_seconds") == 0:
            self._clear_moved_highlight()

    # -- Undo/Redo（選択と表示位置） -----------------------------------------------------

    def capture_ui_state(self):
        if self.view.scene() is None:
            return None
        return {"selection": self.view.selected_keys(), "view": self.view.view_state()}

    def restore_ui_state(self, state):
        """gui/main.py の _restore_ui_state から、refresh_choices() の直後に呼ばれる。
        再計算が終わっていれば今すぐ、まだならその結果を描くときに選択と表示位置を戻す。"""
        if not state:
            return
        if self.cache.is_fresh() and self.view.scene() is not None:
            self.view.restore_view_state(state["view"])
            self.view.select_keys(state["selection"], ensure_visible=True)
        else:
            self._pending_selection = state["selection"]
            self._pending_view_state = state["view"]
