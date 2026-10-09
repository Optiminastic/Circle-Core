"""Verifying that a webhook really came from the service that claims to send it.

A shared secret in a header proves only that the caller knows the secret. It
travels in full on every request, so a proxy log, a misrouted retry or a stale
dashboard copy leaks the whole credential, and anyone who picks it up can post
any payload they like.

An HMAC proves two further things: the body is the one that was signed, and the
secret itself never crosses the wire. Pairing it with a signed timestamp closes
replay, which a bare signature does not - a captured request stays valid
forever otherwise.

Pure except for `SeenSignatures`, which has to remember something to do its job.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from dataclasses import dataclass, field
from threading import Lock

#: How far out of step a sender's clock may be. Long enough to absorb skew and
#: a retry, short enough that a captured request stops working the same minute.
DEFAULT_TOLERANCE_SECONDS = 300

#: Only SHA-256. Vapi's credential form also offers SHA-1, which is not a
#: choice worth exposing in config for something guarding identity documents.
_DIGEST = hashlib.sha256


@dataclass(frozen=True)
class SignatureCheck:
    """Why a signature was refused, or that it was accepted."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:  # lets callers write `if not check:`
        return self.ok


def signed_payload(body: bytes, timestamp: str | None) -> bytes:
    """What the signature is computed over.

    Mirrors the two payload formats Vapi's HMAC credential offers: the raw body
    on its own, or `{timestamp}.{body}`. The body is used as received - never
    re-serialised JSON, because key order and spacing would differ and the
    digest would too.
    """
    if timestamp:
        return timestamp.encode("utf-8", "replace") + b"." + body
    return body


def expected_signature(body: bytes, timestamp: str | None, secret: str) -> str:
    """The hex digest this request should carry."""
    return hmac.new(
        secret.encode("utf-8"), signed_payload(body, timestamp), _DIGEST
    ).hexdigest()


def _timestamp_fresh(timestamp: str, now: float, tolerance: int) -> SignatureCheck:
    try:
        sent = float(timestamp)
    except (TypeError, ValueError):
        return SignatureCheck(False, "timestamp is not a number")
    # NaN and the infinities parse happily, and every comparison against NaN
    # is False - so without this, "NaN" sails through the freshness check
    # below and buys an unlimited replay window.
    if not math.isfinite(sent):
        return SignatureCheck(False, "timestamp is not a finite number")
    # Milliseconds are as common as seconds and the two are told apart by
    # magnitude: anything past the year 2286 in seconds is milliseconds.
    if sent > 10_000_000_000:
        sent /= 1000.0
    drift = abs(now - sent)
    if drift > tolerance:
        return SignatureCheck(False, f"timestamp is {int(drift)}s out of date")
    return SignatureCheck(True)


def verify_signature(
    *,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    secret: str,
    require_timestamp: bool,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> SignatureCheck:
    """Is this body signed with `secret`, and recent enough to act on?

    `require_timestamp` follows the payload format configured at the sender:
    with it on, an unsigned-age request is refused rather than quietly accepted
    as the body-only format, which would hand back the replay window.
    """
    if not secret:
        return SignatureCheck(False, "no signing secret configured")
    if not signature:
        return SignatureCheck(False, "signature header missing")
    if require_timestamp:
        if not timestamp:
            return SignatureCheck(False, "timestamp header missing")
        fresh = _timestamp_fresh(timestamp, time.time() if now is None else now, tolerance_seconds)
        if not fresh:
            return fresh

    expected = expected_signature(body, timestamp if require_timestamp else None, secret)
    # Tolerate a `sha256=` prefix: several senders write one, and rejecting it
    # looks identical to a wrong key while being a far duller problem.
    provided = signature.strip()
    if "=" in provided and provided.split("=", 1)[0].lower() in {"sha256", "sha-256"}:
        provided = provided.split("=", 1)[1]
    if not hmac.compare_digest(provided.lower().encode(), expected.encode()):
        return SignatureCheck(False, "signature does not match")
    return SignatureCheck(True)


@dataclass
class SeenSignatures:
    """Signatures already acted on, so a valid request cannot be replayed.

    A signature is unique per body, so remembering it is remembering the exact
    request. Entries are only kept for the freshness window - past that the
    timestamp check refuses the request anyway, so holding them longer would
    grow without bound for no extra safety.

    Process-local, like `core.rate_limit`. Across several instances a replay
    could land on a different process within the window; the timestamp check
    still bounds it, and the service ignores events for a finished call. Move
    this to the shared store if the API is ever scaled out.
    """

    ttl_seconds: int = DEFAULT_TOLERANCE_SECONDS
    _seen: dict[str, float] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def _evict(self, moment: float) -> None:
        cutoff = moment - self.ttl_seconds
        for stale in [sig for sig, at in self._seen.items() if at < cutoff]:
            del self._seen[stale]

    def was_used(self, signature: str, now: float | None = None) -> bool:
        """Has this exact request already been acted on?"""
        moment = time.time() if now is None else now
        with self._lock:
            self._evict(moment)
            return signature in self._seen

    def remember(self, signature: str, now: float | None = None) -> None:
        """Record it, once the request it signs has actually been handled."""
        moment = time.time() if now is None else now
        with self._lock:
            self._evict(moment)
            self._seen[signature] = moment

    def check_and_remember(self, signature: str, now: float | None = None) -> bool:
        """True the first time this signature is seen, False on a repeat.

        The two-step `was_used` / `remember` pair is what the route uses, so a
        delivery that fails on our side can be retried. This stays for callers
        with nothing to fail between the two.
        """
        moment = time.time() if now is None else now
        with self._lock:
            self._evict(moment)
            if signature in self._seen:
                return False
            self._seen[signature] = moment
            return True
