// df-coop browser client. Draws what the hub sends; sends back only which
// level this player is looking at and where their cursor is.
"use strict";

const BLOCK = 16;
const HIDDEN = -1;

const canvas = document.getElementById("map");
const ctx = canvas.getContext("2d");
const $ = (id) => document.getElementById(id);

const st = {
  me: null, map: null, tiles: [], blocks: new Map(), buildings: [], units: [],
  players: [], paused: true, df: "online",
  z: 0, camX: 0, camY: 0,          // camera centre, in tiles
  cell: 16, mouse: null, cursor: null, dirty: true,
  mode: "look", corner: null,      // dig tool and the first corner of its rectangle
  owners: new Map(),               // "x,y,z" -> [player name, colour]
  holders: [],                     // players holding the pause
  catalog: { pages: {}, page_labels: {}, items: [] }, canBuild: false, buildItem: null,
  pageStack: [],                   // the build menu path, e.g. ["MAIN", "WORKSHOPS", "WORKSHOPS_FARMING"]
  orderCatalog: { groups: [], orders: [] }, canOrder: false, orders: [],
  canSquads: false, squads: [], squadOpen: null, squadCandidates: null,
  canNobles: false, nobles: [], nobleOpen: null, nobleCandidates: null,
  reports: [], canInspect: false,
  chat: [], pings: [],             // pings: {x, y, z, color, from, until}
  role: "", perms: new Set(),
  priority: 4,                     // designation priority: 1 first, 7 last (DF's default 4)
};

// toolbar mode -> the permission it needs
const MODE_NEEDS = { dig: "dig", channel: "dig", updown: "dig", down: "dig", up: "dig", ramp: "dig",
                     erase: "dig", remove: "remove", build: "build", ping: "chat" };
const PING_MS = 4000;
const graphics = { mode: "text", sprites: {}, layouts: {}, pages: new Map() };

function loadPage(name) {
  const page = graphics.pages.get(name);
  if (!page) return Promise.resolve(false);
  if (!page.pending) {
    const img = new Image();
    img.src = page.url;
    page.pending = img.decode().then(() => {
      page.img = img;
      st.dirty = true;
      return true;
    }).catch(() => false);
  }
  return page.pending;
}

function spriteReady(token) {
  const entry = graphics.sprites[token];
  if (!entry) return false;
  const page = graphics.pages.get(entry[0]);
  if (!page) return false;
  if (!page.img) { loadPage(entry[0]); return false; }
  return true;
}

async function loadGraphics() {
  const select = $("graphics");
  const option = select.querySelector('[value="steam"]');
  let preferred = "text";
  try { preferred = localStorage.getItem("df-coop-graphics") || "text"; } catch (error) {}
  select.onchange = () => {
    graphics.mode = select.value;
    try { localStorage.setItem("df-coop-graphics", graphics.mode); } catch (error) {}
    sendView(true);
    st.dirty = true;
  };
  try {
    const response = await fetch("/tileset/manifest.json");
    if (!response.ok) throw new Error("Tileset unavailable");
    const manifest = await response.json();
    if (!manifest.available) throw new Error("Tileset unavailable");
    graphics.sprites = manifest.sprites;
    graphics.layouts = manifest.layouts || {};
    for (const [name, page] of Object.entries(manifest.pages)) graphics.pages.set(name, { ...page, img: null });
    const firstPage = manifest.pages.FLOORS ? "FLOORS" : Object.keys(manifest.pages)[0];
    if (!await loadPage(firstPage)) throw new Error("Tileset unavailable");
    option.textContent = "Steam tiles";
    option.disabled = false;
    if (preferred === "steam") {
      select.value = "steam";
      graphics.mode = "steam";
      sendView(true);
    }
    select.title = "Steam terrain, buildings, creatures, vegetation, items and flows; missing graphics use glyphs";
    st.dirty = true;
  } catch (error) {
    option.textContent = "Steam tiles (unavailable)";
    select.title = "Host needs Steam graphics installed in DF_DIR; restart the hub after installing";
  }
}

function sprite(token, x, y, alpha) {
  if (graphics.mode !== "steam" || !spriteReady(token)) return false;
  const entry = graphics.sprites[token];
  const page = graphics.pages.get(entry[0]);
  const width = entry[3] || 1, height = entry[4] || 1;
  ctx.globalAlpha = alpha;
  ctx.drawImage(page.img, entry[1] * page.w, entry[2] * page.h, page.w * width, page.h * height,
                x - (width - 1) * st.cell / 2, y - (height - 1) * st.cell, st.cell * width, st.cell * height);
  return true;
}

function spriteLayers(tokens, x, y, alpha) {
  if (graphics.mode !== "steam" || !tokens || !tokens.length) return false;
  const ready = tokens.map(spriteReady);
  if (!ready.every(Boolean)) return false;
  for (const token of tokens) sprite(token, x, y, alpha);
  return true;
}

function unitSprite(unit, px, py) {
  if (graphics.mode !== "steam" || !unit.g || !unit.g.length) return false;
  if (!unit.g.map(spriteReady).every(Boolean)) return false;
  const entries = unit.g.map(token => graphics.sprites[token]);
  const width = Math.max(...entries.map(entry => entry[3] || 1));
  const height = Math.max(...entries.map(entry => entry[4] || 1));
  ctx.save();
  ctx.beginPath();
  for (let row = unit.y - height + 1; row <= unit.y; row++) {
    for (let col = Math.floor(unit.x - (width - 1) / 2); col < Math.ceil(unit.x + (width + 1) / 2); col++) {
      if (inMap(col, row) && tileAt(col, row, st.z).t !== HIDDEN)
        ctx.rect(px + (col - unit.x) * st.cell, py + (row - unit.y) * st.cell, st.cell, st.cell);
    }
  }
  ctx.clip();
  const drawn = spriteLayers(unit.g, px, py, 1);
  ctx.restore();
  return drawn;
}

function terrainToken(def, mapX, mapY, level) {
  const token = def[4];
  if (graphics.mode !== "steam" || !token || mapX === undefined) return token;
  const wall = token.match(/^(.*_WALL)_N_S_W_E(_1)?$/);
  const ramp = token.match(/^(.*_RAMP_WITH_WALL)_N_S_E_W$/);
  if (!wall && !ramp) return token;
  const sides = [];
  for (const [name, dx, dy] of [["N",0,-1],["S",0,1],["W",-1,0],["E",1,0]]) {
    const neighbor = tileAt(mapX + dx, mapY + dy, level);
    const kind = st.tiles[neighbor.t];
    const solid = neighbor.t === HIDDEN || (kind && ["WALL", "FORTIFICATION"].includes(kind[5]));
    if (wall ? !solid : solid) sides.push(name);
  }
  const candidate = (wall || ramp)[1] + "_" + sides.join("_") + (wall ? wall[2] || "" : "");
  return graphics.sprites[candidate] ? candidate : token;
}

function terrain(def, x, y, alpha, mapX, mapY, level = st.z) {
  if (def[4] && /^(TREE_|SHRUB$|SAPLING$)/.test(def[4])) sprite("DIRT_FLOOR_5", x, y, alpha);
  if (!sprite(terrainToken(def, mapX, mapY, level), x, y, alpha)) glyph(def[0], x, y, def[1], alpha);
}

// DF's 16 text colours, for announcements.
const DF_COLORS = ["#555555", "#4d6bff", "#3dbf3d", "#3dbfbf", "#d24545", "#c050c0", "#b8860b", "#c0c0c0",
                   "#808080", "#8fa6ff", "#7fe07f", "#7fe0e0", "#ff7f7f", "#ff8fff", "#f0e060", "#ffffff"];

const DIG_NAMES = { 1: "dig", 2: "up/down stairs", 3: "channel", 4: "ramp", 5: "down stairs", 6: "up stairs" };
const MODE_COLORS = { dig: "#e0b050", channel: "#5aa0ff", updown: "#c080ff", down: "#c080ff",
                      up: "#c080ff", ramp: "#80d080", erase: "#ff6666" };
// Steam DF's defaults: m opens the dig menu; inside it u channel, t stairs
// (pressing t again cycles the stair kind, as DF's stair tool does), r ramp.
const DIG_FAMILY = new Set(["dig", "channel", "updown", "down", "up", "ramp"]);
const STAIR_CYCLE = ["updown", "down", "up"];

// -- token -------------------------------------------------------------------

function token() {
  const fromUrl = new URLSearchParams(location.search).get("token");
  try {
    if (fromUrl) localStorage.setItem("df-coop-token", fromUrl);
    return fromUrl || localStorage.getItem("df-coop-token");
  } catch (e) {
    return fromUrl;
  }
}

// -- connection ----------------------------------------------------------------

let ws = null;
let retry = 1000;

