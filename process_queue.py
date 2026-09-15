"""予約キュー処理: 投稿時刻が来た予約をThreadsに投稿する（GitHub Actions用）

    python process_queue.py --check   時間が来た予約の件数だけを出す（標準ライブラリのみ・投稿しない）
    python process_queue.py           時間が来た予約を投稿する

公開リポジトリで動くので、ログやキューファイルにトークンを出さない（queue_store.redact を通す）。
投稿ログ（posts_log.jsonl）は書かない。結果はキューファイルの status に残し、PC側の tool.py が取り込む。
"""

import os
import sys
from datetime import datetime

import queue_store as qs


def count_due() -> int:
    now = qs.now_jst()
    return sum(1 for _, entry in qs.load_entries() if qs.is_due(entry, now))


def _label(entry: dict) -> str:
    return entry.get("label") or entry.get("name", "")[:20]


def _record_error(path: str, entry: dict, error: Exception):
    entry["retry_count"] = entry.get("retry_count", 0) + 1
    entry["last_error"] = qs.redact(error)[:300]
    if entry["retry_count"] >= 3:
        entry["status"] = qs.STATUS_ERROR
        print(f"      ❌ 3回失敗したので中止: {entry['last_error']}")
    else:
        print(f"      ⚠️ 失敗（{entry['retry_count']}/3回目、次回また試す）: {entry['last_error']}")
    qs.save_entry(path, entry)


def process_queue() -> int:
    from threads_api import ThreadsClient

    now = qs.now_jst()
    entries = qs.load_entries()
    print(f"📅 予約キュー処理: {now:%Y-%m-%d %H:%M}（JST） 予約{len(entries)}件")

    client = ThreadsClient(
        user_id=os.environ.get("THREADS_USER_ID", ""),
        access_token=os.environ.get("THREADS_ACCESS_TOKEN", ""),
    )
    posted = 0

    for path, entry in entries:
        if not qs.is_due(entry, now):
            continue

        scheduled = datetime.fromisoformat(entry["scheduled_at"])
        if now - scheduled > qs.MAX_DELAY:
            entry["status"] = qs.STATUS_EXPIRED
            entry["last_error"] = f"予定時刻（{scheduled:%m/%d %H:%M}）から3時間以上遅れたため投稿しなかった"
            qs.save_entry(path, entry)
            print(f"   ⏭️ 期限切れ: {_label(entry)}")
            continue

        print(f"   📤 {scheduled:%m/%d %H:%M} {_label(entry)}")

        # 本文の投稿が済んでいたら（前回は返信だけ失敗した）、本文は出し直さない
        if not entry.get("post_id"):
            try:
                image_urls = entry["image_urls"]
                if len(image_urls) >= 2:
                    result = client.publish_carousel_post(text=entry["post_text"], image_urls=image_urls)
                else:
                    result = client.publish_image_post(text=entry["post_text"], image_url=image_urls[0])
                entry["post_id"] = result.get("id", "")
                entry["posted_at"] = qs.now_jst().isoformat(timespec="seconds")
                qs.save_entry(path, entry)
                print(f"      ✅ 本文を投稿")
            except Exception as e:
                _record_error(path, entry, e)
                continue

        try:
            reply = client.publish_reply(text=entry["reply_text"], reply_to_id=entry["post_id"])
            entry["reply_id"] = reply.get("id", "")
            entry["status"] = qs.STATUS_POSTED
            entry.pop("last_error", None)
            qs.save_entry(path, entry)
            posted += 1
            print(f"      ✅ 返信（リンク）を投稿")
        except Exception as e:
            _record_error(path, entry, e)

    print(f"✅ 完了: {posted}件投稿")
    return posted


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--check" in sys.argv:
        due = count_due()
        print(f"時間が来た予約: {due}件")
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as f:
                f.write(f"due={due}\n")
    else:
        process_queue()
