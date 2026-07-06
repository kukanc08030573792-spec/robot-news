#!/usr/bin/env python3
"""ロボットニュース収集スクリプト
RSS / Google News / YouTubeチャンネルRSS から記事を収集し、
GitHub Models で要約・分類して docs/data/ に出力する。
GitHub Actions から毎朝実行される想定（ローカル実行も可）。
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"
ARCHIVE = DATA / "archive"
JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (compatible; robot-news-collector/1.0)"}

RECENT_LIMIT = 500  # articles.json に載せる最新件数（全件はarchiveに保持）


def log(*args):
    print("[collect]", *args, file=sys.stderr)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def normalize_url(url):
    """トラッキングパラメータ除去などでURLを正規化（重複排除キー用）"""
    try:
        p = urllib.parse.urlsplit(url.strip())
        q = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(p.query)
            if not k.lower().startswith(("utm_", "fbclid", "gclid", "yclid", "cmpid"))
        ]
        return urllib.parse.urlunsplit(
            (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"),
             urllib.parse.urlencode(q), "")
        )
    except ValueError:
        return url


def item_id(url):
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:12]


def entry_datetime(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def strip_tags(text, limit=300):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


# ---------------- ソース収集 ----------------

def fetch_feed(url, timeout):
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
        return feedparser.parse(r.content)
    except Exception as e:  # noqa: BLE001
        log(f"feed取得失敗 {url}: {e}")
        return None


def resolve_youtube_channel(handle, cache, timeout):
    """@handle → channel_id（UC...）を解決。結果はキャッシュ。"""
    if handle in cache:
        return cache[handle]
    try:
        r = requests.get(f"https://www.youtube.com/@{handle}", headers=UA, timeout=timeout)
        m = re.search(r'"channelId":"(UC[\w-]{22})"', r.text) or re.search(
            r'itemprop="identifier" content="(UC[\w-]{22})"', r.text
        )
        if m:
            cache[handle] = m.group(1)
            return m.group(1)
    except Exception as e:  # noqa: BLE001
        log(f"YouTubeハンドル解決失敗 @{handle}: {e}")
    return None


def collect_candidates(cfg):
    """全ソースから候補記事を集める（要約前の生データ）"""
    st = cfg.get("settings", {}) or {}
    timeout = st.get("request_timeout_sec", 15)
    per_feed = st.get("max_items_per_feed", 20)
    candidates = []

    def add_entries(feed, source, kind, is_expert=False):
        if not feed:
            return
        for e in feed.entries[:per_feed]:
            link = e.get("link")
            if not link:
                continue
            item = {
                "url": link,
                "title": strip_tags(e.get("title", ""), 200),
                "source": source,
                "type": kind,
                "expert": is_expert,
                "published": entry_datetime(e),
                "description": strip_tags(
                    e.get("summary", "") or
                    (e.get("content", [{}])[0].get("value", "") if e.get("content") else "")
                ),
            }
            if kind == "video":
                vid = e.get("yt_videoid")
                if vid:
                    item["thumbnail"] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            candidates.append(item)

    for f in cfg.get("rss_feeds") or []:
        add_entries(fetch_feed(f["url"], timeout), f["name"], "article")

    for q in cfg.get("google_news_queries") or []:
        url = ("https://news.google.com/rss/search?q=" +
               urllib.parse.quote(str(q)) + "&hl=ja&gl=JP&ceid=JP:ja")
        feed = fetch_feed(url, timeout)
        if feed:
            for e in feed.entries[:per_feed]:
                # Google Newsのタイトルは「記事名 - 媒体名」形式
                title = strip_tags(e.get("title", ""), 200)
                source = "Google News"
                if " - " in title:
                    title, source = title.rsplit(" - ", 1)
                candidates.append({
                    "url": e.get("link", ""),
                    "title": title,
                    "source": source,
                    "type": "article",
                    "expert": False,
                    "published": entry_datetime(e),
                    "description": strip_tags(e.get("summary", "")),
                })

    ch_cache = load_json(DATA / "channel_cache.json", {})
    for ch in cfg.get("youtube_channels") or []:
        cid = ch.get("channel_id") or resolve_youtube_channel(ch.get("handle", ""), ch_cache, timeout)
        if not cid:
            log(f"チャンネルID不明のためスキップ: {ch.get('name')}")
            continue
        feed = fetch_feed(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", timeout)
        add_entries(feed, ch.get("name", "YouTube"), "video")
    save_json(DATA / "channel_cache.json", ch_cache)

    for ex in cfg.get("experts") or []:
        if ex.get("rss"):
            add_entries(fetch_feed(ex["rss"], timeout), ex.get("name", "有識者"), "article", is_expert=True)

    return candidates


# ---------------- サムネイル・本文情報（OGP） ----------------

def fetch_ogp(url, timeout):
    """og:image / og:description を取得（Google Newsリダイレクトも解決）"""
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        final = r.url
        if "news.google.com" in urllib.parse.urlsplit(final).netloc:
            return None, None, url  # 解決できず
        soup = BeautifulSoup(r.text, "html.parser")

        def og(prop):
            tag = soup.find("meta", attrs={"property": prop}) or soup.find(
                "meta", attrs={"name": prop})
            return tag.get("content", "").strip() if tag else None

        return og("og:image"), og("og:description"), final
    except Exception as e:  # noqa: BLE001
        log(f"OGP取得失敗 {url[:80]}: {e}")
        return None, None, url


# ---------------- GitHub Models 要約・分類 ----------------

PROMPT = """あなたはロボット業界ニュースの編集者です。以下の記事リストをJSONで分類・要約してください。

