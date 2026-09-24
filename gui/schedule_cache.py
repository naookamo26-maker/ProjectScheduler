"""
スケジューリング結果のキャッシュ（ガントチャートタブ・プロジェクト分析タブが共有）。

以前は `gui/tab_gantt.py` の `GanttTab` が計算結果（_result_df/_display）と
ワーカースレッドの管理を自前で抱えていたが、プロジェクト分析タブも同じ結果を
必要とするため、判定・ワーカー管理・結果の配布をこのクラスへ集約する
（docs/project_analysis_tab_design.md §5）。GanttTabで採用していた
「要求時点のrevisionを刻む」「実行中のワーカーを全件追跡して終了を待つ」という
2つの決定はそのままここへ移す——どちらも実際の不具合から入った規約のため、
作り直さず踏襲する。

どちらのタブからでも `ensure_fresh()` を呼んで起動できる。DBの内容が
`db.revision` の比較で変わっていなければ再計算しない。結果（またはエラー）が
更新されるたびに `updated` シグナルを発火するので、タブ側はそれを購読し、
自分が表示中のときだけ画面に反映すればよい——非表示のタブは、次に
`refresh_choices()` が呼ばれた時点で `ensure_fresh()` が既に最新の状態を
同期的に返す（またはまだ計算中であることを `is_pending()` で判定できる）ので、
自然と追いつく。
"""

import gc

from PySide6.QtCore import QObject, QThread, Signal

from gui.gantt_generator import (
    build_display,
    build_frames,
    build_plan,
    compute_schedule_from_frames,
    compute_schedule_with_plan,
    validate_for_generation,
)
from gui.plan_confirmation import PlanState
from project_scheduler import SchedulingError
from i18n import tr

# 大規模サンプルで、_ScheduleWorker が別スレッドで大量にオブジェクトを確保して
# CPythonの循環GCが走るタイミングが、GUIスレッドが大きな QGraphicsScene を
# 破棄するタイミングと重なると、ほぼ確実にSegmentation faultになることを
# faulthandlerで再現・特定した（gc.disable()で再現しなくなることも確認済み）。
# 参照カウントによる解放は引き続き効くため、止めているのは循環参照の回収だけ。
gc.disable()


class _ScheduleThread(QThread):
    """スケジューリングをGUIスレッドの外で実行するスレッド。

    受け取るのは build_frames() が作ったDataFrame群だけで、DB接続は持たない
    （sqlite3の接続はスレッドをまたげないうえ、計算中にGUI側がDBを書き換えると
    結果が壊れるため。gui/gantt_generator.py の compute_schedule_from_frames
    を参照）。

    完了したら結果を、失敗したら例外メッセージを、いずれも要求時の通し番号
    （seq）付きでシグナルとして返す。呼び出し側は自分が最後に出した要求の
    番号と照合し、古い要求の結果を捨てる。

    以前は QObject のワーカーを moveToThread() でこのスレッドへ移し、スレッドの
    終了（finished）に worker.deleteLater() を繋いでいた。そうすると、ワーカー
    （と、それが抱える大きなDataFrame群）の破棄が**終了しかけのワーカー
    スレッド上で**起きる。その瞬間にGUIスレッドがGILを手放す処理（sqlite3の
    close等）をしていると両者が衝突し、テストの後片付けで数回に1回
    Segmentation fault になっていた（faulthandler で「落ちたのはPythonの
    フレームを持たないスレッド、GUIスレッドは db.close() の中」と確認）。

    いまは QThread 自体に run() を持たせ、スレッドのオブジェクトはGUIスレッドに
    置いたままにする。ワーカースレッド上ではPythonのオブジェクトを破棄しない
    （入力のDataFrame群はスレッドのオブジェクトと一緒に、GUIスレッドで解放される）。
    スレッドのオブジェクトの破棄は、GUIスレッドが wait() で終了を確かめてから
    行う（ScheduleCache._on_worker_thread_finished）。
    """

    computed = Signal(int, object, object)  # (seq, result_df, 確定を踏まえた計算の情報 or None)
    failed = Signal(int, str)               # (seq, エラーメッセージ)

    def __init__(self, seq, frames, distribution_ratio, plan=None, parent=None):
        super().__init__(parent)
        self._seq = seq
        self._frames = frames
        self._distribution_ratio = distribution_ratio
        # 確定済みのファイルなら、確定を踏まえて計算する材料（gantt_generator.build_plan）
        self._plan = plan

    def run(self):
        info = None
        try:
            if self._plan is not None:
                result_df, info = compute_schedule_with_plan(
                    self._frames, self._plan, verbose=False,
                    distribution_ratio=self._distribution_ratio,
                )
            else:
                result_df = compute_schedule_from_frames(
                    self._frames, verbose=False, distribution_ratio=self._distribution_ratio,
                )
        except SchedulingError as e:
            self.failed.emit(self._seq, str(e))
        except Exception as e:  # noqa: BLE001 - ワーカースレッドで例外を握り潰さない
            self.failed.emit(self._seq, tr("予期しないエラー: {e}", e=e))
        else:
            self.computed.emit(self._seq, result_df, info)


