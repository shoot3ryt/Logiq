"""
Test della dashboard web: login Discord, permessi, impostazioni, ruoli, ticket, admin, sessioni.
Nessun Discord e nessun database reali: tutto è simulato (vedi web_fakes.py).
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.web_fakes import (
    ALICE, BOB, GA, GB, GC, OWNER, FakeBot, discord_handler, revoked,
)
from web.api import create_app


# ---------------- utilità ----------------
def new_client(bot, app=None):
    app = app or create_app(bot, auth_transport=httpx.MockTransport(discord_handler))
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def login(client, code):
    r = client.get("/auth/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return client.get(f"/auth/callback?code={code}&state={state}")


def logged(bot, code):
    client = new_client(bot)
    login(client, code)
    me = client.get("/api/me").json()
    return client, {"X-CSRF-Token": me["csrf"]}, me


@pytest.fixture
def bot():
    revoked.clear()
    b = FakeBot()
    now = datetime.now(timezone.utc).timestamp()
    b.db.analytics = [{"type": "message", "timestamp": now - 100}] * 4 + [{"type": "member_join", "timestamp": now - 100}]
    return b


@pytest.fixture
def alice(bot):
    return logged(bot, "goodcode")


@pytest.fixture
def owner(bot):
    return logged(bot, "ownercode")


# ---------------- login e sessione ----------------
def test_senza_sessione_401(bot):
    c = new_client(bot)
    assert c.get("/api/me").status_code == 401
    assert c.get("/guilds").status_code == 401
    assert c.get(f"/api/guilds/{GA}/overview").status_code == 401


def test_login_redirect_a_discord(bot):
    r = new_client(bot).get("/auth/login")
    loc = r.headers["location"]
    q = parse_qs(urlparse(loc).query)
    assert loc.startswith("https://discord.com/oauth2/authorize")
    assert q["client_id"] == ["999000999000999000"]
    assert q["scope"] == ["identify guilds"]
    assert q["redirect_uri"] == ["https://example.test/auth/callback"]
    cookie = r.headers["set-cookie"]
    assert "logiq_oauth" in cookie and "HttpOnly" in cookie and "Secure" in cookie


def test_callback_rifiuta_state_errato_o_mancante(bot):
    c = new_client(bot)
    c.get("/auth/login")
    r = c.get("/auth/callback?code=goodcode&state=sbagliato")
    assert r.headers["location"] == "/dashboard?error=invalid_state"
    assert c.get("/api/me").status_code == 401
    assert new_client(bot).get("/auth/callback?code=goodcode&state=x").headers["location"].endswith("invalid_state")


def test_callback_utente_nega_e_codice_non_valido(bot):
    c = new_client(bot)
    st = parse_qs(urlparse(c.get("/auth/login").headers["location"]).query)["state"][0]
    assert c.get(f"/auth/callback?error=access_denied&state={st}").headers["location"] == "/dashboard?error=denied"
    st = parse_qs(urlparse(c.get("/auth/login").headers["location"]).query)["state"][0]
    assert c.get(f"/auth/callback?code=boh&state={st}").headers["location"] == "/dashboard?error=login_failed"


def test_login_ok_cookie_e_revoca_token(bot):
    c = new_client(bot)
    r = login(c, "goodcode")
    assert r.headers["location"] == "/dashboard"
    cookie = r.headers["set-cookie"].lower()
    assert all(x in cookie for x in ("logiq_session", "httponly", "samesite=lax", "secure"))
    assert revoked == ["tok-alice"]


def test_me_elenco_server(alice):
    client, _, d = alice
    ids = {g["id"]: g for g in d["guilds"]}
    assert set(ids) == {str(GA), str(GC)}  # A col bot, C senza bot, B escluso (nessun permesso)
    assert ids[str(GA)]["bot_present"] and ids[str(GA)]["invite_url"] is None
    assert not ids[str(GC)]["bot_present"]
    assert f"guild_id={GC}" in ids[str(GC)]["invite_url"]
    assert ids[str(GC)]["icon_url"] == f"https://cdn.discordapp.com/icons/{GC}/iconhash.png?size=64"
    assert d["is_owner"] is False and len(d["csrf"]) > 20 and d["user"]["name"] == "Alice"


def test_cookie_manomesso_e_logout(bot):
    c = new_client(bot)
    login(c, "goodcode")
    cookie = c.cookies.get("logiq_session")
    c.cookies.set("logiq_session", cookie[:-3] + ("AAA" if not cookie.endswith("AAA") else "BBB"))
    assert c.get("/api/me").status_code == 401
    c.cookies.set("logiq_session", "garbage")
    assert c.get("/api/me").status_code == 401

    client, headers, _ = logged(bot, "goodcode")
    old = client.cookies.get("logiq_session")
    assert client.post("/auth/logout").status_code == 403
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/me").status_code == 401
    client.cookies.set("logiq_session", old)
    assert client.get("/api/me").status_code == 401  # sessione distrutta anche lato server


# ---------------- panoramica ----------------
def test_overview(alice, bot):
    client, _, _ = alice
    r = client.get(f"/api/guilds/{GA}/overview")
    d = r.json()
    assert r.status_code == 200
    assert d["leaderboard"][0]["name"] == "Alice" and d["leaderboard"][1]["name"].startswith("Utente …")
    assert isinstance(d["leaderboard"][0]["user_id"], str) and isinstance(d["guild"]["id"], str)
    assert sum(d["activity"]["messages"]) == 4 and sum(d["activity"]["joins"]) == 1
    assert client.get(f"/api/guilds/{GB}/overview").status_code == 404  # nessun permesso
    assert client.get("/api/guilds/123/overview").status_code == 404
    assert client.get(f"/api/guilds/{GC}/overview").status_code == 404  # il bot non c'è


# ---------------- impostazioni ----------------
def test_settings_get(alice):
    client, _, _ = alice
    s = client.get(f"/api/guilds/{GA}/settings").json()
    assert [x["name"] for x in s["roles"]] == ["Staff", "Membro"]
    assert {x["name"]: x["assignable"] for x in s["roles"]} == {"Staff": False, "Membro": True}
    assert [c["name"] for c in s["channels"]["text"]] == ["generale", "log"]
    mods = {m["key"]: m for m in s["modules"]}
    assert mods["ai_chat"]["locked"] and not mods["ai_chat"]["enabled"]
    assert mods["leveling"]["enabled"] and not mods["leveling"]["locked"]
    assert s["defaults"]["xp_per_message"] == 10 and s["defaults"]["daily_reward"] == 100
    assert s["defaults"]["currency_name"] == "ProgrammiCoin" and s["defaults"]["currency_symbol"] == "💎"
    assert "_id" not in s


def put(client, headers, payload, gid=GA):
    return client.put(f"/api/guilds/{gid}/settings", json=payload, headers=headers)


@pytest.mark.parametrize("payload", [
    {"log_channel": str(GA + 20)},            # canale vocale
    {"log_channel": str(GB + 10)},            # canale di un altro server
    {"welcome_channel": str(GA + 30)},        # categoria
    {"verified_role": str(GA)},               # @everyone
    {"support_role": str(GA + 42)},           # ruolo gestito
    {"log_channel": "abc"},
    {"modules": {"hack": True}},
    {"xp_per_message": 0},
    {"daily_reward": 10**9},
    {"prefix": "!"},
    {"verification_type": "oauth"},
    {"welcome_message": "x" * 1001},
    {"currency_name": "x" * 33},
    {"currency_symbol": "x" * 9},
    {},
])
def test_settings_put_rifiuta_dati_non_validi(alice, bot, payload):
    client, headers, _ = alice
    assert put(client, headers, payload).status_code == 422
    assert GA not in bot.db.guilds  # nulla è stato scritto


def test_settings_put_richiede_csrf(alice):
    client, _, _ = alice
    body = {"log_channel": str(GA + 11)}
    assert client.put(f"/api/guilds/{GA}/settings", json=body).status_code == 403
    assert client.put(f"/api/guilds/{GA}/settings", json=body, headers={"X-CSRF-Token": "x"}).status_code == 403


def test_settings_put_salva_e_il_bot_lo_vede(alice, bot):
    client, headers, _ = alice
    r = put(client, headers, {
        "log_channel": str(GA + 11), "welcome_channel": str(GA + 10), "ticket_category": str(GA + 30),
        "temp_voice_creator": str(GA + 20), "verified_role": str(GA + 40), "support_role": str(GA + 41),
        "verification_type": "captcha", "verification_method": "channel", "welcome_message": "Ciao {user}!",
        "xp_per_message": 25, "xp_cooldown": 30, "daily_reward": 500,
        "currency_name": "  Gemme  ", "currency_symbol": "🔥",
        "modules": {"leveling": False, "economy": True},
    })
    assert r.status_code == 200, r.text
    doc = bot.db.guilds[GA]
    assert doc["log_channel"] == GA + 11 and doc["verified_role"] == GA + 40 and doc["ticket_category"] == GA + 30
    assert doc["verification_type"] == "captcha" and doc["welcome_message"] == "Ciao {user}!"
    assert doc["settings"] == {"xp_per_message": 25, "xp_cooldown": 30, "daily_reward": 500,
                               "currency_name": "Gemme", "currency_symbol": "🔥"}
    assert doc["modules"] == {"leveling": False, "economy": True}

    async def look():
        return (await bot.module_enabled(GA, "leveling"), await bot.module_enabled(GB, "leveling"),
                await bot.guild_setting(GA, "xp_per_message", 10), await bot.guild_setting(GB, "xp_per_message", 10),
                await bot.guild_setting(GA, "currency_symbol", "💎"), await bot.guild_setting(GB, "currency_symbol", "💎"))

    assert asyncio.run(look()) == (False, True, 25, 10, "🔥", "💎")

    # azzeramento con null / stringhe vuote
    r = put(client, headers, {"log_channel": None, "xp_per_message": None, "welcome_message": "   ", "currency_name": "  "})
    assert r.status_code == 200
    doc = bot.db.guilds[GA]
    assert doc["log_channel"] is None and doc["settings"]["xp_per_message"] is None
    assert doc["welcome_message"] is None and doc["settings"]["currency_name"] is None
    assert asyncio.run(bot.guild_setting(GA, "xp_per_message", 10)) == 10


def test_valuta_toglie_caratteri_di_controllo(alice, bot):
    client, headers, _ = alice
    put(client, headers, {"currency_name": "Ge\x00mme\n"})
    assert bot.db.guilds[GA]["settings"]["currency_name"] == "Gemme"


def test_utente_senza_permessi(bot):
    client, headers, me = logged(bot, "bobcode")
    assert me["guilds"] == []
    assert client.get(f"/api/guilds/{GA}/settings").status_code == 404
    assert put(client, headers, {"log_channel": str(GA + 11)}).status_code == 404
    assert client.get("/api/admin/overview").status_code == 403
    assert me["user"]["avatar_url"] == f"https://cdn.discordapp.com/avatars/{BOB}/abc123.png?size=64"


def test_permesso_revocato_vale_subito(alice, bot):
    client, _, _ = alice
    bot.alice.guild_permissions.manage_guild = False
    assert client.get(f"/api/guilds/{GA}/settings").status_code == 404


# ---------------- ruoli a tendina ----------------
def menu(**over):
    body = {"title": "Giochi", "description": "Scegli", "channel_id": str(GA + 10),
            "role_ids": [str(GA + 40)], "exclusive": False}
    body.update(over)
    return body


def test_role_menu_crea_elenca_elimina(alice, bot):
    client, headers, _ = alice
    url = f"/api/guilds/{GA}/role-menus"
    r = client.post(url, json=menu(role_ids=[str(GA + 40), str(GA + 40)], exclusive=True), headers=headers)
    assert r.status_code == 200, r.text
    posted = bot.cogs["Roles"].posted
    assert len(posted) == 1 and posted[0]["role_ids"] == [GA + 40] and posted[0]["exclusive"] is True  # duplicati tolti
    listed = client.get(url).json()
    assert [m["title"] for m in listed["menus"]] == ["Giochi"] and listed["menus"][0]["roles"] == ["Membro"]
    mid = listed["menus"][0]["menu_id"]
    assert client.delete(f"{url}/{mid}", headers=headers).status_code == 200
    assert client.get(url).json()["menus"] == []
    assert bot.gA.get_channel(GA + 10).deleted_messages == [4242]


@pytest.mark.parametrize("over", [
    {"channel_id": str(GB + 10)},               # canale di un altro server
    {"channel_id": str(GA + 20)},               # canale vocale
    {"role_ids": [str(GA + 41)]},               # sopra al ruolo del bot
    {"role_ids": [str(GA + 42)]},               # gestito da un'integrazione
    {"role_ids": [str(GA)]},                    # @everyone
    {"role_ids": []},
    {"role_ids": [str(GA + 40)] * 26 and [str(i) for i in range(26)]},
    {"title": ""},
    {"unknown": 1},
])
def test_role_menu_rifiuta_dati_non_validi(alice, bot, over):
    client, headers, _ = alice
    assert client.post(f"/api/guilds/{GA}/role-menus", json=menu(**over), headers=headers).status_code == 422
    assert bot.cogs["Roles"].posted == []


def test_role_menu_sicurezza(alice, bot):
    client, headers, _ = alice
    url = f"/api/guilds/{GA}/role-menus"
    assert client.post(url, json=menu()).status_code == 403  # senza CSRF
    assert client.delete(f"{url}/aaaaaaaaaaaa").status_code == 403
    assert client.delete(f"{url}/aaaaaaaaaaaa", headers=headers).status_code == 404
    assert client.delete(f"{url}/../x", headers=headers).status_code in (404, 405)
    # un menu di un altro server non si può eliminare passando dal proprio
    bot.db.role_menus.append({"menu_id": "abcdefabcdef", "guild_id": GB, "channel_id": GB + 10, "message_id": 1,
                              "title": "Altrui", "role_ids": [], "exclusive": False})
    assert client.delete(f"{url}/abcdefabcdef", headers=headers).status_code == 404
    assert len(bot.db.role_menus) == 1
    # senza permessi
    bobc, bh, _ = logged(bot, "bobcode")
    assert bobc.post(url, json=menu(), headers=bh).status_code == 404
    assert bobc.get(url).status_code == 404


def test_role_menu_bot_non_puo_scrivere(alice, bot):
    client, headers, _ = alice
    bot.gA.get_channel(GA + 10).can_send = False
    assert client.post(f"/api/guilds/{GA}/role-menus", json=menu(), headers=headers).status_code == 422


def test_role_menu_modulo_non_caricato(alice, bot):
    client, headers, _ = alice
    del bot.cogs["Roles"]
    assert client.post(f"/api/guilds/{GA}/role-menus", json=menu(), headers=headers).status_code == 503


# ---------------- ticket ----------------
def ticket(gid, channel, status="open", user=ALICE):
    return {"guild_id": gid, "user_id": int(user), "channel_id": channel, "status": status, "created_at": 1.0}


def test_ticket_elenco_e_pannello(alice, bot):
    client, headers, _ = alice
    bot.db.tickets = [ticket(GA, GA + 10), ticket(GA, 5, "closed", BOB), ticket(GB, GB + 10)]
    d = client.get(f"/api/guilds/{GA}/tickets").json()
    assert [t["status"] for t in d["tickets"]] == ["closed", "open"]  # dal più recente; nessun ticket di B
    assert d["tickets"][1]["channel"] == "generale" and d["tickets"][1]["user"] == "Alice"
    assert d["tickets"][0]["channel"] is None and d["configured"] is False
    assert client.post(f"/api/guilds/{GA}/tickets/panel", json={"channel_id": str(GA + 10)}).status_code == 403
    r = client.post(f"/api/guilds/{GA}/tickets/panel", json={"channel_id": str(GA + 10)}, headers=headers)
    assert r.status_code == 200 and bot.cogs["Tickets"].panels == [GA + 10]
    assert client.post(f"/api/guilds/{GA}/tickets/panel", json={"channel_id": str(GB + 10)}, headers=headers).status_code == 422


def test_ticket_chiusura(alice, bot):
    client, headers, _ = alice
    bot.db.tickets = [ticket(GA, GA + 10), ticket(GB, GB + 10), ticket(GA, 5, "closed")]
    close = lambda gid, ch, **kw: client.post(f"/api/guilds/{gid}/tickets/{ch}/close", json=kw.get("json", {}), headers=kw.get("headers", headers))
    assert client.post(f"/api/guilds/{GA}/tickets/{GA + 10}/close", json={}).status_code == 403
    assert close(GA, GB + 10).status_code == 404       # ticket di un altro server
    assert close(GA, 999).status_code == 404           # non è un ticket
    assert close(GA, 5).status_code == 404             # già chiuso
    assert bot.cogs["Tickets"].closed == []
    r = close(GA, GA + 10, json={"reason": "Fatto"})
    assert r.status_code == 200
    client.get("/api/me")  # lascia girare il compito in background
    closed = bot.cogs["Tickets"].closed
    assert closed == [(GA + 10, "Alice (dashboard)", int(ALICE), "Fatto")]


def test_ticket_con_canale_gia_eliminato_si_chiude_solo_nel_db(alice, bot):
    client, headers, _ = alice
    bot.db.tickets = [ticket(GA, 12345)]
    r = client.post(f"/api/guilds/{GA}/tickets/12345/close", json={}, headers=headers)
    assert r.status_code == 200 and r.json()["channel_deleted"] is True
    assert bot.db.tickets[0]["status"] == "closed" and bot.cogs["Tickets"].closed == []


# ---------------- proprietario ----------------
def test_non_proprietario_non_entra_nell_admin(alice, bot):
    client, headers, _ = alice
    assert client.get("/api/admin/overview").status_code == 403
    assert client.get("/guilds").status_code == 403
    assert client.post(f"/api/admin/guilds/{GB}/leave", headers=headers).status_code == 403


def test_admin_panel(owner, bot):
    client, headers, me = owner
    asyncio.run(bot.db.create_guild(GA))
    assert me["is_owner"] is True and {g["id"] for g in me["guilds"]} == {str(GA), str(GB)}
    a = client.get("/api/admin/overview").json()
    assert a["guilds"][0]["id"] == str(GA) and a["database"]["connected"]
    assert any(m["key"] == "ai_chat" and not m["enabled"] for m in a["modules"])
    assert a["bot"]["guilds"] == 2 and a["bot"]["commands"] == 7 and a["bot"]["ready"]
    assert client.get(f"/guilds/{GA}").status_code == 200 and "_id" not in client.get(f"/guilds/{GA}").json()["config"]
    assert client.get(f"/api/guilds/{GB}/settings").status_code == 200
    assert client.post(f"/api/admin/guilds/{GB}/leave").status_code == 403
    assert client.post("/api/admin/guilds/5/leave", headers=headers).status_code == 404
    assert client.post(f"/api/admin/guilds/{GB}/leave", headers=headers).status_code == 200
    assert bot.gB.left and not bot.gA.left


# ---------------- pagine e intestazioni ----------------
def test_pagine_e_intestazioni(bot):
    c = new_client(bot)
    r = c.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert c.get("/api/me").headers["cache-control"] == "no-store"
    assert c.get("/admin").headers["location"] == "/dashboard#/admin"
    assert c.get("/dashboard").status_code == 200
    assert c.get("/stats").status_code == 200 and c.get("/health").status_code == 200


def test_login_non_configurato(monkeypatch):
    monkeypatch.delenv("DISCORD_CLIENT_SECRET")
    assert new_client(FakeBot()).get("/auth/login").headers["location"] == "/dashboard?error=not_configured"


# ---------------- sessioni salvate nel database ----------------
def test_sessioni_sopravvivono_al_riavvio(bot):
    first = new_client(bot)
    login(first, "goodcode")
    first.get("/api/me")  # lascia completare il salvataggio in background
    assert len(bot.db.sessions) == 1
    key = next(iter(bot.db.sessions))
    assert key != first.cookies.get("logiq_session")  # nel database c'è un hash, non l'id in chiaro

    cookie = first.cookies.get("logiq_session")
    # "riavvio": nuova applicazione, stesso database e stesso SESSION_SECRET
    with TestClient(create_app(bot, auth_transport=httpx.MockTransport(discord_handler)),
                    base_url="https://testserver", follow_redirects=False) as second:
        second.cookies.set("logiq_session", cookie)
        assert second.get("/api/me").status_code == 200


def test_logout_cancella_la_sessione_dal_database(bot):
    client, headers, _ = logged(bot, "goodcode")
    assert len(bot.db.sessions) == 1
    client.post("/auth/logout", headers=headers)
    client.get("/api/me")
    assert bot.db.sessions == {}


def test_sessioni_scadute_non_vengono_ricaricate(bot):
    from web.security import SessionStore

    bot.db.sessions["k1"] = ({"user": {"id": "1"}}, time.time() - 10, 0)
    bot.db.sessions["k2"] = ({"user": {"id": "2"}}, time.time() + 1000, 0)
    store = SessionStore(backend=bot.db)
    assert asyncio.run(store.load()) == 1


def test_database_giu_non_rompe_il_login(bot):
    async def boom(*a, **k):
        raise RuntimeError("db giù")

    bot.db.save_web_session = boom
    client = new_client(bot)
    assert login(client, "goodcode").headers["location"] == "/dashboard"
    assert client.get("/api/me").status_code == 200
