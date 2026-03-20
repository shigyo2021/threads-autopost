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

    def publish_image_post(self, text: str, image_url: str) -> dict:
        """
        画像付きスレッドを投稿する。

        Threads APIの投稿は2ステップ:
          1. メディアコンテナを作成
          2. コンテナを公開

        Args:
            text: 投稿テキスト
            image_url: 画像URL（公開アクセス可能なURL）

        Returns:
            投稿結果の dict
        """
        # Step 1: メディアコンテナ作成
        container_id = self._create_media_container(text, image_url)

        # コンテナの処理完了を待機（画像アップロード等）
        self._wait_for_container(container_id)

        # Step 2: 公開
        result = self._publish_container(container_id)

        return result

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
