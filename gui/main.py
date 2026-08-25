"""
GUIエントリポイント（MainWindow）。

4タブ（基本情報設定・ワークフロー設計・ジョブ・ガントチャート）を束ね、
File メニューでプロジェクトファイル（.pschedule）の新規作成/オープンを行う。
プロジェクトファイルをウィンドウにドラッグ&ドロップして開くこともできる。
"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui.db import ProjectDatabase
from gui.gantt_generator import generate_gantt, validate_for_generation
from gui.tab_basic_info import BasicInfoTab
from gui.tab_gantt import GanttTab
from gui.tab_jobs import JobsTab
from gui.tab_workflows import WorkflowsTab
from gui.undo_manager import UndoManager
from project_scheduler import SchedulingError

FILE_FILTER = "Project Scheduler Files (*.pschedule);;All Files (*)"
DEFAULT_SUFFIX = "pschedule"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db: ProjectDatabase | None = None
        # プロジェクトファイルを開くたびに作り直す（ファイルをまたいだUndoは
        # 行わない）。詳細は gui/undo_manager.py 参照。
        self.undo_manager: UndoManager | None = None

        self.setWindowTitle("プロジェクトスケジューラー")
        self.resize(1500, 900)
        self.setAcceptDrops(True)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        # 接続はここで一度だけ行う。プロジェクトを開くたびに実行される
        # _rebuild_tabs() の側で接続すると、同じQTabWidgetに対して接続が
        # 累積し、タブ切り替え1回につき refresh_choices() が開いた回数だけ
        # 呼ばれてしまう（ジョブタブの refresh_choices() はDBへ書き込む
        # sync_dependency_templates() を含むため、Undo履歴にも影響する）。
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._build_empty_state_tabs()

        self._build_menu()
        self.statusBar().showMessage("プロジェクトファイルを新規作成するか、開いてください")

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
            QMessageBox.critical(self, "エラー", f"プロジェクトを開けませんでした:\n{e}")
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

    def _build_empty_state_tabs(self):
        """プロジェクト未オープン時のプレースホルダー（DB操作を必要とする
        実タブは開いた後に _rebuild_tabs() で差し替える）。"""
        self.tabs.clear()
        self.tabs.addTab(
            self._placeholder_tab("プロジェクト名・開始日・マイルストーン・チーム・休業日をここで設定します。"),
            "基本情報設定",
        )
        self.tabs.addTab(
            self._placeholder_tab(
                "ワークフローとタスクの依存関係、およびワークフロー間の依存テンプレートをここで設計します。"
            ),
            "ワークフロー設計",
        )
        self.tabs.addTab(
            self._placeholder_tab(
                "ジョブ（ワークフローの実体化）と、ジョブをまたぐ依存関係をここで作成します。"
            ),
            "ジョブ作成",
        )
        self.tabs.addTab(
            self._placeholder_tab(
                "現在の設定でのスケジューリング結果を、ファイル出力せずにその場で確認できます。"
            ),
            "ガントチャート",
        )
        self.tabs.setEnabled(False)

    def _rebuild_tabs(self):
        """DBオープン後、実際に機能するタブへ差し替える。"""
        self.tabs.clear()

        self.tab_basic_info = BasicInfoTab(self.db, on_teams_changed=self._on_teams_changed)
        self.tabs.addTab(self.tab_basic_info, "基本情報設定")

        self.tab_workflows = WorkflowsTab(self.db)
        self.tabs.addTab(self.tab_workflows, "ワークフロー設計")

        self.tab_jobs = JobsTab(self.db)
        self.tabs.addTab(self.tab_jobs, "ジョブ作成")

        self.tab_gantt = GanttTab(self.db)
        self.tabs.addTab(self.tab_gantt, "ガントチャート")

        self.tabs.setEnabled(True)
        self.generate_action.setEnabled(True)
        self.save_action.setEnabled(True)
        self.save_as_action.setEnabled(True)

    def _on_tab_changed(self, index):
        """タブを切り替えるたびに、そのタブの表示をDBの最新状態へ合わせる
        （他タブでの変更—ワークフロー名の変更やジョブの追加等—を反映するため）。"""
        widget = self.tabs.widget(index)
        if hasattr(widget, "refresh_choices"):
            widget.refresh_choices()

    def _on_teams_changed(self):
        """チームマスタが変更された際、ワークフロー設計タブの表示中キャンバスの
        色・ラベルを更新する（タスクノード編集ダイアログ自体は開くたびに
        DBから最新のチーム一覧を読むため、ここでは表示更新のみでよい）。"""
        if hasattr(self, "tab_workflows"):
            self.tab_workflows.refresh_team_choices()

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("ファイル(&F)")

        new_action = QAction("新規プロジェクト(&N)...", self)
        new_action.triggered.connect(self.on_new_project)
        file_menu.addAction(new_action)

        open_action = QAction("プロジェクトを開く(&O)...", self)
        open_action.triggered.connect(self.on_open_project)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        self.save_action = QAction("保存(&S)", self)
        self.save_action.setShortcut(QKeySequence.Save)  # 標準的にCtrl+S
        self.save_action.triggered.connect(self.on_save)
        self.save_action.setEnabled(False)
        file_menu.addAction(self.save_action)

        self.save_as_action = QAction("名前を付けて保存(&A)...", self)
        self.save_as_action.setShortcut(QKeySequence.SaveAs)  # 標準的にCtrl+Shift+S
        self.save_as_action.triggered.connect(self.on_save_as)
        self.save_as_action.setEnabled(False)
        file_menu.addAction(self.save_as_action)

        file_menu.addSeparator()

        self.generate_action = QAction("ガントチャートを生成(&G)...", self)
        self.generate_action.triggered.connect(self.on_generate_gantt)
        self.generate_action.setEnabled(False)
        file_menu.addAction(self.generate_action)

        file_menu.addSeparator()

        exit_action = QAction("終了(&X)", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        edit_menu = self.menuBar().addMenu("編集(&E)")

        self.undo_action = QAction("元に戻す", self)
        self.undo_action.setShortcut(QKeySequence.Undo)
        self.undo_action.triggered.connect(self.on_undo)
        self.undo_action.setEnabled(False)
        edit_menu.addAction(self.undo_action)

        self.redo_action = QAction("やり直す", self)
        self.redo_action.setShortcut(QKeySequence.Redo)
        self.redo_action.triggered.connect(self.on_redo)
        self.redo_action.setEnabled(False)
        edit_menu.addAction(self.redo_action)

    def on_new_project(self):
        """保存先パスはこの時点では選ばせず、初回保存（Ctrl+S/名前を付けて保存）
        まで未定のまま進める（ファイルはまだディスク上に作られない）。"""
        if not self._confirm_discard_unsaved():
            return
        try:
            self._open_database(ProjectDatabase.create_new())
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"プロジェクトを作成できませんでした:\n{e}")

    def on_open_project(self):
        if not self._confirm_discard_unsaved():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "プロジェクトファイルを開く", "", FILE_FILTER
        )
        if not path:
            return
        try:
            self._open_database(ProjectDatabase.open_existing(path))
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"プロジェクトを開けませんでした:\n{e}")

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
            QMessageBox.critical(self, "エラー", f"保存できませんでした:\n{e}")
            return False
        self._update_title()
        self.statusBar().showMessage(f"保存しました: {self.db.path}", 5000)
        return True

    def on_save_as(self):
        """名前を付けて保存（Ctrl+Shift+S）。"""
        if self.db is None:
            return False
        default_path = self.db.path or ""
        path, _ = QFileDialog.getSaveFileName(
            self, "名前を付けて保存", default_path, FILE_FILTER
        )
        if not path:
            return False
        if not path.endswith(f".{DEFAULT_SUFFIX}"):
            path = f"{path}.{DEFAULT_SUFFIX}"
        try:
            self.db.save_as(path)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"保存できませんでした:\n{e}")
            return False
        self._update_title()
        self.statusBar().showMessage(f"保存しました: {self.db.path}", 5000)
        return True

    def _confirm_discard_unsaved(self):
        """未保存の変更がある場合、保存/破棄/キャンセルを確認する。
        Returns True: 続行してよい（保存済み、または破棄を選択）。False: キャンセル。"""
        if self.db is None or not self.db.is_dirty():
            return True
        reply = QMessageBox.question(
            self, "未保存の変更",
            "現在のプロジェクトに未保存の変更があります。保存しますか？",
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
                self, "生成できません",
                "以下を解決してから再度お試しください:\n\n- " + "\n- ".join(errors),
            )
            return

        default_dir = str(Path(self.db.path).resolve().parent) if self.db.path else str(Path.home())
        out_dir = QFileDialog.getExistingDirectory(self, "ガントチャートの出力先フォルダ", default_dir)
        if not out_dir:
            return

        md_path = str(Path(out_dir) / "schedule_gantt.md")
        html_path = str(Path(out_dir) / "schedule_gantt.html")
        try:
            generate_gantt(
                self.db, mermaid_output_path=md_path, plotly_output_path=html_path, verbose=False,
            )
        except SchedulingError as e:
            QMessageBox.critical(self, "生成に失敗しました", str(e))
            return

        QMessageBox.information(
            self, "生成完了", f"ガントチャートを書き出しました:\n\n{md_path}\n{html_path}",
        )

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
        self.db = db
        self.db.on_change = self._on_db_changed
        # UndoManagerを繋ぐ前に確認しておく（繋いだ後の is_dirty() は
        # Undo履歴上の位置で判定されるようになるため）。既存ファイルを開いた
        # 直後は保存済み、新規プロジェクトは未保存。
        opened_clean = not self.db.is_dirty()
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
        )
        self.db.undo_manager = self.undo_manager
        if opened_clean:
            self.undo_manager.mark_clean()
        self._update_undo_redo_actions()
        self.statusBar().showMessage(f"開いているプロジェクト: {db.path or '無題（未保存）'}")
        self._update_title()

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
        self.undo_action.setEnabled(can_undo)
        self.undo_action.setText(f"元に戻す: {self.undo_manager.undo_label()}" if can_undo else "元に戻す")
        self.redo_action.setEnabled(can_redo)
        self.redo_action.setText(f"やり直す: {self.undo_manager.redo_label()}" if can_redo else "やり直す")

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
        タイトルバーに未保存マークを反映する。"""
        self._update_title()

    def _update_title(self):
        if self.db is None:
            self.setWindowTitle("プロジェクトスケジューラー")
            return
        mark = "*" if self.db.is_dirty() else ""
        self.setWindowTitle(f"プロジェクトスケジューラー — {mark}{self.db.path or '無題（未保存）'}")

    def closeEvent(self, event):
        if self.db is not None and not self._confirm_discard_unsaved():
            event.ignore()
            return
        if self.db is not None:
            # _open_database と同じ理由で、タブを片付けてからDBを閉じる
            # （フォーカスが外れた入力欄が、閉じたDBへ書き込もうとするのを防ぐ）。
            self.db.on_change = None
            self.db.undo_manager = None
            self._build_empty_state_tabs()
            self.db.close()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