function connect() {
  const t = token();
  if (!t) { showGate(); return; }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(t)}`);
  ws.onopen = () => { retry = 1000; setConn("connected"); };
  ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
  ws.onclose = (ev) => {
    if (ev.code === 1006 && !st.me) {
      // Refused at the handshake: almost always a bad token.
      setConn("refused (bad token?)", true);
      try { localStorage.removeItem("df-coop-token"); } catch (e) {}
      showGate();
      return;
    }
    setConn("disconnected, retrying…", true);
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 15000);
  };
}

function showGate() {
  $("gate").style.display = "flex";
  $("tokenInput").onkeydown = (e) => {
    if (e.key !== "Enter") return;
    const v = e.target.value.trim();
    if (!v) return;
    location.search = "?token=" + encodeURIComponent(v);
  };
}

function setConn(text, bad) {
  $("conn").textContent = text;
  $("conn").className = bad ? "offline" : "";
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

// -- incoming ----------------------------------------------------------------------

function onMessage(m) {
  switch (m.type) {
    case "hello":
      st.me = m.you; st.map = m.map; st.tiles = m.tiles; st.paused = m.paused;
      st.df = m.df;
      if (m.map.w && !st.camSet) { st.camX = m.view.x; st.camY = m.view.y; st.z = m.view.z; st.camSet = true; }
      if (m.df === "online") st.blocks.clear();  // (re)connected: possibly a different fort
      setConn(`playing as ${m.you.name}`);
      st.role = m.you.role; st.perms = new Set(m.you.perms);
      st.catalog = m.catalog; st.canBuild = m.can_build;
      $("date").textContent = m.date || "";
      renderBuildButton();
      st.orderCatalog = m.order_catalog; st.canOrder = m.can_order; st.orders = m.orders;
      st.canEditOrders = m.can_edit_orders;
      st.canSquads = m.can_squads; st.squads = m.squads; renderSquads();
      st.canNobles = m.can_nobles; st.nobles = m.nobles; renderNobles();
      renderOrderForm(); renderOrders();
      st.canInspect = m.can_inspect; st.reports = m.reports || []; renderReports();
      st.chat = m.chat || []; renderChat();
      applyPerms();
      lastSent = "";
      sendView(true);
      break;
    case "blocks":
      for (const b of m.blocks) st.blocks.set(`${b.x},${b.y},${b.z}`, b);
      break;
    case "buildings": st.buildings = m.buildings; break;
    case "units":
      st.units = m.units;
      if (st.paused !== m.paused) { st.paused = m.paused; renderPause(); }
      break;
    case "players": st.players = m.players; renderPlayers(); break;
    case "status": st.df = m.df; break;
    case "pause": st.paused = m.paused; st.holders = m.holders; renderPause(); break;
    case "owners":
      st.owners = new Map(m.owners.map(([x, y, z, n, c, p]) => [`${x},${y},${z}`, [n, c, p || 4]]));
      break;
    case "date": $("date").textContent = m.date; break;
    case "orders": st.orders = m.orders; renderOrders(); break;
    case "squads": st.squads = m.squads; renderSquads(); break;
    case "nobles": st.nobles = m.nobles; renderNobles(); break;
    case "noble_candidates":
      if (m.ok) { st.nobleCandidates = m.units; renderNobles(); }
      else toast(m.error || "that didn't work", true);
      break;
    case "noble_result":
      if (!m.ok) toast(m.error || "that didn't work", true);
      else if (m.action === "appoint_noble") toast(`${m.name} is now ${m.title}`);
      else toast(`${m.name} stepped down as ${m.title}`);
      break;
    case "squad_candidates":
      if (m.ok) { st.squadCandidates = m.units; renderSquads(); }
      else toast(m.error || "that didn't work", true);
      break;
    case "squad_result":
      if (!m.ok) { toast(m.error || "that didn't work", true); break; }
      if (m.action === "squad_add") toast(`${m.name} joined the squad`);
      else if (m.action === "squad_remove") toast(`${m.name} left the squad`);
      else toast(`Squad is now ${m.name}`);
      if (document.activeElement && $("squads").contains(document.activeElement)) document.activeElement.blur();
      if (m.action !== "rename_squad" && st.squadOpen !== null) send({ type: "squad_candidates" });
      break;
    case "reports": st.reports = st.reports.concat(m.reports).slice(-100); renderReports(); break;
    case "chat":
      st.chat = st.chat.concat([m.entry]).slice(-50);
      renderChat();
      flashChat(m.entry);
      break;
    case "chat_refused": toast(m.error, true); break;
    case "away": showAway(m); break;
    case "denied": toast(m.error, true); break;
    case "role":
      st.role = m.role; st.perms = new Set(m.perms);
      applyPerms();
      toast(`You are now ${m.role}`);
      break;
    case "ping":
      st.pings.push({ x: m.x, y: m.y, z: m.z, color: m.color, from: m.from, until: performance.now() + PING_MS });
      st.dirty = true;
      break;
    case "unit_info":
      if (!m.ok) { toast(m.error || "can't inspect that", true); break; }
      renderUnit(m.unit);
      if (!window.matchMedia("(min-width: 701px)").matches)
        toast(`${m.unit.name} · ${m.unit.job}${m.unit.mood ? " · " + m.unit.mood : ""}`);
      break;
    case "order_result":
      if (m.ok) toast(`Queued ${m.amount} × ${m.label}`);
      else toast(m.error || "that didn't work", true);
      break;
    case "cancel_result":
      if (m.ok) toast(`Cancelled ${m.label}`);
      else toast(m.error || "that didn't work", true);
      break;
    case "edit_result":
      if (m.ok) {
        toast(`Updated ${m.label}`);
        if (document.activeElement && $("orders").contains(document.activeElement)) document.activeElement.blur();
      } else toast(m.error || "that didn't work", true);
      break;
    case "move_result":
      if (!m.ok) toast(m.error || "that didn't work", true);
      break;
    case "build_result": {
      // "Food" reads better as "Food stockpile" once it's out of the palette.
      const it = st.catalog.items.find((i) => i.id === m.item);
      const name = it ? it.label + ({ STOCKPILES: " stockpile", ZONES: " zone" }[it.group] || "") : m.label;
      if (!m.ok) toast(m.error || "that didn't work", true);
      else if (!m.placed) toast(`Couldn't place ${name}` + (m.problems.length ? `: ${m.problems.join("; ")}` : ""), true);
      else toast(`Placed ${name}` + (m.problems.length ? ` (${m.problems.join("; ")})` : ""));
      break;
    }
    case "remove_result":
      if (!m.ok) toast(m.error || "that didn't work", true);
      else if (m.outcome === "nothing") toast("Nothing there to remove", true);
      else if (m.outcome === "queued") toast(`Marked ${m.what} for deconstruction`);
      else if (m.outcome === "already") toast(`${m.what} is already marked for removal`);
      else toast(`Removed ${m.what}`);
      break;
    case "dig_result":
      if (!m.ok) toast(m.error || "that didn't work", true);
      else if (!m.applied) toast("Nothing there can take that; no tiles changed", true);
      else toast(`${m.kind === "erase" ? "Cleared" : "Designated"} ${m.applied} tile${m.applied === 1 ? "" : "s"}` +
                 (m.kind !== "erase" && m.priority && m.priority !== 4 ? ` at priority ${m.priority}` : "") +
                 (m.skipped ? `, ${m.skipped} skipped` : "") + (m.error ? ` (${m.error})` : ""), !!m.error);
      break;
  }
  st.dirty = true;
}

// -- view updates --------------------------------------------------------------------

let lastSent = "";
let sendTimer = null;

function sendView(now) {
  const cx = st.cursor ? st.cursor.x : Math.round(st.camX);
  const cy = st.cursor ? st.cursor.y : Math.round(st.camY);
  const msg = { type: "view", z: st.z, cx, cy, graphics: graphics.mode === "steam" };
  const key = JSON.stringify(msg);
  if (key === lastSent) return;
  if (now) { lastSent = key; send(msg); return; }
  if (sendTimer) return;
  sendTimer = setTimeout(() => { sendTimer = null; sendView(true); }, 100);
}

// -- map lookups ------------------------------------------------------------------------

function blockAt(x, y, z) {
  return st.blocks.get(`${Math.floor(x / BLOCK)},${Math.floor(y / BLOCK)},${z}`);
}

function tileAt(x, y, z) {
  const b = blockAt(x, y, z);
  if (!b) return { t: null };  // never sent: open air
  const i = (y % BLOCK) * BLOCK + (x % BLOCK);
  return { t: b.t[i], w: b.w ? b.w[i] : 0, m: b.m ? b.m[i] : 0, d: b.d ? b.d[i] : 0 };
}

function inMap(x, y) {
  return st.map && x >= 0 && y >= 0 && x < st.map.w && y < st.map.h;
}

// -- drawing --------------------------------------------------------------------------------

