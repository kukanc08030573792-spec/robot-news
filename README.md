# ロボットニュース自動収集

清掃ロボットを中心としたロボット関連ニュースを**毎朝6時（JST）に自動収集・AI要約**し、Webサイトとして公開するシステムです。追加料金なし（GitHub無料枠のみ）で動作します。

- 収集：RSS（ロボスタ等）＋ Google Newsキーワード検索 ＋ YouTubeチャンネル ＋ 有識者のnote/ブログ
- 要約・分類：GitHub Models（無料）で日本語要約＋カテゴリ分類（清掃／隣接領域／全般ビッグニュース）
- 閲覧：GitHub Pages（スマホ対応・サムネイル付きカード・URLを知っていれば誰でも閲覧可）
- ブックマーク：記事の「☆ 保存」→ GitHub Issue経由で保存（**リポジトリ所有者のみ**）
- X（Twitter）：API有料化のため自動収集はせず、「有識者」タブにリンク集を表示

---

## セットアップ手順（初回のみ・約15分）

### 1. リポジトリを作成してアップロード

1. GitHubにログイン → 右上「＋」→「New repository」
2. Repository name：`robot-news`（任意）／ **Public** を選択 →「Create repository」
3. このフォルダの中身をアップロード：
   - **Webだけで済ます場合**：リポジトリページの「uploading an existing file」リンクから、このフォルダの中身（`.github`フォルダ含む※）をドラッグ＆ドロップ → Commit
   - **gitを使う場合**：
     ```bash
     cd robot-news
     git init && git add -A && git commit -m "initial"
     git branch -M main
     git remote add origin https://github.com/あなたのユーザー名/robot-news.git
     git push -u origin main
     ```
   - ※ Webアップロードでは `.github` などドットで始まるフォルダがドラッグできない場合があります。その場合は「Add file → Create new file」でファイル名に `.github/workflows/collect.yml` と入力し、中身をコピペしてください（`bookmark.yml` も同様）。

### 2. Actionsの書き込み権限を有効化

リポジトリの **Settings → Actions → General → Workflow permissions** で
**「Read and write permissions」を選択 → Save**（botがコミットするために必要）

### 3. GitHub Pagesを有効化

**Settings → Pages → Build and deployment** で
- Source：**Deploy from a branch**
- Branch：**main** ／ フォルダ：**/docs** → Save

数分後、`https://あなたのユーザー名.github.io/robot-news/` でサイトが見られます。

### 4. 初回収集を実行

**Actionsタブ → collect-news → Run workflow** で手動実行。
2〜3分で完了し、サイトに記事が並びます。以降は毎朝6時（JST）に自動実行されます。

---

## 日常の使い方

| したいこと | 方法 |
|---|---|
| ニュースを見る | サイトURLを開くだけ（スマホOK・共有可） |
| ブックマーク | 記事の「☆ 保存」→ Issue画面が開く → そのまま「Submit new issue」→ 1〜2分でサイトの★ブックマークタブに反映 |
| ブックマーク削除 | ★済みボタン → 削除用Issueを送信 |
| 収集対象を増減 | `sources.yml` をGitHub上で編集（鉛筆アイコン）→ Commit |
| 有識者を登録 | `sources.yml` の `experts:` に追記（noteやブログのRSSを書けば投稿も自動収集） |
| 今すぐ収集 | Actionsタブ → collect-news → Run workflow |
| 通知が欲しい人 | サイトの「RSS」リンクをRSSリーダー（Feedly等）に登録 |

## 仕組み

```
毎朝6時 GitHub Actions（collect.yml）
  → scripts/collect.py が全ソースを巡回
  → 新着をGitHub ModelsでAI要約・分類（cleaning/adjacent/general、対象外はskip）
  → docs/data/articles.json（最新500件）＋ archive/YYYY-MM.json（全量）＋ feed.xml を更新
  → GitHub Pagesが自動配信

☆保存ボタン → 入力済みIssue → bookmark.yml → scripts/bookmark.py
  → 所有者本人か確認 → bookmarks.json 更新 → Issue自動クローズ
```

## 補足・注意

- **費用**：公開リポジトリはActions実行が無料・無制限。GitHub Models・Pagesも無料枠内。**完全無料**で運用できます。
- **公開範囲**：サイト・データ・設定はすべて公開されます（URLを知らなければ実質見つかりませんが、非公開ではありません）。
- **サムネイル**：記事のOGP画像・YouTube公式サムネを直接参照しています（画像はこのリポジトリに保存しません）。リンク先の都合で表示されない場合はプレースホルダーが出ます。
- **AI要約の限界**：要約・分類は自動生成のため誤りがあり得ます。重要な判断はリンク先の原文で確認してください。
- **X（Twitter）**：X APIの読み取りは有料（月額$200〜）のため自動収集していません。代わりにGoogle News・note・ブログRSSで有識者情報を近似カバーし、有識者タブに手動チェック用リンクを置いています。
- **`sources.yml` の初期値**：YouTubeチャンネルのhandleと有識者Xアカウントは例です。実際にフォローしたい対象に差し替えてください（handleが解決できないチャンネルはログに出てスキップされます）。
- **メンテナンス**：60日間コミットがないとGitHubがスケジュール実行を自動停止します。毎日コミットが発生する本システムでは通常起きませんが、長期停止後はActionsタブで再有効化してください。
