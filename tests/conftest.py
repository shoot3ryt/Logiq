import os

# Variabili d'ambiente fisse per i test della dashboard (nessun valore reale)
os.environ.update(
    DISCORD_CLIENT_ID="999000999000999000",
    DISCORD_CLIENT_SECRET="sec",
    SESSION_SECRET="test-secret-test-secret",
    OWNER_ID="555555555555555555",
    PUBLIC_URL="https://example.test",
)
