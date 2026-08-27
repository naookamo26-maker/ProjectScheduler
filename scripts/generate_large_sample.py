#!/usr/bin/env python3
"""大規模プロジェクト（約16,000タスク）のサンプル `.pschedule` を生成する。

AAA級のコンシューマ向けRPG「エルドラシル・サーガ：黄昏の継承者」（架空）の
制作進行を想定した内容にしてある。既存の小規模サンプル
（`data/Project_Schedule_Sample_GameDev_v22.pschedule`、340タスク）が
「機能を一通り触るための最小構成」なのに対し、こちらは

- スケジューリング・描画が実運用の規模で成立するかの確認
- チームのライン数変動、優先度、締切超過といった機能が意味を持つ規模での確認

に使う。生成は決定的（乱数シード固定）で、同じ内容のファイルが何度でも
再現できる。

使い方:
    python scripts/generate_large_sample.py [出力先パス]

既定の出力先は `data/Project_Schedule_Sample_AAA_Large.pschedule`。

規模の内訳（ワークフローのタスク数 × ジョブ数）:
    プレイアブルキャラクター  10 ×  26 =   260
    エネミー・ボス            10 × 180 = 1,800
    背景ロケーション          11 × 420 = 4,620
    プロップ・武具             7 × 520 = 3,640
    カットシーン              10 × 140 = 1,400
    クエスト・ミッション       9 × 200 = 1,800
    UI画面                     7 ×  90 =   630
    VFXアセット                6 × 190 = 1,140
    サウンドアセット           6 × 130 =   780
    乗り物・騎乗獣             8 ×  20 =   160
                                        -------
                                         16,230
"""

import itertools
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.db import ProjectDatabase  # noqa: E402

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "Project_Schedule_Sample_AAA_Large.pschedule"

PROJECT_NAME = "エルドラシル・サーガ：黄昏の継承者"
PROJECT_START = date(2026, 1, 5)  # 月曜

# -- チーム（名前, 開発開始時点の同時ライン数） --------------------------------------
TEAMS = [
    ("コンセプトアート", 9),
    ("キャラクターモデル", 5),
    ("背景モデル", 23),
    ("リギング", 2),
    ("アニメーション", 7),
    ("シネマティクス", 3),
    ("VFX", 9),
    ("テクニカルアート", 10),
    ("レベルデザイン", 9),
    ("ゲームプレイ実装", 9),
    ("UIデザイン", 3),
    ("UI実装", 3),
    ("サウンド", 7),
    ("ローカライズ", 3),
    ("QA", 7),
]

# -- チームの増減（適用開始日, ライン数）。制作フェーズに応じた増員・縮小を表す ---------
TEAM_CAPACITY_CHANGES = {
    # 立ち上げ（ヴァーティカルスライス）は少人数、ファーストプレイアブル以降で
    # 量産体制に入り、コンテンツロック後は制作系を縮小してQA・ローカライズに
    # 寄せる——という、実際のAAA制作でよくある増減の形にしてある。
    "背景モデル": [("2026-07-01", 48), ("2027-01-04", 59), ("2028-04-03", 28)],
    "テクニカルアート": [("2026-07-01", 20), ("2027-01-04", 24), ("2028-04-03", 12)],
    "VFX": [("2026-07-01", 19), ("2027-01-04", 23), ("2028-04-03", 12)],
    "ゲームプレイ実装": [("2026-07-01", 19), ("2027-01-04", 23), ("2028-04-03", 12)],
    "レベルデザイン": [("2026-07-01", 17), ("2027-01-04", 23), ("2028-04-03", 12)],
    "コンセプトアート": [("2026-07-01", 17), ("2027-01-04", 21), ("2028-04-03", 7)],
    "サウンド": [("2026-07-01", 17), ("2027-01-04", 21), ("2028-04-03", 12)],
    "アニメーション": [("2026-07-01", 16), ("2027-01-04", 20), ("2028-04-03", 7)],
    "キャラクターモデル": [("2026-07-01", 10), ("2027-01-04", 12), ("2028-04-03", 5)],
    "シネマティクス": [("2026-07-01", 7), ("2027-01-04", 10), ("2028-04-03", 5)],
    "リギング": [("2026-07-01", 3), ("2027-01-04", 5)],
    "UIデザイン": [("2026-07-01", 5), ("2028-04-03", 2)],
    "UI実装": [("2026-07-01", 5), ("2027-01-04", 6), ("2028-04-03", 3)],
    # QA・ローカライズは後工程なので、アルファ以降に段階的に増やす
    "QA": [("2026-07-01", 16), ("2027-01-04", 20), ("2028-04-03", 7)],
    "ローカライズ": [("2026-07-01", 6), ("2027-01-04", 9), ("2028-04-03", 3)],
}

