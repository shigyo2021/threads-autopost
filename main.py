"""メインオーケストレーター: 全パイプラインを実行"""

import io
import sys
import platform

# Windows環境でのUnicode出力対応
if platform.system() == "Windows":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import argparse
import json
import os
import random
import sys
from datetime import datetime

from config import RAKUTEN_GENRES, ROOM_STYLES, IMAGES_DIR, OUTPUT_DIR, POSTS_LOG
from rakuten_api import search_products
from image_generator import generate_interior_image
from post_generator import generate_post_text, generate_alt_text
from threads_api import ThreadsClient
from image_uploader import get_uploader


def load_posted_items() -> set:
    """投稿済み商品コードを読み込み（重複投稿防止）"""
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
    """投稿ログを追記"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(POSTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_pipeline(
    count: int = 1,
    category: str | None = None,
    style: str | None = None,
    dry_run: bool = False,
    upload_method: str = "firebase",
    upload_kwargs: dict | None = None,
):
    """
    パイプライン実行

    1. 楽天APIで商品取得
    2. AI画像生成
    3. 投稿文生成
    4. Threads投稿

    Args:
        count: 投稿件数
        category: 商品カテゴリ（None=ランダム）
        style: インテリアスタイル（None=ランダム）
        dry_run: True=投稿せず確認のみ
        upload_method: 画像アップロード方法
        upload_kwargs: アップローダーの追加引数
    """
    print(f"\n{'='*60}")
    print(f"🏠 Threads×楽天アフィリエイト 自動投稿Bot")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   件数: {count} | ドライラン: {dry_run}")
    print(f"{'='*60}\n")

    posted_items = load_posted_items()

    # --- Step 1: 商品取得 ---
    print("📦 Step 1: 楽天APIで商品を検索中...")
    products = search_products(category=category, count=count * 3)  # 余裕を持って取得

    # 投稿済みを除外
    products = [p for p in products if p["item_code"] not in posted_items]
    if not products:
        print("⚠️  未投稿の商品が見つかりません。カテゴリを変更してください。")
        return

    products = products[:count]
    print(f"   → {len(products)}件の商品を選定\n")

    # アップローダー準備
    uploader = None
    if not dry_run:
        uploader = get_uploader(upload_method, **(upload_kwargs or {}))
        threads_client = ThreadsClient()

    for i, product in enumerate(products, 1):
        chosen_style = style or random.choice(list(ROOM_STYLES.keys()))
        room_name = ROOM_STYLES[chosen_style]["name"]

        print(f"--- [{i}/{len(products)}] ---")
        print(f"商品: {product['name'][:50]}")
        print(f"価格: ¥{product['price']:,}")
        print(f"スタイル: {room_name}")
        print()

        # --- Step 2: 画像生成 ---
        print("🎨 Step 2: インテリアイメージを生成中...")
        try:
            image_path = generate_interior_image(product, style=chosen_style)
            print(f"   → 保存: {image_path}\n")
        except Exception as e:
            print(f"   ❌ 画像生成エラー: {e}\n")
            continue

        # --- Step 3: 投稿文生成 ---
        print("✍️  Step 3: 投稿文を生成中...")
        try:
            post_text = generate_post_text(product, style=chosen_style)
            print(f"   --- 投稿文プレビュー ---")
            print(f"   {post_text[:200]}...")
            print()
        except Exception as e:
            print(f"   ❌ 投稿文生成エラー: {e}\n")
            continue

        # --- Step 4: 投稿 ---
        if dry_run:
            print("🔍 [ドライラン] 投稿をスキップ")
            print(f"\n{'- '*30}")
            print(f"[メイン投稿] {post_text}")
            print(f"[返信] {product['url']}\npr")
            print(f"{'- '*30}\n")
        else:
            print("📤 Step 4: Threadsに投稿中...")
            try:
                # 画像をアップロード
                image_url = uploader.upload(image_path)
                print(f"   画像URL: {image_url}")

                # メイン投稿（短文 + 画像）
                result = threads_client.publish_image_post(
                    text=post_text,
                    image_url=image_url,
                )
                post_id = result.get("id", "")
                print(f"   ✅ メイン投稿完了! ID: {post_id}")

                # 返信（アフィリエイトリンク + pr）
                reply_text = f"{product['url']}\npr"
                reply_result = threads_client.publish_reply(
                    text=reply_text,
                    reply_to_id=post_id,
                )
                print(f"   ✅ 返信投稿完了! ID: {reply_result.get('id', 'unknown')}\n")
            except Exception as e:
                print(f"   ❌ 投稿エラー: {e}\n")
                continue

        # ログ記録
        log_post({
            "item_code": product["item_code"],
            "name": product["name"],
            "price": product["price"],
            "url": product["url"],
            "style": chosen_style,
            "image_path": image_path,
            "timestamp": datetime.now().isoformat(),
            "dry_run": dry_run,
        })

    print(f"\n{'='*60}")
    print(f"✅ 完了: {len(products)}件処理")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Threads×楽天アフィリエイト自動投稿Bot")
    parser.add_argument("--count", type=int, default=1, help="投稿件数")
    parser.add_argument("--category", type=str, default=None,
                        choices=list(RAKUTEN_GENRES.keys()),
                        help="商品カテゴリ")
    parser.add_argument("--style", type=str, default=None,
                        choices=list(ROOM_STYLES.keys()),
                        help="インテリアスタイル")
    parser.add_argument("--dry-run", action="store_true",
                        help="投稿せずに確認のみ")
    parser.add_argument("--test", action="store_true",
                        help="テスト実行（1件・ドライラン）")
    parser.add_argument("--upload-method", type=str, default="firebase",
                        choices=["firebase", "imgbb", "local"],
                        help="画像アップロード方法")

    args = parser.parse_args()

    if args.test:
        args.count = 1
        args.dry_run = True

    run_pipeline(
        count=args.count,
        category=args.category,
        style=args.style,
        dry_run=args.dry_run,
        upload_method=args.upload_method,
    )


if __name__ == "__main__":
    main()
