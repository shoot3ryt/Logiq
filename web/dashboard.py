"""
API della dashboard: pannello per server (utenti con permessi) e admin panel (proprietario)
"""

import asyncio
import logging
import platform
import re
import time
from datetime import datetime, timedelta, timezone
from importlib import metadata
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from utils.modules import TOGGLEABLE_MODULES
from .auth import WebAuth
from .common import invite_url

logger = logging.getLogger(__name__)

TEXT_SETTINGS = ("log_channel", "welcome_channel", "verify_channel", "ticket_log_channel")
CATEGORY_SETTINGS = ("ticket_category",)
VOICE_SETTINGS = ("temp_voice_creator",)
ROLE_SETTINGS = ("verified_role", "support_role")
NUMERIC_SETTINGS = ("xp_per_message", "xp_cooldown", "daily_reward")
CURRENCY_SETTINGS = ("currency_name", "currency_symbol")


class SettingsUpdate(BaseModel):
    """Campi modificabili dal pannello. Un campo presente con valore null viene azzerato."""

    model_config = ConfigDict(extra="forbid")

    log_channel: Optional[str] = None
    welcome_channel: Optional[str] = None
    verify_channel: Optional[str] = None
    ticket_log_channel: Optional[str] = None
    ticket_category: Optional[str] = None
    temp_voice_creator: Optional[str] = None
    verified_role: Optional[str] = None
    support_role: Optional[str] = None
    verification_type: Optional[Literal["button", "captcha"]] = None
    verification_method: Optional[Literal["dm", "channel"]] = None
    welcome_message: Optional[str] = Field(default=None, max_length=1000)
    xp_per_message: Optional[int] = Field(default=None, ge=1, le=1000)
    xp_cooldown: Optional[int] = Field(default=None, ge=0, le=3600)
    daily_reward: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    currency_name: Optional[str] = Field(default=None, max_length=32)
    currency_symbol: Optional[str] = Field(default=None, max_length=8)
    modules: Optional[Dict[str, bool]] = None


class RoleMenuCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    channel_id: str
    role_ids: List[str] = Field(min_length=1, max_length=25)
    exclusive: bool = False


class PanelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str


class TicketClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = Field(default=None, max_length=200)


_background_tasks: set = set()

MENU_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _as_id(value: str, field: str) -> int:
    if not isinstance(value, str) or not value.isdigit() or len(value) > 25:
        raise HTTPException(status_code=422, detail=f"{field}: ID non valido")
    return int(value)


def _clean_name(name: str) -> str:
    return str(name)[:100]


def _guild_summary(guild) -> Dict[str, Any]:
    return {
        "id": str(guild.id),
        "name": _clean_name(guild.name),
        "icon_url": str(guild.icon.url) if guild.icon else None,
        "member_count": guild.member_count or 0,
    }


def _channels(guild) -> Dict[str, List[Dict[str, str]]]:
    def items(seq):
        return [{"id": str(c.id), "name": _clean_name(c.name)} for c in sorted(seq, key=lambda c: c.position)]

    return {
        "text": items(guild.text_channels),
        "voice": items(guild.voice_channels),
        "category": items(guild.categories),
    }


def _roles(guild) -> List[Dict[str, Any]]:
    me = guild.me
    top = me.top_role if me else None
    result = []
    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if role.is_default() or role.managed:
            continue
        result.append(
            {
                "id": str(role.id),
                "name": _clean_name(role.name),
                "assignable": bool(top is not None and role < top),
            }
        )
    return result