# -- マイルストーン（名前, 締切日, 備考） ---------------------------------------------
MILESTONES = [
    ("ヴァーティカルスライス", "2026-06-30", "コアループを通しで遊べる縦切り版。社内レビュー用"),
    ("ファーストプレイアブル", "2026-12-18", "第1章を通しでプレイ可能。パブリッシャー向けデモ"),
    ("アルファ", "2027-06-30", "全機能実装完了。コンテンツは仮素材可"),
    ("ベータ", "2027-12-17", "全コンテンツ実装完了。以降は調整と不具合修正のみ"),
    ("コンテンツロック", "2028-03-31", "新規アセットの追加を停止"),
    ("マスターアップ", "2028-06-30", "ROM提出"),
]

# -- 休業日 ---------------------------------------------------------------------------
COMPANY_HOLIDAYS = []
for year in (2025, 2026, 2027, 2028):
    for offset in range(0, 6):  # 年末年始（12/29〜1/3）
        COMPANY_HOLIDAYS.append((date(year, 12, 29) + timedelta(days=offset), "年末年始休業"))
for year in (2026, 2027, 2028):  # GWの全社一斉休業（祝日に挟まれた平日）
    COMPANY_HOLIDAYS.append((date(year, 5, 1), "GW一斉休業"))
    COMPANY_HOLIDAYS.append((date(year, 5, 2), "GW一斉休業"))
COMPANY_HOLIDAYS += [
    (date(2026, 9, 25), "全社キックオフ・全体会議"),
    (date(2027, 4, 2), "アルファ版レビュー合宿"),
    (date(2027, 9, 17), "社内試遊会"),
]

TEAM_HOLIDAYS = [
    ("QA", date(2027, 3, 12), "テスト計画策定合宿"),
    ("QA", date(2027, 9, 10), "検証環境の全面入れ替え"),
    ("ローカライズ", date(2027, 8, 13), "海外拠点の夏季休業"),
    ("ローカライズ", date(2027, 11, 25), "海外拠点の祝日"),
    ("ローカライズ", date(2027, 11, 26), "海外拠点の祝日"),
    ("サウンド", date(2027, 6, 11), "スタジオ設備メンテナンス"),
    ("シネマティクス", date(2027, 2, 19), "モーションキャプチャ設備の校正"),
]

