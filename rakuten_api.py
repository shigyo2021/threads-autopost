"""楽天商品検索API + アフィリエイトリンク生成"""

import re
import random
import requests
from config import RAKUTEN_APP_ID, RAKUTEN_ACCESS_KEY, RAKUTEN_AFFILIATE_ID, RAKUTEN_GENRES

SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"

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
        # タイトルから不要部分を除去（【楽天市場】、ショップ名等）
        keyword = re.sub(r"【[^】]*】", "", page_title)  # 【】内を除去
        keyword = re.sub(r"\|.*$", "", keyword)            # | 以降を除去
        keyword = re.sub(r"[:：].*$", "", keyword)         # : 以降を除去（ショップ名）
        keyword = keyword.strip()[:50]  # 長すぎるとヒットしないので制限

    if not keyword:
        # ページ取得失敗時はURLパスからキーワード生成
        keyword = re.sub(r"[-_]", " ", item_path).strip()

    items = _search_in_shop(shop_code, keyword)

    # Step 3: shopCodeのハイフン有無を切り替えて再試行
    if not items:
        alt_shop_code = shop_url_name.replace("-", "") if "-" in shop_url_name else shop_url_name
        if alt_shop_code != shop_code:
            items = _search_in_shop(alt_shop_code, keyword)
            if items:
                shop_code = alt_shop_code

    # Step 4: キーワードを短くして再検索
    if not items and keyword:
        short_keyword = " ".join(keyword.split()[:3])
        items = _search_in_shop(shop_code, short_keyword)

    # Step 5: それでもダメならshopCode全商品から探す
    if not items:
        items = _search_in_shop(shop_code, None)

    if not items:
        raise ValueError(f"商品が見つかりません: {shop_code}/{item_path}")

    # 最も一致する商品を選択
    best_item = items[0]["Item"]
    for item_wrapper in items:
        item = item_wrapper["Item"]
        # itemCodeやitemUrlにitem_pathが含まれていれば完全一致
        item_url = item.get("itemUrl", "")
        if item_path in item.get("itemCode", "") or item_path in item_url:
            best_item = item
            break

    # 画像URL取得: まずページスクレイピングで全画像を取得
    all_image_urls = _scrape_product_images(rakuten_url, item_path)

    # スクレイピング失敗時はAPI画像にフォールバック
    if not all_image_urls:
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
    }


def _scrape_product_images(page_url: str, item_path: str) -> list[str]:
    """
    商品ページHTMLから全画像URLを取得する。

    楽天APIは最大3枚しか画像を返さないが、
    商品ページには5〜15枚の画像があることが多い。
    商品コード/パスを含む画像URLをスクレイピングで取得する。

    Returns:
        画像URLリスト（重複除去・ソート済み）
    """
    try:
        resp = requests.get(
            page_url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        if resp.status_code != 200:
            return []

        # 商品コード/パスを含む画像URLを抽出
        all_urls = re.findall(r'https?://[^\"\' >]+', resp.text)
        img_exts = ('.jpg', '.jpeg', '.png', '.webp')
        item_imgs = []
        seen = set()

        for url in all_urls:
            # 商品コードを含む画像のみ
            if item_path not in url:
                continue
            # 画像拡張子チェック
            clean = url.split("?")[0].lower()
            if not any(clean.endswith(ext) for ext in img_exts):
                continue
            # クエリパラメータ除去して重複チェック
            base_url = url.split("?")[0]
            filename = base_url.split("/")[-1]
            if base_url in seen or filename in seen:
                continue
            seen.add(base_url)
            seen.add(filename)
            item_imgs.append(base_url)

        # ファイル名でソート（_01, _02, ... の順になる）
        item_imgs.sort(key=lambda u: u.split("/")[-1])

        return item_imgs
    except Exception:
        return []


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
        "hits": 10,
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
