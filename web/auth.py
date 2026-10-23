"""
Login con Discord (OAuth2) per la dashboard

Flusso: /auth/login -> Discord -> /auth/callback -> sessione lato server + cookie firmato.
Il token di accesso di Discord viene usato solo durante il callback (identità e lista server)
e poi revocato: non viene mai salvato.
"""

import logging
import os
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .security import SessionStore, Signer

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"

SESSION_COOKIE = "logiq_session"
STATE_COOKIE = "logiq_oauth"
SESSION_TTL = 7 * 24 * 3600
STATE_TTL = 600

PERM_ADMINISTRATOR = 0x8
PERM_MANAGE_GUILD = 0x20

_ID_RE = re.compile(r"^\d{5,25}$")
_HASH_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class AuthError(Exception):
    """Errore durante il login con Discord"""


def avatar_url(user_id: str, avatar_hash: Optional[str]) -> str:
    if avatar_hash and _ID_RE.match(user_id) and _HASH_RE.match(avatar_hash):
        return f"{CDN}/avatars/{user_id}/{avatar_hash}.png?size=64"
    index = (int(user_id) >> 22) % 6 if _ID_RE.match(user_id) else 0
    return f"{CDN}/embed/avatars/{index}.png"


def icon_url(guild_id: str, icon_hash: Optional[str]) -> Optional[str]:
    if icon_hash and _ID_RE.match(guild_id) and _HASH_RE.match(icon_hash):
        return f"{CDN}/icons/{guild_id}/{icon_hash}.png?size=64"
    return None


