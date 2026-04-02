"""投稿文生成: Claude APIで魅力的なThreads投稿文を作成"""

import anthropic
from config import (
    ANTHROPIC_API_KEY,
    POST_GENERATION_SYSTEM_PROMPT,
    REPLY_GENERATION_SYSTEM_PROMPT,
    CONTENT_GENERATION_SYSTEM_PROMPT,
    ROOM_STYLES,
)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def generate_post_text(
    product: dict,
    style: str,
    past_good_posts: list[str] | None = None,
    retry_reason: str | None = None,
) -> str:
    """
    商品情報とインテリアスタイルから Threads 投稿文を生成。

    Args:
        product: 商品情報dict
        style: インテリアスタイルキー
        past_good_posts: 過去のスコアが高かった投稿文（参考用）
        retry_reason: 前回ボツになった理由（再生成時に渡す）

    Returns:
        投稿文テキスト
    """
    room = ROOM_STYLES.get(style, ROOM_STYLES["scandinavian"])

    user_prompt = f"""以下の商品情報からThreads投稿文を作成してください。

【商品名】{product['name']}
【価格】¥{product['price']:,}
【ショップ】{product['shop']}
【レビュー】★{product['review_average']}（{product['review_count']}件）
【カテゴリ】{product['category']}
【インテリアスタイル】{room['name']}"""

    # 過去の好投稿を参考として追加
    if past_good_posts:
        user_prompt += "\n\n【参考：過去に反応が良かった投稿（トーンを参考に。コピーはNG）】"
        for i, post in enumerate(past_good_posts, 1):
            # ハッシュタグ部分を除去して本文だけ渡す
            body = post.split("\n#")[0].strip()
            user_prompt += f"\n{i}. 「{body}」"

    # 再生成時のフィードバック
    if retry_reason:
        user_prompt += f"\n\n【重要】前回の投稿文はボツになりました。理由: {retry_reason}\nこの点を改善した新しい投稿文を書いてください。前回とまったく違う切り口で。"

    user_prompt += "\n\n投稿文のみを出力してください。前置きや説明は不要です。"

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=POST_GENERATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = response.content[0].text.strip()

    # リンクやPR表記が混入していた場合は除去
    text = text.replace("[LINK]", "").strip()

    return text


def generate_reply_text(product: dict) -> str:
    """
    返信欄に投稿する商品補足文を生成。

    Returns:
        返信テキスト（[LINK]を実際のURLに置換済み、末尾にpr付き）
    """
    user_prompt = f"""以下の商品の返信文を作成してください。

【商品名】{product['name']}
【価格】¥{product['price']:,}
【レビュー】★{product['review_average']}（{product['review_count']}件）
【カテゴリ】{product['category']}

返信文のみを出力してください。前置きや説明は不要です。"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=REPLY_GENERATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = response.content[0].text.strip()

    # [LINK]を実際のURLに置換
    text = text.replace("[LINK]", product["url"])

    # prが含まれていなければ追加
    if not text.strip().endswith("pr"):
        text = text.strip() + "\npr"

    return text


def generate_content_text(topic: str, topic_label: str, past_posts: list[str] | None = None) -> str:
    """
    非宣伝コンテンツ投稿文を生成（インテリアのコツ・豆知識など）。

    Args:
        topic: トピックキー
        topic_label: トピック表示名
        past_posts: 過去のコンテンツ投稿（重複回避用）

    Returns:
        投稿文テキスト
    """
    user_prompt = f"以下のテーマでThreads投稿文を作成してください。\n\n【テーマ】{topic_label}"

    if past_posts:
        user_prompt += "\n\n【過去の投稿（内容が被らないようにしてください）】"
        for i, post in enumerate(past_posts[-5:], 1):
            body = post.split("\n#")[0].strip()
            user_prompt += f"\n{i}. 「{body}」"

    user_prompt += "\n\n投稿文のみを出力してください。前置きや説明は不要です。"

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=CONTENT_GENERATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = response.content[0].text.strip()
    return text


def extract_image_keywords(post_text: str) -> str:
    """投稿文から画像検索用の英語キーワードを抽出する"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": f"""以下の日本語投稿文に合うストックフォトを検索するための英語キーワードを1〜3語で出力してください。
インテリア・部屋の写真が見つかるようなキーワードにしてください。
キーワードのみ出力。説明不要。

投稿文: {post_text}"""}],
    )
    return response.content[0].text.strip()


def generate_alt_text(product: dict, style: str) -> str:
    """画像のaltテキスト（アクセシビリティ用）を生成"""
    room = ROOM_STYLES.get(style, ROOM_STYLES["scandinavian"])
    return f"AI生成インテリアイメージ: {room['name']}の部屋に{product['category']}を配置したコーディネート例"


if __name__ == "__main__":
    # テスト
    test_product = {
        "name": "北欧風 3人掛けソファ ファブリック グレー",
        "price": 39800,
        "url": "https://r10.to/example",
        "shop": "インテリアショップExample",
        "review_average": 4.5,
        "review_count": 128,
        "category": "ソファ",
    }
    text = generate_post_text(test_product, "scandinavian")
    print(text)
