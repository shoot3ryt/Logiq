"""
Oggetti finti condivisi dai test della dashboard (nessun Discord e nessun database reali)
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx

from utils.modules import GuildConfigMixin

GA, GB, GC = 1000000000000100000, 1000000000000200000, 1000000000000300000
ALICE, BOB, OWNER = "111111111111111111", "222222222222222222", "555555555555555555"


# ---------------- finti oggetti Discord ----------------
class Role:
    def __init__(self, id, name, position, managed=False, default=False):
        self.id, self.name, self.position, self.managed, self._default = id, name, position, managed, default
        self.unicode_emoji = None

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position


class Chan:
    def __init__(self, id, name, position, can_send=True):
        self.id, self.name, self.position = id, name, position
        self.can_send = can_send
        self.deleted_messages = []

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=True, send_messages=self.can_send, embed_links=self.can_send)

    def get_partial_message(self, message_id):
        channel = self

        class _Partial:
            async def delete(self_inner):
                channel.deleted_messages.append(message_id)

        return _Partial()


class Member:
    def __init__(self, uid, name, manage=False):
        self.id = int(uid)
        self.display_name = name
        self.display_avatar = SimpleNamespace(url=f"https://cdn.discordapp.com/avatars/{uid}/x.png")
        self.guild_permissions = SimpleNamespace(administrator=False, manage_guild=manage)


class Guild:
    def __init__(self, gid, name, members):
        self.id, self.name = gid, name
        self.icon = None
        self.owner_id = 777
        self.member_count = len(members) + 5
        self.premium_subscription_count = 2
        self._members = {m.id: m for m in members}
        self.text_channels = [Chan(gid + 10, "generale", 1), Chan(gid + 11, "log", 2)]
        self.voice_channels = [Chan(gid + 20, "vocale", 3)]
        self.categories = [Chan(gid + 30, "Supporto", 0)]
        self.channels = self.text_channels + self.voice_channels + self.categories
        for ch in self.channels:
            ch.guild_id = gid
        everyone = Role(gid, "@everyone", 0, default=True)
        self.roles = [everyone, Role(gid + 40, "Membro", 1), Role(gid + 41, "Staff", 5), Role(gid + 42, "BotManaged", 2, managed=True)]
        self.me = SimpleNamespace(top_role=Role(gid + 99, "Logiq", 3), joined_at=datetime.now(timezone.utc))
        self.left = False
        self.created_at = datetime.now(timezone.utc)

    def get_member(self, uid):
        return self._members.get(uid)

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    async def leave(self):
        self.left = True


class FakeDB:
    is_connected = True
    database_name = "Logiq"

    def __init__(self):
        self.guilds = {}
        self.analytics = []
        self.sessions = {}
        self.role_menus = []
        self.tickets = []

    # sessioni web
    async def ensure_web_indexes(self):
        pass

    async def save_web_session(self, key, data, exp, created):
        self.sessions[key] = (data, exp, created)

    async def delete_web_session(self, key):
        self.sessions.pop(key, None)

    async def load_web_sessions(self):
        return [(k, d, e, c) for k, (d, e, c) in self.sessions.items()]

    # menu dei ruoli
    async def create_role_menu(self, doc):
        self.role_menus.append(doc)

    async def get_role_menus(self, gid):
        return [dict(d) for d in self.role_menus if d["guild_id"] == gid]

    async def get_all_role_menus(self):
        return [dict(d) for d in self.role_menus]

    async def delete_role_menu(self, gid, menu_id):
        before = len(self.role_menus)
        self.role_menus = [d for d in self.role_menus if not (d["guild_id"] == gid and d["menu_id"] == menu_id)]
        return len(self.role_menus) < before

    # ticket
    async def list_tickets(self, gid, limit=50):
        return [dict(t) for t in reversed(self.tickets) if t["guild_id"] == gid][:limit]

    async def get_open_ticket_by_channel(self, gid, channel_id):
        return next((t for t in self.tickets if t["guild_id"] == gid and t["channel_id"] == channel_id and t["status"] == "open"), None)

    async def close_ticket_by_channel(self, channel_id, closed_by, reason):
        for t in self.tickets:
            if t["channel_id"] == channel_id:
                t.update(status="closed", closed_by=closed_by, close_reason=reason)

    async def get_guild(self, gid):
        doc = self.guilds.get(gid)
        return {"_id": object(), **doc} if doc else None

    async def create_guild(self, gid, data=None):
        self.guilds[gid] = {"guild_id": gid, "modules": {}, "log_channel": None}
        return self.guilds[gid]

    async def update_guild(self, gid, data):
        doc = self.guilds[gid]
        for key, value in data.items():
            if "." in key:
                a, b = key.split(".", 1)
                doc.setdefault(a, {})[b] = value
            else:
                doc[key] = value
        return True

    async def get_leaderboard(self, gid, limit=10):
        return [{"user_id": int(ALICE), "xp": 120, "level": 3}, {"user_id": 424242424242, "xp": 40, "level": 1}]

    async def get_analytics(self, gid, event_type=None, start_time=None, end_time=None):
        return [e for e in self.analytics if e["type"] == event_type and start_time <= e["timestamp"] <= end_time]


class FakeRolesCog:
    """Registra le richieste invece di parlare con Discord"""

    def __init__(self, db):
        self.db = db
        self.posted = []

    async def post_menu(self, channel, title, description, role_list, exclusive, created_by):
        doc = {
            "menu_id": f"{len(self.posted):012x}", "guild_id": channel.guild_id, "channel_id": channel.id,
            "message_id": 4242, "title": title, "description": description or "", "exclusive": exclusive,
            "role_ids": [r["role"].id for r in role_list], "created_by": created_by, "created_at": 1.0,
        }
        self.posted.append(doc)
        await self.db.create_role_menu(doc)
        return {**doc, "jump_url": f"https://discord.com/channels/{channel.guild_id}/{channel.id}/4242"}


class FakeTicketsCog:
    def __init__(self):
        self.panels = []
        self.closed = []

    async def send_panel(self, channel):
        self.panels.append(channel.id)
        return SimpleNamespace(id=777)

    async def close_ticket_channel(self, channel, closer_label, closer_id, reason="x"):
        self.closed.append((channel.id, closer_label, closer_id, reason))


class FakeBot(GuildConfigMixin):
    def __init__(self):
        self._init_guild_config()
        self.config = {
            "web": {"cors_origins": []},
            "modules": {"leveling": {"enabled": True, "xp_per_message": 10, "xp_cooldown": 60},
                        "economy": {"enabled": True, "daily_reward": 100},
                        "ai_chat": {"enabled": False}},
        }
        self.user = SimpleNamespace(id=999000999000999000, name="Logiq", display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/a.png"))
        self.db = FakeDB()
        self.latency = 0.05
        self.start_time = datetime.now(timezone.utc) - timedelta(hours=5)
        self.tree = SimpleNamespace(get_commands=lambda: [1] * 7)
        self.alice = Member(ALICE, "Alice", manage=True)
        self.bob = Member(BOB, "Bob", manage=False)
        self.gA = Guild(GA, "Server A", [self.alice, self.bob])
        self.gB = Guild(GB, "Server B", [self.bob])
        self.guilds = [self.gA, self.gB]
        self.cogs = {"Roles": FakeRolesCog(self.db), "Tickets": FakeTicketsCog()}

    def get_cog(self, name):
        return self.cogs.get(name)

    def is_ready(self):
        return True

    def get_guild(self, gid):
        return next((g for g in self.guilds if g.id == gid), None)

    async def application_info(self):
        return SimpleNamespace(owner=SimpleNamespace(id=int(OWNER)))


# ---------------- Discord OAuth finto ----------------
USERS = {
    "goodcode": ("tok-alice", {"id": ALICE, "username": "alice", "global_name": "Alice", "avatar": None}),
    "bobcode": ("tok-bob", {"id": BOB, "username": "bob", "global_name": None, "avatar": "abc123"}),
    "ownercode": ("tok-owner", {"id": OWNER, "username": "owner", "global_name": "Owner", "avatar": None}),
}
GUILDS_BY_TOKEN = {
    "tok-alice": [
        {"id": str(GA), "name": "Server A", "icon": None, "owner": False, "permissions": "32"},
        {"id": str(GB), "name": "Server B", "icon": None, "owner": False, "permissions": "0"},
        {"id": str(GC), "name": "Server C", "icon": "iconhash", "owner": True, "permissions": "8"},
    ],
    "tok-bob": [{"id": str(GA), "name": "Server A", "icon": None, "owner": False, "permissions": "0"}],
    "tok-owner": [],
}
revoked = []


def discord_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/oauth2/token") and request.method == "POST":
        form = parse_qs(request.content.decode())
        code = form.get("code", [""])[0]
        if code not in USERS or form.get("client_secret") != ["sec"] or form.get("redirect_uri") != ["https://example.test/auth/callback"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": USERS[code][0]})
    if path.endswith("/oauth2/token/revoke"):
        revoked.append(parse_qs(request.content.decode()).get("token", [""])[0])
        return httpx.Response(200, json={})
    token = request.headers.get("authorization", "").replace("Bearer ", "")
    if path.endswith("/users/@me"):
        user = next((u for t, u in USERS.values() if t == token), None)
        return httpx.Response(200, json=user) if user else httpx.Response(401)
    if path.endswith("/users/@me/guilds"):
        return httpx.Response(200, json=GUILDS_BY_TOKEN[token]) if token in GUILDS_BY_TOKEN else httpx.Response(401)
    return httpx.Response(404)
