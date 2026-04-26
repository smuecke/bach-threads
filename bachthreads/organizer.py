from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Any, Iterable

from bachthreads.message_queue import MessageQueueStore, QueuedMessageRef
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
    thread_ts: str | None = None


@dataclass(frozen=True)
class Settings:
    thread_emoji: str
    message_emoji: str
    reminder_help_url: str
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            thread_emoji=os.environ.get("THREAD_EMOJI", "thread"),
            message_emoji=os.environ.get("MESSAGE_EMOJI", "bookmark"),
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
        queue_store: MessageQueueStore | None = None,
    ) -> None:
        self.bot_client = bot_client
        self.user_client = user_client
        self.settings = settings
        self.whitelist = whitelist
        self.queue_store = queue_store

    def handle_reaction_added(self, event: dict[str, Any]) -> dict[str, Any]:
        queued = self.handle_message_marker_reaction(event)
        if queued:
            return queued

        if not self._is_thread_reaction(event):
            LOGGER.info(
                "Ignoring reaction %s; expected %s",
                event.get("reaction"),
                self.settings.thread_emoji,
            )
            return {"status": "ignored", "reason": "different_reaction"}

        user_id = event.get("user")
        if self.whitelist and not self.whitelist.trigger_allowed(user_id):
            LOGGER.info("Ignoring thread reaction from non-whitelisted user %s", user_id)
            return {"status": "ignored", "reason": "not_whitelisted"}

        item = event.get("item") or {}
        channel = item.get("channel")
        parent_ts = item.get("ts")
        if item.get("type") != "message" or not channel or not parent_ts:
            LOGGER.info("Ignoring unsupported reaction item: %s", item)
            return {"status": "ignored", "reason": "unsupported_item"}

        saved_messages = self.fetch_marked_messages(user_id, channel, parent_ts)
        if not saved_messages:
            LOGGER.info(
                "No marked messages found to move under %s/%s", channel, parent_ts
            )
            self.remove_trigger_reaction(channel, parent_ts)
            return {"status": "done", "posted": 0, "reminded": 0}

        posted_messages = self.post_thread_replies(channel, parent_ts, saved_messages)
        self.remind_authors(saved_messages, channel, parent_ts)
        self.remove_trigger_reaction(channel, parent_ts)

        return {
            "status": "done",
            "posted": posted_messages,
            "reminded": len(unique_author_ids(saved_messages)),
        }

    def handle_message_marker_reaction(
        self, event: dict[str, Any]
    ) -> dict[str, Any] | None:
        if event.get("reaction") != self.settings.message_emoji or not self.queue_store:
            return None

        user_id = event.get("user")
        if self.whitelist and not self.whitelist.trigger_allowed(user_id):
            LOGGER.info("Ignoring message marker from non-whitelisted user %s", user_id)
            return {"status": "ignored", "reason": "not_whitelisted"}

        item = event.get("item") or {}
        channel = item.get("channel")
        ts = item.get("ts")
        if item.get("type") != "message" or not user_id or not channel or not ts:
            return {"status": "ignored", "reason": "unsupported_item"}

        self.queue_store.add(user_id, channel, ts)
        LOGGER.info("Queued message %s/%s for user %s", channel, ts, user_id)
        return {"status": "queued", "queued": 1}

    def fetch_marked_messages(
        self, user_id: str | None, parent_channel: str, parent_ts: str
    ) -> list[SavedMessage]:
        if not user_id or not self.queue_store:
            return []

        messages: list[SavedMessage] = []
        for ref in self.queue_store.pop(user_id):
            if ref.channel == parent_channel and ref.ts == parent_ts:
                continue
            messages.extend(self.fetch_message_thread(ref, parent_channel, parent_ts))
        return sorted(
            unique_messages(messages), key=lambda message: slack_ts_key(message.ts)
        )

    def fetch_message_thread(
        self, ref: QueuedMessageRef, parent_channel: str, parent_ts: str
    ) -> list[SavedMessage]:
        message = self.fetch_message(ref)
        if not message:
            return []

        root_ts = message.thread_ts or message.ts
        messages: list[SavedMessage] = []
        cursor: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "channel": ref.channel,
                "ts": root_ts,
                "limit": 200,
            }
            if cursor:
                kwargs["cursor"] = cursor

            response = self.bot_client.conversations_replies(**kwargs)
            for raw_message in response.get("messages", []):
                ts = raw_message.get("ts")
                if not ts or (ref.channel == parent_channel and ts == parent_ts):
                    continue
                messages.append(
                    SavedMessage(
                        channel=ref.channel,
                        ts=ts,
                        user=raw_message.get("user"),
                        text=raw_message.get("text") or "",
                        permalink=self._permalink(ref.channel, ts),
                        thread_ts=raw_message.get("thread_ts"),
                    )
                )

            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return messages

    def fetch_message(self, ref: QueuedMessageRef) -> SavedMessage | None:
        response = self.bot_client.conversations_history(
            channel=ref.channel,
            latest=ref.ts,
            inclusive=True,
            limit=1,
        )
        messages = response.get("messages") or []
        if not messages:
            return None

        message = messages[0]
        permalink = None
        try:
            link_response = self.bot_client.chat_getPermalink(
                channel=ref.channel, message_ts=ref.ts
            )
            permalink = link_response.get("permalink")
        except Exception:
            LOGGER.exception(
                "Could not create permalink for marked message %s/%s",
                ref.channel,
                ref.ts,
            )

        return SavedMessage(
            channel=ref.channel,
            ts=ref.ts,
            user=message.get("user"),
            text=message.get("text") or "",
            permalink=permalink,
            thread_ts=message.get("thread_ts"),
        )

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
        for message in sorted(messages, key=lambda message: slack_ts_key(message.ts)):
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
            try:
                self.user_client.stars_remove(
                    channel=message.channel, timestamp=message.ts
                )
            except Exception as error:
                response = getattr(error, "response", {}) or {}
                slack_error = response.get("error")
                if slack_error in {"missing_scope", "method_deprecated"}:
                    LOGGER.warning("Could not clear saved post: %s", slack_error)
                    continue
                raise

    def remove_trigger_reaction(self, channel: str, parent_ts: str) -> None:
        try:
            self.user_client.reactions_remove(
                channel=channel,
                timestamp=parent_ts,
                name=self.settings.thread_emoji,
            )
        except Exception as error:
            response = getattr(error, "response", {}) or {}
            slack_error = response.get("error")
            if slack_error == "no_reaction":
                LOGGER.info(
                    "Trigger reaction was already absent on %s/%s", channel, parent_ts
                )
                return
            if slack_error == "missing_scope":
                LOGGER.warning(
                    "Could not remove trigger reaction on %s/%s; user token needs reactions:write",
                    channel,
                    parent_ts,
                )
                return
            raise

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


def unique_messages(messages: Iterable[SavedMessage]) -> list[SavedMessage]:
    seen: set[tuple[str, str]] = set()
    unique: list[SavedMessage] = []
    for message in messages:
        key = (message.channel, message.ts)
        if key in seen:
            continue
        seen.add(key)
        unique.append(message)
    return unique


def slack_ts_key(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0
