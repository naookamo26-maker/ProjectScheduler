# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A resource-constrained project scheduler. Given projects/milestones/teams/workflows/jobs (with team
concurrency limits and holidays), it computes a realistic schedule and renders it as a Mermaid Gantt chart
(Markdown) and a standalone interactive HTML Gantt chart (Plotly, no server required).

Two ways to build the input data:
- **GUI (primary)**: a PySide6 desktop app (`gui/`) backed by a SQLite file (`.pschedule`). This is what
  most work in this repo touches.
- **Excel + CLI**: edit a multi-sheet `.xlsx` and run `project_scheduler.py` directly (legacy/scriptable path).

## Commands

```bash
pip install -r requirements.txt   # pandas, openpyxl, plotly, PySide6
pip install pytest                # not in requirements.txt; needed only for the test suite

python run_gui.py                                          # launch the GUI
python project_scheduler.py [xlsx] -o out.md --html-output out.html   # CLI path (defaults to the sample xlsx in data/)

pytest tests/ -q                                            # run the whole suite
pytest tests/test_gui_gantt_smoke.py::test_name -v          # run a single test
```

There is no linter/formatter configured in this repo (no ruff/flake8/pyproject config) — match the
surrounding code's style rather than introducing one.

### Testing notes

`tests/test_gui_gantt_smoke.py` only exercises the Qt-independent layers (`gui/db.py`,
`gui/gantt_generator.py`, `project_scheduler.py`) — plain pytest, no Qt event loop. Widget-layer code
(anything in `gui/*.py` that touches `QGraphicsScene`/`QWidget` rendering, zoom/pan math, dialogs) has no
automated test coverage by design (pytest-qt is explicitly out of scope — see `docs/requirements.md`).
Verify that code by running a small script under headless Qt instead:

```bash
QT_QPA_PLATFORM=offscreen python3 your_check_script.py
```

A typical script opens `ProjectDatabase.create_new()`/`open_existing(...)`, builds the widget under test
(e.g. `GanttTab(db)`, `JobsTab(db)`), calls `app.processEvents()`, and either asserts on the resulting Qt
object graph or calls `.grab().save(path)` to produce a screenshot for visual review. `data/Project_Schedule_Sample_GameDev_v22.pschedule`
is a ready-made sample project for this.

## Architecture

### Two halves, one shared scheduling core

```
project_scheduler.py        scheduling engine (Excel-CLI entry points + the shared engine)
  ├─ run_resource_constrained_scheduler(excel_file, ...)              legacy Excel/CLI entry point
  ├─ run_resource_constrained_scheduler_from_frames(df_project, ...)  GUI entry point — no Excel involved
  └─ _run_scheduler_on_frames(...)                                    shared scheduling body used by both

gui/                         PySide6 desktop app, edits a .pschedule (SQLite) file
  ├─ db.py                     ProjectDatabase: schema + CRUD (no Qt dependency — testable standalone)
  ├─ main.py                   MainWindow, tab assembly, File menu, drag & drop
  ├─ tab_basic_info.py         Tab 1: project/milestones/teams/holidays
  ├─ tab_workflows.py          Tab 2: workflows + dependency templates (hosts node_canvas.py)
  ├─ node_canvas.py            node-graph editor for task dependencies within a workflow
  ├─ tab_jobs.py                Tab 3: jobs, task overrides, cross-job dependencies
  ├─ tab_gantt.py                Tab 4: in-app Gantt preview (uses gantt_view.py)
  ├─ gantt_view.py                 Gantt QGraphicsScene building + the frozen-pane zoom/pan view
  ├─ widgets_common.py           shared UI building blocks (CrudSection, combo/spinbox helpers)
  └─ gantt_generator.py         DB → project_scheduler.py DataFrames → chart files
```

`_load_data_from_frames(...)` in `project_scheduler.py` is the shared validation/normalization entry
point for both Excel- and DB-sourced data; `_load_data(excel_file)` is a thin Excel-only wrapper around it.
This split is why the GUI can call straight into the scheduling engine without ever touching Excel I/O.

### GUI → scheduler data flow

`gui/gantt_generator.py`'s `build_frames(db)` reads every table out of `ProjectDatabase` into a
`dict[str, DataFrame]` and calls `run_resource_constrained_scheduler_from_frames(...)`. Internal integer
primary keys are converted to the string IDs the scheduler expects (e.g. `WF_001`) only inside
`build_frames`, immediately before generation — those string IDs never reach the GUI layer.
`validate_for_generation(db)` should be called before generation to catch an incomplete project (missing
project name/date, zero teams/milestones/workflows/jobs) and surface it as a plain error list instead of
letting an empty DataFrame fail deep inside the scheduler with a confusing exception.

### Data model conventions (`gui/db.py`)

- Every table uses an `INTEGER PRIMARY KEY AUTOINCREMENT`. The GUI never shows raw IDs — every reference
  (team, workflow, milestone, task, job) is chosen and displayed by name.