const BUILDING_GLYPHS = [
  [/bed/i, "θ"], [/door|hatch/i, "+"], [/table/i, "τ"], [/chair|throne/i, "h"],
  [/coffin|casket/i, "0"], [/statue/i, "Ω"], [/cabinet/i, "π"], [/chest|box/i, "■"],
  [/stockpile/i, "="], [/farm/i, "≡"], [/well/i, "o"], [/bridge/i, "≡"],
  [/wall/i, "#"], [/floor/i, "+"], [/workshop|furnace|smelter|kiln|forge|still|loom|kitchen|butcher|tanner|craft|mason|carpenter|mechanic|jeweler|clothier|leather|farmer|fishery|quern|millstone|ashery|dyer|siege|bowyer|soap|screw|kennel/i, "☼"],
  [/trap|cage|lever/i, "^"], [/bookcase/i, "\u2261"], [/window/i, "\u256a"],
];

function isArea(b) {
  return /civzone/i.test(b.kind || "");
}

function buildingGlyph(name) {
  for (const [re, g] of BUILDING_GLYPHS) if (re.test(name)) return g;
  return "◘";
}

function drawBuilding(building, view) {
  const size = st.cell;
  const layout = graphics.mode === "steam" ? graphics.layouts[building.g] : null;
  const width = layout ? layout.w : building.x2 - building.x1 + 1;
  const height = layout ? layout.h : building.y2 - building.y1 + 1;
  const top = layout ? building.y2 - height + 1 : building.y1;
  for (let row = Math.max(0, view.y0 - top); row < Math.min(height, view.y0 + view.rows - top); row++) {
    for (let col = Math.max(0, view.x0 - building.x1); col < Math.min(width, view.x0 + view.cols - building.x1); col++) {
      const x = building.x1 + col, y = top + row;
      if (!inMap(x, y) || tileAt(x, y, st.z).t === HIDDEN) continue;
      const px = (x - view.x0) * size, py = (y - view.y0) * size;
      const tokens = layout ? layout.tiles[`${col},${row}`] : [building.g];
      if (spriteLayers(tokens, px, py, building.g === "STOCKPILE_FLOOR" ? 0.35 : 1)) continue;
      if (y < building.y1) continue;
      ctx.globalAlpha = 1; ctx.fillStyle = "#000"; ctx.fillRect(px, py, size, size);
      glyph(buildingGlyph(building.name), px, py, "#c8a060", 1);
    }
  }
}

function drawExtras(view, field) {
  for (let by = Math.floor(view.y0 / BLOCK); by <= Math.floor((view.y0 + view.rows) / BLOCK); by++) {
    for (let bx = Math.floor(view.x0 / BLOCK); bx <= Math.floor((view.x0 + view.cols) / BLOCK); bx++) {
      const block = st.blocks.get(`${bx},${by},${st.z}`);
      if (!block || !block[field]) continue;
      for (const [dx, dy, token, alpha] of block[field]) {
        const x = bx * BLOCK + dx, y = by * BLOCK + dy;
        if (tileAt(x, y, st.z).t === HIDDEN) continue;
        const px = (x - view.x0) * st.cell, py = (y - view.y0) * st.cell;
        if (px < -st.cell || py < -st.cell || px > view.w || py > view.h) continue;
        if (!sprite(token, px, py, alpha === undefined ? 1 : alpha))
          glyph(field === "o" ? "•" : "~", px, py, field === "o" ? "#c8b890" : "#a0b090", alpha || 0.7);
      }
    }
  }
}

