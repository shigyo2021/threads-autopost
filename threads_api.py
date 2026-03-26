"""Threads API: 画像付き投稿の作成・公開"""

import time
import requests
from config import THREADS_API_BASE, THREADS_USER_ID, THREADS_ACCESS_TOKEN


class ThreadsClient:
    """Threads Graph API クライアント"""

    def __init__(
        self,
        user_id: str = THREADS_USER_ID,
        access_token: str = THREADS_ACCESS_TOKEN,
    ):
        self.user_id = user_id
        self.access_token = access_token
        self.base_url = THREADS_API_BASE

    def publish_image_post(self, text: str, image_url: str, max_retries: int = 3) -> dict:
        """
        画像付きスレッドを投稿する（リトライ付き）。

        Threads APIの投稿は2ステップ:
          1. メディアコンテナを作成
          2. コンテナを公開

        Args:
            text: 投稿テキスト
            image_url: 画像URL（公開アクセス可能なURL）
            max_retries: 最大リトライ回数

        Returns:
            投稿結果の dict
        """
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # Step 1: メディアコンテナ作成
                container_id = self._create_media_container(text, image_url)

                # コンテナの処理完了を待機（画像アップロード等）
                self._wait_for_container(container_id)

                # Step 2: 公開
                result = self._publish_container(container_id)
                return result
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    wait = 10 * attempt
                    print(f"      ⚠️ 投稿リトライ ({attempt}/{max_retries})... {wait}秒待機")
                    time.sleep(wait)
        raise last_error

    def publish_carousel_post(self, text: str, image_urls: list[str], max_retries: int = 3) -> dict:
        """
        カルーセル（複数画像）投稿（リトライ付き）。

        Threads APIのカルーセル投稿は3ステップ:
          1. 各画像のメディアコンテナを作成
          2. カルーセルコンテナを作成
          3. 公開

        Args:
            text: 投稿テキスト
            image_urls: 画像URLリスト（2〜20枚）
            max_retries: 最大リトライ回数
        """
        if len(image_urls) < 2:
            # 1枚の場合は通常の画像投稿にフォールバック
            return self.publish_image_post(text, image_urls[0], max_retries)

        try:
            # Step 1: 各画像のメディアコンテナを作成
            child_ids = []
            for img_url in image_urls:
                child_id = self._create_carousel_item(img_url)
                child_ids.append(child_id)

            # 全コンテナの処理完了を待機
            for child_id in child_ids:
                self._wait_for_container(child_id)

            # Step 2: カルーセルコンテナを作成
            carousel_id = self._create_carousel_container(text, child_ids)
            self._wait_for_container(carousel_id)

            # Step 3: 公開
            result = self._publish_container(carousel_id)
            return result
        except Exception as e:
            print(f"      ⚠️ カルーセル投稿失敗: {e}")
            print(f"      → 1枚画像で投稿します")
            return self.publish_image_post(text, image_urls[0], max_retries)

    def _create_carousel_item(self, image_url: str) -> str:
        """カルーセル用の画像アイテムコンテナを作成"""
        url = f"{self.base_url}/{self.user_id}/threads"
        data = {
            "media_type": "IMAGE",
            "image_url": image_url,
            "is_carousel_item": "true",
            "access_token": self.access_token,
        }
        resp = requests.post(url, data=data, timeout=30)
        if not resp.ok:
            print(f"      [DEBUG] create_carousel_item error: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        return resp.json()["id"]

    def _create_carousel_container(self, text: str, children_ids: list[str]) -> str:
        """カルーセルコンテナを作成"""
        url = f"{self.base_url}/{self.user_id}/threads"
        data = {
            "media_type": "CAROUSEL",
            "children": ",".join(children_ids),
            "text": text,
            "access_token": self.access_token,
        }
        resp = requests.post(url, data=data, timeout=30)
        if not resp.ok:
            print(f"      [DEBUG] create_carousel_container error: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        return resp.json()["id"]

    def publish_text_post(self, text: str) -> dict:
        """テキストのみの投稿"""
        container_id = self._create_text_container(text)
        self._wait_for_container(container_id)
        return self._publish_container(container_id)

    def publish_reply(self, text: str, reply_to_id: str) -> dict:
        """既存の投稿に返信する"""
        url = f"{self.base_url}/{self.user_id}/threads"
        data = {
            "media_type": "TEXT",
            "text": text,
            "reply_to_id": reply_to_id,
            "access_token": self.access_token,
        }
        resp = requests.post(url, data=data, timeout=30)
        resp.raise_for_status()
        container_id = resp.json()["id"]
        self._wait_for_container(container_id)
        return self._publish_container(container_id)

    def _create_media_container(self, text: str, image_url: str) -> str:
        """画像付きメディアコンテナを作成"""
        url = f"{self.base_url}/{self.user_id}/threads"
        data = {
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": text,
            "access_token": self.access_token,
        }
        resp = requests.post(url, data=data, timeout=30)
        if not resp.ok:
            print(f"      [DEBUG] create_media_container error: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        return resp.json()["id"]

    def _create_text_container(self, text: str) -> str:
        """テキストのみのコンテナを作成"""
        url = f"{self.base_url}/{self.user_id}/threads"
        data = {
            "media_type": "TEXT",
            "text": text,
            "access_token": self.access_token,
        }
        resp = requests.post(url, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()["id"]

    def _wait_for_container(self, container_id: str, timeout: int = 60):
        """コンテナの処理完了を待機"""
        url = f"{self.base_url}/{container_id}"
        params = {
            "fields": "status",
            "access_token": self.access_token,
        }
        for _ in range(timeout // 3):
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
            status = data.get("status")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise RuntimeError(f"コンテナ処理エラー: {data}")
            time.sleep(3)
        raise TimeoutError(f"コンテナ処理タイムアウト: {container_id}")

    def _publish_container(self, container_id: str) -> dict:
        """コンテナを公開"""
        url = f"{self.base_url}/{self.user_id}/threads_publish"
        params = {
            "creation_id": container_id,
            "access_token": self.access_token,
        }
        resp = requests.post(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def refresh_long_lived_token(self) -> str:
        """長期トークンをリフレッシュ（60日ごと）"""
        url = f"{self.base_url}/refresh_access_token"
        params = {
            "grant_type": "th_refresh_token",
            "access_token": self.access_token,
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        new_token = data["access_token"]
        print(f"トークン更新完了 (有効期限: {data.get('expires_in', '?')}秒)")
        return new_token
