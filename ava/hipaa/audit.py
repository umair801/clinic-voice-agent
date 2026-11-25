"""Append-only audit log. Each line holds the hash of the line before it, so an edit or a deleted line is detectable.
Holds NO patient details: only who/what/when with pseudonymous references."""
import hashlib
import hmac
import json
import os
import time


def pseudonym(value, salt):
    """Stable, non-reversible reference to a person or call. Salt comes from the environment, never stored in the log."""
    return hmac.new(salt.encode(), str(value).encode(), hashlib.sha256).hexdigest()[:16]


FORBIDDEN_KEYS = {"name", "first_name", "last_name", "dob", "phone", "email", "address", "zip", "ssn", "text", "transcript"}


class AuditLog:
    def __init__(self, path, salt="dev-salt"):
        self.path, self.salt = path, salt
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._last = self._tail()

    def _tail(self):
        if not os.path.exists(self.path):
            return "0" * 64
        last = "0" * 64
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def record(self, call_id, action, subject=None, outcome="ok", **detail):
        bad = FORBIDDEN_KEYS & set(detail)
        if bad:
            raise ValueError(f"audit detail may not hold personal fields: {sorted(bad)}")
        entry = {"t": round(time.time(), 3), "call": pseudonym(call_id, self.salt), "action": action,
                 "subject": pseudonym(subject, self.salt) if subject else None, "outcome": outcome, "detail": detail, "prev": self._last}
        entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        self._last = entry["hash"]
        return entry

    def verify(self):
        """Returns (ok, first_bad_line_number or None)."""
        prev = "0" * 64
        if not os.path.exists(self.path):
            return True, None
        with open(self.path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                e = json.loads(line)
                h = e.pop("hash")
                if e["prev"] != prev or hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest() != h:
                    return False, i
                prev = h
        return True, None
