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
import pathlib
import random
from datetime import date, datetime, timedelta

from config import ROOM_STYLES, OUTPUT_DIR, POSTS_LOG, CONTENT_TOPICS, PEXELS_API_KEY
from rakuten_api import fetch_product_by_url, normalize_rakuten_url, affiliate_link_sns
from post_generator import (
    generate_post_text, generate_reply_text, generate_content_text,
    extract_image_keywords, finalize_reply_text,
)
from quality_checker import score_post, check_similarity, get_past_good_posts, lint_post
from drafts import (
    load_draft as load_cc_draft, is_ready as cc_draft_ready, mark_draft_used,
    load_stock, format_stock_line, post_url, posts_today, posted_dates, print_stock_summary,
    MAX_POSTS_PER_DAY,
)
import queue_sync
from image_processor import process_product_images
from image_uploader import get_uploader
from threads_api import ThreadsClient
from token_tool import token_age_days, REFRESH_WARNING_DAYS
from sheet import export_sheet_quietly


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


def ask_multiline(prompt: str) -> str:
    """複数行の入力を受け取る（「.」だけの行で確定、空のまま「.」でキャンセル）"""
    print(f"\n   {prompt}（複数行OK。入力を終えたら「.」だけの行でEnter）:")
    lines = []
    while True:
        line = input("   ")
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _select_text_source(product: dict, url: str) -> tuple[str | None, dict | None]:
    """
    投稿文の作り方を選ぶ。

    Returns:
        ("api", None) / ("claude_code", 下書きdict) / (None, None)=スキップ
    """
    draft = load_cc_draft(product["item_code"])
    ready = cc_draft_ready(draft)

    print("\n✍️  投稿文の作り方:")
    if ready:
        first_line = draft["post_text"].strip().splitlines()[0]
        print(f"   💬 Claude Codeの下書きがあります:「{first_line[:40]}」")
    print("   1. Claude APIで生成する（APIクレジットを使う）")
    print("   2. Claude Codeの下書きを使う（APIクレジットを使わない）")
    choice = ask("   選択", "2" if ready else "1")

    if choice != "2":
        return "api", None

    while not ready:
        print("\n   ⚠️ この商品の下書きがまだありません。Claude Codeにこう依頼してください:")
        print(f"   「この商品の投稿文を作って {url}」")
        print("   Enter = 下書きを読み込み直す / a = APIで生成する / s = スキップ")
        answer = ask("   選択", "").lower()
        if answer == "a":
            return "api", None
        if answer == "s":
            return None, None
        draft = load_cc_draft(product["item_code"])
        ready = cc_draft_ready(draft)

    return "claude_code", draft


def _generate_post_with_api(product: dict, style: str, past_good: list[str]) -> tuple[str, int]:
    """Claude APIで投稿文を生成し、採点・類似度チェックで最大3回作り直す"""
    print("\n✍️  投稿文を生成中...")
    max_attempts = 3
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

        if not score_result["passed"]:
            retry_reason = score_result["reason"]
            print(f"   ⚠️ ボツ: {retry_reason}")
            continue

        if check_similarity(candidate)["is_unique"]:
            return candidate, score

        retry_reason = "過去投稿と類似"
        print("   ⚠️ 類似度が高いため再生成")

    print("\n   ⚠️ 基準未達ですが最終候補を使用")
    return candidate, score


def _apply_cc_draft(product: dict, draft: dict) -> tuple[str, str, int | None]:
    """Claude Codeの下書きから投稿文と返信文を作る（APIは使わない）"""
    post_text = draft["post_text"].strip()
    reply_source = draft.get("reply_text", "").strip() or \
        f"レビュー★{product['review_average']}（{product['review_count']}件）。\n[LINK]"
    reply_text = finalize_reply_text(reply_source, product["url"])

    for warning in lint_post(post_text):
        print(f"   ⚠️ {warning}")
    sim = check_similarity(post_text)
    if not sim["is_unique"]:
        print(f"   ⚠️ 過去投稿と似ています（類似度{sim['max_similarity']}）: {sim['similar_to']}")

    return post_text, reply_text, draft.get("self_score")


