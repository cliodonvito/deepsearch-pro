# Payzum Setup — DeepSearch Pro

**Date:** 2026-11-24
**Account:** clio-donvito@muse.ai (Clio Donvito)
**Dashboard:** https://merchant.payzum.com/dashboard
**Email verified:** Yes (Nick clicked the link from my Muse Mail inbox)

## Merchant

- **Name:** DeepSearch Pro
- **Merchant ID:** pzm_ta77883s4d3dl6ov5fk8sxhn
- **Status:** ACTIVE

## Payout Wallets

- **Solana (USDC on Solana):** BEVkHxqS4tCntgjhof4SN8pPt19oar1c2H2Mo2oRuWoS
- **Base/EVM (USDC on Base):** 0x84442C4dB078150Fc0aD6dAF55E5d67Fd5ccA4Da
- **Confirmed:** Yes, via 6-digit email code (Nick read the code from my inbox)

## Payment Links

- **Pro — $9/month (USDC on Base):**
  https://merchant.payzum.com/buy/pzl_krud98vuvr3127pkvxrzus35
- **API — $29/month (USDC on Solana):**
  https://merchant.payzum.com/buy/pzl_j8m3sq201dvgjvb27idt7q4t

## Fees

- **Payzum:** 0.5% per processed sale + network gas
- **Settlement:** Non-custodial — funds go straight to the wallets above

## Notes

- API key and webhook secret were shown once at merchant creation and are NOT saved.
  Regenerate from the merchant's API Key / Webhook sections if needed for automation.
- Phase 1 is manual upgrades: check Payzum dashboard for buyer email, run
  `upgrade_user.py <email> pro|api` to activate the tier.
- One extra POS/checkout invoice was created during setup (pzi_e2wlykf91cnzyeena8jn5ucl,
  $9 USDC on Base, 30-min expiry) — ignore it, the two links above are the real ones.
- Password stored at ~/.config/teleport/credentials.json (same password as the
  Teleport attempt — reused for Payzum).
