"""
Moduli attivabili per server e impostazioni per server.

- La configurazione globale (config.yaml) è l'interruttore generale scelto dal proprietario:
  se un modulo è disattivato lì, resta spento ovunque.
- Ogni server può poi spegnere i moduli che non vuole dalla dashboard (campo `modules` del
  documento del server) e cambiare alcuni valori numerici (campo `settings`).
"""

import time
import logging
from typing import Any, Dict, Optional, Tuple

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

# Moduli che un server può attivare/disattivare (chiave = nome del file in cogs/)
TOGGLEABLE_MODULES = (
    "moderation",
    "leveling",
    "economy",
    "verification",
    "roles",
    "tickets",
    "temp_voice",
    "music",
    "games",
    "giveaways",
    "analytics",
    "social_alerts",
    "ai_chat",
)

GUILD_CACHE_TTL = 30  # secondi


def module_of_command(command) -> Optional[str]:
    """Ricava il modulo di un comando slash dal file del cog in cui è definito"""
    callback = getattr(command, "callback", None)
    module_path = getattr(callback, "__module__", "") or ""
    if module_path.startswith("cogs."):
        name = module_path.split(".", 1)[1]
        if name in TOGGLEABLE_MODULES:
            return name
    return None


class ModuleDisabled(app_commands.CheckFailure):
    """Il modulo del comando è disattivato su questo server"""


class LogiqTree(app_commands.CommandTree):
    """CommandTree che blocca i comandi dei moduli disattivati nel server"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.command is None:
            return True

        module = module_of_command(interaction.command)
        if module is None:
            return True

        if await interaction.client.module_enabled(interaction.guild.id, module):
            return True

        await interaction.response.send_message(
            "⛔ Questo modulo è disattivato su questo server.", ephemeral=True
        )
        raise ModuleDisabled(module)

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, ModuleDisabled):
            return
        await super().on_error(interaction, error)


class GuildConfigMixin:
    """Metodi per leggere moduli e impostazioni per server, con una piccola cache"""

    def _init_guild_config(self) -> None:
        self._guild_cache: Dict[int, Tuple[float, Optional[Dict[str, Any]]]] = {}

    def invalidate_guild(self, guild_id: int) -> None:
        self._guild_cache.pop(int(guild_id), None)

    async def get_guild_doc(self, guild_id: int) -> Optional[Dict[str, Any]]:
        guild_id = int(guild_id)
        cached = self._guild_cache.get(guild_id)
        now = time.monotonic()
        if cached and now - cached[0] < GUILD_CACHE_TTL:
            return cached[1]

        doc = await self.db.get_guild(guild_id)
        self._guild_cache[guild_id] = (now, doc)
        return doc

    def module_globally_enabled(self, module: str) -> bool:
        return bool(self.config.get("modules", {}).get(module, {}).get("enabled", True))

    async def module_enabled(self, guild_id: int, module: str) -> bool:
        """Modulo attivo = acceso a livello globale E non spento dal server"""
        if not self.module_globally_enabled(module):
            return False
        try:
            doc = await self.get_guild_doc(guild_id)
        except Exception:
            # Se il database non risponde non blocchiamo il bot: vale l'impostazione globale
            logger.warning("Lettura impostazioni del server %s non riuscita", guild_id, exc_info=True)
            return True
        override = ((doc or {}).get("modules") or {}).get(module)
        return override if isinstance(override, bool) else True

    async def guild_setting(self, guild_id: int, key: str, default: Any) -> Any:
        """Valore numerico/impostazione per server, con valore di riserva globale"""
        try:
            doc = await self.get_guild_doc(guild_id)
        except Exception:
            return default
        value = ((doc or {}).get("settings") or {}).get(key)
        return default if value is None else value
