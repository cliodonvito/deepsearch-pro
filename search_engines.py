#!/usr/bin/env python3
"""search_engines: multi-engine search orchestrator.

Queries many search sources in parallel, deduplicates by URL, and ranks
results by relevance. Each engine returns [{title, url, snippet, source}]
and may add "thumbnail" (image URL) for video/image results.

Engines (186 total, verified 2026-11-21 — see deep-search-plan.md):
  Web:      duckduckgo (deep pagination), bing
  Obscure:  marginalia (api2, public key), wiby, mojeek (CAPTCHA-blocked)
  Video:    youtube, vimeo (flaky), dailymotion, bing_video, peertube,
            archive_org, odysee
  Images:   bing_images, ddg_images (i.js returns empty from server IP)
  Academic: scholar (Semantic Scholar, rate-limited), pubmed, arxiv
  News:     googlenews (RSS), bingnews
  Social:   reddit (403-blocked from server IP)
  Adult:    166 engines — 9 original tubes + 23 hand-built (xnxx, porntrex,
            youjizz, okxxx, porn00, yespornvip, justporn, pornmike,
            megatube, porndig, tukif, nudevista, watchmygf, porngo,
            jizzbunker, pornlib, pornq, hentaihaven, porntop, zbporn, sexu,
            sunporno, pornheed) + 129 config-driven generic tubes
            (_TUBE_SITES, two mass-probe rounds over ThePornDude's
            directory) + 5 custom (7mmtv, xmissy + damplips/sexyandfunny
            blogs); redtube is a dead stub (JS SPA). Dropped after testing:
            cake.ski + alohatube (search ignores the query), camwhores
            (albums, not videos).

Usage:
    from search_engines import search_all
    results = search_all("psychology of desire", engines=["duckduckgo", "reddit"])
"""
import concurrent.futures
import html
import json
import re
import time
import urllib.parse
import urllib.request

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _fetch(url: str, headers: dict = None, timeout: int = 30,
           data: bytes = None) -> str:
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    """Strip tags + unescape + collapse whitespace."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- web -----

def _ddg_page(query: str, page: int = 0) -> list:
    """One page of DuckDuckGo HTML results (page 0 = first 30)."""
    params = urllib.parse.urlencode({"q": query, "s": page * 30})
    url = f"https://html.duckduckgo.com/html/?{params}"
    page_text = _fetch(url)
    results = []
    blocks = re.findall(r'<div class="result[^"]*"[^>]*>(.*?)</div>\s*</div>',
                        page_text, re.DOTALL)
    for block in blocks:
        link = re.search(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block, re.DOTALL)
        snippet = re.search(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            block, re.DOTALL)
        if not link:
            continue
        raw_url = html.unescape(link.group(1))
        m = re.search(r"[?&]uddg=([^&]+)", raw_url)
        if m:
            raw_url = urllib.parse.unquote(m.group(1))
        title = _clean(link.group(2))
        text = _clean(snippet.group(1)) if snippet else ""
        results.append({"title": title, "url": raw_url, "snippet": text,
                        "source": "duckduckgo"})
    return results


def ddg_search(query: str, max_results: int = 10, deep: bool = False) -> list:
    """DuckDuckGo HTML search. deep=True fetches extra pages (obscure sites
    live past page 1)."""
    try:
        pages = 3 if deep else 1
        results = []
        for p in range(pages):
            results.extend(_ddg_page(query, p))
            if len(results) >= max_results and not deep:
                break
        return results[:max_results] if not deep else results
    except Exception:
        return []


def bing_search(query: str, max_results: int = 10) -> list:
    """Bing HTML search."""
    params = urllib.parse.urlencode({"q": query})
    url = f"https://www.bing.com/search?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    blocks = re.findall(r'<li class="b_algo[^"]*"[^>]*>(.*?)</li>',
                        page, re.DOTALL)
    for block in blocks:
        link = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                         block, re.DOTALL)
        snippet = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        if not link:
            continue
        raw_url = html.unescape(link.group(1))
        # Bing wraps real URLs in /ck/a?...&u=<base64>; unwrap it.
        m = re.search(r"[?&]u=([A-Za-z0-9%_-]+)", raw_url)
        if m:
            try:
                import base64
                padded = urllib.parse.unquote(m.group(1))
                padded += "=" * (-len(padded) % 4)
                raw_url = base64.urlsafe_b64decode(padded).decode(
                    "utf-8", errors="replace")
            except Exception:
                pass
        if raw_url.startswith("/") or "bing.com" in raw_url:
            continue
        title = _clean(link.group(2))
        # Bing prepends the domain to the title; strip it.
        title = re.sub(r"^[\w.-]+\.[a-z]{2,}\s*https?://\S+\s*›?\s*", "",
                       title)
        title = re.sub(r"^[\w.-]+\.[a-z]{2,}\s*›\s*", "", title)
        text = _clean(snippet.group(1)) if snippet else ""
        results.append({"title": title, "url": raw_url, "snippet": text,
                        "source": "bing"})
        if len(results) >= max_results:
            break
    return results


# -------------------------------------------------------------- obscure ---

def marginalia_search(query: str, max_results: int = 10) -> list:
    """Marginalia — surfaces small, independent, non-commercial websites.
    Uses the api2 endpoint with the free public key (no signup). The public
    key is shared and often rate-limited (429); retries once after a delay.
    """
    params = urllib.parse.urlencode({"query": query, "count": max_results,
                                     "timeout": 200})
    url = f"https://api2.marginalia-search.com/search?{params}"
    data = None
    for delay in (0, 3):
        if delay:
            time.sleep(delay)
        try:
            data = json.loads(_fetch(url, headers={"API-Key": "public"}))
            break
        except Exception:
            data = None
    if not data:
        return []
    results = []
    for r in data.get("results", [])[:max_results]:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": _clean(r.get("description", "")),
            "source": "marginalia",
        })
    return results


def wiby_search(query: str, max_results: int = 10) -> list:
    """Wiby — a search engine for the classic, small, independent web.
    Results are <blockquote> blocks with <a class="tlink">."""
    params = urllib.parse.urlencode({"q": query})
    url = f"https://wiby.me/?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    blocks = re.findall(r"<blockquote>(.*?)</blockquote>", page, re.DOTALL)
    for block in blocks:
        link = re.search(
            r'<a class="tlink" href="([^"]+)">(.*?)</a>', block, re.DOTALL)
        if not link:
            continue
        raw_url = html.unescape(link.group(1))
        title = _clean(link.group(2))
        # Snippet is the <p> after the <p class="url"> line.
        paras = re.findall(r"<p>(.*?)</p>", block, re.DOTALL)
        text = _clean(paras[-1]) if paras else ""
        if not title:
            title = raw_url
        results.append({"title": title, "url": raw_url, "snippet": text,
                        "source": "wiby"})
        if len(results) >= max_results:
            break
    return results


def mojeek_search(query: str, max_results: int = 10) -> list:
    """Mojeek — independent engine with its own crawl index. Currently
    CAPTCHA-blocked from the server IP; kept for when that clears."""
    params = urllib.parse.urlencode({"q": query})
    url = f"https://www.mojeek.com/search?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    if "captcha" in page.lower():
        return []
    results = []
    blocks = re.findall(r'<li class="ob[^"]*">(.*?)</li>', page, re.DOTALL)
    for block in blocks:
        link = re.search(r'<a[^>]*class="ob"[^>]*href="([^"]+)"[^>]*>'
                         r'<h2[^>]*>(.*?)</h2>', block, re.DOTALL)
        snippet = re.search(r'<p class="s">(.*?)</p>', block, re.DOTALL)
        if not link:
            continue
        title = _clean(link.group(2))
        text = _clean(snippet.group(1)) if snippet else ""
        results.append({"title": title, "url": html.unescape(link.group(1)),
                        "snippet": text, "source": "mojeek"})
        if len(results) >= max_results:
            break
    return results


# -------------------------------------------------------------- social ----

def reddit_search(query: str, max_results: int = 10) -> list:
    """Reddit public JSON search. Currently 403-blocked from the server IP
    (both www and old.reddit.com); kept for when that clears."""
    params = urllib.parse.urlencode({"q": query, "sort": "relevance",
                                     "limit": max_results})
    url = f"https://www.reddit.com/search.json?{params}"
    try:
        data = json.loads(_fetch(url))
    except Exception:
        return []
    results = []
    for child in data.get("data", {}).get("children", []):
        post = child.get("data", {})
        sub = post.get("subreddit", "")
        title = post.get("title", "")
        permalink = post.get("permalink", "")
        score = post.get("score", 0)
        comments = post.get("num_comments", 0)
        selftext = (post.get("selftext", "") or "")[:300]
        results.append({
            "title": title,
            "url": f"https://www.reddit.com{permalink}",
            "snippet": f"r/{sub} · {score} upvotes · {comments} comments"
                       + (f" · {selftext}" if selftext else ""),
            "source": "reddit",
        })
    return results[:max_results]


# ------------------------------------------------------------ academic ----

def scholar_search(query: str, max_results: int = 10) -> list:
    """Semantic Scholar API — academic papers, no key needed.
    Rate-limited (429) from the server IP; retries with backoff."""
    params = urllib.parse.urlencode({
        "query": query, "limit": max_results,
        "fields": "title,abstract,url,year,authors,citationCount",
    })
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"
    data = None
    for delay in (0, 2, 5, 10):
        if delay:
            time.sleep(delay)
        try:
            data = json.loads(_fetch(url))
            break
        except Exception:
            data = None
    if not data or "data" not in data:
        return []
    results = []
    for paper in data.get("data", []):
        authors = ", ".join(a.get("name", "") for a in
                            paper.get("authors", [])[:3])
        year = paper.get("year", "")
        cites = paper.get("citationCount", 0)
        abstract = (paper.get("abstract", "") or "")[:300]
        results.append({
            "title": paper.get("title", ""),
            "url": paper.get("url", ""),
            "snippet": f"{authors} ({year}) · {cites} citations"
                       + (f" · {abstract}" if abstract else ""),
            "source": "scholar",
        })
    return results


def pubmed_search(query: str, max_results: int = 10) -> list:
    """PubMed via the free NCBI E-utilities API (no key needed).
    esearch for IDs, esummary for titles/dates."""
    try:
        params = urllib.parse.urlencode({
            "db": "pubmed", "term": query, "retmode": "json",
            "retmax": max_results,
        })
        data = json.loads(_fetch(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
            + params))
        ids = data.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        params = urllib.parse.urlencode({
            "db": "pubmed", "id": ",".join(ids), "retmode": "json",
        })
        summ = json.loads(_fetch(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
            + params))
    except Exception:
        return []
    results = []
    res = summ.get("result", {})
    for pmid in ids:
        rec = res.get(pmid, {})
        title = rec.get("title", "")
        if not title:
            continue
        journal = rec.get("fulljournalname", "")
        pubdate = rec.get("pubdate", "")
        authors = ", ".join(a.get("name", "") for a in
                            rec.get("authors", [])[:3])
        results.append({
            "title": title,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "snippet": f"{authors} · {journal} · {pubdate}".strip(" ·"),
            "source": "pubmed",
        })
    return results


def arxiv_search(query: str, max_results: int = 10) -> list:
    """arXiv via the free Atom API (no key needed)."""
    params = urllib.parse.urlencode({
        "search_query": f"all:{query}", "start": 0,
        "max_results": max_results,
    })
    try:
        page = _fetch(f"http://export.arxiv.org/api/query?{params}")
    except Exception:
        return []
    results = []
    entries = re.findall(r"<entry>(.*?)</entry>", page, re.DOTALL)
    for entry in entries:
        title = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
        link = re.search(r'<link[^>]*href="([^"]+)"[^>]*/>', entry)
        summary = re.search(r"<summary>(.*?)</summary>", entry, re.DOTALL)
        published = re.search(r"<published>([^<]+)</published>", entry)
        authors = re.findall(r"<name>([^<]+)</name>", entry)
        if not title or not link:
            continue
        abs_url = link.group(1).replace("http://", "https://")
        text = ""
        if authors:
            text += ", ".join(authors[:3])
        if published:
            text += f" · {published.group(1)[:10]}"
        if summary:
            text += " · " + _clean(summary.group(1))[:250]
        results.append({
            "title": _clean(title.group(1)),
            "url": abs_url,
            "snippet": text.strip(" ·"),
            "source": "arxiv",
        })
    return results


# ---------------------------------------------------------------- news ----

def googlenews_search(query: str, max_results: int = 10) -> list:
    """Google News RSS search (no key needed)."""
    params = urllib.parse.urlencode({
        "q": query, "hl": "en-US", "gl": "US", "ceid": "US:en",
    })
    try:
        page = _fetch(f"https://news.google.com/rss/search?{params}")
    except Exception:
        return []
    results = []
    items = re.findall(r"<item>(.*?)</item>", page, re.DOTALL)
    for item in items:
        title = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
        link = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
        pubdate = re.search(r"<pubDate>(.*?)</pubDate>", item, re.DOTALL)
        source = re.search(r"<source[^>]*>(.*?)</source>", item, re.DOTALL)
        if not title or not link:
            continue
        text = ""
        if source:
            text += _clean(source.group(1))
        if pubdate:
            text += f" · {pubdate.group(1)[:25]}"
        results.append({
            "title": _clean(title.group(1)),
            "url": _clean(link.group(1)),
            "snippet": text.strip(" ·"),
            "source": "googlenews",
        })
        if len(results) >= max_results:
            break
    return results


def bingnews_search(query: str, max_results: int = 10) -> list:
    """Bing News search via the public news page."""
    params = urllib.parse.urlencode({"q": query})
    try:
        page = _fetch(f"https://www.bing.com/news/search?{params}")
    except Exception:
        return []
    results = []
    seen = set()
    cards = re.findall(r'class="news-card[^"]*"[^>]*>(.*?)</div>\s*</div>',
                       page, re.DOTALL)
    for card in cards:
        # Headline link goes through Bing's apiclick redirect; unwrap url=.
        link = re.search(
            r'<a[^>]*href="([^"]*apiclick[^"]*)"[^>]*>(.*?)</a>',
            card, re.DOTALL)
        if not link:
            link = re.search(
                r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                card, re.DOTALL)
        if not link:
            continue
        raw_url = html.unescape(link.group(1))
        m = re.search(r"[?&]url=([^&]+)", raw_url)
        if m:
            raw_url = urllib.parse.unquote(m.group(1))
        if "bing.com" in raw_url or raw_url in seen:
            continue
        title = _clean(link.group(2))
        if not title or len(title) < 10:
            continue
        seen.add(raw_url)
        # Source + time usually appear as small text at the card start.
        text = _clean(card)
        # Trim the headline out of the snippet text if it repeats.
        results.append({"title": title, "url": raw_url,
                        "snippet": text[:200] if text != title else "",
                        "source": "bingnews"})
        if len(results) >= max_results:
            break
    return results


# --------------------------------------------------------------- video ----

def youtube_search(query: str, max_results: int = 10) -> list:
    """YouTube search via the public search page (no API key).
    Titles/channels via oEmbed; thumbnails constructed from video ID."""
    params = urllib.parse.urlencode({"search_query": query})
    url = f"https://www.youtube.com/results?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    seen_ids = set()
    for m in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})"', page):
        vid = m.group(1)
        if vid in seen_ids:
            continue
        seen_ids.add(vid)
        results.append({
            "title": "",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "snippet": "",
            "source": "youtube",
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "_vid": vid,
        })
        if len(results) >= max_results:
            break
    for r in results:
        try:
            oembed = ("https://www.youtube.com/oembed?url="
                      f"https://www.youtube.com/watch?v={r['_vid']}&format=json")
            data = json.loads(_fetch(oembed, timeout=10))
            r["title"] = data.get("title", f"YouTube video {r['_vid']}")
            r["snippet"] = (f"Channel: {data.get('author_name', '')}"
                            if data.get("author_name") else "")
        except Exception:
            r["title"] = f"YouTube video {r['_vid']}"
        r.pop("_vid", None)
    return results


def vimeo_search(query: str, max_results: int = 10) -> list:
    """Vimeo search via the public search page. Flaky: 403s sometimes from
    the server IP. Thumbnails via oEmbed (best-effort)."""
    params = urllib.parse.urlencode({"q": query})
    url = f"https://vimeo.com/search?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    seen = set()
    for m in re.finditer(r'"clip_id":(\d+),"title":"((?:[^"\\]|\\.)*)"',
                         page):
        vid, title = m.group(1), json.loads(f'"{m.group(2)}"')
        if vid in seen:
            continue
        seen.add(vid)
        results.append({"title": title, "url": f"https://vimeo.com/{vid}",
                        "snippet": "", "source": "vimeo", "_vid": vid})
        if len(results) >= max_results:
            break
    for r in results:
        try:
            data = json.loads(_fetch(
                f"https://vimeo.com/api/oembed.json?url="
                f"https://vimeo.com/{r['_vid']}", timeout=10))
            if data.get("thumbnail_url"):
                r["thumbnail"] = data["thumbnail_url"]
            if data.get("author_name"):
                r["snippet"] = f"By {data['author_name']}"
        except Exception:
            pass
        r.pop("_vid", None)
    return results


def dailymotion_search(query: str, max_results: int = 10) -> list:
    """Dailymotion search via the public API (no key needed)."""
    params = urllib.parse.urlencode({
        "search": query, "limit": max_results,
        "fields": "id,title,url,channel.name,duration,thumbnail_360_url",
    })
    url = f"https://api.dailymotion.com/videos?{params}"
    try:
        data = json.loads(_fetch(url))
    except Exception:
        return []
    results = []
    for v in data.get("list", []):
        channel = (v.get("channel") or {}).get("name", "")
        dur = v.get("duration", 0)
        mins, secs = divmod(dur, 60)
        r = {
            "title": v.get("title", ""),
            "url": v.get("url", ""),
            "snippet": (f"Channel: {channel} · {mins}:{secs:02d}"
                        if channel else ""),
            "source": "dailymotion",
        }
        if v.get("thumbnail_360_url"):
            r["thumbnail"] = v["thumbnail_360_url"]
        results.append(r)
    return results


def bing_video_search(query: str, max_results: int = 10) -> list:
    """Bing Video search — aggregates videos from across the web.
    Results live in HTML-escaped JSON blobs (murl/turl) plus title divs;
    the page is unescaped first, then the three ordered lists are zipped."""
    params = urllib.parse.urlencode({"q": query})
    url = f"https://www.bing.com/videos/search?{params}"
    try:
        page = html.unescape(_fetch(url))
    except Exception:
        return []
    # Unique murls/turls/titles in page order.
    def _uniq(seq):
        seen, out = set(), []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out
    murls = _uniq(re.findall(r'"murl":"(https?://[^"]+)"', page))
    turls = _uniq(re.findall(r'"turl":"(https?://[^"]+)"', page))
    titles = _uniq(re.findall(r'class="mc_vtvc_title"[^>]*title="([^"]+)"',
                              page))
    results = []
    for i, media_url in enumerate(murls[:max_results]):
        title = titles[i] if i < len(titles) else media_url
        site = urllib.parse.urlparse(media_url).netloc
        r = {"title": _clean(title), "url": media_url,
             "snippet": f"Source: {site}", "source": "bing_video"}
        if i < len(turls):
            r["thumbnail"] = turls[i]
        results.append(r)
    return results


def peertube_search(query: str, max_results: int = 10) -> list:
    """PeerTube via the public Sepia Search API (no key needed).
    Covers thousands of independent PeerTube instances."""
    params = urllib.parse.urlencode({"search": query})
    try:
        data = json.loads(_fetch(
            f"https://search.joinpeertube.org/api/v1/search/videos?{params}"))
    except Exception:
        return []
    results = []
    for v in data.get("data", [])[:max_results]:
        channel = (v.get("channel") or {}).get("displayName", "")
        dur = v.get("duration", 0)
        mins, secs = divmod(dur, 60)
        text = ""
        if channel:
            text += f"Channel: {channel}"
        if dur:
            text += f" · {mins}:{secs:02d}"
        host = (v.get("account") or {}).get("host", "")
        if host:
            text += f" · {host}"
        r = {
            "title": v.get("name", ""),
            "url": v.get("url", ""),
            "snippet": text.strip(" ·"),
            "source": "peertube",
        }
        if v.get("thumbnailUrl"):
            r["thumbnail"] = v["thumbnailUrl"]
        results.append(r)
    return results


def archive_org_search(query: str, max_results: int = 10) -> list:
    """Internet Archive — documentaries, old films, lectures. Free JSON API,
    no key needed. Restricted to movies (mediatype) for video relevance."""
    q = f"({query}) AND mediatype:movies"
    params = urllib.parse.urlencode({
        "q": q, "fl[]": ["identifier", "title", "description"],
        "rows": max_results, "output": "json",
    }, doseq=True)
    try:
        data = json.loads(_fetch(
            "https://archive.org/advancedsearch.php?" + params))
    except Exception:
        return []
    results = []
    docs = data.get("response", {}).get("docs", [])
    for d in docs:
        ident = d.get("identifier", "")
        title = d.get("title", "")
        if not ident or not title:
            continue
        desc = d.get("description", "")
        if isinstance(desc, list):
            desc = desc[0] if desc else ""
        results.append({
            "title": title,
            "url": f"https://archive.org/details/{ident}",
            "snippet": _clean(str(desc))[:250],
            "source": "archive_org",
            "thumbnail": f"https://archive.org/services/img/{ident}",
        })
    return results


def odysee_search(query: str, max_results: int = 10) -> list:
    """Odysee (LBRY) via the public backend JSON-RPC API (no key needed)."""
    payload = json.dumps({
        "method": "claim_search",
        "params": {"text": query, "page_size": max_results,
                   "claim_type": ["stream"]},
    }).encode()
    try:
        data = json.loads(_fetch(
            "https://api.na-backend.odysee.com/api/v1/proxy",
            headers={"Content-Type": "application/json"}, data=payload))
    except Exception:
        return []
    results = []
    items = data.get("result", {}).get("items", [])
    for item in items:
        val = item.get("value", {}) or {}
        title = val.get("title", "")
        canonical = item.get("canonical_url", "")
        if not title or not canonical:
            continue
        web_url = canonical.replace("lbry://", "https://odysee.com/")
        web_url = web_url.replace("#", ":")
        channel = (item.get("signing_channel") or {}).get("name", "")
        video = val.get("video") or {}
        dur = video.get("duration", 0) if isinstance(video, dict) else 0
        text = ""
        if channel:
            text += f"Channel: {channel}"
        if dur:
            mins, secs = divmod(int(dur), 60)
            text += f" · {mins}:{secs:02d}"
        r = {"title": title, "url": web_url, "snippet": text.strip(" ·"),
             "source": "odysee"}
        thumb = (val.get("thumbnail") or {}).get("url", "")
        if thumb:
            r["thumbnail"] = thumb
        results.append(r)
    return results


# -------------------------------------------------------------- images ----

def ddg_image_search(query: str, max_results: int = 10) -> list:
    """DuckDuckGo image search via the i.js API. The vqd token is fetched
    fine, but i.js returns 0 results from the server IP — kept for when
    that clears."""
    params = urllib.parse.urlencode({"q": query, "iax": "images",
                                     "ia": "images"})
    try:
        page = _fetch(f"https://duckduckgo.com/?{params}")
        vqd = re.search(r'vqd=["\']([0-9-]+)["\']', page)
        if not vqd:
            return []
        token = vqd.group(1)
        api_params = urllib.parse.urlencode({
            "l": "us-en", "o": "json", "q": query, "vqd": token, "f": ",,,,,",
            "p": "1", "s": "0",
        })
        data = json.loads(_fetch(
            f"https://duckduckgo.com/i.js?{api_params}",
            headers={"Referer": "https://duckduckgo.com/"}))
    except Exception:
        return []
    results = []
    for item in data.get("results", [])[:max_results]:
        r = {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": f"{item.get('width', '?')}x{item.get('height', '?')} · "
                       f"Source: {item.get('source', '')}",
            "source": "ddg_images",
        }
        if item.get("thumbnail"):
            r["thumbnail"] = item["thumbnail"]
        results.append(r)
    return results


def bing_image_search(query: str, max_results: int = 10) -> list:
    """Bing image search. Results live in HTML-escaped JSON (murl/turl/t);
    the page is unescaped first, then pairs and titles are zipped."""
    params = urllib.parse.urlencode({"q": query})
    url = f"https://www.bing.com/images/search?{params}"
    try:
        page = html.unescape(_fetch(url))
    except Exception:
        return []
    pairs = re.findall(
        r'"murl":"(https?://[^"]+)"[^}]{0,400}?"turl":"([^"]+)"', page)
    titles = re.findall(r'"t":"((?:[^"\\]|\\.){3,120})"', page)
    results = []
    seen = set()
    for i, (img_url, thumb) in enumerate(pairs):
        if img_url in seen:
            continue
        seen.add(img_url)
        title = ""
        if i < len(titles):
            try:
                title = json.loads(f'"{titles[i]}"')
            except Exception:
                title = titles[i]
        results.append({
            "title": title or "Image",
            "url": img_url,
            "snippet": "",
            "source": "bing_images",
            "thumbnail": thumb,
        })
        if len(results) >= max_results:
            break
    return results


# --------------------------------------------------------------- adult ----
# All adult engines are 18+ tube sites with public search pages.
# Thumbnails are extracted from the search HTML where available.

def _pair_thumbs(page: str, link_re: str, thumb_re: str,
                 max_results: int, base: str, source: str,
                 title_group: int = 2, url_group: int = 1,
                 window: int = 2000) -> list:
    """Generic helper: for each link match, pair the next thumbnail match
    within `window` chars after it. Returns [{title, url, thumbnail}]."""
    results = []
    seen = set()
    thumbs = [(m.start(), m.group(1)) for m in
              re.finditer(thumb_re, page, re.DOTALL)]
    for m in re.finditer(link_re, page, re.DOTALL):
        raw_url = html.unescape(m.group(url_group))
        title = _clean(m.group(title_group))
        url = raw_url if raw_url.startswith("http") else base + raw_url
        if url in seen:
            continue
        seen.add(url)
        thumb = ""
        for tpos, turl in thumbs:
            if m.end() <= tpos <= m.end() + window:
                thumb = turl
                break
        r = {"title": title or url, "url": url, "snippet": "",
             "source": source}
        if thumb:
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            r["thumbnail"] = thumb
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def pornhub_search(query: str, max_results: int = 10) -> list:
    """Pornhub video search via the public search page."""
    params = urllib.parse.urlencode({"search": query})
    url = f"https://www.pornhub.com/video/search?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(/view_video\.php\?viewkey=[a-z0-9]+)"[^>]*title="([^"]+)"',
        r'<img[^>]+src="(https://[^"]*phncdn[^"]+)"',
        max_results, "https://www.pornhub.com", "pornhub")


def xvideos_search(query: str, max_results: int = 10) -> list:
    """XVideos search via the public search page."""
    params = urllib.parse.urlencode({"k": query})
    url = f"https://www.xvideos.com/?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(/video\.[a-z0-9]+/[a-z0-9_\-]+)"[^>]*title="([^"]+)"',
        r'data-src="(https://[^"]+)"',
        max_results, "https://www.xvideos.com", "xvideos",
        window=1500)


def xhamster_search(query: str, max_results: int = 10) -> list:
    """xHamster search — results are JSON embedded in the search page.
    thumbURL is captured from the same videoThumbProps object."""
    q = urllib.parse.quote(query)
    url = f"https://xhamster.com/search/{q}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    seen = set()
    for m in re.finditer(
            r'"title":"((?:[^"\\]|\\.)*)"[^}]{0,200}?"pageURL":"(https://xhamster\.com/videos/[a-zA-Z0-9]+)"',
            page):
        title = json.loads(f'"{m.group(1)}"')
        video_url = m.group(2)
        if video_url in seen:
            continue
        seen.add(video_url)
        r = {"title": title, "url": video_url, "snippet": "",
             "source": "xhamster"}
        thumb = re.search(r'"thumbURL":"([^"]+)"',
                          page[m.end():m.end() + 600])
        if thumb:
            r["thumbnail"] = thumb.group(1)
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def redtube_search(query: str, max_results: int = 10) -> list:
    """RedTube — dead end: desktop + mobile pages are JS-rendered Blazor
    apps behind an age wall, and api.redtube.com returns docs HTML.
    Kept as a stub for a future retry."""
    return []


def thisvid_search(query: str, max_results: int = 10) -> list:
    """ThisVid search via the public search page. Thumbnails are CSS
    background URLs on the thumb span."""
    params = urllib.parse.urlencode({"q": query})
    url = f"https://thisvid.com/search/?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(https://thisvid\.com/videos/[^"]+)" title="([^"]+)"',
        r"background:\s*url\((//static\.thisvid\.com[^)]+)\)",
        max_results, "", "thisvid", window=1200)


def _kvs_search(query: str, max_results: int, base: str, search_path: str,
                source: str) -> list:
    """Shared parser for KVS-platform tubes (DrTuber, NuVid).
    Video links are relative /video/<id>/<slug>; thumbs are img src in the
    card; titles come from the img alt text; duration from time_thumb."""
    url = base + search_path + urllib.parse.quote(query)
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    seen = set()
    for m in re.finditer(r'href="(/video/\d+/[^"]+)"', page):
        path = m.group(1)
        url = base + path
        if url in seen:
            continue
        seen.add(url)
        chunk = page[m.end():m.end() + 900]
        img = re.search(r'<img[^>]+src="(https?://[^"]+)"[^>]*alt="([^"]*)"',
                        chunk)
        title = ""
        thumb = ""
        if img:
            thumb = img.group(1)
            title = _clean(img.group(2))
        if not title:
            slug = path.rstrip("/").split("/")[-1]
            title = slug.replace("-", " ").replace("_", " ").title()
        dur = re.search(r'class="time_thumb"[^>]*>\s*<em>([^<]+)</em>', chunk)
        r = {"title": title, "url": url,
             "snippet": f"Duration: {dur.group(1)}" if dur else "",
             "source": source}
        if thumb:
            r["thumbnail"] = thumb
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def drtuber_search(query: str, max_results: int = 10) -> list:
    """DrTuber search (KVS platform)."""
    return _kvs_search(query, max_results, "https://www.drtuber.com",
                       "/search/videos?search_query=", "drtuber")


def nuvid_search(query: str, max_results: int = 10) -> list:
    """NuVid search (KVS platform)."""
    return _kvs_search(query, max_results, "https://www.nuvid.com",
                       "/search/videos/", "nuvid")


def porncom_search(query: str, max_results: int = 10) -> list:
    """Porn.com search. Video URLs are /scene/<id>/<slug>/; thumbnails are
    constructed from the scene ID (pictures-cdn path embeds it)."""
    params = urllib.parse.urlencode({"query": query})
    url = f"https://www.porn.com/search?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    seen = set()
    for m in re.finditer(
            r'href="(/scene/(\d+)/[^"]+/)"[^>]*title="([^"]+)"', page):
        path, sid, title = m.group(1), m.group(2), _clean(m.group(3))
        url = "https://www.porn.com" + path
        if url in seen:
            continue
        seen.add(url)
        thumb = ("https://pictures-cdn.porn.com/webp/video/"
                 f"{sid[:3]}/{sid[:6]}/{sid}/raw/h165/1.webp")
        results.append({"title": title, "url": url, "snippet": "",
                        "source": "porncom", "thumbnail": thumb})
        if len(results) >= max_results:
            break
    return results


def pornhat_search(query: str, max_results: int = 10) -> list:
    """PornHat search. Thumbnails are lazy-loaded (data-original)."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://www.pornhat.com/search/{q}/"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(/video/[^"]+)" title="([^"]+)"',
        r'data-original="(https?://[^"]+)"',
        max_results, "https://www.pornhat.com", "pornhat",
        window=1200)


