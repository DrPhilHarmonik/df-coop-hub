-- df-coop's in-game half: the few things the hub asks DF to do that
-- RemoteFortressReader can't. The hub calls these over DFHack's RunLua RPC,
-- which only allows modules named rpc.*; install with `hub.py install-lua`
-- (it lands in hack/lua/rpc/coop.lua).
--
-- Every argument arrives as a string and every result leaves as a JSON
-- string. Inputs are checked again here even though the hub validates them:
-- only quickfort's build/place/zone modes, and only a key (letters, maybe a
-- leading ~ as in "~h" for a hive) with an optional (WxH) extent, so nothing
-- else in quickfort is reachable from a player.

local _ENV = mkmodule('rpc.coop')

local json = require('json')

VERSION = '10'

local MODES = {build=true, place=true, zone=true}

-- DF keeps text in CP437; JSON and the browser need UTF-8. Every string that
-- comes out of DF goes through this before it leaves the module.
local function utf(s)
    return s and dfhack.df2utf(s) or ''
end

function ping()
    return VERSION
end

local function int(s, name)
    local n = tonumber(s)
    if not n or n ~= math.floor(n) then error(('bad %s: %s'):format(name, tostring(s))) end
    return n
end

-- Place a building, stockpile or zone with quickfort. `text` is one cell of
-- a quickfort blueprint: a key such as "wc" (centred on x,y if the building
-- is bigger than one tile) or "f(5x3)" (x,y is the top-left corner).
-- Returns quickfort's stats as {id: {label, value}}.
function apply(mode, x, y, z, text, dry)
    if not MODES[mode] then error('mode not allowed: ' .. tostring(mode)) end
    local key, extent = (type(text) == 'string' and text or ''):match('^(~?%a+)(.*)$')
    if not key or #key > 10 or not (extent == '' or extent:match('^%(%d+x%d+%)$')) then
        error('bad blueprint key: ' .. tostring(text))
    end
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local quickfort = reqscript('quickfort')
    local stats = quickfort.apply_blueprint{
        mode=mode, data=text,
        pos={x=int(x, 'x'), y=int(y, 'y'), z=int(z, 'z')},
        dry_run=(dry == '1'),
    }
    local out = {}
    for id, stat in pairs(stats) do
        out[id] = {label=stat.label, value=stat.value}
    end
    return json.encode(out)
end

local function covers(bld, x, y, z)
    return bld.x1 <= x and x <= bld.x2 and bld.y1 <= y and y <= bld.y2 and bld.z == z
end

-- Remove one building, stockpile or zone. With `id`, exactly that one, and
-- only if it covers the tile, so overlapping zones can't be mixed up: the
-- player removes what they saw highlighted. Without `id`, a building or
-- stockpile on the tile, else the newest zone touching it.
-- Planned buildings, stockpiles and zones go at once; a finished building
-- gets a deconstruction job, as when a player marks it in DF.
function remove(x, y, z, id)
    x, y, z = int(x, 'x'), int(y, 'y'), int(z, 'z')
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local bld
    if id and id ~= '' then
        bld = df.building.find(int(id, 'id'))
        if bld and not covers(bld, x, y, z) then bld = nil end
    else
        bld = dfhack.buildings.findAtTile(x, y, z)
        if not bld then
            local zones = dfhack.buildings.findCivzonesAt(xyz2pos(x, y, z))
            if zones and #zones > 0 then bld = zones[#zones] end
        end
    end
    if not bld then return json.encode{found=false} end
    local out = {found=true, id=bld.id, type=df.building_type[bld:getType()]}
    if dfhack.buildings.markedForRemoval(bld) then
        out.outcome = 'already'
    elseif dfhack.buildings.deconstruct(bld) then
        out.outcome = 'removed'
    else
        out.outcome = 'queued'
    end
    return json.encode(out)
end

-- Work orders. DFHack's `workorder` JSON can carry Lua code (amount_total),
-- so the JSON is built here from checked pieces only: a df.job_type name, a
-- reaction code that exists in the raws, one of a few materials, an integer.
local ORDER_MATERIALS = {
    wood='"material_category":["wood"]', stone='"material":"INORGANIC"',
    leather='"material_category":["leather"]', plant='"material_category":["plant"]',
}

