"""
タブ5「プロジェクト分析」。

ガントチャートタブと共有する `ScheduleCache`（`gui/schedule_cache.py`）の
計算結果を集計するだけの表示専用タブ。自分では計算を起こさず、集計は
`gui/summary_metrics.py` の純粋関数に任せる——`docs/project_analysis_tab_design.md`
参照。

`ScheduleCache` はどちらのタブからでも起動できるため、ガントチャートタブを
一度も開いていない状態でこのタブを開いても、自分で計算を要求する
（`refresh_choices()` 内の `self.cache.ensure_fresh()`）。"""

import html

import pandas as pd
from PySide6.QtCore import Qt, QTimer
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
    compute_all_teams_row,
    compute_kpi,
    compute_milestone_breakdown_all,
    compute_milestone_cumulative_progress_pct,
    compute_milestone_rows,
    compute_team_summary_rows,
    week_starts,
    weekly_capacity,
    weekly_concurrency_by_team,
    weekly_peak_breakdown_by_team,
)
from gui.team_summary_view import TeamSummaryChartView, build_team_detail_scene, build_team_stacked_scene
from gui.widgets_common import NoWheelComboBox, auto_size_columns

_BREAKDOWN_ALL = "all"
_BREAKDOWN_TEAM = "team"
_BREAKDOWN_WORKFLOW = "workflow"
_BREAKDOWN_LABELS = [
    (_BREAKDOWN_ALL, "全体"),
    (_BREAKDOWN_TEAM, "チーム別"),
    (_BREAKDOWN_WORKFLOW, "ワークフロー別"),
]
_BREAKDOWN_EXTRA_COLUMNS = ["ジョブ", "タスク", "完了", "進行中", "未着手"]

_BASE_COLUMNS = ["マイルストーン", "進捗", "締切日", "最終終了日", "スラック", "超過"]