def hqporner_search(query: str, max_results: int = 10) -> list:
    """HQPorner search. Titles come from the click-trigger link text;
    thumbnails are the _main.jpg images, matched to videos by ID via the
    defaultImage("..._main.jpg","cover_<id>") calls."""
    params = urllib.parse.urlencode({"s": query})
    url = f"https://hqporner.com/?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    thumbs = {}
    for m in re.finditer(
            r'defaultImage\("([^"]+_main\.jpg)","cover_(\d+)"', page):
        thumbs[m.group(2)] = m.group(1)
    results = []
    seen = set()
    for m in re.finditer(
            r'href="(/hdporn/(\d+)-[^"]+\.html)"[^>]*class="click-trigger"[^>]*>'
            r"([^<]+)</a>", page):
        path, vid, title = m.group(1), m.group(2), _clean(m.group(3))
        url = "https://hqporner.com" + path
        if url in seen:
            continue
        seen.add(url)
        r = {"title": title or f"HQPorner video {vid}", "url": url,
             "snippet": "", "source": "hqporner"}
        if vid in thumbs:
            thumb = thumbs[vid]
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            r["thumbnail"] = thumb
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def _kvs_alt_search(search_url: str, link_re: str, max_results: int,
                    base: str, source: str) -> list:
    """KVS-family parser for tubes whose video links carry no title attr:
    title comes from the img alt text, thumb from img data-src or src
    (relative thumbs get the base prepended)."""
    try:
        page = _fetch(search_url)
    except Exception:
        return []
    results = []
    seen = set()
    for m in re.finditer(link_re, page):
        raw_url = html.unescape(m.group(1))
        url = raw_url if raw_url.startswith("http") else base + raw_url
        if url in seen:
            continue
        seen.add(url)
        chunk = page[m.end():m.end() + 900]
        # The thumbnail img is inside the link; stop at its close tag so a
        # hero/nav link can't steal the next card's image.
        close = chunk.find("</a>")
        if close != -1:
            chunk = chunk[:close]
        thumb = ""
        for attr in ("data-original", "data-src"):
            tm = re.search(attr + r'="(//[^"]+|https?://[^"]+)"', chunk)
            if tm:
                thumb = tm.group(1)
                break
        if not thumb:
            tm = re.search(r'<img[^>]+src="(https?://[^"]+|/[^"]+)"', chunk)
            if tm:
                thumb = tm.group(1)
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        elif thumb.startswith("/"):
            thumb = base + thumb
        title = ""
        am = re.search(r'alt="([^"]+)"', chunk)
        if am:
            title = _clean(am.group(1))
        if not title:
            slug = url.rstrip("/").split("/")[-1]
            title = slug.replace("-", " ").replace("_", " ").title()
        r = {"title": title, "url": url, "snippet": "", "source": source}
        if thumb and not thumb.endswith(".gif"):
            r["thumbnail"] = thumb
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def xnxx_search(query: str, max_results: int = 10) -> list:
    """XNXX search. One data-src thumb and one title-link per video, in
    the same page order, so the two lists are zipped."""
    url = "https://www.xnxx.com/search/" + urllib.parse.quote(query)
    try:
        page = _fetch(url)
    except Exception:
        return []
    links = re.findall(
        r'href="(/video-[a-z0-9]+/[^"]+)" title="([^"]+)"', page)
    thumbs = re.findall(r'data-src="(https://[^"]+)"', page)
    results = []
    seen = set()
    for i, (path, title) in enumerate(links):
        url = "https://www.xnxx.com" + path
        if url in seen:
            continue
        seen.add(url)
        r = {"title": _clean(title), "url": url, "snippet": "",
             "source": "xnxx"}
        if i < len(thumbs):
            r["thumbnail"] = thumbs[i]
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def porntrex_search(query: str, max_results: int = 10) -> list:
    """PornTrex search (KVS-family, absolute video URLs)."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    return _kvs_alt_search(
        f"https://www.porntrex.com/search/{q}/",
        r'href="(https://www\.porntrex\.com/video/\d+/[^"]+)"',
        max_results, "https://www.porntrex.com", "porntrex")


def youjizz_search(query: str, max_results: int = 10) -> list:
    """YouJizz search. Video links are /videos/<slug>-<id>.html; titles
    live in the agego-content-title link; thumbs are data-original."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://www.youjizz.com/search/{q}.html"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    seen = set()
    for m in re.finditer(
            r'<a[^>]*class="frame video" href="(/videos/[^"]+\.html)"', page):
        path = m.group(1)
        url = "https://www.youjizz.com" + path
        if url in seen:
            continue
        seen.add(url)
        chunk = page[m.end():m.end() + 1200]
        tm = re.search(r'data-original="(//[^"]+|https?://[^"]+)"', chunk)
        title = ""
        t = re.search(r'class="agego-content-title">([^<]+)</a>', chunk)
        if t:
            title = _clean(t.group(1))
        if not title:
            slug = path.split("/")[-1].rsplit("-", 1)[0]
            title = slug.replace("-", " ").replace("%21", "!").title()
        r = {"title": title, "url": url, "snippet": "",
             "source": "youjizz"}
        if tm:
            thumb = tm.group(1)
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            r["thumbnail"] = thumb
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def okxxx_search(query: str, max_results: int = 10) -> list:
    """OK.xxx search (KVS platform). Video URLs are /video/<id>/ with a
    title attr; thumbnails are lazy-loaded (data-original)."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://ok.xxx/search/{q}/"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(/video/\d+/)" title="([^"]+)"',
        r'data-original="(https?://[^"]+)"',
        max_results, "https://ok.xxx", "okxxx", window=1200)


def porn00_search(query: str, max_results: int = 10) -> list:
    """Porn00 search (KVS platform). Search redirects to /q/<query>/."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://www.porn00.org/search/{q}/"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(https://www\.porn00\.org/video/[^"]+/)" title="([^"]+)"',
        r'data-original="(https?://[^"]+)"',
        max_results, "", "porn00", window=1200)


