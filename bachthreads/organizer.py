from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Any, Iterable

from bachthreads.whitelist import WhitelistManager


LOGGER = logging.getLogger(__name__)

SLACK_THREAD_HELP_URL = (
    "https://slack.com/help/articles/115000769927-Use-threads-to-organize-discussions"
)


@dataclass(frozen=True)
class SavedMessage:
    channel: str
    ts: str
    user: str | None
    text: str
    permalink: str | None = None


@dataclass(frozen=True)
class Settings:
    thread_emoji: str
    reminder_help_url: str
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            thread_emoji=os.environ.get("THREAD_EMOJI", "thread"),
            reminder_help_url=os.environ.get(
                "THREAD_HELP_URL", SLACK_THREAD_HELP_URL
            ),
            dry_run=os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"},
        )


class ThreadOrganizer:
    def __init__(
        self,
        bot_client: Any,
        user_client: Any,
        settings: Settings,
        whitelist: WhitelistManager | None = None,
    ) -> None:
        self.bot_client = bot_client
        self.user_client = user_client
        self.settings = settings
        self.whitelist = whitelist

    def handle_reaction_added(self, event: dict[str, Any]) -> dict[str, Any]:
        if not self._is_thread_reaction(event):
            return {"status": "ignored", "reason": "different_reaction"}

        user_id = event.get("user")
        if self.whitelist and not self.whitelist.trigger_allowed(user_id):
            return {"status": "ignored", "reason": "not_whitelisted"}

        item = event.get("item") or {}
        channel = item.get("channel")
        parent_ts = item.get("ts")
        if item.get("type") != "message" or not channel or not parent_ts:
            return {"status": "ignored", "reason": "unsupported_item"}

        saved_messages = [
            message
            for message in self.fetch_saved_messages()
            if not (message.channel == channel and message.ts == parent_ts)
        ]
        if not saved_messages:
            self.remove_trigger_reaction(channel, parent_ts)
            return {"status": "done", "posted": 0, "reminded": 0}

        posted_messages = self.post_thread_replies(channel, parent_ts, saved_messages)
        self.remind_authors(saved_messages, channel, parent_ts)
        self.clear_saved_messages(saved_messages)
        self.remove_trigger_reaction(channel, parent_ts)

        return {
            "status": "done",
            "posted": posted_messages,
            "reminded": len(unique_author_ids(saved_messages)),
        }

    def fetch_saved_messages(self) -> list[SavedMessage]:
        messages: list[SavedMessage] = []
        cursor: str | None = None

        while True:
            kwargs: dict[str, Any] = {"count": 100}
            if cursor:
                kwargs["cursor"] = cursor

            response = self.user_client.stars_list(**kwargs)
            for item in response.get("items", []):
                message = saved_item_to_message(item)
                if message:
                    messages.append(message)

            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return messages

    def post_thread_replies(
        self, channel: str, parent_ts: str, messages: Iterable[SavedMessage]
    ) -> int:
        count = 0
        for message in messages:
            self.bot_client.chat_postMessage(
                channel=channel,
                thread_ts=parent_ts,
                text=format_thread_reply(message),
                unfurl_links=False,
                unfurl_media=False,
            )
            count += 1
        return count

    def remind_authors(
        self, messages: Iterable[SavedMessage], channel: str, parent_ts: str
    ) -> None:
        parent_link = self._permalink(channel, parent_ts)
        for author_id in unique_author_ids(messages):
            dm = self.bot_client.conversations_open(users=author_id)
            dm_channel = dm["channel"]["id"]
            self.bot_client.chat_postMessage(
                channel=dm_channel,
                text=reminder_text(parent_link, self.settings.reminder_help_url),
                unfurl_links=False,
                unfurl_media=False,
            )

    def clear_saved_messages(self, messages: Iterable[SavedMessage]) -> None:
        for message in messages:
            self.user_client.stars_remove(channel=message.channel, timestamp=message.ts)

    def remove_trigger_reaction(self, channel: str, parent_ts: str) -> None:
        self.bot_client.reactions_remove(
            channel=channel,
            timestamp=parent_ts,
            name=self.settings.thread_emoji,
        )

    def _is_thread_reaction(self, event: dict[str, Any]) -> bool:
        return event.get("reaction") == self.settings.thread_emoji

    def _permalink(self, channel: str, ts: str) -> str | None:
        try:
            response = self.bot_client.chat_getPermalink(channel=channel, message_ts=ts)
            return response.get("permalink")
        except Exception:
            LOGGER.exception("Could not create permalink for %s/%s", channel, ts)
            return None


def saved_item_to_message(item: dict[str, Any]) -> SavedMessage | None:
    item_type = item.get("type")
    channel = item.get("channel")
    ts = item.get("message", {}).get("ts") or item.get("date_create")
    message = item.get("message") or {}

    if item_type != "message" or not channel or not ts:
        return None

    return SavedMessage(
        channel=channel,
        ts=str(ts),
        user=message.get("user"),
        text=message.get("text") or "",
        permalink=item.get("url"),
    )


def format_thread_reply(message: SavedMessage) -> str:
    author = f"<@{message.user}> wrote" if message.user else "Original message"
    link = f"\nOriginal: {message.permalink}" if message.permalink else ""
    text = message.text.strip() or "_No text content_"
    return f"{author}:\n>{text}{link}"


def reminder_text(parent_link: str | None, help_url: str) -> str:
    parent = (
        f"Ich habe Ihre Nachricht hier in die passende Unterhaltung verschoben: {parent_link}\n\n"
        if parent_link
        else ""
    )
    return (
        "Guten Tag! Nur ein kleiner freundlicher Hinweis zu Slack:\n\n"
        f"{parent}"
        "Wenn es zu einem Thema schon eine Unterhaltung gibt, nutzen Sie bitte die "
        "Antwort-Funktion statt einer neuen Nachricht im Kanal. So bleibt alles "
        "beisammen und der Kanal ist für alle leichter zu überblicken.\n\n"
        "Keine Sorge, das passiert schnell. Eine kurze Anleitung von Slack finden "
        f"Sie hier: {help_url}"
    )


def unique_author_ids(messages: Iterable[SavedMessage]) -> list[str]:
    seen: set[str] = set()
    author_ids: list[str] = []
    for message in messages:
        if not message.user or message.user in seen:
            continue
        seen.add(message.user)
        author_ids.append(message.user)
    return author_ids