# -- ワークフロー（制作パイプライン） --------------------------------------------------
# (タスク名, 担当チーム, 標準所要日数) を上流から下流の順に並べる。
# 依存関係は隣り合うタスク間の直列（前工程が終わってから次工程）とする。
WORKFLOWS = {
    "プレイアブルキャラクター": [
        ("キャラクターコンセプト", "コンセプトアート", 8),
        ("ハイポリモデル", "キャラクターモデル", 12),
        ("リトポロジ・ローポリ", "キャラクターモデル", 6),
        ("テクスチャ・マテリアル", "キャラクターモデル", 7),
        ("リギング・スキニング", "リギング", 6),
        ("基本モーション", "アニメーション", 10),
        ("戦闘モーション", "アニメーション", 14),
        ("固有アビリティ実装", "ゲームプレイ実装", 8),
        ("専用エフェクト", "VFX", 5),
        ("プレイフィール調整", "QA", 5),
    ],
    "エネミー・ボス": [
        ("エネミーコンセプト", "コンセプトアート", 5),
        ("モデリング", "キャラクターモデル", 8),
        ("テクスチャ", "キャラクターモデル", 4),
        ("リギング", "リギング", 3),
        ("戦闘モーション", "アニメーション", 7),
        ("AI挙動実装", "ゲームプレイ実装", 6),
        ("攻撃エフェクト", "VFX", 4),
        ("戦闘サウンド実装", "サウンド", 3),
        ("バトルバランス調整", "レベルデザイン", 4),
        ("動作検証", "QA", 3),
    ],
    "背景ロケーション": [
        ("ロケーションコンセプト", "コンセプトアート", 6),
        ("ブロックアウト", "レベルデザイン", 5),
        ("地形・建築モデリング", "背景モデル", 12),
        ("プロップ配置", "背景モデル", 6),
        ("テクスチャ・マテリアル", "背景モデル", 7),
        ("ライティング", "テクニカルアート", 5),
        ("環境エフェクト", "VFX", 3),
        ("環境サウンド", "サウンド", 3),
        ("コリジョン・ナビメッシュ", "ゲームプレイ実装", 3),
        ("描画負荷の最適化", "テクニカルアート", 4),
        ("巡回検証", "QA", 3),
    ],
    "プロップ・武具": [
        ("デザイン画", "コンセプトアート", 2),
        ("モデリング", "背景モデル", 4),
        ("テクスチャ", "背景モデル", 3),
        ("LOD作成", "テクニカルアート", 2),
        ("ゲーム内実装", "ゲームプレイ実装", 2),
        ("装備エフェクト", "VFX", 2),
        ("表示確認", "QA", 1),
    ],
    "カットシーン": [
        ("絵コンテ", "シネマティクス", 4),
        ("プリビズ", "シネマティクス", 6),
        ("カメラワーク", "シネマティクス", 5),
        ("フェイシャルアニメーション", "アニメーション", 8),
        ("ボディアニメーション", "アニメーション", 10),
        ("エフェクト演出", "VFX", 5),
        ("シーンライティング", "テクニカルアート", 4),
        ("ボイス・BGM実装", "サウンド", 4),
        ("字幕・多言語対応", "ローカライズ", 3),
        ("再生検証", "QA", 3),
    ],
    "クエスト・ミッション": [
        ("クエスト設計", "レベルデザイン", 5),
        ("スクリプト実装", "ゲームプレイ実装", 6),
        ("会話テキスト執筆", "レベルデザイン", 4),
        ("ボイス収録・実装", "サウンド", 4),
        ("多言語テキスト対応", "ローカライズ", 4),
        ("UI連携", "UI実装", 2),
        ("報酬・難易度調整", "レベルデザイン", 3),
        ("進行不能バグ検証", "QA", 4),
        ("最終プレイ確認", "QA", 2),
    ],
    "UI画面": [
        ("ワイヤーフレーム", "UIデザイン", 3),
        ("ビジュアルデザイン", "UIデザイン", 5),
        ("アセット書き出し", "UIデザイン", 2),
        ("画面実装", "UI実装", 6),
        ("多言語レイアウト対応", "ローカライズ", 3),
        ("アニメーション演出", "UI実装", 3),
        ("操作検証", "QA", 2),
    ],
    "VFXアセット": [
        ("イメージボード", "コンセプトアート", 2),
        ("テクスチャ制作", "VFX", 3),
        ("エフェクト制作", "VFX", 5),
        ("シェーダー調整", "テクニカルアート", 3),
        ("ゲーム内実装", "ゲームプレイ実装", 2),
        ("表示検証", "QA", 1),
    ],
    "サウンドアセット": [
        ("音響仕様の整理", "サウンド", 2),
        ("素材収録・制作", "サウンド", 5),
        ("ミキシング", "サウンド", 3),
        ("ゲーム内実装", "サウンド", 3),
        ("多言語音声対応", "ローカライズ", 3),
        ("再生検証", "QA", 1),
    ],
    "乗り物・騎乗獣": [
        ("コンセプト", "コンセプトアート", 4),
        ("モデリング", "背景モデル", 9),
        ("テクスチャ", "背景モデル", 5),
        ("リギング", "リギング", 4),
        ("挙動アニメーション", "アニメーション", 8),
        ("操作・物理実装", "ゲームプレイ実装", 7),
        ("走行エフェクト", "VFX", 3),
        ("挙動検証", "QA", 3),
    ],
}

