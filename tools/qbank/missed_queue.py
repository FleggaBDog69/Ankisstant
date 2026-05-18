# Persistent queue of captured "missed questions" awaiting review.
# Stored in ankisstant/user_files/missed_queue.json.

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime


def _queue_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    uf = os.path.join(root, "user_files")
    os.makedirs(uf, exist_ok=True)
    return os.path.join(uf, "missed_queue.json")


def load_queue() -> list[dict]:
    path = _queue_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_queue(items: list[dict]) -> None:
    with open(_queue_path(), "w") as f:
        json.dump(items, f, indent=2)


def add_item(
    stem: str,
    concept: str,
    platform: str = "",
    system: str = "",
    subsystem: str = "",
    topic: str = "",
) -> dict:
    item = {
        "id":          uuid.uuid4().hex[:12],
        "stem":        stem.strip(),
        "concept":     concept.strip(),
        "system":      system.strip(),
        "subsystem":   subsystem.strip(),
        "topic":       topic.strip(),
        "platform":    platform,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    items = load_queue()
    items.append(item)
    save_queue(items)
    return item


def remove_item(item_id: str) -> None:
    save_queue([i for i in load_queue() if i.get("id") != item_id])


def queue_count() -> int:
    return len(load_queue())
