"""手動投稿ツール: 楽天URLを貼って対話的にThreads投稿"""

import io
import sys
import platform

# Windows環境でのUnicode出力対応
if platform.system() == "Windows":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import json
import os
import random
from datetime import datetime

from config import ROOM_STYLES, OUTPUT_DIR, POSTS_LOG
from rakuten_api import fetch_product_by_url
from post_generator import generate_post_text, generate_reply_text
from quality_checker import score_post, check_similarity, get_past_good_posts
from image_processor import process_product_images
from image_uploader import get_uploader
from threads_api import ThreadsClient


def load_posted_items() -> set:
    posted = set()
    if os.path.exists(POSTS_LOG):
        with open(POSTS_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    posted.add(entry.get("item_code", ""))
                except json.JSONDecodeError:
                    pass
    return posted


def log_post(entry: dict):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(POSTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ask(prompt: str, default: str = "") -> str:
    """ユーザー入力を受け取る"""
    if default:
        result = input(f"{prompt} [{default}]: ").strip()
        return result if result else default
    return input(f"{prompt}: ").strip()


def ask_yn(prompt: str, default: bool = True) -> bool:
    """Y/N質問"""
    suffix = " (Y/n)" if default else " (y/N)"
    result = input(f"{prompt}{suffix}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes", "はい")


def select_style() -> str:
    """スタイル選択"""
    styles = list(ROOM_STYLES.items())
    print("\n   スタイル一覧:")
    for i, (key, val) in enumerate(styles, 1):
        print(f"   {i}. {val['name']} ({key})")
    print(f"   0. ランダム")

    choice = ask("   番号を選択", "0")
    if choice == "0" or not choice.isdigit():
        key = random.choice(list(ROOM_STYLES.keys()))
        print(f"   → {ROOM_STYLES[key]['name']}")
        return key

    idx = int(choice) - 1
    if 0 <= idx < len(styles):
        key = styles[idx][0]
        print(f"   → {ROOM_STYLES[key]['name']}")
        return key

    key = random.choice(list(ROOM_STYLES.keys()))
    print(f"   → {ROOM_STYLES[key]['name']}（ランダム）")
    return key


def process_one_product(url: str, uploader, threads_client, posted_items: set):
    """1商品を処理"""
    print(f"\n{'─'*50}")

    # --- 商品取得 ---
    print("📦 商品情報を取得中...")
    try:
        product = fetch_product_by_url(url)
    except Exception as e:
        print(f"   ❌ 取得失敗: {e}")
        return

    if product["item_code"] in posted_items:
        print(f"   ⚠️ この商品は投稿済みです: {product['name'][:40]}")
        if not ask_yn("   それでも投稿しますか？", False):
            return

    print(f"   商品名: {product['name'][:60]}")
    print(f"   価格:  ¥{product['price']:,}")
    print(f"   ショップ: {product['shop']}")
    print(f"   レビュー: ★{product['review_average']}（{product['review_count']}件）")
    print(f"   画像数: {len(product['image_urls'])}枚")

    # --- スタイル選択 ---
    style = select_style()

    # --- 画像選択 ---
    image_urls = product.get("image_urls", [])
    print(f"\n🖼️  商品画像一覧（全{len(image_urls)}枚）:")
    for idx, img_url in enumerate(image_urls, 1):
        # URLからファイル名を抽出して表示
        filename = img_url.split("/")[-1].split("?")[0]
        print(f"   {idx}. {filename}")

    print(f"\n   使用する画像の番号をカンマ区切りで入力してください")
    print(f"   例: 1,2,5  /  空欄=自動選択（品質チェック付き）")
    img_choice = ask("   画像番号", "")

    selected_indices = None
    if img_choice:
        try:
            selected_indices = [int(x.strip()) for x in img_choice.split(",") if x.strip().isdigit()]
            if selected_indices:
                print(f"   → 画像 {', '.join(str(i) for i in selected_indices)} を使用")
        except ValueError:
            print("   ⚠️ 無効な入力、自動選択を使用します")
            selected_indices = None

    print("\n   画像を処理中...")
    try:
        image_paths = process_product_images(
            product, max_images=3, selected_indices=selected_indices,
        )
        print(f"   → {len(image_paths)}枚の画像を準備")
    except Exception as e:
        print(f"   ❌ 画像処理エラー: {e}")
        return

    # --- 投稿文生成 + 品質チェック ---
    print("\n✍️  投稿文を生成中...")
    past_good = get_past_good_posts(limit=3)
    max_attempts = 3
    post_text = None
    quality_score = 0
    retry_reason = None

    for attempt in range(1, max_attempts + 1):
        candidate = generate_post_text(
            product,
            style=style,
            past_good_posts=past_good,
            retry_reason=retry_reason,
        )

        score_result = score_post(candidate, product["name"])
        score = score_result["score"]
        print(f"\n   [候補{attempt}] スコア {score}/7")
        print(f"   {candidate}")

        if score_result["passed"]:
            sim = check_similarity(candidate)
            if sim["is_unique"]:
                post_text = candidate
                quality_score = score
                break
            else:
                retry_reason = f"過去投稿と類似"
                print(f"   ⚠️ 類似度が高いため再生成")
                continue
        else:
            retry_reason = score_result["reason"]
            print(f"   ⚠️ ボツ: {retry_reason}")
            continue

    if post_text is None:
        post_text = candidate
        quality_score = score
        print(f"\n   ⚠️ 基準未達ですが最終候補を使用")

    # --- 確認 ---
    reply_text = generate_reply_text(product)

    print(f"\n{'━'*50}")
    print(f"📝 投稿プレビュー")
    print(f"{'━'*50}")
    print(f"\n[メイン投稿]")
    print(f"{post_text}")
    print(f"\n[画像] {len(image_paths)}枚")
    print(f"\n[返信]")
    print(f"{reply_text}")
    print(f"\n{'━'*50}")

    # 編集オプション
    while True:
        print("\n   1. このまま投稿する")
        print("   2. 投稿文を再生成する")
        print("   3. 投稿文を手動で編集する")
        print("   4. スキップ（投稿しない）")

        choice = ask("   選択", "1")

        if choice == "1":
            break
        elif choice == "2":
            print("\n   🔄 再生成中...")
            retry_reason = "ユーザーが再生成を要求"
            post_text = generate_post_text(
                product, style=style,
                past_good_posts=past_good,
                retry_reason=retry_reason,
            )
            score_result = score_post(post_text, product["name"])
            quality_score = score_result["score"]
            print(f"\n   [新候補] スコア {quality_score}/7")
            print(f"   {post_text}")
            reply_text = generate_reply_text(product)
            print(f"\n   [返信] {reply_text[:100]}")
        elif choice == "3":
            print("\n   現在の投稿文:")
            print(f"   {post_text}")
            new_text = input("\n   新しい投稿文を入力（空欄でキャンセル）:\n   ").strip()
            if new_text:
                post_text = new_text
                print("   ✅ 更新しました")
        elif choice == "4":
            print("   ⏭️ スキップ")
            return
        else:
            continue

    # --- 投稿 ---
    print("\n📤 Threadsに投稿中...")
    try:
        # 画像アップロード
        uploaded_urls = []
        for img_path in image_paths:
            img_url = uploader.upload(img_path)
            uploaded_urls.append(img_url)
            print(f"   画像: {img_url[:60]}...")

        # メイン投稿
        if len(uploaded_urls) >= 2:
            result = threads_client.publish_carousel_post(
                text=post_text, image_urls=uploaded_urls,
            )
        else:
            result = threads_client.publish_image_post(
                text=post_text, image_url=uploaded_urls[0],
            )
        post_id = result.get("id", "")
        print(f"   ✅ メイン投稿完了! ID: {post_id}")

        # 返信
        reply_result = threads_client.publish_reply(
            text=reply_text, reply_to_id=post_id,
        )
        print(f"   ✅ 返信投稿完了! ID: {reply_result.get('id', '')}")

    except Exception as e:
        print(f"   ❌ 投稿エラー: {e}")
        return

    # ログ記録
    log_post({
        "item_code": product["item_code"],
        "name": product["name"],
        "price": product["price"],
        "url": product["url"],
        "style": style,
        "image_paths": image_paths,
        "post_text": post_text,
        "quality_score": quality_score,
        "timestamp": datetime.now().isoformat(),
        "dry_run": False,
    })

    print(f"\n   ✅ 完了!")


def main():
    print(f"\n{'='*50}")
    print(f"🏠 Threads手動投稿ツール")
    print(f"   楽天URLを貼るだけで投稿できます")
    print(f"{'='*50}")
    print(f"\n   使い方:")
    print(f"   - 楽天商品URLを貼り付けてEnter")
    print(f"   - 複数URLはスペース区切りで一度に入力可能")
    print(f"   - 'q' で終了\n")

    uploader = get_uploader("imgbb")
    threads_client = ThreadsClient()
    posted_items = load_posted_items()

    while True:
        url_input = ask("\n🔗 楽天URL（qで終了）")

        if url_input.lower() in ("q", "quit", "exit", "終了"):
            print("\n👋 終了します\n")
            break

        if not url_input:
            continue

        # 複数URL対応（スペース区切り）
        urls = url_input.split()
        urls = [u for u in urls if "rakuten.co.jp" in u]

        if not urls:
            print("   ⚠️ 楽天のURLを入力してください")
            continue

        for url in urls:
            process_one_product(url, uploader, threads_client, posted_items)
            # 投稿済みリストを更新
            posted_items = load_posted_items()


if __name__ == "__main__":
    main()
