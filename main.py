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
import re
import sys
from datetime import datetime

from config import RAKUTEN_GENRES, ROOM_STYLES, IMAGES_DIR, OUTPUT_DIR, POSTS_LOG
from rakuten_api import search_products, fetch_product_by_url
from post_generator import generate_post_text, generate_reply_text, generate_alt_text
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
    upload_method: str = "imgbb",
    upload_kwargs: dict | None = None,
    image_mode: str = "product",
    urls: list[str] | None = None,
):
    """
    パイプライン実行

    Args:
        count: 投稿件数
        category: 商品カテゴリ（None=ランダム）
        style: インテリアスタイル（None=ランダム）
        dry_run: True=投稿せず確認のみ
        upload_method: 画像アップロード方法
        upload_kwargs: アップローダーの追加引数
        image_mode: "product"=商品画像使用, "ai"=AI画像生成
        urls: 楽天商品URLリスト（指定時はURL直接指定モード）
    """
    mode_label = "URL指定" if urls else image_mode
    print(f"\n{'='*60}")
    print(f"🏠 Threads×楽天アフィリエイト 自動投稿Bot")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   件数: {len(urls) if urls else count} | モード: {mode_label} | ドライラン: {dry_run}")
    print(f"{'='*60}\n")

    posted_items = load_posted_items()

    # --- Step 1: 商品取得 ---
    if urls:
        # URL指定モード: 指定されたURLから商品情報を取得
        print("📦 Step 1: 指定URLから商品情報を取得中...")
        products = []
        for url in urls:
            try:
                product = fetch_product_by_url(url)
                if product["item_code"] in posted_items:
                    print(f"   ⚠️ 投稿済みのためスキップ: {product['name'][:30]}")
                    continue
                products.append(product)
                print(f"   ✅ {product['name'][:40]}")
            except Exception as e:
                print(f"   ❌ 取得失敗: {url[:60]}... → {e}")
    else:
        # 自動モード: 楽天APIで商品検索
        print("📦 Step 1: 楽天APIで商品を検索中...")
        check_img = (image_mode == "product")
        products = search_products(category=category, count=count * 3, check_images=check_img)
        products = [p for p in products if p["item_code"] not in posted_items]

    if not products:
        print("⚠️  未投稿の商品が見つかりません。")
        return

    if not urls:
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

        # --- Step 2: 画像準備 ---
        image_paths = []
        if image_mode == "ai" and not urls:
            print("🎨 Step 2: AI画像を生成中...")
            try:
                from image_generator import generate_interior_image
                image_path = generate_interior_image(product, style=chosen_style)
                image_paths = [image_path]
                print(f"   → 保存: {image_path}\n")
            except Exception as e:
                print(f"   ❌ 画像生成エラー: {e}\n")
                continue
        else:
            # 商品画像モード（URL指定時も商品画像を使用）
            print("🖼️  Step 2: 商品画像を取得・処理中...")
            try:
                from image_processor import process_product_images
                image_paths = process_product_images(product, max_images=3)
                print(f"   → {len(image_paths)}枚の画像を準備\n")
            except Exception as e:
                print(f"   ❌ 商品画像処理エラー: {e}\n")
                continue

        # --- Step 3: 投稿文生成 ---
        print("✍️  Step 3: 投稿文を生成中...")
        try:
            post_text = generate_post_text(product, style=chosen_style)
            reply_text = generate_reply_text(product)
            print(f"   --- メイン投稿プレビュー ---")
            print(f"   {post_text[:200]}")
            print(f"   --- 返信プレビュー ---")
            print(f"   {reply_text[:200]}")
            print()
        except Exception as e:
            print(f"   ❌ 投稿文生成エラー: {e}\n")
            continue

        # --- Step 4: 投稿 ---
        if dry_run:
            print("🔍 [ドライラン] 投稿をスキップ")
            print(f"\n{'- '*30}")
            print(f"[メイン投稿] {post_text}")
            print(f"[画像] {len(image_paths)}枚")
            print(f"[返信] {reply_text}")
            print(f"{'- '*30}\n")
        else:
            print("📤 Step 4: Threadsに投稿中...")
            try:
                # 画像をアップロード
                uploaded_urls = []
                for img_path in image_paths:
                    url = uploader.upload(img_path)
                    uploaded_urls.append(url)
                    print(f"   画像URL: {url}")

                # メイン投稿（複数画像ならカルーセル、1枚なら通常投稿）
                if len(uploaded_urls) >= 2:
                    result = threads_client.publish_carousel_post(
                        text=post_text,
                        image_urls=uploaded_urls,
                    )
                else:
                    result = threads_client.publish_image_post(
                        text=post_text,
                        image_url=uploaded_urls[0],
                    )
                post_id = result.get("id", "")
                print(f"   ✅ メイン投稿完了! ID: {post_id}")

                # 返信（商品補足 + アフィリエイトリンク + pr）
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
            "image_paths": image_paths,
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
    parser.add_argument("--upload-method", type=str, default="imgbb",
                        choices=["firebase", "imgbb", "local"],
                        help="画像アップロード方法")
    parser.add_argument("--image-mode", type=str, default="product",
                        choices=["product", "ai"],
                        help="画像モード: product=商品画像, ai=AI生成画像")
    parser.add_argument("--url", type=str, nargs="+", default=None,
                        help="楽天商品URL（1つ以上指定）。例: --url https://item.rakuten.co.jp/shop/item/")

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
        image_mode=args.image_mode,
        urls=args.url,
    )


if __name__ == "__main__":
    main()