local function reaction_exists(code)
    for _, r in ipairs(df.global.world.raws.reactions.reactions) do
        if r.code == code then return true end
    end
    return false
end

local function frequency(name)
    if type(name) ~= 'string' or not name:match('^%a+$') then
        error('bad frequency: ' .. tostring(name))
    end
    local value = df.workquota_frequency_type[name]
    if type(value) ~= 'number' or value < 0 then error('bad frequency: ' .. name) end
    return value
end

local function checked_amount(amount)
    local n = int(amount, 'amount')
    if n < 1 or n > 999 then error('amount out of range') end
    return n
end

local function find_order(id)
    local all = df.global.world.manager_orders.all
    for i = 0, #all - 1 do
        if all[i].id == id then return all[i], i, all end
    end
end

function order(job, reaction, material, amount, freq)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    if type(job) ~= 'string' or not job:match('^%a+$') or df.job_type[job] == nil then
        error('unknown job: ' .. tostring(job))
    end
    local n = checked_amount(amount)
    local parts = {('"job":"%s"'):format(job), ('"amount_total":%d'):format(n)}
    if freq and freq ~= '' then
        frequency(freq)
        table.insert(parts, ('"frequency":"%s"'):format(freq))
    end
    if reaction and reaction ~= '' then
        if job ~= 'CustomReaction' or not reaction:match('^[A-Z0-9_]+$') or not reaction_exists(reaction) then
            error('unknown reaction: ' .. tostring(reaction))
        end
        table.insert(parts, ('"reaction":"%s"'):format(reaction))
    end
    if material and material ~= '' then
        local m = ORDER_MATERIALS[material]
        if not m then error('unknown material: ' .. tostring(material)) end
        table.insert(parts, m)
    end
    local payload = '{' .. table.concat(parts, ',') .. '}'
    local output, rc = dfhack.run_command_silent('workorder', payload)
    return json.encode{ok=(rc == CR_OK), output=(output or ''):sub(1, 300)}
end

-- Cancel a manager order, the way DFHack's own `orders clear` removes them,
-- but only while no dwarf is working on it: erasing an order out from under
-- a job is the one case that could leave DF holding a dangling reference.
function cancel_order(id)
    local want = int(id, 'id')
    local all = df.global.world.manager_orders.all
    for i, o in ipairs(all) do
        if o.id == want then
            local link = df.global.world.jobs.list.next
            while link do
                if link.item and link.item.order_id == want then
                    return json.encode{ok=false, busy=true}
                end
                link = link.next
            end
            all:erase(i)
            o:delete()
            return json.encode{ok=true}
        end
    end
    return json.encode{ok=false, missing=true}
end

-- Change an order's amount and/or how often it repeats. A new amount restarts
-- the count: `amount` more to make, whatever was left before.
function set_order(id, amount, freq)
    local want = int(id, 'id')
    local n = (amount and amount ~= '') and checked_amount(amount) or nil
    local f = (freq and freq ~= '') and frequency(freq) or nil
    if not n and not f then error('nothing to change') end
    local o = find_order(want)
    if not o then return json.encode{ok=false, missing=true} end
    if n then
        o.amount_left = n
        o.amount_total = n
    end
    if f then o.frequency = f end
    return json.encode{ok=true}
end

