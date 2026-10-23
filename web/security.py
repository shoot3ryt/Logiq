"""
Sicurezza della dashboard: cookie firmati e sessioni lato server (solo libreria standard)
"""

import asyncio
import base64
import hashlib
import logging
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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


def _sid_key(sid: str) -> str:
    """Nel database non si salva l'identificativo in chiaro, ma il suo hash"""
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


class SessionStore:
    """
    Sessioni lato server. La copia di lavoro è in memoria (lettura sincrona e veloce);
    se è presente un backend (database) ogni modifica viene salvata anche lì, così le
    sessioni sopravvivono al riavvio del bot. Il backend deve offrire:
    save_web_session(key, data, exp), delete_web_session(key), load_web_sessions() -> [(key, data, exp, created)]
    """

    def __init__(self, max_sessions: int = 5000, backend: Any = None):
        self._items: Dict[str, Dict[str, Any]] = {}
        self._max = max_sessions
        self._backend = backend
        self._tasks: set = set()

    # ----- persistenza (mai bloccante e mai fatale) -----
    def _spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._guard(coro))
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _guard(coro) -> None:
        try:
            await coro
        except Exception as exc:  # il database non deve mai rompere il login
            logger.warning("Salvataggio sessione non riuscito: %s", exc)

    async def load(self) -> int:
        """Ricarica dal database le sessioni non scadute. Restituisce quante ne ha caricate"""
        if not self._backend:
            return 0
        try:
            rows = await self._backend.load_web_sessions()
        except Exception as exc:
            logger.warning("Impossibile ricaricare le sessioni: %s", exc)
            return 0
        now = time.time()
        loaded = 0
        for sid_key, data, exp, created in rows:
            if exp > now and isinstance(data, dict):
                self._items[sid_key] = {"data": data, "exp": exp, "created": created}
                loaded += 1
        return loaded

    # ----- API sincrona -----
    def _purge(self) -> None:
        now = time.time()
        for k in [k for k, v in self._items.items() if v["exp"] < now]:
            del self._items[k]
        # difesa da riempimento: elimina le più vecchie
        while len(self._items) >= self._max:
            oldest = min(self._items, key=lambda k: self._items[k]["created"])
            del self._items[oldest]
            if self._backend:
                self._spawn(self._backend.delete_web_session(oldest))

    def create(self, data: Dict[str, Any], ttl: int) -> str:
        self._purge()
        sid = secrets.token_urlsafe(32)
        key = _sid_key(sid)
        now = time.time()
        self._items[key] = {"data": data, "exp": now + ttl, "created": now}
        if self._backend:
            self._spawn(self._backend.save_web_session(key, data, now + ttl, now))
        return sid

    def get(self, sid: Optional[str]) -> Optional[Dict[str, Any]]:
        if not sid:
            return None
        key = _sid_key(sid)
        item = self._items.get(key)
        if not item:
            return None
        if item["exp"] < time.time():
            self._items.pop(key, None)
            if self._backend:
                self._spawn(self._backend.delete_web_session(key))
            return None
        return item["data"]

    def destroy(self, sid: Optional[str]) -> None:
        if not sid:
            return
        key = _sid_key(sid)
        self._items.pop(key, None)
        if self._backend:
            self._spawn(self._backend.delete_web_session(key))