function resize() {
  const r = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(r.width * dpr);
  canvas.height = Math.round(r.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.imageSmoothingEnabled = false;
  st.dirty = true;
}

function viewport() {
  const r = canvas.getBoundingClientRect();
  const cols = Math.ceil(r.width / st.cell) + 1;
  const rows = Math.ceil(r.height / st.cell) + 1;
  const fx = st.camX - cols / 2, fy = st.camY - rows / 2;
  const x0 = Math.floor(fx), y0 = Math.floor(fy);
  // ox/oy: how far into the first tile the view starts, in px, so a drag
  // moves the map smoothly instead of a whole tile at a time.
  return { x0, y0, cols, rows, w: r.width, h: r.height,
           ox: (fx - x0) * st.cell, oy: (fy - y0) * st.cell };
}

function glyph(ch, x, y, color, alpha) {
  ctx.globalAlpha = alpha;
  ctx.fillStyle = color;
  if (ch === "\u2588") {
    // A font's full block is taller than a tile and bleeds into its
    // neighbours at large zoom; draw the solid wall as the tile itself.
    ctx.fillRect(x, y, st.cell, st.cell);
    return;
  }
  ctx.fillText(ch, x + st.cell / 2, y + st.cell / 2 + 1);
}

function designation(tile, x, y, px, py, size) {
  if (!tile.d) return;
  const owner = st.owners.get(`${x},${y},${st.z}`);
  ctx.globalAlpha = tile.t === HIDDEN ? 0.45 : 0.3;
  ctx.fillStyle = owner ? owner[1] : "#e0b050";
  ctx.fillRect(px, py, size, size);
  if (owner && owner[2] !== 4 && size >= 12) {
    ctx.globalAlpha = 1; ctx.fillStyle = "#fff";
    ctx.font = `${Math.round(size * 0.45)}px "DejaVu Sans Mono", monospace`;
    ctx.fillText(String(owner[2]), px + size * 0.78, py + size * 0.26);
    ctx.font = `${Math.round(size * 0.95)}px "DejaVu Sans Mono", Menlo, monospace`;
  }
}

function draw() {
  requestAnimationFrame(draw);
  if (st.pings.length) {
    const now = performance.now();
    st.pings = st.pings.filter((p) => p.until > now);
    st.dirty = true;
  }
  if (!st.dirty) return;
  if (!st.map || !st.map.w) { renderStatus(); st.dirty = false; return; }
  st.dirty = false;
  const v = viewport();
  const c = st.cell;
  ctx.globalAlpha = 1;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, v.w, v.h);
  ctx.save();
  ctx.translate(-v.ox, -v.oy);
  ctx.font = `${Math.round(c * 0.95)}px "DejaVu Sans Mono", Menlo, monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";

  for (let row = 0; row < v.rows; row++) {
    for (let col = 0; col < v.cols; col++) {
      const x = v.x0 + col, y = v.y0 + row;
      const px = col * c, py = row * c;
      if (!inMap(x, y)) { ctx.globalAlpha = 1; ctx.fillStyle = "#08080a"; ctx.fillRect(px, py, c, c); continue; }
      const tile = tileAt(x, y, st.z);
      if (tile.t === HIDDEN) { designation(tile, x, y, px, py, c); continue; }
      const def = tile.t === null ? null : st.tiles[tile.t];
      if (def === null || def === undefined || def[3]) {
        // open: show the level below, dimmed
        const below = tileAt(x, y, st.z - 1);
        if (below.t !== null && below.t !== HIDDEN) {
          const bd = st.tiles[below.t];
          if (bd && !bd[3]) terrain(bd, px, py, 0.35, x, y, st.z - 1);
          if (below.w && !sprite("WATER", px, py, 0.4)) glyph("≈", px, py, "#3070ff", 0.4);
          if (below.m && !sprite("MAGMA_1", px, py, 0.5)) glyph("≈", px, py, "#ff3300", 0.5);
        }
        if (def && def[0] !== " ") terrain(def, px, py, 1, x, y);
      } else {
        terrain(def, px, py, 1, x, y);
      }
      if (tile.m || tile.w) {
        sprite(tile.m ? "MAGMA_1" : "WATER", px, py, 1);
        glyph(String(tile.m || tile.w), px, py, tile.m ? "#ff4400" : "#4a8cff", 1);
      }
      designation(tile, x, y, px, py, c);
    }
  }

  // buildings on this level
  for (const b of st.buildings) {
    if (st.z < b.z1 || st.z > b.z2) continue;
    if (isArea(b)) {
      // zones are areas, not objects: a faint wash, drawn under everything else on it
      ctx.globalAlpha = 0.10; ctx.fillStyle = "#7fb0ff";
      ctx.fillRect((b.x1 - v.x0) * c, (b.y1 - v.y0) * c, (b.x2 - b.x1 + 1) * c, (b.y2 - b.y1 + 1) * c);
      continue;
    }
    drawBuilding(b, v);
  }

  drawExtras(v, "o");

  // units
  for (const u of st.units) {
    if (u.z !== st.z) continue;
    const px = (u.x - v.x0) * c, py = (u.y - v.y0) * c;
    if (px < -c * 4 || py < -c * 4 || px > v.w + c * 4 || py > v.h + c * 4) continue;
    if (tileAt(u.x, u.y, st.z).t === HIDDEN) continue;
    if (unitSprite(u, px, py)) continue;
    ctx.globalAlpha = 1; ctx.fillStyle = "#000"; ctx.fillRect(px, py, c, c);
    glyph(u.n ? "☺" : "•", px, py, u.c, 1);
  }

  drawExtras(v, "f");

  // pings: a pulsing ring with the pinger's name
  for (const p of st.pings) {
    if (p.z !== st.z) continue;
    const left = (p.until - performance.now()) / PING_MS;
    const cx = (p.x - v.x0) * c + c / 2, cy = (p.y - v.y0) * c + c / 2;
    const r = c * (0.8 + 1.6 * ((1 - left) * 3 % 1));
    ctx.globalAlpha = Math.max(0, left);
    ctx.strokeStyle = p.color; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.strokeRect((p.x - v.x0) * c + 1, (p.y - v.y0) * c + 1, c - 2, c - 2);
  }
  // other players' cursors
  ctx.globalAlpha = 1;
  ctx.font = `11px "DejaVu Sans Mono", monospace`;
  ctx.textAlign = "left";
  for (const p of st.players) {
    if (st.me && p.id === st.me.id) continue;
    if (p.z !== st.z) continue;
    const px = (p.cx - v.x0) * c, py = (p.cy - v.y0) * c;
    ctx.strokeStyle = p.color; ctx.lineWidth = 2;
    ctx.strokeRect(px + 1, py + 1, c - 2, c - 2);
    ctx.fillStyle = p.color;
    ctx.fillText(p.name, px + c + 3, py + c / 2);
  }
  // what a click would remove
  if (st.mode === "remove" && st.cursor) {
    const b = buildingAt(st.cursor.x, st.cursor.y, st.z);
    if (b) {
      ctx.globalAlpha = 1; ctx.strokeStyle = "#ff5555"; ctx.lineWidth = 2;
      ctx.strokeRect((b.x1 - v.x0) * c + 1, (b.y1 - v.y0) * c + 1, (b.x2 - b.x1 + 1) * c - 2, (b.y2 - b.y1 + 1) * c - 2);
    }
  }
  // the footprint of the item being placed
  if (st.mode === "build" && st.buildItem && !st.buildItem.area && st.cursor) {
    // quickfort centres a building on the clicked tile: it starts floor(size/2) back.
    const w = st.buildItem.w, h = st.buildItem.h;
    const px = (st.cursor.x - Math.floor(w / 2) - v.x0) * c, py = (st.cursor.y - Math.floor(h / 2) - v.y0) * c;
    ctx.globalAlpha = 0.22; ctx.fillStyle = "#7fd0ff"; ctx.fillRect(px, py, w * c, h * c);
    ctx.globalAlpha = 1; ctx.strokeStyle = "#7fd0ff"; ctx.lineWidth = 2;
    ctx.strokeRect(px + 1, py + 1, w * c - 2, h * c - 2);
  }
  // the rectangle being selected
  if (st.corner && st.cursor && st.corner.z === st.z) {
    const r = selRect();
    const px = (r.x1 - v.x0) * c, py = (r.y1 - v.y0) * c;
    const w = (r.x2 - r.x1 + 1) * c, h = (r.y2 - r.y1 + 1) * c;
    const col = st.mode === "build" ? "#7fd0ff" : (MODE_COLORS[st.mode] || "#fff");
    ctx.globalAlpha = 0.18; ctx.fillStyle = col; ctx.fillRect(px, py, w, h);
    ctx.globalAlpha = 1; ctx.strokeStyle = col; ctx.lineWidth = 2;
    ctx.strokeRect(px + 1, py + 1, w - 2, h - 2);
  }
  // my cursor
  if (st.cursor && st.me) {
    const px = (st.cursor.x - v.x0) * c, py = (st.cursor.y - v.y0) * c;
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 1;
    ctx.strokeRect(px + 0.5, py + 0.5, c - 1, c - 1);
  }

  ctx.restore();
  renderStatus();
}

function renderStatus() {
  $("zlabel").textContent = `z ${st.z}`;
  $("clock").textContent = st.df === "offline" ? "waiting for the fortress…"
    : !st.paused ? "running"
    : st.holders.length ? `paused by ${st.holders.join(", ")}` : "paused";
  $("clock").className = st.df === "offline" ? "offline" : "";
  if (!st.cursor) { $("hover").textContent = "—"; $("tapinfo").classList.remove("show"); return; }
  const { x, y } = st.cursor;
  const lines = [`${x}, ${y}, ${st.z}`];
  const t = tileAt(x, y, st.z);
  if (t.t === HIDDEN) lines.push("unexplored");
  else if (t.t === null) lines.push("open air");
  else if (st.tiles[t.t]) lines.push(st.tiles[t.t][2]);
  if (t.w) lines.push(`water ${t.w}/7`);
  if (t.m) lines.push(`magma ${t.m}/7`);
  if (t.d) {
    const owner = st.owners.get(`${x},${y},${st.z}`);
    lines.push(`designated: ${DIG_NAMES[t.d] || "dig"}${owner ? `, priority ${owner[2]} (by ${owner[0]})` : ""}`);
  }
  if (st.mode === "remove") {
    const b = buildingAt(x, y, st.z);
    lines.push(b ? `click to remove ${b.name}` : "nothing to remove here");
  }
  if (st.corner && st.corner.z === st.z) {
    const r = selRect();
    const what = st.mode === "build" && st.buildItem ? st.buildItem.label : st.mode;
    lines.push(`selecting ${r.x2 - r.x1 + 1}×${r.y2 - r.y1 + 1} for ${what}`);
  }
  for (const b of st.buildings)
    if (x >= b.x1 && x <= b.x2 && y >= b.y1 && y <= b.y2 && st.z >= b.z1 && st.z <= b.z2) lines.push(b.name);
  for (const u of st.units)
    if (u.x === x && u.y === y && u.z === st.z) lines.push(u.n || `creature #${u.i}`);
  for (const p of st.players)
    if (p.cx === x && p.cy === y && p.z === st.z && (!st.me || p.id !== st.me.id)) lines.push(`[${p.name}]`);
  $("hover").textContent = lines.join("\n");
  $("tapinfo").textContent = lines.join(" · ");
  $("tapinfo").classList.add("show");
}

function renderPlayers() {
  const box = $("players");
  box.textContent = "";
  for (const p of st.players) {
    const row = document.createElement("div");
    row.className = "player";
    const sw = document.createElement("span");
    sw.className = "swatch"; sw.style.background = p.color;
    const name = document.createElement("span");
    name.textContent = p.name + (st.me && p.id === st.me.id ? " (you)" : "");
    const role = document.createElement("span");
    role.className = "role"; role.textContent = p.role || "";
    const where = document.createElement("span");
    where.className = "where"; where.textContent = `z${p.z}`;
    row.append(sw, name, role, where);
    row.onclick = () => { st.z = p.z; st.camX = p.cx; st.camY = p.cy; st.dirty = true; sendView(true); };
    box.append(row);
  }
}

// -- input --------------------------------------------------------------------------------------

function changeZ(dz) {
  if (!st.map) return;
  st.z = Math.max(0, Math.min(st.map.d - 1, st.z + dz));
  st.dirty = true;
  sendView(true);
}

function openPalette(root) {
  if (!can("build")) { toast(`Your role (${st.role}) can't build`, true); return; }
  if (!st.canBuild) { toast("Building isn't set up on the host yet", true); return; }
  st.pageStack = [root];
  renderPalette();
  $("palette").classList.add("open");
}

function openSide(target) {
  // On phones the panel slides out; on desktop it's always there.
  if (window.matchMedia("(max-width: 700px)").matches) $("side").classList.add("open");
  target.scrollIntoView({ block: "start" });
}

