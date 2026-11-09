"""What a returning player is told they missed."""

from __future__ import annotations

import json

from away import SeenStore, summarize_actions


def entry(t, player, **kw):
    return {"t": t, "player": player, **kw}


def test_summary_is_per_other_player_and_only_since_they_left():
    entries = [
        entry(50, "Bob", type="dig", kind="dig", applied=99),            # before Alice left
        entry(150, "Bob", type="dig", kind="dig", applied=40),
        entry(160, "Bob", type="dig", kind="channel", applied=5),
        entry(170, "Bob", type="build", item="bed", placed=1),
        entry(171, "Bob", type="build", item="bed", placed=1),
        entry(172, "Bob", type="build", item="carpenter", placed=0),     # failed placement: not news
        entry(180, "Carol", type="order", order="o_barrel", amount=10),
        entry(181, "Carol", type="cancel_order", label="Beds"),
        entry(182, "Carol", type="remove", what="Statue"),
        entry(183, "Carol", type="dig", kind="erase", applied=3),
        entry(190, "Alice", type="dig", kind="dig", applied=7),          # her own: left out
    ]
    assert summarize_actions(entries, since=100, me="Alice") == [
        "Bob designated 45 tiles for digging; placed Bed ×2",
        "Carol cleared 3 designations; removed Statue; ordered Barrels ×10; cancelled 1 order",
    ]


def test_changing_an_order_counts_once_however_many_times_it_moved():
    entries = [
        entry(150, "Bob", type="move_order", order_id=5, to="up"),
        entry(151, "Bob", type="move_order", order_id=5, to="up"),
        entry(152, "Bob", type="edit_order", order_id=5, amount=3),
        entry(153, "Bob", type="edit_order", order_id=6, freq="Daily"),
    ]
    assert summarize_actions(entries, since=100, me="Alice") == ["Bob changed 2 orders"]


def test_squad_changes_count_each_squad_once():
    entries = [
        entry(150, "Bob", type="squad_add", squad=2, name="Urist"),
        entry(151, "Bob", type="squad_remove", squad=2, name="Urist"),
        entry(152, "Bob", type="rename_squad", squad=3, name="Axes"),
    ]
    assert summarize_actions(entries, since=100, me="Alice") == ["Bob changed 2 squads"]


def test_noble_changes_name_the_posts():
    entries = [
        entry(150, "Bob", type="appoint_noble", post=1, title="manager", name="Urist"),
        entry(151, "Bob", type="vacate_noble", post=1, title="manager", name="Urist"),
        entry(152, "Bob", type="appoint_noble", post=2, title="doctor", name="Kadol"),
    ]
    assert summarize_actions(entries, since=100, me="Alice") == ["Bob changed the doctor, manager"]


def test_place_changes_count_each_place_once():
    entries = [
        entry(150, "Bob", type="stockpile_category", place=300, args=["food", "1"]),
        entry(151, "Bob", type="stockpile_containers", place=300, args=["1", "2", "0"]),
        entry(152, "Bob", type="zone_owner", place=58, args=["7"]),
    ]
    assert summarize_actions(entries, since=100, me="Alice") == ["Bob changed 2 stockpiles or zones"]


def test_squad_orders_count_as_changing_that_squad():
    entries = [
        entry(150, "Bob", type="squad_station", squad=2, args=["1", "2", "3"]),
        entry(151, "Bob", type="squad_clear", squad=2, args=[]),
        entry(152, "Bob", type="squad_kill", squad=3, args=["7"]),
    ]
    assert summarize_actions(entries, since=100, me="Alice") == ["Bob changed 2 squads"]


def test_nothing_happened_gives_no_lines():
    assert summarize_actions([entry(10, "Bob", type="dig", kind="dig", applied=5)], since=100, me="Alice") == []


def test_long_lists_are_trimmed():
    entries = [entry(200, "Bob", type="build", item=i, placed=1)
               for i in ("bed", "table", "chair", "door", "cabinet", "statue")]
    line = summarize_actions(entries, since=100, me="Alice")[0]
    assert line.endswith("and 2 more")


def test_seen_store_round_trips_and_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "s.seen.json"
    SeenStore(path).left("Alice", 123.0, 77, "1st Granite, year 1")
    assert SeenStore(path).get("Alice") == {"t": 123.0, "report": 77, "date": "1st Granite, year 1"}
    path.write_text("{not json")
    assert SeenStore(path).get("Alice") is None
    assert not list(tmp_path.glob("*.tmp"))


def test_changing_an_orders_conditions_counts_as_changing_that_order():
    entries = [
        entry(150, "Bob", type="order_conditions", order_id=5, change="add_condition"),
        entry(151, "Bob", type="order_conditions", order_id=5, change="remove_condition"),
        entry(152, "Bob", type="edit_order", order_id=5, max_workshops=2),
    ]
    assert summarize_actions(entries, since=100, me="Alice") == ["Bob changed 1 order"]


def test_changing_a_work_detail_is_named_once():
    entries = [
        entry(150, "Bob", type="work_detail_member", detail="Miners", unit=5, on=True),
        entry(151, "Bob", type="work_detail_member", detail="Miners", unit=6, on=True),
        entry(152, "Bob", type="work_detail_mode", detail="Haulers", mode="NobodyDoesThis"),
    ]
    assert summarize_actions(entries, since=100, me="Alice") == ["Bob changed the Haulers, Miners work details"]


def test_workshop_changes_count_each_workshop_once():
    entries = [{"type": "workshop_job", "t": 101, "player": "Bob", "place": 400},
               {"type": "workshop_worker", "t": 102, "player": "Bob", "place": 400},
               {"type": "workshop_job", "t": 103, "player": "Bob", "place": 401}]
    assert summarize_actions(entries, since=100, me="Alice") == ["Bob changed tasks or workers at 2 workshops"]


def test_farm_changes_count_each_plot_once():
    entries = [{"type": "farm_crop", "t": 101, "player": "Bob", "place": 500},
               {"type": "farm_fertilize", "t": 102, "player": "Bob", "place": 500}]
    assert summarize_actions(entries, since=100, me="Alice") == ["Bob changed 1 farm plot"]
