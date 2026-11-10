"""How long the hub's calls to Dwarf Fortress take.

The hub logs this once a minute, to show which calls are worth making cheaper.
It is not the frame rate the hub costs: much of a call's time is spent waiting
for DF to reach a safe point. Measured 11-08, calls took 19% of the time while
the game slowed by 3-7%.
"""

from __future__ import annotations

REPORT_EVERY = 60.0


def call_name(fn, args=()) -> str:
    """The call's name; a Lua call is named after its function, since they all
    go through run_lua and differ widely in cost."""
    name = getattr(fn, "__name__", "call")
    if name == "run_lua" and len(args) >= 2:
        return f"lua:{args[1]}"
    return name


class RpcStats:
    def __init__(self, every: float = REPORT_EVERY):
        self.every = every
        self.window_start: float | None = None
        self.calls: dict[str, list[float]] = {}  # name -> [count, total, longest]

    def record(self, name: str, seconds: float) -> None:
        entry = self.calls.setdefault(name, [0, 0.0, 0.0])
        entry[0] += 1
        entry[1] += seconds
        entry[2] = max(entry[2], seconds)

    def report(self, now: float) -> str | None:
        """A summary line once per window, or None if it isn't due or nothing
        was called. Starts a new window whenever it comes due."""
        if self.window_start is None:
            self.window_start = now
            return None
        elapsed = now - self.window_start
        if elapsed < self.every:
            return None
        calls, self.calls = self.calls, {}
        self.window_start = now
        if not calls:
            return None
        busy = sum(c[1] for c in calls.values())
        parts = [f"{name} {count}x {total:.2f}s (longest {longest * 1000:.0f} ms)"
                 for name, (count, total, longest)
                 in sorted(calls.items(), key=lambda kv: kv[1][1], reverse=True)]
        return f"DF calls took {busy / elapsed:.1%} of the last {elapsed:.0f}s: " + ", ".join(parts)
