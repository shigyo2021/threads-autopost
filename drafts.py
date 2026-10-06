"""Claude Code 下書き（ストック）: APIを使わずに、Claude Codeが書いた投稿文をtool.pyへ渡す

    py drafts.py fetch <楽天URL>          商品情報を取得し、空の下書きファイルを作る
    （下書きファイルの post_text / reply_text を書き込む）
    py drafts.py check <item_code>        ルールチェック + 過去投稿との類似度チェック
    py drafts.py list                     ストック一覧（投稿予定日・時間つき）
    py drafts.py plan [1日の件数]          予定のない下書きに、空いている投稿枠を順番に割り当てる（既定: 1日2件）。
                                          ¥5,000以上の商品はセール・ポイントアップの日に寄せる
    py drafts.py replan [1日の件数]        手動で決めた予定と予約済み以外をいったん外して、割り当て直す
                                          （手動で決めた予定でも、日付が過ぎたものは組み直す）
    py drafts.py sale                     セール期間と、これから30日のセールの日を一覧
    py drafts.py sale add <開始日> <終了日> <名前>
                                          お買い物マラソンなどの期間を登録（終了日は最後の丸1日。1:59終了なら前日）
    py drafts.py sale del <開始日>         登録した期間を消す
    py drafts.py next                     次の投稿枠のリマインダー文を1行で出す
    py drafts.py content                  リンクなし投稿の下書き（content_drafts.json）の一覧とチェック
    py drafts.py link <item_code> <Threads用リンク>
                                          SNSボタンで作ったThreads用リンクを下書きに付ける（返信にそのリンクを使う）
    py drafts.py date <item_code> <日付> [時間]
                                          予定を手動で決める（YYYY-MM-DD [HH:MM] / none で解除）。
                                          手動で決めた予定は replan でも動かない

tool.py 側は「3. ストックから投稿」か、URLを貼って「2. Claude Code の下書きを使う」で読み込む。
"""

import glob
import html
import json
import os
import platform
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from config import OUTPUT_DIR, POSTS_LOG

DRAFTS_DIR = os.path.join(OUTPUT_DIR, "drafts")
USED_DIR = os.path.join(DRAFTS_DIR, "used")

# 楽天アフィリエイトのスパム判定を避けるための上限（手動・自動を問わず）。リンクなし投稿は数えない
MAX_POSTS_PER_DAY = 2

# 商品投稿の時間枠（上から順に使う）
POST_SLOTS = ["12:30", "18:30"]

# 単価の高い商品は、楽天市場のポイントアップの日に寄せる（plan / replan）。
# クリックから24時間以内にかごに入れないと成果にならないので、「今日買う理由」がある日に出す
HIGH_PRICE = 5000

# 毎月決まった日（2026-09-27に楽天カード公式・キャンペーン一覧で確認）
MONTHLY_SALE_DAYS = {
    1: "ワンダフルデー",
    18: "ご愛顧感謝デー",
    **{d: "5と0のつく日" for d in (5, 10, 15, 20, 25, 30)},
}

# お買い物マラソン・楽天スーパーSALEは毎回日程が違うので、発表されたら `py drafts.py sale add` で登録する
SALE_PERIODS_FILE = os.path.join(OUTPUT_DIR, "sale_periods.json")


def draft_path(item_code: str, used: bool = False) -> str:
    # item_code は "shop:12345" 形式。Windowsのファイル名に ":" は使えない
    filename = item_code.replace(":", "_") + ".json"
    return os.path.join(USED_DIR if used else DRAFTS_DIR, filename)


def load_draft(item_code: str) -> dict | None:
    """未使用の下書きを読み込む（なければNone）"""
    path = draft_path(item_code)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_ready(draft: dict | None) -> bool:
    return bool(draft and draft.get("post_text", "").strip())


def save_draft(draft: dict):
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    draft["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(draft_path(draft["item_code"]), "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)


def post_url(draft: dict) -> str:
    """tool.py に渡す商品ページURL"""
    if draft.get("item_url"):
        return draft["item_url"]
    pc = parse_qs(urlparse(draft.get("affiliate_url", "")).query).get("pc")
    return pc[0] if pc else draft.get("source_url", "")