# -- ワークフローをまたぐ依存の既定ルール ----------------------------------------------
# (依存する側のワークフロー, そのタスク, 依存される側のワークフロー, そのタスク)
DEPENDENCY_TEMPLATES = [
    # カットシーンのアニメーションは、登場キャラのリグが出来ていないと着手できない
    ("カットシーン", "フェイシャルアニメーション", "プレイアブルキャラクター", "リギング・スキニング"),
    # クエストのスクリプトは、舞台となるロケーションが歩ける状態になってから
    ("クエスト・ミッション", "スクリプト実装", "背景ロケーション", "コリジョン・ナビメッシュ"),
    # エネミーの攻撃エフェクトは、共通VFXの素材が出来てから
    ("エネミー・ボス", "攻撃エフェクト", "VFXアセット", "エフェクト制作"),
    # 装備品のゲーム内実装は、装備するキャラのリグが確定してから
    ("プロップ・武具", "ゲーム内実装", "プレイアブルキャラクター", "リギング・スキニング"),
]


# =====================================================================================
# ジョブ名の生成
# =====================================================================================

REGIONS = [
    "王都アルヴァレス", "辺境の村ミルダ", "霧の森ヴェルデ", "氷結峰カルダ",
    "灼熱砂漠ザハル", "沈黙の湿原ロウ", "古代遺跡エルドラ", "海都リヴァン",
    "地下都市ドヴェルグ", "天空回廊アステル", "荒野ガレス", "鉱山都市ハンマーフォール",
    "呪われた城砦ノクス", "黄昏の大聖堂",
]

LOCATION_SPOTS = [
    "中央広場", "大通り", "市場", "宿屋", "武具店", "民家区画", "城門", "城壁上",
    "兵舎", "中庭", "礼拝堂", "地下水路", "倉庫街", "桟橋", "鍛冶場", "井戸端",
    "墓地", "裏路地", "街道", "石橋", "見張り塔", "洞窟入口", "中層回廊", "最深部",
    "祭壇", "廃坑", "野営地", "崖道", "展望台", "隠し部屋",
]

PLAYABLE_CHARACTERS = [
    ("レイン（主人公・剣士）", 1), ("セラフィナ（聖女）", 1), ("ガイウス（重装騎士）", 1),
    ("ミラ（斥候）", 1), ("オルドリン（賢者）", 1), ("カイル（狩人）", 1),
    ("ノエル（魔導砲手）", 2), ("ヴァレリア（竜騎士）", 2), ("テオ（吟遊詩人）", 2),
    ("イグナ（拳闘士）", 2), ("シルヴィ（召喚士）", 2), ("ダリオ（傭兵）", 2),
    ("エレナ（錬金術師）", 3), ("グラン（狂戦士）", 3), ("リィナ（暗殺者）", 3),
    ("マルク（盾兵）", 3), ("ユーリ（機工士）", 3), ("アイシャ（舞踏家）", 3),
    ("ゼノ（黒魔道士）", 4), ("フィオナ（白魔道士）", 4), ("ボルグ（重斧使い）", 4),
    ("ネル（双剣士）", 4), ("クロウ（影使い）", 5), ("リタ（獣使い）", 5),
    ("ジーク（黄昏の継承者・最終形態）", 1), ("暗黒レイン（分岐ルート専用）", 3),
]

ENEMY_SPECIES = [
    "ゴブリン", "オーク", "スケルトン", "グール", "ワイバーン", "ガーゴイル",
    "スライム", "ハーピー", "リザードマン", "ゴーレム", "ワーウルフ", "インプ",
    "バジリスク", "レイス", "トロール", "マンドラゴラ", "ヒュドラ", "キマイラ",
]
ENEMY_VARIANTS = [
    "森林種", "洞窟種", "氷結種", "溶岩種", "腐敗種", "影種", "古代種", "守護種", "精鋭種",
]
NAMED_BOSSES = [
    "邪竜ヴァルガンド", "剣聖アルバート", "氷の女王フリーゲル", "炎帝バアル",
    "沼の魔女モルガナ", "機械神オートマトン", "堕天使ルシオラ", "樹霊イグドラシル",
    "深海王リヴァイア", "双頭鬼オーガブラザーズ", "砂漠の覇者スカラベ", "亡霊将軍ヴェイン",
    "黄昏の使徒ノクターン", "屍竜アンデッドドラゴン", "大賢者の残滓", "虚無の門番",
    "初代継承者の亡霊", "終焉のエルドラシル",
]

