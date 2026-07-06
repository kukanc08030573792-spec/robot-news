#!/usr/bin/env python3
"""ロボットニュース収集スクリプト
RSS / Google News / YouTubeチャンネルRSS から記事を収集し、
GitHub Models で要約・分類して docs/data/ に出力する。
GitHub Actions から毎朝実行される想定（ローカル実行も可）。

v2: Google NewsリダイレクトURLの実URL復号（サムネイル取得対応）、
    タイトルベースの重複排除（配信先違いの同一記事）を追加。
v3: 既存データの自動修復を追加（過去記事のURL復号・サムネイル取得・
    タイトル重複掃除を毎回の実行で少しずつ実施）。
"""
import base64
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


def title_key(title):
    """タイトルの実質重複を検出するための正規化キー"""
    t = re.sub(r"[\s　]+", "", str(title).lower())
    t = re.sub(r"[^0-9a-zA-Zぁ-んァ-ヶ一-龠ー]", "", t)
    return t[:60]


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


def is_gnews(url):
    return "news.google.com" in urllib.parse.urlsplit(url).netloc


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


# ---------------- Google News URL復号 ----------------

def _gnews_id(url):
    m = re.search(r"news\.google\.com/(?:rss/)?(?:articles|read)/([^?/&]+)", url)
    return m.group(1) if m else None


def decode_gnews_url(url, timeout):
    """Google NewsリダイレクトURL → 実記事URL（失敗時はNone）"""
    gid = _gnews_id(url)
    if not gid:
        return None
    # 旧形式: base64内に実URLが直接埋まっている
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
        m = re.search(rb'https?://[^\x00-\x20"\\]+', raw)
        if m and b"news.google.com" not in m.group(0):
            return m.group(0).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        pass
    # 新形式: 内部API（batchexecute）で復号
    try:
        page = requests.get(f"https://news.google.com/rss/articles/{gid}",
                            headers=UA, timeout=timeout)
        soup = BeautifulSoup(page.text, "html.parser")
        div = soup.select_one("c-wiz > div[data-n-a-sg][data-n-a-ts]")
        if not div:
            return None
        sg, ts = div["data-n-a-sg"], div["data-n-a-ts"]
        inner = (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"JP:ja",null,1,'
            'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
            f'"{gid}",{ts},"{sg}"]'
        )
        body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
        r = requests.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            headers={**UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            data=body, timeout=timeout)
        r.raise_for_status()
        chunk = json.loads(r.text.split("\n\n")[1])
        real = json.loads(chunk[0][2])[1]
        if isinstance(real, str) and real.startswith("http"):
            return real
    except Exception as e:  # noqa: BLE001
        log(f"GoogleNews復号失敗 {gid[:24]}…: {e}")
    return None


# ---------------- サムネイル・本文情報（OGP） ----------------

def fetch_ogp(url, timeout):
    """og:image / og:description を取得"""
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        final = r.url
        if is_gnews(final):
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


# ---------------- 既存データの修復 ----------------

REPAIR_DECODE_LIMIT = 30   # 1回の実行で復号を試す既存記事の上限
REPAIR_THUMB_LIMIT = 30    # 1回の実行でサムネ取得を試す既存記事の上限
REPAIR_MAX_TRIES = 3       # 失敗の再試行上限（超えたら諦める）


def _better_item(a, b):
    """同一タイトルの2件から残す方を選ぶ（実URL＞サムネ有り＞先勝ち）"""
    def score(x):
        return (not is_gnews(x.get("url", "")), bool(x.get("thumbnail")))
    return a if score(a) >= score(b) else b


def repair_items(items, seen, timeout):
    """過去記事の修復：タイトル重複掃除・Google News URL復号・サムネ取得。
    処理量は上限付きで、数日かけて自然に全件修復される。"""
    changed = False

    # 1) タイトル重複の掃除
    order, by_key, dropped = [], {}, set()
    for it in items:
        k = title_key(it.get("title", ""))
        if not k:
            order.append(it)
            continue
        if k in by_key:
            keep = _better_item(by_key[k], it)
            drop = it if keep is by_key[k] else by_key[k]
            dropped.add(drop["id"])
            if keep is not by_key[k]:
                order[order.index(by_key[k])] = keep
                by_key[k] = keep
            changed = True
        else:
            by_key[k] = it
            order.append(it)
    items = order
    if dropped:
        log(f"修復: タイトル重複 {len(dropped)}件を削除")

    # 2) Google News URLの復号（サムネ取得の前提）
    n = 0
    for it in items:
        if n >= REPAIR_DECODE_LIMIT:
            break
        if is_gnews(it.get("url", "")) and it.get("_fix_tries", 0) < REPAIR_MAX_TRIES:
            real = decode_gnews_url(it["url"], timeout)
            time.sleep(1)
            n += 1
            changed = True
            if real:
                it["url"] = real
                seen.add(item_id(real))  # 実URL側でも再収集を防ぐ
                it.pop("_fix_tries", None)
            else:
                it["_fix_tries"] = it.get("_fix_tries", 0) + 1
    if n:
        log(f"修復: URL復号を{n}件試行")

    # 3) サムネイル取得
    m = 0
    for it in items:
        if m >= REPAIR_THUMB_LIMIT:
            break
        if (it.get("type") == "article" and not it.get("thumbnail")
                and not is_gnews(it.get("url", ""))
                and it.get("_thumb_tries", 0) < REPAIR_MAX_TRIES):
            img, _desc, _final = fetch_ogp(it["url"], timeout)
            m += 1
            changed = True
            if img:
                it["thumbnail"] = img
                it.pop("_thumb_tries", None)
            else:
                it["_thumb_tries"] = it.get("_thumb_tries", 0) + 1
    if m:
        log(f"修復: サムネ取得を{m}件試行")

    return items, dropped, changed


