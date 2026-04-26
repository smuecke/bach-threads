from bachthreads.organizer import (
    SavedMessage,
    Settings,
    ThreadOrganizer,
    format_thread_reply,
    reminder_text,
    saved_item_to_message,
    unique_author_ids,
)
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
    assert "reply function" in text


def test_handle_reaction_added_moves_saved_messages_and_cleans_up():
    saved_items = [
        {
            "type": "message",
            "channel": "C1",
            "message": {"ts": "111.111", "user": "U1", "text": "belongs here"},
            "url": "https://example.test/original",
        },
        {
            "type": "message",
            "channel": "C1",
            "message": {"ts": "999.999", "user": "U2", "text": "parent"},
        },
    ]
    bot = FakeClient(
        {
            "conversations_open": {"channel": {"id": "D1"}},
            "chat_getPermalink": {"permalink": "https://example.test/thread"},
        }
    )
    user = FakeClient({"stars_list": {"items": saved_items}})
    organizer = ThreadOrganizer(
        bot,
        user,
        Settings(
            thread_emoji="thread",
            reminder_help_url="https://slack.test/help",
        ),
        WhitelistManager(bot, WhitelistStore(":memory:", ["UORGANIZER"])),
    )

    result = organizer.handle_reaction_added(
        {
            "type": "reaction_added",
            "user": "UORGANIZER",
            "reaction": "thread",
            "item": {"type": "message", "channel": "C1", "ts": "999.999"},
        }
    )

    assert result == {"status": "done", "posted": 1, "reminded": 1}
    assert ("chat_postMessage", {"channel": "C1", "thread_ts": "999.999", "text": "<@U1> wrote:\n>belongs here\nOriginal: https://example.test/original", "unfurl_links": False, "unfurl_media": False}) in bot.calls
    assert ("stars_remove", {"channel": "C1", "timestamp": "111.111"}) in user.calls
    assert ("reactions_remove", {"channel": "C1", "timestamp": "999.999", "name": "thread"}) in bot.calls


def test_handle_reaction_added_ignores_users_outside_whitelist():
    organizer = ThreadOrganizer(
        FakeClient(),
        FakeClient(),
        Settings("thread", "https://slack.test/help"),
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


def test_parse_whitelist_command():
    assert parse_whitelist_command("/whitelist add @ada U123") == (
        "add",
        ["@ada", "U123"],
    )
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
