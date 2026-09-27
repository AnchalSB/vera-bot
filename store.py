"""
store.py — in-memory context store.

Matches testing-brief §2.1 exactly:
  - Idempotent by (scope, context_id, version)
  - Re-posting the same version is a no-op (still 200, accepted=true, since
    the brief's example only shows 409 for a version *older* than current —
    same version is a no-op, not a conflict)
  - A higher version for the same context_id replaces atomically
  - A version older than what's stored -> 409 stale_version
"""

from __future__ import annotations
import threading
from typing import Any, Optional


class ContextStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], dict] = {}   # (scope, context_id) -> {version, payload}
        # sent-message log for suppression / anti-repetition, keyed by suppression_key
        self._sent: dict[str, set[str]] = {}            # suppression_key -> set(body hashes)
        # conversation turn logs, for reply handling + anti-repeat within a conversation
        self._conversations: dict[str, list[dict]] = {}  # conversation_id -> [{from, body}, ...]

    def put(self, scope: str, context_id: str, version: int, payload: dict) -> dict:
        key = (scope, context_id)
        with self._lock:
            cur = self._data.get(key)
            if cur is not None:
                if version < cur["version"]:
                    return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
                if version == cur["version"]:
                    return {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "noop": True}
            self._data[key] = {"version": version, "payload": payload}
            return {"accepted": True, "ack_id": f"ack_{context_id}_v{version}"}

    def get(self, scope: str, context_id: str) -> Optional[dict]:
        entry = self._data.get((scope, context_id))
        return entry["payload"] if entry else None

    def counts(self) -> dict:
        out = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for (scope, _cid) in self._data.keys():
            out[scope] = out.get(scope, 0) + 1
        return out

    # ---- suppression / anti-repetition ----

    def already_sent(self, suppression_key: str, body: str) -> bool:
        seen = self._sent.get(suppression_key)
        return bool(seen and body in seen)

    def mark_sent(self, suppression_key: str, body: str):
        self._sent.setdefault(suppression_key, set()).add(body)

    def was_suppressed_recently(self, suppression_key: str) -> bool:
        """True if *anything* has been sent under this key before — used to
        avoid re-sending on the same suppression_key even with different
        wording, which is what suppression_key is actually for (dedup of the
        underlying event, not just literal text)."""
        return suppression_key in self._sent and len(self._sent[suppression_key]) > 0

    # ---- conversations ----

    def log_turn(self, conversation_id: str, from_role: str, body: str):
        self._conversations.setdefault(conversation_id, []).append({"from": from_role, "body": body})

    def history(self, conversation_id: str) -> list[dict]:
        return self._conversations.get(conversation_id, [])

    # ---- teardown ----

    def wipe(self):
        with self._lock:
            self._data.clear()
            self._sent.clear()
            self._conversations.clear()
