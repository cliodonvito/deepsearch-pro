#!/usr/bin/env python3
"""DeepSearch Pro forum backup sync — runs on the home server (cron, 5 min).

Pulls the public /forum/feed.json from the live service and commits any
new/changed posts to forum/posts.json in the cliodonvito/deepsearch-pro
repo. That file is the durable store: on a fresh database the app
restores posts from it, so a Render restart/redeploy no longer eats
the forum.

The repo is public and the feed is public — no private data moves here.
Auth is the repo PAT (contents:write), read from
~/.config/github/credentials.json (mode 600) on this server. Never
print the token. Exit 0 = in sync or synced; 1 = feed unreachable
(try again next run); 2 = GitHub error.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

FEED_URL = "https://deepsearch-pro.onrender.com/forum/feed.json"
REPO = "cliodonvito/deepsearch-pro"
FILE_PATH = "forum/posts.json"
API_URL = f"https://api.github.com/repos/{REPO}/contents/{FILE_PATH}"
TIMEOUT = 120  # Render free tier sleeps; first request can take ~60 s


def log(msg):
    print(msg, flush=True)


def load_pat():
    path = os.path.expanduser("~/.config/github/credentials.json")
    with open(path, encoding="utf-8") as f:
        creds = json.load(f)
    pat = creds.get("pat", "")
    if not pat:
        raise SystemExit("no PAT in ~/.config/github/credentials.json")
    return pat


def fetch_feed():
    req = urllib.request.Request(FEED_URL, headers={
        "User-Agent": "DeepSearchPro-forum-sync/1.0"})
    last_err = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — log and retry once
            last_err = e
            log(f"feed fetch attempt {attempt + 1} failed: {e}")
    raise SystemExit(f"feed unreachable: {last_err}")


def gh(pat, method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "DeepSearchPro-forum-sync/1.0",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise SystemExit(f"GitHub {method} {url} -> {e.code}: {body}")


def canonical(doc):
    return json.dumps(doc, sort_keys=True, ensure_ascii=False)


def main():
    pat = load_pat()
    feed = fetch_feed()
    posts = feed.get("posts", []) if isinstance(feed, dict) else []
    new_doc = {"version": 1, "posts": posts}

    status, current = gh(pat, "GET", API_URL)
    if status == 200:
        sha = current.get("sha")
        old_raw = base64.b64decode(current.get("content", "")).decode(
            "utf-8")
        try:
            old_doc = json.loads(old_raw)
        except json.JSONDecodeError:
            old_doc = None
        if old_doc is not None and canonical(old_doc) == canonical(new_doc):
            log(f"in sync: {len(posts)} post(s), no commit needed")
            return 0
    elif status == 404:
        sha = None
    else:
        raise SystemExit(f"unexpected GitHub GET status {status}")

    content = base64.b64encode(
        (json.dumps(new_doc, indent=1, ensure_ascii=False) + "\n")
        .encode("utf-8")).decode()
    payload = {
        "message": f"forum backup: {len(posts)} post(s)",
        "content": content,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    status, resp = gh(pat, "PUT", API_URL, payload)
    log(f"committed forum backup ({len(posts)} post(s)): "
        f"{resp.get('commit', {}).get('sha', '?')[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