WEAPON_CLASSES = ["片手剣", "両手剣", "槍", "戦斧", "長弓", "短剣", "魔道杖", "大盾", "銃槍"]
WEAPON_GRADES = [
    "鉄製", "鋼鉄", "銀装飾", "王国騎士団", "森人", "竜骨", "古代遺物",
    "呪詛", "聖別", "氷結晶", "溶岩", "星鉄", "黄昏", "継承者",
]
ARMOR_PARTS = ["兜", "鎧", "手甲", "具足", "外套", "篭手"]
ARMOR_SETS = [
    "旅人", "傭兵", "王国騎士", "森人", "魔道士", "竜鱗",
    "古代", "影", "聖騎士", "呪印術師", "氷狼", "黄昏",
]
FURNITURE = [
    "木製テーブル", "長椅子", "樽", "木箱", "本棚", "燭台", "暖炉", "壺", "麻袋",
    "干し草の山", "鍛冶炉", "金床", "水桶", "洗濯物", "荷車", "看板", "露店の天幕",
    "香炉", "祭壇の燭台", "ステンドグラス",
]
NATURE_PROPS = [
    "広葉樹", "針葉樹", "枯れ木", "低木", "岩塊", "苔むした岩", "切り株", "倒木",
    "薬草", "毒キノコ", "花畑", "蔦", "鍾乳石", "氷柱", "サボテン", "流木",
    "蓮の葉", "枯れ草", "砂丘", "溶岩石",
]
TOWN_PROPS = [
    "街灯", "井戸", "掲示板", "馬繋ぎ", "石畳の段差", "門扉", "柵", "旗", "鐘",
    "彫像", "噴水", "橋の欄干", "階段", "ガーゴイル装飾", "屋台", "宿屋の看板",
    "郵便受け", "武器ラック", "訓練用の的", "積み荷",
]
CONSUMABLES = [
    "回復薬", "上級回復薬", "解毒薬", "魔力薬", "携帯食料", "松明", "投擲爆弾",
    "煙玉", "転移石", "修理キット", "研磨剤", "罠キット", "鍵束", "地図の断片",
    "手紙", "鉱石", "皮革", "魔石", "染料", "香辛料",
]
TREASURES = [
    "古代の指輪", "王家の首飾り", "護符", "呪印のブローチ", "竜の鱗", "聖遺物の欠片",
    "封印された箱", "黄金杯", "星読みの水晶", "継承の紋章",
]

CHAPTERS = [
    "プロローグ", "第1章", "第2章", "第3章", "第4章", "第5章",
    "第6章", "第7章", "第8章", "第9章", "第10章", "終章", "エピローグ", "追加エピソード",
]
SCENE_BEATS = [
    "導入", "邂逅", "決別", "追跡", "対峙", "回想", "決戦前夜", "決着", "別離", "幕間",
]

MAIN_QUESTS = [
    "目覚めの朝", "失われた紋章", "王都への道", "騎士団の試練", "裏切りの晩餐",
    "霧の森の導き手", "氷結峰の封印", "灼熱の試練場", "沈黙の湿原を越えて",
    "古代遺跡の扉", "海都の陰謀", "地下都市の反乱", "天空回廊の守護者",
    "鉱山都市の炎", "呪われた城砦", "黄昏の大聖堂", "継承の儀", "終焉との対話",
]
QUEST_KINDS = [
    "薬草採取の依頼", "魔物討伐の依頼", "行方不明者の捜索", "隊商の護衛", "物資輸送",
    "遺失物の回収", "密輸の摘発", "水路の修繕", "迷子の子ども", "古文書の解読",
    "害獣駆除", "収穫祭の準備", "亡霊の鎮魂",
]

UI_SCREENS = [
    "タイトル", "メインメニュー", "ステータス", "インベントリ", "装備",
    "スキルツリー", "ワールドマップ", "クエストログ", "オプション設定", "ショップ",
    "鍛冶・強化", "図鑑", "パーティ編成", "会話ウィンドウ", "戦闘HUD",
    "セーブ／ロード", "実績・トロフィー", "フォトモード",
]
UI_PARTS = ["一覧", "詳細", "並べ替え・絞り込み", "確認ダイアログ", "通知バナー"]

VFX_ELEMENTS = [
    "炎", "氷", "雷", "風", "土", "水", "光", "闇", "毒", "回復",
    "斬撃", "打撃", "射撃", "爆発", "雨", "雪", "砂塵", "霧", "瘴気",
]
VFX_PHASES = ["発生", "継続", "着弾", "消滅", "溜め", "範囲展開", "貫通", "反射", "弱体付与", "強化付与"]

