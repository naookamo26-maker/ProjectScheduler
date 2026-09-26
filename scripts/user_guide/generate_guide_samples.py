"""利用者向けドキュメント（docs/user_guide/）の画面撮影に使うサンプルを生成する。

ドキュメントの題材はゲーム開発に統一する（docs/user_guide/outline.md）。
活用例（8章）の2つに合わせた2ファイルを作る。

- data/Guide_Sample_NewTitle.pschedule 「新作タイトルの制作」
    大きな計画を粗い粒度（1タスク＝数週間）で組んだ例。見通しと合意に使う
- data/Guide_Sample_Update.pschedule   「アップデートの制作」
    小さな計画を細かい粒度（1タスク＝数日）で組んだ例。進み具合（状態・
    開始固定日）を反映した途中の状態にしてある

    python scripts/user_guide/generate_guide_samples.py

内容は決定的（乱数を使わない）で、何度実行しても同じファイルになる。
画面に写るので、名前・日付は読み手にとって自然なものにしておくこと。
同梱の Project_Schedule_Sample_GameDev_v22 は45ジョブあり紙面では読みにくいため、
撮影用はこれとは別に小さく作る。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gui.db import ProjectDatabase  # noqa: E402

NEW_TITLE = ROOT / "data" / "Guide_Sample_NewTitle.pschedule"
UPDATE = ROOT / "data" / "Guide_Sample_Update.pschedule"

# ライン数の None は「指定なし」（上限を設けない。新規チームの既定）


# ---------------------------------------------------------------------------
# A. 新作タイトルの制作（粗い粒度）
# ---------------------------------------------------------------------------

NT_PROJECT = ("新作タイトルの制作", "2026-10-05")

NT_TEAMS = [
    # (名前, ライン数, [(変動の開始日, ライン数), …])
    ("プランナー", None, []),
    ("コンセプトアート", 2, []),
    ("キャラクターモデル", 2, []),
    ("背景モデル", 2, []),
    ("モーション", 2, []),
    # 別タイトルの仕上げが終わってから合流する（それまでは 0）
    ("エフェクト", 0, [("2027-02-01", 2)]),
    ("プログラマー", None, []),
    ("サウンド", 1, []),
    ("QA", 2, []),
]

NT_MILESTONES = [
    ("企画承認", "2026-11-30", "コンセプトと開発計画の承認"),
    ("アルファ版", "2027-05-31", "主要な遊びが一通り動く"),
    ("ベータ版", "2027-08-31", "全コンテンツが入った状態"),
    ("マスターアップ", "2027-11-30", ""),
]

NT_HOLIDAYS = [
    ("2026-12-29", None, "年末年始休業"),
    ("2026-12-30", None, "年末年始休業"),
    ("2026-12-31", None, "年末年始休業"),
    ("2027-01-04", None, "年末年始休業"),
    ("2027-08-12", None, "夏季休業"),
    ("2027-08-13", None, "夏季休業"),
    ("2027-06-01", "モーション", "モーションキャプチャ機材の入れ替え"),
]

# ワークフロー: タスク (名前, チーム, 日数) と依存 (先行, 後続)
NT_WORKFLOWS = {
    "キャラクター制作": (
        [
            ("設定・仕様", "プランナー", 10),
            ("コンセプトアート", "コンセプトアート", 15),
            ("モデル制作", "キャラクターモデル", 30),
            ("モーション制作", "モーション", 25),
            ("ボイス収録", "サウンド", 10),
            ("組み込み", "プログラマー", 10),
            ("確認", "QA", 5),
        ],
        [
            ("設定・仕様", "コンセプトアート"),
            ("コンセプトアート", "モデル制作"),
            ("モデル制作", "モーション制作"),
            ("モデル制作", "ボイス収録"),  # 見た目が固まってから収録する。モーションと並行
            ("モーション制作", "組み込み"),
            ("ボイス収録", "組み込み"),
            ("組み込み", "確認"),
        ],
    ),
    "背景制作": (
        [
            ("レベルデザイン", "プランナー", 15),
            ("コンセプトアート", "コンセプトアート", 10),
            ("仮組み", "プログラマー", 10),
            ("背景モデル", "背景モデル", 40),
            ("仕上げ", "プログラマー", 10),
            ("確認", "QA", 5),
        ],
        [
            ("レベルデザイン", "コンセプトアート"),
            ("レベルデザイン", "仮組み"),  # 遊びの確認は仮の地形で先に進める
            ("コンセプトアート", "背景モデル"),
            ("背景モデル", "仕上げ"),
            ("仮組み", "仕上げ"),
            ("仕上げ", "確認"),
        ],
    ),
    "カットシーン制作": (
        [
            ("絵コンテ", "プランナー", 10),
            ("モーション収録", "モーション", 15),
            ("エフェクト", "エフェクト", 10),
            ("編集・仕上げ", "プログラマー", 10),
            ("確認", "QA", 3),
        ],
        [
            ("絵コンテ", "モーション収録"),
            ("モーション収録", "エフェクト"),
            ("エフェクト", "編集・仕上げ"),
            ("編集・仕上げ", "確認"),
        ],
    ),
}

# 依存テンプレート: (ワークフロー, タスク) は (依存先ワークフロー, タスク) の完了を待つ
NT_TEMPLATES = [
    # 登場キャラクターのモデルが無いと収録できない
    ("カットシーン制作", "モーション収録", "キャラクター制作", "モデル制作"),
    # 舞台となる背景が仕上がってから編集する
    ("カットシーン制作", "編集・仕上げ", "背景制作", "背景モデル"),
]

# ジョブ: (名前, ワークフロー, マイルストーン, 優先度, タグ)
NT_JOBS = [
    ("主人公", "キャラクター制作", "アルファ版", 1, "メイン"),
    ("ヒロイン", "キャラクター制作", "アルファ版", 1, "メイン"),
    ("ライバル", "キャラクター制作", "ベータ版", 2, "メイン"),
    ("師匠", "キャラクター制作", "ベータ版", 3, "サブ"),
    ("魔王", "キャラクター制作", "ベータ版", 2, "メイン"),
    ("始まりの村", "背景制作", "アルファ版", 1, ""),
    ("森のダンジョン", "背景制作", "アルファ版", 2, ""),
    ("王都", "背景制作", "ベータ版", 3, ""),
    # 体験版で見せるためアルファ版に前倒しした。締切超過の例になる
    ("魔王城", "背景制作", "アルファ版", 3, ""),
    ("オープニング", "カットシーン制作", "ベータ版", 2, "メイン"),
    ("旅立ち", "カットシーン制作", "ベータ版", 4, "サブ"),
    ("エンディング", "カットシーン制作", "マスターアップ", 3, "メイン"),
]

# ジョブ間の依存: (ジョブ, 依存先ジョブ)。タスク単位の対応は依存テンプレートから展開される
NT_JOB_LINKS = [
    ("オープニング", "主人公"),
    ("オープニング", "始まりの村"),
    ("旅立ち", "主人公"),
    ("旅立ち", "師匠"),
    ("旅立ち", "始まりの村"),
    ("エンディング", "主人公"),
    ("エンディング", "魔王"),
    ("エンディング", "魔王城"),
]

# タスク上書き: (ジョブ, タスク, {列: 値})。マイルストーンは名前で書く
NT_OVERRIDES = [
    # 主人公とヒロインの設定は「企画承認」までに固める
    ("主人公", "設定・仕様", {"milestone_id": "企画承認"}),
    ("ヒロイン", "設定・仕様", {"milestone_id": "企画承認"}),
    # 魔王は形態変化があるのでモデル制作が長い
    ("魔王", "モデル制作", {"override_days": 45}),
    # 師匠は既存の声素材を流用するので収録しない
    ("師匠", "ボイス収録", {"is_active": False}),
]


# ---------------------------------------------------------------------------
# B. アップデートの制作（細かい粒度・進行中）
# ---------------------------------------------------------------------------

UP_PROJECT = ("冬のアップデートの制作", "2026-10-05")

UP_TEAMS = [
    ("プランナー", 2, []),
    ("アート", 3, []),
    ("プログラマー", 2, []),
    ("QA", 2, []),
]

UP_MILESTONES = [
    ("仕様確定", "2026-10-23", ""),
    ("実装完了", "2026-11-27", "全コンテンツの実装とテストが完了"),
    ("配信", "2026-12-18", ""),
]

UP_HOLIDAYS = [
    ("2026-11-20", "QA", "社内研修"),
]

UP_WORKFLOWS = {
    "追加コンテンツ制作": (
        [
            ("仕様作成", "プランナー", 3),
            ("データ作成", "プランナー", 4),
            ("アート制作", "アート", 8),
            ("実装", "プログラマー", 5),
            ("調整", "プランナー", 3),
            ("テスト", "QA", 4),
        ],
        [
            ("仕様作成", "データ作成"),
            ("仕様作成", "アート制作"),
            ("データ作成", "実装"),
            ("アート制作", "実装"),
            ("実装", "調整"),
            ("調整", "テスト"),
        ],
    ),
    "配信準備": (
        [
            ("告知素材", "アート", 3),
            ("ストア申請", "プランナー", 2),
            ("最終確認", "QA", 2),
            ("配信作業", "プログラマー", 1),
        ],
        [
            ("告知素材", "ストア申請"),
            ("ストア申請", "最終確認"),
            ("最終確認", "配信作業"),
        ],
    ),
}

UP_TEMPLATES = [
    # 告知素材は、コンテンツのアートが仕上がってから作る
    ("配信準備", "告知素材", "追加コンテンツ制作", "アート制作"),
    # 最終確認は、コンテンツのテストが終わってから
    ("配信準備", "最終確認", "追加コンテンツ制作", "テスト"),
]

UP_JOBS = [
    ("新キャラクター", "追加コンテンツ制作", "実装完了", 1, ""),
    ("新ステージ", "追加コンテンツ制作", "実装完了", 1, ""),
    ("イベントクエスト", "追加コンテンツ制作", "実装完了", 2, ""),
    ("新アイテム", "追加コンテンツ制作", "実装完了", 3, ""),
    ("国内版", "配信準備", "配信", 1, "国内"),
    ("海外版", "配信準備", "配信", 2, "海外"),
]

UP_JOB_LINKS = [
    (region, content)
    for region in ("国内版", "海外版")
    for content in ("新キャラクター", "新ステージ", "イベントクエスト", "新アイテム")
]

# 進み具合を反映した途中の状態（定例ミーティングで更新した想定）
UP_OVERRIDES = [
    ("新キャラクター", "仕様作成", {"status": "done"}),
    ("新キャラクター", "データ作成", {"status": "done"}),
    ("新キャラクター", "アート制作", {"status": "in_progress"}),
    ("新ステージ", "仕様作成", {"status": "done"}),
    ("新ステージ", "アート制作", {"status": "in_progress"}),
    # 担当プランナーが別案件を終えてから着手する日が決まっている
    ("イベントクエスト", "仕様作成", {"start_pin_date": "2026-10-14"}),
    # 新アイテムはアイコンだけなのでアート制作が短い
    ("新アイテム", "アート制作", {"override_days": 3}),
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
    _build(NEW_TITLE, NT_PROJECT, NT_TEAMS, NT_MILESTONES, NT_HOLIDAYS, NT_WORKFLOWS,
           NT_TEMPLATES, NT_JOBS, NT_JOB_LINKS, NT_OVERRIDES)
    _build(UPDATE, UP_PROJECT, UP_TEAMS, UP_MILESTONES, UP_HOLIDAYS, UP_WORKFLOWS,
           UP_TEMPLATES, UP_JOBS, UP_JOB_LINKS, UP_OVERRIDES,
           # 進行中の計画なので、完了・進行中のタスクが先頭に来るよう「最速」に寄せる
           distribution_ratio=0.0)


if __name__ == "__main__":
    main()