def _days_series(events: List[Dict[str, Any]], start_day: int, days: int) -> List[int]:
    series = [0] * days
    for event in events:
        ts = event.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        index = int(ts // 86400) - start_day
        if 0 <= index < days:
            series[index] += 1
    return series


def install(app: FastAPI, bot, auth: WebAuth) -> None:
    user_dep = Depends(auth.require_user)
    csrf_dep = Depends(auth.require_user_csrf)
    owner_dep = Depends(auth.require_owner)
    owner_csrf_dep = Depends(auth.require_owner_csrf)

    # ---------- profilo e elenco server ----------
    @app.get("/api/me")
    async def me(session: Dict[str, Any] = user_dep):
        is_owner = await auth.is_owner(session)
        client_id = auth.client_id
        listed: Dict[str, Dict[str, Any]] = {}

        for g in session["guilds"]:
            gid = int(g["id"])
            guild = bot.get_guild(gid)
            present = guild is not None
            allowed = g["can_manage"]
            if present:
                allowed = await auth.can_manage_guild(session, guild)
            if not allowed:
                continue
            listed[g["id"]] = {
                "id": g["id"],
                "name": g["name"],
                "icon_url": g["icon_url"],
                "bot_present": present,
                "invite_url": None if present else invite_url(client_id, g["id"]),
            }

        if is_owner:  # il proprietario vede anche i server in cui il bot c'è ma lui non è admin
            for guild in bot.guilds:
                listed.setdefault(
                    str(guild.id),
                    {
                        "id": str(guild.id),
                        "name": _clean_name(guild.name),
                        "icon_url": str(guild.icon.url) if guild.icon else None,
                        "bot_present": True,
                        "invite_url": None,
                    },
                )

        servers = sorted(listed.values(), key=lambda s: (not s["bot_present"], s["name"].lower()))
        return {
            "user": session["user"],
            "csrf": session["csrf"],
            "is_owner": is_owner,
            "guilds": servers,
        }

    # ---------- panoramica server ----------
    @app.get("/api/guilds/{guild_id}/overview")
    async def guild_overview(guild_id: int, days: int = 7, session: Dict[str, Any] = user_dep):
        guild = await auth.guild_access(session, guild_id)
        days = max(1, min(days, 30))
        now = time.time()
        start = now - timedelta(days=days).total_seconds()
        start_day = int(start // 86400)
        # giorni coperti: da start_day a oggi incluso
        span = int(now // 86400) - start_day + 1

        async def series(event_type: str) -> List[int]:
            events = await bot.db.get_analytics(guild.id, event_type=event_type, start_time=start, end_time=now)
            return _days_series(events, start_day, span)

        leaderboard = []
        for i, entry in enumerate(await bot.db.get_leaderboard(guild.id, limit=10)):
            uid = int(entry["user_id"])
            member = guild.get_member(uid)
            leaderboard.append(
                {
                    "rank": i + 1,
                    "user_id": str(uid),
                    "name": _clean_name(member.display_name) if member else f"Utente …{str(uid)[-4:]}",
                    "avatar_url": str(member.display_avatar.url) if member else None,
                    "xp": int(entry.get("xp", 0)),
                    "level": int(entry.get("level", 0)),
                }
            )

        return {
            "guild": _guild_summary(guild),
            "stats": {
                "members": guild.member_count or 0,
                "channels": len(guild.channels),
                "roles": len(guild.roles),
                "boosts": getattr(guild, "premium_subscription_count", 0) or 0,
            },
            "activity": {
                "start_day": start_day,
                "messages": await series("message"),
                "joins": await series("member_join"),
                "leaves": await series("member_leave"),
            },
            "leaderboard": leaderboard,
        }

    # ---------- impostazioni server ----------
    @app.get("/api/guilds/{guild_id}/settings")
    async def get_settings(guild_id: int, session: Dict[str, Any] = user_dep):
        guild = await auth.guild_access(session, guild_id)
        doc = await bot.db.get_guild(guild.id) or {}
        stored = doc.get("settings") or {}
        overrides = doc.get("modules") or {}
        global_cfg = bot.config.get("modules", {})

        def ident(key: str) -> Optional[str]:
            value = doc.get(key)
            return str(value) if value else None

        values: Dict[str, Any] = {key: ident(key) for key in TEXT_SETTINGS + CATEGORY_SETTINGS + VOICE_SETTINGS + ROLE_SETTINGS}
        values["verification_type"] = doc.get("verification_type") or "button"
        values["verification_method"] = doc.get("verification_method") or "dm"
        values["welcome_message"] = doc.get("welcome_message")
        defaults = {
            "xp_per_message": global_cfg.get("leveling", {}).get("xp_per_message", 10),
            "xp_cooldown": global_cfg.get("leveling", {}).get("xp_cooldown", 60),
            "daily_reward": global_cfg.get("economy", {}).get("daily_reward", 100),
            "currency_name": global_cfg.get("economy", {}).get("currency_name", "ProgrammiCoin"),
            "currency_symbol": global_cfg.get("economy", {}).get("currency_symbol", "💎"),
        }
        for key in NUMERIC_SETTINGS + CURRENCY_SETTINGS:
            values[key] = stored.get(key)

        modules = []
        for key in TOGGLEABLE_MODULES:
            locked = not bot.module_globally_enabled(key)
            override = overrides.get(key)
            modules.append(
                {
                    "key": key,
                    "locked": locked,
                    "enabled": (not locked) and (override if isinstance(override, bool) else True),
                }
            )

        return {
            "guild": _guild_summary(guild),
            "values": values,
            "defaults": defaults,
            "modules": modules,
            "channels": _channels(guild),
            "roles": _roles(guild),
        }

    @app.put("/api/guilds/{guild_id}/settings")
    async def put_settings(guild_id: int, body: SettingsUpdate, session: Dict[str, Any] = csrf_dep):
        guild = await auth.guild_access(session, guild_id)
        provided = body.model_fields_set
        if not provided:
            raise HTTPException(status_code=422, detail="Nessuna modifica")

        updates: Dict[str, Any] = {}

        def resolve_channel(field: str, pool: Dict[int, Any]):
            raw = getattr(body, field)
            if raw is None:
                updates[field] = None
                return
            channel_id = _as_id(raw, field)
            if channel_id not in pool:
                raise HTTPException(status_code=422, detail=f"{field}: canale non valido per questo server")
            updates[field] = channel_id

        text_pool = {c.id: c for c in guild.text_channels}
        voice_pool = {c.id: c for c in guild.voice_channels}
        category_pool = {c.id: c for c in guild.categories}

        for field in TEXT_SETTINGS:
            if field in provided:
                resolve_channel(field, text_pool)
        for field in CATEGORY_SETTINGS:
            if field in provided:
                resolve_channel(field, category_pool)
        for field in VOICE_SETTINGS:
            if field in provided:
                resolve_channel(field, voice_pool)

        for field in ROLE_SETTINGS:
            if field in provided:
                raw = getattr(body, field)
                if raw is None:
                    updates[field] = None
                    continue
                role = guild.get_role(_as_id(raw, field))
                if role is None or role.is_default() or role.managed:
                    raise HTTPException(status_code=422, detail=f"{field}: ruolo non valido per questo server")
                updates[field] = role.id

        for field in ("verification_type", "verification_method", "welcome_message"):
            if field in provided:
                value = getattr(body, field)
                if field == "welcome_message" and value is not None and not value.strip():
                    value = None
                updates[field] = value

        for field in NUMERIC_SETTINGS:
            if field in provided:
                updates[f"settings.{field}"] = getattr(body, field)

        for field in CURRENCY_SETTINGS:
            if field in provided:
                value = getattr(body, field)
                if value is not None:
                    value = "".join(ch for ch in value if ch.isprintable()).strip()
                updates[f"settings.{field}"] = value or None

        if "modules" in provided and body.modules is not None:
            unknown = [m for m in body.modules if m not in TOGGLEABLE_MODULES]
            if unknown:
                raise HTTPException(status_code=422, detail=f"Modulo sconosciuto: {unknown[0]}")
            for module, enabled in body.modules.items():
                updates[f"modules.{module}"] = bool(enabled)

        if not updates:
            raise HTTPException(status_code=422, detail="Nessuna modifica")

        if not await bot.db.get_guild(guild.id):
            await bot.db.create_guild(guild.id)
        await bot.db.update_guild(guild.id, updates)
        bot.invalidate_guild(guild.id)

        logger.info(
            "Dashboard: utente %s ha modificato %s nel server %s",
            session["user"]["id"], sorted(updates.keys()), guild.id,
        )
        return {"ok": True, "updated": sorted(updates.keys())}

    # ---------- ruoli a tendina e ticket ----------
    def cog_or_503(name: str):
        cog = bot.get_cog(name)
        if cog is None:
            raise HTTPException(status_code=503, detail="Questo modulo non è attivo sul bot")
        return cog

    def postable_channel(guild, raw: str):
        channel = next((c for c in guild.text_channels if c.id == _as_id(raw, "channel_id")), None)
        if channel is None:
            raise HTTPException(status_code=422, detail="Canale non valido per questo server")
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise HTTPException(status_code=422, detail="Il bot non può scrivere con embed in quel canale")
        return channel

    def jump_url(guild_id, channel_id, message_id) -> Optional[str]:
        if not (guild_id and channel_id and message_id):
            return None
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    @app.get("/api/guilds/{guild_id}/role-menus")
    async def list_role_menus(guild_id: int, session: Dict[str, Any] = user_dep):
        guild = await auth.guild_access(session, guild_id)
        menus = []
        for doc in await bot.db.get_role_menus(guild.id):
            channel = guild.get_channel(doc.get("channel_id") or 0)
            roles = [guild.get_role(rid) for rid in doc.get("role_ids", [])]
            menus.append(
                {
                    "menu_id": doc.get("menu_id"),
                    "title": _clean_name(doc.get("title", "")),
                    "exclusive": bool(doc.get("exclusive")),
                    "channel": _clean_name(channel.name) if channel else None,
                    "roles": [_clean_name(r.name) for r in roles if r],
                    "created_at": doc.get("created_at"),
                    "url": jump_url(guild.id, doc.get("channel_id"), doc.get("message_id")),
                }
            )
        return {"guild": _guild_summary(guild), "menus": menus, "channels": _channels(guild)["text"], "roles": _roles(guild)}

    @app.post("/api/guilds/{guild_id}/role-menus")
    async def create_role_menu(guild_id: int, body: RoleMenuCreate, session: Dict[str, Any] = csrf_dep):
        guild = await auth.guild_access(session, guild_id)
        cog = cog_or_503("Roles")
        channel = postable_channel(guild, body.channel_id)

        role_list = []
        seen = set()
        top = guild.me.top_role if guild.me else None
        for raw in body.role_ids:
            role_id = _as_id(raw, "role_ids")
            if role_id in seen:
                continue
            seen.add(role_id)
            role = guild.get_role(role_id)
            if role is None or role.is_default() or role.managed:
                raise HTTPException(status_code=422, detail="Ruolo non valido per questo server")
            if top is None or not role < top:
                raise HTTPException(status_code=422, detail=f"Il ruolo del bot deve stare sopra a «{_clean_name(role.name)}»")
            role_list.append({"role": role, "emoji": role.unicode_emoji or "🎭", "label": role.name[:100]})

        try:
            doc = await cog.post_menu(
                channel, body.title.strip(), (body.description or "").strip() or None,
                role_list, body.exclusive, int(session["user"]["id"]),
            )
        except Exception as exc:
            logger.warning("Dashboard: pubblicazione menu ruoli fallita: %s", exc)
            raise HTTPException(status_code=502, detail="Discord ha rifiutato il messaggio")
        logger.info("Dashboard: utente %s ha creato un menu ruoli nel server %s", session["user"]["id"], guild.id)
        return {"ok": True, "menu_id": doc["menu_id"], "url": doc.get("jump_url")}

    @app.delete("/api/guilds/{guild_id}/role-menus/{menu_id}")
    async def delete_role_menu(guild_id: int, menu_id: str, session: Dict[str, Any] = csrf_dep):
        guild = await auth.guild_access(session, guild_id)
        if not MENU_ID_RE.match(menu_id):
            raise HTTPException(status_code=404, detail="Menu non trovato")
        doc = next((d for d in await bot.db.get_role_menus(guild.id) if d.get("menu_id") == menu_id), None)
        if doc is None:
            raise HTTPException(status_code=404, detail="Menu non trovato")
        channel = guild.get_channel(doc.get("channel_id") or 0)
        if channel is not None:
            try:
                await channel.get_partial_message(doc["message_id"]).delete()
            except Exception:
                pass  # messaggio già eliminato o permessi mancanti: il menu esce comunque dall'elenco
        await bot.db.delete_role_menu(guild.id, menu_id)
        logger.info("Dashboard: utente %s ha eliminato il menu ruoli %s (server %s)", session["user"]["id"], menu_id, guild.id)
        return {"ok": True}

    @app.get("/api/guilds/{guild_id}/tickets")
    async def list_tickets(guild_id: int, session: Dict[str, Any] = user_dep):
        guild = await auth.guild_access(session, guild_id)
        doc = await bot.db.get_guild(guild.id) or {}
        tickets = []
        for row in await bot.db.list_tickets(guild.id, 50):
            channel = guild.get_channel(row.get("channel_id") or 0)
            member = guild.get_member(row.get("user_id") or 0)
            tickets.append(
                {
                    "channel_id": str(row.get("channel_id")),
                    "channel": _clean_name(channel.name) if channel else None,
                    "user": _clean_name(member.display_name) if member else None,
                    "status": "closed" if row.get("status") == "closed" else "open",
                    "created_at": row.get("created_at"),
                    "closed_at": row.get("closed_at"),
                    "reason": str(row.get("close_reason") or "")[:200] or None,
                    "url": f"https://discord.com/channels/{guild.id}/{channel.id}" if channel else None,
                }
            )
        category = guild.get_channel(doc.get("ticket_category") or 0)
        return {
            "guild": _guild_summary(guild),
            "tickets": tickets,
            "configured": bool(category),
            "channels": _channels(guild)["text"],
        }

    @app.post("/api/guilds/{guild_id}/tickets/panel")
    async def send_ticket_panel(guild_id: int, body: PanelCreate, session: Dict[str, Any] = csrf_dep):
        guild = await auth.guild_access(session, guild_id)
        cog = cog_or_503("Tickets")
        channel = postable_channel(guild, body.channel_id)
        try:
            message = await cog.send_panel(channel)
        except Exception as exc:
            logger.warning("Dashboard: pannello ticket non inviato: %s", exc)
            raise HTTPException(status_code=502, detail="Discord ha rifiutato il messaggio")
        logger.info("Dashboard: utente %s ha pubblicato il pannello ticket nel server %s", session["user"]["id"], guild.id)
        return {"ok": True, "url": jump_url(guild.id, channel.id, message.id)}

    @app.post("/api/guilds/{guild_id}/tickets/{channel_id}/close")
    async def close_ticket(guild_id: int, channel_id: int, body: TicketClose, session: Dict[str, Any] = csrf_dep):
        guild = await auth.guild_access(session, guild_id)
        # Solo canali registrati come ticket aperti di QUESTO server: mai un canale qualsiasi
        row = await bot.db.get_open_ticket_by_channel(guild.id, channel_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Ticket aperto non trovato")
        user = session["user"]
        reason = (body.reason or "").strip() or "Closed from the dashboard"
        channel = guild.get_channel(channel_id)
        if channel is None:
            await bot.db.close_ticket_by_channel(channel_id, int(user["id"]), reason)
            return {"ok": True, "channel_deleted": True}

        cog = cog_or_503("Tickets")

        async def run():
            try:
                await cog.close_ticket_channel(channel, f"{user['name']} (dashboard)", int(user["id"]), reason)
            except Exception as exc:
                logger.error("Dashboard: chiusura ticket %s fallita: %s", channel_id, exc)

        task = asyncio.create_task(run())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        logger.info("Dashboard: utente %s chiude il ticket %s (server %s)", user["id"], channel_id, guild.id)
        return {"ok": True}

    # ---------- admin panel (solo proprietario) ----------
    @app.get("/api/admin/overview")
    async def admin_overview(session: Dict[str, Any] = owner_dep):
        user = bot.user
        now_uptime = 0
        start = getattr(bot, "start_time", None)
        if start:
            now_uptime = int((datetime.now(timezone.utc) - start).total_seconds())

        try:
            discord_py = metadata.version("discord.py")
        except Exception:
            discord_py = "?"

        guilds = []
        for guild in sorted(bot.guilds, key=lambda g: g.member_count or 0, reverse=True):
            joined = guild.me.joined_at.isoformat() if guild.me and guild.me.joined_at else None
            guilds.append(
                {
                    "id": str(guild.id),
                    "name": _clean_name(guild.name),
                    "icon_url": str(guild.icon.url) if guild.icon else None,
                    "members": guild.member_count or 0,
                    "owner_id": str(guild.owner_id),
                    "joined_at": joined,
                }
            )

        return {
            "bot": {
                "name": user.name if user else "Logiq",
                "id": str(user.id) if user else None,
                "avatar_url": str(user.display_avatar.url) if user else None,
                "ready": bot.is_ready(),
                "latency_ms": round(bot.latency * 1000) if bot.is_ready() else None,
                "uptime_seconds": now_uptime,
                "guilds": len(bot.guilds),
                "users": sum((g.member_count or 0) for g in bot.guilds),
                "commands": len(bot.tree.get_commands()),
            },
            "database": {
                "connected": bool(getattr(bot.db, "is_connected", False)),
                "name": getattr(bot.db, "database_name", None),
            },
            "guilds": guilds,
            "modules": [
                {"key": key, "enabled": bot.module_globally_enabled(key)} for key in TOGGLEABLE_MODULES
            ],
            "system": {"python": platform.python_version(), "discord_py": discord_py},
        }

    @app.post("/api/admin/guilds/{guild_id}/leave")
    async def admin_leave_guild(guild_id: int, session: Dict[str, Any] = owner_csrf_dep):
        guild = bot.get_guild(guild_id)
        if guild is None:
            raise HTTPException(status_code=404, detail="Server non trovato")
        name = guild.name
        await guild.leave()
        logger.warning("Admin panel: il bot è uscito dal server %s (%s) su richiesta di %s", guild_id, name, session["user"]["id"])
        return {"ok": True}