class WebAuth:
    """Configurazione, sessioni e controlli di accesso della dashboard"""

    def __init__(self, bot, transport: Optional[httpx.AsyncBaseTransport] = None):
        web_cfg = bot.config.get("web", {}) or {}
        self.bot = bot
        self._transport = transport
        self._owner_id: Optional[int] = None
        self._warned_public_url = False

        self._client_id = os.getenv("DISCORD_CLIENT_ID") or (str(web_cfg.get("client_id")) if web_cfg.get("client_id") else None)
        self.client_secret = os.getenv("DISCORD_CLIENT_SECRET") or None
        self.public_url = (os.getenv("PUBLIC_URL") or web_cfg.get("public_url") or "").rstrip("/") or None

        secret = os.getenv("SESSION_SECRET")
        if not secret:
            secret = secrets.token_urlsafe(48)
            logger.warning("SESSION_SECRET non impostata: le sessioni della dashboard si perdono a ogni riavvio")
        self.signer = Signer(secret)
        backend = getattr(bot, "db", None)
        self.sessions = SessionStore(backend=backend if hasattr(backend, "load_web_sessions") else None)

    # ----- configurazione -----
    @property
    def client_id(self) -> Optional[str]:
        if self._client_id:
            return self._client_id
        return str(self.bot.user.id) if self.bot.user else None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def base_url(self, request: Request) -> str:
        return self.public_url or str(request.base_url).rstrip("/")

    def redirect_uri(self, request: Request) -> str:
        return f"{self.base_url(request)}/auth/callback"

    def _cookie_secure(self, request: Request) -> bool:
        return self.base_url(request).startswith("https://")

    async def owner_id(self) -> Optional[int]:
        """ID del proprietario: da OWNER_ID, altrimenti il proprietario dell'app Discord"""
        if self._owner_id:
            return self._owner_id
        env = (os.getenv("OWNER_ID") or "").strip()
        if env.isdigit():
            self._owner_id = int(env)
            return self._owner_id
        try:
            info = await self.bot.application_info()
            self._owner_id = int(info.owner.id)
        except Exception:
            logger.warning("Impossibile determinare il proprietario del bot", exc_info=True)
        return self._owner_id

    # ----- OAuth -----
    def authorize_url(self, request: Request, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri(request),
                "response_type": "code",
                "scope": "identify guilds",
                "state": state,
                "prompt": "none",
            },
            quote_via=quote,
        )
        return f"{DISCORD_API.replace('/api/v10', '')}/oauth2/authorize?{query}"

    async def fetch_identity(self, code: str, redirect_uri: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                token_res = await client.post(
                    f"{DISCORD_API}/oauth2/token",
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
                if token_res.status_code != 200:
                    raise AuthError(f"scambio del codice rifiutato ({token_res.status_code})")
                access_token = token_res.json().get("access_token")
                if not access_token:
                    raise AuthError("token mancante")

                headers = {"Authorization": f"Bearer {access_token}"}
                user_res = await client.get(f"{DISCORD_API}/users/@me", headers=headers)
                guilds_res = await client.get(f"{DISCORD_API}/users/@me/guilds", headers=headers)
                if user_res.status_code != 200 or guilds_res.status_code != 200:
                    raise AuthError("lettura profilo non riuscita")
                user, guilds = user_res.json(), guilds_res.json()

                # Il token non serve più: revoca (best effort)
                try:
                    await client.post(
                        f"{DISCORD_API}/oauth2/token/revoke",
                        data={
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "token": access_token,
                            "token_type_hint": "access_token",
                        },
                    )
                except Exception:
                    pass
        except httpx.HTTPError as exc:
            raise AuthError(f"errore di rete: {type(exc).__name__}") from exc

        if not isinstance(user, dict) or not _ID_RE.match(str(user.get("id", ""))) or not isinstance(guilds, list):
            raise AuthError("risposta inattesa da Discord")
        return user, guilds

    def build_session_data(self, user: Dict[str, Any], guilds: List[Dict[str, Any]]) -> Dict[str, Any]:
        uid = str(user["id"])
        snapshot = []
        for g in guilds:
            gid = str(g.get("id", ""))
            if not _ID_RE.match(gid):
                continue
            try:
                perms = int(g.get("permissions", 0))
            except (TypeError, ValueError):
                perms = 0
            snapshot.append(
                {
                    "id": gid,
                    "name": str(g.get("name", ""))[:100],
                    "icon_url": icon_url(gid, g.get("icon")),
                    "owner": bool(g.get("owner")),
                    "can_manage": bool(g.get("owner")) or bool(perms & (PERM_ADMINISTRATOR | PERM_MANAGE_GUILD)),
                }
            )
        return {
            "user": {
                "id": uid,
                "name": str(user.get("global_name") or user.get("username") or "Utente")[:64],
                "avatar_url": avatar_url(uid, user.get("avatar")),
            },
            "guilds": snapshot,
            "csrf": secrets.token_urlsafe(32),
        }

    # ----- sessioni -----
    def start_session(self, response, request: Request, data: Dict[str, Any]) -> None:
        sid = self.sessions.create(data, SESSION_TTL)
        response.set_cookie(
            SESSION_COOKIE,
            self.signer.sign({"sid": sid}, SESSION_TTL),
            max_age=SESSION_TTL,
            httponly=True,
            samesite="lax",
            secure=self._cookie_secure(request),
            path="/",
        )

    def _sid_from_request(self, request: Request) -> Optional[str]:
        payload = self.signer.verify(request.cookies.get(SESSION_COOKIE))
        return payload.get("sid") if payload else None

    def session_data(self, request: Request) -> Optional[Dict[str, Any]]:
        return self.sessions.get(self._sid_from_request(request))

    def end_session(self, request: Request, response) -> None:
        self.sessions.destroy(self._sid_from_request(request))
        response.delete_cookie(SESSION_COOKIE, path="/")

    # ----- dipendenze FastAPI -----
    def require_user(self, request: Request) -> Dict[str, Any]:
        data = self.session_data(request)
        if not data:
            raise HTTPException(status_code=401, detail="Non autenticato")
        return data

    def require_user_csrf(self, request: Request) -> Dict[str, Any]:
        """Per le richieste che modificano dati: sessione valida + token CSRF"""
        data = self.require_user(request)
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not secrets.compare_digest(sent, data["csrf"]):
            raise HTTPException(status_code=403, detail="Token CSRF non valido")
        return data

    async def is_owner(self, data: Dict[str, Any]) -> bool:
        owner = await self.owner_id()
        return owner is not None and int(data["user"]["id"]) == owner

    async def require_owner(self, request: Request) -> Dict[str, Any]:
        data = self.require_user(request)
        if not await self.is_owner(data):
            raise HTTPException(status_code=403, detail="Solo il proprietario del bot")
        return data

    async def require_owner_csrf(self, request: Request) -> Dict[str, Any]:
        data = self.require_user_csrf(request)
        if not await self.is_owner(data):
            raise HTTPException(status_code=403, detail="Solo il proprietario del bot")
        return data

    async def can_manage_guild(self, data: Dict[str, Any], guild) -> bool:
        """Controllo permessi in tempo reale quando il membro è in cache, altrimenti l'elenco del login"""
        uid = int(data["user"]["id"])
        if await self.is_owner(data):
            return True
        member = guild.get_member(uid)
        if member is not None:
            perms = member.guild_permissions
            return bool(perms.administrator or perms.manage_guild)
        if guild.owner_id == uid:
            return True
        return any(g["id"] == str(guild.id) and g["can_manage"] for g in data["guilds"])

    async def guild_access(self, data: Dict[str, Any], guild_id: int):
        """Restituisce il server se il bot c'è e l'utente lo può gestire"""
        guild = self.bot.get_guild(guild_id)
        if guild is None or not await self.can_manage_guild(data, guild):
            # stessa risposta nei due casi: non riveliamo in quali server c'è il bot
            raise HTTPException(status_code=404, detail="Server non trovato")
        return guild

    # ----- rotte -----
    def install(self, app: FastAPI) -> None:
        auth = self

        @app.get("/auth/login", include_in_schema=False)
        async def login(request: Request):
            if not auth.configured:
                return RedirectResponse("/dashboard?error=not_configured")
            if not auth.public_url and not auth._warned_public_url:
                auth._warned_public_url = True
                logger.warning(
                    "PUBLIC_URL non impostata: l'indirizzo di ritorno del login viene dedotto dalla richiesta (%s) "
                    "e dietro a un proxy può non coincidere con quello registrato nel portale Discord",
                    auth.redirect_uri(request),
                )
            state = secrets.token_urlsafe(24)
            response = RedirectResponse(auth.authorize_url(request, state))
            response.set_cookie(
                STATE_COOKIE,
                auth.signer.sign({"s": state}, STATE_TTL),
                max_age=STATE_TTL,
                httponly=True,
                samesite="lax",
                secure=auth._cookie_secure(request),
                path="/auth",
            )
            return response

        @app.get("/auth/callback", include_in_schema=False)
        async def callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
            def fail(reason: str):
                resp = RedirectResponse(f"/dashboard?error={reason}")
                resp.delete_cookie(STATE_COOKIE, path="/auth")
                return resp

            if error:
                return fail("denied")
            payload = auth.signer.verify(request.cookies.get(STATE_COOKIE))
            if not code or not state or not payload or not secrets.compare_digest(str(payload.get("s", "")), state):
                return fail("invalid_state")
            if not auth.configured:
                return fail("not_configured")

            try:
                user, guilds = await auth.fetch_identity(code, auth.redirect_uri(request))
            except AuthError as exc:
                logger.warning("Login Discord non riuscito: %s", exc)
                return fail("login_failed")

            response = RedirectResponse("/dashboard")
            response.delete_cookie(STATE_COOKIE, path="/auth")
            auth.start_session(response, request, auth.build_session_data(user, guilds))
            logger.info("Login dashboard: utente %s", user.get("id"))
            return response

        @app.post("/auth/logout", include_in_schema=False)
        async def logout(request: Request):
            auth.require_user_csrf(request)
            response = JSONResponse({"ok": True})
            auth.end_session(request, response)
            return response
