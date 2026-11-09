"""The test bench's safety rules: it must never act on the real game."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import bench  # noqa: E402


def test_every_lua_call_checks_it_is_on_the_bench(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["code"] = Path(cmd[-1]).read_text()
        seen["env"] = kwargs["env"]
        seen["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(cmd, 0, "fine", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert bench.run_lua_text("print('hi')") == "fine"
    assert seen["code"].startswith(bench.GUARD)
    assert "print('hi')" in seen["code"]
    assert seen["env"]["DFHACK_PORT"] == str(bench.PORT) != "5000"
    assert seen["cwd"] == bench.INSTALL


def test_the_guard_compares_plain_text():
    # `-` is special in a Lua pattern, so a pattern search would never match this path.
    assert "find('df-coop/bench', 1, true)" in bench.GUARD


def test_a_lua_error_is_not_mistaken_for_a_result(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "x.lua:1: not the bench\nstack traceback:\n", ""))
    with pytest.raises(RuntimeError, match="not the bench"):
        bench.run_lua_text("print('hi')")


def test_stop_leaves_processes_it_did_not_start(monkeypatch, tmp_path):
    pids = tmp_path / "pids.json"
    pids.write_text(json.dumps({"df": os.getpid(), "xvfb": os.getpid()}))   # a reused pid, say
    monkeypatch.setattr(bench, "PIDS", pids)
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    assert not bench.running()
    bench.stop()
    assert killed == []
    assert not pids.exists()


def test_stop_ends_df_before_its_display(monkeypatch, tmp_path):
    pids = tmp_path / "pids.json"
    pids.write_text(json.dumps({"xvfb": 11, "df": 22}))
    monkeypatch.setattr(bench, "PIDS", pids)
    alive = {11, 22}
    killed = []
    monkeypatch.setattr(bench, "_is_ours", lambda pid, what: pid in alive)

    def fake_kill(pid, sig):
        killed.append(pid)
        alive.discard(pid)

    monkeypatch.setattr(os, "kill", fake_kill)
    bench.stop()
    assert killed == [22, 11]


def test_only_df_in_the_bench_folder_counts_as_ours(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "INSTALL", tmp_path)
    assert not bench._is_ours(os.getpid(), "df")
    monkeypatch.setattr(bench, "INSTALL", Path.cwd())
    assert bench._is_ours(os.getpid(), "df")
    assert not bench._is_ours(os.getpid(), "xvfb")
    assert not bench._is_ours(2 ** 22 + 1, "df")


def test_find_text_gives_the_middle_of_the_words():
    rows = ["", "     Continue active game    ", "other"]
    assert bench.find_text(rows, "Continue active game") == (15, 1)
    assert bench.find_text(rows, "Quit") is None


def test_setup_refuses_a_missing_save_and_a_running_bench(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "REAL_USER", tmp_path)
    with pytest.raises(SystemExit, match="no save named"):
        bench.setup("nope")
    (tmp_path / "save/real").mkdir(parents=True)
    monkeypatch.setattr(bench, "running", lambda: True)
    with pytest.raises(SystemExit, match="running"):
        bench.setup("real")


def test_working_folder_is_never_taken_for_a_save(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "USER", tmp_path)
    for name, stamp in (("autosave 1", 100), ("current", 999)):
        folder = tmp_path / "save" / name
        folder.mkdir(parents=True)
        (folder / "f").write_text("x")
        os.utime(folder / "f", (stamp, stamp))
    assert list(bench.saves()) == ["autosave 1"]
    assert bench.newest_save() == "autosave 1"
