#!/usr/bin/env python3
"""ブックマーク処理スクリプト（GitHub Issueトリガー）
サイトの★ボタン → 入力済みIssue作成 → このスクリプトが
bookmarks.json を更新し、Issueにコメントしてクローズする。
リポジトリ所有者本人のIssueのみ有効。
タイトル: "[BM] ..." で追加、"[BM削除] ..." で削除。
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
JST = timezone(timedelta(hours=9))


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def gh(method, path_, token, **kwargs):
    r = requests.request(
        method, f"https://api.github.com{path_}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        timeout=30, **kwargs)
    r.raise_for_status()
    return r


def close_with_comment(repo, number, token, message):
    try:
        gh("POST", f"/repos/{repo}/issues/{number}/comments", token,
           json={"body": message})
        gh("PATCH", f"/repos/{repo}/issues/{number}", token,
           json={"state": "closed"})
    except Exception as e:  # noqa: BLE001
        print(f"Issue操作失敗: {e}", file=sys.stderr)


def find_article(article_id, url):
    """articles.json と月別アーカイブから記事情報を探す"""
    pools = [load_json(DATA / "articles.json", {}).get("items", [])]
    for p in sorted((DATA / "archive").glob("*.json"), reverse=True):
        pools.append(load_json(p, []))
    for pool in pools:
        for a in pool:
            if a.get("id") == article_id or (url and a.get("url") == url):
                return a
    return None


def main():
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    number = os.environ["ISSUE_NUMBER"]
    title = os.environ.get("ISSUE_TITLE", "")
    body = os.environ.get("ISSUE_BODY", "") or ""
    user = os.environ.get("ISSUE_USER", "")
    owner = os.environ.get("REPO_OWNER", "")

    if user != owner:
        close_with_comment(repo, number, token,
                           f"@{user} さん、ブックマークはリポジトリ所有者のみ利用できます。"
                           "（このIssueは自動クローズされます）")
        return

    remove = title.startswith("[BM削除]")
    m_id = re.search(r"^ID:\s*(\S+)", body, re.M)
    m_url = re.search(r"^URL:\s*(\S+)", body, re.M)
    article_id = m_id.group(1) if m_id else None
    url = m_url.group(1) if m_url else None
    if not article_id and not url:
        close_with_comment(repo, number, token,
                           "IssueのbodyにID:またはURL:が見つからなかったため処理できませんでした。")
        return

    bookmarks = load_json(DATA / "bookmarks.json", [])

    if remove:
        before = len(bookmarks)
        bookmarks = [b for b in bookmarks
                     if b.get("id") != article_id and b.get("url") != url]
        msg = ("ブックマークを削除しました。" if len(bookmarks) < before
               else "対象のブックマークが見つかりませんでした。")
    else:
        if any(b.get("id") == article_id or (url and b.get("url") == url)
               for b in bookmarks):
            msg = "既にブックマーク済みです。"
        else:
            art = find_article(article_id, url)
            if art:
                entry = dict(art)
            else:
                entry = {"id": article_id or "", "url": url or "",
                         "title": re.sub(r"^\[BM\]\s*", "", title),
                         "summary": "", "source": "", "type": "article",
                         "category": "general", "thumbnail": None,
                         "published": ""}
            entry["bookmarked_at"] = datetime.now(JST).isoformat(timespec="minutes")
            bookmarks.insert(0, entry)
            msg = "ブックマークに追加しました。サイトへの反映は1〜2分後です。"

    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "bookmarks.json", "w", encoding="utf-8") as f:
        json.dump(bookmarks, f, ensure_ascii=False, indent=1)

    close_with_comment(repo, number, token, msg)


if __name__ == "__main__":
    main()
