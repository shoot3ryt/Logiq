"""
Test dell'interfaccia della dashboard in un DOM simulato (jsdom).
Facoltativi: vengono saltati se Node.js o jsdom non sono installati
(per attivarli: `cd tests/ui && npm install`).
"""

import json
import os
import shutil
import time
import subprocess
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.web_fakes import FakeBot, GA, GB, Role, discord_handler
from web.api import create_app

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


def _jsdom_available() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    return subprocess.run([node, "-e", "require.resolve('jsdom')"], cwd=UI_DIR, capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _jsdom_available(), reason="Node.js/jsdom non disponibili (cd tests/ui && npm install)")


def _fixtures() -> dict:
    bot = FakeBot()
    evil = '<img src=x onerror="window.__xss=1">'
    bot.gA.name = evil + "Server A"  # nome malevolo: deve restare testo ovunque
    now = time.time()
    bot.db.analytics = [{"type": "message", "timestamp": now - 100}] * 4 + [{"type": "member_join", "timestamp": now - 90000}]
    bot.gA.roles.append(Role(GA + 43, "Gamer", 1))
    bot.db.guilds[GA] = {"guild_id": GA, "modules": {}, "welcome_message": evil + "Ciao {user} in #generale"}
    bot.db.role_menus = [{"menu_id": "a1b2c3d4e5f6", "guild_id": GA, "channel_id": GA + 10, "message_id": 1,
                          "title": "Scegli i giochi", "exclusive": False, "role_ids": [GA + 40], "created_at": 1.0}]
    bot.db.tickets = [
        {"guild_id": GA, "user_id": 111111111111111111, "channel_id": GA + 10, "status": "open", "created_at": 1.0},
        {"guild_id": GA, "user_id": 222222222222222222, "channel_id": 5, "status": "closed", "created_at": 1.0, "close_reason": "Resolved"},
    ]
    app = create_app(bot, auth_transport=httpx.MockTransport(discord_handler))

    def session(code):
        c = TestClient(app, base_url="https://testserver", follow_redirects=False)
        r = c.get("/auth/login")
        st = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        c.get(f"/auth/callback?code={code}&state={st}")
        return c

    alice, owner = session("goodcode"), session("ownercode")
    return {
        "me": alice.get("/api/me").json(),
        "overview": alice.get(f"/api/guilds/{GA}/overview").json(),
        "settings": alice.get(f"/api/guilds/{GA}/settings").json(),
        "roles": alice.get(f"/api/guilds/{GA}/role-menus").json(),
        "tickets": alice.get(f"/api/guilds/{GA}/tickets").json(),
        "me_owner": owner.get("/api/me").json(),
        "admin": owner.get("/api/admin/overview").json(),
        "GA": str(GA),
        "GB": str(GB),
    }


def test_interfaccia_dashboard(tmp_path):
    fx = tmp_path / "fixtures.json"
    data = _fixtures()
    fx.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(["node", os.path.join(UI_DIR, "dashboard_ui.test.js"), str(fx)], cwd=UI_DIR, capture_output=True, text=True, timeout=120)
    failed = [line for line in result.stdout.splitlines() if line.startswith("FAIL")]
    assert result.returncode == 0 and not failed, "\n".join(failed) + result.stderr
