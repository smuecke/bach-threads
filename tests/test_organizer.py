from bachthreads.organizer import (
    SavedMessage,
    Settings,
    ThreadOrganizer,
    format_thread_reply,
    reminder_text,
    saved_item_to_message,
    unique_author_ids,
)
from bachthreads.message_queue import MessageQueueStore
from bachthreads.whitelist import WhitelistManager, WhitelistStore, parse_whitelist_command


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            response = self.responses.get(name)
            if callable(response):
                return response(**kwargs)
            return response or {"ok": True}

        return call


def test_saved_item_to_message_extracts_message_items():
    item = {
        "type": "message",
        "channel": "C1",
        "message": {"ts": "123.456", "user": "U1", "text": "hello"},
        "url": "https://example.test/message",
    }

    assert saved_item_to_message(item) == SavedMessage(
        channel="C1",
        ts="123.456",
        user="U1",
        text="hello",
        permalink="https://example.test/message",
    )


def test_saved_item_to_message_ignores_non_messages():
    assert saved_item_to_message({"type": "file", "channel": "C1"}) is None


def test_unique_author_ids_preserves_order_and_skips_missing_users():
    messages = [
        SavedMessage("C1", "1", "U1", "one"),
        SavedMessage("C1", "2", None, "two"),
        SavedMessage("C1", "3", "U2", "three"),
        SavedMessage("C1", "4", "U1", "four"),
    ]

    assert unique_author_ids(messages) == ["U1", "U2"]


def test_format_thread_reply_includes_author_text_and_source_link():
    message = SavedMessage("C1", "1", "U1", "hello world", "https://example.test")

    assert format_thread_reply(message) == (
        "<@U1> wrote:\n>hello world\nOriginal: https://example.test"
    )


def test_reminder_text_links_to_thread_and_help_page():
    text = reminder_text("https://example.test/thread", "https://slack.test/help")

    assert "https://example.test/thread" in text
    assert "https://slack.test/help" in text
    assert "Antwort-Funktion" in text
    assert "Keine Sorge" in text


def test_handle_reaction_added_without_marked_messages_cleans_up_trigger_reaction():
    bot = FakeClient(
        {"chat_getPermalink": {"permalink": "https://example.test/thread"}}
    )
    user = FakeClient()
    organizer = ThreadOrganizer(
        bot,
        user,
        Settings(
            thread_emoji="thread",
            message_emoji="bookmark",
            reminder_help_url="https://slack.test/help",
        ),
        WhitelistManager(bot, WhitelistStore(":memory:", ["UORGANIZER"])),
        MessageQueueStore(":memory:"),
    )

    result = organizer.handle_reaction_added(
        {
            "type": "reaction_added",
            "user": "UORGANIZER",
            "reaction": "thread",
            "item": {"type": "message", "channel": "C1", "ts": "999.999"},
        }
    )

    assert result == {"status": "done", "posted": 0, "reminded": 0}
    assert ("reactions_remove", {"channel": "C1", "timestamp": "999.999", "name": "thread"}) in user.calls


def test_handle_reaction_added_ignores_users_outside_whitelist():
    organizer = ThreadOrganizer(
        FakeClient(),
        FakeClient(),
        Settings("thread", "bookmark", "https://slack.test/help"),
        WhitelistManager(FakeClient(), WhitelistStore(":memory:", ["UORGANIZER"])),
    )

    result = organizer.handle_reaction_added(
        {
            "type": "reaction_added",
            "user": "U_OTHER",
            "reaction": "thread",
            "item": {"type": "message", "channel": "C1", "ts": "999.999"},
        }
    )

    assert result == {"status": "ignored", "reason": "not_whitelisted"}


def test_message_marker_reaction_queues_message_for_trigger_user():
    store = MessageQueueStore(":memory:")
    organizer = ThreadOrganizer(
        FakeClient(),
        FakeClient(),
        Settings("thread", "bookmark", "https://slack.test/help"),
        WhitelistManager(FakeClient(), WhitelistStore(":memory:", ["UORGANIZER"])),
        store,
    )

    result = organizer.handle_reaction_added(
        {
            "type": "reaction_added",
            "user": "UORGANIZER",
            "reaction": "bookmark",
            "item": {"type": "message", "channel": "C1", "ts": "111.111"},
        }
    )

    assert result == {"status": "queued", "queued": 1}
    assert store.pop("UORGANIZER")[0].ts == "111.111"


