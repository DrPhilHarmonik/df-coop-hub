"""Exercise the actual Lua priority writer against a small fake map."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_priority_spans_validate_before_writing_and_preserve_other_orders():
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    script = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
local decoded
package.preload.json = function()
    return {decode=function() return decoded end, encode=function(value) return value.set end}
end
local block = {designation={}, flags={}, block_events={}}
local event = {priority={}}
local lookups, block_lookups = 0, 0
function event:getType() lookups = lookups + 1; return 1 end
block.block_events[1] = event
for x=0,15 do
    block.designation[x], event.priority[x] = {}, {}
    for y=0,15 do
        block.designation[x][y] = {dig=1}
        event.priority[x][y] = 6000
    end
end
block.designation[3][4].dig = 0
df = {tile_dig_designation={No=0}, block_square_event_type={designation_priority=1}}
dfhack = {isMapLoaded=function() return true end, maps={
    isValidTilePos=function(x,y,z) return x >= 0 and y >= 0 and x < 128 and y < 128 and z == 5 end,
    getTileBlock=function() block_lookups = block_lookups + 1; return block end,
}}
assert(loadfile(arg[1]))()
assert(module.ping() == '10')
decoded = {{2,3,3}, {2,5,6}, {3,4,4}}
assert(module.set_priority_spans('fixture', '5', '1') == 3)
assert(event.priority[2][3] == 1000 and event.priority[2][5] == 1000 and event.priority[2][6] == 1000)
assert(event.priority[2][4] == 6000 and event.priority[3][4] == 6000)
assert(block.flags.designated and lookups == 1 and block_lookups == 1)
local function rejected(spans, z, priority)
    decoded = spans
    block.flags.designated = false
    local before = event.priority[2][3]
    assert(not pcall(module.set_priority_spans, 'fixture', z or '5', priority or '2'))
    assert(not block.flags.designated and event.priority[2][3] == before)
end
rejected({{2,3,3},{2,4,4},{2,2,2}})
rejected({{2,3,3},{2,3,4}})
rejected({{2,3,3},{3,4,2}})
rejected({{2,3,3},{3,-1,2}})
rejected({{2,3,3},{3,128,128}})
rejected({{2,3,3},{3,4.5,5}})
rejected({{2,3,3},{'3',4,5}})
rejected({{2,3,3}}, '6')
rejected({{2,3,3}}, '5', '8')
rejected({})
local huge = {}
for x=0,32 do huge[#huge+1] = {x,0,127} end
rejected(huge)
dfhack.isMapLoaded = function() return false end
rejected({{2,3,3}})
"""
    result = subprocess.run([lua, "-", str(source)], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_order_editing_changes_only_the_named_order_and_checks_inputs():
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    script = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
package.preload.json = function()
    return {encode=function(value) return value end}
end
-- A 0-based vector, like DF's.
local items = {}
local all = setmetatable({}, {
    __index=function(_, k)
        if k == 'erase' then return function(_, i) table.remove(items, i + 1) end end
        if k == 'insert' then return function(_, i, v) table.insert(items, i + 1, v) end end
        return items[k + 1]
    end,
    __len=function() return #items end,
})
local function make(id) return {id=id, amount_left=2, amount_total=5, frequency=0} end
items[1], items[2], items[3] = make(10), make(11), make(12)
df = {workquota_frequency_type={NONE=-1, OneTime=0, Daily=1, Monthly=2},
      global={world={manager_orders={all=all}}}}
assert(loadfile(arg[1]))()
local function ids() return items[1].id .. ',' .. items[2].id .. ',' .. items[3].id end

assert(module.set_order('11', '7', '').ok)
assert(items[2].amount_left == 7 and items[2].amount_total == 7 and items[2].frequency == 0)
assert(module.set_order('11', '', 'Daily').ok)
assert(items[2].amount_total == 7 and items[2].frequency == 1)
assert(items[1].amount_total == 5 and items[3].frequency == 0)
assert(module.set_order('99', '7', '').missing)
for _, bad in ipairs{{'11', '0', ''}, {'11', '1000', ''}, {'11', '1.5', ''}, {'11', '', 'NONE'},
                     {'11', '', 'Hourly'}, {'11', '', 'Daily"'}, {'11', '', ''}, {'x', '1', ''}} do
    assert(not pcall(module.set_order, bad[1], bad[2], bad[3]), bad[3])
end
assert(items[2].amount_total == 7 and items[2].frequency == 1)

assert(module.move_order('12', 'up').ok and ids() == '10,12,11')
assert(module.move_order('10', 'up').ok and ids() == '10,12,11')       -- already first
assert(module.move_order('11', 'down').ok and ids() == '10,12,11')     -- already last
assert(module.move_order('11', 'top').ok and ids() == '11,10,12')
assert(module.move_order('11', 'bottom').ok and ids() == '10,12,11')
assert(module.move_order('99', 'up').missing and ids() == '10,12,11')
assert(not pcall(module.move_order, '10', 'sideways') and ids() == '10,12,11')
"""
    result = subprocess.run([lua, "-", str(source)], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_squad_changes_check_who_and_where_before_touching_df():
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    script = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
package.preload.json = function()
    return {encode=function(value) return value end}
end
local function positions(occupants)
    local p = setmetatable({}, {__len=function() return #occupants end})
    for i, hf in ipairs(occupants) do p[i - 1] = {occupant=hf} end
    return p
end
local squads = {
    [1]={id=1, entity_id=20, alias='', positions=positions{100, 101, -1}},
    [2]={id=2, entity_id=20, alias='', positions=positions{100, 101}},        -- full
    [3]={id=3, entity_id=20, alias='', positions=positions{-1, -1}},          -- no leader
    [4]={id=4, entity_id=99, alias='', positions=positions{100, -1}},         -- someone else's
}
local function unit(id, squad, slot, adult, citizen, inactive)
    return {id=id, adult=adult ~= false, citizen=citizen ~= false,
            flags1={inactive=inactive or false},
            military={squad_id=squad or -1, squad_position=slot or -1}}
end
local units = {[1]=unit(1, 1, 0), [2]=unit(2, 1, 1), [3]=unit(3), [4]=unit(4, nil, nil, false),
               [5]=unit(5, nil, nil, true, false), [6]=unit(6, 2, 1), [7]=unit(7, 1, 2, true, true, true)}
local calls = {}
df = {squad={find=function(id) return squads[id] end}, unit={find=function(id) return units[id] end},
      global={plotinfo={group_id=20}}}
dfhack = {isMapLoaded=function() return true end, df2utf=function(s) return s end, utf2df=function(s) return s end,
    units={isCitizen=function(u) return u.citizen end, isAdult=function(u) return u.adult end,
           getReadableName=function(u) return 'unit' .. u.id end},
    military={getSquadName=function(id) return squads[id].alias ~= '' and squads[id].alias or 'generated' end,
        addToSquad=function(uid, sid, slot)
            table.insert(calls, 'add')
            if units[uid].refuse then return false end
            units[uid].military.squad_id, units[uid].military.squad_position = sid, 2
            return true
        end,
        removeFromSquad=function(uid)
            table.insert(calls, 'remove')
            if not units[uid].refuse then units[uid].military.squad_id, units[uid].military.squad_position = -1, -1 end
        end}}
assert(loadfile(arg[1]))()
local function refuses(fn, needle, ...)
    local r = fn(...)
    assert(r.ok == false and r.why:find(needle, 1, true), tostring(r.why))
    assert(#calls == 0)
end
refuses(module.squad_add, 'gone', '9', '3')
refuses(module.squad_add, 'gone', '4', '3')           -- not this fort's
refuses(module.squad_add, 'adult citizens', '1', '4')
refuses(module.squad_add, 'adult citizens', '1', '5')
refuses(module.squad_add, 'adult citizens', '1', '99')
refuses(module.squad_add, 'already in a squad', '1', '2')
refuses(module.squad_add, 'full', '2', '3')
refuses(module.squad_add, 'needs a leader', '3', '3')
refuses(module.squad_remove, 'gone', '4', '3')
refuses(module.squad_remove, 'not in this squad', '2', '2')
refuses(module.squad_remove, 'not in this squad', '1', '3')
refuses(module.squad_remove, 'leader', '1', '1')
refuses(module.squad_remove, 'away', '1', '7')
assert(not pcall(module.squad_add, 'x', '3') and not pcall(module.squad_add, '1', 'y'))

local added = module.squad_add('1', '3')
assert(added.ok and added.name == 'unit3' and units[3].military.squad_id == 1 and calls[1] == 'add')
local removed = module.squad_remove('1', '3')
assert(removed.ok and units[3].military.squad_id == -1 and calls[2] == 'remove')
-- a refusal is noticed by looking at the unit, whatever the call returned
units[2].refuse = true
assert(module.squad_remove('1', '2').why == 'DF refused that')
units[3].refuse = true
assert(module.squad_add('1', '3').why == 'DF refused that')

assert(module.rename_squad('1', 'Axes').name == 'Axes' and squads[1].alias == 'Axes')
assert(module.rename_squad('1', '').name == 'generated' and squads[1].alias == '')
assert(module.rename_squad('4', 'Axes').why and squads[4].alias == '')
assert(not pcall(module.rename_squad, '1', 'two\nlines') and squads[1].alias == '')
"""
    result = subprocess.run([lua, "-", str(source)], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


NOBLE_FIXTURE = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
package.preload.json = function()
    return {encode=function(value) return value end}
end
-- 0-based vectors, like DF's, with ipairs walking them the same way.
local function vector(items)
    local v = {n=#items}
    for i, x in ipairs(items) do v[i - 1] = x end
    function v:insert(_, x)
        if x.new then x.kind, x.new = x.new, nil end
        v[v.n] = x
        v.n = v.n + 1
    end
    function v:erase(i)
        for j = i, v.n - 2 do v[j] = v[j + 1] end
        v[v.n - 1] = nil
        v.n = v.n - 1
    end
    return setmetatable(v, {__len=function(t) return t.n end})
end
local plain_ipairs = ipairs
function ipairs(t)
    if type(t) == 'table' and t.n then
        local i = -1
        return function() i = i + 1; if i < t.n then return i, t[i] end end
    end
    return plain_ipairs(t)
end
local POSITION = {is_instance=function(_, x) return x.kind == 'POSITION' end}
local FORMER = 'FORMER'
local function link(id) return {kind='POSITION', entity_id=20, assignment_id=id, start_year=5,
                                delete=function(self) self.deleted = true end} end
hfs = {[112]={id=112, unit_id=1, entity_links=vector{link(16)}},
       [200]={id=200, unit_id=2, entity_links=vector{}},
       [300]={id=300, unit_id=3, entity_links=vector{}}}          -- holds a post with no link
local function position(id, extra)
    local p = {id=id, code='P' .. id, name={[0]='post' .. id}, precedence=id, squad_size=0, flags={ACTIVE=true},
               appointed_by=vector{5}, allowed_creature=vector{}, allowed_class=vector{},
               rejected_creature=vector{}, rejected_class=vector{}, responsibilities={}}
    for k, v in pairs(extra or {}) do p[k] = v end
    return p
end
local function assignment(id, pos, holder, extra)
    local a = {id=id, position_id=pos, histfig=holder, histfig2=holder, squad_id=-1, flags={active=true}}
    for k, v in pairs(extra or {}) do a[k] = v end
    return a
end
fort = {id=20, artifact_claims=vector{}, positions={
    own=vector{position(6), position(7), position(0, {squad_size=10}), position(5, {appointed_by=vector{},
        flags={ACTIVE=true, SUCCESSION_BY_HEIR=true}}), position(8), position(9), position(10)},
    assignments=vector{assignment(16, 6, 112), assignment(15, 7, -1, {histfig2=631}), assignment(21, 0, 112, {squad_id=2}),
        assignment(17, 5, 112), assignment(18, 8, -1, {flags={}}), assignment(19, 9, 300), assignment(22, 10, -1)}}}
units = {[1]={id=1, hist_figure_id=112, flags1={}, adult=true, citizen=true},
         [2]={id=2, hist_figure_id=200, flags1={}, adult=true, citizen=true},
         [3]={id=3, hist_figure_id=-1, flags1={}, adult=true, citizen=true},
         [4]={id=4, hist_figure_id=200, flags1={}, adult=false, citizen=true},
         [5]={id=5, hist_figure_id=200, flags1={}, adult=true, citizen=false},
         [6]={id=6, hist_figure_id=200, flags1={inactive=true}, adult=true, citizen=true}}
events = vector{}
df = {historical_entity={find=function(id) return id == 20 and fort or nil end},
      historical_figure={find=function(id) return hfs[id] end},
      unit={find=function(id) return units[id] end},
      histfig_entity_link_positionst=POSITION, histfig_entity_link_former_positionst=FORMER,
      history_event_add_hf_entity_linkst='ADD', history_event_remove_hf_entity_linkst='REMOVE',
      histfig_entity_link_type={POSITION=10}, artifact_claim_type={Symbol=1},
      global={plotinfo={group_id=20}, cur_year=24, cur_year_tick=99, hist_event_next_id=1000,
              world={entities={all=vector{{id=1}, {id=20}}}, history={events=events}}}}
-- a new link made with {new=histfig_entity_link_positionst} reads back as POSITION
local insert_link = function(v) local base = v.insert; function v:insert(w, x)
    if x.new == POSITION then x.new = 'POSITION' end
    return base(self, w, x)
end end
for _, hf in pairs(hfs) do insert_link(hf.entity_links) end
dfhack = {isMapLoaded=function() return true end, df2utf=function(s) return s end,
    units={isCitizen=function(u) return u.citizen end, isAdult=function(u) return u.adult end,
           getReadableName=function(x) return 'name' .. x.id end, getCitizens=function() return vector{} end}}
assert(loadfile(arg[1]))()
"""


def run_noble_lua(body):
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    result = subprocess.run([lua, "-", str(source)], input=NOBLE_FIXTURE + body, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_replacing_a_noble_writes_what_df_writes():
    run_noble_lua(r"""
local a = fort.positions.assignments[0]
local old_link = hfs[112].entity_links[0]
local r = module.appoint_noble('16', '2')
assert(r.ok and r.name == 'name200')
assert(a.histfig == 200 and a.histfig2 == 200)
-- the old holder's link became a former-position link, keeping its start
assert(old_link.deleted and #hfs[112].entity_links == 1)
local former = hfs[112].entity_links[0]
assert(former.kind == 'FORMER')
assert(former.assignment_id == 16 and former.start_year == 5 and former.end_year == 24 and former.entity_id == 20)
-- the new holder's link carries both vector indexes
local new = hfs[200].entity_links[0]
assert(new.kind == 'POSITION' and new.entity_id == 20 and new.assignment_id == 16)
assert(new.entity_vector_idx == 1 and new.assignment_vector_idx == 0 and new.start_year == 24)
-- one history event each way, with fresh ids
assert(#events == 2 and events[0].kind == 'REMOVE' and events[1].kind == 'ADD')
assert(events[0].id == 1000 and events[1].id == 1001 and df.global.hist_event_next_id == 1002)
assert(events[1].histfig == 200 and events[1].position_id == 6 and events[1].link_type == 10 and events[1].civ == 20)
assert(events[1].year == 24 and events[1].seconds == 99)
""")


def test_vacating_keeps_the_previous_holder_like_df():
    run_noble_lua(r"""
local a = fort.positions.assignments[0]
local r = module.vacate_noble('16')
assert(r.ok and r.name == 'name112')
assert(a.histfig == -1 and a.histfig2 == 112)
assert(hfs[112].entity_links[0].kind == 'FORMER')
assert(#events == 1 and events[0].kind == 'REMOVE')
assert(module.vacate_noble('16').why:find('already empty', 1, true))
assert(#events == 1)
-- filling an empty post removes nobody
local empty = fort.positions.assignments[1]
assert(module.appoint_noble('15', '2').ok and empty.histfig == 200 and #events == 2 and events[1].kind == 'ADD')
""")


def test_noble_refusals_change_nothing():
    run_noble_lua(r"""
local function snapshot()
    local out = {#events, df.global.hist_event_next_id, #hfs[112].entity_links, #hfs[200].entity_links}
    for _, a in ipairs(fort.positions.assignments) do table.insert(out, a.histfig) end
    return table.concat(out, ',')
end
local before = snapshot()
local function refuses(needle, fn, ...)
    local r = fn(...)
    assert(r.ok == false and r.why:find(needle, 1, true), tostring(r.why))
    assert(snapshot() == before)
end
refuses('leads a squad', module.appoint_noble, '21', '2')
refuses('leads a squad', module.vacate_noble, '21')
refuses('heir', module.appoint_noble, '17', '2')
refuses('gone', module.appoint_noble, '18', '2')        -- retired post
refuses('gone', module.appoint_noble, '99', '2')
refuses('adult citizens', module.appoint_noble, '16', '3')   -- no historical figure
refuses('adult citizens', module.appoint_noble, '16', '4')   -- child
refuses('adult citizens', module.appoint_noble, '16', '5')   -- not a citizen
refuses('adult citizens', module.appoint_noble, '16', '6')   -- away
refuses('adult citizens', module.appoint_noble, '16', '99')
refuses('already hold', module.appoint_noble, '16', '1')
refuses("don't line up", module.appoint_noble, '19', '2')    -- holder without a link
refuses("don't line up", module.vacate_noble, '19')
assert(not pcall(module.appoint_noble, 'x', '2') and not pcall(module.appoint_noble, '16', 'y'))
assert(snapshot() == before)
fort.artifact_claims:insert('#', {claim_type=1, symbol_claim_id=16})
refuses('symbol', module.vacate_noble, '16')
""")
