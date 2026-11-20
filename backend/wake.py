"""
The wake-up the Worker sends the step function (docs/BACKEND.md), standard library only:

    x-ro-wake: v1,<ts>,<lane>,<nonce>,<sig>
    sig = lowercase hex HMAC-SHA256(WAKE_SECRET, "v1|<ts>|<lane>|<nonce>")

verify() is what api/step.py runs before it imports anything else; sign() is the Worker's side, for the
dev server and the tests.
"""
import hashlib
import hmac
import re
import secrets
import time

SCHEME = "v1"
LANES = ("main", "news", "model", "add")          # CONTRACT.lanes
MAX_SKEW_S = 60                                   # CONTRACT.wake.max_skew_s
_NONCE = re.compile(r"^[0-9a-f]{16}$")
_SIG = re.compile(r"^[0-9a-f]{64}$")
_TS = re.compile(r"^[0-9]{1,12}$")


def signature(secret, ts, lane, nonce):
    msg = f"{SCHEME}|{ts}|{lane}|{nonce}".encode("utf-8")
    return hmac.new(str(secret).encode("utf-8"), msg, hashlib.sha256).hexdigest()


def sign(secret, lane, ts=None, nonce=None):
    """The header value for a wake-up of lane."""
    ts = int(time.time() if ts is None else ts)
    nonce = nonce or secrets.token_hex(8)
    return f"{SCHEME},{ts},{lane},{nonce},{signature(secret, ts, lane, nonce)}"


def verify(header, secret, now=None, lanes=LANES, max_skew_s=MAX_SKEW_S):
    """The lane a wake-up header names when it is well formed, signed with secret and within max_skew_s of now;
    else None. No secret set: None (the function then refuses every call)."""
    if not secret or not isinstance(header, str) or len(header) > 200:
        return None
    parts = header.strip().split(",")
    if len(parts) != 5:
        return None
    scheme, ts, lane, nonce, sig = parts
    if scheme != SCHEME or lane not in lanes or not _TS.match(ts) or not _NONCE.match(nonce) or not _SIG.match(sig):
        return None
    now = time.time() if now is None else now
    if abs(now - int(ts)) > max_skew_s:
        return None
    if not hmac.compare_digest(signature(secret, ts, lane, nonce), sig):
        return None
    return lane