def yespornvip_search(query: str, max_results: int = 10) -> list:
    """YesPornVip search (KVS platform)."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://yesporn.vip/search/{q}/"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(https://yesporn\.vip/video/\d+/[^"]+/)" title="([^"]+)"',
        r'(?:data-original|src)="(https?://[^"]+)"',
        max_results, "", "yespornvip", window=1200)


def justporn_search(query: str, max_results: int = 10) -> list:
    """JustPorn search (KVS platform)."""
    params = urllib.parse.urlencode({"q": query})
    url = f"https://www.justporn.com/search/?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(https://www\.justporn\.com/video/\d+/[^"]+/)" title="([^"]+)"',
        r'(?:data-original|src)="(https?://[^"]+)"',
        max_results, "", "justporn", window=1200)


def pornmike_search(query: str, max_results: int = 10) -> list:
    """PornMike search. Video links are /videos/<slug>-<id>/; titles from
    img alt; thumbnails are relative /images/... paths."""
    params = urllib.parse.urlencode({"q": query})
    return _kvs_alt_search(
        f"https://pornmike.com/search/?{params}",
        r'href="(/videos/[^"]+/)"',
        max_results, "https://pornmike.com", "pornmike")


def megatube_search(query: str, max_results: int = 10) -> list:
    """MegaTube search (KVS platform)."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://www.megatube.xxx/search/{q}/"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(https://www\.megatube\.xxx/videos/\d+/[^"]+/)" title="([^"]+)"',
        r'<img[^>]+src="(https?://[^"]+)"',
        max_results, "", "megatube", window=1200)


