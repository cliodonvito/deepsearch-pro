# DeepSearch Pro

The Deep Search tool (186 engines) productized as a hosted SaaS.

## What's here

| File | What it is |
|------|-----------|
| `deepsearch_pro.py` | The Pro server — auth, tiers, rate limiting, JSON API, Teleport webhook |
| `search_engines.py` | Copy of the 186-engine search orchestrator (unchanged from the home tool) |
| `landing.html` | Landing page with pricing tiers and engine coverage |
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
