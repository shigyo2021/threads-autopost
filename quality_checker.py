"""投稿品質チェック: AI採点 + 類似度チェック"""

import json
import os
import re
import anthropic
from config import ANTHROPIC_API_KEY, POSTS_LOG

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# --- AI採点プロンプト ---
SCORING_SYSTEM_PROMPT = """あなたはThreads投稿の品質を採点する編集者です。
インテリア系アカウントの投稿文を7つの項目で厳しく採点してください。

■ 採点項目（各1点、合計7点）：
1. 自然さ: AIっぽくない、人間が書いたように見えるか
2. 簡潔さ: 短くて無駄がないか（80文字以内が理想）
3. 独自性: テンプレ的でなく、この商品ならではの切り口か
4. 共感性: 読んだ人が「わかる」と思えるか
5. リズム感: 体言止めや句点の使い方にリズムがあるか
6. 押し売り感のなさ: 宣伝臭さがないか
7. スクロール停止力: タイムラインで目に留まる一言があるか

■ NGワードチェック（該当したら自然さを0点にする）：
- 「〜だなぁ」「素敵」「いいよね」「おすすめ」「必見」「マスト」
- ✨🌟💡🏠などキラキラ系絵文字
- 「使ってみました」等の実体験偽装

■ 出力フォーマット（厳守）：
score: [合計点]
details: [各項目の点数をカンマ区切り]
reason: [1行で改善ポイント]

例：
score: 5
details: 1,1,0,1,1,1,0
reason: 独自性が弱い。この商品ならではの視点がほしい。"""


def score_post(post_text: str, product_name: str) -> dict:
    """
    投稿文をAIが7項目で採点する。

    Returns:
        {"score": int, "details": list[int], "reason": str, "passed": bool}
    """
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        system=SCORING_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"以下の投稿文を採点してください。\n\n商品: {product_name}\n投稿文:\n{post_text}",
        }],
    )

    text = response.content[0].text.strip()

    # パース
    score = 0
    details = []
    reason = ""

    score_match = re.search(r"score:\s*(\d+)", text)
    if score_match:
        score = int(score_match.group(1))

    details_match = re.search(r"details:\s*([\d,\s]+)", text)
    if details_match:
        details = [int(x.strip()) for x in details_match.group(1).split(",") if x.strip().isdigit()]

    reason_match = re.search(r"reason:\s*(.+)", text)
    if reason_match:
        reason = reason_match.group(1).strip()

    return {
        "score": score,
        "details": details,
        "reason": reason,
        "passed": score >= 5,
    }


def check_similarity(new_text: str, threshold: float = 0.5) -> dict:
    """
    新しい投稿文と過去の投稿文の類似度をチェック。

    簡易的なn-gram類似度で判定。
    完全一致や非常に似た投稿を防ぐ。

    Returns:
        {"is_unique": bool, "max_similarity": float, "similar_to": str|None}
    """
    past_texts = _load_past_post_texts()

    if not past_texts:
        return {"is_unique": True, "max_similarity": 0.0, "similar_to": None}

    # ハッシュタグを除去して本文のみで比較
    new_body = _strip_hashtags(new_text)
    new_ngrams = _get_ngrams(new_body, n=3)

    if not new_ngrams:
        return {"is_unique": True, "max_similarity": 0.0, "similar_to": None}

    max_sim = 0.0
    most_similar = None

    for past_text in past_texts:
        past_body = _strip_hashtags(past_text)
        past_ngrams = _get_ngrams(past_body, n=3)

        if not past_ngrams:
            continue

        # Jaccard類似度
        intersection = new_ngrams & past_ngrams
        union = new_ngrams | past_ngrams
        similarity = len(intersection) / len(union) if union else 0

        if similarity > max_sim:
            max_sim = similarity
            most_similar = past_text[:50]

    return {
        "is_unique": max_sim < threshold,
        "max_similarity": round(max_sim, 3),
        "similar_to": most_similar if max_sim >= threshold else None,
    }


def _load_past_post_texts() -> list[str]:
    """過去の投稿文をログから読み込み"""
    texts = []
    if not os.path.exists(POSTS_LOG):
        return texts

    with open(POSTS_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                post_text = entry.get("post_text", "")
                if post_text:
                    texts.append(post_text)
            except json.JSONDecodeError:
                pass

    return texts


def _strip_hashtags(text: str) -> str:
    """ハッシュタグ行を除去"""
    lines = text.split("\n")
    body_lines = [l for l in lines if not l.strip().startswith("#")]
    return " ".join(body_lines).strip()


def _get_ngrams(text: str, n: int = 3) -> set:
    """テキストからn-gramセットを生成"""
    text = text.replace(" ", "").replace("\n", "")
    if len(text) < n:
        return set()
    return {text[i:i+n] for i in range(len(text) - n + 1)}


def get_past_good_posts(limit: int = 3) -> list[str]:
    """
    過去の投稿文からスコアが高かったものを取得。
    次の投稿生成のプロンプトに「参考例」として渡す。
    """
    if not os.path.exists(POSTS_LOG):
        return []

    scored_posts = []
    with open(POSTS_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                post_text = entry.get("post_text", "")
                score = entry.get("quality_score", 0)
                if post_text and score >= 5:
                    scored_posts.append((score, post_text))
            except json.JSONDecodeError:
                pass

    # スコア順にソート
    scored_posts.sort(key=lambda x: x[0], reverse=True)
    return [text for _, text in scored_posts[:limit]]