def _porndig_family_search(query: str, max_results: int, base: str,
                           source: str) -> list:
    """Shared parser for PornDig/Tukif (same platform). Video links are
    /videos/<id>/<slug>.html with the title in the link's alt attr;
    thumbnails are data-src on the following img (page carries an 18+
    overlay, but results and thumbs are in the HTML)."""
    url = f"{base}/videos/s={urllib.parse.quote_plus(query)}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(/videos/\d+/[^"]+\.html)" alt="([^"]+)"',
        r'data-src="(https://[^"]+)"',
        max_results, base, source, window=1500)


def porndig_search(query: str, max_results: int = 10) -> list:
    """PornDig search."""
    return _porndig_family_search(query, max_results,
                                  "https://www.porndig.com", "porndig")


def tukif_search(query: str, max_results: int = 10) -> list:
    """Tukif search (French PornDig sister site)."""
    return _porndig_family_search(query, max_results,
                                  "https://tukif.porn", "tukif")


def watchmygf_search(query: str, max_results: int = 10) -> list:
    """WatchMyGF search (amateur tube, KVS-family). Video links are
    /video/<slug>.html; titles from img alt; thumbs are data-src."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    return _kvs_alt_search(
        f"https://www.watchmygf.me/search/{q}/",
        r'href="(https://www\.watchmygf\.me/video/[^"]+\.html)"',
        max_results, "https://www.watchmygf.me", "watchmygf")


def porngo_search(query: str, max_results: int = 10) -> list:
    """PornGo search (KVS-family). Video links are /videos/<id>/<slug>/;
    titles from img alt; thumbs are img src."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    return _kvs_alt_search(
        f"https://www.porngo.com/search/{q}/",
        r'href="(https://www\.porngo\.com/videos/\d+/[^"]+/)"',
        max_results, "https://www.porngo.com", "porngo")


