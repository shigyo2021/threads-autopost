"""予約キュー処理: 投稿時刻が来た予約をThreadsに投稿する（GitHub Actions用）"""

import io
import sys
import platform

if platform.system() == "Windows":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import json
import os
from datetime import datetime

from config import OUTPUT_DIR, POSTS_LOG
from threads_api import ThreadsClient

QUEUE_FILE = os.path.join(OUTPUT_DIR, "post_queue.json")


def load_queue() -> list[dict]:
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_queue(queue: list[dict]):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)


def log_post(entry: dict):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(POSTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def process_queue():
    """投稿時刻が来た予約を処理する"""
    queue = load_queue()
    now = datetime.now()
    posted_count = 0
    changed = False

    print(f"📅 予約キュー処理: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   キュー内: {len(queue)}件\n")

    threads_client = ThreadsClient()

    for entry in queue:
        if entry.get("status") != "pending":
            continue

        scheduled_at = entry.get("scheduled_at", "")
        try:
            scheduled_time = datetime.fromisoformat(scheduled_at)
        except (ValueError, TypeError):
            print(f"   ⚠️ 無効な日時: {scheduled_at}、スキップ")
            entry["status"] = "error"
            entry["error"] = "Invalid scheduled_at"
            changed = True
            continue

        if scheduled_time > now:
            remaining = scheduled_time - now
            hours = remaining.total_seconds() / 3600
            print(f"   ⏳ {entry['name'][:30]} → あと{hours:.1f}時間")
            continue

        # --- 投稿時刻到達、投稿実行 ---
        print(f"   📤 投稿中: {entry['name'][:40]}")

        try:
            post_text = entry["post_text"]
            reply_text = entry["reply_text"]
            image_urls = entry["image_urls"]

            # メイン投稿
            if len(image_urls) >= 2:
                result = threads_client.publish_carousel_post(
                    text=post_text, image_urls=image_urls,
                )
            else:
                result = threads_client.publish_image_post(
                    text=post_text, image_url=image_urls[0],
                )
            post_id = result.get("id", "")
            print(f"      ✅ メイン投稿完了! ID: {post_id}")

            # 返信
            reply_result = threads_client.publish_reply(
                text=reply_text, reply_to_id=post_id,
            )
            print(f"      ✅ 返信投稿完了! ID: {reply_result.get('id', '')}")

            # ステータス更新
            entry["status"] = "posted"
            entry["posted_at"] = now.isoformat()
            entry["post_id"] = post_id
            posted_count += 1
            changed = True

            # ログ記録
            log_post({
                "item_code": entry.get("item_code", ""),
                "name": entry.get("name", ""),
                "price": entry.get("price", 0),
                "url": entry.get("affiliate_url", ""),
                "style": entry.get("style", ""),
                "post_text": post_text,
                "quality_score": entry.get("quality_score", 0),
                "timestamp": now.isoformat(),
                "dry_run": False,
                "scheduled": True,
            })

        except Exception as e:
            print(f"      ❌ 投稿エラー: {e}")
            # リトライ回数を管理（3回まで再試行、超えたらエラー確定）
            retry_count = entry.get("retry_count", 0) + 1
            entry["retry_count"] = retry_count
            entry["last_error"] = str(e)
            changed = True

            if retry_count >= 3:
                entry["status"] = "error"
                print(f"      ⚠️ 3回失敗のため諦めます")
            else:
                # pending のまま残して次回再試行
                print(f"      🔄 次回再試行します（{retry_count}/3回目）")

    # 完了済み・エラーのエントリを削除してキューを整理
    if changed:
        # pending以外を削除
        cleaned = [e for e in queue if e.get("status") == "pending"]
        removed = len(queue) - len(cleaned)
        save_queue(cleaned)
        if removed > 0:
            print(f"\n   キュー整理: {removed}件削除、{len(cleaned)}件残り")
        elif cleaned:
            print(f"\n   キュー: {len(cleaned)}件が次回再試行待ち")

    print(f"\n✅ 処理完了: {posted_count}件投稿")
    return posted_count


if __name__ == "__main__":
    process_queue()
