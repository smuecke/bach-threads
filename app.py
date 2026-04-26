from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from bachthreads.organizer import Settings, ThreadOrganizer
from bachthreads.whitelist import WhitelistManager, WhitelistStore, parse_user_ids


load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger(__name__)

bot_token = os.environ["SLACK_BOT_TOKEN"]
user_token = os.environ["SLACK_USER_TOKEN"]
signing_secret = os.environ.get("SLACK_SIGNING_SECRET")

app = App(token=bot_token, signing_secret=signing_secret)
whitelist = WhitelistManager(
    app.client,
    WhitelistStore(
        os.environ.get("WHITELIST_FILE", "data/whitelist.json"),
        parse_user_ids(os.environ.get("INITIAL_WHITELIST_USER_IDS")),
    ),
)
organizer = ThreadOrganizer(
    bot_client=app.client,
    user_client=WebClient(token=user_token),
    settings=Settings.from_env(),
    whitelist=whitelist,
)


def log_startup_state() -> None:
    try:
        auth = app.client.auth_test()
        LOGGER.info(
            "Connected to Slack workspace=%s bot_user_id=%s",
            auth.get("team"),
            auth.get("user_id"),
        )
    except Exception:
        LOGGER.exception("Slack auth_test failed. Check SLACK_BOT_TOKEN and scopes.")

    LOGGER.info("Whitelist contains %s user(s)", len(whitelist.store.list()))


@app.event("reaction_added")
def handle_reaction_added(event, logger):
    logger.info(
        "Received reaction_added reaction=%s user=%s item=%s",
        event.get("reaction"),
        event.get("user"),
        event.get("item"),
    )
    result = organizer.handle_reaction_added(event)
    logger.info("reaction_added result: %s", result)


@app.event("message")
def handle_message_events(event, say, logger):
    logger.info(
        "Received message event channel_type=%s user=%s subtype=%s",
        event.get("channel_type"),
        event.get("user"),
        event.get("subtype"),
    )
    if event.get("channel_type") != "im" or event.get("subtype"):
        return

    result = whitelist.handle_dm_text(event.get("user"), event.get("text") or "")
    if result:
        logger.info("whitelist command result: %s", result.ok)
        say(text=result.text)


if __name__ == "__main__":
    log_startup_state()
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if app_token:
        LOGGER.info("Starting BachThreads in Socket Mode")
        SocketModeHandler(app, app_token).start()
    else:
        port = int(os.environ.get("PORT", "3000"))
        LOGGER.info("Starting BachThreads HTTP server on port %s", port)
        app.start(port=port)
