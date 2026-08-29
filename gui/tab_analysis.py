"""
タブ5「プロジェクト分析」。

ガントチャートタブ（`gui/tab_gantt.py`）が計算したスケジューリング結果を
集計するだけの表示専用タブ。自分では計算を起こさない（`gui/summary_metrics.py`
の純粋関数に集計を任せる）——`docs/project_analysis_tab_design.md`参照。

**段階2の暫定実装**: 結果はガントチャートタブのインスタンス（`gantt_tab`）が
持つ `_result_df` / `_display` / `_computed_revision` を直接覗いて使う。
本来はキャッシュ判定・結果の配布を専用の `ScheduleCache` に切り出す予定だが
（設計案 §6）、段階2の時点では表とタイルの妥当性を先に確認するため、この
簡易な直接参照のままにしてある。"""

from datetime import date

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.summary_metrics import (
    compute_kpi,
    compute_milestone_breakdown_all,
    compute_milestone_breakdown_by,
    compute_milestone_rows,
)
from gui.widgets_common import auto_size_columns

_BREAKDOWN_ALL = "all"
_BREAKDOWN_TEAM = "team"
_BREAKDOWN_WORKFLOW = "workflow"
_BREAKDOWN_LABELS = [
    (_BREAKDOWN_ALL, "全体"),
    (_BREAKDOWN_TEAM, "チーム別"),
    (_BREAKDOWN_WORKFLOW, "ワークフロー別"),
]

_BASE_COLUMNS = ["マイルストーン", "締切日", "残", "最終終了日", "スラック", "超過", "判定"]
# ガントチャートタブのエラー表示と同じ赤（gui/tab_gantt.py の _set_status 参照）。
_ALERT_COLOR = QColor("#b3261e")


def _fmt_date(ts):
    return "—" if ts is None else ts.strftime("%Y-%m-%d")


def _fmt_int(n):
    return f"{n:,}"


class _KpiTile(QGroupBox):
    """KPIタイル1枚。タイトル（QGroupBoxのネイティブタイトル）＋大きな数値＋
    補足の小さな文字列。alert=Trueの間は数値を赤字にする（締切超過等、
    見逃したくない値のため）。"""

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        layout = QVBoxLayout(self)
        self.value_label = QLabel("—")
        value_font = self.value_label.font()
        value_font.setPointSize(value_font.pointSize() + 6)
        value_font.setBold(True)
        self.value_label.setFont(value_font)
        layout.addWidget(self.value_label)
        self.sub_label = QLabel("")
        self.sub_label.setWordWrap(True)
        layout.addWidget(self.sub_label)
        layout.addStretch(1)

    def set_value(self, value_text, sub_text, alert=False):
        self.value_label.setText(value_text)
        self.value_label.setStyleSheet("color: #b3261e;" if alert else "")
        self.sub_label.setText(sub_text)