def _print_post_preview(post_text: str, reply_text: str, img_count: int, text_source: str):
    source_label = "Claude API" if text_source == "api" else "Claude Code下書き"
    print(f"\n{'━'*50}")
    print(f"📝 投稿プレビュー（文章: {source_label}）")
    print(f"{'━'*50}")
    print(f"\n[メイン投稿]")
    print(post_text)
    print(f"\n[画像] {img_count}枚")
    print(f"\n[返信]")
    print(reply_text)
    print(f"\n{'━'*50}")


def _threads_reply_link(product: dict, url: str) -> str | None:
    """
    返信に使うThreads用リンク。下書きに付いていればそれ、
    貼り付けたURL自体がThreads用リンクならそれ。なければ None（APIのリンクを使う）。
    """
    draft = load_cc_draft(product["item_code"])
    if draft and draft.get("reply_link"):
        return draft["reply_link"]
    if affiliate_link_sns(url) == "Threads":
        return url
    return None


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

    # --- 返信に付けるリンク ---
    # 以降の product["url"]（返信文・ログ・予約）は、Threads用リンクがあればそれになる
    reply_link = _threads_reply_link(product, url)
    if reply_link:
        product = {**product, "url": reply_link}
        print(f"   🔗 返信のリンク: Threads用リンク（SNS別レポートに載る）")
    else:
        print(f"   🔗 返信のリンク: 通常のアフィリエイトリンク（SNS別レポートには載らない）")
    item_url = normalize_rakuten_url(url).split("?")[0]

    # --- 画像選択（ブラウザプレビュー付き） ---
    image_urls = product.get("image_urls", [])
    print(f"\n🖼️  商品画像: 全{len(image_urls)}枚")

    if image_urls:
        preview_path = _create_image_preview(product, image_urls)
        if preview_path:
            import webbrowser
            webbrowser.open(pathlib.Path(os.path.abspath(preview_path)).as_uri())
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

    # --- 投稿文の用意（Claude API or Claude Code下書き） ---
    text_source, draft = _select_text_source(product, url)
    if text_source is None:
        print("   ⏭️ スキップ")
        _cleanup_temp_files(image_paths)
        return

    style = select_style() if text_source == "api" else draft.get("style", "")
    past_good = get_past_good_posts(limit=3)

    try:
        if text_source == "api":
            post_text, quality_score = _generate_post_with_api(product, style, past_good)
            reply_text = generate_reply_text(product)
        else:
            post_text, reply_text, quality_score = _apply_cc_draft(product, draft)
    except Exception as e:
        print(f"   ❌ 投稿文の用意に失敗: {e}")
        if text_source == "api":
            print("   → Claude Codeの下書きモード（2）なら APIを使わずに続けられます")
        _cleanup_temp_files(image_paths)
        return

    # --- 確認 ---
    img_count = len(direct_image_urls) if direct_image_urls else len(image_paths)

    # 編集オプション
    while True:
        _print_post_preview(post_text, reply_text, img_count, text_source)

        print("\n   1. このまま投稿する")
        if text_source == "api":
            print("   2. 投稿文を再生成する（API）")
        else:
            print("   2. 下書きを読み込み直す（Claude Codeで書き直した後）")
        print("   3. 投稿文を手動で編集する")
        print("   4. スキップ（投稿しない）")
        if text_source == "claude_code":
            print("   5. Claude APIで生成し直す")

        choice = ask("   選択", "1")

        if choice == "1":
            break
        elif choice == "2" and text_source == "api":
            print("\n   🔄 再生成中...")
            post_text = generate_post_text(
                product, style=style,
                past_good_posts=past_good,
                retry_reason="ユーザーが再生成を要求",
            )
            quality_score = score_post(post_text, product["name"])["score"]
            print(f"\n   [新候補] スコア {quality_score}/7")
            reply_text = generate_reply_text(product)
        elif choice == "2":
            reloaded = load_cc_draft(product["item_code"])
            if cc_draft_ready(reloaded):
                draft = reloaded
                post_text, reply_text, quality_score = _apply_cc_draft(product, draft)
            else:
                print("   ⚠️ 下書きが見つかりません")
        elif choice == "5" and text_source == "claude_code":
            text_source = "api"
            style = select_style()
            post_text, quality_score = _generate_post_with_api(product, style, past_good)
            reply_text = generate_reply_text(product)
        elif choice == "3":
            print("\n   現在の投稿文:")
            print(f"   {post_text}")
            new_text = ask_multiline("新しい投稿文を入力")
            if new_text:
                post_text = new_text
                print("   ✅ 更新しました")
                for warning in lint_post(post_text):
                    print(f"   ⚠️ {warning}")
        elif choice == "4":
            print("   ⏭️ スキップ")
            _cleanup_temp_files(image_paths)
            return
        else:
            continue

    # --- 今すぐ投稿 or 予約 ---
    planned_at = _planned_datetime(product["item_code"])
    print("\n   📅 投稿タイミング:")
    print("   1. 今すぐ投稿する")
    print("   2. 予約投稿（GitHub Actionsが指定の時間に投稿）")
    # 予定が先の日時なら予約を初期値にする（前日に準備する使い方）
    timing = ask("   選択", "2" if planned_at and planned_at > datetime.now() else "1")

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
        # --- 予約投稿（投稿ログにはまだ記録しない。投稿後に sync_results で取り込む） ---
        scheduled_time = _ask_schedule_time(planned_at)
        if scheduled_time is None:
            print("   ⚠️ 予約をやめました")
            _cleanup_temp_files(image_paths)
            return

        day = scheduled_time[:10]
        booked = queue_sync.count_on_date(day) + posted_dates()[day]
        if booked >= MAX_POSTS_PER_DAY:
            print(f"   ⚠️ {day} はすでに{booked}件の投稿・予約があります（目安は1日{MAX_POSTS_PER_DAY}件まで）")
            if not ask_yn("   それでも予約しますか？", False):
                _cleanup_temp_files(image_paths)
                return

        print("\n📤 GitHubに予約を送信中...")
        draft_for_label = load_cc_draft(product["item_code"]) or {}
        ok = queue_sync.schedule({
            "item_code": product["item_code"],
            "label": draft_for_label.get("label", ""),
            "name": product["name"],
            "price": product["price"],
            "url": product["url"],
            "item_url": item_url,
            "post_text": post_text,
            "reply_text": reply_text,
            "image_urls": uploaded_urls,
            "style": style,
            "quality_score": quality_score,
            "text_source": text_source,
            "scheduled_at": scheduled_time,
        })
        _cleanup_temp_files(image_paths)
        if ok:
            print(f"\n   ✅ 予約しました: {scheduled_time[:16].replace('T', ' ')} 以降にGitHub Actionsが投稿します")
            print("   （GitHubの実行は数時間遅れることがあります。予約一覧・取り消し: py tool.py --queue）")
            export_sheet_quietly()
        else:
            print("\n   ❌ 予約できませんでした（下書きはストックに残っています）")
        return
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
        "item_url": item_url,
        "style": style,
        "image_urls": uploaded_urls,
        "post_text": post_text,
        "quality_score": quality_score,
        "text_source": text_source,
        "timestamp": datetime.now().isoformat(),
        "dry_run": False,
        "scheduled": False,
    })

    if text_source == "claude_code":
        mark_draft_used(product["item_code"])
    export_sheet_quietly()

    print(f"\n   ✅ 完了!")

    # 投稿完了後にローカル画像を削除
    if image_paths:
        _cleanup_temp_files(image_paths)