// Keys follow Steam DF's defaults (data/init/interface.txt); see the help panel.
window.addEventListener("keydown", (e) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) {
    // Typing in a field never triggers map keys; Esc leaves the field, so
    // the next key goes back to the map (as backing out of a DF menu does).
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const k = e.key;

  if (k === "Escape") {
    if (!$("away").hidden) $("away").hidden = true;
    else if (st.corner) cancelCorner();
    else if ($("palette").classList.contains("open")) {
      // Like DF: Esc goes up one page, and closes the menu from the top.
      if (st.pageStack.length > 1) { st.pageStack.pop(); renderPalette(); } else closePalette();
    }
    else if ($("side").classList.contains("open")) $("side").classList.remove("open");
    else setMode("look");
    return;
  }

  // The build palette takes letters first, like DF's build menu: a category
  // key switches tabs, an item key picks that item (repeats cycle).
  // The build menu takes letters first, as DF's does: b o f p is
  // Build > Workshops > Farming > Farm plot. Keys are unique on each page.
  if ($("palette").classList.contains("open")) {
    const page = st.pageStack[st.pageStack.length - 1];
    const entry = (st.catalog.pages[page] || []).find((x) => x.key === k);
    if (entry) {
      e.preventDefault();
      if (entry.page) { st.pageStack.push(entry.page); renderPalette(); }
      else pickItem(st.catalog.items.find((i) => i.id === entry.item));
      return;
    }
  }

  const fast = e.shiftKey ? 10 : 1;
  if (k === "ArrowLeft" || k === "a" || k === "A") st.camX -= fast;
  else if (k === "ArrowRight" || k === "d" || k === "D") st.camX += fast;
  else if (k === "ArrowUp" || k === "w" || k === "W") st.camY -= fast;
  else if (k === "ArrowDown" || k === "s" || k === "S") st.camY += fast;
  else if (k === "e" || k === "<") { changeZ(+1); return; }
  else if (k === "c" || k === ">") { changeZ(-1); return; }
  else if (k === "[") { st.cell = Math.min(40, st.cell + 2); }
  else if (k === "]") { st.cell = Math.max(8, st.cell - 2); }
  else if (k === " ") {
    e.preventDefault();
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    if (!$("pausebtn").disabled) $("pausebtn").click();
    return;
  }
  else if (k === "m") { closePalette(); setMode("dig"); return; }
  else if (DIG_FAMILY.has(st.mode) && (k === "p" || k === "P")) {
    // DF's dig menu: p raises the priority (towards 1), P lowers it.
    setPriority(st.priority + (k === "p" ? -1 : 1));
    return;
  }
  else if (DIG_FAMILY.has(st.mode) && (k === "u" || k === "r" || k === "t")) {
    if (k === "u") setMode("channel");
    else if (k === "r") setMode("ramp");
    else setMode(STAIR_CYCLE.includes(st.mode) ? STAIR_CYCLE[(STAIR_CYCLE.indexOf(st.mode) + 1) % 3] : "updown");
    return;
  }
  else if (k === "x") { closePalette(); setMode("erase"); return; }
  else if (k === "X") { closePalette(); setMode("remove"); return; }
  else if (k === "b") { openPalette("MAIN"); return; }
  else if (k === "p") { openPalette("STOCKPILES"); return; }
  else if (k === "z") { openPalette("ZONES"); return; }
  else if (k === "o") {
    if (!can("order") || !st.canOrder) { toast(`Work orders aren't available to you`, true); return; }
    $("orderform").hidden = false;
    openSide($("orderform"));
    $("orderpick").focus();
    return;
  }
  else if (k === "q") { openSide($("squads")); return; }
  else if (k === "n") { openSide($("nobles")); return; }
  else if (k === "N") { openSide($("reports")); return; }
  else if (k === "g") {
    if (st.cursor && can("chat")) send({ type: "ping", x: st.cursor.x, y: st.cursor.y, z: st.z });
    return;
  }
  else if (k === "Enter") {
    e.preventDefault();
    // DF's Enter selects at the cursor; with no tool active it opens chat.
    if (st.mode !== "look" && st.cursor) pickCorner();
    else $("chatinput").focus();
    return;
  }
  else return;
  e.preventDefault();
  st.dirty = true;
  updateCursor();
});

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  if (e.ctrlKey) {
    st.cell = Math.max(8, Math.min(32, st.cell + (e.deltaY < 0 ? 2 : -2)));
    st.dirty = true;
    updateCursor();
  } else {
    changeZ(e.deltaY < 0 ? +1 : -1);
  }
}, { passive: false });

// Pointer events cover mouse, pen and touch. One pointer drags the map; a
// press that doesn't move is a tap/click and places the cursor; two pointers
// pinch-zoom. A mouse with no button down just hovers.
const pointers = new Map();
let drag = null;
let pinch = null;
const TAP_SLOP = 6;  // px a press may wander and still count as a tap

function localPos(e) {
  const r = canvas.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

function spread() {
  const [a, b] = [...pointers.values()];
  return Math.hypot(a.x - b.x, a.y - b.y);
}

canvas.addEventListener("pointerdown", (e) => {
  canvas.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, localPos(e));
  if (pointers.size === 1) {
    const p = localPos(e);
    drag = { x: p.x, y: p.y, camX: st.camX, camY: st.camY, moved: false };
  } else if (pointers.size === 2) {
    drag = null;
    pinch = { d: spread() || 1, cell: st.cell };
  }
});

canvas.addEventListener("pointermove", (e) => {
  const p = localPos(e);
  if (pointers.has(e.pointerId)) pointers.set(e.pointerId, p);
  if (pinch && pointers.size === 2) {
    st.cell = Math.max(8, Math.min(40, Math.round(pinch.cell * spread() / pinch.d)));
    st.dirty = true;
    return;
  }
  if (drag) {
    const dx = p.x - drag.x, dy = p.y - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) > TAP_SLOP) {
      drag.moved = true;
      canvas.classList.add("dragging");
    }
    if (drag.moved) {
      st.camX = drag.camX - dx / st.cell;
      st.camY = drag.camY - dy / st.cell;
      st.dirty = true;
    }
  }
  if (e.pointerType === "mouse") { st.mouse = p; updateCursor(); }
});

function endPointer(e, cancelled) {
  const wasTap = drag && !drag.moved && pointers.size === 1 && !cancelled;
  pointers.delete(e.pointerId);
  canvas.classList.remove("dragging");
  if (pointers.size < 2) pinch = null;
  if (pointers.size === 0) drag = null;
  if (wasTap && e.button === 2) { cancelCorner(); return; }
  if (wasTap) { st.mouse = localPos(e); updateCursor(); pickCorner(); }
}
canvas.addEventListener("contextmenu", (e) => e.preventDefault());
canvas.addEventListener("pointerup", (e) => endPointer(e, false));
canvas.addEventListener("pointercancel", (e) => endPointer(e, true));
canvas.addEventListener("pointerleave", (e) => {
  if (e.pointerType === "mouse" && !pointers.size) { st.mouse = null; st.cursor = null; st.dirty = true; }
});

$("zup").onclick = () => changeZ(+1);
$("zdown").onclick = () => changeZ(-1);
$("menu").onclick = () => $("side").classList.toggle("open");
// Picking a player from the slide-out panel should also close it.
$("players").addEventListener("click", () => $("side").classList.remove("open"));

function updateCursor() {
  if (!st.mouse) return;
  const v = viewport();
  const x = v.x0 + Math.floor((st.mouse.x + v.ox) / st.cell);
  const y = v.y0 + Math.floor((st.mouse.y + v.oy) / st.cell);
  if (!st.cursor || st.cursor.x !== x || st.cursor.y !== y) {
    st.cursor = { x, y };
    st.dirty = true;
    sendView(false);
  }
}

// -- digging and pausing --------------------------------------------------------------------------

// The building a remove click would take: the hub's choice is a building or
// stockpile first, then a zone, so mirror that for the highlight.
function buildingAt(x, y, z) {
  let zone = null;
  for (const b of st.buildings) {
    if (x < b.x1 || x > b.x2 || y < b.y1 || y > b.y2 || z < b.z1 || z > b.z2) continue;
    if (isArea(b)) zone = b; else return b;
  }
  return zone;
}

function modeHint() {
  if (st.mode === "look") return "";
  if (st.mode === "remove") return "click a building, stockpile or zone";
  if (st.mode === "ping") return "click to point everyone there";
  if (st.mode === "build" && st.buildItem && !st.buildItem.area) return `click to place ${st.buildItem.label}`;
  return "click the first corner";
}

function setMode(mode) {
  if (MODE_NEEDS[mode] && !can(MODE_NEEDS[mode])) {
    toast(`Your role (${st.role}) can't do that`, true);
    mode = "look";
  }
  st.mode = mode;
  st.corner = null;
  if (mode !== "build") st.buildItem = null;
  for (const btn of document.querySelectorAll("#toolbar [data-mode]"))
    btn.classList.toggle("on", btn.dataset.mode === mode);
  renderBuildButton();
  $("prio").hidden = !DIG_FAMILY.has(mode);
  $("hint").textContent = modeHint();
  st.dirty = true;
}

function setPriority(p) {
  st.priority = Math.max(1, Math.min(7, p));
  $("prioval").textContent = st.priority;
}
$("priominus").onclick = () => setPriority(st.priority - 1);
$("prioplus").onclick = () => setPriority(st.priority + 1);

function cancelCorner() {
  if (!st.corner) return;
  st.corner = null;
  $("hint").textContent = modeHint();
  st.dirty = true;
}

function selRect() {
  const a = st.corner, b = st.cursor;
  return { x1: Math.min(a.x, b.x), y1: Math.min(a.y, b.y), x2: Math.max(a.x, b.x), y2: Math.max(a.y, b.y) };
}