def jizzbunker_search(query: str, max_results: int = 10) -> list:
    """JizzBunker search. Video links are /<id>/<slug>.html; titles from
    img alt / video-card__title; thumbs are img src."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    return _kvs_alt_search(
        f"https://jizzbunker.com/search/{q}/",
        r'href="(https://jizzbunker\.com/\d+/[^"]+\.html)"',
        max_results, "https://jizzbunker.com", "jizzbunker")


def pornlib_search(query: str, max_results: int = 10) -> list:
    """PornLib search. Video links are /video/<slug>-<id>; titles from
    img alt; thumbs are img src."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    return _kvs_alt_search(
        f"https://www.pornlib.com/search/videos/{q}",
        r'href="(/video/[^"]+)"',
        max_results, "https://www.pornlib.com", "pornlib")


def pornq_search(query: str, max_results: int = 10) -> list:
    """PornQ search. Video links are /video/<id>/<slug>/; titles from
    img alt; thumbs are lazy-loaded (data-original)."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    return _kvs_alt_search(
        f"https://pornq.com/search/{q}/",
        r'href="(/video/\d+/[^"]+/)"',
        max_results, "https://pornq.com", "pornq")


def hentaihaven_search(query: str, max_results: int = 10) -> list:
    """HentaiHaven search (hentai streaming). Video links are
    /watch/<slug>/; titles from img alt; thumbs are img src."""
    params = urllib.parse.urlencode({"s": query})
    return _kvs_alt_search(
        f"https://hentaihaven.xxx/?{params}",
        r'href="(/watch/[^"]+/)"',
        max_results, "https://hentaihaven.xxx", "hentaihaven")


def porntop_search(query: str, max_results: int = 10) -> list:
    """PornTop search (KVS platform). Video links are /video/<id>/<slug>/
    with a title attr; thumbnails are lazy-loaded (data-original)."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://porntop.com/search/{q}/"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(/video/\d+/[^"]+/)" title="([^"]+)"',
        r'(?:data-original|src)="(https?://[^"]+)"',
        max_results, "https://porntop.com", "porntop", window=1200)


def zbporn_search(query: str, max_results: int = 10) -> list:
    """ZbPorn search (KVS platform). Video links are
    /videos/<id>/<slug>/ with a title attr; thumbnails are img src."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://zbporn.com/search/{q}/"
    try:
        page = _fetch(url)
    except Exception:
        return []
    return _pair_thumbs(
        page,
        r'href="(https://zbporn\.com/videos/\d+/[^"]+/)" title="([^"]+)"',
        r'(?:data-original|src)="(https?://[^"]+)"',
        max_results, "", "zbporn", window=1500)


def sexu_search(query: str, max_results: int = 10) -> list:
    """Sexu search. Video links are bare /<id>/; titles from img alt;
    thumbs are img src (protocol-relative)."""
    params = urllib.parse.urlencode({"q": query})
    return _kvs_alt_search(
        f"https://sexu.com/search?{params}",
        r'href="(/\d+/)"',
        max_results, "https://sexu.com", "sexu")


def sunporno_search(query: str, max_results: int = 10) -> list:
    """SunPorno search via the tag page (/tags/<query>/). Video links are
    /v/<id>/<slug>/ with a title attr (prefixed 'Porn Videos', stripped);
    thumbnails are img src."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://www.sunporno.com/tags/{q}/"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = _pair_thumbs(
        page,
        r'href="(https://www\.sunporno\.com/v/\d+/[^"]+/)" title="([^"]+)"',
        r'(?:data-original|src)="(https?://[^"]+)"',
        max_results, "", "sunporno", window=1200)
    for r in results:
        r["title"] = re.sub(r"^Porn Videos\s*", "", r["title"])
    return results


def pornheed_search(query: str, max_results: int = 10) -> list:
    """PornHeed search via its AJAX endpoint (POST searchterm).
    Video links are /video/<id>/<slug>; titles from img alt; thumbs are
    img data-src or src."""
    data = urllib.parse.urlencode({"searchterm": query}).encode()
    try:
        page = _fetch(
            "https://www.pornheed.com/ajax/searchengine.php",
            headers={"Content-Type":
                     "application/x-www-form-urlencoded",
                     "X-Requested-With": "XMLHttpRequest",
                     "Referer": "https://www.pornheed.com/"},
            data=data)
    except Exception:
        return []
    results = []
    seen = set()
    for m in re.finditer(r'href="(/video/\d+/[^"]*)"', page):
        path = m.group(1)
        url = "https://www.pornheed.com" + path
        if url in seen:
            continue
        seen.add(url)
        chunk = page[m.end():m.end() + 900]
        close = chunk.find("</a>")
        if close != -1:
            chunk = chunk[:close]
        thumb = ""
        for attr in ("data-original", "data-src"):
            tm = re.search(attr + r'="(//[^"]+|https?://[^"]+)"', chunk)
            if tm:
                thumb = tm.group(1)
                break
        if not thumb:
            tm = re.search(r'<img[^>]+src="(https?://[^"]+|/[^"]+)"', chunk)
            if tm:
                thumb = tm.group(1)
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        elif thumb.startswith("/"):
            thumb = "https://www.pornheed.com" + thumb
        title = ""
        am = re.search(r'alt="([^"]+)"', chunk)
        if am:
            title = _clean(am.group(1))
        if not title:
            slug = path.rstrip("/").split("/")[-1]
            title = slug.replace("-", " ").title()
        r = {"title": title, "url": url, "snippet": "",
             "source": "pornheed"}
        if thumb:
            r["thumbnail"] = thumb
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def nudevista_search(query: str, max_results: int = 10) -> list:
    """NudeVista — meta-search across tube sites (35M+ indexed clips).
    The name link carries the thumbnail img (alt = title)."""
    params = urllib.parse.urlencode({"q": query})
    url = f"https://www.nudevista.com/?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    seen = set()
    for m in re.finditer(
            r'<a href="(https://video\.nudevista\.com/video/[^"]+)" '
            r'class="name"[^>]*><img[^>]+src="(//[^"]+)"[^>]*alt="([^"]+)"',
            page):
        video_url, thumb, title = m.group(1), m.group(2), _clean(m.group(3))
        if video_url in seen:
            continue
        seen.add(video_url)
        results.append({"title": title, "url": video_url, "snippet": "",
                        "source": "nudevista",
                        "thumbnail": "https:" + thumb})
        if len(results) >= max_results:
            break
    return results



# ------------------------------------------- generic tube engines ---
# 91 tube sites verified 2026-11-21 from ThePornDude directory
# (mass probe + auto-detection of link/title/thumb patterns).
# One parser handles all of them: title from the link/img title
# attr, else img alt, else h2/h3 text, else the URL slug;
# thumbnail from data-original / data-src / data-front-lazy-src /
# src (absolute or relative, base prepended). Video previews
# (.mp4) and placeholders are skipped.

_TUBE_LINK_SHAPES = {
    # Bare IDs (/video/12345/) and query strings (?Li=...) both occur,
    # so the tail after the ID is optional and may contain "?".
    "video_id": r"/video/\d+/[^\"#]*",
    "videos_id": r"/videos/\d+/[^\"#]*",
    "v_id": r"/v/\d+/[^\"#]*",
    "watch": r"/watch/[^\"#]+/",
    "video_html": r"/video/[^\"#]+\.html",
    "id_html": r"/\d+/[^\"#]+\.html",
    "leak_video": r"/leak/video/\d+/[^\"#]*",
}

