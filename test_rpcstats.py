"""The once-a-minute line saying how long the calls to DF took."""

from __future__ import annotations

from rpcstats import RpcStats, call_name


def run_lua(module, name):
    pass


def blocks():
    pass


def test_lua_calls_are_named_after_their_function():
    assert call_name(run_lua, ("rpc.coop", "orders")) == "lua:orders"
    assert call_name(run_lua) == "run_lua"
    assert call_name(blocks) == "blocks"
    assert call_name(object()) == "call"


def test_report_waits_for_a_full_window():
    stats = RpcStats(every=60)
    assert stats.report(100.0) is None
    stats.record("blocks", 1.0)
    assert stats.report(159.0) is None
    assert stats.report(160.0) is not None


def test_report_gives_the_share_held_and_the_costliest_call_first():
    stats = RpcStats(every=60)
    stats.report(0.0)
    stats.record("units", 0.5)
    stats.record("blocks", 2.0)
    stats.record("blocks", 3.5)
    stats.record("lua:orders", 0.01)
    assert stats.report(60.0) == ("DF calls took 10.0% of the last 60s: "
                                  "blocks 2x 5.50s (longest 3500 ms), "
                                  "units 1x 0.50s (longest 500 ms), "
                                  "lua:orders 1x 0.01s (longest 10 ms)")


def test_each_window_starts_empty_and_an_idle_one_says_nothing():
    stats = RpcStats(every=60)
    stats.report(0.0)
    stats.record("blocks", 6.0)
    assert stats.report(60.0).startswith("DF calls took 10.0%")
    assert stats.report(120.0) is None
    stats.record("units", 3.0)
    assert stats.report(180.0).startswith("DF calls took 5.0% of the last 60s: units 1x")
