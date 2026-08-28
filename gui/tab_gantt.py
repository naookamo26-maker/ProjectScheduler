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
と同じ構造）の中に、ワークフロー／チーム／タグのチェックボックス（ワーク
フロー・チームはチャート本体と対応する色スペース付き）、ジョブ名の文字列
検索、「間に合わないジョブのみ表示」をまとめてあり、一時的に表示件数を
絞り込める（絞り込みはあくまで表示上のもので、スケジューリング自体は
やり直さない）。

ジョブはそのジョブの最初のタスクの開始日が早い順。マイルストーンは縦線として
表示する。
"""

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal
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

from gui.db import parse_job_tags
from gui.gantt_generator import (
    build_display,
    build_frames,
    compute_schedule_from_frames,
    validate_for_generation,
)
from gui.gantt_view import FrozenGanttPane, build_gantt_scenes
from gui.widgets_common import (
    ChoiceFilterGroup,
    CollapsibleSection,
    NoWheelSlider,
    NoWheelSpinBox,
    bind_undo_session,
)
from project_scheduler import SchedulingError

# ジョブが1つもタグを持たない場合にまとめる擬似キー（gui/tab_jobs.py と同じ考え方）。
_NO_TAG_FILTER_KEY = None
_NO_TAG_FILTER_LABEL = "（タグなし）"

# 配置コントロール（チャート本体右下にオーバーレイ表示する小さな操作パネル）の
# 幅・チャート右端／下端からの余白(px)。ラベル・スライダー・スピンボックスを
# 縦に積まず1行に収めて縦方向を圧迫しないぶん、幅は広めに取る。
_PLACEMENT_CONTROL_WIDTH = 300
_PLACEMENT_CONTROL_MARGIN = 12

# ジョブ名検索は1文字入力するたびに絞り込みを走らせず、入力が止まってから
# まとめて反映する（デバウンス）。値はキー入力の間隔として自然に感じられる
# 程度（他の即時反映系UIとの一貫性より「打ち終わってから絞り込まれる」体感を優先）。
_SEARCH_DEBOUNCE_MS = 300


class _ScheduleWorker(QObject):
    """スケジューリングをGUIスレッドの外で実行するためのワーカー。

    受け取るのは build_frames() が作ったDataFrame群だけで、DB接続は持たない
    （sqlite3の接続はスレッドをまたげないうえ、計算中にGUI側がDBを書き換えると
    結果が壊れるため。gui/gantt_generator.py の compute_schedule_from_frames
    を参照）。

    完了したら結果を、失敗したら例外メッセージを、いずれも要求時の通し番号
    （seq）付きでシグナルとして返す。呼び出し側は自分が最後に出した要求の
    番号と照合し、古い要求の結果を捨てる。
    """

    finished = Signal(int, object)   # (seq, result_df)
    failed = Signal(int, str)        # (seq, エラーメッセージ)

    def __init__(self, seq, frames, distribution_ratio):
        super().__init__()
        self._seq = seq
        self._frames = frames
        self._distribution_ratio = distribution_ratio

    def run(self):
        try:
            result_df = compute_schedule_from_frames(
                self._frames, verbose=False, distribution_ratio=self._distribution_ratio,
            )
        except SchedulingError as e:
            self.failed.emit(self._seq, str(e))
        except Exception as e:  # noqa: BLE001 - ワーカースレッドで例外を握り潰さない
            self.failed.emit(self._seq, f"予期しないエラー: {e}")
        else:
            self.finished.emit(self._seq, result_df)


class GanttTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._result_df = None
        self._display = None
        # 「配置コントロール」で調整する distribution_ratio（project_scheduler.py
        # 参照。各タスクを[ASAP, ALAP]のどのあたりに配置するかの基準点）。
        # プロジェクト設定としてDB（project.distribution_ratio）に保存する。
        # ここに持つのは表示用のキャッシュで、DB側が正——Undo/Redo等でDBの値が
        # 変わった場合は refresh_choices() の先頭で読み直して同期する。
        self._distribution_ratio = self.db.get_project()["distribution_ratio"]
        # 直近の計算結果がどの時点のDB内容に対応するか（db.revision の値）。
        # 一致している間は再計算しない（タブを行き来するたびに数秒かかる
        # スケジューリングを走らせないため）。distribution_ratioの変更もDBへの
        # 書き込みを伴う＝db.revisionが進むため、これだけで両方カバーできる。
        self._computed_revision = None
        # 実行中のスケジューリング要求の通し番号。結果が返ってきたときに
        # 「最後に出した要求のものか」を判定し、古い結果は捨てる。
        self._request_seq = 0
        self._thread = None
        self._worker = None
        # 計算中の要求に対応する表示用補助情報（結果が返ってきたら _display へ移す）
        self._pending_display = None

        layout = QVBoxLayout(self)

        # ワークフロー／チーム／タグの3つの絞り込み（いずれもOR条件の
        # チェックボックス一覧で、3つの間はAND条件で組み合わせる。
        # gui/tab_jobs.py の絞り込みと同じ構造・見た目）。
        self.filters_section = CollapsibleSection("絞り込み")
        layout.addWidget(self.filters_section)

        self.workflow_filter = ChoiceFilterGroup("ワークフロー")
        self.workflow_filter.changed.connect(self._refresh_chart)
        self.filters_section.content_layout.addWidget(self.workflow_filter)

        self.team_filter = ChoiceFilterGroup("チーム")
        self.team_filter.changed.connect(self._refresh_chart)
        self.filters_section.content_layout.addWidget(self.team_filter)

        self.tag_filter = ChoiceFilterGroup("タグ")
        self.tag_filter.changed.connect(self._refresh_chart)
        self.filters_section.content_layout.addWidget(self.tag_filter)

        # ジョブ名の文字列検索・「間に合わないジョブのみ表示」も、ワークフロー
        # ／チーム／タグと同じ「絞り込み」セクションにまとめる。
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

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.view = FrozenGanttPane()
        layout.addWidget(self.view, 1)

        # 配置コントロール（distribution_ratio の調整）。チャート本体（self.view）
        # の右下に、レイアウトへは組み込まないフローティングパネルとして重ねる
        # （self.viewのリサイズに追従させて位置合わせし直す。_reposition_placement_control
        # /eventFilter参照）。ドラッグ中に毎回再計算すると重い上にUndoできない
        # 中間状態が大量にできてしまうため、スライダーを離した時にだけ
        # DBへ書き込み・再計算する（_on_placement_slider_released）。
        # QGroupBoxのネイティブタイトルは枠線をまたぐ固定位置にしか描画できない
        # ため、タイトルは通常のQLabelとして枠内に置き、位置を自由に調整できる
        # ようにする（QGroupBoxはタイトル無しの単なる枠として使う）。
        self.placement_group = QGroupBox("", self.view)
        self.placement_group.setFixedWidth(_PLACEMENT_CONTROL_WIDTH)
        outer_layout = QVBoxLayout(self.placement_group)
        placement_title = QLabel("配置コントロール")
        outer_layout.addWidget(placement_title)
        # ガントチャートの縦方向を圧迫しないよう、ラベル・スライダー・
        # スピンボックスを縦に積まず1行に収める（そのぶん幅を確保する）。
        placement_layout = QHBoxLayout()
        outer_layout.addLayout(placement_layout)
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
        self.view.installEventFilter(self)
        self.placement_group.adjustSize()
        self.placement_group.raise_()
        QTimer.singleShot(0, self._reposition_placement_control)

    def eventFilter(self, obj, event):
        if obj is self.view and event.type() == QEvent.Resize:
            self._reposition_placement_control()
        return super().eventFilter(obj, event)

    def _reposition_placement_control(self):
        """配置コントロールをチャート本体（self.view）の右下に留め直す。"""
        self.placement_group.adjustSize()
        x = self.view.width() - self.placement_group.width() - _PLACEMENT_CONTROL_MARGIN
        y = self.view.height() - self.placement_group.height() - _PLACEMENT_CONTROL_MARGIN
        self.placement_group.move(max(0, x), max(0, y))

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

        タスク数が増えるとスケジューリングは数秒かかるため、次の2つでUIが
        固まらないようにしている。

        1. 前回計算した時点からDBの内容が変わっていなければ再計算しない
           （db.revision で判定）。タブを行き来しただけで毎回計算し直すのを防ぐ。
           distribution_ratioの変更もDBへの書き込みを伴う（db.set_distribution_ratio）
           ためdb.revisionが進み、これだけで両方カバーできる。
        2. 計算本体はワーカースレッドで実行する（_ScheduleWorker）。DBを読むのは
           GUIスレッド（build_frames）、計算だけ別スレッド、という分割にしている。
           計算中も画面は操作でき、途中で内容を変えれば新しい要求が古い要求を
           追い越す（古い結果は通し番号で判定して捨てる）。

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

        errors = validate_for_generation(self.db)
        if errors:
            self._cancel_pending_request()
            self._clear_chart_state(
                "ガントチャートを表示できません。以下を解決してください:\n- " + "\n- ".join(errors),
                is_error=True,
            )
            return

        if self._result_df is not None and self._computed_revision == self.db.revision:
            # 前回計算した時点から内容が変わっていないので、表示だけ作り直す。
            self._apply_result()
            return

        # DBの読み出しはGUIスレッドで行い、DataFrameだけをワーカーへ渡す。
        try:
            frames = build_frames(self.db)
            display = build_display(self.db)
        except Exception as e:  # noqa: BLE001 - 未完成なデータでも落とさない
            self._cancel_pending_request()
            self._clear_chart_state(f"スケジューリングに失敗しました: {e}", is_error=True)
            return

        self._pending_display = display
        self._request_seq += 1
        seq = self._request_seq
        self._start_worker(seq, frames)
        self._set_status("スケジューリングを計算中です...")

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

    def _start_worker(self, seq, frames):
        """ワーカースレッドを起こしてスケジューリングを走らせる。

        実行中の古いスレッドは、結果を捨てる（通し番号で判定）だけで止めずに
        放置する。スケジューリングはDBに触れない純粋な計算なので、放置しても
        害はなく、途中で強制終了させるより安全なため（終了は quit()/wait() を
        shutdown() でまとめて待つ）。"""
        thread = QThread(self)
        worker = _ScheduleWorker(seq, frames, self._distribution_ratio)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_schedule_finished)
        worker.failed.connect(self._on_schedule_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _cancel_pending_request(self):
        """実行中の要求の結果を無視する（通し番号を進めるだけ）。"""
        self._request_seq += 1

    def shutdown(self):
        """ウィンドウを閉じる際に、走っているスケジューリングの終了を待つ。

        ワーカーはDBに触れないため放置しても壊れないが、QThreadが動いたまま
        プロセスを終えるとQt側が警告を出すため、明示的に待ち合わせる。"""
        self._cancel_pending_request()
        thread = self._thread
        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                    thread.wait(5000)
            except RuntimeError:
                # 既にdeleteLater()で破棄済み（＝計算は完了している）
                pass
        self._thread = None
        self._worker = None

    def _on_schedule_finished(self, seq, result_df):
        if seq != self._request_seq:
            return  # 追い越された古い要求の結果なので捨てる
        self._result_df = result_df
        self._display = self._pending_display
        self._computed_revision = self.db.revision
        self._apply_result()

    def _on_schedule_failed(self, seq, message):
        if seq != self._request_seq:
            return
        self._clear_chart_state(f"スケジューリングに失敗しました: {message}", is_error=True)

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
        self._computed_revision = None
        self.view.setScene(None)
        self._rebuild_filters()
        self._set_status(status_message, is_error=is_error)

    # -- ワークフロー／チーム／タグの絞り込み ------------------------------------------

    def _job_tag_keys(self, job_id):
        """絞り込み判定に使う、ジョブが持つタグのキー集合。タグが1つも無い
        ジョブは擬似キー _NO_TAG_FILTER_KEY（＝「（タグなし）」）を持つ扱いにする。"""
        job_tags = (self._display or {}).get("job_tags") or {}
        tags = parse_job_tags(job_tags.get(job_id, ""))
        return set(tags) if tags else {_NO_TAG_FILTER_KEY}

    def _rebuild_filters(self):
        """ワークフロー／チーム／タグの絞り込み用チェックボックスを、直近の
        計算結果（全ジョブ）に合わせて再構築する。既存のチェック状態はキーで
        可能な限り維持し、新規キーは既定で表示にする（gui/tab_jobs.py と同じ
        考え方）。"""
        if self._result_df is None or not self._display:
            self.workflow_filter.rebuild([])
            self.team_filter.rebuild([])
            self.tag_filter.rebuild([])
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

        tag_keys = set()
        has_no_tag = False
        for job_id in dict.fromkeys(self._result_df["Job_ID"].tolist()):
            keys = self._job_tag_keys(job_id)
            if keys == {_NO_TAG_FILTER_KEY}:
                has_no_tag = True
            else:
                tag_keys.update(keys)
        tag_items = [(tag, tag) for tag in sorted(tag_keys)]
        if has_no_tag:
            tag_items.append((_NO_TAG_FILTER_KEY, _NO_TAG_FILTER_LABEL))
        self.tag_filter.rebuild(tag_items)

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
        keep_job_ids = {
            job_id for job_id in df["Job_ID"].unique()
            if self._job_tag_keys(job_id) & visible_tags
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
