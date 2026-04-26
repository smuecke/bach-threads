from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
from typing import Any, Iterable


LOGGER = logging.getLogger(__name__)
USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")
MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>")


@dataclass(frozen=True)
class WhitelistResult:
    ok: bool
    text: str


class WhitelistStore:
    def __init__(self, path: str | Path, initial_user_ids: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self.initial_user_ids = set(clean_user_ids(initial_user_ids))
        self._memory_user_ids: set[str] | None = None
        if str(path) == ":memory:":
            self._memory_user_ids = set(self.initial_user_ids)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(self.initial_user_ids)

    def list(self) -> set[str]:
        if self._memory_user_ids is not None:
            return set(self._memory_user_ids)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return set(self.initial_user_ids)
        return set(clean_user_ids(data.get("allowed_user_ids", [])))

    def save(self, user_ids: Iterable[str]) -> None:
        data = {"allowed_user_ids": sorted(clean_user_ids(user_ids))}
        if self._memory_user_ids is not None:
            self._memory_user_ids = set(data["allowed_user_ids"])
            return
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def add(self, user_ids: Iterable[str]) -> set[str]:
        allowed = self.list()
        before = set(allowed)
        allowed.update(clean_user_ids(user_ids))
        self.save(allowed)
        return allowed - before

    def remove(self, user_ids: Iterable[str]) -> set[str]:
        allowed = self.list()
        removed = allowed.intersection(clean_user_ids(user_ids))
        allowed.difference_update(removed)
        self.save(allowed)
        return removed

    def contains(self, user_id: str | None) -> bool:
        return bool(user_id and user_id in self.list())


class WhitelistManager:
    def __init__(self, client: Any, store: WhitelistStore) -> None:
        self.client = client
        self.store = store

    def trigger_allowed(self, user_id: str | None) -> bool:
        return self.store.contains(user_id)

    def command_allowed(self, user_id: str | None) -> bool:
        if not user_id:
            return False
        return self.store.contains(user_id) or self._is_slack_admin(user_id)

    def handle_dm_text(self, user_id: str | None, text: str) -> WhitelistResult | None:
        command = parse_whitelist_command(text)
        if not command:
            return None

        if not self.command_allowed(user_id):
            return WhitelistResult(
                False,
                "Sorry, only Slack admins/owners or current whitelist members can manage the whitelist.",
            )

        action, args = command
        if action == "list":
            return WhitelistResult(True, self._format_allowed_users())

        if not args:
            return WhitelistResult(
                False,
                "Please include at least one user, for example `/whitelist add @ada`.",
            )

        resolved = self.resolve_users(args)
        if not resolved:
            return WhitelistResult(False, "I could not find any matching users.")

        if action == "add":
            changed = self.store.add(resolved)
            verb = "Added" if changed else "Already allowed"
            return WhitelistResult(True, f"{verb}: {format_user_ids(resolved)}")

        if action == "remove":
            changed = self.store.remove(resolved)
            verb = "Removed" if changed else "Not currently allowed"
            return WhitelistResult(True, f"{verb}: {format_user_ids(resolved)}")

        return WhitelistResult(
            False,
            "Unknown whitelist command. Use `/whitelist list`, `/whitelist add <users>`, or `/whitelist remove <users>`.",
        )

    def resolve_users(self, tokens: list[str]) -> list[str]:
        resolved: list[str] = []
        unresolved_names: list[str] = []

        for token in tokens:
            mention = MENTION_RE.fullmatch(token)
            if mention:
                resolved.append(mention.group(1))
                continue
            cleaned = token.strip("@, ")
            if USER_ID_RE.fullmatch(cleaned):
                resolved.append(cleaned)
            elif cleaned:
                unresolved_names.append(cleaned.casefold())

        if unresolved_names:
            members = self._all_users()
            for wanted in unresolved_names:
                matches = [member["id"] for member in members if user_matches(member, wanted)]
                if len(matches) == 1:
                    resolved.append(matches[0])

        return list(dict.fromkeys(resolved))

    def _format_allowed_users(self) -> str:
        allowed = sorted(self.store.list())
        if not allowed:
            return (
                "No users are whitelisted yet. Slack admins/owners can add users "
                "with `/whitelist add <users>`."
            )
        return "Whitelisted users:\n" + "\n".join(f"- <@{user_id}>" for user_id in allowed)

    def _is_slack_admin(self, user_id: str) -> bool:
        try:
            response = self.client.users_info(user=user_id)
        except Exception:
            LOGGER.exception("Could not check Slack admin status for %s", user_id)
            return False
        user = response.get("user") or {}
        return bool(
            user.get("is_admin")
            or user.get("is_owner")
            or user.get("is_primary_owner")
        )

    def _all_users(self) -> list[dict[str, Any]]:
        members: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            response = self.client.users_list(**kwargs)
            members.extend(response.get("members", []))
            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return members


def clean_user_ids(user_ids: Iterable[str]) -> list[str]:
    return [user_id for user_id in user_ids if USER_ID_RE.fullmatch(user_id)]


def parse_user_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return clean_user_ids(
        part.strip() for part in re.split(r"[\s,]+", value) if part.strip()
    )


def parse_whitelist_command(text: str) -> tuple[str, list[str]] | None:
    parts = text.strip().split()
    if len(parts) < 2 or parts[0].lower() not in {"/whitelist", "whitelist"}:
        return None
    return parts[1].lower(), parts[2:]


def format_user_ids(user_ids: Iterable[str]) -> str:
    return ", ".join(f"<@{user_id}>" for user_id in user_ids)


def user_lookup_names(member: dict[str, Any]) -> set[str]:
    profile = member.get("profile") or {}
    names = {
        member.get("id"),
        member.get("name"),
        member.get("real_name"),
        profile.get("display_name"),
        profile.get("display_name_normalized"),
        profile.get("real_name"),
        profile.get("real_name_normalized"),
    }
    return {str(name).casefold() for name in names if name}


def user_matches(member: dict[str, Any], wanted: str) -> bool:
    return bool(member.get("id") and wanted in user_lookup_names(member))
