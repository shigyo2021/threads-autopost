"""投稿文生成: Claude APIで魅力的なThreads投稿文を作成"""

import anthropic
from config import ANTHROPIC_API_KEY, POST_GENERATION_SYSTEM_PROMPT, ROOM_STYLES

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def generate_post_text(product: dict, style: str) -> str:
    """
    商品情報とインテリアスタイルから Threads 投稿文を生成。

    Returns:
        投稿文テキスト（[LINK] を実際のURLに置換済み）
    """
    room = ROOM_STYLES.get(style, ROOM_STYLES["scandinavian"])

    user_prompt = f"""以下の商品情報からThreads投稿文を作成してください。

【商品名】{product['name']}
【価格】¥{product['price']:,}
【ショップ】{product['shop']}
【レビュー】★{product['review_average']}（{product['review_count']}件）
【カテゴリ】{product['category']}
【インテリアスタイル】{room['name']}

投稿文のみを出力してください。前置きや説明は不要です。"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=POST_GENERATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = response.content[0].text.strip()

    # リンクやハッシュタグが混入していた場合は除去
    text = text.replace("[LINK]", "").strip()

    return text


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
