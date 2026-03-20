"""AI画像生成: 商品画像 → インテリア空間イメージ"""

import base64
import os
import random
import time
import requests
from openai import OpenAI
from config import OPENAI_API_KEY, ROOM_STYLES, IMAGES_DIR

client = OpenAI(api_key=OPENAI_API_KEY)


def generate_interior_image(
    product: dict,
    style: str | None = None,
    output_path: str | None = None,
) -> str:
    """
    商品情報からインテリアイメージ画像を生成する。

    Args:
        product: 楽天商品情報 dict
        style: ROOM_STYLES のキー（None ならランダム）
        output_path: 保存先パス（None なら自動生成）

    Returns:
        生成画像の保存パス
    """
    if style is None:
        style = random.choice(list(ROOM_STYLES.keys()))

    room = ROOM_STYLES[style]

    # --- プロンプト構築 ---
    prompt = (
        f"A photorealistic interior design photograph of a typical modern Japanese apartment room. "
        f"The room has {room['description']}. "
        f"The room features a {product['name'][:50]} as the focal point. "
        f"IMPORTANT: The furniture product must look exactly like the reference product image. "
        f"The room should look like a real Japanese home (マンション/アパート): "
        f"compact but well-organized space, realistic Japanese ceiling height (~2.4m), "
        f"Japanese-style flooring, practical layout typical of Japanese living rooms or bedrooms. "
        f"Natural daylight from standard Japanese windows. "
        f"The overall atmosphere should feel achievable and relatable for Japanese homeowners. "
        f"Professional interior magazine photography style, warm and inviting. "
        f"No text, no watermarks, no logos."
    )

    # --- 方法1: GPT-4o (gpt-image-1) で生成（リトライ付き） ---
    max_retries = 3
    image_result = None
    for attempt in range(1, max_retries + 1):
        try:
            if product.get("image_url"):
                image_result = _generate_with_reference(prompt, product["image_url"])
            else:
                image_result = _generate_without_reference(prompt)
            break  # 成功したらループを抜ける
        except Exception as e:
            print(f"   ⚠️ 画像生成リトライ ({attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                raise RuntimeError(f"画像生成が{max_retries}回失敗しました: {e}")
            time.sleep(5 * attempt)  # 段階的に待機

    # --- 保存 ---
    os.makedirs(IMAGES_DIR, exist_ok=True)
    if output_path is None:
        safe_name = product["item_code"].replace(":", "_")
        output_path = os.path.join(IMAGES_DIR, f"{safe_name}_{style}.png")

    with open(output_path, "wb") as f:
        f.write(image_result)

    return output_path


def _generate_with_reference(prompt: str, reference_image_url: str) -> bytes:
    """
    商品画像を参照して、その家具が配置されたインテリア画像を生成。
    GPT-4oの画像生成（gpt-image-1）を使用。
    """
    # 参照画像をダウンロード
    img_resp = requests.get(reference_image_url, timeout=15)
    img_b64 = base64.b64encode(img_resp.content).decode()

    response = client.responses.create(
        model="gpt-4o",
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{img_b64}",
                    },
                    {
                        "type": "input_text",
                        "text": (
                            f"This is a product photo. Your task: \n"
                            f"1. COPY this product EXACTLY as it appears - do NOT change its shape, color, pattern, material, size ratio, or any design details. The product must be pixel-level faithful to the reference.\n"
                            f"2. Place this exact product into a room scene.\n"
                            f"3. The room setting: {prompt}\n"
                            f"CRITICAL: The product is the hero of the image. Do not modify, stylize, or reinterpret the product in any way."
                        ),
                    },
                ],
            }
        ],
        tools=[{"type": "image_generation", "size": "1024x1024", "quality": "high"}],
    )

    # レスポンスから画像を抽出
    for block in response.output:
        if block.type == "image_generation_call":
            image_data = block.result
            if isinstance(image_data, str):
                return base64.b64decode(image_data)
            elif hasattr(image_data, "image"):
                return base64.b64decode(image_data.image)
            elif hasattr(image_data, "b64_json"):
                return base64.b64decode(image_data.b64_json)
            else:
                return base64.b64decode(str(image_data))

    raise RuntimeError("画像生成に失敗しました")


def _generate_without_reference(prompt: str) -> bytes:
    """参照画像なしで生成（フォールバック）"""
    response = client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        size="1024x1024",
        quality="high",
        n=1,
    )

    # URL or base64
    image_data = response.data[0]
    if hasattr(image_data, "b64_json") and image_data.b64_json:
        return base64.b64decode(image_data.b64_json)
    else:
        img_resp = requests.get(image_data.url, timeout=30)
        return img_resp.content


# --- 代替: Stability AI APIを使う場合 ---

def generate_with_stability(
    product: dict,
    style: str | None = None,
    output_path: str | None = None,
    stability_api_key: str = "",
) -> str:
    """
    Stability AI API (SDXL) を使った代替実装。
    コストを抑えたい場合はこちらを使用。
    
    必要: STABILITY_API_KEY を .env に追加
    """
    if style is None:
        style = random.choice(list(ROOM_STYLES.keys()))
    room = ROOM_STYLES[style]

    url = "https://api.stability.ai/v2beta/stable-image/generate/sd3"

    prompt = (
        f"Interior design magazine photo, {room['description']}, "
        f"featuring {product['name'][:50]}, professional photography, "
        f"perfect lighting, high quality, no text"
    )

    headers = {
        "Authorization": f"Bearer {stability_api_key}",
        "Accept": "image/*",
    }

    # 商品画像を参照として送信
    files = {"none": ""}
    data = {
        "prompt": prompt,
        "negative_prompt": "text, watermark, logo, low quality, blurry",
        "output_format": "png",
        "aspect_ratio": "1:1",
    }

    # image-to-image（商品画像の参照）
    if product.get("image_url"):
        img_resp = requests.get(product["image_url"], timeout=15)
        files = {"image": ("product.jpg", img_resp.content, "image/jpeg")}
        data["strength"] = 0.65  # 元画像の影響度（低いほど元画像に近い）

    resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    resp.raise_for_status()

    os.makedirs(IMAGES_DIR, exist_ok=True)
    if output_path is None:
        safe_name = product["item_code"].replace(":", "_")
        output_path = os.path.join(IMAGES_DIR, f"{safe_name}_{style}.png")

    with open(output_path, "wb") as f:
        f.write(resp.content)

    return output_path
