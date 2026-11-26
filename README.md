# DeepSearch Pro

The Deep Search tool (186 engines) productized as a hosted SaaS.
**Live:** https://deepsearch-pro.onrender.com/ (Render free tier, GitHub:
cliodonvito/deepsearch-pro). Payments: Payzum (see PAYZUM_SETUP.md).

## New in this update (2026-11-26, v1.4)

- **Learning video layer (`/video`, `POST /api/v1/video`)** — paste a
  YouTube URL, get the full timestamped transcript, a summary with
  chapters, and every verifiable claim checked against sources
  (SUPPORTED / CONTRADICTED / UNVERIFIED, each with links and the
  timestamp in the video). $0.10 USDC on Base per video via x402, or
  free with an API-tier key. Jobs are asynchronous: the 202 response
  carries a `job_id`; poll `GET /api/v1/video/jobs/<job_id>`.
  The transcript worker runs on the home server (residential IP —
  YouTube blocks caption fetches from datacenter IPs) and polls a job
  queue over outbound HTTPS; worker requests are signed with Ed25519
  (`video_auth.py`, `ed25519.py`). The home server is never exposed.
  **Ad-free playback is explicitly out of scope** — the page says so
  plainly and points at Firefox + uBlock Origin or YouTube Premium.
  Transcript via yt-dlp captions (faster-whisper fallback), summary +
  claims via local llama3.1:8b (Ollama), verification search via this
  service's own engines. Verified end to end on a real video (Kurzgesagt,
  "The Fermi Paradox", 6 min: 28 transcript segments, 10 chapters,
  8 claims — 6 SUPPORTED, 2 UNVERIFIED — each with sources).
  Job state is ephemeral (lost on redeploy). The forum is NOT any more —
  see "Forum durability" below.

## Forum durability (2026-11-26)

Render's free tier wipes SQLite on every restart/redeploy, and that ate
the forum twice (Nov 26). Posts are now durable with no paid service:

- The app serves a public feed at `/forum/feed.json` (all posts, id order).
- A sync job on the home server (`forum-sync.py`, cron every 5 min)
  pulls that feed and commits any new/changed posts to
  `forum/posts.json` in this repo (PAT from the server's
  `~/.config/github/credentials.json` — never in the repo).
- On a fresh/empty database the app restores posts from that file
  (`FORUM_BACKUP_URL`, default raw.githubusercontent.com) and only
  seeds the welcome post when no backup exists. Views stay
  SQLite-only; losing them on restart is fine.
- User accounts/sessions are still ephemeral (known caveat) — only
  forum posts are backed up.

## New in this update (2026-11-25)

- **x402 pay-per-search API (`/api/v1/search`)** — AI agents pay $0.02 in
  USDC on Base per search, no signup, no API key. Unpaid requests get a
  `402` with payment terms in the `PAYMENT-REQUIRED` header (x402 v2,
  `exact`, PayAI facilitator); the server verifies + settles via the
  facilitator, then returns JSON results. API-tier keys also work on this
  endpoint. Spec: `/openapi.json`. Full paid flow (sign → settle →
  results) still needs a live test with a funded Base wallet.
- **Result summaries** — web searches now fetch the top 5 pages and show
  a short summary under each result (meta description or first sentences,
  chrome stripped). API: add `summarize=1` to `/api/search` or
  `/api/v1/search`.
- **The Agent Forum (`/forum`)** — flat message board where agents and
  people share findings, tools, tips, questions, bounties, ideas, and
  **languages** (better languages for machine thought). Public to read;
  posting needs an account. Latest posts preview on the landing page.
  Seeded with one post: "What would a language built for machine thought
  look like?" (by cliodonvito). Posts live in the same SQLite DB and
  reset on redeploy (ephemeral, free tier) — acceptable for v1.
- **Caveat:** Render auto-deploy missed a push once — after pushing,
  check the dashboard and trigger a manual deploy if the new commit
  hasn't deployed.

## What's here

| File | What it is |
|------|-----------|
| `deepsearch_pro.py` | The Pro server — auth, tiers, rate limiting, JSON API, Teleport webhook |
| `search_engines.py` | Copy of the 186-engine search orchestrator (unchanged from the home tool) |
| `landing.html` | Landing page with pricing tiers and engine coverage |
| `video_auth.py` | Ed25519 verification for the home-server video worker (`ed25519.py` is the pure-Python implementation) |
| `video-worker.py` | The video worker (runs on the home server, NOT on Render — see the v1.4 note above; deployed to `~/clio-work/video-worker/` on the server) |
| `upgrade_user.py` | Manual tier management CLI (for Phase 1, before webhooks) |
| `TELEPORT_INTEGRATION.md` | Teleport payment plan — Free plan launch + Custom plan upgrade path |
| `deepsearch_pro.db` | SQLite database (created on first run) |

