from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class QueuedMessageRef:
    channel: str
    ts: str


class MessageQueueStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._memory: dict[str, list[dict[str, str]]] | None = None
        if str(path) == ":memory:":
            self._memory = {}
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save({})

    def add(self, user_id: str, channel: str, ts: str) -> None:
        data = self._load()
        refs = data.setdefault(user_id, [])
        ref = {"channel": channel, "ts": ts}
        if ref not in refs:
            refs.append(ref)
        self._save(data)

    def pop(self, user_id: str) -> list[QueuedMessageRef]:
        data = self._load()
        refs = [
            QueuedMessageRef(ref["channel"], ref["ts"])
            for ref in data.pop(user_id, [])
            if ref.get("channel") and ref.get("ts")
        ]
        self._save(data)
        return refs

    def _load(self) -> dict[str, list[dict[str, str]]]:
        if self._memory is not None:
            return {user_id: list(refs) for user_id, refs in self._memory.items()}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return {
            user_id: refs
            for user_id, refs in data.items()
            if isinstance(user_id, str) and isinstance(refs, list)
        }

    def _save(self, data: dict[str, Iterable[dict[str, str]]]) -> None:
        normalized = {user_id: list(refs) for user_id, refs in data.items()}
        if self._memory is not None:
            self._memory = normalized
            return
        self.path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")

