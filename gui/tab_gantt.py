"""
タブ4「ガントチャート」（段階2: QGraphicsSceneによる独自バーチャート描画）。

メニューの「ガントチャートを生成」（HTMLファイル出力）とは別に、
DBの現在の設定のまま素早くスケジューリング結果を確認するためのタブ。
このタブに切り替えるたびに自動的にスケジューリングを実行し直し（refresh_choices、
gui/main.py の _on_tab_changed から呼ばれる）、選択中の対象（ワークフロー
または後述のチーム）のタスクをバーチャートとして描画する（gui/gantt_view.py
参照。gui/node_canvas.py と同じQGraphicsView/QGraphicsSceneベースで、
ホイールズーム・中ボタンパン対応）。

「表示単位」で ワークフロー別／チーム別 を切り替えられる。対象コンボで既に
1件に絞り込まれている軸（ワークフロー別ならワークフロー、チーム別なら
チーム）ではなく、もう一方の軸で凡例チェックボックスによる絞り込みを行う
（既に1件に固定された軸をチェックボックスで絞り込んでも意味がないため）。
- ワークフロー別: 選んだワークフロー1件分のタスクを、ジョブ単位の行で表示
  （既定）。チーム凡例のチェックボックスで、Plotly版HTML出力と同様に
  チーム単位の絞り込みができる（非表示にしたチームのタスクは除外し、
  ジョブ内のレーンを詰め直す＝スケジューリングのやり直しではなく表示上の
  フィルタのみ）。バーの色はチーム別に塗り分ける。
- チーム別: 選んだチーム1件分のタスクを、ワークフローをまたいでジョブ単位の
  行で表示する（そのチームの稼働状況を横断的に見せる）。既にチーム1件に
  絞り込まれているため、凡例はワークフロー絞り込みに切り替わる（非表示に
  したワークフローのタスクは除外）。バーの色はワークフロー別に塗り分ける
  （同一チーム内でどのワークフローの仕事かを見分けるため）。

いずれのモードでも、ジョブはそのジョブの最初のタスクの開始日が早い順。
マイルストーンは縦線として表示する。
"""

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui.gantt_generator import (
    build_display,
    build_frames,
    compute_schedule_from_frames,
    validate_for_generation,
)
from gui.gantt_view import FrozenGanttPane, build_gantt_scenes
from gui.widgets_common import NoWheelComboBox
from project_scheduler import SchedulingError


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

    def __init__(self, seq, frames):
        super().__init__()
        self._seq = seq
        self._frames = frames

    def run(self):
        try:
            result_df = compute_schedule_from_frames(self._frames, verbose=False)
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
        # 直近の計算結果がどの時点のDB内容に対応するか（db.revision の値）。
        # 一致している間は再計算しない（タブを行き来するたびに数秒かかる
        # スケジューリングを走らせないため）。
        self._computed_revision = None
        # 実行中のスケジューリング要求の通し番号。結果が返ってきたときに
        # 「最後に出した要求のものか」を判定し、古い結果は捨てる。
        self._request_seq = 0
        self._thread = None
        self._worker = None
        # 計算中の要求に対応する表示用補助情報（結果が返ってきたら _display へ移す）
        self._pending_display = None
        self._filter_checks = {}  # 絞り込み対象ID(文字列) -> QCheckBox
        self._filter_dim = "team"  # 凡例チェックボックスが対象にしている軸（"team" or "workflow"）

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("表示単位:"))
        self.mode_combo = NoWheelComboBox()
        self.mode_combo.addItem("ワークフロー別", "workflow")
        self.mode_combo.addItem("チーム別", "team")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        toolbar.addWidget(self.mode_combo)
        toolbar.addSpacing(8)
        toolbar.addWidget(QLabel("対象:"))
        self.group_combo = NoWheelComboBox()
        self.group_combo.currentIndexChanged.connect(self._refresh_chart)
        toolbar.addWidget(self.group_combo)
        toolbar.addSpacing(16)
        toolbar.addWidget(QLabel(
            "（ホイールでズーム、Ctrl+ホイールで横のみ、Shift+ホイールで縦のみ、中ボタンドラッグでパン、"
            "Aキーで全体表示、Fキーで選択中のタスクにズーム）"
        ))
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        legend_toolbar = QHBoxLayout()
        self.legend_title_label = QLabel("チームで絞り込み:")
        legend_toolbar.addWidget(self.legend_title_label)
        select_all_btn = QPushButton("すべて表示")
        select_all_btn.clicked.connect(lambda: self._set_all_filters(True))
        select_none_btn = QPushButton("すべて解除")
        select_none_btn.clicked.connect(lambda: self._set_all_filters(False))
        legend_toolbar.addWidget(select_all_btn)
        legend_toolbar.addWidget(select_none_btn)
        legend_toolbar.addSpacing(12)
        self.legend_layout = QHBoxLayout()
        legend_toolbar.addLayout(self.legend_layout)
        legend_toolbar.addStretch(1)
        layout.addLayout(legend_toolbar)

        self.view = FrozenGanttPane()
        layout.addWidget(self.view, 1)

    def refresh_choices(self):
        """このタブに切り替わるたびに gui/main.py の _on_tab_changed から呼ばれ、
        現在のDB内容でスケジューリングを実行し直す。

        タスク数が増えるとスケジューリングは数秒かかるため、次の2つでUIが
        固まらないようにしている。

        1. 前回計算した時点からDBの内容が変わっていなければ再計算しない
           （db.revision で判定）。タブを行き来しただけで毎回計算し直すのを防ぐ。
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

    def _start_worker(self, seq, frames):
        """ワーカースレッドを起こしてスケジューリングを走らせる。

        実行中の古いスレッドは、結果を捨てる（通し番号で判定）だけで止めずに
        放置する。スケジューリングはDBに触れない純粋な計算なので、放置しても
        害はなく、途中で強制終了させるより安全なため（終了は quit()/wait() を
        shutdown() でまとめて待つ）。"""
        thread = QThread(self)
        worker = _ScheduleWorker(seq, frames)
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
        """計算済みの結果でタブ内の表示（対象コンボ・凡例・チャート）を作り直す。"""
        self._refresh_group_choices()
        self._refresh_legend()
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
        # 満たせない日付制約も、締切超過と同じく例外ではなく結果として返ってくる
        # （制約は入力・日付は出力という分離を守るため、矛盾はデータを書き換えて
        # 解消しない）。ここで件数を出さないと気付けない。
        broken = self._result_df[self._result_df["Constraint_Violation"] != ""]
        if not broken.empty:
            notes.append(
                f"うち{len(broken)}件が日付制約を満たせません"
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
        """スケジューリングに失敗した場合に、前回の生成結果（チャート・凡例・
        対象コンボ）を全てクリアする。クリアしないと、直前まで表示していた
        古い結果が失敗後もそのまま残ってしまい、あたかも最新の内容であるかの
        ように誤解させてしまうため。"""
        self._result_df = None
        self._display = None
        self._computed_revision = None
        self.view.setScene(None)
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.blockSignals(False)
        while self.legend_layout.count():
            item = self.legend_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        self._filter_checks = {}
        self._set_status(status_message, is_error=is_error)

    def _on_mode_changed(self):
        self._refresh_group_choices()
        self._refresh_legend()
        self._refresh_chart()

    def _refresh_group_choices(self):
        """表示単位（ワークフロー別／チーム別）に応じて、「対象」コンボの
        選択肢をワークフロー一覧またはチーム一覧に入れ替える。"""
        self.group_combo.blockSignals(True)
        current = self.group_combo.currentData()
        self.group_combo.clear()
        if self._result_df is not None:
            if self.mode_combo.currentData() == "team":
                ids = list(dict.fromkeys(self._result_df["Team_ID"].tolist()))
                names = self._display["team_names"]
            else:
                ids = list(dict.fromkeys(self._result_df["Workflow_ID"].tolist()))
                names = self._display["workflow_names"]
            for entity_id in ids:
                self.group_combo.addItem(names.get(entity_id, entity_id), entity_id)
        self.group_combo.blockSignals(False)
        if current is not None:
            idx = self.group_combo.findData(current)
            if idx >= 0:
                self.group_combo.setCurrentIndex(idx)

    def _refresh_legend(self):
        # 表示単位（ワークフロー別／チーム別）に応じて、凡例チェックボックスの
        # 対象軸を切り替える。対象コンボで既に1件に絞り込まれている軸ではなく、
        # もう一方の軸で絞り込む（ワークフロー別ならチーム、チーム別なら
        # ワークフロー）。
        new_dim = "workflow" if self.mode_combo.currentData() == "team" else "team"
        # 既存のチェック状態は同じ軸である限り可能な限り維持し（tab_jobs.pyの
        # _rebuild_workflow_filterと同じ考え方）、新規項目は既定で表示する。
        previous_unchecked = (
            {entity_id for entity_id, cb in self._filter_checks.items() if not cb.isChecked()}
            if self._filter_dim == new_dim else set()
        )
        self._filter_dim = new_dim
        while self.legend_layout.count():
            item = self.legend_layout.takeAt(0)
            widget = item.widget()
            if widget:
                # deleteLater()だけだとレイアウトから外れた後も実際に破棄される
                # （次のイベントループ）までウィジェットが古い位置に表示され続け、
                # 新しく追加したチェックボックスと重なって古い表記が残って見える
                # ことがあるため、hide()で即座に非表示にしてから破棄する。
                widget.hide()
                widget.deleteLater()
        self._filter_checks = {}
        if not self._display:
            return

        self.legend_title_label.setText(
            "ワークフローで絞り込み:" if new_dim == "workflow" else "チームで絞り込み:"
        )
        names = self._display["workflow_names"] if new_dim == "workflow" else self._display["team_names"]
        colors = self._display["workflow_colors"] if new_dim == "workflow" else self._display["team_colors"]
        for entity_id, name in names.items():
            color = colors.get(entity_id, "#cbc9c2")
            swatch = QLabel("　")
            swatch.setFixedWidth(14)
            swatch.setStyleSheet(f"background-color: {color}; border: 1px solid #0b0b0b;")
            self.legend_layout.addWidget(swatch)

            checkbox = QCheckBox(name)
            checkbox.setChecked(entity_id not in previous_unchecked)
            checkbox.stateChanged.connect(lambda _state: self._refresh_chart())
            self.legend_layout.addWidget(checkbox)
            self._filter_checks[entity_id] = checkbox
        self.legend_layout.addStretch(1)

    def _set_all_filters(self, checked):
        for checkbox in self._filter_checks.values():
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)
        self._refresh_chart()

    def _visible_filter_ids(self):
        return {entity_id for entity_id, cb in self._filter_checks.items() if cb.isChecked()}

    def _refresh_chart(self):
        if self._result_df is None:
            self.view.setScene(None)
            return
        group_id = self.group_combo.currentData()
        if group_id is None:
            self.view.setScene(None)
            return
        if self.mode_combo.currentData() == "team":
            # 既に対象コンボで1チームに絞り込まれているため、凡例では
            # ワークフローで絞り込み、バーの色もワークフロー別に塗り分ける。
            df = self._result_df[self._result_df["Team_ID"] == group_id]
            df = df[df["Workflow_ID"].isin(self._visible_filter_ids())]
            color_by = "workflow"
        else:
            df = self._result_df[self._result_df["Workflow_ID"] == group_id]
            df = df[df["Team_ID"].isin(self._visible_filter_ids())]
            color_by = "team"
        scenes = build_gantt_scenes(df, self._display, color_by=color_by)
        self.view.setScene(scenes)
        if scenes is not None:
            # setScene直後はビューポートのジオメトリがまだ確定していないことが
            # あるため、次のイベントループでスケジュール全体が収まるようズームを
            # 合わせる（gui/node_canvas.py の fit_all() と同じ考え方）。
            QTimer.singleShot(0, self._fit_chart_view)

    def _fit_chart_view(self):
        self.view.fit_all()
