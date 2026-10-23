"""
FastAPI Web Dashboard for Logiq
Homepage pubblica, login con Discord, pannello per server e admin panel del proprietario
"""

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from datetime import datetime, timedelta, timezone
import logging
import os
import time

from . import dashboard
from .auth import WebAuth
from .common import INVITE_PERMISSIONS, invite_url  # noqa: F401  (INVITE_PERMISSIONS riesportato)

logger = logging.getLogger(__name__)

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

# Le pagine sono servite da questo stesso dominio: niente risorse esterne tranne le immagini di Discord
PAGE_CSP = (
    "default-src 'self'; "
    "img-src 'self' https://cdn.discordapp.com data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


def _read_template(name: str, fallback: str) -> str:
    """Leggi un template HTML dalla cartella web/templates"""
    path = os.path.join(TEMPLATE_DIR, name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    logger.warning("Template %s non trovato in %s", name, TEMPLATE_DIR)
    return fallback


def _page(name: str, fallback: str) -> HTMLResponse:
    return HTMLResponse(_read_template(name, fallback), headers={"Content-Security-Policy": PAGE_CSP})


def _format_uptime(seconds: int) -> str:
    """Formatta una durata in secondi come '2g 3h 14m'"""
    days, rest = divmod(max(seconds, 0), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days}g")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _uptime_seconds(bot) -> int:
    start = getattr(bot, "start_time", None)
    if not start:
        return 0
    return int((datetime.now(timezone.utc) - start).total_seconds())


def create_app(bot, auth_transport=None) -> FastAPI:
    """Create FastAPI application"""

    app = FastAPI(
        title="Logiq API",
        description="REST API for Logiq Discord Bot",
        version="2.0.0"
    )

    auth = WebAuth(bot, transport=auth_transport)
    owner_only = Depends(auth.require_owner)

    @app.on_event("startup")
    async def restore_sessions():
        backend = getattr(bot, "db", None)
        if auth.sessions._backend is None:
            return
        try:
            await backend.ensure_web_indexes()
        except Exception as exc:
            logging.getLogger(__name__).warning("Indice sessioni non creato: %s", exc)
        count = await auth.sessions.load()
        if count:
            logging.getLogger(__name__).info("Ripristinate %d sessioni della dashboard", count)

    # CORS middleware
    cors_origins = bot.config.get('web', {}).get('cors_origins', ['http://localhost:3000'])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith(("/api/", "/auth/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ---------- pagine ----------
    @app.get("/", response_class=HTMLResponse)
    async def root():
        """Homepage pubblica"""
        return _page("index.html", "<h1>Logiq</h1><p>Homepage non trovata (web/templates/index.html).</p>")

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page():
        """Dashboard (login con Discord)"""
        return _page("dashboard.html", "<h1>Dashboard</h1><p>Pagina non trovata (web/templates/dashboard.html).</p>")

    @app.get("/admin", include_in_schema=False)
    async def admin_dashboard():
        """Vecchio indirizzo dell'admin panel"""
        return RedirectResponse("/dashboard#/admin")

    @app.get("/invite")
    async def invite():
        """Reindirizza al link di invito del bot"""
        url = invite_url(auth.client_id)
        if not url:
            raise HTTPException(status_code=503, detail="Il bot si sta ancora avviando, riprova tra qualche secondo")
        return RedirectResponse(url)

    # ---------- dati pubblici (solo numeri aggregati) ----------
    @app.get("/api/public")
    async def public_info():
        user = bot.user
        uptime = _uptime_seconds(bot)
        return {
            "ready": bot.is_ready(),
            "name": user.name if user else "Logiq",
            "avatar_url": str(user.display_avatar.url) if user else None,
            "invite_url": invite_url(auth.client_id),
            "guilds": len(bot.guilds),
            "users": sum((g.member_count or 0) for g in bot.guilds),
            "commands": len(bot.tree.get_commands()),
            "latency_ms": round(bot.latency * 1000) if bot.is_ready() else None,
            "uptime_seconds": uptime,
            "uptime": _format_uptime(uptime),
        }

    @app.get("/stats")
    async def get_stats():
        """Get bot statistics"""
        uptime = _uptime_seconds(bot)
        return {
            "guilds": len(bot.guilds),
            "users": sum((g.member_count or 0) for g in bot.guilds),
            "channels": sum(len(g.channels) for g in bot.guilds),
            "uptime": _format_uptime(uptime),
            "uptime_seconds": uptime,
            "latency": round(bot.latency * 1000) if bot.is_ready() else None
        }

    @app.get("/health")
    async def health_check():
        """Health check endpoint"""
        db_connected = bot.db.is_connected if hasattr(bot.db, 'is_connected') else False

        return {
            "status": "healthy" if bot.is_ready() and db_connected else "unhealthy",
            "bot_ready": bot.is_ready(),
            "database_connected": db_connected,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    # ---------- login e dashboard ----------
    auth.install(app)
    dashboard.install(app, bot, auth)

    # ---------- endpoint storici: ora solo per il proprietario ----------
    @app.get("/guilds")
    async def get_guilds(session=owner_only):
        """Get list of guilds"""
        return {
            "guilds": [
                {
                    "id": guild.id,
                    "name": guild.name,
                    "member_count": guild.member_count,
                    "icon_url": str(guild.icon.url) if guild.icon else None
                }
                for guild in bot.guilds
            ]
        }

    @app.get("/guilds/{guild_id}")
    async def get_guild(guild_id: int, session=owner_only):
        """Get guild details"""
        guild = bot.get_guild(guild_id)
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")

        guild_config = await bot.db.get_guild(guild_id)
        if guild_config:
            guild_config.pop("_id", None)

        return {
            "id": guild.id,
            "name": guild.name,
            "member_count": guild.member_count,
            "icon_url": str(guild.icon.url) if guild.icon else None,
            "owner_id": guild.owner_id,
            "created_at": guild.created_at.isoformat(),
            "config": guild_config
        }

    @app.get("/guilds/{guild_id}/leaderboard")
    async def get_guild_leaderboard(guild_id: int, limit: int = 10, session=owner_only):
        """Get guild XP leaderboard"""
        guild = bot.get_guild(guild_id)
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")

        limit = max(1, min(limit, 100))
        leaderboard = await bot.db.get_leaderboard(guild_id, limit=limit)

        return {
            "guild_id": guild_id,
            "leaderboard": [
                {
                    "rank": i + 1,
                    "user_id": entry['user_id'],
                    "xp": entry.get('xp', 0),
                    "level": entry.get('level', 0)
                }
                for i, entry in enumerate(leaderboard)
            ]
        }

    @app.get("/guilds/{guild_id}/analytics")
    async def get_guild_analytics(guild_id: int, days: int = 7, session=owner_only):
        """Get guild analytics"""
        guild = bot.get_guild(guild_id)
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")

        days = max(1, min(days, 90))
        end_time = time.time()
        start_time = end_time - timedelta(days=days).total_seconds()

        messages = await bot.db.get_analytics(guild_id, event_type='message', start_time=start_time, end_time=end_time)
        joins = await bot.db.get_analytics(guild_id, event_type='member_join', start_time=start_time, end_time=end_time)
        leaves = await bot.db.get_analytics(guild_id, event_type='member_leave', start_time=start_time, end_time=end_time)

        return {
            "guild_id": guild_id,
            "period_days": days,
            "total_messages": len(messages),
            "member_joins": len(joins),
            "member_leaves": len(leaves),
            "net_growth": len(joins) - len(leaves)
        }

    @app.get("/modules")
    async def get_modules(session=owner_only):
        """Get module status"""
        modules = bot.config.get('modules', {})
        return {
            "modules": {
                name: config.get('enabled', True)
                for name, config in modules.items()
            }
        }

    return app
