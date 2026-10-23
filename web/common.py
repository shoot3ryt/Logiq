"""
Utilità condivise dalla parte web
"""

from typing import Optional
from urllib.parse import urlencode, quote

# Permessi richiesti dall'invito: moderazione, ruoli, canali, messaggi, voce (musica / temp voice)
INVITE_PERMISSIONS = 1099800374358
INVITE_SCOPES = "bot applications.commands"


def invite_url(client_id: Optional[str], guild_id: Optional[str] = None) -> Optional[str]:
    """Link di invito del bot; con guild_id il server è già selezionato"""
    if not client_id:
        return None
    params = {
        "client_id": client_id,
        "permissions": INVITE_PERMISSIONS,
        "scope": INVITE_SCOPES,
    }
    if guild_id:
        params["guild_id"] = guild_id
        params["disable_guild_select"] = "true"
    return f"https://discord.com/oauth2/authorize?{urlencode(params, quote_via=quote)}"