SOUND_CATEGORIES = [
    "環境音", "足音", "戦闘SE", "UI SE", "キャラクターボイス", "BGM（フィールド）",
    "BGM（戦闘）", "BGM（イベント）", "魔法SE", "乗り物SE", "天候SE", "生物の鳴き声",
    "カットシーンSE",
]
SOUND_VARIANTS = [
    "森林", "洞窟", "市街", "城内", "雪原", "砂漠", "水辺", "地下", "空中", "遺跡",
]

VEHICLES = [
    "軍馬", "駿馬", "騎乗鳥グリフィス", "飛竜ワイバーン", "砂漠のラクダ", "雪原のトナカイ",
    "機械馬オートホース", "小型飛空艇", "大型飛空艇", "帆船", "小舟", "魔導動力車",
    "巨大甲虫", "騎乗狼", "白虎", "水上バイク型精霊", "石造ゴーレム騎", "浮遊円盤",
    "地底軌道車", "黄昏の翼",
]


def _pairs(outer, inner, count, fmt):
    """outer × inner の組み合わせから count 件の名前を作る（外側でまとまる順）。"""
    names = [fmt(a, b) for a, b in itertools.product(outer, inner)]
    if len(names) < count:
        raise ValueError(f"名前のプールが足りません: {len(names)} < {count}")
    return names[:count]


def build_job_names():
    """ワークフロー名 -> [(ジョブ名, 優先度の基準), ...] を作る。"""
    jobs = {}

    jobs["プレイアブルキャラクター"] = list(PLAYABLE_CHARACTERS)

    enemies = [(f"{v}{s}", 4 if i < 90 else 6)
               for i, (s, v) in enumerate(itertools.product(ENEMY_SPECIES, ENEMY_VARIANTS))]
    enemies = enemies[:162] + [(name, 2) for name in NAMED_BOSSES]
    jobs["エネミー・ボス"] = enemies

    locations = []
    for ri, region in enumerate(REGIONS):
        for si, spot in enumerate(LOCATION_SPOTS):
            # 序盤の地域・主要スポットほど優先度を高くする
            priority = 2 if ri < 4 and si < 12 else (4 if si < 20 else 6)
            locations.append((f"{region} - {spot}", priority))
    jobs["背景ロケーション"] = locations

    props = []
    props += [(f"{g}の{c}", 5) for g, c in itertools.product(WEAPON_GRADES, WEAPON_CLASSES)]
    props += [(f"{s}の{p}", 5) for s, p in itertools.product(ARMOR_SETS, ARMOR_PARTS)]
    props += [(f"調度品 - {n}", 7) for n in FURNITURE]
    props += [(f"自然物 - {n}", 7) for n in NATURE_PROPS]
    props += [(f"街の小物 - {n}", 7) for n in TOWN_PROPS]
    props += [(f"アイテム - {n}", 6) for n in CONSUMABLES]
    props += [(f"重要アイテム - {n}", 3) for n in TREASURES]
    # 地域ごとの固有プロップで残りを埋める
    props += [(f"{r} 固有プロップ - {s}", 6)
              for r, s in itertools.product(REGIONS, LOCATION_SPOTS[:17])]
    if len(props) < 520:
        raise ValueError(f"プロップの名前プールが足りません: {len(props)} < 520")
    jobs["プロップ・武具"] = props[:520]

    cutscenes = [(f"{ch} {beat}", 1 if ci < 6 else 3)
                 for ci, ch in enumerate(CHAPTERS) for beat in SCENE_BEATS]
    jobs["カットシーン"] = cutscenes[:140]

    quests = [(f"メインクエスト - {t}", 1) for t in MAIN_QUESTS]
    quests += [(f"{r} - {k}", 5) for r, k in itertools.product(REGIONS, QUEST_KINDS)]
    jobs["クエスト・ミッション"] = quests[:200]

    ui = [(f"{screen} - {part}", 3 if si < 8 else 5)
          for si, screen in enumerate(UI_SCREENS) for part in UI_PARTS]
    jobs["UI画面"] = ui[:90]

    vfx = [(f"{el}属性 - {ph}", 5) for el, ph in itertools.product(VFX_ELEMENTS, VFX_PHASES)]
    jobs["VFXアセット"] = vfx[:190]

    sounds = [(f"{cat} - {var}", 5) for cat, var in itertools.product(SOUND_CATEGORIES, SOUND_VARIANTS)]
    jobs["サウンドアセット"] = sounds[:130]

    jobs["乗り物・騎乗獣"] = [(name, 4 if i < 8 else 6) for i, name in enumerate(VEHICLES)]

    return jobs


