"""Browser test line ("Talk to Ava"): spend guard and token builder.

Public endpoint, so every limit here protects the owner's bill. Counters live in memory and reset when the
backend restarts (a deploy), which is acceptable for a demo: the global cap is enforced again from zero.
Nothing here touches patient data. Switch the whole feature off with WEB_CALL_ENABLED unset or 0.
"""
import os
import time
import uuid
from datetime import timedelta

AGENT_NAME = "clinic-ava"


def _int(env, key, default):
    try:
        return int(env.get(key, default))
    except (TypeError, ValueError):
        return default


class WebCallGate:
    def __init__(self, env=None, now=time.time):
        self.env, self.now = (env if env is not None else os.environ), now
        self.by_ip = {}          # ip -> list of start times in the last hour
        self.day_key, self.day_count = None, 0
        self.active = {}         # room -> expiry time

    @property
    def enabled(self):
        return str(self.env.get("WEB_CALL_ENABLED", "0")).lower() in ("1", "true", "yes")

    @property
    def max_seconds(self):
        return _int(self.env, "WEB_CALL_MAX_SECONDS", 240)

    def _purge(self):
        t = self.now()
        self.active = {r: e for r, e in self.active.items() if e > t}
        self.by_ip = {ip: [s for s in v if t - s < 3600] for ip, v in self.by_ip.items()}
        self.by_ip = {ip: v for ip, v in self.by_ip.items() if v}
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        if day != self.day_key:
            self.day_key, self.day_count = day, 0

    def check(self, ip):
        """Return (ok, reason). reason is one of disabled, busy, ip_limit, daily_cap."""
        if not self.enabled:
            return False, "disabled"
        self._purge()
        if len(self.active) >= _int(self.env, "WEB_CALL_MAX_CONCURRENT", 2):
            return False, "busy"
        if len(self.by_ip.get(ip, [])) >= _int(self.env, "WEB_CALL_PER_IP_PER_HOUR", 3):
            return False, "ip_limit"
        if self.day_count >= _int(self.env, "WEB_CALL_DAILY_CAP", 30):
            return False, "daily_cap"
        return True, ""

    def admit(self, ip, room):
        t = self.now()
        self.by_ip.setdefault(ip, []).append(t)
        self.day_count += 1
        self.active[room] = t + self.max_seconds + 5

    def release(self, room):
        return self.active.pop(room, None) is not None


def new_room():
    return f"web-{uuid.uuid4().hex[:12]}"


def make_token(room, key, secret, max_seconds=240):
    """Join-only token for one room. The agent is dispatched when the caller joins, so the greeting is heard."""
    from livekit import api
    identity = f"web-caller-{uuid.uuid4().hex[:6]}"
    return (api.AccessToken(key, secret).with_identity(identity).with_name("Web caller")
            .with_ttl(timedelta(seconds=max_seconds + 120))
            .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                         can_publish_data=False))
            .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]))
            .to_jwt())
