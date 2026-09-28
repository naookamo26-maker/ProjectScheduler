# 用語集（多言語対応）

GUIの文言とメッセージを英語・ベトナム語・中国語（簡体字）に訳すときの訳語
（`docs/roadmap.md` §11）。訳のぶれを防ぐため、**翻訳はこの表に必ず従う**。
新しい用語を使うときは、先にここへ足してから訳す。

仕組みは `i18n.py`（翻訳関数 `tr()`）と `locales/<言語>.json`（{原文: 訳}）。
訳の抜けは `tests/test_i18n.py` が検出する。

## 表記の決まり

- 日付は全言語で `2026-09-24` 形式（月日だけのときは `09-24` ではなく原文と同じ `09/24`）
- 件数・日数などの数は訳文でも `{n}` のまま差し込む。単位は各言語の自然な形にする
  （英語は単数・複数を区別しない書き方を選ぶ。例: `Tasks: {n}`、`{n} day(s)` は使わない）
- 英語のボタン・メニューは先頭だけ大文字（Sentence case。例: `Add job`）
- 「」で囲んだ名前は、英語・ベトナム語では `"…"`、中国語では `“…”` にする
- 省略記号「…」はそのまま使う

## 計画・日程

| 日本語 | English | Tiếng Việt | 简体中文 |
|---|---|---|---|
| 確定（する） | commit | chốt | 确定 |
| 確定済み | Committed | Đã chốt | 已确定 |
| 未確定 | Uncommitted | Chưa chốt | 未确定 |
| 確定した日程 | committed schedule | lịch đã chốt | 已确定的日程 |
| 変更案 | Draft | Bản nháp | 变更草案 |
| 変更を確定 | Commit changes | Chốt thay đổi | 确定变更 |
| 変更を破棄 | Discard changes | Hủy thay đổi | 放弃变更 |
| 未確定に戻す | Revert to uncommitted | Trở về chưa chốt | 恢复为未确定 |
| 全面再計画 | Full replan | Lập lại toàn bộ kế hoạch | 全面重新排程 |
| 基準日 | base date | ngày gốc | 基准日 |
| 影響範囲 | affected range | phạm vi ảnh hưởng | 影响范围 |
| 日程 | schedule | lịch | 日程 |
| 開始日 / 終了日 | start date / end date | ngày bắt đầu / ngày kết thúc | 开始日 / 结束日 |
| 開発開始日 | development start date | ngày bắt đầu phát triển | 开发开始日 |
| 営業日 | working day | ngày làm việc | 工作日 |
| 休業日 | non-working day | ngày nghỉ | 休息日 |
| 締切 | deadline | hạn chót | 截止日期 |
| 締切超過 | overrun | trễ hạn | 超期 |
| 開始固定日 / 手動ピン | pinned start date / pin | ngày bắt đầu cố định / ghim | 固定开始日 / 固定 |
| 配置 | Placement | Bố trí | 排布 |
| 最速 / ギリギリ | Earliest / Latest | Sớm nhất / Muộn nhất | 最早 / 最晚 |
| 違反 | violation | vi phạm | 冲突 |

## 構成要素

| 日本語 | English | Tiếng Việt | 简体中文 |
|---|---|---|---|
| プロジェクト | project | dự án | 项目 |
| ジョブ | job | hạng mục | 作业 |
| タスク | task | tác vụ | 任务 |
| ワークフロー | workflow | quy trình | 工作流 |
| マイルストーン | milestone | mốc | 里程碑 |
| チーム | team | nhóm | 团队 |
| ライン（同時ライン数） | line (concurrent lines) | luồng (số luồng đồng thời) | 线 (并行线数) |
| 依存（関係） | dependency | phụ thuộc | 依赖 |
| 先行タスク / 後続タスク | predecessor / successor | tác vụ trước / tác vụ sau | 前置任务 / 后续任务 |
| 依存先ジョブ | depended-on job | hạng mục phụ thuộc | 依赖的作业 |
| 依存テンプレート | dependency template | mẫu phụ thuộc | 依赖模板 |
| ラグ | lag | độ trễ | 滞后 |
| タスク対応（依存先ジョブの先行→本ジョブの後続） | task mapping | ánh xạ tác vụ | 任务对应 |
| 変動点（同時ライン数の変更点） | change point | điểm thay đổi | 变更点 |
| 優先度 | priority | độ ưu tiên | 优先级 |
| タグ | tag | thẻ | 标签 |
| 上書き | override | ghi đè | 覆盖 |
| 既定 | default | mặc định | 默认 |
| 有効 / 無効 | enabled / disabled | bật / tắt | 启用 / 停用 |
| 状態（タスクの進捗） | status | trạng thái | 状态 |
| 未着手 / 進行中 / 完了 | Not started / In progress / Done | Chưa bắt đầu / Đang thực hiện / Hoàn thành | 未开始 / 进行中 / 已完成 |

## 画面・操作

| 日本語 | English | Tiếng Việt | 简体中文 |
|---|---|---|---|
| 基本情報設定 | Basic settings | Thiết lập cơ bản | 基本设置 |
| ワークフロー設計 | Workflow design | Thiết kế quy trình | 工作流设计 |
| ジョブ作成 | Jobs | Tạo hạng mục | 作业创建 |
| ガントチャート | Gantt chart | Biểu đồ Gantt | 甘特图 |
| プロジェクト分析 | Project analysis | Phân tích dự án | 项目分析 |
| 状態帯 | status bar | thanh trạng thái | 状态栏 |
| 絞り込み | Filter | Lọc | 筛选 |
| 並び（ガントのジョブの並び順） | Order | Thứ tự | 顺序 |
| 並べ直す | Re-sort | Sắp xếp lại | 重新排序 |
| 昇順 / 降順 | Ascending / Descending | Tăng dần / Giảm dần | 升序 / 降序 |
| 追加 / 複製 / 削除 | Add / Duplicate / Delete | Thêm / Nhân bản / Xóa | 添加 / 复制 / 删除 |
| 元に戻す / やり直す | Undo / Redo | Hoàn tác / Làm lại | 撤销 / 重做 |
| オプション | Options | Tùy chọn | 选项 |
| ヘルプ | Help | Trợ giúp | 帮助 |
| バージョン情報 / バージョン | About / Version | Giới thiệu / Phiên bản | 关于 / 版本 |
| キャンセル | Cancel | Hủy | 取消 |
