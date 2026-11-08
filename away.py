""""While you were away": what a returning player missed.

The hub records when each player last left (wall-clock time, the newest
report id and the game date) in <save>.seen.json beside the action log. When
they come back, other players' actions since then are summarised from the
action log, per player, and the announcements since then are fetched from DF.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from catalog import BY_ID, ORDERS_BY_ID

AWAY_MIN_SECONDS = 300   # shorter absences get no summary


class SeenStore:
    """name -> {"t": unix time, "report": newest report id, "date": game date}"""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, dict] = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {}

    def get(self, name: str) -> dict | None:
        return self.data.get(name)

    def left(self, name: str, t: float, report: int, date: str) -> None:
        self.data[name] = {"t": t, "report": report, "date": date}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)   # never a half-written file


def _counted(counter: Counter) -> str:
    return ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in counter.most_common(4)) + \
        (f" and {len(counter) - 4} more" if len(counter) > 4 else "")


def summarize_actions(entries, since: float, me: str) -> list[str]:
    """One line per other player who did anything since `since`."""
    per: dict[str, dict] = defaultdict(lambda: {"dug": 0, "cleared": 0, "built": Counter(), "removed": Counter(),
                                                "ordered": Counter(), "cancelled": 0, "changed": set(),
                                                "squads": set(), "posts": set()})
    for e in entries:
        who = e.get("player")
        if not who or who == me or e.get("t", 0) <= since:
            continue
        p = per[who]
        kind = e.get("type")
        if kind == "dig":
            if e.get("kind") == "erase":
                p["cleared"] += e.get("applied", 0)
            else:
                p["dug"] += e.get("applied", 0)
        elif kind == "build" and e.get("placed"):
            item = BY_ID.get(e.get("item"))
            p["built"][item.label if item else e.get("item", "?")] += e.get("placed", 1)
        elif kind == "remove":
            p["removed"][e.get("what", "something")] += 1
        elif kind == "order":
            o = ORDERS_BY_ID.get(e.get("order"))
            p["ordered"][o.label if o else e.get("order", "?")] += e.get("amount", 1)
        elif kind == "cancel_order":
            p["cancelled"] += 1
        elif kind in ("edit_order", "move_order"):
            p["changed"].add(e.get("order_id"))
        elif kind in ("squad_add", "squad_remove", "rename_squad"):
            p["squads"].add(e.get("squad"))
        elif kind in ("appoint_noble", "vacate_noble"):
            p["posts"].add(e.get("title"))
    lines = []
    for who in sorted(per):
        p = per[who]
        parts = []
        if p["dug"]:
            parts.append(f"designated {p['dug']} tiles for digging")
        if p["cleared"]:
            parts.append(f"cleared {p['cleared']} designations")
        if p["built"]:
            parts.append(f"placed {_counted(p['built'])}")
        if p["removed"]:
            parts.append(f"removed {_counted(p['removed'])}")
        if p["ordered"]:
            parts.append(f"ordered {_counted(p['ordered'])}")
        if p["cancelled"]:
            parts.append(f"cancelled {p['cancelled']} order{'s' if p['cancelled'] > 1 else ''}")
        if p["changed"]:
            parts.append(f"changed {len(p['changed'])} order{'s' if len(p['changed']) > 1 else ''}")
        if p["squads"]:
            parts.append(f"changed {len(p['squads'])} squad{'s' if len(p['squads']) > 1 else ''}")
        if p["posts"]:
            parts.append("changed the " + ", ".join(sorted(str(t) for t in p["posts"])))
        if parts:
            lines.append(f"{who} " + "; ".join(parts))
    return lines