_TEAM_COLUMNS = ["チーム", "ピーク", "ピーク時期", "上限に張り付いた日数", "タスク件数", "押し出された件数", "超過件数"]
# チーム別サマリーの表の先頭「全チーム」行を、個別チームの行と見分けるための
# キー（Qt.UserRoleに入れる）。Noneのままだと「選択なし」と区別が付かない。
_ALL_TEAMS_KEY = "__all_teams__"

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
    def __init__(self, db, schedule_cache, parent=None):
        super().__init__(parent)
        self.db = db
        self.cache = schedule_cache
        self.cache.updated.connect(self._on_cache_updated)
        self._breakdown_mode = _BREAKDOWN_ALL
        # 「チーム別」「ワークフロー別」で個別に選んだ対象（それぞれ別々に覚えて
        # おき、モードを行き来しても選択が保たれるようにする）。
        self._selected_team_id = None
        self._selected_workflow_id = None
        # チーム別サマリーの表で選択中の行（詳細グラフに連動）。マイルストーン別
        # サマリーの「チーム別」内訳で選ぶ対象とは別の状態として持つ
        # （表・コンボが別ウィジェットのため、選択も独立させる）。
        self._selected_team_summary_id = None
        # _render_team_summary() が最後に描いた週次の系列（行選択が変わるたびに
        # detail_viewだけ作り直すために覚えておく——選択操作のたびに集計を
        # やり直す必要は無い）。
        self._team_summary_display = None
        self._team_summary_weeks = None
        self._team_summary_breakdown = None
        self._team_summary_concurrency = None
        self._team_summary_chart_context = None

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
        # 「チーム別」「ワークフロー別」のときだけ表示する、対象を1件選ぶコンボ
        # （すべてのチーム/ワークフローを列として並べるのではなく、選んだ1件の
        # 内訳を「全体」と同じ列構成で表示する）。
        self._dimension_combo = NoWheelComboBox()
        self._dimension_combo.currentIndexChanged.connect(self._on_dimension_changed)
        self._dimension_combo.setVisible(False)
        breakdown_bar.addWidget(self._dimension_combo)
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

        layout.addWidget(self.milestones_group)

        # -- チーム別サマリー ---------------------------------------------------------
        self.team_group = QGroupBox("チーム別サマリー")
        team_layout = QVBoxLayout(self.team_group)

        # グラフは1枚だけ。表の行選択で中身を差し替える——「全チーム」行なら
        # 全チームの積み上げ、個別チームの行ならそのチームの詳細（塗り＋上限の
        # 破線）。2枚並べると縦を食うわりに、同時に見比べる場面がほぼ無い。
        self.team_chart_label = QLabel("")
        team_layout.addWidget(self.team_chart_label)
        self.team_chart_view = TeamSummaryChartView()
        self.team_chart_view.setMinimumHeight(260)
        team_layout.addWidget(self.team_chart_view, 1)

        # 積み上げグラフの凡例。帯自体にチーム名は描き込まない
        # （gui/team_summary_view.py参照——帯が薄い区間では文字がはみ出して
        # 重なり、フィットで縮小されるほど読めなくなるため）。色とチーム名の
        # 対応は、この凡例と帯のツールチップで補う
        # （docs/project_analysis_tab_design.md §7-6「凡例とツールチップの
        # 文字で必ず補う」）。個別チームの表示中は色が1色なので隠す。
        self.team_legend_label = QLabel("")
        self.team_legend_label.setWordWrap(True)
        self.team_legend_label.setTextFormat(Qt.RichText)
        team_layout.addWidget(self.team_legend_label)

        self.team_table = QTableWidget(0, len(_TEAM_COLUMNS))
        self.team_table.setHorizontalHeaderLabels(_TEAM_COLUMNS)
        self.team_table.verticalHeader().setVisible(False)
        self.team_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.team_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.team_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.team_table.setMinimumHeight(160)
        self.team_table.itemSelectionChanged.connect(self._on_team_row_selected)
        team_layout.addWidget(self.team_table)

        layout.addWidget(self.team_group, 1)

    def _on_breakdown_changed(self, key):
        if key == self._breakdown_mode:
            return
        self._breakdown_mode = key
        self._render()

    def _on_dimension_changed(self, _index):
        selected = self._dimension_combo.currentData()
        if selected is None:
            return
        if self._breakdown_mode == _BREAKDOWN_TEAM:
            self._selected_team_id = selected
        elif self._breakdown_mode == _BREAKDOWN_WORKFLOW:
            self._selected_workflow_id = selected
        self._render()

    # -- 結果の取得（ScheduleCache経由。gui/schedule_cache.py参照） -----------------------

    def _on_cache_updated(self):
        """ScheduleCache の結果・エラーが更新されるたびに呼ばれる。

        自分が非表示の間は反映を後回しにする——次にこのタブへ切り替わった際、
        refresh_choices() が同期的に最新の内容を反映するため、ここで無駄に
        表を作り直す必要がない（gui/tab_gantt.py の _on_cache_updated と同じ考え方）。"""
        if not self.isVisible():
            return
        self._render()

    def refresh_choices(self):
        """タブに切り替わるたび（gui/main.py の _on_tab_changed）に呼ぶ。

        自分では計算せず、共有の ScheduleCache（gui/schedule_cache.py）に
        最新化を要求するだけ——DBの内容が変わっていなければ再計算しない
        （設計案の実測で集計自体は全体で0.1秒未満）。ガントチャートタブを
        一度も開いていなくても、ここで計算が起動する。"""
        self.cache.ensure_fresh()
        self._render()

    def _render(self):
        if self.cache.error_message is not None:
            self._show_status_only(self.cache.error_message, is_error=True)
            return
        if not self.cache.is_fresh():
            self._show_status_only("スケジューリング結果を計算中です...", is_error=False)
            return

        result_df, display = self.cache.result_df, self.cache.display
        self.status_label.setStyleSheet("")
        self.status_label.setText("ガントチャートタブの計算結果を集計しています。")

        milestones = [
            (mid, name, due) for mid, name, due in display["milestone_markers"]
            if mid != "PROJECT_START"
        ]
        project_start = self.db.get_project()["start_date"]
        project_start_ts = pd.Timestamp(project_start) if project_start else None
        task_status_map = display["task_status"]

        self._render_kpi(result_df, project_start_ts, milestones, task_status_map)
        self._render_milestone_table(result_df, display, milestones, task_status_map)
        self._render_team_summary(result_df, display, milestones, project_start_ts)

    def _show_status_only(self, message, is_error):
        """結果がまだ無い（エラー／計算中）ときの表示。タイル・表・グラフを
        すべて空にし、状況表示だけを message に差し替える。"""
        self.status_label.setStyleSheet("color: #b3261e;" if is_error else "")
        self.status_label.setText(message)
        for tile in (
            self.kpi_scale, self.kpi_period, self.kpi_status,
            self.kpi_peak, self.kpi_overrun, self.kpi_violation,
        ):
            tile.set_value("—", "")
        self.milestone_table.setRowCount(0)
        self.breakdown_hint_label.setText("")
        self._dimension_combo.setVisible(False)
        self.team_table.setRowCount(0)
        self.team_chart_view.setScene(None)
        self.team_chart_label.setText("")
        self.team_legend_label.setText("")
        self._clear_team_summary_cache()

    def _clear_team_summary_cache(self):
        self._team_summary_display = None
        self._team_summary_weeks = None
        self._team_summary_breakdown = None
        self._team_summary_concurrency = None
        self._team_summary_chart_context = None

    def _render_kpi(self, result_df, project_start_ts, milestones, task_status_map):
        kpi = compute_kpi(result_df, project_start_ts, milestones, task_status_map)

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

    def _render_milestone_table(self, result_df, display, milestones, task_status_map):
        mode = self._breakdown_mode
        filtered_df = self._sync_dimension_combo(mode, display, result_df)

        hint = "ジョブ件数は延べ（1ジョブが複数マイルストーンにまたがりうる）"
        if mode != _BREAKDOWN_ALL and filtered_df.empty:
            # 対象が1件も無い（例: ワークフローが登録されていない、選択中の
            # チーム/ワークフローにタスクが1件も無い）場合、内訳・進捗の列は
            # すべて0扱いになる——テーブル自体は表示したまま、理由をここで補う。
            hint = "選択中の対象にはタスクがありません（内訳・進捗は0になります）。" + hint
        self.breakdown_hint_label.setText(hint)
        headers = _BASE_COLUMNS + _BREAKDOWN_EXTRA_COLUMNS
        self.milestone_table.setColumnCount(len(headers))
        self.milestone_table.setHorizontalHeaderLabels(headers)

        # 基本列（締切・スラック等）は内訳モードによらず常にプロジェクト全体
        # （全チーム・全ワークフロー）の結果から計算する——「そのマイルストーンが
        # 間に合うか」はチーム別・ワークフロー別に絞り込んでも変わらない事実
        # のため（設計案参照）。内訳（右側の5列）・進捗（%）は選択対象で絞り込む
        # ——「進捗」はチーム別/ワークフロー別のときその対象の件数を基準
        # （＝100%）にする、という利用者の要望による。
        base_rows = compute_milestone_rows(result_df, milestones)
        progress_values = compute_milestone_cumulative_progress_pct(filtered_df, milestones)
        breakdown_rows = compute_milestone_breakdown_all(filtered_df, milestones, task_status_map)

        self.milestone_table.setRowCount(len(base_rows))
        for row_index, (base, progress_pct, breakdown) in enumerate(
            zip(base_rows, progress_values, breakdown_rows)
        ):
            self._set_item(row_index, 0, base["name"])
            self._set_item(row_index, 1, f"{progress_pct:.0f}%", right=True)
            self._set_item(row_index, 2, _fmt_date(base["due_date"]))
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

            self._set_item(row_index, 6, _fmt_int(breakdown["jobs"]), right=True)
            self._set_item(row_index, 7, _fmt_int(breakdown["tasks"]), right=True)
            self._set_item(row_index, 8, _fmt_int(breakdown["done"]), right=True)
            self._set_item(row_index, 9, _fmt_int(breakdown["in_progress"]), right=True)
            self._set_item(row_index, 10, _fmt_int(breakdown["not_started"]), right=True)

        auto_size_columns(self.milestone_table, stretch_last=False)

    def _render_team_summary(self, result_df, display, milestones, project_start_ts):
        """チーム別サマリー（表＋週次の系列の集計）を作り直す。グラフ本体は
        表の行選択に連動して `_render_team_chart()` が描くので、ここでは選択を
        （可能なら）維持したまま表を組み直し、系列を用意するまでに留める。"""
        team_names = display["team_names"]
        team_capacity_schedule = display["team_capacity_schedule"]
        # 先頭に「全チーム」行を足す。この行を選ぶと積み上げグラフ、個別チームの
        # 行を選ぶとそのチームの詳細グラフになる（グラフは1枚に統合してある）。
        rows = [compute_all_teams_row(result_df)] + compute_team_summary_rows(
            result_df, team_names, team_capacity_schedule,
        )

        self.team_table.blockSignals(True)
        self.team_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            name_item = QTableWidgetItem(row["name"])
            # 「全チーム」行は team_id が None なので、専用のキーで見分ける
            # （Noneのままだと「選択なし」と区別が付かない）。
            name_item.setData(
                Qt.UserRole, _ALL_TEAMS_KEY if row["team_id"] is None else row["team_id"],
            )
            if row["team_id"] is None:
                font = name_item.font()
                font.setBold(True)
                name_item.setFont(font)
            self.team_table.setItem(row_index, 0, name_item)
            self._set_team_item(row_index, 1, _fmt_int(row["peak"]) if row["peak"] else "—")
            self._set_team_item(
                row_index, 2, row["peak_month"].replace("-", "/") if row["peak_month"] else "—"
            )
            pinned = row["pinned_days"]
            self._set_team_item(row_index, 3, _fmt_int(pinned) if pinned is not None else "—")
            self._set_team_item(row_index, 4, _fmt_int(row["tasks"]))
            self._set_team_item(
                row_index, 5, _fmt_int(row["resource_adjusted"]) if row["resource_adjusted"] else "—"
            )
            overrun_item = QTableWidgetItem(_fmt_int(row["overrun"]) if row["overrun"] else "—")
            if row["overrun"]:
                overrun_item.setForeground(_ALERT_COLOR)
            overrun_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.team_table.setItem(row_index, 6, overrun_item)
        auto_size_columns(self.team_table, stretch_last=False)
        self.team_table.blockSignals(False)

        if result_df.empty:
            self.team_chart_view.setScene(None)
            self.team_chart_label.setText("")
            self.team_legend_label.setText("")
            self._clear_team_summary_cache()
            return

        # グラフのX軸は週次（日次のままだと変化点が営業日数ぶん並んでギザギザに
        # なり読めず、月次だと数か月の短いプロジェクトで点が数個しか並ばない。
        # gui/team_summary_view.py 参照）。集計は2種類を使い分ける——積み上げる
        # 全体グラフは「合計が実在した同時タスク数になる内訳」、個別チームは
        # そのチーム単独の週内最大（gui/summary_metrics.py の各docstring参照）。
        range_start = result_df["Start_Date"].min()
        range_end = result_df["End_Date"].max()
        weeks = week_starts(range_start, range_end)
        team_ids = list(team_names)

        self._team_summary_display = display
        self._team_summary_weeks = weeks
        self._team_summary_breakdown = weekly_peak_breakdown_by_team(
            result_df, team_ids, range_start, range_end,
        )
        self._team_summary_concurrency = weekly_concurrency_by_team(
            result_df, team_ids, range_start, range_end,
        )
        self._team_summary_chart_context = (milestones, project_start_ts)

        # 選択を可能な限り維持する。前回選んでいた対象が今回の表にもあれば
        # それを、無ければ（初回・チームが無くなった等）「全チーム」行を選ぶ。
        target_row = 0
        for row_index in range(self.team_table.rowCount()):
            if self.team_table.item(row_index, 0).data(Qt.UserRole) == self._selected_team_summary_id:
                target_row = row_index
                break
        if self.team_table.rowCount() > 0:
            self.team_table.selectRow(target_row)
        self._render_team_chart()

    def _set_team_item(self, row, column, text):
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.team_table.setItem(row, column, item)

    def _build_team_legend_html(self, team_names, team_colors):
        """積み上げグラフの凡例（色付きの四角＋チーム名を折り返しで並べる）。
        gui/team_summary_view.py が帯自体にチーム名を描き込まない代わりに、
        ここで色とチーム名の対応を示す（設計案§7-6）。"""
        swatches = [
            f'<span style="color:{team_colors.get(team_id, "#cbc9c2")};">■</span> {html.escape(name)}'
            for team_id, name in team_names.items()
        ]
        return "&nbsp;&nbsp;&nbsp;".join(swatches)

    def _on_team_row_selected(self):
        selected_items = self.team_table.selectedItems()
        self._selected_team_summary_id = (
            selected_items[0].data(Qt.UserRole) if selected_items else None
        )
        self._render_team_chart()

    def _render_team_chart(self):
        """表の行選択に連動してグラフ1枚の中身を差し替える。

        「全チーム」行なら全チームの積み上げ（上限の破線は描かない——上限は
        チームごとの設定で、積み上げた合計に対応する上限という概念が無いため）、
        個別チームの行ならそのチームの詳細（塗り＋折れ線＋上限の破線）。
        いずれも `_render_team_summary()` が集計済みの週次の系列を使い回すので、
        行選択のたびに集計し直さない。"""
        if self._team_summary_weeks is None:
            return
        selected_items = self.team_table.selectedItems()
        if not selected_items:
            self.team_chart_view.setScene(None)
            self.team_chart_label.setText("表の行を選択すると、同時タスク数の推移を表示します。")
            self.team_legend_label.setText("")
            return

        selected = selected_items[0].data(Qt.UserRole)
        display = self._team_summary_display
        team_names = display["team_names"]
        team_colors = display["team_colors"]
        weeks = self._team_summary_weeks
        milestones, project_start_ts = self._team_summary_chart_context

        if selected == _ALL_TEAMS_KEY:
            breakdown_by_team, totals = self._team_summary_breakdown
            scene = build_team_stacked_scene(
                weeks, breakdown_by_team, totals, team_colors, team_names,
                milestones, project_start_ts,
            )
            self.team_chart_label.setText("同時タスク数の推移（チーム別・積み上げ）")
            self.team_legend_label.setText(self._build_team_legend_html(team_names, team_colors))
        else:
            capacity_values = weekly_capacity(
                display["team_capacity_schedule"].get(selected, []), weeks,
            )
            scene = build_team_detail_scene(
                weeks, self._team_summary_concurrency.get(selected, []), capacity_values,
                team_colors.get(selected, "#cbc9c2"), milestones, project_start_ts,
            )
            name = team_names.get(selected, selected)
            self.team_chart_label.setText(
                f"同時タスク数の推移: {name}（塗り＝同時タスク数 / 破線＝設定上限）"
            )
            self.team_legend_label.setText("")  # 1色なので凡例は不要

        self.team_chart_view.setScene(scene)
        QTimer.singleShot(0, self.team_chart_view.fit_all)

    def _sync_dimension_combo(self, mode, display, result_df):
        """「チーム別」「ワークフロー別」のときだけ対象選択コンボを表示し、
        選択中の対象でresult_dfを絞り込んで返す（「全体」なら絞り込まず
        result_dfをそのまま返す）。"""
        if mode == _BREAKDOWN_ALL:
            self._dimension_combo.setVisible(False)
            return result_df

        column = "Team_ID" if mode == _BREAKDOWN_TEAM else "Workflow_ID"
        id_to_label = display["team_names"] if mode == _BREAKDOWN_TEAM else display["workflow_names"]
        current_id = self._selected_team_id if mode == _BREAKDOWN_TEAM else self._selected_workflow_id

        self._dimension_combo.setVisible(True)
        self._dimension_combo.blockSignals(True)
        self._dimension_combo.clear()
        for dim_id, label in id_to_label.items():
            self._dimension_combo.addItem(label, dim_id)
        idx = self._dimension_combo.findData(current_id) if current_id is not None else -1
        if idx < 0 and self._dimension_combo.count() > 0:
            idx = 0
        self._dimension_combo.setCurrentIndex(idx)
        selected_id = self._dimension_combo.currentData()
        self._dimension_combo.blockSignals(False)

        if mode == _BREAKDOWN_TEAM:
            self._selected_team_id = selected_id
        else:
            self._selected_workflow_id = selected_id

        if selected_id is None:
            return result_df.iloc[0:0]
        return result_df[result_df[column] == selected_id]

    def _set_item(self, row, column, text, right=False):
        item = QTableWidgetItem(text)
        if right:
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.milestone_table.setItem(row, column, item)

    # -- Undo/Redo用の選択・表示状態 -------------------------------------------------

    def capture_ui_state(self):
        return {
            "breakdown_mode": self._breakdown_mode,
            "selected_team_id": self._selected_team_id,
            "selected_workflow_id": self._selected_workflow_id,
            "selected_team_summary_id": self._selected_team_summary_id,
        }

    def restore_ui_state(self, state):
        if not state:
            return
        mode = state.get("breakdown_mode")
        if mode not in (_BREAKDOWN_ALL, _BREAKDOWN_TEAM, _BREAKDOWN_WORKFLOW):
            return
        self._breakdown_mode = mode
        self._selected_team_id = state.get("selected_team_id")
        self._selected_workflow_id = state.get("selected_workflow_id")
        self._selected_team_summary_id = state.get("selected_team_summary_id")
        self._breakdown_buttons[mode].setChecked(True)
        self._render()
