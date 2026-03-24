"""商品画像処理: ダウンロード・品質チェック・トリミング・テキスト検出"""

import io
import os
import requests
import numpy as np
from PIL import Image, ImageFilter
from config import IMAGES_DIR


def process_product_images(
    product: dict,
    max_images: int = 3,
    crop_margin_pct: float = 3.0,
    selected_indices: list[int] | None = None,
) -> list[str]:
    """
    商品画像をダウンロード・品質チェック・処理して保存パスのリストを返す。

    テキスト入りバナー画像や壊れた画像は自動的にスキップする。

    Args:
        product: 楽天商品情報 dict（image_urlsを含む）
        max_images: 使用する最大画像数
        crop_margin_pct: 端からトリミングする割合（%）ロゴ除去用
        selected_indices: 使用する画像の番号リスト（1始まり）。
                         指定時は品質チェックをスキップし、指定画像のみ処理。

    Returns:
        処理済み画像のファイルパスリスト
    """
    image_urls = product.get("image_urls", [])
    if not image_urls and product.get("image_url"):
        image_urls = [product["image_url"]]

    if not image_urls:
        raise ValueError("商品画像が見つかりません")

    # 番号指定がある場合、対象画像のみに絞る
    if selected_indices:
        filtered = []
        for idx in selected_indices:
            if 1 <= idx <= len(image_urls):
                filtered.append(image_urls[idx - 1])
            else:
                print(f"      ⚠️ 画像{idx}は存在しません（全{len(image_urls)}枚）")
        if not filtered:
            raise ValueError("指定された画像番号が無効です")
        image_urls = filtered
        max_images = len(filtered)  # 指定枚数分すべて使う

    os.makedirs(IMAGES_DIR, exist_ok=True)
    safe_name = product["item_code"].replace(":", "_")

    processed_paths = []
    for i, url in enumerate(image_urls):
        # 既に十分な枚数を確保したら終了
        if len(processed_paths) >= max_images:
            break

        try:
            # _exパラメータなしの元画像URLを試す（より高解像度）
            clean_url = url.split("?")[0]
            img = _download_image(clean_url)

            # 元URLがダメなら_ex付きで再試行
            if img is None or max(img.size) < 50:
                img = _download_image(url)

            if img is None:
                print(f"      ⚠️ 画像{i+1}: ダウンロード失敗、スキップ")
                continue

            # RGBA → RGB変換（白背景）
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # --- 品質チェック（番号指定時はスキップ） ---
            # 1) サイズチェック（壊れた画像や極小画像を除外）
            if img.width < 200 or img.height < 200:
                print(f"      ⚠️ 画像{i+1}: サイズが小さい ({img.width}x{img.height})、スキップ")
                continue

            if not selected_indices:
                # 自動モードのみ品質チェック（手動選択時はユーザー判断を尊重）
                # 2) テキスト・バナー画像チェック
                if _is_text_heavy(img):
                    print(f"      ⚠️ 画像{i+1}: テキスト/バナー画像と判定、スキップ")
                    continue

            # 3) 単色すぎる画像をスキップ（色情報が少ない＝情報バナー等）
            if _is_low_quality(img):
                print(f"      ⚠️ 画像{i+1}: 品質が低い画像、スキップ")
                continue

            # 端のロゴ・透かしをトリミング
            img = _crop_margins(img, crop_margin_pct)

            # 正方形にリサイズ（Threads向け）
            img = _make_square(img)

            # 保存（JPEG圧縮）
            output_path = os.path.join(IMAGES_DIR, f"{safe_name}_product_{len(processed_paths)+1}.jpg")
            img.save(output_path, format="JPEG", quality=90, optimize=True)
            processed_paths.append(output_path)
            print(f"      ✅ 画像{len(processed_paths)}: {img.width}x{img.height} → {output_path}")

        except Exception as e:
            print(f"      ⚠️ 画像{i+1}の処理に失敗: {e}")
            continue

    if not processed_paths:
        raise RuntimeError("使用可能な商品画像がありません")

    return processed_paths


