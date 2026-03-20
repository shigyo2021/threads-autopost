"""画像ホスティング: Threads APIに渡すための公開URL生成

Threads APIは公開アクセス可能な画像URLを要求するため、
生成した画像をどこかにアップロードする必要がある。

以下の方法から選択可能：
1. Firebase Storage（推奨 - Takakiさんの既存スタック）
2. Cloudflare R2
3. imgBB（無料、手軽）
4. 自前サーバー
"""

import base64
import json
import os
import requests


class FirebaseStorageUploader:
    """Firebase Storage に画像をアップロード（推奨）"""

    def __init__(self, bucket_name: str, service_account_path: str = ""):
        self.bucket_name = bucket_name
        self.service_account_path = service_account_path

    def upload(self, file_path: str, remote_name: str | None = None) -> str:
        """
        Firebase Storage に画像をアップロードし、公開URLを返す。

        ※ firebase-admin SDK を使う場合の実装:
        pip install firebase-admin
        """
        try:
            import firebase_admin
            from firebase_admin import credentials, storage
        except ImportError:
            raise ImportError("pip install firebase-admin が必要です")

        if not firebase_admin._apps:
            if self.service_account_path:
                cred = credentials.Certificate(self.service_account_path)
            else:
                cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {
                "storageBucket": self.bucket_name
            })

        bucket = storage.bucket()
        if remote_name is None:
            remote_name = f"threads-images/{os.path.basename(file_path)}"

        blob = bucket.blob(remote_name)
        blob.upload_from_filename(file_path)
        blob.make_public()

        return blob.public_url


class ImgBBUploader:
    """imgBB（無料画像ホスティング）- 手軽に始める場合"""

    def __init__(self, api_key: str):
        """api_key: https://api.imgbb.com/ で無料取得"""
        self.api_key = api_key
        self.url = "https://api.imgbb.com/1/upload"

    def upload(self, file_path: str, **kwargs) -> str:
        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        resp = requests.post(
            self.url,
            data={"key": self.api_key, "image": b64},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["data"]["url"]


class LocalServerUploader:
    """開発用: ローカルサーバーで画像を配信（ngrok等と併用）"""

    def __init__(self, base_url: str, serve_dir: str):
        self.base_url = base_url.rstrip("/")
        self.serve_dir = serve_dir

    def upload(self, file_path: str, **kwargs) -> str:
        """ファイルを配信ディレクトリにコピーして URL を返す"""
        import shutil
        filename = os.path.basename(file_path)
        dest = os.path.join(self.serve_dir, filename)
        shutil.copy2(file_path, dest)
        return f"{self.base_url}/{filename}"


def get_uploader(method: str = "firebase", **kwargs):
    """設定に応じたアップローダーを返す"""
    if method == "firebase":
        return FirebaseStorageUploader(
            bucket_name=kwargs.get("bucket_name", ""),
            service_account_path=kwargs.get("service_account_path", ""),
        )
    elif method == "imgbb":
        from config import IMGBB_API_KEY
        return ImgBBUploader(api_key=kwargs.get("api_key", "") or IMGBB_API_KEY)
    elif method == "local":
        return LocalServerUploader(
            base_url=kwargs.get("base_url", "http://localhost:8000"),
            serve_dir=kwargs.get("serve_dir", "./output/serve"),
        )
    else:
        raise ValueError(f"Unknown upload method: {method}")