# ---------------- メイン ----------------

def main():
    cfg = yaml.safe_load((ROOT / "sources.yml").read_text(encoding="utf-8")) or {}
    st = cfg.get("settings", {}) or {}
    timeout = st.get("request_timeout_sec", 15)
    lookback = timedelta(days=st.get("lookback_days", 4))
    max_new = st.get("max_new_items_per_run", 60)
    now = datetime.now(timezone.utc)

    seen = set(load_json(DATA / "seen_ids.json", []))
    processed = set()  # 今回の実行で処理した全ID（不採用も含め、再処理を防ぐ）
    candidates = collect_candidates(cfg)
    log(f"候補 {len(candidates)}件")

    # 1) URLベースの重複・期間外を除外
    fresh, batch_ids = [], set()
    for it in candidates:
        it["id"] = item_id(it["url"])
        if it["id"] in seen or it["id"] in batch_ids:
            continue
        if it["published"] and now - it["published"] > lookback:
            continue
        batch_ids.add(it["id"])
        fresh.append(it)
    fresh.sort(key=lambda x: x["published"] or now, reverse=True)
    fresh = fresh[:max_new]
    processed.update(it["id"] for it in fresh)
    log(f"新規 {len(fresh)}件")

    # 2) Google NewsリダイレクトURLを実URLへ復号
    for it in fresh:
        if is_gnews(it["url"]):
            real = decode_gnews_url(it["url"], timeout)
            if real:
                it["url"] = real
                it["id"] = item_id(real)
                processed.add(it["id"])
            time.sleep(1)  # Google側への配慮

    # 3) 復号後URLでの再重複チェック
    deduped, ids2 = [], set()
    for it in fresh:
        if it["id"] in seen or it["id"] in ids2:
            continue
        ids2.add(it["id"])
        deduped.append(it)
    fresh = deduped

    # 4) タイトルの実質重複を排除（配信先違いの同一記事）
    existing_titles = {
        title_key(a.get("title", ""))
        for a in load_json(DATA / "articles.json", {}).get("items", [])
    }

    def better(a, b):
        """同一タイトルの場合、実URL（非Google News）＞サムネイル有り を優先"""
        def score(x):
            return (not is_gnews(x["url"]), bool(x.get("thumbnail")))
        return a if score(a) >= score(b) else b

    no_key, by_title = [], {}
    for it in fresh:
        k = title_key(it["title"])
        if not k:
            no_key.append(it)
        elif k in existing_titles:
            continue  # 過去に掲載済みの同一タイトル
        elif k in by_title:
            by_title[k] = better(by_title[k], it)
        else:
            by_title[k] = it
    fresh = no_key + list(by_title.values())
    fresh.sort(key=lambda x: x["published"] or now, reverse=True)
    log(f"重複排除後 {len(fresh)}件")

    # 5) OGP（サムネ・説明文）取得 ※動画はサムネ取得済み
    for it in fresh:
        if it["type"] == "article":
            img, desc, final_url = fetch_ogp(it["url"], timeout)
            if img:
                it["thumbnail"] = img
            if desc and len(desc) > len(it["description"]):
                it["description"] = desc[:300]
            if final_url != it["url"] and not is_gnews(final_url):
                it["url"] = final_url
                it["id"] = item_id(final_url)
                processed.add(it["id"])
    # 最終URLでの再重複チェック
    final, ids3 = [], set()
    for it in fresh:
        if it["id"] in seen or it["id"] in ids3:
            continue
        ids3.add(it["id"])
        final.append(it)
    fresh = final

    # 6) AI要約・分類
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

    # 過去記事の修復（重複掃除・URL復号・サムネ取得）
    recent, dropped_ids, repaired = repair_items(recent, seen, timeout)

    # 修復結果を月別アーカイブにも反映
    if repaired:
        rep = {it["id"]: it for it in recent}
        for path in ARCHIVE.glob("*.json"):
            arch = load_json(path, [])
            new_arch = [rep.get(a["id"], a) for a in arch
                        if a["id"] not in dropped_ids]
            if new_arch != arch:
                save_json(path, new_arch)

    # 既知ID更新（不採用・重複分も含め再処理しない）
    seen.update(processed)
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
