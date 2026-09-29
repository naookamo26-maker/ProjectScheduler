"""
GUIエントリポイント（MainWindow）。

5タブ（基本情報設定・ワークフロー設計・ジョブ・ガントチャート・プロジェクト分析）を束ね、
File メニューでプロジェクトファイル（.pschedule）の新規作成/オープンを行う。
プロジェクトファイルをウィンドウにドラッグ&ドロップして開くこともできる。
"""

import html
import sys
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, Qt, QTimer, QTranslator
from PySide6.QtGui import QAction, QFontDatabase, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSpacerItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app_version import APP_VERSION
from gui.app_settings import AppSettings
from i18n import current_language, set_language, tr
from gui.db import ProjectDatabase
from gui.gantt_generator import (
    PLAN_OUTPUT_CONFIRMED,
    PLAN_OUTPUT_DRAFT,
    generate_gantt,
    validate_for_generation,
)
from gui.plan_actions import confirmed_rows_from_result
from gui.plan_confirmation import DRAFT, PlanState
from gui.options_dialog import OptionsDialog
from gui.plan_band import PlanStatusBand
from gui.schedule_cache import ScheduleCache
from gui.tab_analysis import AnalysisTab
from gui.tab_basic_info import BasicInfoTab
from gui.tab_gantt import GanttTab
from gui.tab_jobs import JobsTab
from gui.tab_workflows import WorkflowsTab
from gui.undo_manager import UndoManager
from gui.widgets_common import ElidedLabel
from project_scheduler import SchedulingError

FILE_FILTER = "Project Scheduler Files (*.pschedule);;All Files (*)"
DEFAULT_SUFFIX = "pschedule"