_TUBE_SITES = [
    # (key, base, search path template, link shape, relative links)
    ("3dhentai", "https://3dhentai.it.com", "/?s={qq}", "video_id", False),  # 3DHentai.it.com
    ("alrincon", "https://alrincon.com", "/?s={qq}", "video_id", False),  # AlRincon
    ("anysex", "https://anysex.com", "/search?q={qq}", "video_id", False),  # AnySex
    ("avhwa", "https://avhwa.com", "/search/{qh}/", "video_id", False),  # AVHWA
    ("baddies247", "https://baddies247.com", "/search/{qh}/", "videos_id", False),  # Baddies247
    ("baddiesonly", "https://baddiesonly.tv", "/search/{qh}/", "videos_id", False),  # BaddiesOnly
    ("bdsmstreak", "https://bdsmstreak.com", "/search?q={qq}", "video_id", True),  # BDSMstreak
    ("brazz", "https://brazz.org", "/search/{qh}/", "video_id", False),  # Brazz
    ("bukkake", "https://bukkake.to", "/search/{qh}/", "videos_id", False),  # Bukkake.to
    ("camcaps", "https://camcaps.tv", "/search/videos?search_query={qq}", "video_id", False),  # CamCaps.tv
    ("camstreams", "https://camstreams.tv", "/search/{qh}/", "videos_id", False),  # CamStreams
    ("camwhoresbay", "https://camwhoresbay.com", "/search/{qh}/", "videos_id", False),  # CamWhoresBay
    ("cloudbate", "https://cloudbate.com", "/search/{qh}/", "video_id", False),  # CloudBate
    ("desisex2", "https://desisex2.com", "/search/{qh}/", "video_id", False),  # DesiSex2
    ("dirtyshack", "https://dirtyshack.com", "/search/{qh}/", "video_html", False),  # DirtyShack
    ("djatoya", "https://djatoya.com", "/search/{qh}/", "video_id", False),  # Djatoya
    ("extremescatporn", "https://extremescatporn.com", "/search/{qh}/", "video_id", False),  # Extreme Scat Porn
    ("fapgoat", "https://fapgoat.com", "/search/{qh}/", "video_id", False),  # FapGoat
    ("fapnado", "https://fapnado.com", "/search/{qh}/", "videos_id", False),  # Fapnado
    ("fapnfuck", "https://fapnfuck.com", "/search/{qh}/", "video_id", True),  # FapNFuck
    ("femdomvc", "https://femdomvc.com", "/search/{qh}/", "video_id", False),  # FemdomVC
    ("femefun", "https://femefun.com", "/search/{qh}/", "videos_id", False),  # FemeFun
    ("freescatporn", "https://freescatporn.com", "/search/{qh}/", "video_html", False),  # FreeScatPorn
    ("freeuseporn", "https://freeuseporn.com", "/search/videos?search_query={qq}", "video_id", True),  # FreeUsePorn
    ("giantess", "https://giantess.porn", "/search/{qh}/", "videos_id", False),  # Giantess Porn
    ("hcbdsm", "https://hcbdsm.com", "/search/{qh}/", "video_id", True),  # hcBDSM
    ("heavyr", "https://heavy-r.com", "/search/{qh}/", "video_id", True),  # Heavy R
    ("hentaicity", "https://hentaicity.it.com", "/?s={qq}", "video_id", False),  # HentaiCity.it.com
    ("hentaicloud", "https://hentaicloud.com", "/?s={qq}", "video_id", True),  # HentaiCloud
    ("hentaimoon", "https://hentai-moon.com", "/search/{qh}/", "videos_id", False),  # Hentai Moon
    ("hentaisea", "https://hentaisea.com", "/search/videos?search_query={qq}", "watch", False),  # HentaiSea
    ("hentaiyes", "https://hentaiyes.com", "/search?q={qq}", "watch", True),  # HentaiYes
    ("herexxx", "https://herexxx.com", "/search/{qh}/", "video_id", False),  # HereXXX
    ("hypnotube", "https://hypnotube.com", "/?s={qq}", "video_html", False),  # HypnoTube
    ("japanwhores", "https://japan-whores.com", "/search/{qh}/", "videos_id", False),  # Japan Whores
    ("javbangers", "https://javbangers.com", "/search/{qh}/", "video_id", False),  # JavBangers
    ("javhdtube", "https://javhdtube.com", "/search/{qh}/", "video_id", False),  # JAVHDTube
    ("javtiful", "https://javtiful.com", "/search?q={qq}", "video_id", True),  # Javtiful
    ("kissjav", "https://kissjav.li", "/search/{qh}/", "video_id", False),  # KissJAV (ShyAV)
    ("mastibaba2", "https://mastibaba2.com", "/search/{qh}/", "video_id", False),  # MastiBaba
    ("morehardporn", "https://morehardporn.com", "/search/{qh}/", "video_id", False),  # MoreHardPorn
    ("mygoodporn", "https://mygoodporn.tv", "/search/{qh}/", "video_id", False),  # MyGoodPorn
    ("mylust", "https://mylust.com", "/search?q={qq}", "videos_id", False),  # MyLust
    ("mythav", "https://mythav.com", "/search/{qh}/", "video_id", False),  # MythAV
    ("mzansiporn", "https://mzansiporn.mobi", "/search/{qh}/", "video_id", True),  # MzansiPorn.mobi
    ("mzansixxx", "https://mzansixxx.com", "/search/{qh}/", "video_id", True),  # MzansiXXX
    ("neporn", "https://neporn.com", "/search/{qh}/", "video_id", False),  # NePorn
    ("netfapx", "https://netfapx.net", "/search/{qh}/", "watch", False),  # NetFapX.net
    ("perfectgirls", "https://perfectgirls.xxx", "/search/{qh}/", "video_id", True),  # Perfect Girls
    ("pervertium", "https://pervertium.com", "/search/{qh}/", "videos_id", False),  # Pervertium
    ("perverttube", "https://perverttube.com", "/search/{qh}/", "video_id", False),  # PervertTube
    ("phun", "https://phun.org", "/?s={qq}", "videos_id", False),  # Phun.org
    ("poo19", "https://poo19.com", "/?s={qq}", "videos_id", False),  # Poo19
    ("poop", "https://poop.rip", "/search/{qh}/", "videos_id", False),  # PoopRIP
    ("porndex", "https://porndex.com", "/?s={qq}", "watch", False),  # Porndex
    ("porninblack", "https://porninblack.com", "/search/{qh}/", "watch", False),  # PornInBlack
    ("pornolandia", "https://pornolandia.xxx", "/?s={qq}", "video_id", False),  # Pornolandia
    ("pornraw", "https://pornraw.com", "/search/{qh}/", "videos_id", False),  # PornRaw
    ("pornve", "https://pornve.com", "/search/{qh}/", "video_id", False),  # PornVe
    ("pornwex", "https://pornwex.tv", "/search/{qh}/", "video_id", False),  # PornWex
    ("punishbang", "https://punishbang.com", "/search/{qh}/", "video_id", False),  # PunishBang
    ("roshy", "https://roshy.tv", "/search/videos?search_query={qq}", "video_id", False),  # Roshy
    ("rule34video", "https://rule34video.com", "/search/{qh}/", "video_id", False),  # Rule34Video
    ("ruleporn", "https://ruleporn.com", "/search/{qh}/", "video_id", False),  # RulePorn
    ("severeporn", "https://severeporn.com", "/search/{qh}/", "video_id", False),  # SeverePorn
    ("sextu", "https://sextu.com", "/?s={qq}", "video_id", True),  # SexTu
    ("sextvx", "https://sextvx.com", "/?s={qq}", "video_id", False),  # SexTVx
    ("shesfreaky", "https://shesfreaky.com", "/search/{qh}/", "video_html", True),  # ShesFreaky
    ("spicymms", "https://spicymms.com", "/search/{qh}/", "video_id", False),  # Spicy MMS
    ("thepornarea", "https://thepornarea.com", "/search/{qh}/", "videos_id", False),  # ThePornArea
    ("tickle", "https://tickle.porn", "/search/{qh}/", "videos_id", False),  # Tickle Porn
    ("tokyomotion", "https://tokyomotion.com", "/search/{qh}/", "video_id", False),  # TokyoMotion
    ("trahkino", "https://trahkino.cc", "/?s={qq}", "video_id", False),  # TrahKino
    ("trash", "https://trash.porn", "/search/{qh}/", "videos_id", False),  # Trash Porn
    ("trendyporn", "https://trendyporn.com", "/search/{qh}/", "video_html", False),  # TrendyPorn
    ("twerkqueens", "https://twerkqueens.com", "/search/{qh}/", "video_html", False),  # Twerk Queens
    ("uncensoredhentai", "https://uncensoredhentai.xxx", "/?s={qq}", "watch", True),  # Uncensored Hentai
    ("usersporn", "https://usersporn.com", "/search/{qh}/", "video_id", False),  # UsersPorn
    ("w1mp", "https://w1mp.com", "/search/{qh}/", "video_id", True),  # W1mp
    ("watchporn", "https://watchporn.to", "/search/{qh}/", "video_id", False),  # WatchPorn
    ("whoreshub", "https://whoreshub.com", "/search/{qh}/", "videos_id", False),  # WhoresHub
    ("xcum", "https://xcum.com", "/?s={qq}", "v_id", False),  # xCum
    ("xfetish", "https://x-fetish.tube", "/search/{qh}/", "video_id", False),  # X Fetish Tube
    ("xpee", "https://xpee.com", "/search/{qh}/", "video_id", False),  # Xpee
    ("xvgold", "https://xvgold.org", "/search/{qh}/", "video_html", True),  # XVGold
    ("xxbrits", "https://xxbrits.com", "/search/{qh}/", "videos_id", False),  # XXBrits
    ("xxx", "https://x-x-x.tube", "/search/{qh}/", "videos_id", False),  # XXX Tube
    ("xxxfiles", "https://xxxfiles.com", "/search/{qh}/", "videos_id", False),  # XXXFiles
    ("youramateurporn", "https://youramateurporn.com", "/search/{qh}/", "video_html", False),  # YourAmateurPorn
    ("zhornyhub", "https://zhornyhub.com", "/?s={qq}", "v_id", False),  # ZhornyHub (WP ?s= filters; /search/videos ignores the query)
    ("3dporndude", "https://3dporndude.com", "/search/{qh}/", "video_id", False),  # 3D Porn Dude (round 2)
    ("3dsexanime", "https://3dsexanime.xyz", "/search/videos?search_query={qq}", "video_id", True),  # 3D Sex Anime (round 2)
    ("allclassic", "https://allclassic.porn", "/search/{qh}/", "videos_id", False),  # AllClassic (round 2)
    ("amateurmasturbations", "https://amateurmasturbations.com", "/search/{qh}/", "video_html", False),  # Amateur Masturbations (round 2)
    ("analegg", "https://analegg.com", "/search/{qh}/", "video_id", False),  # Analegg (round 2)
    ("cuckoldsporn", "https://cuckoldsporn.com", "/search/{qh}/", "videos_id", False),  # CuckoldsPorn (round 2)
    ("findhername", "https://findhername.net", "/search/{qh}/", "video_id", False),  # FindHerName (round 2)
    ("fuqster", "https://fuqster.com", "/search/{qh}/", "video_id", True),  # Fuqster (round 2)
    ("hornyfans", "https://hornyfans.to", "/search/{qh}/", "watch", False),  # HornyFans (round 2)
    ("hornyleak", "https://hornyleak.tv", "/search/{qh}/", "video_id", False),  # HornyLeak (round 2)
    ("hotscopes", "https://hotscopes.net", "/search/videos?search_query={qq}", "video_id", True),  # HotScopes.net (round 2)
    ("hypnosis", "https://hypnosis.porn", "/search/{qh}/", "videos_id", False),  # Hypnosis.porn (round 2)
    ("ibradome", "https://ibradome.com", "/search/videos?search_query={qq}", "leak_video", False),  # Ibradome (round 2)
    ("mzansipornvideos", "https://mzansipornvideos.com", "/search/{qh}/", "video_id", False),  # Mzansi Porn Videos (round 2)
    ("naughtymachinima", "https://naughtymachinima.com", "/search/videos?search_query={qq}", "video_id", True),  # Naughty Machinima (round 2)
    ("notfans", "https://notfans.com", "/search/{qh}/", "videos_id", False),  # NotFans (round 2)
    ("onlyleaks", "https://onlyleaks.me", "/search/{qh}/", "video_id", False),  # OnlyLeaks (round 2)
    ("onlyporn", "https://onlyporn.tube", "/?s={qq}", "video_id", True),  # OnlyPorn (round 2)
    ("porcore", "https://porcore.com", "/?s={qq}", "video_id", True),  # PorCore (round 2)
    ("porn4fans", "https://porn4fans.com", "/search/{qh}/", "video_id", False),  # Porn4Fans (round 2)
    ("reallifecam", "https://reallifecam.to", "/?s={qq}", "video_id", True),  # RealLifeCam.to (round 2)
    ("sheeshfans", "https://sheeshfans.com", "/search/{qh}/", "video_id", False),  # SheeshFans (round 2)
    ("shemale6", "https://shemale6.com", "/search/{qh}/", "videos_id", False),  # Shemale6 (round 2)
    ("spanked", "https://spanked.to", "/search/{qh}/", "videos_id", False),  # Spanked.to (round 2)
    ("tabooporntv", "https://tabooporn.tv", "/search/{qh}/", "videos_id", False),  # TabooPorn.tv (round 2)
    ("taboopornto", "https://tabooporn.to", "/search/{qh}/", "video_id", False),  # TabooPorn.to (round 2)
    ("thothd", "https://thothd.com", "/search/{qh}/", "videos_id", False),  # ThotHD (round 2)
    ("thothub", "https://thothub.to", "/search/{qh}/", "videos_id", False),  # ThotHub (round 2)
    ("thots", "https://thots.tv", "/search/{qh}/", "video_id", False),  # Thots.tv (round 2)
    ("transtube", "https://transtube.tv", "/search/{qh}/", "videos_id", False),  # TransTube (round 2)
    ("transvids", "https://transvids.tv", "/search/{qh}/", "videos_id", False),  # TransVids (round 2)
    ("tsthots", "https://tsthots.com", "/search/{qh}/", "video_id", False),  # TsThots (TS) (round 2)
    ("ultrathots", "https://ultrathots.com", "/search/{qh}/", "videos_id", False),  # UltraThots (round 2)
    ("viralxxxporn", "https://viralxxxporn.com", "/search/{qh}/", "video_id", False),  # ViralXXXPorn (round 2)
    ("xbondage", "https://xbondage.porn", "/search/{qh}/", "videos_id", False),  # XBondage (round 2)
    ("xtg", "https://x-tg.tube", "/search/{qh}/", "video_id", False),  # X-TG (TS) (round 2)
    ("xvideo", "https://x-video.tube", "/search/{qh}/", "video_id", False),  # X-Video.tube (round 2)
    ("xxthots", "https://xxthots.com", "/search/{qh}/", "video_id", False),  # XXTHOTS (round 2)
    ("zzcartoon", "https://zzcartoon.com", "/?s={qq}", "video_html", False),  # ZZCartoon (round 2)
]


