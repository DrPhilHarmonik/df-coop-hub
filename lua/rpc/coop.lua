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

VERSION = '20'

-- Results go out through this rather than DFHack's json.encode, which took 15 ms
-- of a 20 ms unit read and most of the work-order read on a real fort; this does
-- the same in a fraction of a millisecond. An empty table is [], as DFHack's is.
-- Only strings, numbers, booleans and tables of them are expected; anything
-- else is a mistake here, so it stops loudly.
local ESCAPES = {['"']='\\"', ['\\']='\\\\', ['\b']='\\b', ['\f']='\\f', ['\n']='\\n', ['\r']='\\r', ['\t']='\\t'}

local function escape(c)
    return ESCAPES[c] or ('\\u%04x'):format(c:byte())
end

local function put(v, out)
    local t = type(v)
    if t == 'string' then
        out[#out + 1] = '"' .. v:gsub('[%c"\\]', escape) .. '"'
    elseif t == 'number' then
        if math.type(v) == 'integer' then out[#out + 1] = ('%d'):format(v)
        elseif v ~= v or v == math.huge or v == -math.huge then out[#out + 1] = 'null'
        else out[#out + 1] = ('%.14g'):format(v) end
    elseif t == 'boolean' then
        out[#out + 1] = v and 'true' or 'false'
    elseif t == 'table' then
        local n, keys = #v, 0
        for _ in pairs(v) do keys = keys + 1 end
        if keys == n then
            out[#out + 1] = '['
            for i = 1, n do
                if i > 1 then out[#out + 1] = ',' end
                put(v[i], out)
            end
            out[#out + 1] = ']'
        else
            out[#out + 1] = '{'
            local first = true
            for k, item in pairs(v) do
                if not first then out[#out + 1] = ',' end
                first = false
                put(tostring(k), out)
                out[#out + 1] = ':'
                put(item, out)
            end
            out[#out + 1] = '}'
        end
    else
        error('to_json: cannot send a ' .. t)
    end
end

function to_json(v)
    local out = {}
    put(v, out)
    return table.concat(out)
end

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
    return to_json(out)
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
    if not bld then return to_json{found=false} end
    local out = {found=true, id=bld.id, type=df.building_type[bld:getType()]}
    if dfhack.buildings.markedForRemoval(bld) then
        out.outcome = 'already'
    elseif dfhack.buildings.deconstruct(bld) then
        out.outcome = 'removed'
    else
        out.outcome = 'queued'
    end
    return to_json(out)
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

-- Workshops and furnaces of this fort. `kind` names the type the way the hub's
-- catalog does: a df.workshop_type name, Furnace:<df.furnace_type name>, or
-- Custom:<raws code> for buildings such as the screw press.
local function find_workshop(id)
    local b = df.building.find(int(id, 'workshop'))
    if b and (df.building_workshopst:is_instance(b) or df.building_furnacest:is_instance(b)) then return b end
end

local function is_built(b)
    return b:getBuildStage() == b:getMaxBuildStage()
end

local function workshop_kind(b)
    if df.building_furnacest:is_instance(b) then
        if b.type == df.furnace_type.Custom then
            local def = df.building_def.find(b.custom_type)
            return 'Custom:' .. (def and def.code or '?')
        end
        return 'Furnace:' .. (df.furnace_type[b.type] or '?')
    end
    if b.type == df.workshop_type.Custom then
        local def = df.building_def.find(b.custom_type)
        return 'Custom:' .. (def and def.code or '?')
    end
    return df.workshop_type[b.type] or '?'
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
    return to_json{ok=(rc == CR_OK), output=(output or ''):sub(1, 300)}
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
                    return to_json{ok=false, busy=true}
                end
                link = link.next
            end
            all:erase(i)
            o:delete()
            -- Nothing can wait for an order that no longer exists.
            for _, other in ipairs(all) do
                local conds = other.order_conditions
                for j = #conds - 1, 0, -1 do
                    if conds[j].order_id == want then
                        local c = conds[j]
                        conds:erase(j)
                        c:delete()
                    end
                end
            end
            return to_json{ok=true}
        end
    end
    return to_json{ok=false, missing=true}
end

-- Change an order's amount, how often it repeats and/or the most workshops that
-- may work on it (0 = no limit). A new amount restarts the count: `amount` more
-- to make, whatever was left before.
function set_order(id, amount, freq, max_workshops)
    local want = int(id, 'id')
    local n = (amount and amount ~= '') and checked_amount(amount) or nil
    local f = (freq and freq ~= '') and frequency(freq) or nil
    local m = (max_workshops and max_workshops ~= '') and int(max_workshops, 'max_workshops') or nil
    if m and (m < 0 or m > 99) then error('max workshops out of range') end
    if not n and not f and not m then error('nothing to change') end
    local o = find_order(want)
    if not o then return to_json{ok=false, missing=true} end
    if n then
        o.amount_left = n
        o.amount_total = n
    end
    if f then o.frequency = f end
    if m then o.max_workshops = m end
    return to_json{ok=true}
end

-- Move an order within the queue, which is the order the manager works through.
-- DFHack's `orders sort` rearranges the same vector; nothing refers to an order
-- by its position.
function move_order(id, to)
    local want = int(id, 'id')
    local o, i, all = find_order(want)
    if not o then return to_json{ok=false, missing=true} end
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
    return to_json{ok=true}
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
    -- Walking a bitfield is slow and these are nearly always empty: look first.
    for _, field in ipairs{'flags1', 'flags2', 'flags3'} do
        if c[field].whole ~= 0 then
            for name, on in pairs(c[field]) do
                if on == true and type(name) == 'string' then table.insert(out.flags, name) end
            end
        end
    end
    table.sort(out.flags)
    return out
end

-- A condition has no id of its own, so it is named by what it says. A player
-- working from an out-of-date list then can't change or delete the wrong one.
local function item_condition_key(c)
    return table.concat({'i', c.compare_type, c.compare_val, c.item_type, c.item_subtype, c.mat_type, c.mat_index,
                         c.flags1.whole, c.flags2.whole, c.flags3.whole, c.reaction_id,
                         c.has_material_reaction_product, c.reaction_class}, ':')
end

local function order_condition_key(c)
    return table.concat({'o', c.order_id, c.condition}, ':')
end

local function order_conditions(o)
    local out = {}
    for _, c in ipairs(o.item_conditions) do
        local info = item_condition(c)
        info.key = item_condition_key(c)
        table.insert(out, info)
    end
    for _, c in ipairs(o.order_conditions) do
        table.insert(out, {after=c.order_id, when=df.workquota_order_condition_type[c.condition] or '',
                           key=order_condition_key(c)})
    end
    return out
end

local function compare_type(name)
    if type(name) ~= 'string' or not name:match('^%a+$') then error('bad comparison: ' .. tostring(name)) end
    local value = df.logic_condition_type[name]
    if type(value) ~= 'number' or value < 0 then error('bad comparison: ' .. name) end
    return value
end

local function compare_value(val)
    local n = int(val, 'value')
    if n < 0 or n > 999999 then error('value out of range') end
    return n
end

-- Change the comparison and number of an item condition.
function edit_condition(id, key, cmp, val)
    local compare, n = compare_type(cmp), compare_value(val)
    local o = find_order(int(id, 'id'))
    if not o then return to_json{ok=false, missing=true} end
    for _, c in ipairs(o.item_conditions) do
        if item_condition_key(c) == key then
            c.compare_type = compare
            c.compare_val = n
            return to_json{ok=true}
        end
    end
    return to_json{ok=false, stale=true}
end

function remove_condition(id, key)
    local o = find_order(int(id, 'id'))
    if not o then return to_json{ok=false, missing=true} end
    for _, pair in ipairs{{o.item_conditions, item_condition_key}, {o.order_conditions, order_condition_key}} do
        local conds, key_of = pair[1], pair[2]
        for i = 0, #conds - 1 do
            local c = conds[i]
            if key_of(c) == key then
                conds:erase(i)
                c:delete()
                return to_json{ok=true}
            end
        end
    end
    return to_json{ok=false, stale=true}
end

-- "At least 5 beds". Only the item type is set, like the plain conditions DF
-- keeps for "fewer than N beds"; everything else stays at its default.
function add_item_condition(id, item, cmp, val)
    if type(item) ~= 'string' or not item:match('^[A-Z_]+$') then error('bad item: ' .. tostring(item)) end
    local item_type = df.item_type[item]
    if type(item_type) ~= 'number' or item_type < 0 then error('bad item: ' .. item) end
    local compare, n = compare_type(cmp), compare_value(val)
    local o = find_order(int(id, 'id'))
    if not o then return to_json{ok=false, missing=true} end
    local c = df.manager_order_condition_item:new()
    c.compare_type = compare
    c.compare_val = n
    c.item_type = item_type
    local key = item_condition_key(c)
    for _, other in ipairs(o.item_conditions) do
        if item_condition_key(other) == key then
            c:delete()
            return to_json{ok=false, duplicate=true}
        end
    end
    o.item_conditions:insert('#', c)
    return to_json{ok=true}
end

-- Whether `from` already waits for `target`, directly or through other orders.
local function waits_for(from, target)
    local seen, todo = {}, {from}
    while #todo > 0 do
        local id = table.remove(todo)
        if id == target then return true end
        if not seen[id] then
            seen[id] = true
            local o = find_order(id)
            if o then
                for _, c in ipairs(o.order_conditions) do table.insert(todo, c.order_id) end
            end
        end
    end
    return false
end

-- Wait for another order. Two orders waiting for each other would never start.
function add_order_condition(id, other, when)
    if type(when) ~= 'string' or not when:match('^%a+$') then error('bad state: ' .. tostring(when)) end
    local state = df.workquota_order_condition_type[when]
    if type(state) ~= 'number' or state < 0 then error('bad state: ' .. when) end
    local want, after = int(id, 'id'), int(other, 'other')
    local o = find_order(want)
    if not o or not find_order(after) then return to_json{ok=false, missing=true} end
    if want == after or waits_for(after, want) then return to_json{ok=false, loop=true} end
    for _, c in ipairs(o.order_conditions) do
        if c.order_id == after and c.condition == state then return to_json{ok=false, duplicate=true} end
    end
    local c = df.manager_order_condition_order:new()
    c.order_id = after
    c.condition = state
    o.order_conditions:insert('#', c)
    return to_json{ok=true}
end

-- The manager's order queue, newest last.
function orders()
    local out = {}
    for _, o in ipairs(df.global.world.manager_orders.all) do
        local mat = ''
        local cats = {}
        if o.material_category.whole ~= 0 then
            for name, on in pairs(o.material_category) do
                if on == true and type(name) == 'string' then table.insert(cats, name) end
            end
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
                           max_workshops=o.max_workshops,
                           conds=order_conditions(o)})
    end
    return to_json(out)
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
    return to_json{newest=newest, reports=out}
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
    if not u then return to_json{found=false} end
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
    return to_json{
        found=true, id=u.id, name=utf(dfhack.units.getReadableName(u)),
        profession=utf(dfhack.units.getProfessionName(u)),
        job=u.job.current_job and utf(dfhack.job.getName(u.job.current_job)) or '',
        stress=(okstress and soul) and stress or -1, age=okage and math.floor(age) or -1,
        squad=squad_name(u), needs=top, citizen=dfhack.units.isCitizen(u),
    }
end

-- Everyone in the fort, for the citizens panel. A location only comes with
-- those on a tile the players have seen, so the list can't reveal hidden places.
function citizens()
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local out = {}
    for _, u in ipairs(dfhack.units.getCitizens(true)) do
        local ok, stress = pcall(dfhack.units.getStressCategory, u)
        local entry = {id=u.id, name=utf(dfhack.units.getReadableName(u)),
                       profession=utf(dfhack.units.getProfessionName(u)),
                       job=u.job.current_job and utf(dfhack.job.getName(u.job.current_job)) or '',
                       stress=(ok and u.status.current_soul) and stress or -1, squad=squad_name(u)}
        local x, y, z = dfhack.units.getPosition(u)
        local flags = x and dfhack.maps.getTileFlags(x, y, z)
        if flags and not flags.hidden and not dfhack.units.isHidden(u) then
            entry.x, entry.y, entry.z = x, y, z
        end
        table.insert(out, entry)
    end
    table.sort(out, function(a, b) return a.name < b.name end)
    return to_json(out)
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
    return to_json{set=count}
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
    return to_json{set=count}
end

-- Smoothing, engraving and fortifications, through quickfort's dig mode, which
-- knows which tiles qualify (hard natural rock; smooth before engrave; fortify a
-- smooth wall). DF turns a mark into its own job within a tick and clears the
-- tile's flag (seen 11-09), so marks are read back from those jobs too, and
-- erasing cancels the jobs as well as clearing flags.
local DETAIL_KEYS = {smooth='s', engrave='e', fortify='F'}
local DETAIL_JOBS = {SmoothFloor='smooth', SmoothWall='smooth', DetailFloor='engrave', DetailWall='engrave',
                     CarveFortification='fortify'}
local MAX_DETAIL_SIDE = 200

local function rect(x1, y1, x2, y2, z)
    x1, y1, x2, y2, z = int(x1, 'x1'), int(y1, 'y1'), int(x2, 'x2'), int(y2, 'y2'), int(z, 'z')
    if x2 < x1 then x1, x2 = x2, x1 end
    if y2 < y1 then y1, y2 = y2, y1 end
    if not dfhack.maps.isValidTilePos(x1, y1, z) or not dfhack.maps.isValidTilePos(x2, y2, z) then
        error('rectangle outside the map')
    end
    if x2 - x1 >= MAX_DETAIL_SIDE or y2 - y1 >= MAX_DETAIL_SIDE then error('rectangle too large') end
    return x1, y1, x2, y2, z
end

local function detail_jobs()
    local out = {}
    local link = df.global.world.jobs.list.next
    while link do
        local j = link.item
        local kind = j and DETAIL_JOBS[df.job_type[j.job_type]]
        if kind then out[#out + 1] = {job=j, kind=kind} end
        link = link.next
    end
    return out
end

function detail(kind, x1, y1, x2, y2, z, priority)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local key = DETAIL_KEYS[kind]
    if not key then error('unknown kind: ' .. tostring(kind)) end
    x1, y1, x2, y2, z = rect(x1, y1, x2, y2, z)
    local p = int(priority, 'priority')
    if p < 1 or p > 7 then error('priority must be 1 to 7') end
    local stats = reqscript('quickfort').apply_blueprint{
        mode='dig', data=('%s%d(%dx%d)'):format(key, p, x2 - x1 + 1, y2 - y1 + 1), pos={x=x1, y=y1, z=z}}
    local function value(id) return stats[id] and stats[id].value or 0 end
    return to_json{ok=true, applied=value('dig_designated'),
                   skipped=value('dig_invalid_tiles') + value('dig_protected_engraving')}
end

-- Clear smoothing, engraving and fortification marks in the rectangle, and cancel
-- the jobs DF made from them. Dig marks are left alone: they're RFR's to erase.
function detail_erase(x1, y1, x2, y2, z)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    x1, y1, x2, y2, z = rect(x1, y1, x2, y2, z)
    local cleared = 0
    for x = x1, x2 do
        for y = y1, y2 do
            local flags = dfhack.maps.getTileFlags(x, y, z)
            if flags and flags.smooth ~= 0 then
                flags.smooth = 0
                cleared = cleared + 1
            end
        end
    end
    for _, d in ipairs(detail_jobs()) do
        local pos = d.job.pos
        if pos.z == z and pos.x >= x1 and pos.x <= x2 and pos.y >= y1 and pos.y <= y2
                and dfhack.job.removeJob(d.job) then
            cleared = cleared + 1
        end
    end
    return to_json{ok=true, cleared=cleared}
end

-- Every smoothing, engraving and fortification mark on these levels (comma-separated
-- z): {z={{x, y, kind}, ...}}, from DF's jobs and from flags not yet turned into
-- jobs. Only blocks DF has flagged as designated are looked at.
function detail_marks(levels)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    if type(levels) ~= 'string' or not levels:match('^%d+[%d,]*$') or #levels > 400 then
        error('bad levels: ' .. tostring(levels))
    end
    local zs, out, seen = {}, {}, {}
    for z in levels:gmatch('%d+') do zs[tonumber(z)] = true end
    local function add(x, y, z, kind)
        local key = x .. ',' .. y .. ',' .. z
        if seen[key] then return end
        seen[key] = true
        local list = out[tostring(z)] or {}
        out[tostring(z)] = list
        list[#list + 1] = {x, y, kind}
    end
    for _, d in ipairs(detail_jobs()) do
        local pos = d.job.pos
        if zs[pos.z] then add(pos.x, pos.y, pos.z, d.kind) end
    end
    local bx, by = df.global.world.map.x_count_block, df.global.world.map.y_count_block
    for z in pairs(zs) do
        for i = 0, bx - 1 do
            for j = 0, by - 1 do
                local b = dfhack.maps.getBlock(i, j, z)
                if b and b.flags.designated then
                    for x = 0, 15 do
                        for y = 0, 15 do
                            local d = b.designation[x][y]
                            if d.smooth ~= 0 and not d.hidden then
                                -- 1 marks smoothing (a fortification on a smooth wall), 2 engraving.
                                local tx, ty = b.map_pos.x + x, b.map_pos.y + y
                                local kind = d.smooth == 2 and 'engrave' or 'smooth'
                                if d.smooth == 1 then
                                    local tt = dfhack.maps.getTileType(tx, ty, z)
                                    if tt and df.tiletype.attrs[tt].special == df.tiletype_special.SMOOTH then
                                        kind = 'fortify'
                                    end
                                end
                                add(tx, ty, z, kind)
                            end
                        end
                    end
                end
            end
        end
    end
    return to_json(out)
end

-- Creatures players can see on the levels they're looking at (`levels` is a
-- comma-separated list of z), for the map. Replaces RemoteFortressReader's unit
-- list, which sends every unit DF knows (1960 on a real fort, 535 KB) twice a
-- second at about 23 ms; this took about 4 ms for two busy levels. Names are
-- RFR's (nickname and English surname), colours DF's profession colour. With
-- `equipment` '1', worn and wielded items (item type and subtype) and whether
-- it's a baby, for graphics; they cost as much again, so only when asked.
-- Rows are written directly: they're the one thing sent this often.
local function json_string(v)
    return '"' .. v:gsub('[%c"\\]', escape) .. '"'
end

function units(levels, equipment)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    if type(levels) ~= 'string' or not levels:match('^%d+[%d,]*$') or #levels > 400 then
        error('bad levels: ' .. tostring(levels))
    end
    local zs = {}
    for z in levels:gmatch('%d+') do zs[tonumber(z)] = true end
    local gear = equipment == '1'
    local WORN, WEAPON = df.inv_item_role_type.Worn, df.inv_item_role_type.Weapon
    local palette = df.global.gps.uccolor
    local rows = {}
    for _, u in ipairs(df.global.world.units.active) do
        local p = u.pos
        -- Ambushers stay hidden, as in DF and as the hub always did.
        local f1 = u.flags1
        if zs[p.z] and not (f1.inactive or f1.left or f1.hidden_in_ambush or u.flags2.killed)
                and not dfhack.units.isDead(u) then
            local tile = dfhack.maps.getTileFlags(p.x, p.y, p.z)
            if tile and not tile.hidden then
                local rgb = palette[dfhack.units.getProfessionColor(u)]
                local name = utf(dfhack.translation.translateName(dfhack.units.getVisibleName(u), true))
                local row = ('{"i":%d,"x":%d,"y":%d,"z":%d,"n":%s,"c":"#%02x%02x%02x","race":%d,"caste":%d'):format(
                    u.id, p.x, p.y, p.z, json_string(name), rgb[0], rgb[1], rgb[2], u.race, u.caste)
                if gear then
                    local items = {}
                    for _, inv in ipairs(u.inventory) do
                        if inv.mode == WORN or inv.mode == WEAPON then
                            items[#items + 1] = ('[%d,%d]'):format(inv.item:getType(), inv.item:getSubtype())
                        end
                    end
                    local baby = math.floor(dfhack.units.getAge(u, true)) == 0
                    row = row .. (',"gear":[%s],"baby":%s'):format(table.concat(items, ','), baby and 'true' or 'false')
                end
                rows[#rows + 1] = row .. '}'
            end
        end
    end
    return '[' .. table.concat(rows, ',') .. ']'
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
    return to_json{ok=false, why=why}
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
    if not fort then return to_json(out) end
    for _, id in ipairs(fort.squads) do
        local squad = df.squad.find(id)
        if squad then
            local members, orders = {}, {}
            for slot = 0, #squad.positions - 1 do
                local occupant = squad.positions[slot].occupant
                if occupant ~= -1 then table.insert(members, squad_member(slot, occupant)) end
            end
            local station, kill = nil, 0
            for _, o in ipairs(squad.orders) do
                table.insert(orders, df.squad_order_type[o:getType()] or '')
                if df.squad_order_movest:is_instance(o) then
                    station = {x=o.pos.x, y=o.pos.y, z=o.pos.z}
                elseif df.squad_order_kill_listst:is_instance(o) then
                    kill = kill + #o.units + #o.histfigs
                end
            end
            local routine = df.global.plotinfo.alerts.routines[squad.cur_routine_idx]
            table.insert(out, {id=squad.id, name=utf(dfhack.military.getSquadName(squad.id)),
                               alias=utf(squad.alias), slots=#squad.positions,
                               routine=routine and utf(routine.name) or '',
                               orders=orders, station=station, kill=kill, members=members})
        end
    end
    return to_json(out)
end

-- Orders given right now, as on DF's squad screen. A station order as DF writes
-- it (captured 11-08): a squad_order_movest in squad.orders with the tile,
-- point_id -1, the current year and tick, no flags, no issuer. Cancelling removes
-- it. Kill orders take the same shape; only station and kill orders are touched
-- here, so anything else DF has given a squad is left alone.
local function clear_immediate_orders(squad)
    local orders = squad.orders
    for i = #orders - 1, 0, -1 do
        local o = orders[i]
        if df.squad_order_movest:is_instance(o) or df.squad_order_kill_listst:is_instance(o) then
            orders:erase(i)
            o:delete()
        end
    end
end

local function stamped(class)
    local o = class:new()
    o.year = df.global.cur_year
    o.year_tick = df.global.cur_year_tick
    return o
end

local function has_members(squad)
    for slot = 0, #squad.positions - 1 do
        if squad.positions[slot].occupant ~= -1 then return true end
    end
    return false
end

function squad_station(squad_id, x, y, z)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    x, y, z = int(x, 'x'), int(y, 'y'), int(z, 'z')
    local squad = fort_squad(squad_id)
    if not squad then return refused('that squad is gone') end
    if not has_members(squad) then return refused('this squad has nobody in it') end
    if not dfhack.maps.isValidTilePos(x, y, z) then return refused('that spot is outside the map') end
    local flags = dfhack.maps.getTileFlags(x, y, z)
    if not flags or flags.hidden then return refused("you can't station a squad there") end
    local tiletype = dfhack.maps.getTileType(x, y, z)
    local shape = tiletype and df.tiletype.attrs[tiletype].shape
    if not shape or not df.tiletype_shape.attrs[shape].walkable then
        return refused("dwarves can't stand there")
    end
    clear_immediate_orders(squad)
    local order = stamped(df.squad_order_movest)
    order.pos.x, order.pos.y, order.pos.z = x, y, z
    order.point_id = -1
    squad.orders:insert('#', order)
    return to_json{ok=true}
end

local function is_target(u)
    if not u or not dfhack.units.isActive(u) or dfhack.units.isDead(u) then return false end
    if not dfhack.units.isDanger(u) or dfhack.units.isHidden(u) then return false end
    if u.flags1.caged or u.flags1.chained then return false end
    local flags = dfhack.maps.getTileFlags(dfhack.units.getPosition(u))
    return flags and not flags.hidden
end

-- What a kill order may name: enemies in plain sight, as DF's own screen allows.
function squad_targets()
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local out = {}
    for _, u in ipairs(df.global.world.units.active) do
        if is_target(u) then
            local x, y, z = dfhack.units.getPosition(u)
            table.insert(out, {id=u.id, name=utf(dfhack.units.getReadableName(u)), x=x, y=y, z=z})
        end
    end
    return to_json(out)
end

-- `units` is comma-separated ids. Every one must be an enemy, so a kill order
-- can never be aimed at a citizen or a friend.
function squad_kill(squad_id, units)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    if type(units) ~= 'string' or not units:match('^%d+[%d,]*$') or #units > 1024 then error('bad units') end
    local squad = fort_squad(squad_id)
    if not squad then return refused('that squad is gone') end
    if not has_members(squad) then return refused('this squad has nobody in it') end
    local targets = {}
    for id in units:gmatch('%d+') do
        local u = df.unit.find(tonumber(id))
        if not is_target(u) then return refused('that creature is not an enemy in sight') end
        table.insert(targets, u)
    end
    if #targets == 0 then return refused('no target') end
    clear_immediate_orders(squad)
    local order = stamped(df.squad_order_kill_listst)
    for _, u in ipairs(targets) do order.units:insert('#', u.id) end
    order.title = #targets == 1 and dfhack.units.getReadableName(targets[1]) or ('%d enemies'):format(#targets)
    squad.orders:insert('#', order)
    return to_json{ok=true, count=#targets}
end

function squad_clear(squad_id)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local squad = fort_squad(squad_id)
    if not squad then return refused('that squad is gone') end
    clear_immediate_orders(squad)
    return to_json{ok=true}
end

-- Work details. DF only applies one to dwarves when its own screen is used, so a
-- change here sets their labors too, by the rule below, which matches all 73 adults
-- on a real fort exactly. Children have no labors. Rosters keep the dead.
local TOOL_LABORS = {'MINE', 'CUTWOOD', 'HUNT'}   -- no dwarf on a real fort holds two

local function in_detail(detail, unit_id)
    for _, id in ipairs(detail.assigned_units) do
        if id == unit_id then return true end
    end
    return false
end

-- DF keeps these lists sorted by id.
local function set_in_detail(detail, unit_id, on)
    local list = detail.assigned_units
    for i = 0, #list - 1 do
        if list[i] == unit_id then
            if not on then list:erase(i) end
            return
        end
        if list[i] > unit_id then
            if on then list:insert(i, unit_id) end
            return
        end
    end
    if on then list:insert('#', unit_id) end
end

local function detail_says_yes(detail, unit_id)
    local mode = detail.flags.mode
    return mode == df.work_detail_mode.EverybodyDoesThis
        or (mode == df.work_detail_mode.OnlySelectedDoesThis and in_detail(detail, unit_id))
end

-- Set a dwarf's labors for what this detail covers. A labor can be in several details.
local function apply_detail(unit, detail)
    local details = df.global.plotinfo.labor_info.work_details
    for labor, allowed in ipairs(detail.allowed_labors) do
        if allowed then
            local want = false
            for _, other in ipairs(details) do
                if other.allowed_labors[labor] and detail_says_yes(other, unit.id) then want = true end
            end
            unit.status.labors[labor] = want
        end
    end
    pcall(dfhack.units.setAutomaticProfessions, unit)
end

-- By place and name, so a list that's out of date can't change the wrong one.
local function find_detail(index, name)
    local detail = df.global.plotinfo.labor_info.work_details[int(index, 'detail')]
    if detail and utf(detail.name) == name then return detail end
end

function work_details()
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local people, alive = {}, {}
    for _, u in ipairs(dfhack.units.getCitizens(true)) do
        if dfhack.units.isAdult(u) then
            alive[u.id] = true
            table.insert(people, {id=u.id, name=utf(dfhack.units.getReadableName(u))})
        end
    end
    table.sort(people, function(a, b) return a.name < b.name end)
    local out = {}
    for i, detail in ipairs(df.global.plotinfo.labor_info.work_details) do
        local labors, members = {}, {}
        for labor, allowed in ipairs(detail.allowed_labors) do
            if allowed then table.insert(labors, df.unit_labor[labor] or '') end
        end
        for _, id in ipairs(detail.assigned_units) do
            if alive[id] then table.insert(members, id) end
        end
        table.insert(out, {i=i, name=utf(detail.name), mode=df.work_detail_mode[detail.flags.mode] or '',
                           no_everybody=detail.flags.cannot_be_everybody, labors=labors, members=members})
    end
    return to_json{details=out, people=people}
end

function work_detail_member(index, name, unit_id, on)
    if on ~= '1' and on ~= '0' then error('bad on: ' .. tostring(on)) end
    local detail = find_detail(index, name)
    if not detail then return refused('that work detail has changed') end
    if detail.flags.mode ~= df.work_detail_mode.OnlySelectedDoesThis then
        return refused("everyone follows this detail's setting, so nobody can be picked")
    end
    local u = df.unit.find(int(unit_id, 'unit'))
    if not u or not dfhack.units.isCitizen(u, true) or not dfhack.units.isAdult(u) or u.flags1.inactive then
        return refused('only adult citizens at the fort can be picked')
    end
    if on == '1' then
        for _, tool in ipairs(TOOL_LABORS) do
            if detail.allowed_labors[tool] then
                for _, other in ipairs(TOOL_LABORS) do
                    if other ~= tool and u.status.labors[other] then
                        return refused('they already use a tool for another labor; take them off that first')
                    end
                end
            end
        end
    end
    set_in_detail(detail, u.id, on == '1')
    apply_detail(u, detail)
    return to_json{ok=true, name=utf(dfhack.units.getReadableName(u))}
end

function work_detail_mode(index, name, mode)
    if type(mode) ~= 'string' or not mode:match('^%a+$') then error('bad mode: ' .. tostring(mode)) end
    local value = df.work_detail_mode[mode]
    if value ~= df.work_detail_mode.EverybodyDoesThis and value ~= df.work_detail_mode.NobodyDoesThis
            and value ~= df.work_detail_mode.OnlySelectedDoesThis then
        error('bad mode: ' .. mode)
    end
    local detail = find_detail(index, name)
    if not detail then return refused('that work detail has changed') end
    if value == df.work_detail_mode.EverybodyDoesThis and detail.flags.cannot_be_everybody then
        return refused("this one needs a tool, so it can't be everybody")
    end
    detail.flags.mode = value
    for _, u in ipairs(dfhack.units.getCitizens(true)) do
        if dfhack.units.isAdult(u) then apply_detail(u, detail) end
    end
    return to_json{ok=true}
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
    return to_json(out)
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
    return to_json{ok=true, name=utf(dfhack.units.getReadableName(u))}
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
    return to_json{ok=true, name=utf(dfhack.units.getReadableName(u))}
end

-- A custom name; empty goes back to the squad's generated one.
function rename_squad(squad_id, name)
    local squad = fort_squad(squad_id)
    if not squad then return refused('that squad is gone') end
    if type(name) ~= 'string' or #name > 160 or name:find('%c') then error('bad squad name') end
    squad.alias = dfhack.utf2df(name)
    return to_json{ok=true, name=utf(dfhack.military.getSquadName(squad.id))}
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
    if not fort then return to_json(out) end
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
    return to_json(out)
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
    return to_json(out)
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
    return to_json{ok=true, name=utf(dfhack.units.getReadableName(new))}
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
    return to_json{ok=true, name=name}
end

-- Stocks: everything the fort has, counted by kind (item type, subtype and
-- material), for the Stocks panel. Not DF's own Stocks screen, whose rules aren't
-- known here: left out are foreign and traders' goods, webs, items built into
-- constructions or placed as buildings, things carried by anyone but citizens,
-- and anything on a tile the players haven't seen. Reading 62k items took
-- about 0.3 s on a real fort, so the hub asks only while someone looks.
local SKIP_FLAGS = {'foreign', 'trader', 'spider_web', 'garbage_collect', 'removed', 'construction', 'in_building'}

-- Reading fields through DFHack costs about a microsecond each, which adds up over
-- 62k items (0.37 s). So the skip flags are compared as one number, and instead of
-- asking who holds each of the 18k items "in inventory" (mostly in bins), the few
-- hundred held by anyone but a citizen (and what's in their packs) are collected first.
local function stock_filters()
    local probe = df.item_flags:new()
    for _, name in ipairs(SKIP_FLAGS) do probe[name] = true end
    local mask = probe.whole
    probe:delete()
    local outsiders = {}
    for _, u in ipairs(df.global.world.units.all) do
        if #u.inventory > 0 and not dfhack.units.isCitizen(u, true) then
            for _, inv in ipairs(u.inventory) do
                outsiders[inv.item.id] = true
                for _, inside in ipairs(dfhack.items.getContainedItems(inv.item)) do outsiders[inside.id] = true end
            end
        end
    end
    return mask, outsiders
end

local function counts_as_stock(i, mask, outsiders)
    if i.flags.whole & mask ~= 0 then return false end
    if outsiders[i.id] then return false end
    local x, y, z = dfhack.items.getPosition(i)
    local tile = x and x >= 0 and dfhack.maps.getTileFlags(x, y, z)
    return tile and not tile.hidden or false
end

-- DF's plural name for a subtype (short swords, jugs), where the type has them.
local function subtype_name(item_type, subtype)
    if subtype < 0 then return '' end
    local def = dfhack.items.getSubtypeDef(item_type, subtype)
    if not def then return '' end
    local ok, name = pcall(function() return def.name_plural end)
    if not ok or not name or name == '' then ok, name = pcall(function() return def.name end) end
    return ok and name and utf(name) or ''
end

-- Items that belong to a creature (eggs, corpses, body parts) are named by it.
local CREATURE_ITEMS = {'CORPSE', 'CORPSEPIECE', 'REMAINS', 'EGG', 'VERMIN', 'PET', 'FISH', 'FISH_RAW'}

local function creature_name(race)
    local raw = race and race >= 0 and df.creature_raw.find(race)
    return raw and utf(raw.name[0]) or ''
end

function stocks()
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local by_creature = {}
    for _, name in ipairs(CREATURE_ITEMS) do
        local t = df.item_type[name]
        if t then by_creature[t] = true end
    end
    local mask, outsiders = stock_filters()
    local groups, order = {}, {}
    for _, i in ipairs(df.global.world.items.other.IN_PLAY) do
        if counts_as_stock(i, mask, outsiders) then
            local t, sub, mt, mi = i:getType(), i:getSubtype(), i:getMaterial(), i:getMaterialIndex()
            local race = -1
            if by_creature[t] then
                local ok, r = pcall(function() return i.race end)
                if ok and r then race = r end
            end
            local key = t .. ':' .. sub .. ':' .. mt .. ':' .. mi .. ':' .. race
            local g = groups[key]
            if not g then
                g = {t=t, sub=sub, mt=mt, mi=mi, race=race, count=0}
                groups[key] = g
                table.insert(order, g)
            end
            g.count = g.count + i:getStackSize()
        end
    end
    local out = {}
    for _, g in ipairs(order) do
        local material = creature_name(g.race)
        if material == '' then
            local mat = dfhack.matinfo.decode(g.mt, g.mi)
            material = mat and utf(mat:toString()) or ''
        end
        table.insert(out, {type=df.item_type[g.t] or '?', what=subtype_name(g.t, g.sub), material=material,
                           count=g.count})
    end
    return to_json{items=out}
end

-- Stockpiles and zones: what a player sees when they click one, and its settings.
-- Categories go through DFHack's own stockpile importer and its library presets;
-- owners through dfhack.buildings.setOwner. A category name only ever picks an
-- entry from this table, so no player text reaches a file path.
local STOCK_CATEGORIES = {
    animals='cat_animals', food='cat_food', furniture='cat_furniture', corpses='cat_corpses',
    refuse='cat_refuse', stone='cat_stone', ammo='cat_ammo', coins='cat_coins',
    bars_blocks='cat_bars_blocks', gems='cat_gems', finished_goods='cat_finished_goods',
    leather='cat_leather', cloth='cat_cloth', wood='cat_wood', weapons='cat_weapons',
    armor='cat_armor', sheet='cat_sheets',
}

local function find_stockpile(id)
    local b = df.building.find(int(id, 'id'))
    if b and df.building_stockpilest:is_instance(b) then return b end
end

local function find_zone(id)
    local b = df.building.find(int(id, 'id'))
    if b and df.building_civzonest:is_instance(b) then return b end
end

local function area(b)
    return (b.x2 - b.x1 + 1) * (b.y2 - b.y1 + 1)
end

-- Only rooms a single dwarf can own, as DF's own zone screen offers.
local function ownable(zone)
    local t = zone.type
    return t == df.civzone_type.Bedroom or t == df.civzone_type.Office
        or t == df.civzone_type.DiningHall or t == df.civzone_type.Tomb
end

-- A workshop's jobs as its Tasks tab lists them, and who may work there. DF
-- allows one assigned worker at most (DFHack's manipulator says so too); the
-- list is kept sorted by unit id.
local BUILDING_JOBS = {ConstructBuilding=true, DestroyBuilding=true}

local function building_job(j)
    return BUILDING_JOBS[df.job_type[j.job_type]] or false
end

local function unit_name(id)
    local u = df.unit.find(id)
    return u and utf(dfhack.units.getReadableName(u)) or '?'
end

local function workshop_info(b)
    local jobs = {}
    for _, j in ipairs(b.jobs) do
        local worker = dfhack.job.getWorker(j)
        table.insert(jobs, {id=j.id, name=utf(dfhack.job.getName(j)), order=j.order_id,
                            worker=worker and utf(dfhack.units.getReadableName(worker)) or '',
                            suspended=j.flags.suspend, ['repeat']=j.flags['repeat'], now=j.flags.do_now,
                            building=building_job(j)})
    end
    local workers = {}
    for _, id in ipairs(b.profile.permitted_workers) do table.insert(workers, {id=id, name=unit_name(id)}) end
    local bound = 0
    for _, o in ipairs(df.global.world.manager_orders.all) do
        if o.workshop_id == b.id then bound = bound + 1 end
    end
    return {found=true, kind='workshop', id=b.id, name=utf(dfhack.buildings.getName(b)), custom=utf(b.name),
            workshop=workshop_kind(b), built=is_built(b), z=b.z, tiles=area(b), jobs=jobs, workers=workers,
            general_orders=b.profile.max_general_orders, orders_here=bound}
end

local function find_job(b, job_id)
    local id = int(job_id, 'job')
    for _, j in ipairs(b.jobs) do
        if j.id == id then return j end
    end
end

-- What a player can do to one job, as from the workshop's Tasks tab. Building
-- and removing the workshop itself are left to building and removing.
local JOB_ACTIONS = {suspend=true, resume=true, now=true, notnow=true, ['repeat']=true, once=true, cancel=true}

function workshop_job(id, job_id, action)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    if not JOB_ACTIONS[action] then error('unknown job action: ' .. tostring(action)) end
    local b = find_workshop(id)
    if not b then return refused('that workshop is gone') end
    local j = find_job(b, job_id)
    if not j then return refused('that task is already gone') end
    if building_job(j) then return refused('that is the workshop being built or taken down') end
    if action == 'cancel' then
        if not dfhack.job.removeJob(j) then return refused('DF refused that') end
    elseif action == 'suspend' then
        -- As DFHack's own suspend does.
        j.flags.suspend = true
        j.flags.working = false
        dfhack.job.removeWorker(j, 0)
    elseif action == 'resume' then
        j.flags.suspend = false
    elseif action == 'now' or action == 'notnow' then
        j.flags.do_now = (action == 'now')
    else
        j.flags['repeat'] = (action == 'repeat')
    end
    return to_json{ok=true}
end

-- One assigned worker or none (`unit` empty).
function workshop_worker(id, unit)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local b = find_workshop(id)
    if not b then return refused('that workshop is gone') end
    local u
    if unit and unit ~= '' then
        u = df.unit.find(int(unit, 'unit'))
        if not u or not dfhack.units.isCitizen(u, true) or not dfhack.units.isAdult(u) then
            return refused('only adult citizens can be assigned')
        end
    end
    b.profile.permitted_workers:resize(0)
    if u then b.profile.permitted_workers:insert('#', u.id) end
    return to_json{ok=true, name=u and utf(dfhack.units.getReadableName(u)) or ''}
end

-- Farm plots: a crop for each season, from what grows there. The rule is DFHack's
-- autofarm's: a plant with seeds, not a tree, that grows in that season, with the
-- BIOME_ flag for the plot's place (subterranean underground, else the region's
-- biome). It matched every crop on a real fort's three plots. Crops without seeds
-- can be chosen; nothing is planted until there are some.
local SEASON_FLAGS = {[0]='SPRING', 'SUMMER', 'AUTUMN', 'WINTER'}

local function find_farm(id)
    local b = df.building.find(int(id, 'id'))
    if b and df.building_farmplotst:is_instance(b) then return b end
end

local function farm_biome_flag(b)
    local tile = dfhack.maps.getTileFlags(b.centerx, b.centery, b.z)
    if tile and tile.subterranean then return 'BIOME_SUBTERRANEAN_WATER' end
    local rx, ry = dfhack.maps.getTileBiomeRgn(b.centerx, b.centery, b.z)
    local biome = rx and df.biome_type[dfhack.maps.getBiomeType(rx, ry)]
    return biome and ('BIOME_' .. biome) or ''
end

local function grows(plant, season, flag)
    if not plant.flags.SEED or plant.flags.TREE or not plant.flags[SEASON_FLAGS[season]] then return false end
    local ok, yes = pcall(function() return plant.flags[flag] end)
    return ok and yes or false
end

local function seed_counts()
    local out = {}
    for _, i in ipairs(df.global.world.items.other.SEEDS) do
        if not (i.flags.foreign or i.flags.trader or i.flags.garbage_collect or i.flags.removed) then
            local p = i:getMaterialIndex()
            out[p] = (out[p] or 0) + i:getStackSize()
        end
    end
    return out
end

-- DFHack's autofarm, when on, picks every plot's crops itself from time to time
-- (both a real fort and the bench had it on), so a choice made here or in DF
-- doesn't last. It has no Lua interface; its status line says whether it's on.
local function autofarm_on()
    local ok, output = pcall(dfhack.run_command_silent, 'autofarm', 'status')
    return ok and type(output) == 'string' and output:find('Autofarm is Active', 1, true) ~= nil
end

local function farm_info(b)
    local flag, seeds, plants = farm_biome_flag(b), seed_counts(), df.global.world.raws.plants.all
    local crops, options = {}, {}
    for season = 0, 3 do
        local p = df.plant_raw.find(b.plant_id[season])
        crops[season + 1] = {id=p and b.plant_id[season] or -1, name=p and utf(p.name_plural) or ''}
        local list = {}
        for _, plant in ipairs(plants) do
            if grows(plant, season, flag) then
                table.insert(list, {id=plant.index, name=utf(plant.name_plural), seeds=seeds[plant.index] or 0})
            end
        end
        table.sort(list, function(a, c)
            if (a.seeds > 0) ~= (c.seeds > 0) then return a.seeds > 0 end
            return a.name < c.name
        end)
        options[season + 1] = list
    end
    return {found=true, kind='farm', id=b.id, name=utf(b.name), tiles=area(b), z=b.z,
            underground=(flag == 'BIOME_SUBTERRANEAN_WATER'), fertilize=b.farm_flags.seasonal_fertilize,
            season=df.global.cur_season, crops=crops, options=options, autofarm=autofarm_on()}
end

-- `plant` is a plant raw index, or -1 to leave the plot fallow that season.
function farm_crop(id, season, plant)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local b = find_farm(id)
    if not b then return refused('that farm plot is gone') end
    local s, n = int(season, 'season'), int(plant, 'plant')
    if s < 0 or s > 3 then error('bad season: ' .. season) end
    if n ~= -1 then
        local p = df.plant_raw.find(n)
        if not p or not grows(p, s, farm_biome_flag(b)) then return refused("that doesn't grow here then") end
    end
    b.plant_id[s] = n
    return to_json{ok=true}
end

function farm_fertilize(id, on)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local b = find_farm(id)
    if not b then return refused('that farm plot is gone') end
    b.farm_flags.seasonal_fertilize = (on == '1')
    return to_json{ok=true}
end

function place_info(id, x, y, z)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local b = df.building.find(int(id, 'id'))
    x, y, z = int(x, 'x'), int(y, 'y'), int(z, 'z')
    if not b or not covers(b, x, y, z) then return to_json{found=false} end
    if df.building_stockpilest:is_instance(b) then
        local categories = {}
        for name in pairs(STOCK_CATEGORIES) do
            if b.settings.flags[name] then table.insert(categories, name) end
        end
        table.sort(categories)
        return to_json{found=true, kind='stockpile', id=b.id, number=b.stockpile_number,
                           name=utf(b.name), tiles=area(b), z=b.z, categories=categories,
                           barrels=b.storage.max_barrels, bins=b.storage.max_bins,
                           wheelbarrows=b.storage.max_wheelbarrows}
    end
    if df.building_civzonest:is_instance(b) then
        local owner = dfhack.buildings.getOwner(b)
        return to_json{found=true, kind='zone', id=b.id, number=b.zone_num, name=utf(b.name),
                           tiles=area(b), z=b.z, zone=df.civzone_type[b.type] or '',
                           active=b.spec_sub_flag.active, ownable=ownable(b),
                           owner=owner and utf(dfhack.units.getReadableName(owner)) or ''}
    end
    if df.building_workshopst:is_instance(b) or df.building_furnacest:is_instance(b) then
        return to_json(workshop_info(b))
    end
    if df.building_farmplotst:is_instance(b) then return to_json(farm_info(b)) end
    return to_json{found=false}
end

function stockpile_category(id, category, on)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local preset = type(category) == 'string' and STOCK_CATEGORIES[category]
    if not preset then error('unknown category: ' .. tostring(category)) end
    local b = find_stockpile(id)
    if not b then return refused('that stockpile is gone') end
    require('plugins.stockpiles').import_settings('library/' .. preset,
        {id=b.id, mode=(on == '1') and 'enable' or 'disable'})
    -- 'disable' clears everything inside the category but leaves its own flag set.
    if on ~= '1' then b.settings.flags[category] = false end
    return to_json{ok=true}
end

function stockpile_containers(id, barrels, bins, wheelbarrows)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local b = find_stockpile(id)
    if not b then return refused('that stockpile is gone') end
    local counts = {int(barrels, 'barrels'), int(bins, 'bins'), int(wheelbarrows, 'wheelbarrows')}
    local most = area(b)
    for _, n in ipairs(counts) do
        if n < 0 or n > most then return refused(('this stockpile fits 0 to %d'):format(most)) end
    end
    b.storage.max_barrels, b.storage.max_bins, b.storage.max_wheelbarrows = counts[1], counts[2], counts[3]
    return to_json{ok=true}
end

function rename_place(id, name)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    if type(name) ~= 'string' or #name > 160 or name:find('%c') then error('bad name') end
    local b = find_stockpile(id) or find_zone(id)
    if not b then return refused('that place is gone') end
    b.name = dfhack.utf2df(name)
    return to_json{ok=true}
end

-- Who a room could go to: anyone who lives here, children included, as in DF.
function zone_candidates()
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local out = {}
    for _, u in ipairs(dfhack.units.getCitizens(true)) do
        table.insert(out, {id=u.id, name=utf(dfhack.units.getReadableName(u)), adult=dfhack.units.isAdult(u)})
    end
    table.sort(out, function(a, b) return a.name < b.name end)
    return to_json(out)
end

-- `unit` empty frees the room.
function zone_owner(id, unit)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local zone = find_zone(id)
    if not zone then return refused('that zone is gone') end
    if not ownable(zone) then return refused('nobody can own this kind of zone') end
    local u
    if unit and unit ~= '' then
        u = df.unit.find(int(unit, 'unit'))
        if not u or not dfhack.units.isCitizen(u, true) then return refused('only citizens can own a room') end
    end
    if not dfhack.buildings.setOwner(zone, u) then return refused('DF refused that') end
    local owner = dfhack.buildings.getOwner(zone)
    if (owner and owner.id or -1) ~= (u and u.id or -1) then return refused('DF refused that') end
    return to_json{ok=true, name=u and utf(dfhack.units.getReadableName(u)) or ''}
end

function zone_active(id, on)
    if not dfhack.isMapLoaded() then error('no fortress loaded') end
    local zone = find_zone(id)
    if not zone then return refused('that zone is gone') end
    zone.spec_sub_flag.active = (on == '1')
    return to_json{ok=true}
end

-- The game clock: year and tick within the year (1200 ticks a day, 28 days
-- a month, 12 months).
function date()
    return to_json{year=df.global.cur_year, tick=df.global.cur_year_tick}
end

return _ENV
