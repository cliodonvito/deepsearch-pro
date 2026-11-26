"""ed25519.py — pure-Python RFC 8032 Ed25519 signatures (stdlib only).

No third-party dependencies: only hashlib. Used by Axiom-0's idd service so
identity works with nothing installed beyond Python itself.

Reference: RFC 8032 (Edwards-curve Digital Signature Algorithm).
The arithmetic follows djb's reference implementation.

Security note: this is a straightforward, unoptimized implementation for the
Stage 0 skeleton. It is NOT constant-time and has NOT been audited. It is
verified against OpenSSL's Ed25519 (see README.md) before the services
trust it. A production Axiom would use a vetted library (e.g. `cryptography`).
"""

import hashlib

b = 256
q = (1 << 255) - 19
l = (1 << 252) + 27742317777372353535851937790883648493
# d = -121665 / 121666 mod q
d = -121665 * pow(121666, q - 2, q) % q
# sqrt(-1) mod q
I = pow(2, (q - 1) // 4, q)


def _H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _inv(x: int) -> int:
    return pow(x, q - 2, q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(d * y * y + 1) % q
    x = pow(xx, (q + 3) // 8, q)
    if (x * x - xx) % q != 0:
        x = (x * I) % q
    if x % 2 != 0:
        x = q - x
    return x


_By = 4 * _inv(5) % q
_Bx = _xrecover(_By)
B = [_Bx % q, _By % q]  # base point


def _edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + d * x1 * x2 * y1 * y2 % q) % q
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - d * x1 * x2 * y1 * y2 % q) % q
    return [x3, y3]


def _scalarmult(P, e: int):
    if e == 0:
        return [0, 1]
    Q = _scalarmult(P, e // 2)
    Q = _edwards(Q, Q)
    if e & 1:
        Q = _edwards(Q, P)
    return Q


def _encodeint(y: int) -> bytes:
    return y.to_bytes(32, "little")


def _encodepoint(P) -> bytes:
    x, y = P
    bits = (y & ((1 << 255) - 1)) | ((x & 1) << 255)
    return bits.to_bytes(32, "little")


def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _Hint(m: bytes) -> int:
    return int.from_bytes(_H(m), "little")


def _decodeint(s: bytes) -> int:
    return int.from_bytes(s, "little")


def _decodepoint(s: bytes):
    y = _decodeint(s) & ((1 << 255) - 1)
    sign = (_decodeint(s) >> 255) & 1
    x = _xrecover(y)
    if x & 1 != sign:
        x = q - x
    P = [x, y]
    if not _isoncurve(P):
        raise ValueError("point not on curve")
    return P


def _isoncurve(P) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - d * x * x * y * y) % q == 0


# ---------------------------------------------------------------- public API

def publickey(seed: bytes) -> bytes:
    """Derive the 32-byte public key from a 32-byte seed (secret key)."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _H(seed)
    a = 2 ** 254 + sum(2 ** i * _bit(h, i) for i in range(3, 254))
    return _encodepoint(_scalarmult(B, a))


def sign(msg: bytes, seed: bytes, public_key: bytes) -> bytes:
    """Sign msg with seed; returns the 64-byte signature."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _H(seed)
    a = 2 ** 254 + sum(2 ** i * _bit(h, i) for i in range(3, 254))
    r = _Hint(h[32:] + msg)
    R = _scalarmult(B, r)
    S = (r + _Hint(_encodepoint(R) + public_key + msg) * a) % l
    return _encodepoint(R) + _encodeint(S)


def verify(sig: bytes, msg: bytes, public_key: bytes) -> bool:
    """Return True iff sig is a valid signature of msg under public_key."""
    if len(sig) != 64 or len(public_key) != 32:
        return False
    try:
        R = _decodepoint(sig[:32])
        A = _decodepoint(public_key)
    except ValueError:
        return False
    S = _decodeint(sig[32:])
    if S >= l:
        return False
    h = _Hint(sig[:32] + public_key + msg)
    return _scalarmult(B, S) == _edwards(R, _scalarmult(A, h))