CONTENT_LOG = os.path.join(OUTPUT_DIR, "content_log.jsonl")
DRAFTS_FILE = os.path.join(OUTPUT_DIR, "content_drafts.json")


def _load_drafts() -> list[dict]:
    """下書き一覧を読み込む"""
    if os.path.exists(DRAFTS_FILE):
        with open(DRAFTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_drafts(drafts: list[dict]):
    """下書き一覧を保存"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(DRAFTS_FILE, "w", encoding="utf-8") as f:
        json.dump(drafts, f, ensure_ascii=False, indent=2)


def _add_draft(post_text: str, topic_key: str, image_keywords: str | None = None):
    """下書きに保存"""
    drafts = _load_drafts()
    drafts.append({
        "post_text": post_text,
        "topic": topic_key,
        "image_keywords": image_keywords,
        "created_at": datetime.now().isoformat(),
    })
    _save_drafts(drafts)


def _pick_draft() -> dict | None:
    """下書きを選択して返す（選択された下書きはリストから削除）"""
    drafts = _load_drafts()
    if not drafts:
        return None

    print(f"\n   📋 未投稿の下書きが {len(drafts)}件 あります:")
    for i, d in enumerate(drafts, 1):
        text_preview = d["post_text"].split("\n")[0][:50]
        source = "Claude Code" if d.get("source") == "claude_code" else "保存済み"
        print(f"   {i}. [{d.get('created_at', '')[:10]} {source}] {text_preview}...")

    print(f"\n   番号 = 下書きを使用（APIクレジットを使わない） / n = Claude APIで新規生成")
    choice = ask("   選択", "1")

    if choice and choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(drafts):
            selected = drafts.pop(idx - 1)
            _save_drafts(drafts)
            return selected
    return None


def _choose_content_topic() -> tuple[str, str]:
    topics = list(CONTENT_TOPICS.items())
    print("\n   テーマ一覧:")
    for i, (key, label) in enumerate(topics, 1):
        print(f"   {i}. {label}")
    print(f"   0. ランダム")

    choice = ask("   番号を選択", "0")
    if choice == "0" or not choice.isdigit() or not 1 <= int(choice) <= len(topics):
        topic_key, topic_label = random.choice(topics)
    else:
        topic_key, topic_label = topics[int(choice) - 1]
    print(f"   → テーマ: {topic_label}")
    return topic_key, topic_label


def _search_and_preview_pexels(keywords: str) -> list[dict]:
    photos = _search_pexels(keywords)
    if not photos:
        print("   ⚠️ Pexelsで画像が見つかりませんでした")
        return []
    preview_path = _create_pexels_preview(photos, keywords)
    if preview_path:
        import webbrowser
        webbrowser.open(pathlib.Path(os.path.abspath(preview_path)).as_uri())
        print(f"   → ブラウザでプレビューを開きました（{len(photos)}枚）")
    return photos


def _search_pexels(query: str, count: int = 9) -> list[dict]:
    """Pexels APIで画像を検索する"""
    import requests
    if not PEXELS_API_KEY:
        return []

    resp = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "per_page": count, "orientation": "square"},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"   ⚠️ Pexels検索エラー: {resp.status_code}")
        return []

    results = []
    for photo in resp.json().get("photos", []):
        results.append({
            "id": photo["id"],
            "url": photo["src"]["large"],        # 投稿用（高画質）
            "preview": photo["src"]["medium"],    # プレビュー用
            "photographer": photo["photographer"],
            "alt": photo.get("alt", ""),
        })
    return results


def _create_pexels_preview(photos: list[dict], query: str) -> str | None:
    """Pexels画像のHTMLプレビューを生成してブラウザで開く"""
    preview_dir = os.path.join(OUTPUT_DIR, "preview")
    os.makedirs(preview_dir, exist_ok=True)
    preview_path = os.path.join(preview_dir, "pexels_preview.html")

    try:
        img_cards = ""
        for idx, photo in enumerate(photos, 1):
            img_cards += f"""
            <div class="card">
              <div class="number">{idx}</div>
              <img src="{photo['preview']}" alt="{photo['alt']}" loading="lazy">
              <div class="credit">📷 {photo['photographer']}</div>
            </div>"""

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Pexels画像選択</title>
<style>
  body {{ font-family: sans-serif; background: #1a1a1a; color: #fff; padding: 20px; }}
  h2 {{ text-align: center; }}
  .hint {{ text-align: center; background: #333; padding: 10px; border-radius: 8px; margin: 15px 0; }}
  .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; max-width: 900px; margin: 0 auto; }}
  .card {{ background: #2a2a2a; border-radius: 8px; overflow: hidden; border: 2px solid transparent; }}
  .card:hover {{ border-color: #4CAF50; }}
  .number {{ text-align: center; font-size: 1.5em; font-weight: bold; padding: 8px; }}
  .card img {{ width: 100%; aspect-ratio: 1; object-fit: cover; }}
  .credit {{ padding: 5px; text-align: center; font-size: 0.8em; color: #aaa; }}
  .search {{ text-align: center; color: #888; margin-top: 10px; }}
</style></head>
<body>
  <h2>🖼️ Pexels画像選択</h2>
  <div class="hint">💡 使いたい画像の<b>番号</b>をターミナルに入力してください（例: 1）</div>
  <div class="search">検索: "{query}"</div>
  <div class="grid">{img_cards}</div>
</body></html>"""

        with open(preview_path, "w", encoding="utf-8") as f:
            f.write(html)
        return preview_path
    except Exception:
        return None


def process_content_post(threads_client):
    """非宣伝コンテンツ投稿（インテリアのコツ・豆知識など）"""
    print(f"\n{'─'*50}")
    print("📝 コンテンツ投稿（リンクなし）")
    print(f"{'─'*50}")

    past_content = _load_past_content()
    topic_label = None
    image_keywords = None

    # 下書きがあれば先に提示（Claude Codeの下書きならAPIを使わない）
    draft = _pick_draft()
    if draft:
        post_text = draft["post_text"]
        topic_key = draft.get("topic") or "draft"
        image_keywords = draft.get("image_keywords")
        for warning in lint_post(post_text, max_length=150):
            print(f"   ⚠️ {warning}")
    else:
        topic_key, topic_label = _choose_content_topic()
        print("\n✍️  投稿文を生成中...")
        post_text = generate_content_text(topic_key, topic_label, past_content)

    # プレビュー＆編集
    while True:
        print(f"\n{'━'*50}")
        print(f"{post_text}")
        print(f"{'━'*50}")

        print("\n   1. このまま投稿する")
        print("   2. Claude APIで生成し直す" if topic_label is None else "   2. 再生成する（API）")
        print("   3. 手動で編集する")
        print("   4. スキップ（投稿しない）")

        edit_choice = ask("   選択", "1")

        if edit_choice == "1":
            break
        elif edit_choice == "2":
            if topic_label is None:
                if draft:
                    _add_draft(post_text, topic_key, image_keywords)  # 元の下書きは残す
                    print("   （元の下書きは残しました）")
                topic_key, topic_label = _choose_content_topic()
                image_keywords = None
            print("\n   🔄 生成中...")
            post_text = generate_content_text(topic_key, topic_label, past_content)
            draft = None
        elif edit_choice == "3":
            print(f"\n   現在の投稿文:")
            print(f"   {post_text}")
            new_text = ask_multiline("新しい投稿文を入力")
            if new_text:
                post_text = new_text
                print("   ✅ 更新しました")
        elif edit_choice == "4":
            _add_draft(post_text, topic_key, image_keywords)
            print("   ⏭️ スキップ（下書きに保存しました）")
            return
        else:
            continue

    # --- 画像選択 ---
    image_urls = []
    photos = []
    if PEXELS_API_KEY:
        print("\n🔍 投稿に合う画像を検索中...")
        # 下書きにキーワードがあればAPIを使わない
        keywords = image_keywords or extract_image_keywords(post_text)
        print(f"   検索キーワード: {keywords}")
        photos = _search_and_preview_pexels(keywords)

    while True:
        if image_urls:
            print(f"\n   現在の選択済み画像: {len(image_urls)}枚")

        print(f"\n   画像の選択:")
        print(f"   番号  = Pexels画像を使用（例: 3 / 複数: 1,3,5）")
        print(f"   s     = 別のキーワードで再検索")
        print(f"   f     = ローカルファイル（PC内の画像・複数可）")
        print(f"   空欄  = 選択終了{'（テキストのみ投稿）' if not image_urls else ''}")
        print(f"   x     = 投稿を中止")
        img_choice = ask("   選択", "")

        if img_choice == "x":
            _add_draft(post_text, topic_key, image_keywords)
            print("   ⏭️ 投稿を中止しました（下書きに保存しました）")
            return
        elif img_choice == "s":
            # 手動キーワードで再検索
            new_kw = ask("   検索キーワード（英語推奨）")
            if new_kw:
                photos = _search_and_preview_pexels(new_kw)
            continue
        elif img_choice == "f":
            # フォルダ指定 → 画像一覧から番号選択
            print("   画像が入っているフォルダのパスを入力してください")
            print("   例: C:\\Users\\（ユーザー名）\\Pictures")
            folder_path = ask("   フォルダパス").strip().strip('"')
            if not folder_path or not os.path.isdir(folder_path):
                print("   ⚠️ フォルダが見つかりません")
                continue

            # フォルダ内の画像ファイル一覧
            img_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
            files = sorted([
                f for f in os.listdir(folder_path)
                if f.lower().endswith(img_exts)
            ])
            if not files:
                print("   ⚠️ 画像ファイルが見つかりません")
                continue

            print(f"\n   📂 {os.path.basename(folder_path)} 内の画像（{len(files)}枚）:")
            for i, fname in enumerate(files, 1):
                size_kb = os.path.getsize(os.path.join(folder_path, fname)) // 1024
                print(f"   {i:>3}. {fname}  ({size_kb}KB)")

            print(f"\n   番号を入力（複数: 1,3,5 / 範囲: 1-4 / 全選択: all）")
            sel = ask("   選択", "").strip()
            if not sel:
                continue

            # 選択番号のパース
            selected_indices = set()
            if sel.lower() == "all":
                selected_indices = set(range(1, len(files) + 1))
            else:
                for part in sel.split(","):
                    part = part.strip()
                    if "-" in part:
                        bounds = part.split("-", 1)
                        if bounds[0].isdigit() and bounds[1].isdigit():
                            start, end = int(bounds[0]), int(bounds[1])
                            for n in range(start, end + 1):
                                if 1 <= n <= len(files):
                                    selected_indices.add(n)
                    elif part.isdigit():
                        n = int(part)
                        if 1 <= n <= len(files):
                            selected_indices.add(n)

            if not selected_indices:
                print("   ⚠️ 有効な番号がありません")
                continue

            # アップロード
            uploader = get_uploader("imgbb")
            for idx in sorted(selected_indices):
                fp = os.path.join(folder_path, files[idx - 1])
                print(f"   📤 アップロード中... {files[idx - 1]}")
                try:
                    url = uploader.upload(fp)
                    image_urls.append(url)
                    print(f"   ✅ [{len(image_urls)}枚目] OK")
                except Exception as e:
                    print(f"   ❌ 失敗: {e}")

            if image_urls:
                print(f"\n   📸 合計 {len(image_urls)}枚 の画像を選択済み")
            continue
        elif img_choice and photos:
            # 番号指定（カンマ区切りで複数対応: 1,3,5）
            nums = [n.strip() for n in img_choice.split(",")]
            for n in nums:
                if n.isdigit():
                    idx = int(n)
                    if 1 <= idx <= len(photos):
                        image_urls.append(photos[idx - 1]["url"])
                        print(f"   → 画像 {idx} を追加（📷 {photos[idx - 1]['photographer']}）")
                    else:
                        print(f"   ⚠️ 1〜{len(photos)}の番号を入力してください")
            if image_urls:
                print(f"\n   📸 合計 {len(image_urls)}枚 の画像を選択済み")
            continue
        else:
            # 空欄 = 選択終了
            break

    # 投稿
    print("\n📤 Threadsに投稿中...")
    try:
        if len(image_urls) >= 2:
            result = threads_client.publish_carousel_post(post_text, image_urls)
        elif len(image_urls) == 1:
            result = threads_client.publish_image_post(post_text, image_urls[0])
        else:
            result = threads_client.publish_text_post(post_text)
        post_id = result.get("id", "")
        print(f"   ✅ 投稿完了! ID: {post_id}")

        # ログ記録
        _log_content({
            "topic": topic_key,
            "post_text": post_text,
            "post_id": post_id,
            "image_urls": image_urls if image_urls else None,
            "timestamp": datetime.now().isoformat(),
        })
        print(f"\n   ✅ 完了!")

    except Exception as e:
        print(f"   ❌ 投稿エラー: {e}")
        _add_draft(post_text, topic_key, image_keywords)
        print("   （投稿文は下書きに戻しました）")


def _load_past_content() -> list[str]:
    """過去のコンテンツ投稿文を読み込む"""
    posts = []
    if os.path.exists(CONTENT_LOG):
        with open(CONTENT_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    posts.append(entry.get("post_text", ""))
                except json.JSONDecodeError:
                    pass
    return posts


def _log_content(entry: dict):
    """コンテンツ投稿ログを記録"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(CONTENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _planned_datetime(item_code: str) -> datetime | None:
    """ストックの下書きに入っている投稿予定日時"""
    draft = load_cc_draft(item_code)
    if not draft or not draft.get("planned_date"):
        return None
    return datetime.fromisoformat(f"{draft['planned_date']}T{draft.get('planned_time') or '12:30'}")


def _ask_schedule_time(default: datetime | None = None) -> str | None:
    """予約日時を対話的に取得する（日本時間）。Enterでストックの予定日時"""
    now = datetime.now()
    print("\n   📅 予約日時（日本時間）")
    print("   形式: YYYY-MM-DD HH:MM　例: 2026-09-16 12:30")
    print("   '12' と入力 → 明日 12:30　／　'18' と入力 → 明日 18:30")

    default_text = default.strftime("%Y-%m-%d %H:%M") if default and default > now else ""
    time_input = ask("   日時", default_text).strip()
    if not time_input:
        return None

    if time_input in ("12", "18"):
        tomorrow = now.date() + timedelta(days=1)
        scheduled = datetime.fromisoformat(f"{tomorrow}T{time_input}:30")
    else:
        try:
            scheduled = datetime.strptime(time_input, "%Y-%m-%d %H:%M")
        except ValueError:
            print("   ⚠️ 日時は YYYY-MM-DD HH:MM で入力してください")
            return None

    if scheduled <= now + timedelta(minutes=5):
        print("   ⚠️ 5分以上先の日時を指定してください（すぐ出すなら「1. 今すぐ投稿する」）")
        return None
    print(f"   → {scheduled:%Y-%m-%d %H:%M} に予約")
    return scheduled.isoformat()


def _print_sync_summary(summary: dict):
    for line in summary["posted"]:
        print(f"   ✅ 予約投稿済み: {line}")
    for line in summary["partial"]:
        print(f"   ⚠️ 本文は投稿済みだが、リンクの返信が付けられなかった: {line}")
        print("      → Threadsアプリでその投稿に返信し、リンクと「pr」を手で付けてください（管理表の返信文をコピー）")
    for line in summary["failed"]:
        print(f"   ❌ 予約投稿できなかった（ストックに戻しました）: {line}")
    if summary["failed"]:
        print("   → トークン切れなら、GitHubのSecrets「THREADS_ACCESS_TOKEN」を新しいトークンに差し替えてください")


def show_queue():
    """予約一覧を表示し、番号を選ぶと取り消せる"""
    print("\n   GitHubから最新の状態を取得中...")
    _print_sync_summary(queue_sync.sync_results())

    while True:
        pending = queue_sync.pending_entries()
        if not pending:
            print("\n   📭 予約投稿はありません\n")
            return

        print(f"\n   📅 予約投稿一覧（{len(pending)}件）")
        for i, (_, entry) in enumerate(pending, 1):
            when = entry["scheduled_at"][:16].replace("T", " ")
            label = entry.get("label") or entry.get("name", "")[:24]
            print(f"   {i}. {when}  {label}  （画像{len(entry.get('image_urls', []))}枚）")

        choice = ask("\n   取り消す番号（空欄で終了）", "")
        if not choice:
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(pending):
            print("   ⚠️ 番号が正しくありません")
            continue
        path, entry = pending[int(choice) - 1]
        if ask_yn(f"   「{entry.get('label') or entry.get('name', '')[:24]}」の予約を取り消しますか？", False):
            if queue_sync.cancel(path):
                print("   ✅ 取り消しました（下書きはストックに戻ります）")
                export_sheet_quietly()


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


def _pick_from_stock() -> str | None:
    """作成済みストックから1件選び、その商品URLを返す"""
    stock = [d for d in load_stock() if cc_draft_ready(d)]
    if not stock:
        print("   ⚠️ 作成済みのストックがありません")
        print("   → Claude Codeに「この商品の投稿文を作って <URL>」と依頼してください")
        return None

    today = date.today().isoformat()
    print("\n   📦 ストック（上から投稿順）:")
    for i, draft in enumerate(stock, 1):
        print(f"   {i}. {format_stock_line(draft, today)}")

    choice = ask("   番号を選択", "1")
    if not choice.isdigit() or not 1 <= int(choice) <= len(stock):
        print("   ⚠️ 番号が正しくありません")
        return None

    draft = stock[int(choice) - 1]
    if draft.get("queued_at"):
        print(f"   ⚠️ この商品は予約済みです（{draft['queued_at'][:16].replace('T', ' ')}）")
        if not ask_yn("   予約を置き換えて進めますか？", False):
            return None
    planned = draft.get("planned_date")
    if planned and planned > today:
        print(f"   ℹ️ 予定日（{planned}）より前ですが、このまま進めます")
    return post_url(draft)


def _confirm_daily_pace() -> bool:
    """1日の投稿数が上限に達していたら確認する"""
    count = posts_today()
    if count < MAX_POSTS_PER_DAY:
        return True
    print(f"\n   ⚠️ 今日はすでに{count}件投稿しています（目安は1日{MAX_POSTS_PER_DAY}件まで）")
    print("   楽天アフィリエイトは、繰り返し投稿がスパムと判定されると利用停止の対象になります")
    return ask_yn("   それでも続けますか？", False)


def main():
    # --queue オプション: 予約一覧を表示して終了
    if len(sys.argv) > 1 and sys.argv[1] == "--queue":
        show_queue()
        return

    print(f"\n{'='*50}")
    print(f"🏠 Threads投稿ツール")
    print(f"{'='*50}")
    print(f"\n   qで終了 / Ctrl+Cでいつでも中断可能\n")

    uploader = get_uploader("imgbb")
    threads_client = ThreadsClient()
    posted_items = load_posted_items()

    # 下書きや画像選択をした後で投稿に失敗しないよう、先にトークンを確認
    token_ok, token_message = threads_client.check_token()
    if token_ok:
        age = token_age_days()
        age_note = f"（トークン保存から{age}日）" if age is not None else ""
        print(f"   ✅ Threads: @{token_message}{age_note}")
        if age is not None and age >= REFRESH_WARNING_DAYS:
            print("   ⚠️ トークンの期限（60日）が近いです → py token_tool.py refresh")
    else:
        print(f"   ❌ Threadsトークンが無効です: {token_message}")
        print("   → 投稿はできません。手順書「Threadsトークンの再発行」を参照")
        if not ask_yn("   このまま続けますか？（下書きの確認だけ）", False):
            return

    # GitHub Actions で予約投稿された結果を取り込む（投稿ログ・ストック・管理表に反映）
    summary = queue_sync.sync_results()
    _print_sync_summary(summary)
    pending = queue_sync.pending_entries()
    if pending:
        print(f"   📅 予約中: {len(pending)}件（一覧・取り消し: py tool.py --queue）")
    if any(summary.values()):
        export_sheet_quietly()

    while True:
        print()
        print_stock_summary()
        today = date.today().isoformat()
        has_due = any(
            cc_draft_ready(d) and d.get("planned_date") and d["planned_date"] <= today and not d.get("queued_at")
            for d in load_stock()
        )

        print("\n   ┌─────────────────────────┐")
        print("   │ 1. 商品投稿（楽天URL）   │")
        print("   │ 2. コンテンツ投稿（リンクなし）│")
        print("   │ 3. ストックから投稿      │")
        print("   └─────────────────────────┘")
        mode = ask("   選択", "3" if has_due else "1")

        if mode == "2":
            process_content_post(threads_client)
            continue

        if mode == "3":
            url = _pick_from_stock()
            if url and _confirm_daily_pace():
                process_one_product(url, uploader, threads_client, posted_items)
                posted_items = load_posted_items()
            continue

        # --- 商品投稿モード ---
        url_input = ask("\n🔗 楽天URL")

        if not url_input:
            continue

        # 複数URL対応（スペース区切り）
        urls = url_input.split()
        urls = [u for u in urls if "rakuten.co.jp" in u or "r10.to" in u]

        if not urls:
            print("   ⚠️ 楽天のURLを入力してください")
            continue

        for url in urls:
            if not _confirm_daily_pace():
                break
            process_one_product(url, uploader, threads_client, posted_items)
            posted_items = load_posted_items()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, UserQuit):
        print("\n\n👋 終了します\n")
