"""
GUIエントリポイント（MainWindow）。

4タブ（基本情報設定・ワークフロー設計・ジョブ・依存関係）を束ね、
File メニューでプロジェクトファイル（.pschedule）の新規作成/オープンを行う。
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
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

FILE_FILTER = "Project Scheduler Files (*.pschedule);;All Files (*)"
DEFAULT_SUFFIX = "pschedule"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db: ProjectDatabase | None = None

        self.setWindowTitle("プロジェクトスケジューラー")
        self.resize(1200, 800)

        self.tabs = QTabWidget()
        self.tab_basic_info = self._placeholder_tab("プロジェクト名・開始日・マイルストーン・チーム・休業日をここで設定します。")
        self.tab_workflows = self._placeholder_tab("ワークフローとタスクの依存関係をここで設計します。")
        self.tab_jobs = self._placeholder_tab("ジョブ（ワークフローの実体化）をここで作成します。")
        self.tab_dependencies = self._placeholder_tab("ジョブをまたぐ依存関係をここで設定します。")

        self.tabs.addTab(self.tab_basic_info, "基本情報設定")
        self.tabs.addTab(self.tab_workflows, "ワークフロー設計")
        self.tabs.addTab(self.tab_jobs, "ジョブ")
        self.tabs.addTab(self.tab_dependencies, "依存関係")

        self.setCentralWidget(self.tabs)
        self._set_tabs_enabled(False)

        self._build_menu()
        self.statusBar().showMessage("プロジェクトファイルを新規作成するか、開いてください")

    def _placeholder_tab(self, message):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        label = QLabel(message)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)
        return widget

    def _set_tabs_enabled(self, enabled):
        self.tabs.setEnabled(enabled)

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("ファイル(&F)")

        new_action = QAction("新規プロジェクト(&N)...", self)
        new_action.triggered.connect(self.on_new_project)
        file_menu.addAction(new_action)

        open_action = QAction("プロジェクトを開く(&O)...", self)
        open_action.triggered.connect(self.on_open_project)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        exit_action = QAction("終了(&X)", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def on_new_project(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "新規プロジェクトファイルの作成", "", FILE_FILTER
        )
        if not path:
            return
        if not path.endswith(f".{DEFAULT_SUFFIX}"):
            path = f"{path}.{DEFAULT_SUFFIX}"
        try:
            self._open_database(ProjectDatabase.create_new(path))
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"プロジェクトを作成できませんでした:\n{e}")

    def on_open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "プロジェクトファイルを開く", "", FILE_FILTER
        )
        if not path:
            return
        try:
            self._open_database(ProjectDatabase.open_existing(path))
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"プロジェクトを開けませんでした:\n{e}")

    def _open_database(self, db):
        if self.db is not None:
            self.db.close()
        self.db = db
        self._set_tabs_enabled(True)
        self.statusBar().showMessage(f"開いているプロジェクト: {db.path}")
        self.setWindowTitle(f"プロジェクトスケジューラー — {db.path}")

    def closeEvent(self, event):
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