class AnalysisTab(QWidget):
    def __init__(self, db, gantt_tab, parent=None):
        super().__init__(parent)
        self.db = db
        self.gantt_tab = gantt_tab
        self._breakdown_mode = _BREAKDOWN_ALL

        layout = QVBoxLayout(self)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        kpi_row = QHBoxLayout()
        self.kpi_scale = _KpiTile("規模")
        self.kpi_period = _KpiTile("計画期間")
        self.kpi_status = _KpiTile("タスクの状態")
        self.kpi_peak = _KpiTile("同時タスク数のピーク")
        self.kpi_overrun = _KpiTile("締切に間に合わないタスク")
        self.kpi_violation = _KpiTile("開始固定日の違反")
        for tile in (
            self.kpi_scale, self.kpi_period, self.kpi_status,
            self.kpi_peak, self.kpi_overrun, self.kpi_violation,
        ):
            kpi_row.addWidget(tile)
        layout.addLayout(kpi_row)

        self.milestones_group = QGroupBox("マイルストーン別サマリー")
        ms_layout = QVBoxLayout(self.milestones_group)

        breakdown_bar = QHBoxLayout()
        breakdown_bar.addWidget(QLabel("内訳"))
        self._breakdown_group = QButtonGroup(self)
        self._breakdown_group.setExclusive(True)
        self._breakdown_buttons = {}
        for key, label in _BREAKDOWN_LABELS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == self._breakdown_mode)
            btn.clicked.connect(lambda _checked, k=key: self._on_breakdown_changed(k))
            self._breakdown_group.addButton(btn)
            self._breakdown_buttons[key] = btn
            breakdown_bar.addWidget(btn)
        breakdown_bar.addStretch(1)
        self.breakdown_hint_label = QLabel("")
        self.breakdown_hint_label.setStyleSheet("color: #6d6b66;")
        breakdown_bar.addWidget(self.breakdown_hint_label)
        ms_layout.addLayout(breakdown_bar)

        self.milestone_table = QTableWidget(0, len(_BASE_COLUMNS))
        self.milestone_table.setHorizontalHeaderLabels(_BASE_COLUMNS)
        self.milestone_table.verticalHeader().setVisible(False)
        self.milestone_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.milestone_table.setSelectionMode(QAbstractItemView.NoSelection)
        ms_layout.addWidget(self.milestone_table)

        layout.addWidget(self.milestones_group, 1)

    def _on_breakdown_changed(self, key):
        if key == self._breakdown_mode:
            return
        self._breakdown_mode = key
        self._render()

    # -- 結果の取得（段階2の暫定: GanttTabの内部状態を直接覗く） -----------------------

    def _current_result(self):
        """(result_df, display) を返す。結果が無い/古い場合は (None, None)。"""
        gantt_tab = self.gantt_tab
        if gantt_tab is None or gantt_tab._result_df is None:
            return None, None
        if gantt_tab._computed_revision != self.db.revision:
            return None, None
        return gantt_tab._result_df, gantt_tab._display

    def refresh_choices(self):
        """タブに切り替わるたび（gui/main.py の _on_tab_changed）に呼ぶ。
        自分では計算しない——ガントチャートタブ側の結果をそのまま集計し直す
        だけなので軽い（設計案の実測で全集計0.1秒未満）。"""
        self._render()

    def _render(self):
        result_df, display = self._current_result()
        if result_df is None:
            self._show_no_result()
            return
        self.status_label.setStyleSheet("")
        self.status_label.setText("ガントチャートタブの計算結果を集計しています。")

        milestones = [
            (mid, name, due) for mid, name, due in display["milestone_markers"]
            if mid != "PROJECT_START"
        ]
        project_start = self.db.get_project()["start_date"]
        project_start_ts = pd.Timestamp(project_start) if project_start else None
        today = date.today()

        self._render_kpi(result_df, project_start_ts, milestones, today)
        self._render_milestone_table(result_df, display, milestones, today)

    def _show_no_result(self):
        self.status_label.setStyleSheet("color: #b3261e;")
        self.status_label.setText(
            "スケジューリング結果がありません。ガントチャートタブを開いて計算してください。"
        )
        for tile in (
            self.kpi_scale, self.kpi_period, self.kpi_status,
            self.kpi_peak, self.kpi_overrun, self.kpi_violation,
        ):
            tile.set_value("—", "")
        self.milestone_table.setRowCount(0)
        self.breakdown_hint_label.setText("")

    def _render_kpi(self, result_df, project_start_ts, milestones, today):
        kpi = compute_kpi(result_df, project_start_ts, milestones, today)

        self.kpi_scale.set_value(_fmt_int(kpi["jobs"]) + " ジョブ", f"{_fmt_int(kpi['tasks'])} タスク")

        if project_start_ts is not None and kpi["plan_end"] is not None:
            period_text = f"{project_start_ts.strftime('%Y/%m')} → {kpi['plan_end'].strftime('%Y/%m')}"
        else:
            period_text = "—"
        margin = kpi["milestone_margin_days"]
        if margin is None:
            margin_text = ""
        elif margin >= 0:
            margin_text = f"最終マイルストーンまで{margin}日の余裕"
        else:
            margin_text = f"最終マイルストーンを{-margin}日超過"
        self.kpi_period.set_value(period_text, margin_text)

        self.kpi_status.set_value(
            f"{_fmt_int(kpi['done'])} 完了",
            f"進行中 {_fmt_int(kpi['in_progress'])} / 未着手 {_fmt_int(kpi['not_started'])}",
        )

        if kpi["peak_month"] is None:
            self.kpi_peak.set_value("—", "")
        else:
            self.kpi_peak.set_value(
                f"{_fmt_int(kpi['peak'])} 本", f"全チーム合計 / {kpi['peak_month'].replace('-', '/')}"
            )

        self.kpi_overrun.set_value(
            f"{_fmt_int(kpi['overrun_tasks'])} 件",
            f"{_fmt_int(kpi['overrun_jobs'])} ジョブ / 最大 {kpi['max_overrun_days']}日超過",
            alert=kpi["overrun_tasks"] > 0,
        )

        self.kpi_violation.set_value(
            f"{kpi['start_pin_violations']} 件",
            "固定と依存の矛盾なし" if kpi["start_pin_violations"] == 0 else "固定日どおりに配置できていません",
            alert=kpi["start_pin_violations"] > 0,
        )

    def _render_milestone_table(self, result_df, display, milestones, today):
        mode = self._breakdown_mode
        if mode == _BREAKDOWN_TEAM:
            dim_ids = list(display["team_names"].keys())
            dim_labels = display["team_names"]
            column = "Team_ID"
            hint = "値はタスク件数。行方向の合計はタスク件数と一致する"
        elif mode == _BREAKDOWN_WORKFLOW:
            dim_ids = list(display["workflow_names"].keys())
            dim_labels = display["workflow_names"]
            column = "Workflow_ID"
            hint = "値はタスク件数。行方向の合計はタスク件数と一致する"
        else:
            dim_ids, dim_labels, column = None, None, None
            hint = "ジョブ件数は延べ（1ジョブが複数マイルストーンにまたがりうる）"
        self.breakdown_hint_label.setText(hint)

        if mode == _BREAKDOWN_ALL:
            extra_columns = ["ジョブ", "タスク", "完了", "進行中", "未着手"]
        else:
            extra_columns = [dim_labels[d] for d in dim_ids]
        headers = _BASE_COLUMNS + extra_columns
        self.milestone_table.setColumnCount(len(headers))
        self.milestone_table.setHorizontalHeaderLabels(headers)

        base_rows = compute_milestone_rows(result_df, milestones, today)
        if mode == _BREAKDOWN_ALL:
            breakdown_rows = compute_milestone_breakdown_all(result_df, milestones, today)
        else:
            breakdown_rows = compute_milestone_breakdown_by(result_df, milestones, column, dim_ids)

        self.milestone_table.setRowCount(len(base_rows))
        for row_index, (base, breakdown) in enumerate(zip(base_rows, breakdown_rows)):
            self._set_item(row_index, 0, base["name"])
            self._set_item(row_index, 1, _fmt_date(base["due_date"]))
            self._set_item(row_index, 2, f"{base['remaining_days']}日" if base["remaining_days"] else "—")
            self._set_item(row_index, 3, _fmt_date(base["last_end_date"]))

            slack = base["slack_days"]
            slack_text = "—" if slack is None else (f"+{slack}日" if slack >= 0 else f"{slack}日")
            slack_item = QTableWidgetItem(slack_text)
            if slack is not None and slack < 0:
                slack_item.setForeground(_ALERT_COLOR)
            slack_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.milestone_table.setItem(row_index, 4, slack_item)

            overrun_item = QTableWidgetItem(_fmt_int(base["overrun_count"]) if base["overrun_count"] else "—")
            if base["overrun_count"]:
                overrun_item.setForeground(_ALERT_COLOR)
            overrun_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.milestone_table.setItem(row_index, 5, overrun_item)

            judge_item = QTableWidgetItem("○" if base["on_time"] else "×")
            judge_item.setTextAlignment(Qt.AlignCenter)
            if not base["on_time"]:
                judge_item.setForeground(_ALERT_COLOR)
            self.milestone_table.setItem(row_index, 6, judge_item)

            if mode == _BREAKDOWN_ALL:
                self._set_item(row_index, 7, _fmt_int(breakdown["jobs"]), right=True)
                self._set_item(row_index, 8, _fmt_int(breakdown["tasks"]), right=True)
                self._set_item(row_index, 9, _fmt_int(breakdown["done"]), right=True)
                self._set_item(row_index, 10, _fmt_int(breakdown["in_progress"]), right=True)
                self._set_item(row_index, 11, _fmt_int(breakdown["not_started"]), right=True)
            else:
                for col_offset, dim_id in enumerate(dim_ids):
                    value = breakdown[dim_id]
                    text = _fmt_int(value) if value else "—"
                    self._set_item(row_index, len(_BASE_COLUMNS) + col_offset, text, right=True)

        auto_size_columns(self.milestone_table, stretch_last=False)

    def _set_item(self, row, column, text, right=False):
        item = QTableWidgetItem(text)
        if right:
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.milestone_table.setItem(row, column, item)

    # -- Undo/Redo用の選択・表示状態 -------------------------------------------------

    def capture_ui_state(self):
        return {"breakdown_mode": self._breakdown_mode}

    def restore_ui_state(self, state):
        if not state:
            return
        mode = state.get("breakdown_mode")
        if mode not in (_BREAKDOWN_ALL, _BREAKDOWN_TEAM, _BREAKDOWN_WORKFLOW):
            return
        self._breakdown_mode = mode
        self._breakdown_buttons[mode].setChecked(True)
        self._render()