function pickCorner() {
  if (!st.cursor) return;
  if (st.mode === "look") {
    const u = st.units.find((u) => u.x === st.cursor.x && u.y === st.cursor.y && u.z === st.z);
    if (u && st.canInspect) send({ type: "inspect", unit: u.i });
    return;
  }
  if (st.mode === "build" && st.buildItem && !st.buildItem.area) {
    send({ type: "build", item: st.buildItem.id, x1: st.cursor.x, y1: st.cursor.y, z: st.z });
    return;
  }
  if (st.mode === "ping") {
    send({ type: "ping", x: st.cursor.x, y: st.cursor.y, z: st.z });
    return;
  }
  if (st.mode === "remove") {
    // Send the id of what's outlined, so overlapping zones can't be mixed up.
    const b = buildingAt(st.cursor.x, st.cursor.y, st.z);
    if (!b) { toast("Nothing there to remove", true); return; }
    send({ type: "remove", x: st.cursor.x, y: st.cursor.y, z: st.z, id: b.i });
    return;
  }
  if (!st.corner || st.corner.z !== st.z) {
    st.corner = { x: st.cursor.x, y: st.cursor.y, z: st.z };
    $("hint").textContent = "now the opposite corner (Esc cancels)";
  } else {
    const c = st.corner;
    if (st.mode === "build")
      send({ type: "build", item: st.buildItem.id, x1: c.x, y1: c.y, x2: st.cursor.x, y2: st.cursor.y, z: c.z });
    else
      send({ type: "dig", kind: st.mode, x1: c.x, y1: c.y, x2: st.cursor.x, y2: st.cursor.y, z: c.z,
             priority: st.priority });
    st.corner = null;
    $("hint").textContent = modeHint();
  }
  st.dirty = true;
}

for (const btn of document.querySelectorAll("#toolbar [data-mode]"))
  btn.onclick = () => { closePalette(); setMode(btn.dataset.mode); };

// -- while you were away --

function showAway(m) {
  $("awayspan").textContent = m.since && m.now && m.since !== m.now ? `${m.since} → ${m.now}` : (m.now || "");
  const body = $("awaybody");
  body.textContent = "";
  const section = (title, items) => {
    const h = document.createElement("h4");
    h.textContent = title;
    const ul = document.createElement("ul");
    for (const it of items) ul.append(it);
    body.append(h, ul);
  };
  if (m.actions.length) {
    section("Other players", m.actions.map((line) => {
      const li = document.createElement("li");
      li.textContent = line;
      return li;
    }));
  }
  if (m.reports.length) {
    section(`Announcements (${m.reports.length}${m.reports.length >= 50 ? ", the latest" : ""})`,
      m.reports.slice().reverse().map((r) => {
        const li = document.createElement("li");
        li.textContent = r.text;
        li.style.color = DF_COLORS[r.color] || "#c0c0c0";
        if (r.z !== undefined) {
          li.style.cursor = "pointer";
          li.title = "Go there";
          li.onclick = () => {
            st.z = r.z; st.camX = r.x; st.camY = r.y; st.dirty = true; sendView(true);
            $("away").hidden = true;
          };
        }
        return li;
      }));
  }
  if (!m.actions.length && !m.reports.length) {
    const p = document.createElement("p");
    p.textContent = "Quiet times: nobody else did anything and DF had nothing to announce.";
    body.append(p);
  }
  $("away").hidden = false;
}

$("awayclose").onclick = () => { $("away").hidden = true; };

// -- chat --

function chatLine(e) {
  const row = document.createElement("div");
  row.className = "chatline";
  const who = document.createElement("b");
  who.style.color = e.color;
  who.textContent = e.from;
  row.append(who);
  if (e.kind === "ping") {
    row.append(` pinged z${e.z} (${e.x}, ${e.y})`);
    row.classList.add("go");
    row.title = "Go there";
    row.onclick = () => {
      st.z = e.z; st.camX = e.x; st.camY = e.y; st.dirty = true; sendView(true);
      $("side").classList.remove("open");
    };
  } else {
    row.append(": " + e.text);
  }
  return row;
}

function renderChat() {
  const box = $("chatlog");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  box.textContent = "";
  for (const e of st.chat) box.append(chatLine(e));
  if (atBottom) box.scrollTop = box.scrollHeight;
}

let flashTimer = null;
function flashChat(e) {
  const el = $("chatflash");
  el.textContent = "";
  el.append(chatLine(e));
  el.classList.add("show");
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => el.classList.remove("show"), 6000);
}

$("chatinput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const text = e.target.value.trim();
    if (text) send({ type: "chat", text });
    e.target.value = "";
    e.preventDefault();
  } else if (e.key === "Escape") {
    e.target.blur();
  }
  e.stopPropagation();   // typing here never triggers map shortcuts
});

// -- creatures and announcements --

function renderUnit(u) {
  const box = $("unitinfo");
  box.textContent = "";
  const line = (text, cls) => {
    const d = document.createElement("div");
    d.textContent = text;
    if (cls) d.className = cls;
    box.append(d);
  };
  line(u.name, "uname");
  if (u.profession && !u.name.endsWith(u.profession)) line(u.profession, "dim");
  line(`Doing: ${u.job}`);
  if (u.mood) line(`Mood: ${u.mood}`);
  if (u.age >= 0) line(`Age: ${u.age}`, "dim");
  if (u.squad) line(`Squad: ${u.squad}`);
  if (u.needs.length) line(`Missing: ${u.needs.map((n) => n.toLowerCase()).join(", ")}`, "dim");
}

function renderReports() {
  const box = $("reports");
  box.textContent = "";
  if (!st.reports.length) {
    const note = document.createElement("span");
    note.className = "count";
    note.textContent = st.canInspect ? "nothing yet" : "not available on this host yet";
    box.append(note);
    return;
  }
  for (const r of st.reports.slice().reverse()) {
    const row = document.createElement("div");
    row.className = "report" + (r.z !== undefined ? " go" : "");
    row.style.color = DF_COLORS[r.color] || "#c0c0c0";
    row.textContent = r.text;
    const when = document.createElement("small");
    when.textContent = r.date;
    row.append(when);
    if (r.z !== undefined) {
      row.title = "Go there";
      row.onclick = () => {
        st.z = r.z; st.camX = r.x; st.camY = r.y; st.dirty = true; sendView(true);
        $("side").classList.remove("open");
      };
    }
    box.append(row);
  }
}

// -- work orders --

const ORDER_STATUS = {
  active: "Being worked on now",
  validated: "Checked by the manager, waiting for its conditions or a free workshop",
  waiting: "Waiting for the manager to check it",
};

function freqLabel(id) {
  const f = (st.orderCatalog.freqs || []).find((x) => x.id === id);
  return f ? f.label : id;
}

function orderEditor(o) {
  const box = document.createElement("div");
  box.className = "orderedit";
  const status = document.createElement("div");
  status.className = "count";
  status.textContent = ORDER_STATUS[o.status] || "";
  box.append(status);
  if (o.conds.length) {
    const list = document.createElement("ul");
    for (const text of o.conds) {
      const li = document.createElement("li");
      li.textContent = text;
      list.append(li);
    }
    const head = document.createElement("div");
    head.className = "count";
    head.textContent = "Conditions:";
    box.append(head, list);
  }
  if (!can("order") || !st.canEditOrders) {
    const repeat = document.createElement("div");
    repeat.className = "count";
    repeat.textContent = `Repeats: ${freqLabel(o.freq)}`;
    box.append(repeat);
    return box;
  }
  const amount = document.createElement("input");
  amount.type = "number";
  amount.min = 1;
  amount.max = 999;
  amount.value = o.total;
  amount.title = "How many more to make; saving a new amount restarts the count";
  amount.className = "editamount";
  const freq = document.createElement("select");
  freq.className = "editfreq";
  for (const f of st.orderCatalog.freqs || []) {
    const opt = document.createElement("option");
    opt.value = f.id;
    opt.textContent = f.label;
    freq.append(opt);
  }
  freq.value = o.freq;
  const save = document.createElement("button");
  save.textContent = "Save";
  save.className = "editsave";
  save.onclick = () => {
    const msg = { type: "edit_order", order: o.id };
    if (amount.value !== String(o.total)) {
      const n = parseInt(amount.value, 10);
      if (!(n >= 1 && n <= 999)) { toast("The amount must be 1 to 999", true); return; }
      msg.amount = n;
    }
    if (freq.value !== o.freq) msg.freq = freq.value;
    if (msg.amount === undefined && msg.freq === undefined) { toast("Nothing changed"); return; }
    send(msg);
  };
  amount.onkeydown = (e) => { if (e.key === "Enter") save.click(); };
  const row = document.createElement("div");
  row.className = "row";
  row.append(amount, freq, save);
  const moves = document.createElement("div");
  moves.className = "row";
  for (const [to, text] of [["top", "To top"], ["bottom", "To bottom"]]) {
    const b = document.createElement("button");
    b.textContent = text;
    b.dataset.to = to;
    b.onclick = () => send({ type: "move_order", order: o.id, to });
    moves.append(b);
  }
  box.append(row, moves);
  return box;
}