-- Move an order within the queue, which is the order the manager works through.
-- DFHack's `orders sort` rearranges the same vector; nothing refers to an order
-- by its position.
function move_order(id, to)
    local want = int(id, 'id')
    local o, i, all = find_order(want)
    if not o then return json.encode{ok=false, missing=true} end
    local target
    if to == 'up' then target = math.max(i - 1, 0)
    elseif to == 'down' then target = math.min(i + 1, #all - 1)
    elseif to == 'top' then target = 0
    elseif to == 'bottom' then target = #all - 1
    else error('bad move: ' .. tostring(to)) end
    if target ~= i then
        all:erase(i)
        all:insert(target, o)
    end
    return json.encode{ok=true}
end

-- What an order waits for, as pieces the hub turns into a sentence.
local function item_condition(c)
    local out = {cmp=df.logic_condition_type[c.compare_type] or '', val=c.compare_val,
                 product=utf(c.has_material_reaction_product), class=utf(c.reaction_class), flags={}}
    if c.item_type >= 0 then
        out.item = df.item_type[c.item_type] or ''
        local ok, def = pcall(dfhack.items.getSubtypeDef, c.item_type, c.item_subtype)
        if ok and def then out.sub = utf(def.name) end
    end
    if c.mat_type ~= -1 then
        local info = dfhack.matinfo.decode(c.mat_type, c.mat_index)
        if info then out.mat = utf(info:toString()) end
    end
    for _, field in ipairs{'flags1', 'flags2', 'flags3'} do
        for name, on in pairs(c[field]) do
            if on == true and type(name) == 'string' then table.insert(out.flags, name) end
        end
    end
    table.sort(out.flags)
    return out
end

local function order_conditions(o)
    local out = {}
    for _, c in ipairs(o.item_conditions) do table.insert(out, item_condition(c)) end
    for _, c in ipairs(o.order_conditions) do
        table.insert(out, {after=c.order_id, when=df.workquota_order_condition_type[c.condition] or ''})
    end
    return out
end

-- The manager's order queue, newest last.
function orders()
    local out = {}
    for _, o in ipairs(df.global.world.manager_orders.all) do
        local mat = ''
        local cats = {}
        for name, on in pairs(o.material_category) do
            if on == true then table.insert(cats, name) end
        end
        if #cats > 0 then
            table.sort(cats)
            mat = table.concat(cats, '/')
        elseif o.mat_type ~= -1 then
            local info = dfhack.matinfo.decode(o.mat_type, o.mat_index)
            mat = info and utf(info:toString()) or ''
        end
        table.insert(out, {id=o.id, job=df.job_type[o.job_type] or '', reaction=o.reaction_name,
                           left=o.amount_left, total=o.amount_total, material=mat,
                           freq=df.workquota_frequency_type[o.frequency] or '',
                           validated=o.status.validated, active=o.status.active,
                           conds=order_conditions(o)})
    end
    return json.encode(out)
end

-- Announcements newer than `after` (a report id; -1 for "the latest few").
-- Not RemoteFortressReader's GetReports: that keeps one "last sent" marker
-- for every client at once, so another tool calling it steals our reports,
-- and its first call returns the whole history. Combat and sparring logs
-- are left out; DF's own announcement list is what players expect.
-- Continuation lines (DF splits long messages) are joined.
function reports(after)
    local after_id = int(after, 'after')
    local reps = df.global.world.status.reports
    local start, count = #reps, 0
    for i = #reps - 1, 0, -1 do
        local r = reps[i]
        if r.id <= after_id then break end
        start = i
        if r.flags.announcement and not r.flags.continuation then
            count = count + 1
            if count >= 50 then break end
        end
    end
    local out = {}
    for i = start, #reps - 1 do
        local r = reps[i]
        if r.flags.announcement then
            local text = utf(r.text)
            if r.flags.continuation and #out > 0 then
                out[#out].text = out[#out].text .. ' ' .. text
            else
                -- Whether the place is unexplored, straight from DF: the hub's own
                -- map may not have loaded that level yet.
                local flags = r.pos.x >= 0 and dfhack.maps.getTileFlags(r.pos.x, r.pos.y, r.pos.z)
                table.insert(out, {id=r.id, text=text, type=df.announcement_type[r.type] or '',
                                   year=r.year, time=r.time, x=r.pos.x, y=r.pos.y, z=r.pos.z,
                                   hidden=(not flags) or flags.hidden,
                                   color=r.color + (r.bright and 8 or 0)})
            end
        end
    end
    local newest = #reps > 0 and reps[#reps - 1].id or after_id
    return json.encode{newest=newest, reports=out}
end

local function squad_name(u)
    if u.military.squad_id == -1 then return '' end
    local sq = df.squad.find(u.military.squad_id)
    if not sq then return '' end
    if sq.alias ~= '' then return utf(sq.alias) end
    local translate = (dfhack.translation and dfhack.translation.translateName) or dfhack.TranslateName
    local ok, name = pcall(translate, sq.name, true)
    return ok and utf(name) or ''
end

-- A unit, as the inspector shows it. Read-only.
function unit(id)
    local u = df.unit.find(int(id, 'id'))
    if not u then return json.encode{found=false} end
    local needs = {}
    local soul = u.status.current_soul
    if soul then
        for _, n in ipairs(soul.personality.needs) do
            if n.focus_level < 0 then table.insert(needs, {df.need_type[n.id] or '?', n.focus_level}) end
        end
    end
    table.sort(needs, function(a, b) return a[2] < b[2] end)
    local top = {}
    for i = 1, math.min(3, #needs) do top[i] = needs[i][1] end
    local okstress, stress = pcall(dfhack.units.getStressCategory, u)
    local okage, age = pcall(dfhack.units.getAge, u)
    return json.encode{
        found=true, id=u.id, name=utf(dfhack.units.getReadableName(u)),
        profession=utf(dfhack.units.getProfessionName(u)),
        job=u.job.current_job and utf(dfhack.job.getName(u.job.current_job)) or '',
        stress=(okstress and soul) and stress or -1, age=okage and math.floor(age) or -1,
        squad=squad_name(u), needs=top, citizen=dfhack.units.isCitizen(u),
    }
end

-- Designation priority, 1 (first) to 7 (last), for every tile in the
-- rectangle that carries a dig designation. DF keeps it per block in a
-- designation_priority event, as priority * 1000; this is how quickfort's
-- dig mode sets it too. RemoteFortressReader's dig command can't.
local function priority_event(block)
    for _, ev in ipairs(block.block_events) do
        if ev:getType() == df.block_square_event_type.designation_priority then return ev end
    end
    block.block_events:insert('#', {new=df.block_square_event_designation_priorityst})
    return block.block_events[#block.block_events - 1]
end

function set_priority(x1, y1, x2, y2, z, priority)
    x1, y1, x2, y2, z = int(x1, 'x1'), int(y1, 'y1'), int(x2, 'x2'), int(y2, 'y2'), int(z, 'z')
    local p = int(priority, 'priority')
    if p < 1 or p > 7 then error('priority must be 1 to 7') end
    if x2 < x1 or y2 < y1 or (x2 - x1 + 1) * (y2 - y1 + 1) > 4096 then error('bad rectangle') end
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local count = 0
    for x = x1, x2 do
        for y = y1, y2 do
            local block = dfhack.maps.getTileBlock(x, y, z)
            if block and block.designation[x % 16][y % 16].dig ~= df.tile_dig_designation.No then
                priority_event(block).priority[x % 16][y % 16] = p * 1000
                block.flags.designated = true
                count = count + 1
            end
        end
    end
    return json.encode{set=count}
end

function set_priority_spans(encoded, z, priority)
    z = int(z, 'z')
    local p = int(priority, 'priority')
    if p < 1 or p > 7 then error('priority must be 1 to 7') end
    if type(encoded) ~= 'string' or #encoded > 131072 then error('bad spans') end
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local spans = json.decode(encoded)
    if type(spans) ~= 'table' or #spans < 1 or #spans > 4096 then error('bad spans') end
    local total, previous_x, previous_y = 0, -1, -1
    for _, span in ipairs(spans) do
        if type(span) ~= 'table' or #span ~= 3 then error('bad span') end
        for _, value in ipairs(span) do
            if type(value) ~= 'number' or value ~= math.floor(value) then error('bad span coordinate') end
        end
        local x, first_y, last_y = span[1], span[2], span[3]
        if last_y < first_y or x < previous_x or (x == previous_x and first_y <= previous_y) then
            error('spans must be ordered and non-overlapping')
        end
        if not dfhack.maps.isValidTilePos(x, first_y, z)
                or not dfhack.maps.isValidTilePos(x, last_y, z) then error('span outside map') end
        total = total + last_y - first_y + 1
        if total > 4096 then error('too many tiles') end
        previous_x, previous_y = x, last_y
    end
    local count, blocks, events = 0, {}, {}
    for _, span in ipairs(spans) do
        local x = span[1]
        for y = span[2], span[3] do
            local key = math.floor(x / 16) .. ',' .. math.floor(y / 16)
            local block = blocks[key]
            if block == nil then
                block = dfhack.maps.getTileBlock(x, y, z) or false
                blocks[key] = block
            end
            if block and block.designation[x % 16][y % 16].dig ~= df.tile_dig_designation.No then
                local event = events[key]
                if not event then
                    event = priority_event(block)
                    events[key] = event
                end
                event.priority[x % 16][y % 16] = p * 1000
                block.flags.designated = true
                count = count + 1
            end
        end
    end
    return json.encode{set=count}
end

-- Squads. The leader is always in slot 0 and comes with a noble position, so
-- dfhack.military won't change that slot; squad.leader_position is the entity
-- position's id, not a slot. Only squads of the player's own fort are reachable.
local LEADER_SLOT = 0

local function fort_squad(id)
    local squad = df.squad.find(int(id, 'squad'))
    if squad and squad.entity_id == df.global.plotinfo.group_id then return squad end
end

local function refused(why)
    return json.encode{ok=false, why=why}
end

local function squad_member(slot, occupant)
    local hf = df.historical_figure.find(occupant)
    local u = hf and df.unit.find(hf.unit_id)
    return {slot=slot, leader=(slot == LEADER_SLOT), unit=u and u.id or -1,
            name=(u or hf) and utf(dfhack.units.getReadableName(u or hf)) or '?',
            away=(not u) or u.flags1.inactive}
end

function squads()
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local fort = df.historical_entity.find(df.global.plotinfo.group_id)
    local out = {}
    if not fort then return json.encode(out) end
    for _, id in ipairs(fort.squads) do
        local squad = df.squad.find(id)
        if squad then
            local members, orders = {}, {}
            for slot = 0, #squad.positions - 1 do
                local occupant = squad.positions[slot].occupant
                if occupant ~= -1 then table.insert(members, squad_member(slot, occupant)) end
            end
            for _, o in ipairs(squad.orders) do
                table.insert(orders, df.squad_order_type[o:getType()] or '')
            end
            local routine = df.global.plotinfo.alerts.routines[squad.cur_routine_idx]
            table.insert(out, {id=squad.id, name=utf(dfhack.military.getSquadName(squad.id)),
                               alias=utf(squad.alias), slots=#squad.positions,
                               routine=routine and utf(routine.name) or '',
                               orders=orders, members=members})
        end
    end
    return json.encode(out)
end

-- Who could join a squad: adult citizens who aren't in one.
function squad_candidates()
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local out = {}
    for _, u in ipairs(dfhack.units.getCitizens(true)) do
        if dfhack.units.isAdult(u) and u.military.squad_id == -1 then
            table.insert(out, {id=u.id, name=utf(dfhack.units.getReadableName(u))})
        end
    end
    table.sort(out, function(a, b) return a.name < b.name end)
    return json.encode(out)
end

function squad_add(squad_id, unit_id)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local squad = fort_squad(squad_id)
    if not squad then return refused('that squad is gone') end
    local u = df.unit.find(int(unit_id, 'unit'))
    if not u or not dfhack.units.isCitizen(u, true) or not dfhack.units.isAdult(u) then
        return refused('only adult citizens can join a squad')
    end
    if u.military.squad_id ~= -1 then return refused('they are already in a squad') end
    if squad.positions[LEADER_SLOT].occupant == -1 then
        return refused('this squad needs a leader first')
    end
    local open = false
    for slot = 0, #squad.positions - 1 do
        if squad.positions[slot].occupant == -1 then open = true end
    end
    if not open then return refused('this squad is full') end
    dfhack.military.addToSquad(u.id, squad.id, -1)
    if u.military.squad_id ~= squad.id then return refused('DF refused that') end
    return json.encode{ok=true, name=utf(dfhack.units.getReadableName(u))}
end

function squad_remove(squad_id, unit_id)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local squad = fort_squad(squad_id)
    if not squad then return refused('that squad is gone') end
    local u = df.unit.find(int(unit_id, 'unit'))
    if not u or u.military.squad_id ~= squad.id then return refused('they are not in this squad') end
    if u.military.squad_position == LEADER_SLOT then
        return refused('the leader comes with a noble position and stays')
    end
    if u.flags1.inactive then return refused('they are away from the fort') end
    dfhack.military.removeFromSquad(u.id)
    if u.military.squad_id ~= -1 then return refused('DF refused that') end
    return json.encode{ok=true, name=utf(dfhack.units.getReadableName(u))}
end

-- A custom name; empty goes back to the squad's generated one.
function rename_squad(squad_id, name)
    local squad = fort_squad(squad_id)
    if not squad then return refused('that squad is gone') end
    if type(name) ~= 'string' or #name > 160 or name:find('%c') then error('bad squad name') end
    squad.alias = dfhack.utf2df(name)
    return json.encode{ok=true, name=utf(dfhack.military.getSquadName(squad.id))}
end

-- Nobles and administrators. DFHack has no call for appointing, so this writes
-- what DF itself leaves behind, as read from a fort's own records (53.16):
--   assignment.histfig and histfig2 = the holder; on leaving, histfig = -1 and
--     histfig2 keeps the previous holder
--   the holder has a position link (entity_vector_idx = the entity's index in
--     world.entities.all); on leaving it becomes a former-position link
--   each change adds an add/remove_hf_entity_link history event
-- Squad-leading posts stay out of reach: their holder is also slot 0 of a squad.
local function fort_entity()
    return df.historical_entity.find(df.global.plotinfo.group_id)
end

local function position_of(fort, assignment)
    for _, p in ipairs(fort.positions.own) do
        if p.id == assignment.position_id then return p end
    end
end

local function holds_symbol(fort, assignment)
    for _, claim in ipairs(fort.artifact_claims) do
        if claim.claim_type == df.artifact_claim_type.Symbol and claim.symbol_claim_id == assignment.id then
            return true
        end
    end
    return false
end

-- Why players can't change this post, or nil if they can.
local function locked_because(fort, position, assignment)
    if not position.flags.ACTIVE then return 'this post is no longer in use' end
    if assignment.squad_id ~= -1 or position.squad_size > 0 then return 'this post leads a squad' end
    if position.flags.SUCCESSION_BY_HEIR then return 'this title passes to an heir' end
    if position.flags.ELECTED then return 'this post is elected' end
    if #position.appointed_by == 0 then return 'DF fills this post itself' end
    if #position.allowed_creature + #position.allowed_class + #position.rejected_creature
            + #position.rejected_class > 0 then
        return 'only some creatures may hold this post'
    end
    if holds_symbol(fort, assignment) then return 'this post holds a symbol of office' end
end

function nobles()
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local fort = fort_entity()
    local out = {}
    if not fort then return json.encode(out) end
    for _, a in ipairs(fort.positions.assignments) do
        local p = position_of(fort, a)
        if p and a.flags.active then
            local hf = a.histfig ~= -1 and df.historical_figure.find(a.histfig) or nil
            local duties = {}
            for name, on in pairs(p.responsibilities) do
                if on == true and type(name) == 'string' then table.insert(duties, name) end
            end
            table.sort(duties)
            table.insert(out, {id=a.id, code=p.code, title=utf(p.name[0]), rank=p.precedence, duties=duties,
                               holder=hf and utf(dfhack.units.getReadableName(hf)) or '',
                               locked=locked_because(fort, p, a) or '',
                               needs={office=p.required_office, bedroom=p.required_bedroom,
                                      dining=p.required_dining, tomb=p.required_tomb,
                                      boxes=p.required_boxes, cabinets=p.required_cabinets,
                                      racks=p.required_racks, stands=p.required_stands}})
        end
    end
    table.sort(out, function(x, y) return x.rank < y.rank end)
    return json.encode(out)
end

-- Who could hold a post: adult citizens here at the fort. As in DF, one dwarf
-- may hold several.
function noble_candidates()
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local out = {}
    for _, u in ipairs(dfhack.units.getCitizens(true)) do
        if dfhack.units.isAdult(u) and u.hist_figure_id ~= -1 and not u.flags1.inactive then
            table.insert(out, {id=u.id, name=utf(dfhack.units.getReadableName(u))})
        end
    end
    table.sort(out, function(a, b) return a.name < b.name end)
    return json.encode(out)
end

local function find_assignment(fort, id)
    for i, a in ipairs(fort.positions.assignments) do
        if a.id == id then return a, i end
    end
end

local function position_link_index(hf, fort, assignment)
    for i, link in ipairs(hf.entity_links) do
        if df.histfig_entity_link_positionst:is_instance(link) and link.entity_id == fort.id
                and link.assignment_id == assignment.id then
            return i, link
        end
    end
end

local function add_event(class, fields)
    fields.new = class
    fields.year = df.global.cur_year
    fields.seconds = df.global.cur_year_tick
    fields.id = df.global.hist_event_next_id
    df.global.hist_event_next_id = df.global.hist_event_next_id + 1
    df.global.world.history.events:insert('#', fields)
end

-- Everything that can refuse is checked by the callers first; these only write.
local function write_vacate(fort, position, assignment, hf, index, link)
    local start_year = link.start_year
    hf.entity_links:erase(index)
    link:delete()
    hf.entity_links:insert('#', {new=df.histfig_entity_link_former_positionst, entity_id=fort.id,
                                 link_strength=100, assignment_id=assignment.id,
                                 start_year=start_year, end_year=df.global.cur_year})
    assignment.histfig = -1
    add_event(df.history_event_remove_hf_entity_linkst, {civ=fort.id, histfig=hf.id, position_id=position.id,
                                                         link_type=df.histfig_entity_link_type.POSITION})
end

local function write_appoint(fort, position, assignment, assignment_index, entity_index, hf)
    hf.entity_links:insert('#', {new=df.histfig_entity_link_positionst, entity_id=fort.id,
                                 entity_vector_idx=entity_index, link_strength=100,
                                 assignment_id=assignment.id, assignment_vector_idx=assignment_index,
                                 start_year=df.global.cur_year})
    assignment.histfig = hf.id
    assignment.histfig2 = hf.id
    add_event(df.history_event_add_hf_entity_linkst, {civ=fort.id, histfig=hf.id, position_id=position.id,
                                                      link_type=df.histfig_entity_link_type.POSITION,
                                                      appointer_hfid=-1, promise_to_hfid=-1})
end

local function changeable(id)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local fort = fort_entity()
    local assignment, index = nil, nil
    if fort then assignment, index = find_assignment(fort, int(id, 'post')) end
    if not assignment or not assignment.flags.active then return nil, 'that post is gone' end
    local position = position_of(fort, assignment)
    if not position then return nil, 'that post is gone' end
    local why = locked_because(fort, position, assignment)
    if why then return nil, why end
    return {fort=fort, assignment=assignment, index=index, position=position}
end

function appoint_noble(id, unit_id)
    local post, why = changeable(id)
    if not post then return refused(why) end
    local u = df.unit.find(int(unit_id, 'unit'))
    if not u or not dfhack.units.isCitizen(u, true) or not dfhack.units.isAdult(u)
            or u.flags1.inactive or u.hist_figure_id == -1 then
        return refused('only adult citizens at the fort can hold a post')
    end
    local new = df.historical_figure.find(u.hist_figure_id)
    if not new then return refused('only adult citizens at the fort can hold a post') end
    local fort, a = post.fort, post.assignment
    if a.histfig == new.id then return refused('they already hold this post') end
    local entity_index
    for i, entity in ipairs(df.global.world.entities.all) do
        if entity.id == fort.id then entity_index = i break end
    end
    if not entity_index then return refused('DF refused that') end
    local old = a.histfig ~= -1 and df.historical_figure.find(a.histfig) or nil
    local link_index, link
    if a.histfig ~= -1 then
        if old then link_index, link = position_link_index(old, fort, a) end
        if not link then return refused("DF's records for this post don't line up; change it in DF") end
    end
    if link then write_vacate(fort, post.position, a, old, link_index, link) end
    write_appoint(fort, post.position, a, post.index, entity_index, new)
    return json.encode{ok=true, name=utf(dfhack.units.getReadableName(new))}
end

function vacate_noble(id)
    local post, why = changeable(id)
    if not post then return refused(why) end
    local fort, a = post.fort, post.assignment
    if a.histfig == -1 then return refused('that post is already empty') end
    local hf = df.historical_figure.find(a.histfig)
    local link_index, link
    if hf then link_index, link = position_link_index(hf, fort, a) end
    if not link then return refused("DF's records for this post don't line up; change it in DF") end
    local name = utf(dfhack.units.getReadableName(hf))
    write_vacate(fort, post.position, a, hf, link_index, link)
    return json.encode{ok=true, name=name}
end

-- The game clock: year and tick within the year (1200 ticks a day, 28 days
-- a month, 12 months).
function date()
    return json.encode{year=df.global.cur_year, tick=df.global.cur_year_tick}
end

return _ENV
