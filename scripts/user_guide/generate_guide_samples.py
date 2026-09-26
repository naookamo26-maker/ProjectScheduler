"""利用者向けドキュメント（docs/user_guide/）の画面撮影に使うサンプルを生成する。

ドキュメントは特定の業界に寄せない方針（docs/user_guide/outline.md）なので、
業界を問わない題材で、活用例（8章）の2つに合わせた2ファイルを作る。

- data/Guide_Sample_NewProduct.pschedule   「新製品の立ち上げ」
    大きな計画を粗い粒度（1タスク＝数週間）で組んだ例。見通しと合意に使う
- data/Guide_Sample_SystemRollout.pschedule 「社内システムの導入」
    小さな計画を細かい粒度（1タスク＝数日）で組んだ例。進み具合（状態・
    開始固定日）を反映した途中の状態にしてある

    python scripts/user_guide/generate_guide_samples.py

内容は決定的（乱数を使わない）で、何度実行しても同じファイルになる。
画面に写るので、名前・日付は読み手にとって自然なものにしておくこと。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gui.db import ProjectDatabase  # noqa: E402

NEW_PRODUCT = ROOT / "data" / "Guide_Sample_NewProduct.pschedule"
SYSTEM_ROLLOUT = ROOT / "data" / "Guide_Sample_SystemRollout.pschedule"

# ライン数の None は「指定なし」（上限を設けない。新規チームの既定）


# ---------------------------------------------------------------------------
# A. 新製品の立ち上げ（粗い粒度）
# ---------------------------------------------------------------------------

NP_PROJECT = ("新製品の立ち上げ", "2026-10-05")

NP_TEAMS = [
    # (名前, ライン数, [(変動の開始日, ライン数), …])
    ("企画", None, []),
    ("設計", 3, []),
    ("調達", 3, []),
    ("試作", 3, []),
    ("品質保証", 3, []),
    # 工場の立ち上げ前は稼働しない（0）→ 立ち上げ後に2本
    ("製造", 0, [("2027-02-01", 2)]),
    ("営業", None, []),
    ("マーケティング", 2, []),
    ("サポート", 1, []),
]

NP_MILESTONES = [
    ("企画承認", "2026-11-30", "製品コンセプトと事業計画の承認"),
    ("試作完了", "2027-05-31", "全モジュールの試作・評価が完了"),
    ("量産判定", "2027-08-31", "量産に移ってよいかの判定"),
    ("発売", "2027-11-30", ""),
]

NP_HOLIDAYS = [
    ("2026-12-29", None, "年末年始休業"),
    ("2026-12-30", None, "年末年始休業"),
    ("2026-12-31", None, "年末年始休業"),
    ("2027-01-04", None, "年末年始休業"),
    ("2027-08-12", None, "夏季休業"),
    ("2027-08-13", None, "夏季休業"),
    ("2027-08-16", "製造", "工場の設備点検"),
]

# ワークフロー: タスク (名前, チーム, 日数) と依存 (先行, 後続)
NP_WORKFLOWS = {
    "製品モジュール開発": (
        [
            ("要件定義", "企画", 15),
            ("基本設計", "設計", 20),
            ("詳細設計", "設計", 25),
            ("部品調達", "調達", 40),
            ("試作", "試作", 25),
            ("評価", "品質保証", 20),
        ],
        [
            ("要件定義", "基本設計"),
            ("基本設計", "詳細設計"),
            ("基本設計", "部品調達"),  # 長納期の部品は基本設計の段階で手配する
            ("詳細設計", "試作"),
            ("部品調達", "試作"),
            ("試作", "評価"),
        ],
    ),
    "量産準備": (
        [
            ("工程設計", "製造", 30),
            ("設備手配", "調達", 45),
            ("量産試作", "製造", 20),
            ("最終検査", "品質保証", 10),
        ],
        [
            ("工程設計", "設備手配"),
            ("設備手配", "量産試作"),
            ("量産試作", "最終検査"),
        ],
    ),
    "販売準備": (
        [
            ("販売計画", "営業", 15),
            ("販促物の制作", "マーケティング", 25),
            ("販売店への説明", "営業", 10),
            ("受注開始", "営業", 5),
        ],
        [
            ("販売計画", "販促物の制作"),
            ("販売計画", "販売店への説明"),
            ("販促物の制作", "受注開始"),
            ("販売店への説明", "受注開始"),
        ],
    ),
    "サポート準備": (
        [
            ("マニュアル作成", "サポート", 30),
            ("窓口の研修", "サポート", 15),
        ],
        [
            ("マニュアル作成", "窓口の研修"),
        ],
    ),
}

# 依存テンプレート: (ワークフロー, タスク) は (依存先ワークフロー, タスク) の完了を待つ
NP_TEMPLATES = [
    ("量産準備", "工程設計", "製品モジュール開発", "詳細設計"),
    ("量産準備", "量産試作", "製品モジュール開発", "評価"),
    ("販売準備", "販促物の制作", "製品モジュール開発", "評価"),
    ("サポート準備", "マニュアル作成", "製品モジュール開発", "詳細設計"),
]

# ジョブ: (名前, ワークフロー, マイルストーン, 優先度, タグ)
NP_JOBS = [
    ("本体", "製品モジュール開発", "試作完了", 1, ""),
    ("制御基板", "製品モジュール開発", "試作完了", 1, ""),
    ("電源ユニット", "製品モジュール開発", "試作完了", 2, ""),
    ("筐体", "製品モジュール開発", "試作完了", 2, ""),
    ("ソフトウェア", "製品モジュール開発", "試作完了", 1, ""),
    ("付属品", "製品モジュール開発", "量産判定", 5, ""),
    ("第1工場", "量産準備", "量産判定", 1, "国内"),
    ("第2工場", "量産準備", "発売", 3, "海外"),
    ("国内・直販", "販売準備", "発売", 2, "国内"),
    ("国内・代理店", "販売準備", "発売", 3, "国内"),
    ("北米", "販売準備", "発売", 4, "海外"),
    ("欧州", "販売準備", "発売", 4, "海外"),
    ("国内サポート", "サポート準備", "発売", 3, "国内"),
    ("海外サポート", "サポート準備", "発売", 5, "海外"),
]

# ジョブ間の依存: (ジョブ, 依存先ジョブ)。タスク単位の対応は依存テンプレートから展開される
NP_JOB_LINKS = [
    ("第1工場", "本体"),
    ("第1工場", "筐体"),
    ("第2工場", "本体"),
    ("第2工場", "筐体"),
    ("国内・直販", "本体"),
    ("国内・代理店", "本体"),
    ("北米", "本体"),
    ("欧州", "本体"),
    ("国内サポート", "ソフトウェア"),
    ("海外サポート", "ソフトウェア"),
]

# タスク上書き: (ジョブ, タスク, {列: 値})
NP_OVERRIDES = [
    # 要件定義は「企画承認」までに終える
    *[(module, "要件定義", {"milestone_id": "企画承認"})
      for module in ("本体", "制御基板", "電源ユニット", "筐体", "ソフトウェア", "付属品")],
    # 付属品は既製品を使うので部品調達が短い
    ("付属品", "部品調達", {"override_days": 10}),
    # 本体は評価項目が多い
    ("本体", "評価", {"override_days": 30}),
]


# ---------------------------------------------------------------------------
# B. 社内システムの導入（細かい粒度・進行中）
# ---------------------------------------------------------------------------

SR_PROJECT = ("社内システムの導入", "2026-10-05")

SR_TEAMS = [
    ("情報システム部", 2, []),
    ("業務部門", None, []),
    ("導入支援会社", 3, []),
]

SR_MILESTONES = [
    ("要件確定", "2026-10-30", ""),
    ("テスト完了", "2026-12-18", "全機能の受入テストが完了"),
    ("本番稼働", "2027-01-29", ""),
]

SR_HOLIDAYS = [
    ("2026-12-29", None, "年末年始休業"),
    ("2026-12-30", None, "年末年始休業"),
    ("2026-12-31", None, "年末年始休業"),
    ("2027-01-04", None, "年末年始休業"),
]

SR_WORKFLOWS = {
    "機能の導入": (
        [
            ("現行業務の確認", "業務部門", 3),
            ("要件の整理", "情報システム部", 4),
            ("設定・開発", "導入支援会社", 8),
            ("動作確認", "導入支援会社", 3),
            ("操作手順書", "情報システム部", 3),
            ("受入テスト", "業務部門", 4),
        ],
        [
            ("現行業務の確認", "要件の整理"),
            ("要件の整理", "設定・開発"),
            ("設定・開発", "動作確認"),
            ("設定・開発", "操作手順書"),
            ("動作確認", "受入テスト"),
            ("操作手順書", "受入テスト"),
        ],
    ),
    "部署への展開": (
        [
            ("説明会", "情報システム部", 1),
            ("操作研修", "情報システム部", 2),
            ("試行運用", "業務部門", 5),
            ("本番切り替え", "情報システム部", 1),
        ],
        [
            ("説明会", "操作研修"),
            ("操作研修", "試行運用"),
            ("試行運用", "本番切り替え"),
        ],
    ),
}

SR_TEMPLATES = [
    ("部署への展開", "説明会", "機能の導入", "受入テスト"),
]

SR_JOBS = [
    ("勤怠管理", "機能の導入", "テスト完了", 1, ""),
    ("経費精算", "機能の導入", "テスト完了", 1, ""),
    ("申請・承認", "機能の導入", "テスト完了", 2, ""),
    ("帳票出力", "機能の導入", "テスト完了", 3, ""),
    ("営業部", "部署への展開", "本番稼働", 1, ""),
    ("経理部", "部署への展開", "本番稼働", 1, ""),
    ("製造部", "部署への展開", "本番稼働", 2, ""),
]

SR_JOB_LINKS = [
    (dept, func)
    for dept in ("営業部", "経理部", "製造部")
    for func in ("勤怠管理", "経費精算", "申請・承認", "帳票出力")
]

# 進み具合を反映した途中の状態（定例ミーティングで更新した想定）
SR_OVERRIDES = [
    ("勤怠管理", "現行業務の確認", {"status": "done"}),
    ("勤怠管理", "要件の整理", {"status": "done"}),
    ("勤怠管理", "設定・開発", {"status": "in_progress"}),
    ("経費精算", "現行業務の確認", {"status": "done"}),
    ("経費精算", "要件の整理", {"status": "in_progress"}),
    # 担当者の都合で、実際の着手日が決まっている
    ("申請・承認", "現行業務の確認", {"start_pin_date": "2026-10-19"}),
    # 帳票出力は既存の仕組みを流用するので、操作手順書は作らない
    ("帳票出力", "操作手順書", {"is_active": False}),
]


def _build(path, project, teams, milestones, holidays, workflows, templates,
           jobs, job_links, overrides, distribution_ratio=None):
    if path.exists():
        path.unlink()
    db = ProjectDatabase.create_new(str(path))
    db.set_project(*project)
    if distribution_ratio is not None:
        db.set_distribution_ratio(distribution_ratio)

    team_ids = {}
    for name, lines, changes in teams:
        team_ids[name] = db.add_team(name, lines)
        for start_date, change_lines in changes:
            db.add_team_capacity_change(team_ids[name], start_date, change_lines)

    milestone_ids = {name: db.add_milestone(name, end, note) for name, end, note in milestones}
    for day, team, note in holidays:
        db.add_holiday(day, team_ids[team] if team else None, note)

    workflow_ids, task_ids = {}, {}
    for wf_name, (tasks, deps) in workflows.items():
        wf_id = db.add_workflow(wf_name)
        workflow_ids[wf_name] = wf_id
        for task_name, team, days in tasks:
            task_ids[(wf_name, task_name)] = db.add_workflow_task(wf_id, task_name, team_ids[team], days)
        for before, after in deps:
            db.add_task_dependency(wf_id, task_ids[(wf_name, before)], task_ids[(wf_name, after)])

    for wf, task, dep_wf, dep_task in templates:
        db.add_dependency_template(workflow_ids[wf], task_ids[(wf, task)],
                                   workflow_ids[dep_wf], task_ids[(dep_wf, dep_task)])

    job_ids, job_workflow = {}, {}
    for name, wf, milestone, priority, tags in jobs:
        job_ids[name] = db.add_job(name, workflow_ids[wf], milestone_ids[milestone], priority, tags)
        job_workflow[name] = wf

    for job, depends_on in job_links:
        db.add_job_dependency_link(job_ids[job], job_ids[depends_on])

    for job, task, fields in overrides:
        if "milestone_id" in fields:  # 原稿ではマイルストーンを名前で書く
            fields = {**fields, "milestone_id": milestone_ids[fields["milestone_id"]]}
        db.upsert_job_task_override(job_ids[job], task_ids[(job_workflow[job], task)], **fields)

    db.save()
    db.close()
    print(f"saved {path.relative_to(ROOT)}  "
          f"({len(jobs)}ジョブ / {sum(len(workflows[job_workflow[j]][0]) for j in job_ids)}タスク)")


def main():
    _build(NEW_PRODUCT, NP_PROJECT, NP_TEAMS, NP_MILESTONES, NP_HOLIDAYS, NP_WORKFLOWS,
           NP_TEMPLATES, NP_JOBS, NP_JOB_LINKS, NP_OVERRIDES,
           # 締切超過が2件（最大15日）だけ出る状態にする。超過の表示を紹介できる程度に留める
           distribution_ratio=0.0)
    _build(SYSTEM_ROLLOUT, SR_PROJECT, SR_TEAMS, SR_MILESTONES, SR_HOLIDAYS, SR_WORKFLOWS,
           SR_TEMPLATES, SR_JOBS, SR_JOB_LINKS, SR_OVERRIDES,
           # 進行中の計画なので、完了・進行中のタスクが先頭に来るよう「最速」に寄せる
           distribution_ratio=0.0)


if __name__ == "__main__":
    main()
