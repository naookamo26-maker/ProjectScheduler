"""
GUIエントリポイント（MainWindow）。

4タブ（基本情報設定・ワークフロー設計・ジョブ・依存関係）を束ね、
File メニューでプロジェクトファイル（.pschedule）の新規作成/オープンを行う。
"""

import sys
from pathlib import Path

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
from gui.gantt_generator import generate_gantt, validate_for_generation
from gui.tab_basic_info import BasicInfoTab
from gui.tab_dependencies import DependenciesTab
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
        self.resize(1200, 800)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_empty_state_tabs()

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

    def _build_empty_state_tabs(self):
        """プロジェクト未オープン時のプレースホルダー（DB操作を必要とする
        実タブは開いた後に _rebuild_tabs() で差し替える）。"""
        self.tabs.clear()
        self.tabs.addTab(
            self._placeholder_tab("プロジェクト名・開始日・マイルストーン・チーム・休業日をここで設定します。"),
            "基本情報設定",
        )
        self.tabs.addTab(
            self._placeholder_tab("ワークフローとタスクの依存関係をここで設計します。"), "ワークフロー設計"
        )
        self.tabs.addTab(self._placeholder_tab("ジョブ（ワークフローの実体化）をここで作成します。"), "ジョブ")
        self.tabs.addTab(self._placeholder_tab("ジョブをまたぐ依存関係をここで設定します。"), "依存関係")
        self.tabs.setEnabled(False)

    def _rebuild_tabs(self):
        """DBオープン後、実際に機能するタブへ差し替える。"""
        self.tabs.clear()

        self.tab_basic_info = BasicInfoTab(self.db, on_teams_changed=self._on_teams_changed)
        self.tabs.addTab(self.tab_basic_info, "基本情報設定")

        self.tab_workflows = WorkflowsTab(self.db)
        self.tabs.addTab(self.tab_workflows, "ワークフロー設計")

        self.tab_jobs = JobsTab(self.db)
        self.tabs.addTab(self.tab_jobs, "ジョブ")

        self.tab_dependencies = DependenciesTab(self.db)
        self.tabs.addTab(self.tab_dependencies, "依存関係")

        self.tabs.setEnabled(True)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.generate_action.setEnabled(True)

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

        self.generate_action = QAction("ガントチャートを生成(&G)...", self)
        self.generate_action.triggered.connect(self.on_generate_gantt)
        self.generate_action.setEnabled(False)
        file_menu.addAction(self.generate_action)

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

        default_dir = str(Path(self.db.path).resolve().parent)
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
            self.db.close()
        self.db = db
        self._rebuild_tabs()
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