# 各ワークフローのジョブを、どのマイルストーンにどの割合で割り振るか。
# 序盤マイルストーンほど主要コンテンツ、後半ほど量産アセットが集まるようにする。
MILESTONE_MIX = {
    "プレイアブルキャラクター": [0.15, 0.27, 0.28, 0.20, 0.08, 0.02],
    "エネミー・ボス":           [0.08, 0.20, 0.28, 0.26, 0.15, 0.03],
    "背景ロケーション":         [0.10, 0.22, 0.28, 0.25, 0.13, 0.02],
    "プロップ・武具":           [0.08, 0.18, 0.27, 0.27, 0.17, 0.03],
    "カットシーン":             [0.08, 0.20, 0.26, 0.28, 0.15, 0.03],
    "クエスト・ミッション":     [0.08, 0.20, 0.27, 0.27, 0.15, 0.03],
    "UI画面":                   [0.15, 0.25, 0.28, 0.20, 0.10, 0.02],
    "VFXアセット":              [0.10, 0.22, 0.28, 0.25, 0.13, 0.02],
    "サウンドアセット":         [0.08, 0.20, 0.27, 0.27, 0.15, 0.03],
    "乗り物・騎乗獣":           [0.10, 0.22, 0.28, 0.25, 0.13, 0.02],
}


def assign_milestones(count, mix):
    """mix の比率どおりに、0..len(mix)-1 のマイルストーン番号を count 個並べて返す。"""
    assignments = []
    for index, ratio in enumerate(mix):
        assignments += [index] * round(count * ratio)
    while len(assignments) < count:
        assignments.append(len(mix) - 1)
    return assignments[:count]


def generate(output_path):
    rnd = random.Random(20260105)  # 生成を決定的にする
    db = ProjectDatabase.create_new(str(output_path))
    db.set_project(PROJECT_NAME, PROJECT_START.isoformat())

    print("チーム・マイルストーン・休業日を登録...")
    team_ids = {}
    for name, lines in TEAMS:
        team_ids[name] = db.add_team(name, lines)
    for team_name, changes in TEAM_CAPACITY_CHANGES.items():
        for start_date, lines in changes:
            db.add_team_capacity_change(team_ids[team_name], start_date, lines)

    milestone_ids = []
    for name, end_date, note in MILESTONES:
        milestone_ids.append(db.add_milestone(name, end_date, note))

    for day, note in COMPANY_HOLIDAYS:
        if day.weekday() < 5:  # 土日は自動で休業日になるので登録しない
            db.add_holiday(day.isoformat(), None, note)
    for team_name, day, note in TEAM_HOLIDAYS:
        if day.weekday() < 5:
            db.add_holiday(day.isoformat(), team_ids[team_name], note)

    print("ワークフロー（制作パイプライン）を登録...")
    workflow_ids = {}
    workflow_task_ids = {}  # (ワークフロー名, タスク名) -> id
    for wf_name, tasks in WORKFLOWS.items():
        wf_id = db.add_workflow(wf_name)
        workflow_ids[wf_name] = wf_id
        previous_task_id = None
        for task_name, team_name, days in tasks:
            task_id = db.add_workflow_task(wf_id, task_name, team_ids[team_name], days)
            workflow_task_ids[(wf_name, task_name)] = task_id
            if previous_task_id is not None:
                db.add_task_dependency(wf_id, previous_task_id, task_id)
            previous_task_id = task_id

    for wf_name, task_name, dep_wf_name, dep_task_name in DEPENDENCY_TEMPLATES:
        db.add_dependency_template(
            workflow_ids[wf_name], workflow_task_ids[(wf_name, task_name)],
            workflow_ids[dep_wf_name], workflow_task_ids[(dep_wf_name, dep_task_name)],
        )

    print("ジョブ（制作物）を登録...")
    job_names = build_job_names()
    jobs_by_workflow = {}
    total_tasks = 0
    for wf_name, entries in job_names.items():
        milestone_slots = assign_milestones(len(entries), MILESTONE_MIX[wf_name])
        ids = []
        for (job_name, base_priority), ms_index in zip(entries, milestone_slots):
            # 同じ基準優先度の中でも少しばらつかせる（実プロジェクトでも
            # 「同じ種別だが今回は急ぎ」といった差が付くため）。
            priority = max(1, min(9, base_priority + rnd.choice((-1, 0, 0, 0, 1))))
            job_id = db.add_job(job_name, workflow_ids[wf_name],
                                 milestone_ids[ms_index], priority)
            # 依存先を選ぶときにマイルストーンの前後を見るため、番号を持ち回る
            ids.append((job_id, ms_index))
        jobs_by_workflow[wf_name] = ids
        total_tasks += len(entries) * len(WORKFLOWS[wf_name])
        print(f"  {wf_name}: {len(entries)}ジョブ × {len(WORKFLOWS[wf_name])}タスク")

    print("ジョブ間の依存関係を登録...")
    _add_job_dependency_links(db, jobs_by_workflow, rnd)

    db.save()
    db.close()
    print(f"\n生成しました: {output_path}")
    print(f"  ジョブ数: {sum(len(v) for v in jobs_by_workflow.values()):,}")
    print(f"  タスク数: {total_tasks:,}")


