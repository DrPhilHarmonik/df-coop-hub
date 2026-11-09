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
module.to_json = require('json').encode   -- keep results as tables to look at
assert(module.ping() == '19')
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
module.to_json = require('json').encode   -- keep results as tables to look at
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
module.to_json = require('json').encode   -- keep results as tables to look at
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
module.to_json = require('json').encode   -- keep results as tables to look at
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


PLACE_FIXTURE = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
package.preload.json = function()
    return {encode=function(value) return value end}
end
imports = {}
package.preload['plugins.stockpiles'] = function()
    -- as the real importer: 'enable' sets the category's flag, 'disable' leaves it alone
    return {import_settings=function(name, opts)
        table.insert(imports, {name=name, id=opts.id, mode=opts.mode})
        if opts.mode == 'enable' then pile.settings.flags[name:gsub('library/cat_', '')] = true end
    end}
end
local STOCK, ZONE = 'STOCK', 'ZONE'
pile = {kind=STOCK, id=300, stockpile_number=1, name='', x1=0, x2=2, y1=0, y2=3, z=5,
        settings={flags={wood=true, food=false}},
        storage={max_barrels=1, max_bins=2, max_wheelbarrows=0}}
room = {kind=ZONE, id=58, zone_num=2, name='', x1=5, x2=7, y1=5, y2=7, z=5, type=92, spec_sub_flag={active=true}}
pen = {kind=ZONE, id=59, zone_num=3, name='', x1=8, x2=9, y1=8, y2=9, z=5, type=88, spec_sub_flag={active=true}}
-- A vector as DF's: resize and insert('#', value) at the end.
local function vector(t)
    t = t or {}
    function t:resize(n) for i = #self, n + 1, -1 do self[i] = nil end end
    function t:insert(_, v) self[#self + 1] = v end
    return t
end
units = {[1]={id=1, citizen=true, adult=true}, [2]={id=2, citizen=false, adult=true},
         [3]={id=3, citizen=true, adult=false}, [4]={id=4, citizen=true, adult=true}}
local function job(id, type, extra)
    local j = {id=id, job_type=type, order_id=-1, flags={suspend=false, working=true, do_now=false, ['repeat']=false}}
    for k, v in pairs(extra or {}) do j[k] = v end
    return j
end
local function building(t)
    t.getBuildStage = function(self) return self.stage end
    t.getMaxBuildStage = function() return 3 end
    t.profile = {permitted_workers=vector(t.workers), max_general_orders=5}
    return t
end
shop = building{kind='SHOP', id=400, name='', type=3, x1=10, x2=12, y1=10, y2=12, z=5, stage=3, workers={4},
                jobs={job(11, 20, {order_id=8, worker=1}), job(12, 21), job(13, 1)}}
kiln = building{kind='FURNACE', id=401, name='Hot', type=2, x1=14, x2=16, y1=10, y2=12, z=5, stage=1, jobs={}}
removed = {}
-- Farm plots: one on the surface (in a swamp), one underground. Plant 0 grows only
-- underground all year; 1 on the surface in spring; 2 is a tree; 3 has no seeds.
field = {kind='FARM', id=500, name='', x1=20, x2=23, y1=20, y2=23, z=5, centerx=21, centery=21,
         plant_id={[0]=1, -1, -1, -1}, farm_flags={seasonal_fertilize=false}}
cave = {kind='FARM', id=501, name='Caves', x1=30, x2=31, y1=30, y2=31, z=4, centerx=30, centery=30,
        plant_id={[0]=0, 0, 0, 0}, farm_flags={seasonal_fertilize=true}}
local function plant(index, name, flags)
    return {index=index, name_plural=name, flags=flags}
end
plants = {[0]=plant(0, 'plump helmets', {SEED=true, SPRING=true, SUMMER=true, AUTUMN=true, WINTER=true,
                                          BIOME_SUBTERRANEAN_WATER=true}),
          plant(1, 'barley', {SEED=true, SPRING=true, BIOME_SWAMP_TEMPERATE_FRESHWATER=true}),
          plant(2, 'apple trees', {SEED=true, TREE=true, SPRING=true, BIOME_SWAMP_TEMPERATE_FRESHWATER=true}),
          plant(3, 'reeds', {SPRING=true, BIOME_SWAMP_TEMPERATE_FRESHWATER=true})}
local plant_list = {plants[0], plants[1], plants[2], plants[3]}
autofarm_status = 'Autofarm is not running.'   -- the real "on" line is "Autofarm is Active."
local function seeds(index, n, f)
    return {flags=f or {}, getMaterialIndex=function() return index end, getStackSize=function() return n end}
end
local places = {[300]=pile, [58]=room, [59]=pen, [7]={kind='BED', id=7, x1=0, x2=0, y1=0, y2=0, z=5},
                [400]=shop, [401]=kiln, [500]=field, [501]=cave}
owner = nil
refuse_owner = false
local function is_a(kind) return {is_instance=function(_, b) return b.kind == kind end} end
df = {building={find=function(id) return places[id] end}, unit={find=function(id) return units[id] end},
      building_stockpilest=is_a(STOCK), building_civzonest=is_a(ZONE),
      building_workshopst=is_a('SHOP'), building_furnacest=is_a('FURNACE'), building_farmplotst=is_a('FARM'),
      plant_raw={find=function(i) return plants[i] end}, biome_type={[7]='SWAMP_TEMPERATE_FRESHWATER'},
      workshop_type={[3]='Masons', Custom=-1}, furnace_type={[2]='Kiln', Custom=-1},
      job_type={[20]='ConstructBlocks', [21]='ConstructThrone', [1]='DestroyBuilding'},
      global={cur_season=0, world={manager_orders={all={{workshop_id=400}, {workshop_id=-1}}},
              raws={plants={all=plant_list}},
              items={other={SEEDS={seeds(0, 5), seeds(0, 2), seeds(1, 3), seeds(1, 9, {foreign=true})}}}}},
      civzone_type={Bedroom=92, Office=93, DiningHall=80, Tomb=97, Pen=88, [92]='Bedroom', [88]='Pen'}}
dfhack = {isMapLoaded=function() return true end, df2utf=function(s) return s end, utf2df=function(s) return s end,
    units={isCitizen=function(u) return u.citizen end, isAdult=function(u) return u.adult end,
           getReadableName=function(u) return 'unit' .. u.id end},
    job={getWorker=function(j) return j.worker and units[j.worker] end, getName=function(j) return 'job' .. j.id end,
         removeWorker=function(j) j.worker = nil end,
         removeJob=function(j)
             for i, other in ipairs(shop.jobs) do if other == j then table.remove(shop.jobs, i) end end
             table.insert(removed, j.id)
             return true
         end},
    run_command_silent=function(cmd) return autofarm_status, 0 end,
    maps={getTileFlags=function(x, y, z) return {subterranean=(z == 4)} end,
          getTileBiomeRgn=function() return 1, 1 end, getBiomeType=function() return 7 end},
    buildings={getOwner=function() return owner end, getName=function(b) return b.kind .. b.id end,
               setOwner=function(_, u) if refuse_owner then return true end; owner = u; return true end}}
assert(loadfile(arg[1]))()
module.to_json = require('json').encode   -- keep results as tables to look at
"""


def run_place_lua(body):
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    result = subprocess.run([lua, "-", str(source)], input=PLACE_FIXTURE + body, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_place_info_only_answers_for_stockpiles_and_zones_on_that_tile():
    run_place_lua(r"""
local p = module.place_info('300', '1', '1', '5')
assert(p.found and p.kind == 'stockpile' and p.tiles == 12 and p.bins == 2)
assert(#p.categories == 1 and p.categories[1] == 'wood')
assert(not module.place_info('300', '9', '1', '5').found)     -- not on this tile
assert(not module.place_info('300', '1', '1', '6').found)     -- other level
assert(not module.place_info('7', '0', '0', '5').found)       -- a bed
assert(not module.place_info('999', '0', '0', '5').found)
owner = units[1]
local z = module.place_info('58', '6', '6', '5')
assert(z.kind == 'zone' and z.zone == 'Bedroom' and z.ownable and z.owner == 'unit1' and z.active)
assert(not module.place_info('59', '8', '8', '5').ownable)
""")


def test_workshops_show_their_jobs_and_worker():
    run_place_lua(r"""
local w = module.place_info('400', '11', '11', '5')
assert(w.found and w.kind == 'workshop' and w.workshop == 'Masons' and w.built and w.name == 'SHOP400')
assert(#w.jobs == 3 and w.jobs[1].name == 'job11' and w.jobs[1].order == 8 and w.jobs[1].worker == 'unit1')
assert(not w.jobs[1].building and w.jobs[3].building)          -- taking the workshop down
assert(#w.workers == 1 and w.workers[1].id == 4 and w.general_orders == 5 and w.orders_here == 1)
local k = module.place_info('401', '15', '11', '5')
assert(k.workshop == 'Furnace:Kiln' and not k.built and k.custom == 'Hot' and #k.jobs == 0)
assert(not module.place_info('400', '15', '11', '5').found)  -- tile belongs to the kiln
""")


def test_workshop_jobs_change_as_from_its_tasks_tab():
    run_place_lua(r"""
local j = shop.jobs[1]
assert(module.workshop_job('400', '11', 'suspend').ok)
assert(j.flags.suspend and not j.flags.working and j.worker == nil)
assert(module.workshop_job('400', '11', 'resume').ok and not j.flags.suspend)
assert(module.workshop_job('400', '11', 'now').ok and j.flags.do_now)
assert(module.workshop_job('400', '11', 'notnow').ok and not j.flags.do_now)
assert(module.workshop_job('400', '11', 'repeat').ok and j.flags['repeat'])
assert(module.workshop_job('400', '11', 'once').ok and not j.flags['repeat'])
assert(module.workshop_job('400', '12', 'cancel').ok and removed[1] == 12 and #shop.jobs == 2)
assert(module.workshop_job('400', '12', 'cancel').why:find('already gone', 1, true))
assert(module.workshop_job('400', '13', 'cancel').why:find('built or taken down', 1, true))
assert(module.workshop_job('400', '13', 'suspend').why and not shop.jobs[2].flags.suspend)
assert(module.workshop_job('300', '11', 'cancel').why:find('gone', 1, true))     -- a stockpile
assert(module.workshop_job('999', '11', 'cancel').why:find('gone', 1, true))
for _, bad in ipairs{'', 'delete', 'Cancel'} do assert(not pcall(module.workshop_job, '400', '11', bad)) end
assert(not pcall(module.workshop_job, '400', 'x', 'cancel'))
assert(#removed == 1)
""")


def test_a_workshop_takes_one_adult_citizen_or_anyone():
    run_place_lua(r"""
local r = module.workshop_worker('400', '1')
assert(r.ok and r.name == 'unit1' and #shop.profile.permitted_workers == 1 and shop.profile.permitted_workers[1] == 1)
assert(module.workshop_worker('400', '2').why:find('adult citizens', 1, true))    -- not a citizen
assert(module.workshop_worker('400', '3').why:find('adult citizens', 1, true))    -- a child
assert(module.workshop_worker('400', '99').why)
assert(shop.profile.permitted_workers[1] == 1)
assert(module.workshop_worker('400', '').ok and #shop.profile.permitted_workers == 0)
assert(module.workshop_worker('58', '1').why:find('gone', 1, true))
""")


def test_farm_plots_offer_what_grows_there_with_seed_counts():
    run_place_lua(r"""
local f = module.place_info('500', '21', '21', '5')
assert(f.kind == 'farm' and not f.underground and not f.fertilize and f.tiles == 16 and f.season == 0)
assert(f.crops[1].id == 1 and f.crops[1].name == 'barley' and f.crops[2].id == -1 and f.crops[2].name == '')
assert(#f.options[1] == 1 and f.options[1][1].name == 'barley' and f.options[1][1].seeds == 3)   -- not the tree, reeds
assert(#f.options[2] == 0)                                       -- nothing grows here in summer
assert(f.autofarm == false)
autofarm_status = 'Autofarm is Active.\nDefault: 50\n'
assert(module.place_info('500', '21', '21', '5').autofarm == true)
local c = module.place_info('501', '30', '30', '4')
assert(c.underground and c.fertilize and c.name == 'Caves' and #c.options[4] == 1 and c.options[4][1].seeds == 7)
""")


def test_farm_crops_must_grow_there_then():
    run_place_lua(r"""
assert(module.farm_crop('500', '0', '-1').ok and field.plant_id[0] == -1)
assert(module.farm_crop('500', '0', '1').ok and field.plant_id[0] == 1)
assert(module.farm_crop('500', '1', '1').why:find("doesn't grow", 1, true))     -- barley in summer
assert(module.farm_crop('500', '0', '0').why:find("doesn't grow", 1, true))     -- a cave crop outside
assert(module.farm_crop('500', '0', '2').why and module.farm_crop('500', '0', '3').why)   -- tree, no seeds
assert(module.farm_crop('500', '0', '99').why)
assert(module.farm_crop('501', '3', '0').ok and cave.plant_id[3] == 0)
assert(module.farm_crop('300', '0', '1').why:find('gone', 1, true))             -- a stockpile
for _, bad in ipairs{{'4', '1'}, {'-1', '1'}, {'0', 'x'}} do
    assert(not pcall(module.farm_crop, '500', bad[1], bad[2]))
end
assert(field.plant_id[0] == 1 and field.plant_id[1] == -1)
assert(module.farm_fertilize('500', '1').ok and field.farm_flags.seasonal_fertilize)
assert(module.farm_fertilize('500', '0').ok and not field.farm_flags.seasonal_fertilize)
assert(module.farm_fertilize('58', '1').why:find('gone', 1, true))
""")


def test_stockpile_categories_only_use_listed_presets():
    run_place_lua(r"""
assert(module.stockpile_category('300', 'food', '1').ok)
assert(imports[1].name == 'library/cat_food' and imports[1].id == 300 and imports[1].mode == 'enable')
assert(pile.settings.flags.food == true)
assert(module.stockpile_category('300', 'food', '0').ok and pile.settings.flags.food == false)
table.remove(imports, 2)
assert(module.stockpile_category('300', 'sheet', '0').ok)
assert(imports[2].name == 'library/cat_sheets' and imports[2].mode == 'disable')
assert(module.stockpile_category('999', 'food', '1').why:find('gone', 1, true))
assert(module.stockpile_category('58', 'food', '1').why:find('gone', 1, true))     -- a zone
for _, bad in ipairs{'../../secrets', 'cat_food', '', 'all'} do
    assert(not pcall(module.stockpile_category, '300', bad, '1'), bad)
end
assert(#imports == 2)
""")


def test_stockpile_limits_are_kept_to_its_size():
    run_place_lua(r"""
assert(module.stockpile_containers('300', '3', '12', '0').ok)
assert(pile.storage.max_barrels == 3 and pile.storage.max_bins == 12 and pile.storage.max_wheelbarrows == 0)
for _, bad in ipairs{{'13', '0', '0'}, {'0', '13', '0'}, {'0', '0', '13'}, {'-1', '0', '0'}} do
    assert(module.stockpile_containers('300', bad[1], bad[2], bad[3]).why:find('fits 0 to 12', 1, true))
end
assert(pile.storage.max_barrels == 3 and pile.storage.max_bins == 12)
assert(not pcall(module.stockpile_containers, '300', 'x', '0', '0'))
assert(module.stockpile_containers('58', '0', '0', '0').why:find('gone', 1, true))
""")


def test_room_owners_and_zone_state():
    run_place_lua(r"""
assert(module.zone_owner('58', '1').ok and owner == units[1])
assert(module.zone_owner('58', '').ok and owner == nil)
assert(module.zone_owner('58', '2').why:find('citizens', 1, true) and owner == nil)
assert(module.zone_owner('58', '99').why:find('citizens', 1, true))
assert(module.zone_owner('59', '1').why:find('own this kind', 1, true) and owner == nil)
assert(module.zone_owner('300', '1').why:find('gone', 1, true))
refuse_owner = true
assert(module.zone_owner('58', '1').why == 'DF refused that')
assert(module.zone_active('58', '0').ok and room.spec_sub_flag.active == false)
assert(module.zone_active('58', '1').ok and room.spec_sub_flag.active == true)
assert(module.zone_active('300', '0').why:find('gone', 1, true))
assert(module.rename_place('58', 'Vault').ok and room.name == 'Vault')
assert(module.rename_place('300', 'Logs').ok and pile.name == 'Logs')
assert(module.rename_place('7', 'Bed').why:find('gone', 1, true))
assert(not pcall(module.rename_place, '58', 'two\nlines') and room.name == 'Vault')
""")


ORDER_FIXTURE = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
package.preload.json = function()
    return {encode=function(value) return value end}
end
-- 0-based vectors, like DF's
local function vector(items)
    local v = {n=#items}
    for i, x in ipairs(items) do v[i - 1] = x end
    function v:insert(_, x) v[v.n] = x; v.n = v.n + 1 end
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
deleted = 0
local function class(kind, make)
    return {kind=kind, is_instance=function(_, o) return o.kind == kind end,
            new=function()
                local o = make()
                o.kind, o.year, o.year_tick = kind, -1, -1
                function o:delete() deleted = deleted + 1; self.dead = true end
                return o
            end}
end
local movest = class('MOVE', function() return {pos={x=-1, y=-1, z=-1}, point_id=0} end)
local killst = class('KILL', function() return {units=vector{}, histfigs=vector{}, title=''} end)
local burrows = {kind='BURROW', delete=function() error('a burrow order must be left alone') end}
local function positions(list)
    local p = setmetatable({}, {__len=function() return #list end})
    for i, hf in plain_ipairs(list) do p[i - 1] = {occupant=hf} end
    return p
end
squads = {[2]={id=2, entity_id=20, orders=vector{burrows}, positions=positions{100}},
          [3]={id=3, entity_id=20, orders=vector{}, positions=positions{-1}},
          [4]={id=4, entity_id=99, orders=vector{}, positions=positions{100}}}
local function unit(id, extra)
    local u = {id=id, enemy=true, active=true, dead=false, unit_hidden=false, x=5, y=5, z=5, flags1={}}
    for k, v in pairs(extra or {}) do u[k] = v end
    return u
end
units = {[1]=unit(1), [2]=unit(2), [3]=unit(3, {enemy=false}), [4]=unit(4, {dead=true}),
         [5]=unit(5, {active=false}), [6]=unit(6, {unit_hidden=true}), [7]=unit(7, {x=0, y=0}),
         [8]=unit(8, {flags1={caged=true}}), [9]=unit(9, {flags1={chained=true}})}
local FLOOR, WALL, NOSHAPE = 1, 2, 3
df = {squad={find=function(id) return squads[id] end}, unit={find=function(id) return units[id] end},
      squad_order_movest=movest, squad_order_kill_listst=killst,
      tiletype={attrs={[FLOOR]={shape=10}, [WALL]={shape=20}}},
      tiletype_shape={attrs={[10]={walkable=true}, [20]={walkable=false}}},
      global={plotinfo={group_id=20}, cur_year=24, cur_year_tick=5000,
              world={units={active=vector{units[1], units[3], units[4], units[5], units[6], units[7], units[8], units[9], units[2]}}}}}
dfhack = {isMapLoaded=function() return true end, df2utf=function(s) return s end,
    maps={isValidTilePos=function(x, y, z) return x >= 0 and y >= 0 and x < 50 and y < 50 and z == 5 end,
          getTileFlags=function(x, y) return {hidden=(x == 0 and y == 0)} end,
          getTileType=function(x) return x == 9 and WALL or FLOOR end},
    units={isActive=function(u) return u.active end, isDead=function(u) return u.dead end,
           isDanger=function(u) return u.enemy end, isHidden=function(u) return u.unit_hidden end,
           getPosition=function(u) return u.x, u.y, u.z end,
           getReadableName=function(u) return 'unit' .. u.id end}}
assert(loadfile(arg[1]))()
module.to_json = require('json').encode   -- keep results as tables to look at
local function immediate(squad)
    local out = {}
    for _, o in ipairs(squad.orders) do if o.kind ~= 'BURROW' then table.insert(out, o) end end
    return out
end
"""


def run_order_lua(body):
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    result = subprocess.run([lua, "-", str(source)], input=ORDER_FIXTURE + body, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_a_station_order_is_what_df_writes_and_replaces_the_last():
    run_order_lua(r"""
assert(module.squad_station('2', '7', '8', '5').ok)
local first = immediate(squads[2])[1]
assert(#immediate(squads[2]) == 1 and first.kind == 'MOVE')
assert(first.pos.x == 7 and first.pos.y == 8 and first.pos.z == 5 and first.point_id == -1)
assert(first.year == 24 and first.year_tick == 5000)
assert(module.squad_station('2', '1', '1', '5').ok)
assert(#immediate(squads[2]) == 1 and first.dead and deleted == 1)
assert(immediate(squads[2])[1].pos.x == 1)
assert(#squads[2].orders == 2)                 -- the burrow order was left alone
assert(module.squad_clear('2').ok and #immediate(squads[2]) == 0 and #squads[2].orders == 1)
""")


def test_bad_stations_change_nothing():
    run_order_lua(r"""
assert(module.squad_station('2', '7', '8', '5').ok)
local before = immediate(squads[2])[1]
local function refused(needle, ...)
    local r = module.squad_station(...)
    assert(r.ok == false and r.why:find(needle, 1, true), tostring(r.why))
    assert(immediate(squads[2])[1] == before and not before.dead)
end
refused('gone', '9', '1', '1', '5')
refused('gone', '4', '1', '1', '5')            -- another entity's squad
refused('nobody', '3', '1', '1', '5')          -- empty squad
refused('outside', '2', '99', '1', '5')
refused('outside', '2', '1', '1', '6')
refused("can't station", '2', '0', '0', '5')   -- hidden
refused("can't stand", '2', '9', '1', '5')     -- a wall
assert(not pcall(module.squad_station, '2', 'x', '1', '5'))
assert(immediate(squads[2])[1] == before)
""")


def test_kill_orders_can_only_name_enemies_in_sight():
    run_order_lua(r"""
local t = module.squad_targets()
assert(#t == 2 and t[1].id == 1 and t[2].id == 2)   -- friend, dead, gone, unseen, hidden-tile and caged left out
assert(module.squad_kill('2', '1,2').ok)
local o = immediate(squads[2])[1]
assert(o.kind == 'KILL' and #o.units == 2 and o.units[0] == 1 and o.units[1] == 2 and o.title == '2 enemies')
assert(o.year == 24 and o.year_tick == 5000)
assert(module.squad_kill('2', '2').ok and o.dead and immediate(squads[2])[1].title == 'unit2')
-- a station replaces a kill order too
assert(module.squad_station('2', '7', '8', '5').ok and immediate(squads[2])[1].kind == 'MOVE')
local keep = immediate(squads[2])[1]
-- one bad name refuses the whole order and leaves the current one
for _, bad in plain_ipairs({'3', '4', '5', '6', '7', '8', '9', '99', '1,3'}) do
    assert(module.squad_kill('2', bad).why:find('not an enemy', 1, true), bad)
    assert(immediate(squads[2])[1] == keep and not keep.dead)
end
assert(module.squad_kill('4', '1').why:find('gone', 1, true))
assert(module.squad_kill('3', '1').why:find('nobody', 1, true))
for _, bad in plain_ipairs({'', 'x', '1;2', ',1'}) do assert(not pcall(module.squad_kill, '2', bad), bad) end
""")


CONDITION_FIXTURE = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
package.preload.json = function() return {encode=function(value) return value end} end
-- A 0-based vector, like DF's, walked from 0 as DFHack's ipairs does.
local function vector(items)
    items = items or {}
    return setmetatable({}, {
        __index=function(_, k)
            if k == 'erase' then return function(_, i) table.remove(items, i + 1) end end
            if k == 'insert' then
                return function(_, i, v)
                    if i == '#' then table.insert(items, v) else table.insert(items, i + 1, v) end
                end
            end
            return items[k + 1]
        end,
        __len=function() return #items end,
        is_vector=true,
    })
end
local plain_ipairs = ipairs
function ipairs(t)
    local meta = getmetatable(t)
    if not (meta and meta.is_vector) then return plain_ipairs(t) end
    return function(_, i) i = i + 1; if t[i] ~= nil then return i, t[i] end end, t, -1
end
deleted = 0
local function item_cond(fields)
    local c = {compare_type=-1, compare_val=0, item_type=-1, item_subtype=-1, mat_type=-1, mat_index=-1,
               flags1={whole=0}, flags2={whole=0}, flags3={whole=0}, reaction_id=-1,
               has_material_reaction_product='', reaction_class='', delete=function() deleted = deleted + 1 end}
    for k, v in pairs(fields or {}) do c[k] = v end
    return c
end
local function order_cond(after, state)
    return {order_id=after, condition=state, delete=function() deleted = deleted + 1 end}
end
local function make(id, items, orders)
    return {id=id, amount_left=2, amount_total=5, frequency=0, max_workshops=0, delete=function() end,
            item_conditions=vector(items), order_conditions=vector(orders)}
end
beds = item_cond{compare_type=3, compare_val=10, item_type=20}
o10 = make(10, {beds, item_cond{compare_type=0, compare_val=2, item_type=21}})
o11 = make(11, {}, {order_cond(12, 1)})
o12 = make(12)
all = vector({o10, o11, o12})
df = {workquota_frequency_type={NONE=-1, OneTime=0},
      workquota_order_condition_type={NONE=-1, Activated=0, Completed=1},
      logic_condition_type={NONE=-1, AtLeast=0, AtMost=1, GreaterThan=2, LessThan=3},
      item_type={NONE=-1, BED=20, DOOR=21},
      manager_order_condition_item={new=function() return item_cond() end},
      manager_order_condition_order={new=function() return order_cond(-1, 1) end},
      global={world={manager_orders={all=all}, jobs={list={}}}}}
assert(loadfile(arg[1]))()
module.to_json = require('json').encode   -- keep results as tables to look at
function key_of(c)
    return table.concat({'i', c.compare_type, c.compare_val, c.item_type, c.item_subtype, c.mat_type, c.mat_index,
                         c.flags1.whole, c.flags2.whole, c.flags3.whole, c.reaction_id,
                         c.has_material_reaction_product, c.reaction_class}, ':')
end
M = module
"""


def run_condition_lua(body):
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    result = subprocess.run([lua, "-", str(source)], input=CONDITION_FIXTURE + body, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_editing_a_condition_needs_its_current_key():
    run_condition_lua(r"""
local key = key_of(beds)
assert(M.edit_condition('10', key, 'AtMost', '7').ok)
assert(beds.compare_type == 1 and beds.compare_val == 7)
assert(M.edit_condition('10', key, 'AtLeast', '1').stale)       -- someone already changed it
assert(beds.compare_type == 1 and beds.compare_val == 7)
assert(M.edit_condition('99', key, 'AtMost', '7').missing)
for _, bad in ipairs{{'10', key_of(beds), 'NONE', '1'}, {'10', key_of(beds), 'Sideways', '1'},
                     {'10', key_of(beds), 'AtLeast', '-1'}, {'10', key_of(beds), 'AtLeast', '1000000'},
                     {'10', key_of(beds), 'AtLeast', '1.5'}, {'x', key_of(beds), 'AtLeast', '1'}} do
    assert(not pcall(M.edit_condition, bad[1], bad[2], bad[3], bad[4]), bad[3])
end
assert(beds.compare_type == 1 and beds.compare_val == 7)
""")


def test_removing_a_condition_deletes_only_that_one():
    run_condition_lua(r"""
local other = o10.item_conditions[1]
assert(M.remove_condition('10', 'i:gone').stale and #o10.item_conditions == 2)
assert(M.remove_condition('10', key_of(beds)).ok)
assert(#o10.item_conditions == 1 and o10.item_conditions[0] == other and deleted == 1)
assert(M.remove_condition('11', 'o:12:1').ok and #o11.order_conditions == 0 and deleted == 2)
assert(M.remove_condition('99', 'o:12:1').missing)
""")


def test_a_new_item_condition_is_as_plain_as_dfs_own():
    run_condition_lua(r"""
assert(M.add_item_condition('12', 'BED', 'LessThan', '5').ok)
local c = o12.item_conditions[0]
assert(c.item_type == 20 and c.compare_type == 3 and c.compare_val == 5)
assert(c.item_subtype == -1 and c.mat_type == -1 and c.reaction_id == -1 and c.flags1.whole == 0)
assert(M.add_item_condition('12', 'BED', 'LessThan', '5').duplicate and #o12.item_conditions == 1 and deleted == 1)
assert(M.add_item_condition('12', 'BED', 'LessThan', '6').ok and #o12.item_conditions == 2)
assert(M.add_item_condition('99', 'BED', 'LessThan', '5').missing)
for _, item in ipairs{'NONE', 'SPOON', 'bed', 'BED"', ''} do
    assert(not pcall(M.add_item_condition, '12', item, 'LessThan', '5'), item)
end
assert(#o12.item_conditions == 2)
""")


def test_orders_cant_wait_for_each_other():
    run_condition_lua(r"""
assert(M.add_order_condition('10', '11', 'Completed').ok)       -- 10 waits 11, which waits 12
local c = o10.order_conditions[0]
assert(c.order_id == 11 and c.condition == 1)
assert(M.add_order_condition('10', '11', 'Completed').duplicate)
assert(M.add_order_condition('10', '11', 'Activated').ok and #o10.order_conditions == 2)
assert(M.add_order_condition('12', '10', 'Completed').loop)     -- 12 -> 10 -> 11 -> 12
assert(M.add_order_condition('11', '10', 'Completed').loop)
assert(M.add_order_condition('10', '10', 'Completed').loop)
assert(M.add_order_condition('10', '99', 'Completed').missing)
assert(M.add_order_condition('99', '10', 'Completed').missing)
assert(#o12.order_conditions == 0)
assert(not pcall(M.add_order_condition, '10', '12', 'NONE'))
assert(not pcall(M.add_order_condition, '10', '12', 'Soon'))
""")


def test_cancelling_an_order_frees_those_waiting_for_it():
    run_condition_lua(r"""
assert(M.cancel_order('12').ok)
assert(#all == 2 and #o11.order_conditions == 0 and deleted == 1)
""")


def test_workshop_limit_is_optional_and_checked():
    run_condition_lua(r"""
assert(M.set_order('10', '', '', '2').ok and o10.max_workshops == 2 and o10.amount_total == 5)
assert(M.set_order('10', '7', '').ok and o10.max_workshops == 2 and o10.amount_total == 7)
assert(M.set_order('10', '', '', '0').ok and o10.max_workshops == 0)
for _, bad in ipairs{'-1', '100', '1.5', 'x'} do
    assert(not pcall(M.set_order, '10', '', '', bad), bad)
end
assert(not pcall(M.set_order, '10', '', '', ''))
assert(o10.max_workshops == 0)
""")


def test_citizens_only_say_where_they_are_on_seen_tiles():
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    script = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
package.preload.json = function() return {encode=function(value) return value end} end
local function make(id, name, x, soul)
    return {id=id, name=name, x=x, military={squad_id=-1}, job={}, status={current_soul=soul}}
end
local people = {make(2, 'Zan', 5, {}), make(1, 'Asa', 6, {}), make(3, 'Baby', 7, nil), make(4, 'Lost', 8, {}), make(5, 'Ghost', nil, {})}
people[2].job.current_job = {name='Mining'}
dfhack = {df2utf=function(s) return s end, isMapLoaded=function() return true end,
          units={getCitizens=function() return people end,
                 getReadableName=function(u) return u.name end,
                 getProfessionName=function() return 'Miner' end,
                 getStressCategory=function() return 2 end,
                 getPosition=function(u) if u.x then return u.x, 1, 1 end end,
                 isHidden=function(u) return u.x == 7 end},
          job={getName=function(j) return j.name end},
          maps={getTileFlags=function(x) return {hidden=x == 8} end}}
df = {}
assert(loadfile(arg[1]))()
module.to_json = require('json').encode   -- keep results as tables to look at
local list = module.citizens()
assert(#list == 5)
assert(list[1].name == 'Asa' and list[1].job == 'Mining' and list[1].x == 6 and list[1].stress == 2)
assert(list[2].name == 'Baby' and list[2].stress == -1 and list[2].x == nil)   -- no soul; a hidden unit
assert(list[3].name == 'Ghost' and list[3].x == nil and list[3].z == nil)      -- no position
assert(list[4].name == 'Lost' and list[4].x == nil and list[4].z == nil)       -- on an unseen tile
assert(list[5].name == 'Zan' and list[5].job == '' and list[5].x == 5)
"""
    result = subprocess.run([lua, "-", str(source)], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


DETAIL_FIXTURE = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
package.preload.json = function() return {encode=function(value) return value end} end
local function vector(items)
    items = items or {}
    return setmetatable({}, {
        __index=function(_, k)
            if k == 'erase' then return function(_, i) table.remove(items, i + 1) end end
            if k == 'insert' then
                return function(_, i, v)
                    if i == '#' then table.insert(items, v) else table.insert(items, i + 1, v) end
                end
            end
            return items[k + 1]
        end,
        __len=function() return #items end,
        is_vector=true,
    })
end
local plain_ipairs = ipairs
function ipairs(t)
    local meta = getmetatable(t)
    if not (meta and meta.is_vector) then return plain_ipairs(t) end
    return function(_, i) i = i + 1; if t[i] ~= nil then return i, t[i] end end, t, -1
end
MODES = {Default=0, EverybodyDoesThis=1, NobodyDoesThis=2, OnlySelectedDoesThis=3}
function labor_set(on)
    local out = setmetatable({}, {__index=function(t, k) return false end})
    for _, name in plain_ipairs(on) do out[name] = true end
    return out
end
-- allowed_labors walks by name here; DF's walks by number. Both index the same way.
local function allowed(names)
    local list = {}
    for _, n in plain_ipairs(names) do list[n] = true end
    return setmetatable(list, {__index=function() return false end, is_vector=true,
        __len=function() return 0 end})
end
function detail(name, mode, labors, members, no_everybody)
    return {name=name, flags={mode=MODES[mode], cannot_be_everybody=no_everybody or false},
            allowed_labors=allowed(labors), assigned_units=vector(members)}
end
function ipairs_allowed(t) return pairs(t) end
function person(id, opts)
    opts = opts or {}
    return {id=id, status={labors=labor_set(opts.labors or {})}, flags1={inactive=opts.inactive or false},
            adult=opts.adult ~= false, citizen=opts.citizen ~= false}
end
"""


def run_detail_lua(body):
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    setup = r"""
ann, bob, kid, away = person(5, {labors={'MINE'}}), person(9), person(3, {adult=false}), person(7, {inactive=true})
miners = detail('Miners', 'OnlySelectedDoesThis', {'MINE'}, {5}, true)
woods = detail('Woodcutters', 'OnlySelectedDoesThis', {'CUTWOOD'}, {}, true)
haulers = detail('Haulers', 'EverybodyDoesThis', {'HAUL_STONE'}, {})
stones = detail('Stonecutters', 'OnlySelectedDoesThis', {'STONECUTTER'}, {2, 4, 9, 12})
extra = detail('Quarry', 'OnlySelectedDoesThis', {'STONECUTTER'}, {})
details = setmetatable({}, {__index=function(_, k) return ({[0]=miners, woods, haulers, stones, extra})[k] end,
                            __len=function() return 5 end, is_vector=true})
units = {[5]=ann, [9]=bob, [3]=kid, [7]=away}
dfhack = {df2utf=function(s) return s end, isMapLoaded=function() return true end,
          units={getCitizens=function() return {ann, bob, kid, away} end,
                 getReadableName=function(u) return 'Dwarf ' .. u.id end,
                 isCitizen=function(u) return u.citizen end, isAdult=function(u) return u.adult end,
                 setAutomaticProfessions=function() end}}
df = {work_detail_mode=setmetatable({[0]='Default', 'EverybodyDoesThis', 'NobodyDoesThis', 'OnlySelectedDoesThis'},
                                    {__index=MODES}),
      unit_labor={MINE='MINE'}, unit={find=function(id) return units[id] end},
      global={plotinfo={labor_info={work_details=details}}}}
assert(loadfile(arg[1]))()
module.to_json = require('json').encode   -- keep results as tables to look at
M = module
-- The module walks allowed_labors with ipairs; give it name keys in this fixture.
local vector_ipairs = ipairs
function ipairs(t)
    local meta = getmetatable(t)
    if meta and meta.is_vector and meta.__len and #t == 0 and rawlen(t) == 0 and next(t) ~= nil then
        return pairs(t)
    end
    return vector_ipairs(t)
end
function ids(d)
    local out = {}
    for i = 0, #d.assigned_units - 1 do out[#out + 1] = d.assigned_units[i] end
    return table.concat(out, ',')
end
"""
    result = subprocess.run([lua, "-", str(source)], input=DETAIL_FIXTURE + setup + body, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_picking_someone_keeps_the_list_sorted_and_sets_their_labor():
    run_detail_lua(r"""
assert(M.work_detail_member('3', 'Stonecutters', '5', '1').ok)
assert(ids(stones) == '2,4,5,9,12' and ann.status.labors.STONECUTTER == true)
assert(M.work_detail_member('3', 'Stonecutters', '5', '1').ok and ids(stones) == '2,4,5,9,12')  -- already in
assert(M.work_detail_member('3', 'Stonecutters', '5', '0').ok)
assert(ids(stones) == '2,4,9,12' and ann.status.labors.STONECUTTER == false)
assert(M.work_detail_member('4', 'Quarry', '9', '1').ok and ids(extra) == '9')
assert(M.work_detail_member('0', 'Miners', '5', '0').ok and ids(miners) == '' and ann.status.labors.MINE == false)
""")


def test_a_labor_stays_on_while_another_detail_still_gives_it():
    run_detail_lua(r"""
assert(M.work_detail_member('3', 'Stonecutters', '9', '1').ok)
assert(M.work_detail_member('4', 'Quarry', '9', '1').ok)
assert(M.work_detail_member('4', 'Quarry', '9', '0').ok)
assert(bob.status.labors.STONECUTTER == true)                   -- still a Stonecutter
assert(M.work_detail_member('3', 'Stonecutters', '9', '0').ok)
assert(bob.status.labors.STONECUTTER == false)
""")


def test_tool_labors_dont_mix_and_the_wrong_people_cant_be_picked():
    run_detail_lua(r"""
assert(M.work_detail_member('1', 'Woodcutters', '5', '1').why:find('tool'))    -- a miner
assert(ids(woods) == '' and ann.status.labors.CUTWOOD == false)
assert(M.work_detail_member('1', 'Woodcutters', '9', '1').ok and bob.status.labors.CUTWOOD)
assert(M.work_detail_member('0', 'Miners', '3', '1').why:find('adult'))        -- a child
assert(M.work_detail_member('0', 'Miners', '7', '1').why:find('adult'))        -- away
assert(M.work_detail_member('0', 'Miners', '99', '1').why:find('adult'))
assert(M.work_detail_member('2', 'Haulers', '9', '1').why:find('setting'))     -- everybody does it
assert(M.work_detail_member('0', 'Woodcutters', '9', '1').why:find('changed')) -- stale list
assert(not pcall(M.work_detail_member, '0', 'Miners', '9', 'yes'))
""")


def test_changing_who_does_it_reaches_every_adult_but_no_child():
    run_detail_lua(r"""
assert(M.work_detail_mode('3', 'Stonecutters', 'EverybodyDoesThis').ok)
assert(ann.status.labors.STONECUTTER and bob.status.labors.STONECUTTER and not kid.status.labors.STONECUTTER)
assert(M.work_detail_mode('3', 'Stonecutters', 'NobodyDoesThis').ok)
assert(not ann.status.labors.STONECUTTER and not bob.status.labors.STONECUTTER)
assert(M.work_detail_mode('3', 'Stonecutters', 'OnlySelectedDoesThis').ok)
assert(bob.status.labors.STONECUTTER and not ann.status.labors.STONECUTTER)      -- 9 is on its list
assert(M.work_detail_mode('0', 'Miners', 'EverybodyDoesThis').why:find('tool'))
assert(miners.flags.mode == MODES.OnlySelectedDoesThis)
assert(not pcall(M.work_detail_mode, '3', 'Stonecutters', 'Default'))
assert(not pcall(M.work_detail_mode, '3', 'Stonecutters', 'Sometimes'))
assert(M.work_detail_mode('3', 'Miners', 'NobodyDoesThis').why:find('changed'))
""")


STOCK_FIXTURE = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
package.preload.json = function() return {encode=function(value) return value end} end
-- Flags as DF's: named bits, and `whole`, the number they make together.
local BITS = {foreign=1, trader=2, spider_web=4, garbage_collect=8, removed=16, construction=32, in_building=64,
              in_inventory=128}
local function whole_of(t)
    local w = 0
    for name, bit in pairs(BITS) do if rawget(t, name) then w = w | bit end end
    return w
end
local function flags(t)
    local f = {}
    for k, v in pairs(t or {}) do f[k] = v end
    f.whole = whole_of(f)
    return f
end
local next_id = 0
local function item(t, sub, mt, mi, n, where, f, holder)
    next_id = next_id + 1
    return {id=next_id, flags=flags(f), where=where or {1, 1, 5}, holder=holder, contents={},
            getType=function() return t end, getSubtype=function() return sub end,
            getMaterial=function() return mt end, getMaterialIndex=function() return mi end,
            getStackSize=function() return n end}
end
local citizen, goblin = {citizen=true, inventory={}}, {citizen=false, inventory={}}
local items = {
    item(0, -1, 0, 7, 4),                       -- 4 granite bars
    item(0, -1, 0, 7, 3),                       -- 3 more, same kind
    item(0, -1, 0, 8, 2),                       -- 2 silver bars
    item(1, 2, 0, 8, 1),                        -- a silver short sword
    item(1, 2, 0, 8, 1, nil, {in_inventory=true}, citizen),   -- carried by a citizen: counts
    item(1, 2, 0, 8, 1, nil, {in_inventory=true}, goblin),    -- a goblin's: doesn't
    item(0, -1, 0, 7, 9, nil, {foreign=true}),
    item(0, -1, 0, 7, 9, nil, {trader=true}),
    item(0, -1, 0, 7, 9, nil, {spider_web=true}),
    item(0, -1, 0, 7, 9, nil, {construction=true}),
    item(0, -1, 0, 7, 9, nil, {in_building=true}),
    item(0, -1, 0, 7, 9, nil, {removed=true}),
    item(0, -1, 0, 7, 9, {2, 2, 5}),            -- on a hidden tile
    item(0, -1, 0, 7, 9, {-30000, -30000, -30000}),   -- nowhere
    item(2, 0, 0, 7, 1),                        -- a subtype whose def has no plural
    item(3, -1, -1, -1, 2), item(3, -1, -1, -1, 1), item(3, -1, -1, -1, 5),   -- eggs: hen, hen, duck
    item(1, 2, 0, 8, 1, nil, {in_inventory=true}),            -- in the goblin's pack: doesn't count
}
items[16].race, items[17].race, items[18].race = 10, 10, 11
citizen.inventory = {{item=items[5]}}
goblin.inventory = {{item=items[6]}}
items[6].contents = {items[19]}
df = {item_type={[0]='BAR', [1]='WEAPON', [2]='FOOD', [3]='EGG', EGG=3},
      global={world={items={other={IN_PLAY=items}}, units={all={citizen, goblin}}}},
      item_flags={new=function()
          return setmetatable({}, {__index=function(t, k)
              if k == 'whole' then return whole_of(t) end
              if k == 'delete' then return function() end end
          end})
      end},
      creature_raw={find=function(r) return ({[10]={name={[0]='hen'}}, [11]={name={[0]='duck'}}})[r] end}}
local mats = {[7]='granite', [8]='silver'}
dfhack = {isMapLoaded=function() return true end, df2utf=function(s) return s end,
    units={isCitizen=function(u) return u.citizen end},
    items={getHolderUnit=function(i) return i.holder end, getContainedItems=function(i) return i.contents end,
           getPosition=function(i) return i.where[1], i.where[2], i.where[3] end,
           getSubtypeDef=function(t, sub)
               if t == 1 then return {name='short sword', name_plural='short swords'} end
               if t == 2 then return setmetatable({}, {__index=function(_, k)
                   if k == 'name' then return 'roast' end
                   error('Cannot read field ' .. k) end}) end
           end},
    maps={getTileFlags=function(x, y, z)
        if x < 0 then return nil end
        return {hidden=(x == 2)}
    end},
    matinfo={decode=function(mt, mi) return mats[mi] and {toString=function() return mats[mi] end} end}}
assert(loadfile(arg[1]))()
module.to_json = require('json').encode   -- keep results as tables to look at
"""


def test_stocks_count_by_kind_and_leave_out_what_isnt_the_forts():
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    body = r"""
local out = module.stocks().items
local got = {}
for _, r in ipairs(out) do got[#got + 1] = r.type .. '|' .. r.what .. '|' .. r.material .. '|' .. r.count end
assert(#out == 6, table.concat(got, ' / '))
assert(got[5] == 'EGG||hen|3' and got[6] == 'EGG||duck|5', table.concat(got, ' / '))
assert(got[1] == 'BAR||granite|7' and got[2] == 'BAR||silver|2', table.concat(got, ' / '))
assert(got[3] == 'WEAPON|short swords|silver|2', got[3])
assert(got[4] == 'FOOD|roast|granite|1', got[4])
"""
    result = subprocess.run([lua, "-", str(source)], input=STOCK_FIXTURE + body, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_to_json_writes_what_python_reads_back():
    import json
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("Lua interpreter is not installed")
    source = Path(__file__).parent / "lua/rpc/coop.lua"
    script = r"""
local module = setmetatable({}, {__index=_G})
function mkmodule() return module end
package.preload.json = function() return {} end
assert(loadfile(arg[1]))()
local J = module.to_json
print(J({}))
print(J({1, 2.5, -3, 0}))
print(J({ok=true, why='quote " slash \\ new\nline tab\t bell\a', n=-7, f=false}))
print(J({nested={{a=1}, {}}, ['spaced key']='x'}))
print(J('Ürist ☼ mead'))
print(J({1/0, -1/0, 0/0, 1e20, 123456789012}))
print(pcall(J, {print}))
"""
    result = subprocess.run([lua, "-", str(source)], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert json.loads(lines[0]) == []
    assert json.loads(lines[1]) == [1, 2.5, -3, 0]
    assert json.loads(lines[2]) == {"ok": True, "why": 'quote " slash \\ new\nline tab\t bell\a', "n": -7, "f": False}
    assert json.loads(lines[3]) == {"nested": [{"a": 1}, []], "spaced key": "x"}
    assert json.loads(lines[4]) == "Ürist ☼ mead"
    assert json.loads(lines[5]) == [None, None, None, 1e20, 123456789012]
    assert lines[6].startswith("false") and "cannot send a function" in lines[6]
