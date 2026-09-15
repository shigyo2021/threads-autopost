"""接続チェックとThreadsトークン管理

    py token_tool.py check     Threads・楽天API・Claude APIの接続を確認（投稿はしない）
    py token_tool.py set       コピーしたThreadsトークンを確認して .env に保存
    py token_tool.py exchange  コピーしたトークンを60日トークンに交換して保存（交換不要ならそのまま保存）
    py token_tool.py refresh   有効な長期トークンの期限を60日延長して保存
    py token_tool.py copy      GitHubのSecretsに貼るため、トークンをクリップボードにコピー（画面には出さない）
"""

import os
import platform
import re
import subprocess
import sys
from datetime import date, datetime

import requests

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
TOKEN_LIFETIME_DAYS = 60
REFRESH_WARNING_DAYS = 50


def _write_env(updates: dict):
    """.env の指定キーだけを書き換える（他の行はそのまま）"""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

    remaining = dict(updates)
    for i, line in enumerate(lines):
        match = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines[i] = f"{key}={remaining.pop(key)}"
    lines += [f"{key}={value}" for key, value in remaining.items()]

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _save_token(token: str):
    _write_env({
        "THREADS_ACCESS_TOKEN": token,
        "THREADS_TOKEN_SAVED_AT": date.today().isoformat(),
    })
    print(f"✅ .env に保存しました（{date.today().isoformat()}）")
    print("   ⚠️ 予約投稿を使うなら、GitHubのSecrets「THREADS_ACCESS_TOKEN」も差し替えてください → py token_tool.py copy")


def token_age_days() -> int | None:
    """トークンを保存してからの日数（記録がなければNone）"""
    from dotenv import dotenv_values

    saved_at = dotenv_values(ENV_PATH).get("THREADS_TOKEN_SAVED_AT")
    if not saved_at:
        return None
    try:
        return (date.today() - datetime.strptime(saved_at, "%Y-%m-%d").date()).days
    except ValueError:
        return None


def _check_threads(token: str) -> bool:
    from threads_api import ThreadsClient

    ok, message = ThreadsClient(access_token=token).check_token()
    if ok:
        age = token_age_days()
        age_note = f"／保存から{age}日" if age is not None else ""
        print(f"✅ Threads: @{message}{age_note}")
        if age is not None and age >= REFRESH_WARNING_DAYS:
            print(f"   ⚠️ 期限（{TOKEN_LIFETIME_DAYS}日）が近いです → py token_tool.py refresh")
    else:
        print(f"❌ Threads: {message}")
        print("   → 手順書「Threadsトークンの再発行」を参照")
    return ok


def cmd_check():
    from config import (THREADS_ACCESS_TOKEN, RAKUTEN_APP_ID, RAKUTEN_ACCESS_KEY,
                        RAKUTEN_AFFILIATE_ID, ANTHROPIC_API_KEY, CLAUDE_MODEL)
    from rakuten_api import SEARCH_URL

    _check_threads(THREADS_ACCESS_TOKEN)

    resp = requests.get(SEARCH_URL, params={
        "format": "json", "applicationId": RAKUTEN_APP_ID, "accessKey": RAKUTEN_ACCESS_KEY,
        "affiliateId": RAKUTEN_AFFILIATE_ID, "keyword": "収納", "hits": 1,
    }, timeout=15)
    if resp.ok:
        print("✅ 楽天API")
    else:
        print(f"❌ 楽天API: {resp.status_code} {resp.text[:120]}")
        print("   → rakuten_api.py の SEARCH_URL の日付が古い可能性")

    # モデル情報の取得はクレジットを消費しない
    import anthropic
    try:
        anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).models.retrieve(CLAUDE_MODEL)
        print(f"✅ Claude API: {CLAUDE_MODEL}")
    except Exception as e:
        print(f"❌ Claude API: {type(e).__name__} {str(e)[:120]}")
        print("   → Claude Codeの下書きモードなら投稿はできます")


