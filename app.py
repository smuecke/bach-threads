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


@app.event("reaction_added")
def handle_reaction_added(event, logger):
    result = organizer.handle_reaction_added(event)
    logger.info("reaction_added result: %s", result)


@app.event("message")
def handle_message_events(event, say, logger):
    if event.get("channel_type") != "im" or event.get("subtype"):
        return

    result = whitelist.handle_dm_text(event.get("user"), event.get("text") or "")
    if result:
        logger.info("whitelist command result: %s", result.ok)
        say(text=result.text)


if __name__ == "__main__":
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if app_token:
        LOGGER.info("Starting BachThreads in Socket Mode")
        SocketModeHandler(app, app_token).start()
    else:
        port = int(os.environ.get("PORT", "3000"))
        LOGGER.info("Starting BachThreads HTTP server on port %s", port)
        app.start(port=port)