class ScheduleCache(QObject):
    """`MainWindow` が1プロジェクトにつき1つ保持する共有キャッシュ。"""

    updated = Signal()  # result_df/display/error_message のいずれかが変わった

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.result_df = None
        self.display = None
        self.error_message = None
        # 確定に関する状態（gui/plan_confirmation.PlanState）と、確定を踏まえた
        # 計算の情報（影響範囲など）。result_df と同じ時点のもの。
        self.plan_state = None
        self.plan_info = None
        # 直近の結果がどの時点のDB内容に対応するか（db.revision の値）。
        self._computed_revision = None
        # 現在計算中の要求が対応するrevision。None＝計算中の要求なし
        # （同じrevisionに対して重複してワーカーを起動しないための判定に使う
        # ——2つのタブがほぼ同時に ensure_fresh() を呼んでも二重計算しない）。
        self._computing_revision = None
        # 実行中のスケジューリング要求の通し番号。結果が返ってきたときに
        # 「最後に出した要求のものか」を判定し、古い結果は捨てる。
        self._request_seq = 0
        # 実行中（または終了直後の後始末待ち）の (QThread, _ScheduleWorker) の
        # 一覧。「最後の1本」ではなく全件を保持する（shutdown() 参照）。
        self._threads = []
        # 計算中の要求に対応する表示用補助情報・revision（結果が返ってきたら
        # result_df/display/_computed_revision へ移す）。
        self._pending_display = None
        self._pending_revision = None
        self._pending_plan_state = None

    def is_fresh(self):
        return self.result_df is not None and self._computed_revision == self.db.revision

    def is_pending(self):
        return self._computing_revision is not None

    def ensure_fresh(self):
        """DBの現在の内容で結果を最新化する。

        既に最新ならすぐ戻る（呼び出し側は result_df/display を直接読める）。
        古ければ非同期の計算を開始し、完了時に updated を発火する——呼び出し
        側は is_pending() で「計算中」であることを判定し、updated 購読で
        完了を受け取る。検証エラーの場合はダイアログを出さず、同期的に
        error_message へ格納して updated を発火する（呼び出し側はタブ内に
        理由を表示するだけに留める。gui/tab_gantt.py・gui/tab_analysis.py の
        エラー表示方針と同じ）。"""
        errors = validate_for_generation(self.db)
        if errors:
            self._cancel_pending_request()
            self._set_error(tr("スケジューリングできません。以下を解決してください:\n- ") + "\n- ".join(errors))
            return

        if self.is_fresh():
            return
        if self._computing_revision == self.db.revision:
            return  # 同じ内容を対象にした計算が既に進行中

        # DBの読み出しはGUIスレッドで行い、DataFrameだけをワーカーへ渡す。
        # revisionは、渡すDataFrame群が実際に対応する時点の値として、
        # build_frames() の直前で読む（計算中に他タブが編集して revision が
        # 進んでも、この要求の結果には要求時点の値を使う。_on_schedule_finished
        # 参照）。
        request_revision = self.db.revision
        try:
            frames = build_frames(self.db)
            display = build_display(self.db)
            plan_state = PlanState(self.db)
            plan = build_plan(self.db, plan_state)
        except Exception as e:  # noqa: BLE001 - 未完成なデータでも落とさない
            self._cancel_pending_request()
            self._set_error(tr("スケジューリングに失敗しました: {e}", e=e))
            return

        self.error_message = None
        self._pending_display = display
        self._pending_plan_state = plan_state
        self._pending_revision = request_revision
        self._computing_revision = request_revision
        self._request_seq += 1
        seq = self._request_seq
        distribution_ratio = self.db.get_project()["distribution_ratio"]
        self._start_worker(seq, frames, distribution_ratio, plan)

    def _set_error(self, message):
        self.result_df = None
        self.display = None
        self._computed_revision = None
        self.error_message = message
        self.updated.emit()

    def _start_worker(self, seq, frames, distribution_ratio, plan=None):
        """ワーカースレッドを起こしてスケジューリングを走らせる。

        実行中の古いスレッドは、結果を捨てる（通し番号で判定）だけで止めずに
        放置する。スケジューリングはDBに触れない純粋な計算なので、放置しても
        害はなく、途中で強制終了させるより安全なため（終了は wait() で
        shutdown() がまとめて待つ）。

        複数本が同時に走っている状態になり得るため、self._threads に全件を
        保持しておかないと、shutdown() が最後の1本しか待たずに終了し、
        それより前に始まった実行中のスレッドを残したままウィンドウが
        閉じてしまう（"QThread: Destroyed while thread is still running"）。"""
        thread = _ScheduleThread(seq, frames, distribution_ratio, plan, self)
        # スレッドのオブジェクトはGUIスレッドに属するので、run() の中から
        # emit したシグナルは自動的にキュー接続になり、GUIスレッドで受け取る。
        thread.computed.connect(self._on_schedule_finished)
        thread.failed.connect(self._on_schedule_failed)
        # self（GUIスレッドに属するQObject）の束縛メソッドを繋ぐことで、PySide側が
        # 自動的にキュー接続にしてくれる——ラムダ等の素のcallableを直接繋ぐと
        # ワーカースレッド側で実行されてしまい、GUIスレッドの self._threads を
        # ロック無しで書き換えることになる（sender()で「どのスレッドが
        # 終わったか」をGUIスレッド側から安全に判定する）。
        thread.finished.connect(self._on_worker_thread_finished)
        self._threads.append(thread)
        thread.start()

    def _on_worker_thread_finished(self):
        """finished はスレッドが終わる直前に発火するため、wait() で本当に終わった
        ことを確かめてから、GUIスレッドで破棄する（_ScheduleThread のdocstring参照）。"""
        thread = self.sender()
        if thread is None:
            return
        thread.wait()
        self._threads = [t for t in self._threads if t is not thread]
        thread.deleteLater()

    def _cancel_pending_request(self):
        """実行中の要求の結果を無視する（通し番号を進めるだけ）。"""
        self._request_seq += 1
        self._computing_revision = None

    def _on_schedule_finished(self, seq, result_df, plan_info=None):
        if seq != self._request_seq:
            return  # 追い越された古い要求の結果なので捨てる
        self.result_df = result_df
        self.display = self._pending_display
        self.plan_state = self._pending_plan_state
        self.plan_info = plan_info
        # 計算完了時点ではなく、要求時点（frames を組み立てた瞬間）の revision を
        # 刻む（gui/tab_gantt.py の同名の説明を参照——結果は古いDB内容のままなのに
        # 完了時点の新しいrevisionを刻んでしまうと、以降の再計算をスキップして
        # 編集が反映されないままになる）。
        self._computed_revision = self._pending_revision
        self._computing_revision = None
        self.error_message = None
        self.updated.emit()

    def _on_schedule_failed(self, seq, message):
        if seq != self._request_seq:
            return
        self._computing_revision = None
        self._set_error(tr("スケジューリングに失敗しました: {message}", message=message))

    def shutdown(self):
        """ウィンドウを閉じる・DBを閉じる際に、走っているスケジューリング
        すべての終了を待つ。

        ワーカーはDBに触れないため放置しても壊れないが、QThreadが動いたまま
        プロセスを終えるとQt側が警告を出すため、明示的に待ち合わせる
        （_start_worker のコメント参照——「最後の1本」だけでなく、
        追跡している全スレッドを待つ）。"""
        self._cancel_pending_request()
        threads, self._threads = self._threads, []
        for thread in threads:
            try:
                # 計算の途中で止める手段は無い（止めるより待つ方が安全）ので、
                # 終わるまで待つ。
                thread.wait(5000)
            except RuntimeError:
                # 既にdeleteLater()で破棄済み（＝計算は完了している）
                pass