function renderOrders() {
  const box = $("orders");
  // Don't pull a text box out from under someone typing in it; orders change
  // every time a dwarf picks up a job. Blurring it catches up.
  if (box.contains(document.activeElement) && ["INPUT", "SELECT"].includes(document.activeElement.tagName)) {
    st.ordersStale = true;
    return;
  }
  st.ordersStale = false;
  const scroll = box.scrollTop;
  box.textContent = "";
  const filterBox = $("orderfilter");
  filterBox.hidden = !st.canOrder || st.orders.length < 8;
  if (!st.canOrder) {
    const note = document.createElement("span");
    note.className = "count";
    note.textContent = "not available on this host yet";
    box.append(note);
    return;
  }
  if (!st.orders.length) {
    const note = document.createElement("span");
    note.className = "count";
    note.textContent = "no orders";
    box.append(note);
  }
  const filter = filterBox.hidden ? "" : filterBox.value.trim().toLowerCase();
  const editable = can("order") && st.canEditOrders;
  const orderText = (o) => o.label + (o.material ? ` (${o.material})` : "");
  const shown = st.orders.filter((o) => !filter || orderText(o).toLowerCase().includes(filter));
  if (st.orders.length && !shown.length) {
    const note = document.createElement("span");
    note.className = "count";
    note.textContent = "no matching orders";
    box.append(note);
  }
  for (const o of shown) {
    const row = document.createElement("div");
    row.className = "order";
    row.dataset.order = o.id;
    row.classList.toggle("open", st.orderOpen === o.id);
    const dot = document.createElement("span");
    dot.className = `dot ${o.status}`;
    dot.title = ORDER_STATUS[o.status] || "";
    const name = document.createElement("button");
    name.className = "name";
    name.textContent = orderText(o);
    name.title = name.textContent + (o.freq && o.freq !== "OneTime" ? ` · repeats ${o.freq.toLowerCase()}` : "");
    name.onclick = () => {
      st.orderOpen = st.orderOpen === o.id ? null : o.id;
      renderOrders();
    };
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${o.left}/${o.total}`;
    const x = document.createElement("button");
    x.textContent = "×";
    x.title = "Cancel this order";
    x.hidden = !can("order");
    x.onclick = () => {
      if (confirm(`Cancel the order for ${name.textContent}?`)) send({ type: "cancel_order", order: o.id });
    };
    row.append(dot, name, count);
    // A filtered list hides what an order would move past, so arrows wait for
    // the whole queue; To top / To bottom still work.
    if (editable && !filter) {
      for (const [to, text, tip] of [["up", "▲", "Move up the queue"], ["down", "▼", "Move down the queue"]]) {
        const b = document.createElement("button");
        b.textContent = text;
        b.title = tip;
        b.dataset.to = to;
        b.onclick = () => send({ type: "move_order", order: o.id, to });
        row.append(b);
      }
    }
    row.append(x);
    box.append(row);
    if (st.orderOpen === o.id) box.append(orderEditor(o));
  }
  box.scrollTop = scroll;
}

$("orders").addEventListener("focusout", () => {
  setTimeout(() => { if (st.ordersStale) renderOrders(); }, 0);
});
$("orderfilter").oninput = renderOrders;

function renderOrderForm() {
  const pick = $("orderpick");
  pick.textContent = "";
  for (const g of st.orderCatalog.groups) {
    const og = document.createElement("optgroup");
    og.label = g;
    for (const o of st.orderCatalog.orders.filter((x) => x.group === g)) {
      const opt = document.createElement("option");
      opt.value = o.id;
      opt.textContent = o.label;
      og.append(opt);
    }
    pick.append(og);
  }
  renderOrderMaterials();
  const freq = $("orderfreq");
  freq.textContent = "";
  for (const f of st.orderCatalog.freqs || []) {
    const opt = document.createElement("option");
    opt.value = f.id;
    opt.textContent = f.label;
    freq.append(opt);
  }
  freq.disabled = !st.canEditOrders;   // an older in-game module only queues one-time orders
  $("neworder").disabled = !st.canOrder || !can("order");
  if ($("neworder").disabled) $("orderform").hidden = true;
}

function renderOrderMaterials() {
  const o = st.orderCatalog.orders.find((x) => x.id === $("orderpick").value);
  const mat = $("ordermat");
  mat.textContent = "";
  for (const m of (o && o.materials.length ? o.materials : [""])) {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m || "any material";
    mat.append(opt);
  }
  mat.disabled = !o || o.materials.length < 2;
}

$("orderpick").onchange = renderOrderMaterials;
$("neworder").onclick = () => { $("orderform").hidden = !$("orderform").hidden; };
$("orderqueue").onclick = () => {
  const amount = parseInt($("orderamt").value, 10);
  if (!(amount >= 1 && amount <= 999)) { toast("The amount must be 1 to 999", true); return; }
  const msg = { type: "order", id: $("orderpick").value, amount };
  if ($("ordermat").value) msg.material = $("ordermat").value;
  if ($("orderfreq").value && $("orderfreq").value !== "OneTime") msg.freq = $("orderfreq").value;
  send(msg);
};

// -- squads --

function squadEditor(q) {
  const box = document.createElement("div");
  box.className = "squadedit";
  const manage = can("military");
  const line = document.createElement("div");
  line.className = "count";
  line.textContent = q.routine ? `Routine: ${q.routine}` : "";
  box.append(line);
  if (q.orders.length) {
    const orders = document.createElement("div");
    orders.className = "count";
    orders.textContent = `Orders: ${q.orders.join(", ").toLowerCase().replaceAll("_", " ")}`;
    box.append(orders);
  }
  for (const m of q.members) {
    const row = document.createElement("div");
    row.className = "member";
    row.dataset.unit = m.unit;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = (m.leader ? "★ " : "") + m.name + (m.away ? " (away)" : "");
    who.title = who.textContent;
    row.append(who);
    if (manage && !m.leader && !m.away && m.unit !== -1) {
      const x = document.createElement("button");
      x.textContent = "×";
      x.title = "Remove from the squad";
      x.onclick = () => {
        if (confirm(`Remove ${m.name} from ${q.name}?`)) send({ type: "squad_remove", squad: q.id, unit: m.unit });
      };
      row.append(x);
    }
    box.append(row);
  }
  if (!manage) return box;
  if (q.members.length < q.slots) {
    const row = document.createElement("div");
    row.className = "row";
    const pick = document.createElement("select");
    pick.className = "squadpick";
    pick.setAttribute("aria-label", "Dwarf to add");
    const units = st.squadCandidates;
    for (const u of units || []) {
      const opt = document.createElement("option");
      opt.value = u.id;
      opt.textContent = u.name;
      pick.append(opt);
    }
    pick.disabled = !units || !units.length;
    const add = document.createElement("button");
    add.className = "squadadd";
    add.textContent = !units ? "Loading" : units.length ? "Add" : "Nobody free";
    add.disabled = pick.disabled;
    add.onclick = () => send({ type: "squad_add", squad: q.id, unit: parseInt(pick.value, 10) });
    row.append(pick, add);
    box.append(row);
  }
  const rename = document.createElement("div");
  rename.className = "row";
  const input = document.createElement("input");
  input.className = "squadname";
  input.maxLength = 40;
  input.placeholder = "Custom name";
  input.setAttribute("aria-label", "Custom squad name");
  input.value = q.alias;
  const save = document.createElement("button");
  save.className = "squadsave";
  save.textContent = "Rename";
  save.onclick = () => send({ type: "rename_squad", squad: q.id, name: input.value });
  input.onkeydown = (e) => { if (e.key === "Enter") save.click(); };
  rename.append(input, save);
  box.append(rename);
  return box;
}

function renderSquads() {
  const box = $("squads");
  if (box.contains(document.activeElement) && ["INPUT", "SELECT"].includes(document.activeElement.tagName)) {
    st.squadsStale = true;
    return;
  }
  st.squadsStale = false;
  const scroll = box.scrollTop;
  box.textContent = "";
  if (!st.canSquads || !st.squads.length) {
    const note = document.createElement("span");
    note.className = "count";
    note.textContent = st.canSquads ? "no squads" : "not available on this host yet";
    box.append(note);
    return;
  }
  for (const q of st.squads) {
    const row = document.createElement("div");
    row.className = "squad";
    row.classList.toggle("open", st.squadOpen === q.id);
    row.dataset.squad = q.id;
    const name = document.createElement("button");
    name.className = "name";
    name.textContent = q.name;
    name.title = q.name;
    name.onclick = () => {
      st.squadOpen = st.squadOpen === q.id ? null : q.id;
      if (st.squadOpen !== null && can("military")) {
        st.squadCandidates = null;
        send({ type: "squad_candidates" });
      }
      renderSquads();
    };
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${q.members.length}/${q.slots}`;
    row.append(name, count);
    box.append(row);
    if (st.squadOpen === q.id) box.append(squadEditor(q));
  }
  box.scrollTop = scroll;
}

$("squads").addEventListener("focusout", () => {
  setTimeout(() => { if (st.squadsStale) renderSquads(); }, 0);
});

// -- nobles --

const NEED_UNITS = { office: "office", bedroom: "bedroom", dining: "dining room", tomb: "tomb",
                     boxes: "boxes", cabinets: "cabinets", racks: "racks", stands: "armor stands" };

function nobleNeeds(n) {
  return Object.keys(NEED_UNITS).filter((k) => n.needs[k] > 0).map((k) => `${NEED_UNITS[k]} ${n.needs[k]}`).join(", ");
}