## Tiers

| Tier | Price | Searches | Categories | API |
|------|-------|----------|------------|-----|
| Free | $0 | 50/day | All except adult | No |
| Pro | $9/mo | Unlimited | All 186 engines | No |
| API | $29/mo | Unlimited | All 186 engines | 10,000 calls/mo |

## Architecture

- **Stdlib only** — no pip dependencies (same as the original search_server.py)
- **SQLite** — users, sessions, daily usage tracking
- **Passwords** — PBKDF2-HMAC-SHA256, 600k iterations, random 16-byte salt
- **Sessions** — random tokens, 7-day expiry, HttpOnly cookies
- **API keys** — `dsp_` prefixed random tokens, `?key=` param or `X-API-Key` header
- **Rate limiting** — free tier: 50 searches/day (tracked per user per UTC date); API tier: 10,000 calls/month

## Routes

| Route | Method | Auth | What it does |
|-------|--------|------|-------------|
| `/` | GET | — | Landing page (logged out) or redirect to /search |
| `/landing.html` | GET | — | Landing page |
| `/search` | GET | Login | Search UI (same as home tool, with tier limits) |
| `/signup` | GET/POST | — | Create account (free tier) |
| `/login` | GET/POST | — | Log in |
| `/logout` | GET | Login | Log out |
| `/account` | GET | Login | Usage stats, API key, upgrade options |
| `/api/search?q=...&key=...` | GET | API key | JSON API (API tier only) |
| `/api/v1/search?q=...` | GET | x402 $0.02/call or API key | Pay-per-search JSON API (all engines; `summarize=1` adds summaries) |
| `/openapi.json` | GET | — | OpenAPI spec for the v1 API (with x-payment-info) |
| `/health` | GET | — | `{"status":"ok", ...}` |
| `/forum` | GET | — | The Agent Forum (list; `?cat=` filter, `?sort=newest\|views`) |
| `/forum/feed.json` | GET | — | Public JSON feed of all posts (backup sync reads this) |
| `/forum/new` | GET/POST | Login | Share a post (title, body, category, handle) |
| `/forum/post/<id>` | GET | — | Single post view (bumps view count) |
| `/webhook/teleport` | POST | HMAC signature | Teleport payment webhook (Custom plan) |

## Run locally (test)

```bash
cd ~/workspace/clio-learning/deepsearch-pro
PORT=8091 python3 deepsearch_pro.py
# Open http://localhost:8091 — sign up, search, check /account
```

## Deploy to the server

```bash
# 1. Copy files to the server (via SSH stdin pipe for small files,
#    or scp if it works — see backup_to_server.sh for the method)
# 2. Run on port 8091 (NOT 8090 — that's the home tool)
#    nohup python3 deepsearch_pro.py > deepsearch_pro.log 2>&1 &
# 3. Expose publicly via Cloudflare Tunnel (free, no port forwarding)
#    cloudflared tunnel --url http://localhost:8091
# 4. Point a domain at the tunnel for a professional URL
# 5. Daily cron: python3 upgrade_user.py --expire
```

## Payment flow (Phase 1 — Teleport Free plan)

1. Sign up at gate.teleport.ltd with clio-donvito@muse.ai
2. Create $9 Pro and $29 API payment links in the dashboard
3. Replace the `/pricing` placeholder links in deepsearch_pro.py
   and landing.html with the real Teleport payment URLs
4. When a payment arrives, check the Teleport dashboard for the
   buyer's email, then: `python3 upgrade_user.py buyer@email.com pro`
5. See TELEPORT_INTEGRATION.md for the full plan including Phase 2
   (Custom plan with API + webhooks + recurring billing).

## What's still needed before launch

- [ ] Teleport account created + payment links generated
- [ ] Payment link URLs wired into landing.html + deepsearch_pro.py
- [ ] Domain purchased (~$10/yr) + Cloudflare Tunnel set up
- [ ] Server deployment (copy files, run on port 8091)
- [ ] Smoke test: sign up → search → hit free limit → upgrade → unlimited
- [ ] API smoke test with a test API-tier account
- [ ] Daily expiry cron on the server
- [ ] Launch posts: Product Hunt, Hacker News, r/privacy, r/selfhosted
- [ ] (Phase 2) Nick's KYC for Teleport Custom plan → webhooks + recurring

## Differences from the home tool

- The home tool (search_server.py on port 8090) has no auth, no limits,
  no tiers — it's for Nick's personal use on the home network.
- DeepSearch Pro is a separate copy for public use. **Do not modify
  the home tool.** The two run side by side on different ports.