- `job_task_overrides` stores diffs only: a job's tasks are implicitly all of its workflow's
  `workflow_tasks`; a row is created only when a task is disabled or its days/team/milestone is overridden,
  and deleted again when reverted to default (`UNIQUE(job_id, workflow_task_id)`).
- Deletion integrity is enforced in the Python layer, not just via `ON DELETE` clauses: delete methods
  (`delete_team`, `delete_workflow`, ...) check usage counts first and raise `ReferencedEntityError` to
  block deletion where referenced rows would be orphaned; the GUI turns that into a dialog rather than
  surfacing a raw `sqlite3.IntegrityError`.
- Schema evolution goes through `schema_meta.schema_version` + `ProjectDatabase._migrate_schema()`, applied
  automatically in `open_existing()`. Add new migration steps there rather than assuming a fresh schema.
- The DDL in `_SCHEMA_SQL` is the source of truth for the schema; `docs/db_design.md` is a prose summary
  that should be kept in sync with it, not the other way around.

### Dependency templates auto-expand into job-pair dependencies

`workflow_dependency_templates` holds workflow-pair-level defaults ("workflow A's task X waits on workflow
B's task Y"). When a job-to-job dependency is added (`job_dependency_links`), matching templates expand
automatically into `job_external_dependencies` rows tagged with `source_link_id` pointing back at the link
that generated them (`source_link_id IS NULL` means the user added that task-pair by hand). Because a
template can be added/edited/deleted, or a job's workflow reassigned, well after a link already exists,
`ProjectDatabase.sync_dependency_templates()` re-derives every auto-generated pair from the current
templates and is called from every mutation point that could invalidate that mapping (template add/edit/
delete, job workflow reassignment, dependency link add) — plus once more defensively whenever the Jobs tab
becomes active. When adding a new way to mutate templates or job workflows, call it there too rather than
relying on the tab-activation refresh alone.

Two independent circular-dependency checks exist and are **not** interchangeable:
- `node_canvas.py`'s `WorkflowGraphScene` BFS-checks the in-workflow task graph before accepting a new
  edge (immediate UI rejection).
- Cross-job dependencies (`job_external_dependencies`) and workflow-template cycles are instead caught
  later — the former at Gantt generation time via `_build_scheduling_order()`'s `CircularDependencyError`,
  the latter at add/edit time via `_would_create_workflow_template_cycle`.

### Gantt view: frozen panes + anisotropic zoom

`gui/gantt_view.py`'s `FrozenGanttPane` renders the Gantt chart as three separate `QGraphicsScene`s
(header/column/body — see `GanttScenes`) synced by `_sync_panes()`, so the date header and left item-name
column stay fixed while the body scrolls/zooms, without the scenes bleeding into each other. Horizontal and
vertical zoom scale independently (`Qt.IgnoreAspectRatio`), so `sx`/`sy` differ in general.

Any label using `ItemIgnoresTransformations` (used everywhere here to keep text a fixed pixel size
regardless of zoom) still has its `setPos()` interpreted in *scene* coordinates, which **are** affected by
the view's zoom transform — but the label's own pixel width is not. Concretely: an item's `setPos()` offset
gets multiplied by the zoom scale when rendered, so a fixed-pixel centering offset must be divided by the
current scale factor to land correctly, and it must be recomputed every time zoom changes (baking it in at
scene-build time only works for the zoom level active at that moment). The established pattern —
`_center_milestone_labels(sx)`, `_center_tick_labels(sx)`, `_center_task_labels(sx, sy)` — recomputes
`label.setPos(anchor - (width_px / n) / sx, ...)` from `FrozenGanttPane._sync_panes()` on every zoom/pan.
Any new zoom-independent label that needs pixel-accurate positioning relative to a scene coordinate must
follow this same pattern (store `(label, target_scene_x)` on the scene at build time, recompute the
position in a `_sync_panes`-called method), not a one-off offset computed in `build_gantt_scenes()`.

### Explicit save, in-memory DB

`ProjectDatabase` runs against an in-memory SQLite connection; every mutating method commits to that
in-memory connection immediately (`self._commit()` sets `_dirty = True` and fires `on_change`), but nothing
is written to the `.pschedule` file until `save()`/`save_as()` is called explicitly (Ctrl+S / Ctrl+Shift+S).
`ProjectDatabase.create_new()` can be called with no path (`path=None`) to open an untitled project; `save()`
raises if called while `path` is still `None` — the GUI redirects that case to the save-as path picker
rather than ever calling `save()` on a path-less DB (see `gui/main.py`'s `on_save`).

### Team/workflow colors

`project_scheduler.py`'s `_TEAM_COLOR_PALETTE` is a fixed 12-color categorical palette (desaturated,
colorblind-simulation-checked) used app-wide for team/workflow color coding, both in the GUI and in
generated charts. Entities beyond the 12th fall back to a neutral gray (`_TEAM_COLOR_OVERFLOW`). Change
this palette in one place only — it's shared, not duplicated per view.
