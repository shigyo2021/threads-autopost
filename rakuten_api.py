"""楽天商品検索API + アフィリエイトリンク生成"""

import html
import json
import re
import random
import time
import requests
from urllib.parse import parse_qs, unquote, urlparse
from config import RAKUTEN_APP_ID, RAKUTEN_ACCESS_KEY, RAKUTEN_AFFILIATE_ID, RAKUTEN_GENRES

# 末尾の日付はAPIバージョン。古い日付は "API Configuration not found" の400になる
SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"

# 商品名に含まれていたら除外するキーワード（宣伝的・オーダー品・セット売り等）
EXCLUDE_KEYWORDS = [
    "SALE", "sale", "セール", "クーポン", "ポイント", "倍",
    "オーダー", "サイズオーダー", "受注生産",
    "訳あり", "アウトレット", "B品",
    "まとめ買い", "業務用", "法人",
    "サンプル", "生地サンプル",
    "レビュー特典", "レビューで",
    "予約販売",
]


def search_products(
    category: str | None = None,
    count: int = 5,
    check_images: bool = False,
) -> list[dict]:
    """
    楽天APIで商品を検索し、アフィリエイトリンク付きで返す。

    Args:
        category: 商品カテゴリ（None=ランダム）
        count: 必要な商品数
        check_images: True=画像品質チェック付き（遅い）
    """

    if category and category in RAKUTEN_GENRES:
        genre = RAKUTEN_GENRES[category]
    else:
        category = random.choice(list(RAKUTEN_GENRES.keys()))
        genre = RAKUTEN_GENRES[category]

    # 多めに取得して、フィルタリング後にcount件確保する
    fetch_count = min(count * 5, 30)

    params = {
        "format": "json",
        "applicationId": RAKUTEN_APP_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "affiliateId": RAKUTEN_AFFILIATE_ID,
        "keyword": genre["keyword"],
        "genreId": genre["genreId"],
        "hits": fetch_count,
        "sort": "-reviewAverage",
        "minPrice": 5000,
        "imageFlag": 1,
        "page": random.randint(1, 3),  # ページをランダム化して商品を多様化
    }

    resp = requests.get(SEARCH_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    products = []
    for item_wrapper in data.get("Items", []):
        item = item_wrapper["Item"]
        name = item["itemName"]

        # --- 商品名フィルタ: 宣伝的な商品を除外 ---
        if _should_exclude(name):
            continue

        # 商品画像URL（全画像を取得）
        medium_urls = item.get("mediumImageUrls", [])

        # 大きい画像に差し替え（楽天の画像URLパターン）
        all_image_urls = []
        for img in medium_urls:
            url = img.get("imageUrl", "")
            if url:
                all_image_urls.append(url.replace("?_ex=128x128", "?_ex=500x500"))

        # 画像がない商品はスキップ
        if not all_image_urls:
            continue

        # 後方互換: 1枚目をimage_urlとして残す
        image_url = all_image_urls[0] if all_image_urls else None

        product = {
            "name": name,
            "price": item["itemPrice"],
            "url": item.get("affiliateUrl", item["itemUrl"]),  # アフィリエイトURL優先
            "image_url": image_url,
            "image_urls": all_image_urls,  # 全画像URLリスト
            "shop": item["shopName"],
            "review_average": item.get("reviewAverage", 0),
            "review_count": item.get("reviewCount", 0),
            "category": category,
            "item_code": item["itemCode"],
        }

        # 画像品質チェック（有効時のみ、APIコール増えるので注意）
        if check_images:
            from image_processor import check_product_has_good_images
            if not check_product_has_good_images(product):
                print(f"   ⚠️ 画像品質NG、スキップ: {name[:30]}")
                continue

        products.append(product)

    return products[:count]


def _should_exclude(name: str) -> bool:
    """商品名に除外キーワードが含まれているかチェック"""
    for kw in EXCLUDE_KEYWORDS:
        if kw in name:
            return True
    return False


# 楽天アフィリエイトの「SNSボタン」で作ったリンクは、転送先の rafmid でSNSが分かる（2026-09-15 実測）
SNS_BY_RAFMID = {"0103": "Threads", "0101": "X", "0201": "YouTube", "0002": "note", "0106": "Pinterest"}


def affiliate_link_sns(url: str) -> str | None:
    """アフィリエイトリンクがどのSNS用に作られたか（SNSボタンのリンクでなければ None）"""
    target = url.strip()
    if "r10.to" in target:
        try:
            target = requests.get(target, allow_redirects=False, timeout=15).headers.get("Location", "")
        except requests.RequestException:
            return None
    if "hb.afl.rakuten.co.jp" not in target:
        return None
    rafmid = parse_qs(urlparse(target).query).get("rafmid", [None])[0]
    return SNS_BY_RAFMID.get(rafmid)


def normalize_rakuten_url(url: str) -> str:
    """
    アフィリエイトリンク（hb.afl.rakuten.co.jp）や短縮URL（r10.to）を
    商品ページURL（item.rakuten.co.jp）に戻す。
    """
    url = url.strip()

    if "r10.to" in url:
        try:
            url = requests.get(url, timeout=15, allow_redirects=True).url
        except requests.RequestException:
            return url

    if "hb.afl.rakuten.co.jp" in url:
        query = parse_qs(urlparse(url).query)
        if query.get("pc"):
            url = query["pc"][0]  # parse_qs がデコード済み

    return url


def _build_keyword(text: str, max_length: int = 40) -> str:
    """
    検索キーワードを作る。楽天APIは1文字の単語を含むと400を返すので除き、
    長すぎるとヒットしないので単語単位で max_length 文字までにする。
    """
    words = []
    for word in text.split():
        if len(word) < 2:
            continue
        if len(" ".join(words + [word])) > max_length:
            break
        words.append(word)
    return " ".join(words)


def _is_same_item(item: dict, shop_url_name: str, item_path: str) -> bool:
    """APIの商品が、URLの商品ページと同じものか"""
    # affiliateId を渡すと itemUrl はアフィリエイトリンクになり、商品URLはエンコードされて入る
    item_url = unquote(item.get("itemUrl", ""))
    return f"/{shop_url_name}/{item_path}/" in item_url or item_url.rstrip("/").endswith(f"/{shop_url_name}/{item_path}")


def fetch_product_by_url(rakuten_url: str) -> dict:
    """
    楽天商品URLから商品情報を取得する（URL指定モード用）。

    対応URL形式:
      - https://item.rakuten.co.jp/{shopname}/{itemcode}/
      - https://item.rakuten.co.jp/{shopname}/{itemcode}

    処理フロー:
      1. 商品ページHTMLを取得してタイトルを抽出
      2. タイトル + shopCode で楽天APIを検索
      3. 最も一致する商品を返す

    Args:
        rakuten_url: 楽天市場の商品ページURL

    Returns:
        商品情報dict（search_productsと同じ形式）
    """
    rakuten_url = normalize_rakuten_url(rakuten_url)

    # URLからショップ名と商品パスを抽出
    match = re.search(r"item\.rakuten\.co\.jp/([^/]+)/([^/?#]+)", rakuten_url)
    if not match:
        raise ValueError(f"楽天商品URLの形式が不正です: {rakuten_url}")

    shop_url_name = match.group(1)
    item_path = match.group(2)

    # shopCode: まずURLそのまま、ダメならハイフン除去で試す
    shop_code = shop_url_name

    # Step 1: 商品ページからタイトルを取得
    page_title = _fetch_page_title(rakuten_url)

    # Step 2: タイトルからキーワードを抽出してAPI検索
    keyword = None
    if page_title:
        # HTMLエンティティをデコード（&times; → × 等）
        keyword = html.unescape(page_title)
        # タイトルから不要部分を除去（【楽天市場】、ショップ名等）
        keyword = re.sub(r"【[^】]*】", "", keyword)      # 【】内を除去
        keyword = re.sub(r"\|.*$", "", keyword)            # | 以降を除去
        keyword = re.sub(r"[:：].*$", "", keyword)         # : 以降を除去（ショップ名）
        # サイズ表記等を除去（検索ノイズになる）
        keyword = re.sub(r"（[^）]*）", "", keyword)       # （）内を除去
        keyword = re.sub(r"\([^)]*\)", "", keyword)        # ()内を除去
        keyword = re.sub(r"約[０-９0-9×x\.\s]+[ａ-ｚa-zＡ-Ｚ]*", "", keyword)  # 約36×26×31cm等
        keyword = _build_keyword(keyword)

    if not keyword:
        # ページ取得失敗時はURLパスからキーワード生成
        keyword = _build_keyword(re.sub(r"[-_]", " ", item_path))

    # Step 3: キーワードを段階的に短くし、shopCodeのハイフン有無も試して、URLと一致する商品を探す
    words = keyword.split()
    keywords = list(dict.fromkeys([keyword, " ".join(words[:3]), " ".join(words[:1]), None]))
    shop_codes = list(dict.fromkeys([shop_url_name, shop_url_name.replace("-", "")]))

    best_item = None
    attempts = 0
    for shop_code in shop_codes:
        for kw in keywords:
            if attempts:
                time.sleep(1.1)  # レート制限: 1リクエスト/秒
            attempts += 1
            for item_wrapper in _search_in_shop(shop_code, kw or None):
                if _is_same_item(item_wrapper["Item"], shop_url_name, item_path):
                    best_item = item_wrapper["Item"]
                    break
            if best_item:
                break
        if best_item:
            break

    # 一致しない商品を使うと、別商品のアフィリエイトリンクを投稿してしまうので中止する
    if best_item is None:
        raise ValueError(
            f"URLの商品を楽天APIで特定できませんでした: {shop_url_name}/{item_path}\n"
            "ヒント: 商品ページが存在するか、売り切れ・非公開になっていないか確認してください。"
        )

    # 画像URL取得: まずページスクレイピングで全画像を取得（リトライ付き）
    all_image_urls = _scrape_product_images(rakuten_url, item_path)

    if not all_image_urls:
        print("   ⚠️ 画像スクレイピング1回目失敗、リトライ中...")
        time.sleep(2)
        all_image_urls = _scrape_product_images(rakuten_url, item_path)

    # スクレイピング失敗時はAPI画像にフォールバック
    if not all_image_urls:
        print("   ⚠️ スクレイピング失敗、API画像にフォールバック")
        medium_urls = best_item.get("mediumImageUrls", [])
        for img in medium_urls:
            url = img.get("imageUrl", "")
            if url:
                all_image_urls.append(url.replace("?_ex=128x128", "?_ex=500x500"))

    image_url = all_image_urls[0] if all_image_urls else None

    return {
        "name": best_item["itemName"],
        "price": best_item["itemPrice"],
        "url": best_item.get("affiliateUrl", best_item["itemUrl"]),
        "image_url": image_url,
        "image_urls": all_image_urls,
        "shop": best_item["shopName"],
        "review_average": best_item.get("reviewAverage", 0),
        "review_count": best_item.get("reviewCount", 0),
        "category": "手動選定",
        "item_code": best_item["itemCode"],
        "caption": best_item.get("itemCaption", ""),
    }


def _scrape_product_images(page_url: str, item_path: str) -> list[str]:
    """
    商品ページHTMLから全商品画像URLを取得する。

    楽天APIは最大3枚しか画像を返さないが、
    商品ページには5〜15枚の画像があることが多い。

    戦略:
      0. ページに埋め込まれた商品データ（item-page-app-data）のギャラリー画像を使う（表示順どおり・正確）
      1. 0が使えない場合、item_pathを含む画像URLを探す（従来方式）
      2. 見つからない場合、image.rakuten.co.jp/{shop}/cabinet/ の画像を収集し、
         共通プレフィックスで最大グループを商品画像と判定する
         （同じショップの別商品の画像が混ざることがある）

    Returns:
        画像URLリスト（重複除去済み）
    """
    # URLからショップ名を抽出
    shop_match = re.search(r"item\.rakuten\.co\.jp/([^/]+)/", page_url)
    shop_name = shop_match.group(1) if shop_match else ""

    try:
        resp = requests.get(
            page_url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        if resp.status_code != 200:
            return []

        # --- 方式0: 埋め込みの商品データ ---
        gallery = _gallery_images_from_page_data(resp.text)
        if gallery:
            return gallery

        all_urls = re.findall(r'https?://[^\"\' >]+', resp.text)
        img_exts = ('.jpg', '.jpeg', '.png', '.webp')

        # --- 方式1: item_pathを含む画像（従来方式） ---
        item_imgs = []
        seen = set()

        for url in all_urls:
            if item_path not in url:
                continue
            clean = url.split("?")[0].lower()
            if not any(clean.endswith(ext) for ext in img_exts):
                continue
            base_url = url.split("?")[0]
            filename = base_url.split("/")[-1]
            if base_url in seen or filename in seen:
                continue
            # レビュースタンプ・ランキングバッジ等を除外
            if any(skip in base_url for skip in ("revclip", "rankstamp", "banner", "/bn/")):
                continue
            seen.add(base_url)
            seen.add(filename)
            item_imgs.append(base_url)

        if len(item_imgs) >= 3:
            item_imgs.sort(key=_natural_sort_key)
            return item_imgs

        # --- 方式2: ショップのcabinet画像から共通プレフィックスで判定 ---
        cabinet_imgs = []
        seen2 = set()
        # 除外パターン（バナー、ナビ、アイコン等）
        skip_patterns = ("banner", "/bn/", "nav", "icon", "logo", "revclip",
                         "rankstamp", "gold/", "contents/", "shopranking")

        for url in all_urls:
            # image.rakuten.co.jp/{shop}/cabinet/ の画像のみ
            if f"/{shop_name}/cabinet/" not in url:
                continue
            clean = url.split("?")[0].lower()
            if not any(clean.endswith(ext) for ext in img_exts):
                continue
            base_url = url.split("?")[0]
            if any(skip in base_url.lower() for skip in skip_patterns):
                continue
            filename = base_url.split("/")[-1]
            if filename in seen2:
                continue
            seen2.add(filename)
            cabinet_imgs.append(base_url)

        if not cabinet_imgs:
            item_imgs.sort(key=_natural_sort_key)
            return item_imgs

        # ファイル名のプレフィックス（数字・記号を除いた共通部分）でグループ化
        prefix_map = {}
        for url in cabinet_imgs:
            fname = url.split("/")[-1].split(".")[0]  # 拡張子除去
            # アンダースコアやハイフンで区切った先頭部分をプレフィックスとする
            prefix = re.split(r"[_\-]?\d+[a-z]?$", fname)[0]
            if not prefix:
                prefix = fname
            prefix_map.setdefault(prefix, []).append(url)

        # 最大グループを商品画像とみなす
        best_group = max(prefix_map.values(), key=len)

        # 最大グループが2枚以下なら、全cabinet画像を返す（単一商品ページの場合）
        if len(best_group) <= 2 and len(cabinet_imgs) > len(best_group):
            result = cabinet_imgs
        else:
            result = best_group

        result.sort(key=_natural_sort_key)
        return result

    except Exception as e:
        print(f"   [DEBUG] スクレイピングエラー: {e}")
        return []


def _gallery_images_from_page_data(page_html: str) -> list[str]:
    """
    商品ページの <script id="item-page-app-data"> に入っている、ギャラリー画像のURLを表示順で返す。
    （2026-09時点の構造: ...itemInfoSku.media.images[].location）
    """
    match = re.search(r'<script[^>]*id="item-page-app-data"[^>]*>(.*?)</script>', page_html, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    for root in (data.get("api", {}).get("data", {}), data.get("newApi", {})):
        images = root.get("itemInfoSku", {}).get("media", {}).get("images", [])
        urls = []
        for image in images:
            location = image.get("location", "") if isinstance(image, dict) else ""
            if location.startswith("http") and location not in urls:
                urls.append(location)
        if urls:
            return urls
    return []


def _natural_sort_key(url: str):
    """自然数ソート用キー（_1, _2, ..., _10 が正しい順になる）"""
    filename = url.split("/")[-1]
    parts = re.split(r"(\d+)", filename)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def _fetch_page_title(url: str) -> str | None:
    """楽天商品ページのHTMLからタイトルを取得"""
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        if resp.status_code != 200:
            return None
        # titleタグを抽出
        match = re.search(r"<title[^>]*>([^<]+)</title>", resp.text)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return None


def _search_in_shop(shop_code: str, keyword: str | None) -> list:
    """ショップ内でキーワード検索"""
    params = {
        "format": "json",
        "applicationId": RAKUTEN_APP_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "affiliateId": RAKUTEN_AFFILIATE_ID,
        "shopCode": shop_code,
        "hits": 30,
    }
    if keyword:
        params["keyword"] = keyword

    resp = requests.get(SEARCH_URL, params=params, timeout=15)
    if resp.status_code != 200:
        return []

    return resp.json().get("Items", [])


def download_product_image(image_url: str, save_path: str) -> str:
    """商品画像をダウンロード"""
    resp = requests.get(image_url, timeout=15)
    resp.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(resp.content)
    return save_path


if __name__ == "__main__":
    # テスト実行
    products = search_products("ソファ", count=3)
    for p in products:
        print(f"  {p['name'][:40]}  ¥{p['price']:,}  ★{p['review_average']}")
        print(f"  画像数: {len(p['image_urls'])}枚")
        print(f"  {p['url'][:80]}")
        print()