def test_thread_trigger_moves_marked_messages_and_their_replies_in_order():
    store = MessageQueueStore(":memory:")
    store.add("UORGANIZER", "C1", "300.000")
    store.add("UORGANIZER", "C1", "100.000")

    def conversations_history(**kwargs):
        messages = {
            "100.000": {"ts": "100.000", "user": "U1", "text": "stray one"},
            "300.000": {"ts": "300.000", "user": "U3", "text": "stray two"},
        }
        return {"messages": [messages[kwargs["latest"]]]}

    def conversations_replies(**kwargs):
        if kwargs["ts"] == "100.000":
            return {
                "messages": [
                    {"ts": "100.000", "user": "U1", "text": "stray one"},
                    {
                        "ts": "200.000",
                        "user": "U2",
                        "text": "reply to stray one",
                        "thread_ts": "100.000",
                    },
                ]
            }
        return {
            "messages": [
                {"ts": "300.000", "user": "U3", "text": "stray two"},
            ]
        }

    def chat_get_permalink(**kwargs):
        return {"permalink": f"https://example.test/{kwargs['message_ts']}"}

    bot = FakeClient(
        {
            "conversations_history": conversations_history,
            "conversations_replies": conversations_replies,
            "chat_getPermalink": chat_get_permalink,
            "conversations_open": {"channel": {"id": "D1"}},
        }
    )
    user = FakeClient()
    organizer = ThreadOrganizer(
        bot,
        user,
        Settings("thread", "bookmark", "https://slack.test/help"),
        WhitelistManager(bot, WhitelistStore(":memory:", ["UORGANIZER"])),
        store,
    )

    result = organizer.handle_reaction_added(
        {
            "type": "reaction_added",
            "user": "UORGANIZER",
            "reaction": "thread",
            "item": {"type": "message", "channel": "C1", "ts": "999.999"},
        }
    )

    posted = [
        kwargs["text"]
        for name, kwargs in bot.calls
        if name == "chat_postMessage" and kwargs["channel"] == "C1"
    ]
    assert result == {"status": "done", "posted": 3, "reminded": 3}
    assert posted == [
        "<@U1> wrote:\n>stray one\nOriginal: https://example.test/100.000",
        "<@U2> wrote:\n>reply to stray one\nOriginal: https://example.test/200.000",
        "<@U3> wrote:\n>stray two\nOriginal: https://example.test/300.000",
    ]
    assert not [call for call in user.calls if call[0] == "stars_remove"]


def test_parse_whitelist_command():
    assert parse_whitelist_command("/whitelist add @ada U123") == (
        "add",
        ["@ada", "U123"],
    )
    assert parse_whitelist_command("whitelist list") == ("list", [])
    assert parse_whitelist_command("hello") is None


def test_whitelist_admin_can_add_and_remove_users(tmp_path):
    client = FakeClient(
        {
            "users_info": {"user": {"is_admin": True}},
            "users_list": {
                "members": [
                    {
                        "id": "UADA",
                        "name": "ada",
                        "profile": {"display_name": "Ada"},
                    }
                ]
            },
        }
    )
    manager = WhitelistManager(client, WhitelistStore(tmp_path / "allow.json"))

    added = manager.handle_dm_text("UADMIN", "/whitelist add ada")
    listed = manager.handle_dm_text("UADMIN", "/whitelist list")
    removed = manager.handle_dm_text("UADMIN", "/whitelist remove <@UADA>")

    assert added and added.ok
    assert listed and "<@UADA>" in listed.text
    assert removed and removed.ok
    assert not manager.trigger_allowed("UADA")


def test_whitelist_rejects_non_admin_non_whitelisted_user(tmp_path):
    client = FakeClient({"users_info": {"user": {"is_admin": False}}})
    manager = WhitelistManager(client, WhitelistStore(tmp_path / "allow.json"))

    result = manager.handle_dm_text("UNORMAL", "/whitelist add UADA")

    assert result
    assert not result.ok
    assert "only Slack admins" in result.text
