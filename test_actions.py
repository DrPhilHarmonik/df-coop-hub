"""Digging rules, rate limiting, the action log and designation owners."""

from __future__ import annotations

import pytest

pytest.importorskip("google.protobuf", reason="df-coop needs the coop extra")

from actions import (ActionLog, Owners, RateLimiter, color_for, designation_spans, eligible,
                     rect_tiles)


# -- what can be dug ----------------------------------------------------------

@pytest.mark.parametrize("kind", ["dig", "updown", "up", "down", "ramp", "channel"])
def test_every_dig_kind_cuts_natural_rock(kind):
    assert eligible(kind, "WALL", "STONE", hidden=False)


@pytest.mark.parametrize("kind,ok", [("channel", True), ("down", True), ("updown", True),
                                     ("dig", False), ("ramp", False), ("up", False)])
def test_from_a_floor_only_downward_kinds_work(kind, ok):
    assert eligible(kind, "FLOOR", "SOIL", hidden=False) is ok


@pytest.mark.parametrize("material", ["CONSTRUCTION", "TREE_MATERIAL", "MAGMA", "RIVER"])
def test_built_walls_trees_and_liquids_are_not_dug(material):
    assert not eligible("dig", "WALL", material, hidden=False)


def test_open_air_takes_nothing_but_erase():
    assert not eligible("dig", None, None, hidden=False)
    assert not eligible("channel", "EMPTY", "AIR", hidden=False)
    assert eligible("erase", None, None, hidden=False)


def test_unexplored_tiles_take_anything_like_in_df():
    # Also keeps the hub from leaking what's hidden through "skipped" counts:
    # a hidden constructed wall answers the same as hidden rock.
    assert eligible("dig", "WALL", "CONSTRUCTION", hidden=True)
    assert eligible("ramp", None, None, hidden=True)


# -- rectangles ----------------------------------------------------------------

def test_rect_is_normalised_and_clamped_to_the_map():
    assert rect_tiles(10, 8, 2, 3, 32, 32) == (2, 3, 10, 8)
    assert rect_tiles(-5, -5, 3, 40, 32, 32) == (0, 0, 3, 31)


def test_rect_entirely_off_the_map_is_none():
    assert rect_tiles(40, 40, 50, 50, 32, 32) is None


# -- rate limiting -------------------------------------------------------------

def test_rate_limiter_allows_a_burst_then_refills():
    lim = RateLimiter(rate=2.0, burst=3.0, tokens=3.0, stamp=0.0)
    assert [lim.take(0.0) for _ in range(4)] == [True, True, True, False]
    assert lim.take(0.5)        # half a second buys one more
    assert not lim.take(0.5)


# -- colours -------------------------------------------------------------------

def test_player_colour_is_stable_by_name():
    assert color_for("Jeremy") == color_for("Jeremy")
    assert color_for("Jeremy").startswith("#")


# -- log and owners ------------------------------------------------------------

def test_log_survives_a_torn_last_line(tmp_path):
    log = ActionLog(tmp_path / "save.actions.jsonl")
    log.append({"type": "dig", "player": "A", "kind": "dig", "rect": [0, 0, 1, 0], "z": 3})
    with log.path.open("a") as f:
        f.write('{"type": "dig", "pla')   # crash mid-write
    assert len(list(log.entries())) == 1


def test_owners_replay_in_order_and_erase_clears(tmp_path):
    log = ActionLog(tmp_path / "s.jsonl")
    log.append({"type": "dig", "player": "A", "kind": "dig", "rect": [0, 0, 2, 0], "z": 1})
    log.append({"type": "dig", "player": "B", "kind": "channel", "rect": [2, 0, 2, 0], "z": 1})
    log.append({"type": "dig", "player": "B", "kind": "erase", "rect": [0, 0, 0, 0], "z": 1})
    owners = Owners()
    owners.replay(log)
    assert owners.by_tile == {(1, 0, 1): ("A", 4), (2, 0, 1): ("B", 4)}


def test_prune_drops_only_tiles_known_to_be_clear():
    owners = Owners()
    owners.apply("dig", [(0, 0, 1), (1, 0, 1), (2, 0, 1), (0, 0, 2)], "A")
    state = {(0, 0, 1): False, (1, 0, 1): True, (2, 0, 1): None}  # None: not seen yet
    assert owners.prune(1, lambda x, y, z: state[(x, y, z)])
    assert set(owners.by_tile) == {(1, 0, 1), (2, 0, 1), (0, 0, 2)}


def test_spans_preserve_gaps_and_compress_a_whole_column():
    tiles = [(2, 4, 5), (2, 2, 5), (1, 3, 5), (1, 2, 5), (2, 2, 5)]
    assert designation_spans(tiles) == [[1, 2, 3], [2, 2, 2], [2, 4, 4]]
    assert designation_spans((7, y, 5) for y in range(4096)) == [[7, 0, 4095]]
    assert designation_spans([]) == []


def test_owners_replay_exact_spans_and_legacy_rectangles(tmp_path):
    log = ActionLog(tmp_path / "spans.jsonl")
    log.append({"type": "dig", "player": "Bob", "kind": "channel", "rect": [0, 0, 2, 0],
                "z": 5, "priority": 6})
    log.append({"type": "dig", "player": "Alice", "kind": "dig", "rect": [0, 0, 2, 1],
                "spans": [[0, 1, 1], [2, 0, 1]], "z": 5, "priority": 1})
    log.append({"type": "dig", "player": "Alice", "kind": "erase", "rect": [0, 0, 2, 1],
                "spans": [[2, 1, 1]], "z": 5})
    log.append({"type": "dig", "player": "Alice", "kind": "dig", "rect": [0, 0, 2, 1],
                "spans": [], "z": 5})
    owners = Owners()
    owners.replay(log)
    assert owners.by_tile == {(0, 0, 5): ("Bob", 6), (1, 0, 5): ("Bob", 6),
                              (0, 1, 5): ("Alice", 1), (2, 0, 5): ("Alice", 1)}
