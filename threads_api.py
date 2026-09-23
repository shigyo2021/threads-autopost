"""Threads API: 画像付き投稿の作成・公開"""

import time
import requests
from config import THREADS_API_BASE, THREADS_USER_ID, THREADS_ACCESS_TOKEN
from queue_store import redact


class ThreadsAPIError(RuntimeError):
    """Threads APIのエラー。メッセージにはトークンを含めない"""


def _raise_for_status(resp: requests.Response):
    """
    requests の raise_for_status はURLをメッセージに含める。
    公開や状態確認はトークンをURLに載せているので、代わりにこれを使う。
    """
    if resp.ok:
        return
    try:
        error = resp.json().get("error", {})
        detail = f"{error.get('message', '')} (code={error.get('code')}, subcode={error.get('error_subcode')})"
    except ValueError:
        detail = resp.text[:200]
    raise ThreadsAPIError(redact(f"HTTP {resp.status_code}: {detail}"))


def _send(method: str, url: str, **kwargs) -> requests.Response:
    """通信エラーのメッセージにもURL（トークン入り）が含まれるので伏せ字にして投げ直す"""
    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException as e:
        raise ThreadsAPIError(redact(f"{type(e).__name__}: {e}")) from None


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
            投稿結果の dict（実際に投稿した画像枚数を image_count に入れる）
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
                result["image_count"] = 1
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

        1枚の失敗で全体を捨てないように、画像ごとに作成と待機をまとめて行い、
        だめだった画像だけを落とす。2枚以上残れば必ずカルーセルとして投稿する。

        Args:
            text: 投稿テキスト
            image_urls: 画像URLリスト（2〜20枚）
            max_retries: 最大リトライ回数

        Returns:
            投稿結果の dict（実際に投稿した画像枚数を image_count に入れる）
        """
        if len(image_urls) < 2:
            # 1枚の場合は通常の画像投稿にフォールバック
            return self.publish_image_post(text, image_urls[0], max_retries)

        # Step 1: 各画像のメディアコンテナを作成して処理完了まで待つ（失敗した画像だけスキップ）
        print(f"      画像URLの確認中...")
        child_ids = []
        valid_urls = []
        for img_url in image_urls:
            # 確認が通らなくてもAPIには投げる（HEADを弾くだけのサーバーもあるため）
            self._verify_image_url(img_url)
            try:
                child_id = self._create_carousel_item(img_url)
                self._wait_for_container(child_id)
            except Exception as item_err:
                print(f"      ⚠️ 画像スキップ（{type(item_err).__name__}）: {img_url[:60]}...")
                continue
            child_ids.append(child_id)
            valid_urls.append(img_url)

        if len(child_ids) < 2:
            # 有効な画像が1枚以下 → カルーセルを作れないので1枚で投稿する
            fallback_url = valid_urls[0] if valid_urls else image_urls[0]
            print(f"      ⚠️ 使える画像が{len(child_ids)}枚しかないため、{len(image_urls)}枚のうち1枚だけで投稿します")
            return self.publish_image_post(text, fallback_url, max_retries)

        print(f"      → {len(child_ids)}/{len(image_urls)}枚の画像でカルーセル作成")

        # Step 2: カルーセルコンテナを作成（公開はしないのでリトライしてよい）
        last_error = None
        ready_id = None
        for attempt in range(1, max_retries + 1):
            try:
                carousel_id = self._create_carousel_container(text, child_ids)
                self._wait_for_container(carousel_id)
                ready_id = carousel_id
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    wait = 10 * attempt
                    print(f"      ⚠️ カルーセル作成リトライ ({attempt}/{max_retries})... {wait}秒待機")
                    time.sleep(wait)
        if ready_id is None:
            # ここで1枚に落とすと画像が減ったことに気づけないので、呼び出し元にエラーを返す
            raise ThreadsAPIError(redact(f"カルーセルを作成できなかった: {last_error}"))

        # Step 3: 公開（二重投稿になりうるのでリトライしない）
        result = self._publish_container(ready_id)
        result["image_count"] = len(child_ids)
        return result

    def _verify_image_url(self, image_url: str, max_retries: int = 5) -> bool:
        """画像URLがアクセス可能か確認（CDN伝播待ち対応）"""
        for attempt in range(max_retries):
            try:
                resp = requests.head(image_url, timeout=10, allow_redirects=True)
                content_type = resp.headers.get("content-type", "")
                if resp.ok and "image" in content_type:
                    return True
                # 200だがcontent-typeが画像でない場合
                if resp.ok:
                    print(f"      [DEBUG] URL応答OK但しContent-Type: {content_type}")
            except Exception:
                pass
            if attempt < max_retries - 1:
                wait = 3 * (attempt + 1)
                print(f"      ⏳ 画像URL確認待ち ({attempt+1}/{max_retries})... {wait}秒")
                time.sleep(wait)
        print(f"      ⚠️ 画像URLの確認タイムアウト（投稿を試行します）: {image_url[:60]}...")
        return False

    def _create_carousel_item(self, image_url: str) -> str:
        """カルーセル用の画像アイテムコンテナを作成"""
        url = f"{self.base_url}/{self.user_id}/threads"
        data = {
            "media_type": "IMAGE",
            "image_url": image_url,
            "is_carousel_item": "true",
            "access_token": self.access_token,
        }
        resp = _send("POST", url, data=data, timeout=30)
        if not resp.ok:
            print(f"      [DEBUG] create_carousel_item error: {resp.status_code} {redact(resp.text)[:300]}")
        _raise_for_status(resp)
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
        resp = _send("POST", url, data=data, timeout=30)
        if not resp.ok:
            print(f"      [DEBUG] create_carousel_container error: {resp.status_code} {redact(resp.text)[:300]}")
        _raise_for_status(resp)
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
        resp = _send("POST", url, data=data, timeout=30)
        _raise_for_status(resp)
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
        resp = _send("POST", url, data=data, timeout=30)
        if not resp.ok:
            print(f"      [DEBUG] create_media_container error: {resp.status_code} {redact(resp.text)[:300]}")
        _raise_for_status(resp)
        return resp.json()["id"]

    def _create_text_container(self, text: str) -> str:
        """テキストのみのコンテナを作成"""
        url = f"{self.base_url}/{self.user_id}/threads"
        data = {
            "media_type": "TEXT",
            "text": text,
            "access_token": self.access_token,
        }
        resp = _send("POST", url, data=data, timeout=30)
        _raise_for_status(resp)
        return resp.json()["id"]

    def _wait_for_container(self, container_id: str, timeout: int = 60):
        """コンテナの処理完了を待機"""
        url = f"{self.base_url}/{container_id}"
        params = {
            "fields": "status",
            "access_token": self.access_token,
        }
        for _ in range(timeout // 3):
            resp = _send("GET", url, params=params, timeout=15)
            data = resp.json()
            status = data.get("status")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise ThreadsAPIError(redact(f"コンテナ処理エラー: {data}"))
            time.sleep(3)
        raise TimeoutError(f"コンテナ処理タイムアウト: {container_id}")

    def _publish_container(self, container_id: str) -> dict:
        """コンテナを公開"""
        url = f"{self.base_url}/{self.user_id}/threads_publish"
        params = {
            "creation_id": container_id,
            "access_token": self.access_token,
        }
        resp = _send("POST", url, params=params, timeout=30)
        _raise_for_status(resp)
        return resp.json()

    def check_token(self) -> tuple[bool, str]:
        """
        アクセストークンが有効か確認する（投稿はしない）。

        Returns:
            (有効か, ユーザー名 or エラーメッセージ)
        """
        try:
            resp = _send(
                "GET", f"{self.base_url}/me",
                params={"fields": "id,username", "access_token": self.access_token},
                timeout=15,
            )
        except ThreadsAPIError as e:
            return False, f"接続エラー: {e}"

        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.ok:
            return True, data.get("username", "")
        return False, data.get("error", {}).get("message", f"HTTP {resp.status_code}")

    def refresh_long_lived_token(self) -> str:
        """長期トークンをリフレッシュ（60日ごと）"""
        url = "https://graph.threads.net/refresh_access_token"
        params = {
            "grant_type": "th_refresh_token",
            "access_token": self.access_token,
        }
        resp = _send("GET", url, params=params, timeout=15)
        _raise_for_status(resp)
        data = resp.json()
        new_token = data["access_token"]
        print(f"トークン更新完了 (有効期限: {data.get('expires_in', '?')}秒)")
        return new_token
