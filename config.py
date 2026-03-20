"""設定・定数"""

import os
from dotenv import load_dotenv

load_dotenv(encoding="utf-8", override=True)

# --- APIキー ---
RAKUTEN_APP_ID = os.getenv("RAKUTEN_APP_ID", "")
RAKUTEN_ACCESS_KEY = os.getenv("RAKUTEN_ACCESS_KEY", "")
RAKUTEN_AFFILIATE_ID = os.getenv("RAKUTEN_AFFILIATE_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
THREADS_USER_ID = os.getenv("THREADS_USER_ID", "")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN", "")
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "")

# --- 出力 ---
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")
POSTS_LOG = os.path.join(OUTPUT_DIR, "posts_log.jsonl")

# --- 楽天 商品検索の対象ジャンル ---
RAKUTEN_GENRES = {
    "ソファ": {"genreId": "100804", "keyword": "ソファ 北欧"},
    "テーブル": {"genreId": "100804", "keyword": "テーブル おしゃれ"},
    "チェア": {"genreId": "100804", "keyword": "チェア デザイン"},
    "照明": {"genreId": "100804", "keyword": "照明 ペンダントライト"},
    "ラグ": {"genreId": "100804", "keyword": "ラグ カーペット 北欧"},
    "収納": {"genreId": "100804", "keyword": "収納棚 おしゃれ"},
    "ベッド": {"genreId": "100804", "keyword": "ベッドフレーム モダン"},
    "カーテン": {"genreId": "100804", "keyword": "カーテン ナチュラル"},
}

# --- インテリアスタイル（画像生成プロンプト用） ---
ROOM_STYLES = {
    "scandinavian": {
        "name": "北欧スタイル",
        "description": "white walls, light wood laminate flooring, simple white curtains with natural light, minimal Scandinavian-inspired decor, cozy and clean atmosphere",
    },
    "japanese_modern": {
        "name": "和モダン",
        "description": "modern Japanese room with partial tatami area, low furniture, light wood tones, shoji-inspired sliding doors, simple and zen atmosphere",
    },
    "industrial": {
        "name": "インダストリアル",
        "description": "dark accent wall, black metal shelf or rack, warm Edison bulb pendant light, wood and metal mix furniture, compact urban apartment style",
    },
    "natural": {
        "name": "ナチュラル",
        "description": "warm beige and white tones, light wood furniture, a few small indoor plants, linen textiles, soft natural sunlight, relaxing and simple atmosphere",
    },
    "mid_century": {
        "name": "ミッドセンチュリー",
        "description": "warm walnut wood furniture, retro-inspired design accents, mustard or olive color cushions, simple geometric rug, compact and stylish room",
    },
    "korean": {
        "name": "韓国インテリア",
        "description": "soft beige and cream tones throughout, rounded low furniture, warm indirect lighting, clean minimal lines, cozy cafe-like atmosphere",
    },
}

# --- 投稿文生成用プロンプト ---
POST_GENERATION_SYSTEM_PROMPT = """あなたはThreadsでインテリア情報を発信する日本語アカウントの投稿文ライターです。

以下のルールに従って投稿文を作成してください：

1. 1〜2文の短い感想・コメントのみ（100文字以内が理想）
2. カジュアルで自然な口調（友達に話すように）
3. 商品を見た素直な感想や、インテリアへの憧れを表現
4. 語尾は「〜だなぁ」「〜いいよね」「〜素敵」など柔らかく
5. 絵文字は0〜1個（使いすぎない）
6. 感想文の後に空行を1つ入れ、関連ハッシュタグを3〜5個付ける
7. ハッシュタグは商品カテゴリ・スタイルに合ったものを選ぶ
8. リンク、PR表記は含めない（別途追加するため）
9. 「AI生成イメージ」の表記は含めない（別途追加するため）

ハッシュタグ例：
#インテリア #お部屋づくり #暮らしを楽しむ #北欧インテリア #ナチュラルインテリア
#一人暮らしインテリア #模様替え #おしゃれな部屋 #インテリアコーディネート #韓国インテリア

NG事項：
- 「使ってみました」等の実体験を偽る表現
- 過度な煽り文句
- 商品を誤認させる表現
- 長い説明文
"""

# --- Threads API ---
THREADS_API_BASE = "https://graph.threads.net/v1.0"

# --- 投稿スケジュール ---
POST_TIMES = ["08:00", "12:00", "20:00"]  # 1日3回
