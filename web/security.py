"""
Sicurezza della dashboard: cookie firmati e sessioni lato server (solo libreria standard)
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """Firma e verifica piccoli payload JSON con HMAC-SHA256 e scadenza"""

    def __init__(self, secret: str):
        if not secret:
            raise ValueError("secret mancante")
        self._key = secret.encode("utf-8")

    def _mac(self, body: str) -> str:
        return _b64e(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())

    def sign(self, payload: Dict[str, Any], max_age: int) -> str:
        data = dict(payload)
        data["exp"] = int(time.time()) + int(max_age)
        body = _b64e(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        return f"{body}.{self._mac(body)}"

    def verify(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token or token.count(".") != 1:
            return None
        body, mac = token.split(".")
        try:
            if not hmac.compare_digest(mac, self._mac(body)):
                return None
            data = json.loads(_b64d(body))
        except Exception:
            return None
        if not isinstance(data, dict) or int(data.get("exp", 0)) < time.time():
            return None
        return data


class SessionStore:
    """Sessioni in memoria: si perdono al riavvio del bot (basta rifare il login)"""

    def __init__(self, max_sessions: int = 5000):
        self._items: Dict[str, Dict[str, Any]] = {}
        self._max = max_sessions

    def _purge(self) -> None:
        now = time.time()
        for sid in [k for k, v in self._items.items() if v["exp"] < now]:
            del self._items[sid]
        # difesa da riempimento: elimina le più vecchie
        while len(self._items) >= self._max:
            oldest = min(self._items, key=lambda k: self._items[k]["created"])
            del self._items[oldest]

    def create(self, data: Dict[str, Any], ttl: int) -> str:
        self._purge()
        sid = secrets.token_urlsafe(32)
        self._items[sid] = {"data": data, "exp": time.time() + ttl, "created": time.time()}
        return sid

    def get(self, sid: Optional[str]) -> Optional[Dict[str, Any]]:
        if not sid:
            return None
        item = self._items.get(sid)
        if not item:
            return None
        if item["exp"] < time.time():
            self._items.pop(sid, None)
            return None
        return item["data"]

    def destroy(self, sid: Optional[str]) -> None:
        if sid:
            self._items.pop(sid, None)
