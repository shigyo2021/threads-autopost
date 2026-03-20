# Threads × 楽天アフィリエイト 自動投稿Bot

楽天の家具商品を取得 → AI画像生成（インテリアイメージ） → 投稿文生成 → Threads自動投稿

## アーキテクチャ

```
┌─────────────┐    ┌──────────────────┐    ┌──────────────────┐    ┌─────────────┐
│ 楽天API      │───▶│ OpenAI 画像生成   │───▶│ Claude 投稿文生成 │───▶│ Threads API │
│ 商品取得     │    │ インテリア合成    │    │ キャプション作成  │    │ 自動投稿    │
└─────────────┘    └──────────────────┘    └──────────────────┘    └─────────────┘
        │                                                                │
        └────────────────────── cron / scheduler ────────────────────────┘
```

## セットアップ

### 1. 必要なAPIキーを取得

| サービス | 取得先 | 用途 |
|---------|--------|------|
| 楽天アプリID | https://webservice.rakuten.co.jp/ | 商品検索 |
| 楽天アフィリエイトID | https://affiliate.rakuten.co.jp/ | アフィリエイトリンク生成 |
| OpenAI API Key | https://platform.openai.com/ | 画像生成 (GPT-4o) |
| Anthropic API Key | https://console.anthropic.com/ | 投稿文生成 |
| Threads Access Token | Meta Developer Portal | Threads投稿 |

### 2. 環境変数を設定

```bash
cp .env.example .env
# .env を編集して各APIキーを入力
```

### 3. 依存関係インストール

```bash
pip install -r requirements.txt
```

### 4. 実行

```bash
# 単発テスト（1商品を処理して投稿）
python main.py --test

# ドライラン（投稿せずに画像と文章を確認）
python main.py --dry-run

# 本番実行（指定件数を処理）
python main.py --count 3

# cron用（毎日朝8時・昼12時・夜20時に1件ずつ投稿）
# crontab -e で以下を追加:
# 0 8,12,20 * * * cd /path/to/threads-affiliate-bot && python main.py --count 1
```

## Threads API のセットアップ

1. https://developers.facebook.com/ でアプリ作成
2. 「Threads API」を追加
3. アクセストークンを取得（threads_basic, threads_content_publish スコープ）
4. 長期トークンに交換（60日有効、自動更新スクリプト付属）

## 注意事項

- 投稿には必ず「PR」「AI生成イメージ」の表記を含めます
- 楽天アフィリエイトガイドライン・景品表示法・ステマ規制に準拠
- Threads APIのレート制限: 250投稿/24時間（余裕を持って運用）
- 生成画像は `output/images/` に保存されます