分類基準:
- "cleaning": 清掃ロボット・清掃技術に直接関係する
- "adjacent": 業務用サービスロボット（配膳・警備・案内・配送・施設向け）、ビルメンテナンスのDX
- "general": 上記以外のロボット全般のうち、業界の大きな話題になるニュースのみ（大型資金調達、大手企業の参入・撤退、重要な技術発表、大きな社会実装）。小ネタは "skip"
- "skip": ロボットと無関係、家庭用製品のセール情報、重複的な軽微ニュース

要約: 日本語で{max_chars}字以内。事実のみ、誇張なし、「です・ます」不要の体言止め可。

入力記事（id, title, description）:
{articles}

出力は次のJSON配列のみ（コードブロック不要）:
[{{"id": "...", "category": "cleaning|adjacent|general|skip", "summary": "..."}}]
"""


def summarize_batch(items, max_chars, token):
    payload = {
        "model": "openai/gpt-4o-mini",
        "temperature": 0.2,
        "messages": [{
            "role": "user",
            "content": PROMPT.format(
                max_chars=max_chars,
                articles=json.dumps(
                    [{"id": it["id"], "title": it["title"],
                      "description": it["description"][:250]} for it in items],
                    ensure_ascii=False),
            ),
        }],
    }
    r = requests.post(
        "https://models.github.ai/inference/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload, timeout=60)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    return {d["id"]: d for d in json.loads(text) if isinstance(d, dict) and "id" in d}


KEYWORDS_CLEANING = ("清掃", "床洗浄", "クリーニング", "cleaning", "scrub", "vacuum")
KEYWORDS_ADJACENT = ("配膳", "警備", "案内", "搬送", "配送", "サービスロボ", "ビルメン", "delivery robot", "service robot")


def fallback_classify(item):
    text = (item["title"] + " " + item["description"]).lower()
    if any(k.lower() in text for k in KEYWORDS_CLEANING):
        return "cleaning"
    if any(k.lower() in text for k in KEYWORDS_ADJACENT):
        return "adjacent"
    return "general"


def classify_and_summarize(new_items, cfg):
    token = os.environ.get("GITHUB_TOKEN", "")
    max_chars = (cfg.get("classification") or {}).get("max_summary_chars", 120)
    results = {}
    if token:
        for i in range(0, len(new_items), 8):
            batch = new_items[i:i + 8]
            try:
                results.update(summarize_batch(batch, max_chars, token))
                time.sleep(3)  # レート制限への配慮
            except Exception as e:  # noqa: BLE001
                log(f"AI要約失敗（batch {i // 8}）: {e}")
    else:
        log("GITHUB_TOKEN未設定のためAI要約をスキップ（フォールバック使用）")

    kept = []
    for it in new_items:
        res = results.get(it["id"])
        if res:
            it["category"] = res.get("category", "general")
            it["summary"] = str(res.get("summary", ""))[: max_chars + 20]
            it["ai"] = True
        else:
            it["category"] = fallback_classify(it)
            it["summary"] = it["description"][:max_chars]
            it["ai"] = False
        if it["category"] == "skip" and not it["expert"]:
            continue  # 有識者の投稿はskip判定でも残す
        if it["category"] == "skip":
            it["category"] = "general"
        kept.append(it)
    return kept


# ---------------- メイン ----------------

def main():
    cfg = yaml.safe_load((ROOT / "sources.yml").read_text(encoding="utf-8")) or {}
    st = cfg.get("settings", {}) or {}
    timeout = st.get("request_timeout_sec", 15)
    lookback = timedelta(days=st.get("lookback_days", 4))
    max_new = st.get("max_new_items_per_run", 60)
    now = datetime.now(timezone.utc)

    seen = set(load_json(DATA / "seen_ids.json", []))
    candidates = collect_candidates(cfg)
    log(f"候補 {len(candidates)}件")

    # 重複・期間外を除外
    fresh, batch_seen = [], set()
    for it in candidates:
        it["id"] = item_id(it["url"])
        if it["id"] in seen or it["id"] in batch_seen:
            continue
        if it["published"] and now - it["published"] > lookback:
            continue
        batch_seen.add(it["id"])
        fresh.append(it)
    fresh.sort(key=lambda x: x["published"] or now, reverse=True)
    fresh = fresh[:max_new]
    log(f"新規 {len(fresh)}件")

    # OGP（サムネ・要約元）取得 ※動画はサムネ取得済み
    for it in fresh:
        if it["type"] == "article":
            img, desc, final_url = fetch_ogp(it["url"], timeout)
            if img:
                it["thumbnail"] = img
            if desc and len(desc) > len(it["description"]):
                it["description"] = desc[:300]
            if final_url != it["url"]:
                it["url"] = final_url
                new_id = item_id(final_url)
                if new_id in seen:
                    it["id"] = None  # リダイレクト解決後に既知だった
                    continue
                it["id"] = new_id
    fresh = [it for it in fresh if it["id"]]

    kept = classify_and_summarize(fresh, cfg)
    log(f"採用 {len(kept)}件（skip除外 {len(fresh) - len(kept)}件）")

    # 出力形式に整形
    def pack(it):
        return {
            "id": it["id"],
            "url": it["url"],
            "title": it["title"],
            "summary": it["summary"],
            "source": it["source"],
            "type": it["type"],
            "category": it["category"],
            "expert": it["expert"],
            "thumbnail": it.get("thumbnail"),
            "published": (it["published"] or now).astimezone(JST).isoformat(timespec="minutes"),
            "collected": now.astimezone(JST).isoformat(timespec="minutes"),
        }

    packed = [pack(it) for it in kept]

    # 月別アーカイブへ追記（全量保持）
    by_month = {}
    for p in packed:
        by_month.setdefault(p["published"][:7], []).append(p)
    for month, items in by_month.items():
        path = ARCHIVE / f"{month}.json"
        arch = load_json(path, [])
        arch_ids = {a["id"] for a in arch}
        arch.extend([p for p in items if p["id"] not in arch_ids])
        arch.sort(key=lambda x: x["published"], reverse=True)
        save_json(path, arch)

    # 最新N件（表示用）
    recent = load_json(DATA / "articles.json", {"items": []}).get("items", [])
    recent_ids = {a["id"] for a in packed}
    recent = packed + [a for a in recent if a["id"] not in recent_ids]
    recent.sort(key=lambda x: x["published"], reverse=True)
    recent = recent[:RECENT_LIMIT]

    # 既知ID更新
    seen.update(p["id"] for p in packed)
    seen.update(it["id"] for it in fresh)  # skip判定分も再処理しない
    save_json(DATA / "seen_ids.json", sorted(seen))

    repo = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
    months = sorted((p.stem for p in ARCHIVE.glob("*.json")), reverse=True)
    meta = {
        "last_updated": now.astimezone(JST).isoformat(timespec="minutes"),
        "repo": repo,
        "archive_months": months,
        "experts": [
            {"name": e.get("name", ""), "x_url": e.get("x_url", ""), "note": e.get("note", "")}
            for e in (cfg.get("experts") or [])
        ],
    }
    save_json(DATA / "articles.json", {"meta": meta, "items": recent})
    write_rss(recent[:50], repo)
    log("完了")


def write_rss(items, repo):
    owner, _, name = repo.partition("/")
    site = f"https://{owner}.github.io/{name}/"
    e = lambda s: html.escape(str(s or ""), quote=True)  # noqa: E731
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        "<title>ロボットニュース収集</title>",
        f"<link>{e(site)}</link>",
        "<description>清掃ロボットを中心としたロボット関連ニュースの自動収集フィード</description>",
        "<language>ja</language>",
    ]
    for it in items:
        parts.append(
            "<item>"
            f"<title>{e(it['title'])}</title>"
            f"<link>{e(it['url'])}</link>"
            f"<guid isPermaLink=\"false\">{e(it['id'])}</guid>"
            f"<description>{e(it['summary'])}</description>"
            f"<pubDate>{e(datetime.fromisoformat(it['published']).strftime('%a, %d %b %Y %H:%M:%S %z'))}</pubDate>"
            "</item>"
        )
    parts.append("</channel></rss>")
    (DOCS / "feed.xml").write_text("\n".join(parts), encoding="utf-8")


if __name__ == "__main__":
    main()
