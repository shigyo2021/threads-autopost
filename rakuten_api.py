"""楽天商品検索API + アフィリエイトリンク生成"""

import random
import requests
from config import RAKUTEN_APP_ID, RAKUTEN_ACCESS_KEY, RAKUTEN_AFFILIATE_ID, RAKUTEN_GENRES

SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"


def search_products(category: str | None = None, count: int = 5) -> list[dict]:
    """楽天APIで商品を検索し、アフィリエイトリンク付きで返す"""

    if category and category in RAKUTEN_GENRES:
        genre = RAKUTEN_GENRES[category]
    else:
        category = random.choice(list(RAKUTEN_GENRES.keys()))
        genre = RAKUTEN_GENRES[category]

    params = {
        "format": "json",
        "applicationId": RAKUTEN_APP_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "affiliateId": RAKUTEN_AFFILIATE_ID,
        "keyword": genre["keyword"],
        "genreId": genre["genreId"],
        "hits": count,
        "sort": "-reviewAverage",
        "minPrice": 5000,
        "imageFlag": 1,
        "page": 1,
    }

    resp = requests.get(SEARCH_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    products = []
    for item_wrapper in data.get("Items", []):
        item = item_wrapper["Item"]

        # 商品画像URL（複数ある場合は最初の1枚）
        image_urls = item.get("mediumImageUrls", [])
        image_url = image_urls[0]["imageUrl"] if image_urls else None

        # 大きい画像に差し替え（楽天の画像URLパターン）
        if image_url:
            image_url = image_url.replace("?_ex=128x128", "?_ex=400x400")

        products.append({
            "name": item["itemName"],
            "price": item["itemPrice"],
            "url": item.get("affiliateUrl", item["itemUrl"]),  # アフィリエイトURL優先
            "image_url": image_url,
            "shop": item["shopName"],
            "review_average": item.get("reviewAverage", 0),
            "review_count": item.get("reviewCount", 0),
            "category": category,
            "item_code": item["itemCode"],
        })

    return products


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
        print(f"  {p['url'][:80]}")
        print()
