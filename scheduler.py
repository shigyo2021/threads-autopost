"""スケジューラー: 1日3回の自動投稿（常駐プロセス用）

使い方:
  python scheduler.py

  または systemd / pm2 / supervisor 等でデーモン化:
  pm2 start scheduler.py --interpreter python3
"""

import io
import sys

# Windows環境でのUnicode出力対応
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import schedule
import time
import random
from datetime import datetime
from config import POST_TIMES, RAKUTEN_GENRES, ROOM_STYLES


def scheduled_post():
    """定時投稿ジョブ"""
    from main import run_pipeline

    category = random.choice(list(RAKUTEN_GENRES.keys()))
    style = random.choice(list(ROOM_STYLES.keys()))

    print(f"\n⏰ 定時投稿実行: {datetime.now().strftime('%H:%M:%S')}")
    print(f"   カテゴリ: {category} / スタイル: {ROOM_STYLES[style]['name']}")

    try:
        run_pipeline(
            count=1,
            category=category,
            style=style,
            dry_run=False,
            upload_method="imgbb",
        )
    except Exception as e:
        print(f"❌ 投稿エラー: {e}")


def token_refresh_job():
    """Threadsアクセストークンの自動更新（週1回）"""
    from threads_api import ThreadsClient
    try:
        client = ThreadsClient()
        new_token = client.refresh_long_lived_token()
        # .env を更新（本番では Secret Manager 推奨）
        print(f"🔑 トークン更新完了")
    except Exception as e:
        print(f"❌ トークン更新エラー: {e}")


def main():
    print("🤖 Threads自動投稿スケジューラー起動")
    print(f"   投稿時刻: {', '.join(POST_TIMES)}")
    print()

    # 各投稿時刻にジョブを設定
    for post_time in POST_TIMES:
        schedule.every().day.at(post_time).do(scheduled_post)
        print(f"   ✅ {post_time} に投稿をスケジュール")

    # 週1回トークン更新
    schedule.every().sunday.at("03:00").do(token_refresh_job)
    print(f"   ✅ 毎週日曜 03:00 にトークン更新")

    print(f"\n🟢 スケジューラー稼働中... (Ctrl+C で停止)")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
