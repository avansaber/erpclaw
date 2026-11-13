"""Sender settings approved over real books with an existing ledger."""
from erpclaw_core.plugins import disable_plugin, install_plugin


def update_email_sender(request, engine):
    with engine.begin() as conn:
        disable_plugin(conn, "email_folder")
    install_plugin(engine, "email")
    return ({"from_address": "books@example.test", "display_name": "Company Books",
             "smtp_host": "smtp.example.test", "port": 587, "security": "starttls",
             "username": "books", "reply_to": "reply@example.test"},
            "Update email sender settings for Company Books.")


NO_MONEY_SCENARIOS = {"update_email_sender": update_email_sender}