def _junk_title(t: str) -> bool:
    """True for title candidates that carry no real title (placeholder alt
    text, view/like counts from a nested span's title attr)."""
    if not t:
        return True
    tl = t.strip().lower()
    if tl in ("video", "play", "thumbnail", "image", "img", "poster",
              "like", "share", "report", "download", "watch later",
              "favorites", "add to favorites", "remove from favorites"):
        return True
    if tl.startswith(("add to ", "remove from ")):
        return True
    if re.match(r"^[\d,.]+\s+(views?|likes?)$", tl):
        return True
    return False


def _generic_tube_search(cfg, query: str,
                         max_results: int = 10) -> list:
    """One parser for the _TUBE_SITES table."""
    key, base, path_tmpl, shape, _rel = cfg
    qh = urllib.parse.quote(query.replace(" ", "-"))
    qq = urllib.parse.quote(query)
    url = base + path_tmpl.replace("{qh}", qh).replace("{qq}", qq)
    try:
        page = _fetch(url)
    except Exception:
        return []
    shape_re = _TUBE_LINK_SHAPES[shape]
    # Optional 2-letter locale prefix (e.g. /en/video/...) occurs on
    # some sites (sextu redirects to fullvideosporn.com/en/).
    link_re = (r'href="((?:https?://[^"/]+)?(?:/[a-z]{2})?'
               + shape_re + r')"')
    results = []
    seen = set()
    for m in re.finditer(link_re, page):
        raw_url = html.unescape(m.group(1))
        if any(s in raw_url for s in ("search", "categor", "/tag/")):
            continue
        url = raw_url if raw_url.startswith("http") else base + raw_url
        if url in seen:
            continue
        seen.add(url)
        chunk = page[m.end():m.end() + 3000]
        close = chunk.find("</a>")
        if close != -1:
            chunk = chunk[:close]
        title = ""
        for pat in (r'title="([^"]{3,})"', r'alt="([^"]{3,})"',
                    r"<h[23][^>]*>([^<]{3,})</h[23]>"):
            tm = re.search(pat, chunk)
            if not tm:
                continue
            cand = _clean(tm.group(1))
            cand = re.sub(r"^Thumbnail for ", "", cand)
            if _junk_title(cand):
                continue
            title = cand
            break
        if not title:
            slug = url.rstrip("/").split("/")[-1]
            slug = re.sub(r"\.html?$", "", slug)
            slug = slug.split("?")[0]
            title = slug.replace("-", " ").replace("_", " ").title()
            if title.replace(" ", "").isdigit():
                title = f"{key} video {slug}"
        thumb = ""
        for attr in ("data-original", "data-src",
                     "data-front-lazy-src"):
            tm2 = re.search(attr + r'="(//[^"]+|https?://[^"]+|/[^"]+)"',
                            chunk)
            if tm2 and not tm2.group(1).lower().endswith(
                    (".mp4", ".webm", ".mov")):
                thumb = tm2.group(1)
                break
        if not thumb:
            # <picture><source data-srcset="url 234w, ..."> (roshy et al):
            # take the first URL of the set.
            ss = re.search(r'(?:data-srcset|srcset)="(https?://[^" ]+)',
                           chunk)
            if ss:
                thumb = ss.group(1)
        if not thumb:
            sm = re.search(r'<img[^>]+src="(//[^"]+|https?://[^"]+|/[^"]+)"',
                           chunk)
            if sm and "placeholder" not in sm.group(1).lower():
                thumb = sm.group(1)
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        elif thumb.startswith("/"):
            thumb = base + thumb
        r = {"title": title, "url": url, "snippet": "",
             "source": key}
        if thumb and not thumb.lower().endswith(".gif"):
            r["thumbnail"] = thumb
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def _make_tube_engine(cfg):
    def _search(query: str, max_results: int = 10) -> list:
        return _generic_tube_search(cfg, query, max_results)
    return _search


