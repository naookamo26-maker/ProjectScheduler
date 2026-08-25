"""
GUIエントリポイント（MainWindow）。

4タブ（基本情報設定・ワークフロー設計・ジョブ・ガントチャート）を束ね、
File メニューでプロジェクトファイル（.pschedule）の新規作成/オープンを行う。
プロジェクトファイルをウィンドウにドラッグ&ドロップして開くこともできる。
"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt
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
from project_scheduler import SchedulingError

FILE_FILTER = "Project Scheduler Files (*.pschedule);;All Files (*)"
DEFAULT_SUFFIX = "pschedule"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db: ProjectDatabase | None = None

        self.setWindowTitle("プロジェクトスケジューラー")
        self.resize(1500, 900)
        self.setAcceptDrops(True)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
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
        self.tabs.currentChanged.connect(self._on_tab_changed)
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
        if self.db is not None:
            self.db.on_change = None
            self.db.close()
        self.db = db
        self.db.on_change = self._on_db_changed
        self._rebuild_tabs()
        self.statusBar().showMessage(f"開いているプロジェクト: {db.path or '無題（未保存）'}")
        self._update_title()

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
            self.db.close()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