def load_stock() -> list[dict]:
    """
    未使用の下書きを投稿順に並べて返す。
    予定のあるもの（日時順）→ 予定のないもの（作成順）。
    """
    stock = []
    for path in glob.glob(os.path.join(DRAFTS_DIR, "*.json")):
        with open(path, "r", encoding="utf-8") as f:
            stock.append(json.load(f))
    stock.sort(key=lambda d: (
        d.get("planned_date") or "9999-99-99",
        d.get("planned_time") or "99:99",
        d.get("created_at", ""),
    ))
    return stock


def _live_posts() -> list[dict]:
    """
    実際に投稿したログ（ドライランは除く）。
    scheduled=True は予約投稿の記録。予約キューは処理済みなので投稿済みとして扱う。
    """
    if not os.path.exists(POSTS_LOG):
        return []
    posts = []
    with open(POSTS_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not entry.get("dry_run"):
                posts.append(entry)
    return posts


def _posted_item_codes() -> set:
    return {p.get("item_code") for p in _live_posts()}


def posted_dates() -> Counter:
    """投稿日ごとの投稿数"""
    return Counter(p["timestamp"][:10] for p in _live_posts() if p.get("timestamp"))


def posts_today() -> int:
    return posted_dates()[date.today().isoformat()]


def mark_draft_used(item_code: str):
    """投稿に使った下書きを used/ に移す（同じ下書きの再利用を防ぐ）"""
    path = draft_path(item_code)
    if not os.path.exists(path):
        return
    os.makedirs(USED_DIR, exist_ok=True)
    os.replace(path, draft_path(item_code, used=True))


def load_sale_periods() -> list[dict]:
    """登録したセール期間（{"name", "start", "end"}、日付は YYYY-MM-DD、end は最後の丸1日）"""
    if not os.path.exists(SALE_PERIODS_FILE):
        return []
    with open(SALE_PERIODS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_sale_periods(periods: list[dict]):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    periods.sort(key=lambda p: p["start"])
    with open(SALE_PERIODS_FILE, "w", encoding="utf-8") as f:
        json.dump(periods, f, ensure_ascii=False, indent=2)


def sale_name(day: date | str | None, periods: list[dict] | None = None) -> str:
    """その日がセール・ポイントアップの日なら名前を返す（違えば空文字）"""
    if not day:
        return ""
    if isinstance(day, str):
        day = date.fromisoformat(day[:10])
    periods = load_sale_periods() if periods is None else periods
    key = day.isoformat()
    for period in periods:
        if period["start"] <= key <= period["end"]:
            return period["name"]
    return MONTHLY_SALE_DAYS.get(day.day, "")


def is_high_price(draft: dict) -> bool:
    return (draft.get("price") or 0) >= HIGH_PRICE


def _clean_caption(caption: str, limit: int = 1500) -> str:
    text = re.sub(r"<br\s*/?>", "\n", caption or "", flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _recent_post_bodies(limit: int = 10) -> list[str]:
    if not os.path.exists(POSTS_LOG):
        return []
    bodies = []
    with open(POSTS_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("post_text") and not entry.get("dry_run"):
                bodies.append(entry["post_text"].split("\n#")[0].strip())
    return bodies[-limit:]


def cmd_fetch(url: str):
    from rakuten_api import fetch_product_by_url, normalize_rakuten_url, affiliate_link_sns

    product = fetch_product_by_url(url)
    draft = load_draft(product["item_code"]) or {}
    item_url = normalize_rakuten_url(url).split("?")[0]

    # Threads用リンクで登録されたら、返信にはAPIのリンクではなくそれを使う（SNS別レポートに載せるため）
    link_sns = affiliate_link_sns(url)
    if link_sns == "Threads":
        draft["reply_link"] = url

    # 商品情報は最新に更新し、書きかけの文章と予定日は残す
    draft.update({
        "item_code": product["item_code"],
        "source_url": url,
        "item_url": item_url,
        "name": product["name"],
        "price": product["price"],
        "shop": product["shop"],
        "review_average": product["review_average"],
        "review_count": product["review_count"],
        "image_count": len(product.get("image_urls", [])),
        "affiliate_url": product["url"],
        "caption": _clean_caption(product.get("caption", "")),
    })
    draft.setdefault("post_text", "")
    draft.setdefault("reply_text", "")
    draft.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    save_draft(draft)

    print(json.dumps({
        "draft_file": os.path.abspath(draft_path(product["item_code"])),
        "already_posted": product["item_code"] in _posted_item_codes(),
        "link_sns": link_sns,
        "threads_link": bool(draft.get("reply_link")),
        "product": {k: draft[k] for k in (
            "item_code", "name", "price", "shop", "review_average",
            "review_count", "image_count", "caption",
        )},
        "recent_posts_to_avoid": _recent_post_bodies(),
    }, ensure_ascii=False, indent=2))


def cmd_check(item_code: str) -> bool:
    from quality_checker import lint_post, check_similarity

    draft = load_draft(item_code)
    if not is_ready(draft):
        print(f"❌ 投稿文が入った下書きがありません: {draft_path(item_code)}")
        return False

    problems = lint_post(draft["post_text"])

    reply = draft.get("reply_text", "")
    if not reply.strip():
        problems.append("返信文が空（tool.py側でレビュー件数の定型文になる）")
    elif "[LINK]" not in reply:
        problems.append("返信文に[LINK]がない（末尾にリンクが自動で付く）")
    if re.search(r"(^|\n)\s*pr\s*$", reply, re.IGNORECASE):
        problems.append("返信文のprは不要（tool.py側で自動で付く）")

    sim = check_similarity(draft["post_text"])

    print(f"商品: {draft['name'][:50]}")
    print(f"本文:\n{draft['post_text']}\n")
    print(f"返信:\n{reply}\n")
    print(f"類似度: {sim['max_similarity']}（0.5以上でNG）")
    if not sim["is_unique"]:
        problems.append(f"過去投稿と似すぎ: {sim['similar_to']}")

    if problems:
        print("⚠️ 要確認:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("✅ 問題なし")
    return not problems


def format_stock_line(draft: dict, today: str | None = None, periods: list[dict] | None = None) -> str:
    today = today or date.today().isoformat()
    planned = draft.get("planned_date")
    if not is_ready(draft):
        mark = "未作成"
    elif draft.get("queued_at"):
        mark = "予約"
    elif planned and planned <= today:
        mark = "今日" if planned == today else "遅れ"
    else:
        mark = "    "
    when = f"{planned} {draft.get('planned_time') or '     '}" if planned else "予定なし        "
    sale = "★" if sale_name(planned, periods) else " "
    first_line = draft.get("post_text", "").strip().split("。")[0][:24]
    return (f"[{mark}] {when}{sale} ¥{draft['price']:>6,}  "
            f"{first_line or draft['name'][:24]}  ({draft['item_code']})")


def print_stock_summary():
    stock = load_stock()
    ready = [d for d in stock if is_ready(d)]
    today = date.today().isoformat()
    queued = [d for d in ready if d.get("queued_at")]
    due = [d for d in ready if d.get("planned_date") and d["planned_date"] <= today and not d.get("queued_at")]
    print(f"   📦 ストック: 作成済み{len(ready)}件（うち予約済み{len(queued)}件）／未作成{len(stock) - len(ready)}件"
          f"　今日までの予定: {len(due)}件　今日の投稿: {posts_today()}/{MAX_POSTS_PER_DAY}件")


def cmd_list():
    stock = load_stock()
    if not stock:
        print("ストックはありません")
        return
    print_stock_summary()
    print()
    periods = load_sale_periods()
    for d in stock:
        print(format_stock_line(d, periods=periods))
    print(f"\n★＝セール・ポイントアップの日（¥{HIGH_PRICE:,}以上の商品を寄せる。日程は py drafts.py sale）")

    unplanned = [d for d in stock if is_ready(d) and not d.get("planned_date")]
    if unplanned:
        print(f"\n予定日のない下書きが{len(unplanned)}件あります → py drafts.py plan")


def cmd_plan(per_day: int = MAX_POSTS_PER_DAY, reset: bool = False):
    """
    予定のない作成済み下書きに、空いている投稿枠（日付＋時間）を順に割り当てる。
    reset=True なら、手動で決めた予定（date_fixed）と予約済み（queued_at）以外をいったん外してから割り当てる。
    手動で決めた予定でも、日付が過ぎたものは外す。

    単価の高い商品（HIGH_PRICE以上）はセール・ポイントアップの日の枠に、高い順に入れる。
    普通の日は安い商品を作成順に入れ、安い商品がなくなったら、残った高い商品を安い順に入れる（空き日を作らない）。
    """
    if not 1 <= per_day <= MAX_POSTS_PER_DAY:
        print(f"1日の件数は1〜{MAX_POSTS_PER_DAY}で指定してください")
        return

    stock = load_stock()
    if reset:
        today_key = datetime.now().date().isoformat()
        for draft in stock:
            # 予約済みを外すと、GitHubのキューと下書きの予定がずれる
            if not draft.get("planned_date") or draft.get("queued_at"):
                continue
            # 手動で決めた予定は動かさない。ただし日付が過ぎたもの（投稿できなかった）は外して組み直す
            if draft.get("date_fixed") and draft["planned_date"] >= today_key:
                continue
            draft.pop("planned_date")
            draft.pop("planned_time", None)
            draft.pop("date_fixed", None)
            save_draft(draft)

    booked = defaultdict(list)  # 日付 → 予約済みの時間（時間なしの予定も1件と数える）
    for draft in stock:
        if draft.get("planned_date"):
            booked[draft["planned_date"]].append(draft.get("planned_time"))
    posted = posted_dates()
    slots = POST_SLOTS[:per_day]
    now = datetime.now()

    def free_slot(day: date) -> str | None:
        key = day.isoformat()
        if len(booked[key]) + posted[key] >= per_day:
            return None
        for slot in slots:
            if slot in booked[key]:
                continue
            if day == now.date() and slot <= now.strftime("%H:%M"):
                continue  # 今日のすでに過ぎた枠
            return slot
        return None

    pending = [d for d in stock if is_ready(d) and not d.get("planned_date")]
    high = sorted((d for d in pending if is_high_price(d)), key=lambda d: -(d.get("price") or 0))
    normal = [d for d in pending if not is_high_price(d)]
    periods = load_sale_periods()

    day = now.date()
    while high or normal:
        slot = free_slot(day)
        if slot is None:
            day += timedelta(days=1)
            continue
        sale = sale_name(day, periods)
        if sale and high:
            draft = high.pop(0)   # セールの日：残っているうちでいちばん高い商品
        elif normal:
            draft = normal.pop(0)
        else:
            draft = high.pop()    # 安い商品が尽きた普通の日：残っているうちでいちばん安い商品
        draft["planned_date"] = day.isoformat()
        draft["planned_time"] = slot
        booked[day.isoformat()].append(slot)
        save_draft(draft)
        label = f"★{sale}" if sale else ""
        print(f"{day.isoformat()} {slot}  ¥{draft['price']:>6,}  {short_name(draft, 20)}  {label}")

    print()
    cmd_list()


def cmd_sale(args: list[str]):
    """セール期間の一覧・登録・削除"""
    periods = load_sale_periods()
    action = args[0] if args else "list"

    if action == "add" and len(args) >= 4:
        try:
            start = date.fromisoformat(args[1]).isoformat()
            end = date.fromisoformat(args[2]).isoformat()
        except ValueError:
            print("日付は YYYY-MM-DD で指定してください")
            return
        if end < start:
            print("終了日が開始日より前です")
            return
        name = " ".join(args[3:])
        periods = [p for p in periods if p["start"] != start]
        periods.append({"name": name, "start": start, "end": end})
        save_sale_periods(periods)
        print(f"✅ 登録しました: {start}〜{end} {name}")
        print("   → 予定に反映するなら py drafts.py replan")
    elif action == "del" and len(args) >= 2:
        kept = [p for p in periods if p["start"] != args[1]]
        if len(kept) == len(periods):
            print(f"⚠️ {args[1]} に始まる期間は登録されていません")
            return
        save_sale_periods(kept)
        print(f"✅ 削除しました: {args[1]}〜")
        print("   → 予定に反映するなら py drafts.py replan")
    elif action != "list":
        print("py drafts.py sale add <開始日> <終了日> <名前> ／ sale del <開始日> ／ sale")
        return

    today = date.today()
    upcoming = [p for p in load_sale_periods() if p["end"] >= today.isoformat()]
    print("\n登録したセール期間（お買い物マラソン・スーパーSALEなど）:")
    for p in upcoming:
        print(f"  {p['start']}〜{p['end']}  {p['name']}")
    if not upcoming:
        print("  （なし。日程が発表されたら py drafts.py sale add <開始日> <終了日> <名前>）")
    print(f"\nこれから30日のセール・ポイントアップの日（¥{HIGH_PRICE:,}以上の商品を寄せる）:")
    periods = load_sale_periods()
    for offset in range(30):
        day = today + timedelta(days=offset)
        if name := sale_name(day, periods):
            print(f"  {day.isoformat()}（{'月火水木金土日'[day.weekday()]}） {name}")


def short_name(draft: dict, limit: int = 24) -> str:
    """
    通知や一覧用の短い商品名。下書きの label（Claude Codeが付ける）を優先し、
    なければ商品名からクーポン等の宣伝部分を除いて短くする。
    """
    if draft.get("label"):
        return draft["label"]
    name = draft.get("name", "")
    for pattern in (r"【[^】]*】", r"＼[^／]*／", r"★[^★]*★", r"\[[^\]]*\]"):
        name = re.sub(pattern, " ", name)
    name = re.sub(r"\s+", " ", re.sub(r"[「」]", "", name)).strip()
    return name[:limit]


def next_reminder(now: datetime | None = None) -> str:
    """次の投稿枠のリマインダー文（リマインダー用の予定タスクから使う）"""
    now = now or datetime.now()
    today = now.date().isoformat()
    ready = [d for d in load_stock() if is_ready(d) and d.get("planned_date")]
    slot = next((s for s in POST_SLOTS if s >= now.strftime("%H:%M")), None)

    def is_late(d: dict) -> bool:
        if d["planned_date"] < today:
            return True
        return d["planned_date"] == today and (slot is None or (d.get("planned_time") or "") < slot)

    late = [d for d in ready if is_late(d)]
    targets = [d for d in ready if slot and d["planned_date"] == today and d.get("planned_time") == slot]
    upcoming = [d for d in ready if d not in late and d not in targets]

    if targets and targets[0].get("queued_at"):
        message = f"{slot}の投稿：{short_name(targets[0])}（予約済み・GitHubが自動で投稿）"
    elif targets:
        message = f"{slot}の投稿：{short_name(targets[0])}（py tool.py → 3）"
    elif slot:
        message = f"{slot}の予定はありません"
    else:
        message = "今日の投稿枠は終わりました"
    if late:
        message += f"／遅れている投稿{len(late)}件"
    if not targets and upcoming:
        nxt = upcoming[0]
        message += f"／次は{nxt['planned_date'][5:].replace('-', '/')} {nxt.get('planned_time', '')} {short_name(nxt, 16)}"

    # 1日2件なので、2日分を切ったら補充を促す
    remaining = len(upcoming)
    if remaining == 0 and not targets:
        message += "／ストックが空です。Claude Codeに商品URLを送って追加してください"
    elif remaining < MAX_POSTS_PER_DAY * 2:
        message += f"／ストック残り{remaining}件"
    return message


def cmd_link(item_code: str, url: str) -> bool:
    """あとからThreads用リンクを下書きに付ける（商品が一致しているか確認する）"""
    from rakuten_api import affiliate_link_sns, normalize_rakuten_url

    draft = load_draft(item_code)
    if not draft:
        print(f"❌ 下書きがありません: {item_code}")
        return False

    sns = affiliate_link_sns(url)
    if sns != "Threads":
        print(f"❌ Threads用のリンクではありません（判定: {sns or 'SNSボタンのリンクではない'}）")
        return False

    link_item = normalize_rakuten_url(url).split("?")[0].rstrip("/")
    draft_item = post_url(draft).split("?")[0].rstrip("/")
    if link_item != draft_item:
        print(f"❌ 別の商品のリンクです: リンク={link_item} / 下書き={draft_item}")
        return False

    draft["reply_link"] = url
    save_draft(draft)
    print(f"✅ Threads用リンクを付けました: {short_name(draft)} → {url}")
    return True


CONTENT_DRAFTS_FILE = os.path.join(OUTPUT_DIR, "content_drafts.json")
CONTENT_LOG = os.path.join(OUTPUT_DIR, "content_log.jsonl")


def cmd_content_check() -> bool:
    """リンクなし投稿の下書き（content_drafts.json）を一覧して、ルールと類似度を確認する"""
    from quality_checker import lint_post, check_similarity

    if not os.path.exists(CONTENT_DRAFTS_FILE):
        print("リンクなし投稿の下書きはありません")
        return True
    with open(CONTENT_DRAFTS_FILE, "r", encoding="utf-8") as f:
        content_drafts = json.load(f)

    past_content = []
    if os.path.exists(CONTENT_LOG):
        with open(CONTENT_LOG, "r", encoding="utf-8") as f:
            past_content = [json.loads(line).get("post_text", "") for line in f if line.strip()]

    all_ok = True
    for i, draft in enumerate(content_drafts, 1):
        text = draft.get("post_text", "")
        others = [d.get("post_text", "") for j, d in enumerate(content_drafts, 1) if j != i]
        problems = lint_post(text, max_length=150)
        if not draft.get("image_keywords"):
            problems.append("image_keywords がない（tool.py で画像検索のキーワードをAPIで作ることになる）")
        sim = check_similarity(text, extra_texts=past_content + others)
        if not sim["is_unique"]:
            problems.append(f"過去の投稿や他の下書きと似すぎ: {sim['similar_to']}")

        source = "Claude Code" if draft.get("source") == "claude_code" else "保存済み"
        print(f"{i}. [{source}] topic={draft.get('topic')} image_keywords={draft.get('image_keywords')}")
        print(f"{text}\n")
        if problems:
            all_ok = False
            for p in problems:
                print(f"  ⚠️ {p}")
        else:
            print(f"  ✅ 問題なし（類似度 {sim['max_similarity']}）")
        print()
    return all_ok


def cmd_date(item_code: str, value: str, time_value: str | None = None):
    draft = load_draft(item_code)
    if not draft:
        print(f"❌ 下書きがありません: {item_code}")
        return
    if value.lower() == "none":
        for key in ("planned_date", "planned_time", "date_fixed"):
            draft.pop(key, None)
    else:
        try:
            draft["planned_date"] = datetime.strptime(value, "%Y-%m-%d").date().isoformat()
            time_value = time_value or draft.get("planned_time") or POST_SLOTS[0]
            draft["planned_time"] = datetime.strptime(time_value, "%H:%M").strftime("%H:%M")
        except ValueError:
            print("日付は YYYY-MM-DD、時間は HH:MM で指定してください")
            return
        draft["date_fixed"] = True
    save_draft(draft)
    cmd_list()


def main():
    # tool.py から import されたときは触らない（二重に包むと元のstdoutが閉じられる）
    if platform.system() == "Windows":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]
    command = args[0] if args else ""

    if command == "list":
        cmd_list()
        return
    elif command == "next":
        print(next_reminder())
        return
    elif command == "content":
        sys.exit(0 if cmd_content_check() else 2)
    elif command in ("plan", "replan"):
        per_day = int(args[1]) if len(args) > 1 and args[1].isdigit() else MAX_POSTS_PER_DAY
        cmd_plan(per_day, reset=command == "replan")
    elif command == "fetch" and len(args) >= 2:
        cmd_fetch(args[1])
    elif command == "check" and len(args) >= 2:
        sys.exit(0 if cmd_check(args[1]) else 2)
    elif command == "link" and len(args) >= 3:
        if not cmd_link(args[1], args[2]):
            sys.exit(2)
    elif command == "date" and len(args) >= 3:
        cmd_date(args[1], args[2], args[3] if len(args) > 3 else None)
    elif command == "sale":
        cmd_sale(args[1:])
        return
    else:
        print(__doc__)
        sys.exit(1)

    # データを変えるコマンドのあとだけ管理表を作り直す（check は失敗時に exit する）
    from sheet import export_sheet_quietly
    export_sheet_quietly()


if __name__ == "__main__":
    main()
