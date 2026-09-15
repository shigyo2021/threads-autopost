"""予約投稿キュー: 1予約=1ファイル（output/queue/*.json）で、PCとGitHub Actionsが共有する

- PC（tool.py）が予約を作って GitHub に push する
- GitHub Actions（process_queue.py）が時間になったら投稿し、status を書き換えて push する
- PC（tool.py の起動時）が結果を取り込み、投稿ログに記録してファイルを消す

公開リポジトリに置くので、トークンやキーは絶対に書き込まない（redact() を通す）。
標準ライブラリだけで動くようにしている（GitHub Actions の事前チェックで使うため）。
"""

import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone

QUEUE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "queue")

JST = timezone(timedelta(hours=9))

STATUS_PENDING = "pending"
STATUS_POSTED = "posted"
STATUS_ERROR = "error"
STATUS_EXPIRED = "expired"

# 予定時刻からこれ以上遅れたら投稿しない（GitHub Actions が長時間止まっていたときに深夜に出ないように）
MAX_DELAY = timedelta(hours=3)

_SECRET_ENV_NAMES = (
    "THREADS_ACCESS_TOKEN", "THREADS_APP_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "RAKUTEN_APP_ID", "RAKUTEN_ACCESS_KEY", "IMGBB_API_KEY", "PEXELS_API_KEY",
)
_SECRET_PATTERNS = [
    # 値が「数字を含む16文字以上」のときだけ（コード中の access_token=変数名 は対象外）
    (re.compile(r"(access_token|client_secret|accessKey|applicationId|api_key|apikey)=(?=[A-Za-z_.%-]*\d)[A-Za-z0-9_.%-]{16,}", re.I), r"\1=***"),
    (re.compile(r"\bTH[A-Za-z0-9_-]{30,}"), "TH***"),
    (re.compile(r"\bEAA[A-Za-z0-9]{30,}"), "EAA***"),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"), "sk-***"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.I), "Bearer ***"),
]


def redact(text: str) -> str:
    """トークンやキーらしき文字列を伏せ字にする（ログ・キューファイル・画面表示の前に必ず通す）"""
    text = str(text)
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name, "")
        if len(value) >= 8:
            text = text.replace(value, "***")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def contains_secret(text: str) -> bool:
    return redact(text) != str(text)


def now_jst() -> datetime:
    """日本時間の現在時刻（タイムゾーンなし）。GitHub Actions（UTC）でもPCでも同じ値になる"""
    return datetime.now(JST).replace(tzinfo=None)


def entry_filename(scheduled_at: str, item_code: str) -> str:
    stamp = datetime.fromisoformat(scheduled_at).strftime("%Y%m%d-%H%M")
    return f"{stamp}_{item_code.replace(':', '_')}.json"


def load_entries() -> list[tuple[str, dict]]:
    """(ファイルパス, 予約) を予定時刻順に返す"""
    entries = []
    for path in glob.glob(os.path.join(QUEUE_DIR, "*.json")):
        with open(path, "r", encoding="utf-8") as f:
            entries.append((path, json.load(f)))
    entries.sort(key=lambda pe: pe[1].get("scheduled_at", ""))
    return entries


def save_entry(path: str, entry: dict):
    text = json.dumps(entry, ensure_ascii=False, indent=2)
    if contains_secret(text):
        raise ValueError("予約データにトークンやキーらしき文字列が含まれているため保存を中止しました")
    os.makedirs(QUEUE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")


def is_due(entry: dict, now: datetime | None = None) -> bool:
    now = now or now_jst()
    return entry.get("status") == STATUS_PENDING and datetime.fromisoformat(entry["scheduled_at"]) <= now
