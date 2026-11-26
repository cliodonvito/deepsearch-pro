"""video_auth.py — authentication for the DeepSearch Pro video worker.

The transcript worker runs on the home server (residential IP — YouTube
blocks caption fetches from datacenter IPs). The home server is never
exposed to the internet, so the worker reaches Render by polling over
outbound HTTPS. These worker endpoints must not be callable by anyone
else: a stranger who could claim jobs or post results could starve real
jobs or poison a paying user's verdicts.

Authentication is Ed25519 signatures, not a shared secret: the worker
holds the private seed (mode 600 on the home server only), and this
service knows only the public key (baked in below — public keys are
public, so committing it is safe). Every worker request carries a
signature over a canonical message that includes the method, the job id,
a timestamp (replay window: 5 minutes), and the SHA-256 of the request
body. The pure-Python Ed25519 code lives in ed25519.py (copied from the
Axiom-0 skeleton, verified against OpenSSL there).

The keypair was generated on the home server; the seed never left it.
"""

import hashlib
import time

import ed25519

# Public key of the video worker (hex). The matching seed lives only in
# ~/.config/deepsearch-pro/video-worker-key.json on the home server.
VIDEO_WORKER_PUBKEY_HEX = (
    "b53ac8053b1ae14aa519d24530d6c626b438f2ad5b7db8a1aa13490986966093"
)

WORKER_SIG_WINDOW_SECONDS = 300  # replay window for signed requests
WORKER_MESSAGE_PREFIX = "deepsearch-pro-video-v1"


def canonical_message(method: str, job_id: str, timestamp: int,
                      body_sha256: str) -> bytes:
    """The exact bytes the worker signs (and Render verifies)."""
    return ("\n".join([WORKER_MESSAGE_PREFIX, method,
                       job_id or "-", str(timestamp),
                       body_sha256 or "-"])).encode()


def sign_worker_request(seed_hex: str, method: str, job_id: str,
                        timestamp: int, body: bytes) -> str:
    """Worker side: sign a request. Returns the hex signature."""
    seed = bytes.fromhex(seed_hex)
    pubkey = ed25519.publickey(seed)
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = canonical_message(method, job_id, timestamp, body_hash)
    return ed25519.sign(msg, seed, pubkey).hex()


def verify_worker_request(headers, method: str, job_id: str,
                          body: bytes) -> bool:
    """Render side: verify a worker request from its headers.

    headers is a mapping with X-Worker-Timestamp, X-Worker-Signature
    and (optionally) X-Worker-Pubkey. Returns True only if the
    signature is valid under the baked-in worker public key, the
    timestamp is fresh, and the body hash matches.
    """
    try:
        ts_raw = headers.get("X-Worker-Timestamp", "")
        sig_hex = headers.get("X-Worker-Signature", "")
        pubkey_hex = headers.get("X-Worker-Pubkey", "")
        timestamp = int(ts_raw)
    except (ValueError, TypeError, AttributeError):
        return False
    if abs(time.time() - timestamp) > WORKER_SIG_WINDOW_SECONDS:
        return False
    if pubkey_hex and pubkey_hex != VIDEO_WORKER_PUBKEY_HEX:
        return False
    try:
        pubkey = bytes.fromhex(VIDEO_WORKER_PUBKEY_HEX)
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = canonical_message(method, job_id, timestamp, body_hash)
    return ed25519.verify(sig, msg, pubkey)
