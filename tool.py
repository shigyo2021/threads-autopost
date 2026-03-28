"""手動投稿ツール: 楽天URLを貼って対話的にThreads投稿"""

import io
import sys
import platform

# Windows環境でのUnicode出力対応
if platform.system() == "Windows":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import glob
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


class UserQuit(Exception):
    """ユーザーが途中終了を選択した"""
    pass


def ask(prompt: str, default: str = "") -> str:
    """ユーザー入力を受け取る（qで終了）"""
    if default:
        result = input(f"{prompt} [{default}]: ").strip()
    else:
        result = input(f"{prompt}: ").strip()
    if result.lower() == "q":
        raise UserQuit()
    return result if result else default


def ask_yn(prompt: str, default: bool = True) -> bool:
    """Y/N質問（qで終了）"""
    suffix = " (Y/n)" if default else " (y/N)"
    result = input(f"{prompt}{suffix}: ").strip().lower()
    if result == "q":
        raise UserQuit()
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

    # --- 画像選択（ブラウザプレビュー付き） ---
    image_urls = product.get("image_urls", [])
    print(f"\n🖼️  商品画像: 全{len(image_urls)}枚")

    if image_urls:
        preview_path = _create_image_preview(product, image_urls)
        if preview_path:
            import webbrowser
            webbrowser.open(f"file:///{preview_path.replace(os.sep, '/')}")
            print(f"   → ブラウザでプレビューを開きました")
        else:
            # フォールバック: ファイル名一覧
            for idx, img_url in enumerate(image_urls, 1):
                filename = img_url.split("/")[-1].split("?")[0]
                print(f"   {idx}. {filename}")

    print(f"\n   使用する画像の番号をカンマ区切りで入力してください")
    print(f"   例: 1,2,5  /  空欄=自動選択（品質チェック付き）")
    img_choice = ask("   画像番号", "")

    selected_indices = None
    direct_image_urls = None  # 楽天URLを直接使用する場合
    if img_choice:
        try:
            selected_indices = [int(x.strip()) for x in img_choice.split(",") if x.strip().isdigit()]
            if selected_indices:
                print(f"   → 画像 {', '.join(str(i) for i in selected_indices)} を使用")
                # 選択した画像の楽天URLを保持（imgBB不要で直接投稿可能）
                direct_image_urls = []
                for idx in selected_indices:
                    if 1 <= idx <= len(image_urls):
                        direct_image_urls.append(image_urls[idx - 1])
        except ValueError:
            print("   ⚠️ 無効な入力、自動選択を使用します")
            selected_indices = None

    # 画像処理（ローカルダウンロード）- imgBBフォールバック用
    image_paths = None
    if not direct_image_urls:
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
    img_count = len(direct_image_urls) if direct_image_urls else len(image_paths)
    print(f"\n[画像] {img_count}枚")
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
            _cleanup_temp_files(image_paths)
            return
        else:
            continue

    # --- 今すぐ投稿 or 予約 ---
    print("\n   📅 投稿タイミング:")
    print("   1. 今すぐ投稿する")
    print("   2. 予約投稿（日時指定）")
    timing = ask("   選択", "1")

    # 画像URLを準備
    if direct_image_urls:
        # 楽天画像URLを直接使用（imgBB不要）
        uploaded_urls = direct_image_urls
        print(f"\n🖼️  楽天画像URLを直接使用（{len(uploaded_urls)}枚）")
    else:
        # imgBBにアップロード（自動選択 or フォールバック）
        print("\n📤 画像をアップロード中...")
        try:
            uploaded_urls = []
            for img_path in image_paths:
                img_url = uploader.upload(img_path)
                uploaded_urls.append(img_url)
                print(f"   画像: {img_url[:60]}...")
        except Exception as e:
            print(f"   ❌ 画像アップロードエラー: {e}")
            _cleanup_temp_files(image_paths)
            return

    if timing == "2":
        # --- 予約投稿 ---
        scheduled_time = _ask_schedule_time()
        if scheduled_time is None:
            print("   ⚠️ 無効な日時です。スキップします。")
            if image_paths:
                _cleanup_temp_files(image_paths)
            return

        _add_to_queue({
            "item_code": product["item_code"],
            "name": product["name"],
            "price": product["price"],
            "affiliate_url": product["url"],
            "post_text": post_text,
            "reply_text": reply_text,
            "image_urls": uploaded_urls,
            "style": style,
            "quality_score": quality_score,
            "scheduled_at": scheduled_time,
            "created_at": datetime.now().isoformat(),
            "status": "pending",
        })

        print(f"\n   📅 予約完了! {scheduled_time} に投稿されます")
        print(f"   → GitHub Actionsが自動で投稿します")
        print(f"   → 予約一覧: py tool.py --queue")

        # git push して GitHub Actions からアクセス可能にする
        _push_queue()
    else:
        # --- 今すぐ投稿 ---
        print("\n📤 Threadsに投稿中...")
        try:
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
            if image_paths:
                _cleanup_temp_files(image_paths)
            return

    # ログ記録
    log_post({
        "item_code": product["item_code"],
        "name": product["name"],
        "price": product["price"],
        "url": product["url"],
        "style": style,
        "image_urls": uploaded_urls,
        "post_text": post_text,
        "quality_score": quality_score,
        "timestamp": datetime.now().isoformat(),
        "dry_run": False,
        "scheduled": timing == "2",
    })

    print(f"\n   ✅ 完了!")

    # 投稿完了後にローカル画像を削除
    if image_paths:
        _cleanup_temp_files(image_paths)


