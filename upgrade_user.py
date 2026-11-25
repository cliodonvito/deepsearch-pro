#!/usr/bin/env python3
"""upgrade_user.py — manually set a DeepSearch Pro user's tier.

Used in Phase 1 (Teleport Free plan): there are no webhooks, so when a
payment comes in, check the Teleport dashboard (gate.teleport.ltd) for
the buyer's email, then run this script to upgrade them.

Usage:
    python3 upgrade_user.py user@example.com pro       # 1 month
    python3 upgrade_user.py user@example.com api 3     # 3 months
    python3 upgrade_user.py user@example.com free      # downgrade
    python3 upgrade_user.py --list                     # list all users
    python3 upgrade_user.py --expire                   # expire lapsed tiers
"""
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from deepsearch_pro import set_tier, expire_tiers, get_db  # noqa: E402


def list_users():
    db = get_db()
    rows = db.execute(
        "SELECT email, tier, created_at, tier_expires_at FROM users"
        " ORDER BY created_at").fetchall()
    db.close()
    if not rows:
        print("No users yet.")
        return
    print(f"{'Email':<35} {'Tier':<6} {'Created':<12} {'Expires'}")
    print("-" * 75)
    for r in rows:
        print(f"{r['email']:<35} {r['tier']:<6} "
              f"{(r['created_at'] or '')[:10]:<12} "
              f"{(r['tier_expires_at'] or '—')[:10]}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--list"]:
        list_users()
        sys.exit(0)
    if args == ["--expire"]:
        n = expire_tiers()
        print(f"Expired {n} lapsed paid tier(s).")
        sys.exit(0)
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    email, tier = args[0], args[1]
    months = int(args[2]) if len(args) > 2 else 1
    if set_tier(email, tier, months):
        print(f"OK: {email} -> {tier}"
              + (f" for {months} month(s)" if tier != "free" else ""))
    else:
        print(f"FAILED: no user with email {email}, or bad tier '{tier}'.")
        sys.exit(1)
