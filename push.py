"""Apple Push Notification service (APNs) client: token (.p8) authentication over HTTP/2.

One signing key covers every app on the team and both APNs servers; each device registration says
which server its token belongs to (sandbox for Xcode builds, production for TestFlight and the
App Store).

    apns = Apns.from_env()   # APNS_KEY_P8, APNS_KEY_ID, APNS_TEAM_ID; None if any is missing
    result = apns.send(token, "sandbox", "com.example.app", {"title": "Hi", "body": "There"})
"""

import os
import time
from dataclasses import dataclass

import httpx
import jwt

HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
TOKEN_LIFETIME = 50 * 60     # APNs wants a fresh signed token at least hourly, and no more than every 20 min
# Reasons that mean this device token will never work again for this app.
DEAD_TOKEN = {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}


@dataclass
class Result:
    status: int
    reason: str | None = None

    @property
    def ok(self):
        return self.status == 200

    @property
    def dead_token(self):
        return self.status == 410 or self.reason in DEAD_TOKEN


class Apns:
    def __init__(self, key_p8, key_id, team_id):
        self._key, self._key_id, self._team_id = key_p8, key_id, team_id
        self._bearer, self._signed_at = None, 0.0
        self._http = httpx.Client(http2=True, timeout=httpx.Timeout(10.0, connect=5.0))

    @classmethod
    def from_env(cls):
        key, key_id, team_id = os.getenv("APNS_KEY_P8"), os.getenv("APNS_KEY_ID"), os.getenv("APNS_TEAM_ID")
        return cls(key, key_id, team_id) if key and key_id and team_id else None

    def _token(self, refresh=False):
        if refresh or not self._bearer or time.time() - self._signed_at > TOKEN_LIFETIME:
            self._bearer = jwt.encode({"iss": self._team_id, "iat": int(time.time())}, self._key,
                                      algorithm="ES256", headers={"kid": self._key_id})
            self._signed_at = time.time()
        return self._bearer

    def send(self, device_token, environment, topic, alert, data=None, collapse_id=None, thread_id=None,
             expires_in=3600):
        """Send one visible alert ({"title": ..., "body": ...}); extra `data` rides along for the app."""
        aps = {"alert": alert, "sound": "default"}
        if thread_id:
            aps["thread-id"] = thread_id
        headers = {"apns-topic": topic, "apns-push-type": "alert", "apns-priority": "10",
                   "apns-expiration": str(int(time.time()) + expires_in)}
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]
        url = f"{HOSTS[environment]}/3/device/{device_token}"
        payload = {"aps": aps, **(data or {})}
        for attempt in range(2):
            try:
                response = self._http.post(url, json=payload,
                                           headers={**headers, "authorization": f"bearer {self._token(attempt > 0)}"})
            except httpx.HTTPError as exc:
                return Result(0, f"{type(exc).__name__}: {exc}")
            reason = response.json().get("reason") if response.content else None
            # A rejected signing token (clock skew, rotation) gets one retry with a fresh one.
            if response.status_code == 403 and reason in ("ExpiredProviderToken", "InvalidProviderToken") and attempt == 0:
                continue
            return Result(response.status_code, reason)
        return Result(403, "InvalidProviderToken")

    def close(self):
        self._http.close()