QUEUE_FILE = os.path.join(OUTPUT_DIR, "post_queue.json")


def _ask_schedule_time() -> str | None:
    """予約日時を対話的に取得する"""
    print("\n   📅 予約日時を入力してください（JST）")
    print("   形式: YYYY-MM-DD HH:MM")
    print("   例:   2026-03-27 08:00")
    print("   ショートカット:")
    print("     '8'  → 明日 08:00")
    print("     '12' → 明日 12:00")
    print("     '20' → 明日 20:00")

    time_input = ask("   日時", "").strip()

    if not time_input:
        return None

    from datetime import timedelta

    now = datetime.now()

    # ショートカット: 数字だけ → 明日のその時刻
    if time_input.isdigit() and len(time_input) <= 2:
        hour = int(time_input)
        if 0 <= hour <= 23:
            tomorrow = now + timedelta(days=1)
            scheduled = tomorrow.replace(hour=hour, minute=0, second=0, microsecond=0)
            print(f"   → {scheduled.strftime('%Y-%m-%d %H:%M')} に予約")
            return scheduled.isoformat()

    # フル日時入力
    try:
        scheduled = datetime.strptime(time_input, "%Y-%m-%d %H:%M")
        if scheduled <= now:
            print("   ⚠️ 過去の日時です。未来の日時を指定してください。")
            return None
        print(f"   → {scheduled.strftime('%Y-%m-%d %H:%M')} に予約")
        return scheduled.isoformat()
    except ValueError:
        print("   ⚠️ 日時の形式が不正です。YYYY-MM-DD HH:MM で入力してください。")
        return None


