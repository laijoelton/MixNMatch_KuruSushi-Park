"""Simulator MD5 signature over alphabetically sorted field values."""
from __future__ import annotations
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class SignatureResult:
    ok: bool
    matched: tuple[str, ...]
    enforced: bool = True

def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)

def digest(payload: dict[str, Any]) -> str:
    values = "|".join(_stringify(payload[k]) for k in sorted(payload) if k != "Signature")
    return hashlib.md5(values.encode("utf-8"), usedforsecurity=False).hexdigest()

def verify(payload: dict[str, Any]) -> SignatureResult:
    provided = str(payload.get("Signature") or "").lower()
    ok = len(provided) == 32 and provided.isascii() and hmac.compare_digest(digest(payload), provided)
    return SignatureResult(ok, ("md5:pipe:alpha",) if ok else ())