def _read_clipboard() -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return result.stdout


def _read_secret(label: str) -> str:
    """
    秘密の文字列をクリップボードから読む。
    Windowsの非表示入力（getpass）に Ctrl+V すると、貼り付けではなく制御文字が入ることがあるため。
    """
    input(f"{label}をコピーしてからEnter（クリップボードから読み込みます）: ")
    raw = _read_clipboard()
    # 改行・空白・制御文字・前後の引用符を除去
    value = re.sub(r"[\s\x00-\x1f\x7f]", "", raw).strip("\"'")
    if not value:
        print("   ⚠️ クリップボードが空でした")
    return value


def _describe_token(token: str):
    """中身を出さずに形式だけ表示する"""
    print(f"   読み込んだトークン: {len(token)}文字／先頭「{token[:2]}」")
    if not token.startswith("TH"):
        print("   ⚠️ Threadsのトークンは通常「TH」で始まります。")
        print("      Facebook/Instagram用のトークン（EAA… / IG…）をコピーしていないか確認してください")


def _exchange_for_long_lived(token: str, app_secret: str) -> str | None:
    resp = requests.get("https://graph.threads.net/access_token", params={
        "grant_type": "th_exchange_token",
        "client_secret": app_secret,
        "access_token": token,
    }, timeout=15)
    if resp.ok:
        return resp.json()["access_token"]
    message = resp.json().get("error", {}).get("message", resp.text[:150]) \
        if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:150]
    print(f"   交換できませんでした: {message}")
    return None


def _save_new_token(try_exchange: bool):
    token = _read_secret("Threadsのアクセストークン")
    if not token:
        return
    _describe_token(token)

    if not _check_threads(token):
        print("保存しませんでした（トークンが無効）")
        return

    if try_exchange:
        from dotenv import dotenv_values

        app_secret = dotenv_values(ENV_PATH).get("THREADS_APP_SECRET") or \
            _read_secret("ThreadsアプリのApp Secret")
        long_token = _exchange_for_long_lived(token, app_secret) if app_secret else None
        if long_token and _check_threads(long_token):
            print("   → 60日トークンに交換しました")
            _save_token(long_token)
            return
        print("   → 受け取ったトークンをそのまま保存します（すでに60日トークンの場合は交換不要）")

    _save_token(token)


def cmd_set():
    _save_new_token(try_exchange=False)


def cmd_exchange():
    _save_new_token(try_exchange=True)


def cmd_refresh():
    from threads_api import ThreadsClient
    from config import THREADS_ACCESS_TOKEN

    try:
        new_token = ThreadsClient(access_token=THREADS_ACCESS_TOKEN).refresh_long_lived_token()
    except Exception as e:
        print(f"❌ 延長に失敗: {str(e)[:200]}")
        print("   期限切れのトークンは延長できません → py token_tool.py set で再発行したものを保存")
        return
    _save_token(new_token)


def cmd_copy():
    """GitHubのSecretsに貼るため、.env のトークンをクリップボードにコピーする（画面には出さない）"""
    from dotenv import dotenv_values

    token = dotenv_values(ENV_PATH).get("THREADS_ACCESS_TOKEN", "")
    if not token:
        print("❌ .env にトークンがありません")
        return
    subprocess.run(["clip"], input=token.encode("utf-16-le"), check=True)
    print(f"✅ Threadsのトークンをクリップボードにコピーしました（{len(token)}文字）")
    print("   GitHubのリポジトリ → Settings → Secrets and variables → Actions → THREADS_ACCESS_TOKEN の")
    print("   鉛筆アイコン（Update）を開いて貼り付け → Update secret")
    print("   ⚠️ 貼り付けたら、ほかの場所に貼らないよう、別の文字をコピーしてクリップボードを上書きしてください")


COMMANDS = {"check": cmd_check, "set": cmd_set, "exchange": cmd_exchange, "refresh": cmd_refresh, "copy": cmd_copy}

if __name__ == "__main__":
    if platform.system() == "Windows":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
