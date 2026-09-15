"""予約投稿のPC側: 予約の作成・GitHubへの送信・投稿結果の取り込み・取り消し

GitHub には output/queue/ だけを送る。送る前に差分を検査し、トークンらしき文字列があれば中止する。
"""

import json
import os
import subprocess
from datetime import datetime

import queue_store as qs
from config import POSTS_LOG, OUTPUT_DIR
from drafts import load_draft, save_draft, mark_draft_used

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_PATHSPEC = "output/queue"


def _git(*args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args], cwd=REPO_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return result.returncode, qs.redact((result.stdout + result.stderr).strip())


def pull() -> bool:
    code, out = _git("pull", "--rebase", "--autostash", "--quiet")
    if code != 0:
        print(f"   ⚠️ GitHubから最新の予約を取得できませんでした: {out[:200]}")
    return code == 0


def push_queue(message: str) -> bool:
    """予約キューの変更だけをコミットしてpushする。トークンらしき文字列が含まれていたら中止する"""
    _git("add", "-A", QUEUE_PATHSPEC)
    code, _ = _git("diff", "--staged", "--quiet", "--", QUEUE_PATHSPEC)
    if code == 0:
        return True  # 変更なし

    raw = subprocess.run(["git", "diff", "--staged", "--", QUEUE_PATHSPEC], cwd=REPO_DIR,
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    if qs.contains_secret(raw):
        _git("reset", "--quiet", "--", QUEUE_PATHSPEC)
        print("   ❌ 予約データにトークンやキーらしき文字列があったため、GitHubへの送信を中止しました")
        return False

    code, out = _git("commit", "--quiet", "-m", message, "--", QUEUE_PATHSPEC)
    if code != 0:
        print(f"   ⚠️ コミットに失敗: {out[:200]}")
        return False

    for _ in range(3):
        code, out = _git("push", "--quiet")
        if code == 0:
            return True
        pull()
    print(f"   ❌ GitHubに送れませんでした: {out[:200]}")
    print("   → このままでは予約投稿されません。ネット接続を確認して、もう一度 py tool.py を起動してください")
    return False


def count_on_date(date_str: str) -> int:
    """その日に予約されている件数（取り込み前の投稿済みも含む）"""
    return sum(
        1 for _, e in qs.load_entries()
        if e.get("scheduled_at", "").startswith(date_str) and e.get("status") in (qs.STATUS_PENDING, qs.STATUS_POSTED)
    )


def schedule(entry: dict) -> bool:
    """
    予約を作ってGitHubに送る。
    entry には item_code, label, name, price, url, item_url, post_text, reply_text, image_urls,
    style, quality_score, text_source, scheduled_at を入れる。
    """
    pull()
    # 同じ商品のまだ投稿されていない予約があれば置き換える
    for path, existing in qs.load_entries():
        if existing.get("item_code") == entry["item_code"] and existing.get("status") == qs.STATUS_PENDING:
            os.remove(path)
            print("   ℹ️ 同じ商品の予約を置き換えます")

    entry = {**entry, "status": qs.STATUS_PENDING, "created_at": qs.now_jst().isoformat(timespec="seconds")}
    path = os.path.join(qs.QUEUE_DIR, qs.entry_filename(entry["scheduled_at"], entry["item_code"]))
    try:
        qs.save_entry(path, entry)
    except ValueError as e:
        print(f"   ❌ {e}")
        return False

    if not push_queue(f"Schedule post {entry['scheduled_at'][:16]}"):
        return False

    draft = load_draft(entry["item_code"])
    if draft:
        scheduled = datetime.fromisoformat(entry["scheduled_at"])
        draft["queued_at"] = entry["scheduled_at"]
        draft["planned_date"] = scheduled.date().isoformat()
        draft["planned_time"] = scheduled.strftime("%H:%M")
        save_draft(draft)
    return True


def _append_posts_log(entry: dict):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log = {
        "item_code": entry.get("item_code", ""),
        "name": entry.get("name", ""),
        "price": entry.get("price", 0),
        "url": entry.get("url", ""),
        "item_url": entry.get("item_url", ""),
        "style": entry.get("style", ""),
        "image_urls": entry.get("image_urls", []),
        "post_text": entry.get("post_text", ""),
        "quality_score": entry.get("quality_score"),
        "text_source": entry.get("text_source", ""),
        "timestamp": entry.get("posted_at") or entry.get("scheduled_at"),
        "dry_run": False,
        "scheduled": True,
        "post_id": entry.get("post_id", ""),
    }
    with open(POSTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(log, ensure_ascii=False) + "\n")


def _unqueue_draft(item_code: str):
    draft = load_draft(item_code)
    if draft and draft.pop("queued_at", None):
        save_draft(draft)


def sync_results() -> dict:
    """GitHub Actions の投稿結果を取り込む。投稿済み→ログに記録してストックから外す／失敗→ストックに戻す"""
    summary = {"posted": [], "partial": [], "failed": []}
    if not pull():
        return summary

    changed = False
    for path, entry in qs.load_entries():
        status = entry.get("status")
        label = entry.get("label") or entry.get("name", "")[:20]
        when = entry.get("scheduled_at", "")[5:16].replace("T", " ")

        if status == qs.STATUS_POSTED:
            _append_posts_log(entry)
            mark_draft_used(entry["item_code"])
            summary["posted"].append(f"{when} {label}")
        elif status == qs.STATUS_ERROR and entry.get("post_id"):
            # 本文は出ている。ストックに戻すと同じ本文を二重に投稿してしまうので、投稿済みとして扱う
            _append_posts_log(entry)
            mark_draft_used(entry["item_code"])
            summary["partial"].append(f"{when} {label}")
        elif status in (qs.STATUS_ERROR, qs.STATUS_EXPIRED):
            _unqueue_draft(entry["item_code"])
            summary["failed"].append(f"{when} {label}: {qs.redact(entry.get('last_error', ''))[:120]}")
        else:
            continue
        os.remove(path)
        changed = True

    if changed:
        push_queue("Import scheduled post results")
    return summary


def cancel(path: str) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        entry = json.load(f)
    if entry.get("status") != qs.STATUS_PENDING:
        print("   ⚠️ すでに処理された予約は取り消せません")
        return False
    os.remove(path)
    _unqueue_draft(entry["item_code"])
    return push_queue(f"Cancel scheduled post {entry['scheduled_at'][:16]}")


def pending_entries() -> list[tuple[str, dict]]:
    return [(p, e) for p, e in qs.load_entries() if e.get("status") == qs.STATUS_PENDING]