def _download_image(url: str) -> Image.Image | None:
    """画像をダウンロードしてPIL Imageとして返す。失敗時はNone。"""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        return img
    except Exception:
        return None


def _is_text_heavy(img: Image.Image, threshold: float = 15.0) -> bool:
    """
    テキストが多い画像（バナー、サイズ表、ランキング表示等）を検出。

    エッジ検出でテキスト量を推定する。
    テキストが多い画像はエッジの高強度ピクセルが多い。

    Args:
        img: PIL Image（RGB）
        threshold: テキスト率の閾値（%）。これ以上ならテキスト画像と判定

    Returns:
        True = テキストが多い（使わない方がいい）
    """
    # リサイズして処理を高速化
    small = img.copy()
    small.thumbnail((300, 300))

    edges = small.filter(ImageFilter.FIND_EDGES)
    edge_arr = np.array(edges, dtype=np.float32)

    # 高強度エッジの割合（テキストの輪郭）
    high_edge_ratio = (edge_arr > 40).mean() * 100

    return high_edge_ratio > threshold


def _is_low_quality(img: Image.Image) -> bool:
    """
    低品質画像を検出（ほぼ単色、極端に暗い/明るい等）。

    Returns:
        True = 低品質（使わない方がいい）
    """
    small = img.copy()
    small.thumbnail((100, 100))
    arr = np.array(small, dtype=np.float32)

    # 標準偏差が極端に低い → ほぼ単色
    if arr.std() < 15:
        return True

    # 平均輝度が極端（真っ白 or 真っ暗）
    mean_val = arr.mean()
    if mean_val > 248 or mean_val < 10:
        return True

    return False


def _crop_margins(img: Image.Image, margin_pct: float) -> Image.Image:
    """
    画像の端をトリミングしてロゴ・透かしを除去。

    端にロゴが入っていることが多いため、上下左右を一定割合カットする。
    """
    if margin_pct <= 0:
        return img

    w, h = img.size
    margin_x = int(w * margin_pct / 100)
    margin_y = int(h * margin_pct / 100)

    cropped = img.crop((margin_x, margin_y, w - margin_x, h - margin_y))
    return cropped


def _make_square(img: Image.Image, size: int = 1080) -> Image.Image:
    """
    画像を白背景で正方形にパディングしてからリサイズ。
    商品が中央に来るようにする。
    """
    w, h = img.size
    max_dim = max(w, h)

    # 正方形キャンバスを作成
    square = Image.new("RGB", (max_dim, max_dim), (255, 255, 255))

    # 中央に配置
    offset_x = (max_dim - w) // 2
    offset_y = (max_dim - h) // 2
    square.paste(img, (offset_x, offset_y))

    # リサイズ
    if max_dim != size:
        square = square.resize((size, size), Image.LANCZOS)

    return square


def check_product_has_good_images(product: dict) -> bool:
    """
    商品が投稿に適した画像を持っているかを事前チェック。

    商品選定フェーズで使用し、画像がダメな商品をスキップする。
    全画像をダウンロードせず、1枚目のみチェックして高速化。

    Returns:
        True = 使える画像がある
    """
    image_urls = product.get("image_urls", [])
    if not image_urls:
        return False

    # 1枚目をチェック
    clean_url = image_urls[0].split("?")[0]
    img = _download_image(clean_url)

    # 元URLがダメならパラメータ付きで再試行
    if img is None or max(img.size) < 50:
        img = _download_image(image_urls[0])

    if img is None or max(img.size) < 200:
        return False

    if img.mode != "RGB":
        img = img.convert("RGB")

    if _is_text_heavy(img):
        return False

    if _is_low_quality(img):
        return False

    return True


if __name__ == "__main__":
    # テスト
    test_product = {
        "item_code": "test_001",
        "image_urls": [
            "https://thumbnail.image.rakuten.co.jp/@0_mall/example/cabinet/item001.jpg?_ex=500x500",
        ],
    }
    paths = process_product_images(test_product)
    print(f"処理済み: {paths}")