def _load_queue() -> list[dict]:
    """予約キューを読み込む"""
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_queue(queue: list[dict]):
    """予約キューを保存"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


def _add_to_queue(entry: dict):
    """予約キューに追加（同一item_codeの重複を防止）"""
    queue = _load_queue()
    item_code = entry.get("item_code", "")
    # すでにpendingで同じ商品がキューにあればスキップ
    for existing in queue:
        if existing.get("item_code") == item_code and existing.get("status") == "pending":
            print(f"   ⚠️ 同じ商品がすでにキューに入っています（上書きします）")
            queue.remove(existing)
            break
    queue.append(entry)
    _save_queue(queue)


def _push_queue():
    """予約キューファイルをgit commit & pushする"""
    import subprocess
    try:
        # post_queue.jsonをステージング
        subprocess.run(
            ["git", "add", QUEUE_FILE],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Add scheduled post to queue"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
        )
        result = subprocess.run(
            ["git", "push"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("   ✅ キューをGitHubにpushしました")
        else:
            print(f"   ⚠️ push失敗: {result.stderr[:100]}")
            print("   → 手動で git push してください")
    except Exception as e:
        print(f"   ⚠️ git操作エラー: {e}")
        print("   → 手動で git push してください")


def show_queue():
    """予約キューの一覧を表示"""
    queue = _load_queue()
    pending = [q for q in queue if q.get("status") == "pending"]

    if not pending:
        print("\n   📭 予約投稿はありません\n")
        return

    print(f"\n   📅 予約投稿一覧 ({len(pending)}件)")
    print(f"   {'─'*45}")
    for i, entry in enumerate(pending, 1):
        scheduled = entry.get("scheduled_at", "?")
        name = entry.get("name", "?")[:35]
        text_preview = entry.get("post_text", "")[:40]
        images = len(entry.get("image_urls", []))
        print(f"   {i}. [{scheduled[:16]}] {name}")
        print(f"      {text_preview}...")
        print(f"      画像{images}枚")
        print()


def _cleanup_temp_files(image_paths: list[str] | None = None):
    """投稿後の一時ファイルを削除（images/ と preview/）"""
    # 指定された画像ファイルを削除
    if image_paths:
        for path in image_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

    # プレビューHTMLを削除
    preview_dir = os.path.join(OUTPUT_DIR, "preview")
    if os.path.isdir(preview_dir):
        for f in glob.glob(os.path.join(preview_dir, "*")):
            try:
                os.remove(f)
            except Exception:
                pass


def _create_image_preview(product: dict, image_urls: list[str]) -> str | None:
    """商品画像のHTMLプレビューを生成してブラウザで開く"""
    try:
        preview_dir = os.path.join(OUTPUT_DIR, "preview")
        os.makedirs(preview_dir, exist_ok=True)
        preview_path = os.path.join(os.path.abspath(preview_dir), "image_preview.html")

        name = product["name"][:60]
        price = f"¥{product['price']:,}"

        img_cards = ""
        for idx, url in enumerate(image_urls, 1):
            # 動画URLかどうかを判定
            lower_url = url.lower()
            is_video = any(ext in lower_url for ext in [".mp4", ".mov", ".webm", "video"])

            if is_video:
                media_tag = f'<div style="display:flex;align-items:center;justify-content:center;height:300px;background:#f0f0f0;color:#999;font-size:14px;">動画（投稿不可）</div>'
                border_color = "#ccc"
                label_extra = ' <span style="color:#999;font-size:12px;">動画</span>'
            else:
                media_tag = f'<img src="{url}" style="max-width:100%;max-height:300px;object-fit:contain;" loading="lazy" onerror="this.parentElement.innerHTML=\'<div style=padding:40px;color:#999>読込失敗</div>\'">'
                border_color = "#4CAF50"
                label_extra = ""

            img_cards += f"""
            <div style="border:2px solid {border_color};border-radius:8px;padding:8px;text-align:center;background:white;">
                <div style="font-size:24px;font-weight:bold;color:#333;margin-bottom:8px;">{idx}{label_extra}</div>
                {media_tag}
            </div>"""

        html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>画像プレビュー - {name}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #f5f5f5; padding: 20px; margin: 0; }}
  .header {{ background: white; padding: 16px 20px; border-radius: 8px; margin-bottom: 16px; }}
  .header h2 {{ margin: 0 0 8px 0; font-size: 18px; }}
  .header p {{ margin: 0; color: #666; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }}
  .hint {{ background: #FFF3E0; padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }}
</style>
</head>
<body>
  <div class="header">
    <h2>{name}</h2>
    <p>{price} / {product['shop']} / 全{len(image_urls)}枚</p>
  </div>
  <div class="hint">
    💡 使いたい画像の<b>番号</b>をターミナルに入力してください（例: 1,3,5）
  </div>
  <div class="grid">
    {img_cards}
  </div>
</body>
</html>"""

        with open(preview_path, "w", encoding="utf-8") as f:
            f.write(html)

        return preview_path
    except Exception:
        return None


def main():
    # --queue オプション: 予約一覧を表示して終了
    if len(sys.argv) > 1 and sys.argv[1] == "--queue":
        show_queue()
        return

    print(f"\n{'='*50}")
    print(f"🏠 Threads手動投稿ツール")
    print(f"   楽天URLを貼るだけで投稿できます")
    print(f"{'='*50}")
    print(f"\n   使い方:")
    print(f"   - 楽天商品URLを貼り付けてEnter")
    print(f"   - 複数URLはスペース区切りで一度に入力可能")
    print(f"   - 'q' で終了 / Ctrl+C でいつでも中断可能\n")

    uploader = get_uploader("imgbb")
    threads_client = ThreadsClient()
    posted_items = load_posted_items()

    while True:
        url_input = ask("\n🔗 楽天URL（qで終了）")

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
    try:
        main()
    except (KeyboardInterrupt, UserQuit):
        print("\n\n👋 終了します\n")