class MainWindow(QMainWindow):
    def __init__(self, app_settings=None):
        super().__init__()
        # 利用者（PC）ごとのオプション設定（gui/app_settings.py）。プロジェクトを
        # 開き直しても引き継ぐ。
        self.app_settings = app_settings or AppSettings()
        self.db: ProjectDatabase | None = None
        # プロジェクトファイルを開くたびに作り直す（ファイルをまたいだUndoは
        # 行わない）。詳細は gui/undo_manager.py 参照。
        self.undo_manager: UndoManager | None = None
        # ガントチャートタブ・プロジェクト分析タブが共有するスケジューリング
        # 結果のキャッシュ（gui/schedule_cache.py）。DBを開くたびに作り直す。
        self.schedule_cache: ScheduleCache | None = None
        self.tab_gantt: GanttTab | None = None

        self.setWindowTitle(tr("プロジェクトスケジューラー"))
        self.resize(1500, 900)
        self.setAcceptDrops(True)

        self.tabs = QTabWidget()
        # 計画の状態帯（未確定／確定済み／変更案。docs/roadmap.md §8-9）は、
        # どのタブを開いていても見えるよう、タブの上に1本だけ置く。
        self.plan_band = PlanStatusBand()
        self.plan_band.setVisible(False)
        self.plan_band.confirmRequested.connect(lambda: self._run_plan_action("confirm"))
        self.plan_band.confirmSelectedRequested.connect(lambda: self._run_plan_action("confirm_selected"))
        self.plan_band.discardRequested.connect(lambda: self._run_plan_action("discard"))
        self.plan_band.clearRequested.connect(lambda: self._run_plan_action("clear"))
        self.plan_band.fullReplanRequested.connect(lambda: self._run_plan_action("replan"))
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(4, 4, 4, 0)
        central_layout.setSpacing(4)
        central_layout.addWidget(self.plan_band)
        central_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._plan_band_timer = QTimer(self)
        self._plan_band_timer.setSingleShot(True)
        self._plan_band_timer.setInterval(0)
        self._plan_band_timer.timeout.connect(self._refresh_plan_band)
        # 接続はここで一度だけ行う。プロジェクトを開くたびに実行される
        # _rebuild_tabs() の側で接続すると、同じQTabWidgetに対して接続が
        # 累積し、タブ切り替え1回につき refresh_choices() が開いた回数だけ
        # 呼ばれてしまう（ジョブタブの refresh_choices() はDBへ書き込む
        # sync_dependency_templates() を含むため、Undo履歴にも影響する）。
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._build_empty_state_tabs()

        self._build_menu()
        # ガントチャートの計算結果の文言（「◯件のタスクを生成しました。」など）を、
        # ファイル名（左側の一時的なメッセージ）と並べて右端に常に出す。保存時の
        # 「保存しました」に上書きされないよう、別の常設の表示にする。
        self.schedule_summary_label = QLabel("")
        self.schedule_summary_label.setContentsMargins(12, 0, 8, 0)
        self.statusBar().addPermanentWidget(self.schedule_summary_label)
        # 開いているファイルのパスは、幅に収まらなければ中央を省略する（全文はツールチップ）。
        # 以前は一時メッセージとして出していたため、右の計算結果の文言が長いとパスが途中で
        # 切れ、区切りなく文言とつながって見えた。保存時の「保存しました」は従来どおり
        # 一時メッセージで、出ている間だけこの表示を覆う。
        self.project_path_label = ElidedLabel(tr("プロジェクトファイルを新規作成するか、開いてください"))
        self.statusBar().addWidget(self.project_path_label, 1)

    def dragEnterEvent(self, event):
        if self._pschedule_path_from_mime(event.mimeData()) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._pschedule_path_from_mime(event.mimeData())
        if path is None:
            return
        if not self._confirm_discard_unsaved():
            event.acceptProposedAction()
            return
        try:
            self._open_database(ProjectDatabase.open_existing(path))
        except Exception as e:
            QMessageBox.critical(self, tr("エラー"), tr("プロジェクトを開けませんでした:\n{e}", e=e))
        event.acceptProposedAction()

    def _pschedule_path_from_mime(self, mime_data):
        if not mime_data.hasUrls():
            return None
        for url in mime_data.urls():
            if url.isLocalFile() and url.toLocalFile().endswith(f".{DEFAULT_SUFFIX}"):
                return url.toLocalFile()
        return None

    def _placeholder_tab(self, message):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        label = QLabel(message)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)
        return widget

    def _clear_tabs(self):
        """タブをすべて外し、外したタブを破棄する。

        QTabWidget.clear() はタブを外すだけで破棄しない（QStackedWidget の子として
        残り続ける）。そのままだと、プロジェクトを開き直すたびに旧タブ一式（大きな
        ガントチャートの描画内容を含む）がメモリに残って増え続け、ガントのタスク
        編集ウィンドウ（旧タブの子）も閉じたDBを指したまま残っていた。
        破棄は deleteLater() で次のイベントループに回す——この呼び出し自体が
        旧タブのシグナル処理の中から来ることがあるため。"""
        old_widgets = [self.tabs.widget(i) for i in range(self.tabs.count())]
        # 外す途中で「現在のタブ」が移るたびに currentChanged が出て、捨てる旧タブの
        # refresh_choices()（旧DBへの同期や、次のイベントループへ予約する再描画を含む）が
        # 走ってしまう。予約された再描画は旧タブの破棄後に実行され、破棄済みの
        # オブジェクトを触って例外になるので、外す間は通知を止める。
        self.tabs.blockSignals(True)
        try:
            self.tabs.clear()
        finally:
            self.tabs.blockSignals(False)
        for widget in old_widgets:
            widget.deleteLater()

    def _build_empty_state_tabs(self):
        """プロジェクト未オープン時のプレースホルダー（DB操作を必要とする
        実タブは開いた後に _rebuild_tabs() で差し替える）。"""
        self._clear_tabs()
        self.plan_band.setVisible(False)
        self.tabs.addTab(
            self._placeholder_tab(tr("プロジェクト名・開始日・マイルストーン・チーム・休業日をここで設定します。")),
            tr("基本情報設定"),
        )
        self.tabs.addTab(
            self._placeholder_tab(
                tr("ワークフローとタスクの依存関係、およびワークフロー間の依存テンプレートをここで設計します。")
            ),
            tr("ワークフロー設計"),
        )
        self.tabs.addTab(
            self._placeholder_tab(
                tr("ジョブ（ワークフローの実体化）と、ジョブをまたぐ依存関係をここで作成します。")
            ),
            tr("ジョブ作成"),
        )
        self.tabs.addTab(
            self._placeholder_tab(
                tr("現在の設定でのスケジューリング結果を、ファイル出力せずにその場で確認できます。")
            ),
            tr("ガントチャート"),
        )
        self.tabs.addTab(
            self._placeholder_tab(
                tr("ガントチャートタブの計算結果を、KPI・マイルストーン別サマリーとして集計表示します。")
            ),
            tr("プロジェクト分析"),
        )
        self.tabs.setEnabled(False)

    def _shutdown_schedule_cache(self):
        """タブを差し替える/閉じる前に、ScheduleCache が走らせている
        スケジューリングの終了を待つ（gui/schedule_cache.py の shutdown を参照）。"""
        cache = self.schedule_cache
        if cache is not None:
            try:
                if cache.shutdown():
                    # 計算結果（大きなDataFrame）を抱えたまま MainWindow の子として
                    # 残り続けないよう破棄する。計算中のスレッド（キャッシュの子）が
                    # 残っている間は、スレッドごと破棄すると落ちるので残しておく。
                    cache.deleteLater()
            except RuntimeError:
                pass  # 既にQt側で破棄済み
            self.schedule_cache = None
        self.tab_gantt = None
        self.schedule_summary_label.setText("")

    def _rebuild_tabs(self):
        """DBオープン後、実際に機能するタブへ差し替える。"""
        self._shutdown_schedule_cache()
        self._clear_tabs()

        self.tab_basic_info = BasicInfoTab(
            self.db, on_teams_changed=self._on_teams_changed,
            on_jobs_changed=self._on_jobs_changed,
        )
        self.tabs.addTab(self.tab_basic_info, tr("基本情報設定"))

        self.tab_workflows = WorkflowsTab(self.db)
        self.tabs.addTab(self.tab_workflows, tr("ワークフロー設計"))

        self.tab_jobs = JobsTab(self.db)
        self.tabs.addTab(self.tab_jobs, tr("ジョブ作成"))

        # ガントチャートタブ・プロジェクト分析タブはこのキャッシュ経由で
        # スケジューリング結果を共有する（どちらのタブからでも計算を起動できる）。
        self.schedule_cache = ScheduleCache(self.db, parent=self)
        self.schedule_cache.updated.connect(self._plan_band_timer.start)

        self.tab_gantt = GanttTab(self.db, self.schedule_cache, self.app_settings)
        self.tabs.addTab(self.tab_gantt, tr("ガントチャート"))
        self.tab_gantt.planSelectionChanged.connect(self._plan_band_timer.start)
        self.tab_gantt.summaryChanged.connect(self.schedule_summary_label.setText)

        self.tab_analysis = AnalysisTab(self.db, self.schedule_cache)
        self.tabs.addTab(self.tab_analysis, tr("プロジェクト分析"))

        self.tabs.setEnabled(True)
        self._plan_band_timer.start()
        self.generate_action.setEnabled(True)
        self.save_action.setEnabled(True)
        self.save_as_action.setEnabled(True)

    def _on_tab_changed(self, index):
        """タブを切り替えるたびに、そのタブの表示をDBの最新状態へ合わせる
        （他タブでの変更—ワークフロー名の変更やジョブの追加等—を反映するため）。"""
        widget = self.tabs.widget(index)
        if hasattr(widget, "refresh_choices"):
            widget.refresh_choices()
        self._plan_band_timer.start()

    def _on_teams_changed(self):
        """チームマスタが変更された際、ワークフロー設計タブの表示中キャンバスの
        色・ラベルを更新する（タスクノード編集ダイアログ自体は開くたびに
        DBから最新のチーム一覧を読むため、ここでは表示更新のみでよい）。"""
        if hasattr(self, "tab_workflows"):
            self.tab_workflows.refresh_team_choices()

    def _on_jobs_changed(self):
        """基本情報設定タブでのマイルストーン整合性の再調整のように、他タブから
        ジョブ側のデータを書き換えた際、ジョブタブの表示を最新へ合わせる。"""
        if hasattr(self, "tab_jobs"):
            self.tab_jobs.refresh_choices()

    def _build_menu(self):
        file_menu = self.menuBar().addMenu(tr("ファイル(&F)"))

        new_action = QAction(tr("新規プロジェクト(&N)..."), self)
        new_action.triggered.connect(self.on_new_project)
        file_menu.addAction(new_action)

        open_action = QAction(tr("プロジェクトを開く(&O)..."), self)
        open_action.triggered.connect(self.on_open_project)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        self.save_action = QAction(tr("保存(&S)"), self)
        self.save_action.setShortcut(QKeySequence.Save)  # 標準的にCtrl+S
        self.save_action.triggered.connect(self.on_save)
        self.save_action.setEnabled(False)
        file_menu.addAction(self.save_action)

        self.save_as_action = QAction(tr("名前を付けて保存(&A)..."), self)
        self.save_as_action.setShortcut(QKeySequence.SaveAs)  # 標準的にCtrl+Shift+S
        self.save_as_action.triggered.connect(self.on_save_as)
        self.save_as_action.setEnabled(False)
        file_menu.addAction(self.save_as_action)

        file_menu.addSeparator()

        self.generate_action = QAction(tr("ガントチャートを生成(&G)..."), self)
        self.generate_action.triggered.connect(self.on_generate_gantt)
        self.generate_action.setEnabled(False)
        file_menu.addAction(self.generate_action)

        file_menu.addSeparator()

        exit_action = QAction(tr("終了(&X)"), self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        edit_menu = self.menuBar().addMenu(tr("編集(&E)"))

        self.undo_action = QAction(tr("元に戻す"), self)
        self.undo_action.setShortcut(QKeySequence.Undo)
        self.undo_action.triggered.connect(self.on_undo)
        self.undo_action.setEnabled(False)
        edit_menu.addAction(self.undo_action)

        self.redo_action = QAction(tr("やり直す"), self)
        self.redo_action.setShortcut(QKeySequence.Redo)
        self.redo_action.triggered.connect(self.on_redo)
        self.redo_action.setEnabled(False)
        edit_menu.addAction(self.redo_action)

        edit_menu.addSeparator()

        # プロジェクトを開いていなくても使える（利用者ごとの設定のため）。
        options_action = QAction(tr("オプション(&O)..."), self)
        options_action.triggered.connect(self.on_options)
        edit_menu.addAction(options_action)

        help_menu = self.menuBar().addMenu(tr("ヘルプ(&H)"))
        about_action = QAction(tr("バージョン情報(&A)..."), self)
        about_action.triggered.connect(self.on_about)
        help_menu.addAction(about_action)

    def about_box(self):
        """「ヘルプ」→「バージョン情報」で出すメッセージボックス（表示はしない。
        on_about が表示し、テストと利用者ガイドの撮影はこれを直接使う）。"""
        box = QMessageBox(self)
        box.setWindowTitle(tr("バージョン情報"))
        box.setIconPixmap(QIcon(str(app_icon_path())).pixmap(64, 64))
        box.setText("<b>" + html.escape(tr("プロジェクトスケジューラー")) + "</b>")
        box.setInformativeText(tr("バージョン {version}", version=APP_VERSION))
        box.setStandardButtons(QMessageBox.Ok)
        return box

    def on_about(self):
        self.about_box().exec()

    def on_options(self):
        dialog = OptionsDialog(self.app_settings, self)
        if dialog.exec() == QDialog.Accepted:
            self._apply_app_settings()

    def _apply_app_settings(self):
        """保存したオプションを、実行中のウィンドウへ反映する。オプションは
        プロジェクトの内容ではないので、Undo/Redoの履歴には積まない。"""
        if self.undo_manager is not None:
            self.undo_manager.set_max_total_bytes(self.app_settings.undo_memory_limit_bytes())
        if self.tab_gantt is not None:
            self.tab_gantt.apply_app_settings()
        # 全面再計画を案内するしきい値が変わりうる
        self._plan_band_timer.start()

    def on_new_project(self):
        """保存先パスはこの時点では選ばせず、初回保存（Ctrl+S/名前を付けて保存）
        まで未定のまま進める（ファイルはまだディスク上に作られない）。"""
        if not self._confirm_discard_unsaved():
            return
        try:
            self._open_database(ProjectDatabase.create_new())
        except Exception as e:
            QMessageBox.critical(self, tr("エラー"), tr("プロジェクトを作成できませんでした:\n{e}", e=e))

    def on_open_project(self):
        if not self._confirm_discard_unsaved():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, tr("プロジェクトファイルを開く"), "", FILE_FILTER
        )
        if not path:
            return
        try:
            self._open_database(ProjectDatabase.open_existing(path))
        except Exception as e:
            QMessageBox.critical(self, tr("エラー"), tr("プロジェクトを開けませんでした:\n{e}", e=e))

    def on_save(self):
        """保存（Ctrl+S）。ファイルへの書き込みはここで明示的に行うまで発生しない。
        保存先パスが未定（新規プロジェクトの初回保存）の場合は、名前を付けて
        保存と同じ扱いでパス選択に迂回する。"""
        if self.db is None:
            return False
        if self.db.path is None:
            return self.on_save_as()
        try:
            self.db.save()
        except Exception as e:
            QMessageBox.critical(self, tr("エラー"), tr("保存できませんでした:\n{e}", e=e))
            return False
        self._update_title()
        self.statusBar().showMessage(tr("保存しました: {path}", path=self.db.path), 5000)
        return True

    def on_save_as(self):
        """名前を付けて保存（Ctrl+Shift+S）。"""
        if self.db is None:
            return False
        default_path = self.db.path or ""
        path, _ = QFileDialog.getSaveFileName(
            self, tr("名前を付けて保存"), default_path, FILE_FILTER
        )
        if not path:
            return False
        if not path.endswith(f".{DEFAULT_SUFFIX}"):
            # QFileDialogが確認済みの上書きは、拡張子を補う前のパス（例: "foo.txt"）
            # に対してのもの。拡張子を補った実際の書き込み先（"foo.txt.pschedule"）
            # が既存ファイルと衝突する場合、ユーザーが目にしていない別のファイルを
            # 無確認で上書きすることになるため、ここで改めて確認する。
            path = f"{path}.{DEFAULT_SUFFIX}"
            if Path(path).exists():
                reply = QMessageBox.question(
                    self, tr("上書きの確認"),
                    tr("'{name}' は既に存在します。上書きしますか？", name=Path(path).name),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return False
        try:
            self.db.save_as(path)
        except Exception as e:
            QMessageBox.critical(self, tr("エラー"), tr("保存できませんでした:\n{e}", e=e))
            return False
        self._update_title()
        self._show_project_path()
        self.statusBar().showMessage(tr("保存しました: {path}", path=self.db.path), 5000)
        return True

    def _confirm_discard_unsaved(self):
        """未保存の変更がある場合、保存/破棄/キャンセルを確認する。
        Returns True: 続行してよい（保存済み、または破棄を選択）。False: キャンセル。"""
        if self.db is None or not self.db.is_dirty():
            return True
        reply = QMessageBox.question(
            self, tr("未保存の変更"),
            tr("現在のプロジェクトに未保存の変更があります。保存しますか？"),
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if reply == QMessageBox.Save:
            return self.on_save()
        if reply == QMessageBox.Discard:
            return True
        return False

    def on_generate_gantt(self):
        if self.db is None:
            return
        errors = validate_for_generation(self.db)
        if errors:
            QMessageBox.warning(
                self, tr("生成できません"),
                tr("以下を解決してから再度お試しください:\n\n- ") + "\n- ".join(errors),
            )
            return

        plan_output = PLAN_OUTPUT_DRAFT
        if PlanState(self.db).status == DRAFT:
            # 変更案の最中だけ、どちらの日程を出力するかを選ぶ（§8-9）
            plan_output = self._ask_plan_output()
            if plan_output is None:
                return

        default_dir = str(Path(self.db.path).resolve().parent) if self.db.path else str(Path.home())
        out_dir = self._ask_output_dir(default_dir)
        if not out_dir:
            return

        html_path = str(Path(out_dir) / "schedule_gantt.html")
        try:
            result_df = generate_gantt(
                self.db, plotly_output_path=html_path, plan_output=plan_output, verbose=False,
            )
        except SchedulingError as e:
            QMessageBox.critical(self, tr("生成に失敗しました"), str(e))
            return
        except Exception as e:  # noqa: BLE001 - 想定外でも黙って失敗させない
            # スロット内の例外はQtが握りつぶすため、捕まえないと何も表示されずに
            # 終わってしまう（ガントチャートタブは同じ場合に「予期しないエラー」を出す）。
            QMessageBox.critical(self, tr("生成に失敗しました"), tr("予期しないエラー: {e}", e=e))
            return

        message = tr("ガントチャートを書き出しました:\n\n{html_path}", html_path=html_path)
        overruns = result_df[result_df["Deadline_Overrun_Days"] > 0]
        if not overruns.empty:
            # 締切超過は例外ではなく結果として返るため、ここで明示しないと
            # 「生成完了」だけを見て見過ごされてしまう。
            worst = int(overruns["Deadline_Overrun_Days"].max())
            message += tr(
                "\n\n※ マイルストーンの締切に間に合わないタスクが{n}件あります（最大{worst}日超過）。"
                "該当タスクはチャート上で赤く太い枠線で表示しています。",
                n=len(overruns), worst=worst,
            )
        broken = result_df[result_df["Constraint_Violation"] != ""]
        if not broken.empty:
            # 開始固定日の矛盾も締切超過と同じく結果として返る（例外にしない）。
            message += tr(
                "\n\n※ 開始固定日どおりに配置できないタスクが{n}件あります（例: {task} — {violation}）。",
                n=len(broken), task=broken.iloc[0]["Task_Name"],
                violation=broken.iloc[0]["Constraint_Violation"],
            )
        QMessageBox.information(self, tr("生成完了"), message)

    def _ask_plan_output(self):
        """変更案の最中にHTMLを出力するとき、確定した日程と変更案のどちらを出すかを
        尋ねる（キャンセルなら None。テストで差し替える）。"""
        box = QMessageBox(QMessageBox.Question, tr("ガントチャートを生成"),
                          tr("どちらの日程を出力しますか？"), parent=self)
        box.setInformativeText(tr("出力したファイルの見出しに、どちらの日程かを書き添えます。"))
        draft = box.addButton(tr("変更案"), QMessageBox.AcceptRole)
        confirmed = box.addButton(tr("確定した日程"), QMessageBox.AcceptRole)
        box.addButton(tr("キャンセル"), QMessageBox.RejectRole)
        box.setDefaultButton(draft)
        # QMessageBox は幅が狭く、説明文が途中で折り返されるので広げる
        layout = box.layout()
        layout.addItem(QSpacerItem(440, 0, QSizePolicy.Minimum, QSizePolicy.Expanding),
                       layout.rowCount(), 0, 1, layout.columnCount())
        box.exec()
        clicked = box.clickedButton()
        if clicked is draft:
            return PLAN_OUTPUT_DRAFT
        if clicked is confirmed:
            return PLAN_OUTPUT_CONFIRMED
        return None

    def _ask_output_dir(self, default_dir):
        """出力先フォルダを選ばせる（テストで差し替える）。"""
        return QFileDialog.getExistingDirectory(self, tr("ガントチャートの出力先フォルダ"), default_dir)

    def _open_database(self, db):
        # 旧DBを閉じるのは、旧タブを差し替え終えた後にする。タブの差し替えでは
        # 入力欄からフォーカスが外れ、editingFinished 等のシグナルが発火して
        # 旧タブが自分の持つDBへ書き込もうとする——先に閉じてしまうと
        # 「Cannot operate on a closed database」になる（Qtがスロット内の例外を
        # 握りつぶすため画面上は無害に見えるが、書き込みは失われている）。
        previous = self.db
        if previous is not None:
            previous.on_change = None
            previous.undo_manager = None
        # UndoManagerを繋ぐ前に確認しておく（繋いだ後の is_dirty() は
        # Undo履歴上の位置で判定されるようになるため）。既存ファイルを開いた
        # 直後は保存済み、新規プロジェクトは未保存。
        opened_clean = not db.is_dirty()
        # 依存テンプレートから展開するジョブ間の依存を、開いた時点で揃える。
        # これまではジョブ作成タブを開いたときにだけ揃えていたため、開いてすぐ
        # 計画を確定した後にジョブ作成タブを開くと依存が増え、確定したタスクが
        # 「変更あり」になっていた（docs/roadmap.md §8-7）。テンプレートから
        # 導かれる内容を揃えるだけなので、Undo履歴にも未保存の印にも含めない。
        db.sync_dependency_templates()
        self.db = db
        self.db.on_change = self._on_db_changed
        # タスクを進行中・完了にしたとき、表示中の日程を実績として記録するための
        # 材料（gui/db.py の _record_status_fact）。最後に計算した結果から取る。
        self.db.displayed_rows_provider = self._displayed_task_rows
        self._rebuild_tabs()
        if previous is not None:
            previous.close()
        # ファイルを開く/新規作成するたびにUndo履歴も作り直す（ファイルを
        # またいだUndoは行わない）。
        self.undo_manager = UndoManager(
            db=self.db,
            capture_ui_state=self._capture_ui_state,
            restore_ui_state=self._restore_ui_state,
            apply_db_state=self._apply_db_state,
            on_stack_changed=self._update_undo_redo_actions,
            # 「操作直後のUI状態」は、DB更新に続く表の作り直しと再選択まで
            # 終わってから取りたいので、現在のイベント処理の後へ回す
            # （gui/undo_manager.py 冒頭参照）。
            schedule_after_capture=lambda fn: QTimer.singleShot(0, fn),
            max_total_bytes=self.app_settings.undo_memory_limit_bytes(),
        )
        self.db.undo_manager = self.undo_manager
        if opened_clean:
            self.undo_manager.mark_clean()
        self._update_undo_redo_actions()
        self._show_project_path()
        self._update_title()

    def _displayed_task_rows(self, keys):
        """keys（(job_id, workflow_task_id) の並び）の、表示中の日程（確定行と同じ形）。"""
        cache = self.schedule_cache
        result_df = cache.result_df if cache is not None else None
        if result_df is None or result_df.empty:
            return []
        return confirmed_rows_from_result(self.db, result_df, only_keys=set(keys), keep_done_facts=False)

    def _show_project_path(self):
        self.project_path_label.setText(
            tr("開いているプロジェクト: {path}", path=self.db.path or tr("無題（未保存）"))
        )

    # -- Undo/Redo -----------------------------------------------------------
    #
    # 内容だけでなく、操作直前の選択・フォーカスも合わせて復元する
    # （gui/undo_manager.py 参照）。各タブ固有の状態は、refresh_choices() と
    # 対になる規約として capture_ui_state()/restore_ui_state() を実装している
    # タブについてのみ扱う——タブ名をここで列挙しないことで、将来タブが
    # 追加されたり、ガントチャートタブに編集機能が追加されたりしても、
    # そのタブ自身が2メソッドを実装しさえすれば自動的に対応できるようにする。

    def on_undo(self):
        if self.undo_manager is not None:
            self.undo_manager.undo()

    def on_redo(self):
        if self.undo_manager is not None:
            self.undo_manager.redo()

    def _update_undo_redo_actions(self):
        can_undo = self.undo_manager is not None and self.undo_manager.can_undo()
        can_redo = self.undo_manager is not None and self.undo_manager.can_redo()
        # メニューの表示は「元に戻す」「やり直す」だけにする（操作の内容は出さない）
        self.undo_action.setEnabled(can_undo)
        self.redo_action.setEnabled(can_redo)

    def _iter_tab_widgets(self):
        return [(i, self.tabs.widget(i)) for i in range(self.tabs.count())]

    def _capture_ui_state(self):
        return {
            "tab_index": self.tabs.currentIndex(),
            "tabs": {
                i: widget.capture_ui_state()
                for i, widget in self._iter_tab_widgets()
                if hasattr(widget, "capture_ui_state")
            },
        }

    def _restore_ui_state(self, state):
        """操作直前にアクティブだったタブだけを対象に、データの再読込と
        選択・フォーカスの復元を行う。それ以外のタブは意図的に触らない
        ——ガントチャートタブの refresh_choices() はスケジューリングを実行し直す
        重い処理であり、見えていないタブのために毎回走らせる必要がない。
        既存の _on_tab_changed が「タブに切り替えるたびにそのタブを最新化する」
        役目を既に持っているため、非表示タブは次にユーザーが実際に切り替えた
        時に自然と最新化される（＝データが古いまま放置されるわけではなく、
        更新を遅延させているだけ）。

        順序が重要: タブの切り替えを最初に済ませる。タブ切り替えは
        _on_tab_changed 経由で対象タブの refresh_choices() を呼び、表や
        キャンバスを作り直す——選択・フォーカスを復元した後に切り替えると、
        その作り直しで復元したばかりの選択が破棄されてしまう
        （別タブで行った操作をUndoする場合に必ず起きる）。

        state が None（操作直後のUI状態を記録できていない場合。
        gui/undo_manager.py 参照）でも、現在のタブの表示だけは必ず
        最新化する——DBは復元済みなので、画面をそのままにすると内容が
        食い違って見えてしまうため。"""
        state = state or {}
        tab_index = state.get("tab_index", self.tabs.currentIndex())
        if not (0 <= tab_index < self.tabs.count()):
            tab_index = self.tabs.currentIndex()
        self.tabs.setCurrentIndex(tab_index)
        widget = self.tabs.widget(tab_index)
        # 切り替えが発生しなかった場合（既にそのタブにいた場合）は
        # _on_tab_changed が呼ばれないため、ここで明示的に最新化する。
        if hasattr(widget, "refresh_choices"):
            widget.refresh_choices()
        if hasattr(widget, "restore_ui_state"):
            widget.restore_ui_state(state.get("tabs", {}).get(tab_index))

    def _apply_db_state(self, blob):
        self.db.restore_state(blob)

    def _on_db_changed(self):
        """DB変更時のフック（保存以外の全てのCRUD操作後に呼ばれる）。
        タイトルバーに未保存マークを反映し、計画の状態帯を更新する（続けて
        何度も変わっても、イベントループに戻った時点で1回だけ計算する）。"""
        self._update_title()
        self._plan_band_timer.start()

    # -- 計画の確定（docs/roadmap.md §8） --------------------------------------------

    def _refresh_plan_band(self):
        if self.db is None or self.tab_gantt is None:
            self.plan_band.setVisible(False)
            return
        status, detail, ready = self.tab_gantt.plan_band_summary()
        on_gantt = self.tabs.currentWidget() is self.tab_gantt
        self.plan_band.set_state(
            status, detail, show_buttons=on_gantt, buttons_enabled=ready,
            selected_enabled=on_gantt and self.tab_gantt.can_confirm_selected(),
        )
        self.plan_band.setVisible(True)

    def _run_plan_action(self, action):
        if self.tab_gantt is not None:
            self.tab_gantt.run_plan_action(action)

    def _update_title(self):
        if self.db is None:
            self.setWindowTitle(tr("プロジェクトスケジューラー"))
            return
        mark = "*" if self.db.is_dirty() else ""
        self.setWindowTitle(tr("プロジェクトスケジューラー — {mark}{path}", mark=mark, path=self.db.path or tr("無題（未保存）")))

    def closeEvent(self, event):
        if self.db is not None and not self._confirm_discard_unsaved():
            event.ignore()
            return
        if self.db is not None:
            # _open_database と同じ理由で、タブを片付けてからDBを閉じる
            # （フォーカスが外れた入力欄が、閉じたDBへ書き込もうとするのを防ぐ）。
            self.db.on_change = None
            self.db.undo_manager = None
            self._shutdown_schedule_cache()
            self._build_empty_state_tabs()
            self.db.close()
        super().closeEvent(event)


# 表示言語ごとの UI フォントの候補（先頭から、入っているものを使う）。日本語の
# フォントのまま簡体字を出すと一部の字形が日本式になるため、中国語は専用のものにする。
# ベトナム語の声調記号は Windows 既定の Segoe UI で正しく出る。
_LANGUAGE_FONTS = {
    "zh_CN": ["Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC"],
    "vi": ["Segoe UI", "Noto Sans"],
}


def apply_language(app, language):
    """表示言語を決める（docs/roadmap.md §11）。再起動で反映する方式なので、
    ウィンドウを作る前に1回だけ呼ぶ。Qt 自身の標準ダイアログ（ファイル選択等）は
    Qt 同梱の翻訳を読み込む（ベトナム語は Qt 同梱の翻訳が無く、英語で出る）。"""
    set_language(language)
    language = current_language()
    if language != "ja":
        translator = QTranslator(app)
        if translator.load(f"qtbase_{language}", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
            app.installTranslator(translator)
    families = set(QFontDatabase.families())
    for family in _LANGUAGE_FONTS.get(language, []):
        if family in families:
            font = app.font()
            font.setFamily(family)
            app.setFont(font)
            break


def app_icon_path():
    """起動用アイコン（assets/icon/app_icon.ico）の場所。.exe では展開先の
    一時フォルダ（sys._MEIPASS）の下に同梱している（packaging/ の spec）。"""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent.parent
    return root / "assets" / "icon" / "app_icon.ico"


def _set_windows_app_id():
    """python.exe から起動したとき、タスクバーに Python のアイコンではなく
    このアプリのアイコンが出るよう、独自の AppUserModelID を名乗る（Windowsのみ）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ProjectScheduler")
    except (AttributeError, OSError):
        pass


def main():
    _set_windows_app_id()
    app = QApplication(sys.argv)
    # リスト（コンボボックス）の▼で選択肢を開くとき、Windows の設定に従って Qt が
    # 巻き出すアニメーションを付ける。この効果は一覧を一度画像にしてから描き直すため、
    # 開いた瞬間に一覧が一度消えてちらつくように見えていた。アニメーションせずにすぐ出す。
    QApplication.setEffectEnabled(Qt.UI_AnimateCombo, False)
    # 利用者ごとの設定フォルダ（QStandardPaths.AppConfigLocation）の名前になる。
    app.setApplicationName("ProjectScheduler")
    app.setApplicationVersion(APP_VERSION)
    # 全ウィンドウ・ダイアログとタスクバーのアイコンになる（.exe のアイコンは spec の icon=）。
    app.setWindowIcon(QIcon(str(app_icon_path())))
    app_settings = AppSettings()
    apply_language(app, app_settings.get("language"))
    window = MainWindow(app_settings=app_settings)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
