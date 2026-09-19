"""Webhook signature verification with runtime calibration.

The organizer's documentation describes the recipe as: drop ``Signature``,
sort the remaining field NAMES alphabetically, join their VALUES with ``|``,
hash, compare.

That recipe does not reproduce the signatures printed in the same document.
Tested offline against the doc's own ``component_broken`` sample across
MD5/SHA1/SHA256, four separators, three key orderings and several shared-secret
guesses: no match. The doc's worked example includes a ``RealDateTime`` field
that the sample payloads omit, so the published samples are almost certainly
incomplete and cannot be verified without live traffic.

Rather than hardcode a guess that silently drops every real event, this module
scores a family of candidate recipes against each inbound webhook and records
which ones matched. Once one recipe is at 100% over a few hundred events, pin
it with ``WEBHOOK_SIGNATURE_RECIPE`` and set ``WEBHOOK_SIGNATURE_MODE=enforce``.

Run ``python -m scripts.signature_report`` to see the tally.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Optional

from app.config import settings
from app.db import bump_signature_trial, set_meta

_ALGOS = ("md5", "sha1", "sha256")
_SEPARATORS = {"pipe": "|", "none": "", "dash": "-", "colon": ":"}


def _order_alpha(keys: list[str]) -> list[str]:
    """Ordinal sort: uppercase letters sort before lowercase."""
    return sorted(keys)


def _order_alpha_ci(keys: list[str]) -> list[str]:
    return sorted(keys, key=str.lower)


def _order_insertion(keys: list[str]) -> list[str]:
    """Order as they appeared in the JSON body."""
    return list(keys)


_ORDERS = {
    "alpha": _order_alpha,
    "alphaci": _order_alpha_ci,
    "insertion": _order_insertion,
}


@dataclass(frozen=True)
class SignatureResult:
    ok: Optional[bool]          # None => no signature present to check
    matched: tuple[str, ...]    # recipes that reproduced the signature
    enforced: bool


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _candidates(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """(recipe, digest) for every combination we are willing to consider."""
    keys = [k for k in payload.keys() if k != "Signature"]
    out: list[tuple[str, str]] = []
    secret = settings.webhook_secret.encode() if settings.webhook_secret else None

    for order_name, order_fn in _ORDERS.items():
        ordered = order_fn(keys)
        for sep_name, sep in _SEPARATORS.items():
            joined = sep.join(_stringify(payload[k]) for k in ordered).encode()
            for algo in _ALGOS:
                if secret:
                    digest = hmac.new(secret, joined, getattr(hashlib, algo)).hexdigest()
                else:
                    digest = hashlib.new(algo, joined).hexdigest()
                out.append((f"{algo}:{sep_name}:{order_name}", digest))
    return out


def verify(payload: dict[str, Any]) -> SignatureResult:
    """Score every candidate recipe and decide whether to accept the event."""
    provided = payload.get("Signature")
    if not provided:
        return SignatureResult(ok=None, matched=(), enforced=False)

    provided_lc = str(provided).lower()
    matched: list[str] = []

    for recipe, digest in _candidates(payload):
        hit = hmac.compare_digest(digest.lower(), provided_lc)
        if hit:
            matched.append(recipe)
        bump_signature_trial(recipe, hit)

    if matched:
        set_meta("signature_last_match", ",".join(matched))

    pinned = settings.webhook_signature_recipe

    if settings.webhook_signature_mode == "enforce":
        # With no pinned recipe, accept anything that matched at least one
        # candidate rather than rejecting all traffic outright.
        ok = (pinned in matched) if pinned else bool(matched)
        return SignatureResult(ok=ok, matched=tuple(matched), enforced=True)

    return SignatureResult(ok=bool(matched), matched=tuple(matched), enforced=False)