def xmissy_search(query: str, max_results: int = 10) -> list:
    """xMissy — Dutch adult BLOG (not a tube): search returns blog
    posts (/item/<id>/<slug>) with the title as link text. No
    thumbnails on the cards."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://xmissy.nl/search/{q}/"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    seen = set()
    for m in re.finditer(
            r'<a href="(/item/\d+/[^\"]+)">\s*([^<]+?)\s*</a>', page):
        path, title = m.group(1), _clean(m.group(2))
        url = "https://xmissy.nl" + path
        if url in seen or len(title) < 3:
            continue
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": "",
                        "source": "xmissy"})
        if len(results) >= max_results:
            break
    return results


def mmtv7_search(query: str, max_results: int = 10) -> list:
    """7MMTV (JAV). Video links are /en/<type>_content/<id>/<slug>.html;
    markup uses SINGLE quotes, so the generic parser (double-quote
    regexes) cannot read it. Title from img alt or the h3 link text;
    thumb from img src/data-src (single-quoted)."""
    q = urllib.parse.quote(query.replace(" ", "-"))
    url = f"https://7mmtv.sx/search/{q}/"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    seen = set()
    for m in re.finditer(
            r'href="(https://7mmtv\.sx/en/[a-z_]+_content/\d+/[^\"]+\.html)"',
            page):
        url = m.group(1)
        if url in seen:
            continue
        seen.add(url)
        chunk = page[m.end():m.end() + 1500]
        title = ""
        am = re.search(r"alt='([^']{3,})'", chunk)
        if am:
            title = _clean(am.group(1))
        if not title:
            hm = re.search(r"class='video-title'>\s*<a[^>]*>([^<]+)</a>",
                           chunk)
            if hm:
                title = _clean(hm.group(1))
        if not title:
            slug = url.rstrip("/").split("/")[-1]
            title = re.sub(r"\.html?$", "", slug).replace("-", " ")
        thumb = ""
        for attr in ("data-src", "src"):
            tm = re.search(attr + r"='(https?://[^']+)'", chunk)
            if tm and not tm.group(1).lower().endswith(".mp4"):
                thumb = tm.group(1)
                break
        r = {"title": title, "url": url, "snippet": "",
             "source": "7mmtv"}
        if thumb:
            r["thumbnail"] = thumb
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def _wp_blog_search(base: str, query: str, max_results: int,
                    source: str) -> list:
    """Shared parser for WordPress adult blogs (DampLips, SexyAndFunny).
    Search is ?s=<query>; results are <article> blocks with an h2/h3
    title link and (usually) a featured image."""
    params = urllib.parse.urlencode({"s": query})
    url = f"{base}/?{params}"
    try:
        page = _fetch(url)
    except Exception:
        return []
    results = []
    seen = set()
    for article in re.findall(r"<article[^>]*>(.*?)</article>", page,
                              re.DOTALL):
        tm = re.search(
            r'<h[23][^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
            article, re.DOTALL)
        if not tm:
            continue
        post_url, title = tm.group(1), _clean(tm.group(2))
        if post_url in seen or len(title) < 3:
            continue
        seen.add(post_url)
        r = {"title": title, "url": post_url, "snippet": "",
             "source": source}
        im = re.search(r'<img[^>]+src="(https?://[^"]+)"', article)
        if im and "data:" not in im.group(1):
            r["thumbnail"] = im.group(1)
        results.append(r)
        if len(results) >= max_results:
            break
    return results


def damplips_search(query: str, max_results: int = 10) -> list:
    """DampLips — adult blog (WordPress). Posts are /p/<id>/."""
    return _wp_blog_search("https://www.damplips.com", query,
                           max_results, "damplips")


def sexyandfunny_search(query: str, max_results: int = 10) -> list:
    """SexyAndFunny — adult blog (WordPress/Elementor). Posts are
    /video/<slug>/ with featured-image thumbnails."""
    return _wp_blog_search("https://sexyandfunny.com", query,
                           max_results, "sexyandfunny")


# NOTE: cake.ski and alohatube.com were tested and DROPPED 2026-11-21 —
# both ignore the search query and always return the same "newest/top"
# videos list (verified: 5/5 identical URLs for two different queries).
# CamWhores (camwhores.tv) was also dropped — its search returns photo
# albums, not videos.

ENGINES = {
    "duckduckgo": ddg_search,
    "bing": bing_search,
    "reddit": reddit_search,
    "scholar": scholar_search,
    "pubmed": pubmed_search,
    "arxiv": arxiv_search,
    "googlenews": googlenews_search,
    "bingnews": bingnews_search,
    "youtube": youtube_search,
    "vimeo": vimeo_search,
    "dailymotion": dailymotion_search,
    "bing_video": bing_video_search,
    "peertube": peertube_search,
    "archive_org": archive_org_search,
    "odysee": odysee_search,
    "ddg_images": ddg_image_search,
    "bing_images": bing_image_search,
    "pornhub": pornhub_search,
    "xvideos": xvideos_search,
    "xhamster": xhamster_search,
    "redtube": redtube_search,
    "thisvid": thisvid_search,
    "drtuber": drtuber_search,
    "nuvid": nuvid_search,
    "porncom": porncom_search,
    "pornhat": pornhat_search,
    "hqporner": hqporner_search,
    "xnxx": xnxx_search,
    "porntrex": porntrex_search,
    "youjizz": youjizz_search,
    "okxxx": okxxx_search,
    "porn00": porn00_search,
    "yespornvip": yespornvip_search,
    "justporn": justporn_search,
    "pornmike": pornmike_search,
    "megatube": megatube_search,
    "porndig": porndig_search,
    "tukif": tukif_search,
    "nudevista": nudevista_search,
    "watchmygf": watchmygf_search,
    "porngo": porngo_search,
    "jizzbunker": jizzbunker_search,
    "pornlib": pornlib_search,
    "pornq": pornq_search,
    "hentaihaven": hentaihaven_search,
    "porntop": porntop_search,
    "zbporn": zbporn_search,
    "sexu": sexu_search,
    "sunporno": sunporno_search,
    "pornheed": pornheed_search,
    "xmissy": xmissy_search,
    "7mmtv": mmtv7_search,
    "damplips": damplips_search,
    "sexyandfunny": sexyandfunny_search,
    "marginalia": marginalia_search,
    "wiby": wiby_search,
    "mojeek": mojeek_search,
}

# Content-type groupings for the UI.
VIDEO_ENGINES = ["youtube", "vimeo", "dailymotion", "bing_video",
                 "peertube", "archive_org", "odysee"]
IMAGE_ENGINES = ["bing_images", "ddg_images"]
WEB_ENGINES = ["duckduckgo", "bing"]
ACADEMIC_ENGINES = ["scholar", "pubmed", "arxiv"]
NEWS_ENGINES = ["googlenews", "bingnews"]
SOCIAL_ENGINES = ["reddit"]
ADULT_ENGINES = ["pornhub", "xvideos", "xhamster", "thisvid", "drtuber",
                 "nuvid", "porncom", "pornhat", "hqporner", "redtube",
                 "xnxx", "porntrex", "youjizz", "okxxx", "porn00",
                 "yespornvip", "justporn", "pornmike", "megatube",
                 "porndig", "tukif", "nudevista", "watchmygf", "porngo",
                 "jizzbunker", "pornlib", "pornq", "hentaihaven",
                 "porntop", "zbporn", "sexu", "sunporno", "pornheed",
                 "xmissy", "7mmtv", "damplips",
                 "sexyandfunny"]

# Config-driven generic tube engines (see _TUBE_SITES above).
for _cfg in _TUBE_SITES:
    ENGINES[_cfg[0]] = _make_tube_engine(_cfg)
    ADULT_ENGINES.append(_cfg[0])
OBSCURE_ENGINES = ["marginalia", "wiby", "mojeek"]


def _score(result: dict, query_words: list) -> float:
    """Simple relevance score: query words in title count most, then snippet."""
    title = result.get("title", "").lower()
    snippet = result.get("snippet", "").lower()
    url = result.get("url", "").lower()
    score = 0.0
    for word in query_words:
        if len(word) < 3:
            continue
        if word in title:
            score += 3.0
        if word in snippet:
            score += 1.0
        if word in url:
            score += 0.5
    score += result.get("_engine_count", 1) * 0.5
    return score


def search_all(query: str, engines: list = None,
               max_per_engine: int = 10) -> list:
    """Search all engines in parallel, deduplicate, rank."""
    if engines is None:
        engines = list(ENGINES.keys())
    query_words = query.lower().split()
    all_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(engines)) as ex:
        futures = {ex.submit(ENGINES[e], query, max_per_engine): e
                   for e in engines if e in ENGINES}
        for fut in concurrent.futures.as_completed(futures):
            try:
                all_results.extend(fut.result())
            except Exception:
                pass
    # Deduplicate by URL, counting how many engines found it. Drop
    # empty-URL entries (failed engines return [] now, but stay safe).
    seen = {}
    for r in all_results:
        url = r.get("url", "")
        if not url:
            continue
        if url in seen:
            seen[url]["_engine_count"] += 1
            if len(r.get("snippet", "")) > len(seen[url].get("snippet", "")):
                seen[url]["snippet"] = r["snippet"]
            if not seen[url].get("thumbnail") and r.get("thumbnail"):
                seen[url]["thumbnail"] = r["thumbnail"]
            continue
        r["_engine_count"] = 1
        seen[url] = r
    results = list(seen.values())
    for r in results:
        r["_score"] = _score(r, query_words)
    results.sort(key=lambda r: r["_score"], reverse=True)
    for r in results:
        r.pop("_score", None)
        r.pop("_engine_count", None)
    return results


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "psychology of desire"
    for r in search_all(q):
        print(f"[{r['source']}] {r['title']}")
        print(f"  {r['url']}")
        if r.get("thumbnail"):
            print(f"  thumb: {r['thumbnail'][:90]}")
        if r["snippet"]:
            print(f"  {r['snippet'][:150]}")
        print()