function nobleEditor(n) {
  const box = document.createElement("div");
  box.className = "nobleedit";
  const line = (text) => {
    const d = document.createElement("div");
    d.className = "count";
    d.textContent = text;
    box.append(d);
  };
  const holder = document.createElement("div");
  holder.textContent = n.holder || "Nobody holds this post";
  box.append(holder);
  if (n.duties.length) line(`Duties: ${n.duties.join(", ").toLowerCase().replaceAll("_", " ")}`);
  const needs = nobleNeeds(n);
  if (needs) line(`Expects: ${needs}`);
  if (n.locked) { line(`Can't be changed here: ${n.locked}.`); return box; }
  if (!can("nobles")) return box;
  const row = document.createElement("div");
  row.className = "row";
  const pick = document.createElement("select");
  pick.className = "noblepick";
  pick.setAttribute("aria-label", "Dwarf to appoint");
  const units = st.nobleCandidates;
  for (const u of units || []) {
    const opt = document.createElement("option");
    opt.value = u.id;
    opt.textContent = u.name;
    pick.append(opt);
  }
  pick.disabled = !units || !units.length;
  const appoint = document.createElement("button");
  appoint.className = "nobleappoint";
  appoint.textContent = !units ? "Loading" : n.holder ? "Replace" : "Appoint";
  appoint.disabled = pick.disabled;
  appoint.onclick = () => {
    const who = pick.options[pick.selectedIndex].textContent;
    const ask = n.holder ? `Replace ${n.holder} as ${n.title} with ${who}?` : `Appoint ${who} as ${n.title}?`;
    if (confirm(`${ask} This goes in the fort's history.`)) {
      send({ type: "appoint_noble", post: n.id, unit: parseInt(pick.value, 10) });
    }
  };
  row.append(pick, appoint);
  box.append(row);
  if (n.holder) {
    const vacateRow = document.createElement("div");
    vacateRow.className = "row";
    const vacate = document.createElement("button");
    vacate.className = "noblevacate";
    vacate.textContent = "Remove from post";
    vacate.onclick = () => {
      if (confirm(`Remove ${n.holder} as ${n.title}? This goes in the fort's history.`)) {
        send({ type: "vacate_noble", post: n.id });
      }
    };
    vacateRow.append(vacate);
    box.append(vacateRow);
  }
  return box;
}

function renderNobles() {
  const box = $("nobles");
  if (box.contains(document.activeElement) && document.activeElement.tagName === "SELECT") {
    st.noblesStale = true;
    return;
  }
  st.noblesStale = false;
  const scroll = box.scrollTop;
  box.textContent = "";
  if (!st.canNobles || !st.nobles.length) {
    const note = document.createElement("span");
    note.className = "count";
    note.textContent = st.canNobles ? "no posts" : "not available on this host yet";
    box.append(note);
    return;
  }
  for (const n of st.nobles) {
    const row = document.createElement("div");
    row.className = "noble";
    row.dataset.post = n.id;
    const name = document.createElement("button");
    name.className = "name";
    name.textContent = n.title;
    name.onclick = () => {
      st.nobleOpen = st.nobleOpen === n.id ? null : n.id;
      if (st.nobleOpen !== null && can("nobles") && !st.nobleCandidates) send({ type: "noble_candidates" });
      renderNobles();
    };
    const who = document.createElement("span");
    who.className = n.holder ? "who" : "who vacant";
    who.textContent = n.holder ? n.holder.split(",")[0] : "vacant";
    who.title = n.holder || "Nobody holds this post";
    row.append(name, who);
    box.append(row);
    if (st.nobleOpen === n.id) box.append(nobleEditor(n));
  }
  box.scrollTop = scroll;
}

$("nobles").addEventListener("focusout", () => {
  setTimeout(() => { if (st.noblesStale) renderNobles(); }, 0);
});

// -- the build palette --

function renderBuildButton() {
  const btn = $("buildbtn");
  btn.disabled = !st.canBuild || !can("build");
  btn.title = !can("build") ? `your role (${st.role}) can't build`
    : st.canBuild ? "Place furniture, workshops, stockpiles and zones (B)"
    : "Building isn't set up on the host yet (hub.py install-lua)";
  btn.classList.toggle("on", st.mode === "build");
  btn.firstChild.textContent = st.mode === "build" && st.buildItem ? `${st.buildItem.label} ▾` : "Build ▾";
}

// The build menu is DF's: pages of sub-pages and buildings, each with DF's key.
function renderPalette() {
  const crumbs = document.querySelector("#palette .tabs");
  const list = document.querySelector("#palette .items");
  const labels = st.catalog.page_labels || {};
  crumbs.textContent = "";
  st.pageStack.forEach((page, i) => {
    if (i) crumbs.append(" › ");
    const b = document.createElement("button");
    b.textContent = labels[page] || page;
    b.className = i === st.pageStack.length - 1 ? "on" : "";
    b.onclick = () => { st.pageStack = st.pageStack.slice(0, i + 1); renderPalette(); };
    crumbs.append(b);
  });
  list.textContent = "";
  const page = st.pageStack[st.pageStack.length - 1];
  for (const entry of st.catalog.pages[page] || []) {
    const b = document.createElement("button");
    if (entry.page) {
      b.textContent = entry.label + " ▸";
      b.onclick = () => { st.pageStack.push(entry.page); renderPalette(); };
    } else {
      const it = st.catalog.items.find((i) => i.id === entry.item);
      if (!it) continue;
      b.textContent = it.label + " ";
      const note = document.createElement("small");
      note.textContent = it.area ? "area" : (it.w > 1 || it.h > 1 ? `${it.w}×${it.h}` : "");
      b.append(note);
      b.disabled = !it.available;
      if (!it.available) b.title = "quickfort can't place this, so df-coop can't either (yet)";
      b.onclick = () => pickItem(it);
    }
    if (entry.key) { const kb = document.createElement("kbd"); kb.textContent = entry.key; b.append(kb); }
    list.append(b);
  }
}

function pickItem(it) {
  if (!it.available) { toast(`${it.label} can't be placed from here yet`, true); return; }
  st.buildItem = it;
  closePalette();
  setMode("build");
}

function closePalette() { $("palette").classList.remove("open"); }

$("buildbtn").onclick = () => {
  if ($("palette").classList.contains("open")) { closePalette(); return; }
  openPalette("MAIN");
};

function iAmHolding() {
  return st.me && st.holders.includes(st.me.name);
}

function can(perm) { return st.perms.has(perm); }

function applyPerms() {
  const why = (perm) => `your role (${st.role}) can't do this`;
  for (const btn of document.querySelectorAll("#toolbar [data-mode]")) {
    const need = MODE_NEEDS[btn.dataset.mode];
    btn.disabled = !!need && !can(need);
    if (btn.disabled) btn.title = why(need);
  }
  if (MODE_NEEDS[st.mode] && !can(MODE_NEEDS[st.mode])) { closePalette(); setMode("look"); }
  $("chatinput").disabled = !can("chat");
  $("chatinput").placeholder = can("chat") ? "Say something (Enter)" : `your role (${st.role}) can't chat`;
  $("role").textContent = st.role ? `· ${st.role}` : "";
  renderBuildButton();
  renderOrderForm();
  renderOrders();
  renderSquads();
  renderNobles();
  renderPause();
}

function setLabel(btn, text) {
  // Change a button's text but keep its <kbd> hint.
  btn.firstChild.textContent = text;
}

function renderPause() {
  const btn = $("pausebtn");
  btn.disabled = false;
  if (!st.paused) { setLabel(btn, "⏸ Pause"); btn.title = "Pause the game for everyone (Space)"; }
  else if (iAmHolding()) {
    const others = st.holders.filter((n) => n !== st.me.name);
    setLabel(btn, "▶ Ready");
    btn.title = others.length ? `Resumes once ${others.join(", ")} ${others.length > 1 ? "are" : "is"} ready too` : "Resume the game";
  } else if (st.holders.length) {
    setLabel(btn, `Waiting for ${st.holders.join(", ")}`);
    btn.title = "The game resumes when they're ready";
    btn.disabled = true;
  } else { setLabel(btn, "▶ Resume"); btn.title = "Resume the game (Space)"; }
  // Without the pause permission you can still release a hold you already have.
  if (!can("pause") && !iAmHolding()) { btn.disabled = true; btn.title = `your role (${st.role}) can't pause`; }
  st.dirty = true;
}

$("pausebtn").onclick = () => {
  if (!st.paused) send({ type: "pause", value: true });
  else if (iAmHolding() || !st.holders.length) send({ type: "pause", value: false });
};

let toastTimer = null;
function toast(text, bad) {
  const el = $("toast");
  el.textContent = text;
  el.className = "show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = bad ? "bad" : ""; }, 3500);
}

window.addEventListener("resize", resize);
resize();
loadGraphics();
requestAnimationFrame(draw);
connect();
