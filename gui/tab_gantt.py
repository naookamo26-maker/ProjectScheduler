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
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from gui.db import parse_tags
from gui.gantt_view import FrozenGanttPane, build_gantt_scenes
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


class GanttTab(QWidget):
    def __init__(self, db, schedule_cache, parent=None):
        super().__init__(parent)
        self.db = db
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
        top_row.addWidget(self.placement_group, 0, Qt.AlignRight)
        layout.addLayout(top_row)

        self.view = FrozenGanttPane()
        layout.addWidget(self.view, 1)

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
        self.placement_slider.blockSignals(True)
        self.placement_slider.setValue(value)
        self.placement_slider.blockSignals(False)
        self._commit_distribution_ratio(value / 100.0)

    def _commit_distribution_ratio(self, ratio):
        if ratio == self._distribution_ratio:
            return  # 実際には変わっていない（ドラッグして元の値に戻した等）
        self._distribution_ratio = ratio
        self.db.set_distribution_ratio(ratio)
        self.refresh_choices()

    def _apply_result(self):
        """計算済みの結果でタブ内の表示（絞り込み選択肢・チャート）を作り直す。"""
        self._rebuild_filters()
        self._refresh_chart()
        self._set_status(*self._result_summary())

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

        scenes = build_gantt_scenes(df, self._display, color_by="team")
        self.view.setScene(scenes)
        if scenes is not None:
            # setScene直後はビューポートのジオメトリがまだ確定していないことが
            # あるため、次のイベントループでスケジュール全体が収まるようズームを
            # 合わせる（gui/node_canvas.py の fit_all() と同じ考え方）。
            QTimer.singleShot(0, self._fit_chart_view)

    def _fit_chart_view(self):
        self.view.fit_all()