def _add_job_dependency_links(db, jobs_by_workflow, rnd):
    """ジョブ同士の依存を張る（タスク単位の対応は依存テンプレートから自動展開される）。

    実プロジェクトで実際に起きる待ち合わせだけを、現実的な本数に絞って張る。
    全ジョブを総当たりで繋ぐと、依存グラフが密になりすぎて「どのタスクも
    ほとんど動かせない」非現実的なスケジュールになってしまう。

    依存先は必ず「自分と同じか、それより前のマイルストーンのジョブ」から選ぶ。
    後のマイルストーンのジョブに依存させると、待っているだけで自分の締切を
    追い越してしまい、サンプルとして成立しないため（実際の制作計画でも、
    後の工程の完成を待つ作業を先の締切に置くことはしない）。
    """
    def earlier_or_same(candidates, ms_index):
        return [job_id for job_id, ms in candidates if ms <= ms_index]

    characters = jobs_by_workflow["プレイアブルキャラクター"]
    locations = jobs_by_workflow["背景ロケーション"]
    vfx = jobs_by_workflow["VFXアセット"]

    links = []

    def link(job_id, ms_index, candidates, count=1):
        pool = earlier_or_same(candidates, ms_index)
        if not pool:
            return
        for target in rnd.sample(pool, min(count, len(pool))):
            links.append((job_id, target))

    # カットシーンは、そこに登場する主要キャラのリグ完成を待つ
    for cutscene, ms_index in jobs_by_workflow["カットシーン"]:
        link(cutscene, ms_index, characters[:12], count=2)
    # クエストは舞台となるロケーションの完成を待つ
    for quest, ms_index in jobs_by_workflow["クエスト・ミッション"]:
        link(quest, ms_index, locations)
    # ボス級エネミーは専用エフェクトの素材完成を待つ
    for enemy, ms_index in jobs_by_workflow["エネミー・ボス"][162:]:
        link(enemy, ms_index, vfx)
    # 主要武具はキャラのリグ完成を待つ（装備して表示するため）
    for prop, ms_index in jobs_by_workflow["プロップ・武具"][:60]:
        link(prop, ms_index, characters[:12])

    # 1件ずつ add_job_dependency_link を呼ぶと、その都度すべてのリンクに対して
    # テンプレートを適用し直す（sync_dependency_templates）ため、件数の2乗に
    # 比例して遅くなる。生成時は行を先に入れてから、最後に1回だけ同期する。
    with db.undo_group("ジョブ間の依存を一括登録"):
        for job_id, depends_on_job_id in links:
            db._conn.execute(
                "INSERT OR IGNORE INTO job_dependency_links(job_id, depends_on_job_id) "
                "VALUES (?, ?)", (job_id, depends_on_job_id),
            )
        db.sync_dependency_templates()
    print(f"  依存リンク: {len(links)}件")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    generate(out)
