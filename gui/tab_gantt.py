"""
タブ4「ガントチャート」（段階2: QGraphicsSceneによる独自バーチャート描画）。

メニューの「ガントチャートを生成」（Markdown/HTMLファイル出力）とは別に、
DBの現在の設定のまま素早くスケジューリング結果を確認するためのタブ。
このタブに切り替えるたびに自動的にスケジューリングを実行し直し（refresh_choices、
gui/main.py の _on_tab_changed から呼ばれる）、選択中の対象（ワークフロー
または後述のチーム）のタスクをバーチャートとして描画する（gui/gantt_view.py
参照。gui/node_canvas.py と同じQGraphicsView/QGraphicsSceneベースで、
ホイールズーム・中ボタンパン対応）。

「表示単位」で ワークフロー別／チーム別 を切り替えられる（既存のMermaid版
HTML出力が両方の粒度でチャートを作るのと同じ考え方）。対象コンボで既に
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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui.gantt_generator import compute_schedule, validate_for_generation
from gui.gantt_view import GanttGraphicsView, build_gantt_scene
from gui.widgets_common import NoWheelComboBox
from project_scheduler import SchedulingError


class GanttTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._result_df = None
        self._display = None
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
        fit_btn = QPushButton("全体表示")
        fit_btn.clicked.connect(self._fit_chart_view)
        toolbar.addWidget(fit_btn)
        toolbar.addWidget(QLabel("（ホイールでズーム、Ctrl+ホイールで横のみ、Shift+ホイールで縦のみ、中ボタンドラッグでパン）"))
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

        self.view = GanttGraphicsView()
        layout.addWidget(self.view, 1)

    def refresh_choices(self):
        """このタブに切り替わるたびに gui/main.py の _on_tab_changed から呼ばれ、
        現在のDB内容でスケジューリングを実行し直す。"""
        errors = validate_for_generation(self.db)
        if errors:
            QMessageBox.warning(
                self, "生成できません",
                "以下を解決してから再度お試しください:\n\n- " + "\n- ".join(errors),
            )
            return
        try:
            self._result_df, self._display = compute_schedule(self.db, verbose=False)
        except SchedulingError as e:
            QMessageBox.critical(self, "生成に失敗しました", str(e))
            return

        self._refresh_group_choices()
        self._refresh_legend()
        self._refresh_chart()

        if self._result_df.empty:
            self.status_label.setText("有効なタスクがありません。")
        else:
            adjusted = int(self._result_df["Resource_Adjusted"].sum())
            self.status_label.setText(
                f"{len(self._result_df)}件のタスクを生成しました"
                f"（うちリソース制約による前倒し ⚠ {adjusted}件、赤枠のバーで表示）。"
            )

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
            if item.widget():
                item.widget().deleteLater()
        self._filter_checks = {}
        if not self._display:
            return

        self.legend_title_label.setText(
            "ワークフローで絞り込み:" if new_dim == "workflow" else "チームで絞り込み:"
        )
        names = self._display["workflow_names"] if new_dim == "workflow" else self._display["team_names"]
        colors = self._display["workflow_colors"] if new_dim == "workflow" else self._display["team_colors"]
        for entity_id, name in names.items():
            color = colors.get(entity_id, "#898781")
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
        scene = build_gantt_scene(df, self._display, color_by=color_by)
        self.view.setScene(scene)
        if scene is not None:
            # setScene直後はビューポートのジオメトリがまだ確定していないことが
            # あるため、次のイベントループでスケジュール全体が収まるようズームを
            # 合わせる（gui/node_canvas.py の fit_all() と同じ考え方）。
            QTimer.singleShot(0, self._fit_chart_view)

    def _fit_chart_view(self):
        scene = self.view.scene()
        if scene is None:
            return
        rect = scene.itemsBoundingRect()
        if not rect.isEmpty():
            # ガントチャートは横（時間軸）と縦（行数）で必要な縮尺が大きく異なる
            # ことが多い。KeepAspectRatioだと縦横比を保つために片方が余ってしまう
            # ため、IgnoreAspectRatioで縦横それぞれ独立にビューいっぱいへ広げる。
            self.view.fitInView(rect, Qt.IgnoreAspectRatio)
