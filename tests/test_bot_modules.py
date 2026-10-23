"""
Test del lato bot: moduli attivabili per server, impostazioni per server (valuta inclusa),
menu dei ruoli e ticket che sopravvivono ai riavvii. Nessun Discord o database reali.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

from utils.modules import GuildConfigMixin, LogiqTree, ModuleDisabled, TOGGLEABLE_MODULES, module_of_command

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DB:
    def __init__(self):
        self.docs = {}
        self.calls = 0
        self.down = False

    async def get_guild(self, gid):
        self.calls += 1
        if self.down:
            raise RuntimeError("db giù")
        return self.docs.get(gid)


class Bot(GuildConfigMixin):
    def __init__(self):
        self._init_guild_config()
        self.db = DB()
        self.config = {"modules": {"leveling": {"enabled": True}, "economy": {"enabled": False}}}


@pytest.fixture
def bot():
    return Bot()


def run(coro):
    return asyncio.run(coro)


# ---------------- moduli per server ----------------
def test_default_e_override(bot):
    assert run(bot.module_enabled(1, "music")) is True
    assert run(bot.module_enabled(1, "economy")) is False  # spento a livello globale
    bot.db.docs[1] = {"modules": {"leveling": False, "economy": True}}
    bot.invalidate_guild(1)
    assert run(bot.module_enabled(1, "leveling")) is False
    assert run(bot.module_enabled(1, "economy")) is False  # True per server non scavalca lo spento globale
    assert run(bot.module_enabled(2, "leveling")) is True


def test_cache_e_db_giu(bot):
    bot.db.docs[1] = {"modules": {"leveling": False}}

    async def many():
        for _ in range(20):
            await bot.module_enabled(1, "leveling")

    run(many())
    assert bot.db.calls <= 1
    bot.db.down = True
    bot.invalidate_guild(1)
    assert run(bot.module_enabled(1, "leveling")) is True  # fail-open
    assert run(bot.guild_setting(1, "xp_per_message", 10)) == 10


def test_guild_setting(bot):
    bot.db.docs[3] = {"settings": {"xp_per_message": 40, "daily_reward": None, "currency_symbol": "🔥"}}
    assert run(bot.guild_setting(3, "xp_per_message", 10)) == 40
    assert run(bot.guild_setting(3, "daily_reward", 100)) == 100
    assert run(bot.guild_setting(3, "currency_symbol", "💎")) == "🔥"
    assert run(bot.guild_setting(99, "xp_per_message", 10)) == 10


# ---------------- comandi slash ----------------
def cmd(module_path):
    def cb():
        pass

    cb.__module__ = module_path
    return SimpleNamespace(callback=cb)


def test_modulo_del_comando():
    assert module_of_command(cmd("cogs.leveling")) == "leveling"
    assert module_of_command(cmd("cogs.admin")) is None
    assert module_of_command(cmd("cogs.utility")) is None
    assert module_of_command(cmd("altro.modulo")) is None
    assert all(os.path.exists(os.path.join(ROOT, "cogs", f"{m}.py")) for m in TOGGLEABLE_MODULES)


def test_interaction_check(bot):
    bot.db.docs[1] = {"modules": {"leveling": False}}

    class Resp:
        sent = None

        async def send_message(self, text, ephemeral=False):
            self.sent = (text, ephemeral)

    def interaction(path, gid=1):
        return SimpleNamespace(guild=SimpleNamespace(id=gid) if gid else None, command=cmd(path), client=bot, response=Resp())

    async def go():
        i = interaction("cogs.leveling", 1)
        with pytest.raises(ModuleDisabled):
            await LogiqTree.interaction_check(None, i)
        assert i.response.sent == ("⛔ Questo modulo è disattivato su questo server.", True)
        assert await LogiqTree.interaction_check(None, interaction("cogs.leveling", 2)) is True
        assert await LogiqTree.interaction_check(None, interaction("cogs.admin", 1)) is True
        assert await LogiqTree.interaction_check(None, interaction("cogs.leveling", None)) is True

    run(go())


# ---------------- valuta per server ----------------
def test_economia_usa_la_valuta_del_server(bot):
    from cogs.economy import Economy

    bot.config["modules"]["economy"] = {"enabled": True, "currency_symbol": "💎", "currency_name": "ProgrammiCoin"}
    cog = Economy(bot, bot.db, bot.config)
    bot.db.docs[1] = {"settings": {"currency_symbol": "🔥", "currency_name": "Fiamme"}}
    assert run(cog.currency(1)) == ("🔥", "Fiamme")
    assert run(cog.currency(2)) == ("💎", "ProgrammiCoin")


# ---------------- menu dei ruoli persistenti ----------------
class FakeRole(SimpleNamespace):
    def __init__(self, rid, name, emoji=None):
        super().__init__(id=rid, name=name, unicode_emoji=emoji, mention=f"<@&{rid}>")


class RolesBot:
    def __init__(self, guild, docs):
        self.guild = guild
        self.db = SimpleNamespace(get_all_role_menus=self._menus)
        self.docs = docs
        self.added = []
        self.loop = SimpleNamespace(create_task=lambda coro: coro.close())

    async def _menus(self):
        return self.docs

    async def wait_until_ready(self):
        pass

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def add_view(self, view, message_id=None):
        self.added.append((view, message_id))


def test_menu_ruoli_ricaricati_dopo_il_riavvio():
    from cogs.roles import Roles

    roles = {1: FakeRole(1, "Gamer", "🎮"), 2: FakeRole(2, "Artista")}
    guild = SimpleNamespace(id=10, get_role=roles.get)
    docs = [
        {"menu_id": "aaaaaaaaaaaa", "guild_id": 10, "message_id": 100, "title": "A", "exclusive": False, "role_ids": [1, 2]},
        {"menu_id": "bbbbbbbbbbbb", "guild_id": 10, "message_id": 200, "title": "B", "exclusive": True, "role_ids": [2, 999]},
        {"menu_id": "cccccccccccc", "guild_id": 10, "message_id": 300, "title": "C", "exclusive": False, "role_ids": [999]},   # ruoli tutti eliminati
        {"menu_id": "dddddddddddd", "guild_id": 77, "message_id": 400, "title": "D", "exclusive": False, "role_ids": [1]},     # server che il bot non vede più
    ]
    bot = RolesBot(guild, docs)

    async def go():
        cog = Roles(bot, bot.db, {})
        await cog._register_persistent_views()

    run(go())
    assert [m for _, m in bot.added] == [100, 200]
    ids = [v.children[0].custom_id for v, _ in bot.added]
    assert ids == ["rm:aaaaaaaaaaaa", "rm:bbbbbbbbbbbb"]  # un identificativo diverso per ogni menu
    assert [o.label for o in bot.added[0][0].children[0].options] == ["Gamer", "Artista"]
    assert [o.label for o in bot.added[1][0].children[0].options] == ["Artista"]  # ruolo eliminato escluso
    assert bot.added[0][0].children[0].max_values == 2 and bot.added[1][0].children[0].max_values == 1


def test_menu_ruoli_pubblicato_e_salvato():
    from cogs.roles import Roles

    saved, sent = [], []

    class Channel:
        id = 5
        guild = SimpleNamespace(id=10)

        async def send(self, embed=None, view=None):
            sent.append((embed, view))
            return SimpleNamespace(id=555, jump_url="https://discord.com/channels/10/5/555")

    async def create_role_menu(doc):
        saved.append(doc)

    bot = RolesBot(SimpleNamespace(id=10), [])
    bot.db = SimpleNamespace(create_role_menu=create_role_menu)

    async def go():
        cog = Roles(bot, bot.db, {})
        role = FakeRole(1, "Gamer", "🎮")
        return await cog.post_menu(Channel(), "Giochi", None, [{"role": role, "emoji": "🎮", "label": "Gamer"}], True, 42)

    doc = run(go())
    assert saved and saved[0]["message_id"] == 555 and saved[0]["role_ids"] == [1] and saved[0]["exclusive"] is True
    assert doc["jump_url"].endswith("/555") and len(saved[0]["menu_id"]) == 12
    assert sent[0][1].children[0].custom_id == f"rm:{saved[0]['menu_id']}"


# ---------------- ticket ----------------
def test_ticket_pulsanti_registrati_all_avvio():
    from cogs.tickets import Tickets

    bot = RolesBot(SimpleNamespace(id=1), [])
    cog = Tickets(bot, SimpleNamespace(), {})
    run(cog._register_persistent_views())
    assert sorted(v.children[0].custom_id for v, _ in bot.added) == ["close_ticket_btn", "create_ticket"]


def test_chiusura_ticket_dalla_dashboard(monkeypatch):
    import cogs.tickets as tickets
    from cogs.tickets import Tickets

    async def no_sleep(_):
        pass

    monkeypatch.setattr(tickets.asyncio, "sleep", no_sleep)
    events, logged = [], []

    class Chan:
        id = 5
        name = "ticket-mario"

        async def send(self, embed=None, **kw):
            events.append("avviso")

        async def delete(self, reason=None):
            events.append("eliminato")

    class LogChan:
        async def send(self, embed=None, **kw):
            logged.append(embed)

    chan = Chan()
    chan.guild = SimpleNamespace(id=10, get_channel=lambda cid: LogChan() if cid == 99 else None)

    class DB:
        async def get_guild(self, gid):
            return {"ticket_log_channel": 99}

        async def close_ticket_by_channel(self, channel_id, closed_by, reason):
            events.append(("db", channel_id, closed_by, reason))

    bot = RolesBot(SimpleNamespace(id=10), [])
    cog = Tickets(bot, DB(), {})
    run(cog.close_ticket_channel(chan, "Alice (dashboard)", 7, "Fatto"))
    assert events == ["avviso", ("db", 5, 7, "Fatto"), "eliminato"]
    assert len(logged) == 1


def test_database_sessioni_e_ticket_hanno_i_metodi_usati_dalla_dashboard():
    from database.db_manager import DatabaseManager

    for name in ("save_web_session", "delete_web_session", "load_web_sessions", "ensure_web_indexes",
                 "list_tickets", "get_open_ticket_by_channel", "close_ticket_by_channel",
                 "create_role_menu", "get_role_menus", "get_all_role_menus", "delete_role_menu"):
        assert callable(getattr(DatabaseManager, name))
