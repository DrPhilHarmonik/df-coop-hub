"""Optional browser checks; uses synthetic assets and never connects to DF."""

import base64
from pathlib import Path

import pytest


@pytest.fixture
def browser_page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed")
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page()
        page.add_init_script("""
            localStorage.setItem('df-coop-token', 'test');
            window.WebSocket = class {
              static OPEN = 1;
              readyState = 1;
              send() {}
            };
        """)
        yield page
        browser.close()


def serve_client(page, *, available=True, broken_image=False, manifest=None):
    root = Path(__file__).parent / "static"
    pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aLl8AAAAASUVORK5CYII=")
    requests = []

    def route_request(route):
        path = route.request.url.split(".test", 1)[1]
        requests.append(path)
        if path == "/tileset/manifest.json":
            route.fulfill(json=manifest or {"available": available,
                                "pages": {"FLOORS": {"url": "/tileset/page/FLOORS", "w": 1, "h": 1}},
                                "sprites": {"STONE_FLOOR_5": ["FLOORS", 0, 0]}})
        elif path.startswith("/tileset/page/"):
            route.fulfill(status=404 if broken_image else 200, content_type="image/png",
                          body=b"not found" if broken_image else pixel)
        else:
            target = root / ("index.html" if path == "/" else path.lstrip("/"))
            route.fulfill(path=target)

    page.route("http://df-coop.test/**", route_request)
    page.goto("http://df-coop.test/")
    return requests


@pytest.mark.parametrize("width", [1280, 390])
def test_switch_graphics_and_restore_browser_preference(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    page.wait_for_function("!document.querySelector('#graphics option[value=steam]').disabled")
    page.select_option("#graphics", "steam")
    assert page.evaluate("graphics.mode") == "steam"
    assert page.evaluate("localStorage.getItem('df-coop-graphics')") == "steam"
    assert page.evaluate("""() => {
      ctx.clearRect(0, 0, 40, 40);
      const drawn = sprite('STONE_FLOOR_5', 0, 0, 1);
      return drawn && ctx.getImageData(4, 4, 1, 1).data[3] > 0;
    }""")
    assert page.evaluate("sprite('unsupported', 0, 0, 1)") is False
    page.reload()
    page.wait_for_function("graphics.mode === 'steam'")
    page.select_option("#graphics", "text")
    assert page.evaluate("sprite('STONE_FLOOR_5', 0, 0, 1)") is False
    assert not errors


@pytest.mark.parametrize("available,broken_image", [(False, False), (True, True)])
def test_unavailable_assets_keep_text_mode(browser_page, available, broken_image):
    page = browser_page
    page.add_init_script("localStorage.setItem('df-coop-graphics', 'steam')")
    serve_client(page, available=available, broken_image=broken_image)
    page.wait_for_function("document.querySelector('#graphics option[value=steam]').textContent.includes('unavailable')")
    assert page.locator("#graphics").input_value() == "text"
    assert page.evaluate("graphics.mode") == "text"


def test_steam_tiles_preserve_hidden_tiles_and_designation_overlay(browser_page):
    page = browser_page
    serve_client(page)
    page.wait_for_function("!document.querySelector('#graphics option[value=steam]').disabled")
    page.select_option("#graphics", "steam")
    page.evaluate("""() => {
      st.map = { w: 16, h: 16, d: 3 };
      st.z = 1; st.camX = 8; st.camY = 8;
      st.tiles = [ ['.', '#aaa', 'floor', false, 'STONE_FLOOR_5'] ];
      const tiles = Array(256).fill(HIDDEN);
      tiles[8 * 16 + 8] = 0;
      const dig = Array(256).fill(0);
      dig[8 * 16 + 8] = 1;
      dig[8 * 16 + 7] = 1;
      st.blocks.set('0,0,1', { x: 0, y: 0, z: 1, t: tiles, d: dig });
      st.owners.set('8,8,1', ['Alice', '#ff0000', 4]);
      window.spriteDraws = 0;
      const originalDrawImage = ctx.drawImage.bind(ctx);
      ctx.drawImage = (...args) => { window.spriteDraws++; originalDrawImage(...args); };
      st.dirty = true;
    }""")
    page.wait_for_function("!st.dirty")
    result = page.evaluate("""() => {
      const view = viewport();
      const sample = (x, y) => Array.from(ctx.getImageData(
        Math.round((x - view.x0 + 0.5) * st.cell - view.ox),
        Math.round((y - view.y0 + 0.5) * st.cell - view.oy), 1, 1).data);
      return { draws: window.spriteDraws, visible: sample(8, 8),
               hidden: sample(6, 8), hiddenDig: sample(7, 8) };
    }""")
    assert result["draws"] == 1
    assert result["visible"][0] > result["visible"][1]
    assert result["hidden"][:3] == [0, 0, 0]
    assert result["hiddenDig"][0] > 0


def test_building_creature_and_item_sprites_load_on_demand_and_respect_fog(browser_page):
    page = browser_page
    requests = serve_client(page, manifest={"available": True,
        "pages": {name: {"url": f"/tileset/page/{name}", "w": 1, "h": 1} for name in ("FLOORS", "OBJECTS")},
        "sprites": {"floor": ["FLOORS", 0, 0], "bed": ["OBJECTS", 0, 0], "cat": ["OBJECTS", 0, 0],
                    "item": ["OBJECTS", 0, 0], "flow": ["OBJECTS", 0, 0]},
        "layouts": {"workshop": {"w": 2, "h": 2, "tiles": {f"{x},{y}": ["bed"] for x in range(2) for y in range(2)}}}})
    page.wait_for_function("!document.querySelector('#graphics option[value=steam]').disabled")
    assert "/tileset/page/OBJECTS" not in requests
    page.select_option("#graphics", "steam")
    page.evaluate("sprite('bed', 0, 0, 1)")
    page.wait_for_function("graphics.pages.get('OBJECTS').img !== null")
    assert requests.count("/tileset/page/OBJECTS") == 1
    result = page.evaluate("""() => {
      st.map = {w: 16, h: 16, d: 3}; st.z = 1; st.cell = 16;
      const tiles = Array(256).fill(HIDDEN);
      for (const [x,y] of [[1,1],[2,1],[1,2],[3,3],[4,4]]) tiles[y*16+x] = 0;
      st.blocks.set('0,0,1', {t: tiles, o: [[4,4,'item'],[5,5,'item']], f: [[4,4,'flow',0.5],[5,5,'flow',0.5]]});
      const view = {x0: 0,y0: 0,cols: 16,rows: 16,w:256,h:256};
      let draws = 0;
      const original = ctx.drawImage.bind(ctx);
      ctx.drawImage = (...args) => { draws++; original(...args); };
      ctx.clearRect(0,0,256,256);
      drawBuilding({g:'workshop',name:'Workshop',x1:1,y1:1,x2:2,y2:2}, view);
      const buildingDraws = draws;
      const hidden = Array.from(ctx.getImageData(40,40,1,1).data);
      const creature = spriteLayers(['cat'],48,48,1);
      const incomplete = spriteLayers(['cat','unavailable'],48,48,1);
      drawExtras(view,'o'); drawExtras(view,'f');
      return {buildingDraws,draws,hidden,creature,incomplete};
    }""")
    assert result["buildingDraws"] == 3
    assert result["draws"] == 6
    assert result["hidden"][3] == 0
    assert result["creature"] is True and result["incomplete"] is False
    page.select_option("#graphics", "text")
    assert page.evaluate("spriteLayers(['cat'], 0, 0, 1)") is False


def test_large_creatures_are_clipped_to_explored_tiles(browser_page):
    page = browser_page
    serve_client(page)
    page.wait_for_function("!document.querySelector('#graphics option[value=steam]').disabled")
    page.select_option("#graphics", "steam")
    result = page.evaluate("""() => {
      const atlas=document.createElement('canvas'); atlas.width=2; atlas.height=2;
      const paint=atlas.getContext('2d'); paint.fillStyle='white'; paint.fillRect(0,0,2,2);
      graphics.pages.get('FLOORS').img=atlas;
      graphics.sprites.large=['FLOORS',0,0,2,2];
      st.map={w:16,h:16,d:3}; st.z=1; st.cell=16;
      const tiles=Array(256).fill(HIDDEN); tiles[8*16+8]=0;
      st.blocks.set('0,0,1',{t:tiles});
      ctx.clearRect(0,0,256,256);
      const drawn=unitSprite({x:8,y:8,g:['large']},128,128);
      const alpha=(x,y)=>ctx.getImageData(x,y,1,1).data[3];
      return {drawn,visible:alpha(136,136),hiddenLeft:alpha(124,136),hiddenAbove:alpha(136,124)};
    }""")
    assert result == {"drawn": True, "visible": 255, "hiddenLeft": 0, "hiddenAbove": 0}


def test_wall_variants_use_visible_neighbors_and_keep_hidden_neighbors_solid(browser_page):
    page = browser_page
    serve_client(page)
    page.wait_for_function("!document.querySelector('#graphics option[value=steam]').disabled")
    page.select_option("#graphics", "steam")
    result = page.evaluate("""() => {
      const wall=['#','#999','wall',false,'SMOOTHED_STONE_WALL_N_S_W_E','WALL'];
      st.tiles=[wall,['.','#999','floor',false,'STONE_FLOOR_5','FLOOR']];
      const tiles=Array(256).fill(0); tiles[7*16+8]=1; tiles[8*16+7]=HIDDEN;
      st.blocks.set('0,0,1',{t:tiles});
      graphics.sprites.SMOOTHED_STONE_WALL_N=['FLOORS',0,0];
      return terrainToken(wall,8,8,1);
    }""")
    assert result == "SMOOTHED_STONE_WALL_N"


ORDER_FIXTURE = """() => {
  st.perms = new Set(['order']);
  st.canOrder = true; st.canEditOrders = true;
  st.orderCatalog = {groups: [], orders: [], freqs: [{id: 'OneTime', label: 'One time'}, {id: 'Daily', label: 'Daily'}]};
  st.orders = Array.from({length: 9}, (_, i) => ({id: 100 + i, label: i === 3 ? 'Beds' : 'Barrels ' + i, material: '',
    left: 2, total: 5, freq: 'OneTime', status: 'waiting', conds: i === 3 ? ['at least 10 wood'] : []}));
  window.sent = [];
  send = (m) => window.sent.push(m);
  renderOrders();
  openSide($("orders"));
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_order_panel_edits_moves_and_filters(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    page.evaluate(ORDER_FIXTURE)
    assert page.locator("#orders .order").count() == 9
    page.evaluate("document.querySelector('.order[data-order=\\'103\\'] button[data-to=up]').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "move_order", "order": 103, "to": "up"}

    page.evaluate("document.querySelector('.order[data-order=\\'103\\'] .name').click()")
    assert "at least 10 wood" in page.locator(".orderedit").inner_text()
    page.evaluate("""() => {
      const e = document.querySelector('.orderedit');
      e.querySelector('.editamount').value = '42';
      e.querySelector('.editfreq').value = 'Daily';
      e.querySelector('.editsave').click();
    }""")
    assert page.evaluate("window.sent.pop()") == {"type": "edit_order", "order": 103, "amount": 42, "freq": "Daily"}
    page.evaluate("document.querySelector('.orderedit button[data-to=top]').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "move_order", "order": 103, "to": "top"}

    # a queue update doesn't wipe a half-typed amount; leaving the box catches up
    page.evaluate("""() => {
      const box = document.querySelector('.editamount');
      box.focus(); box.value = '77';
      st.orders[0].left = 1; renderOrders();
    }""")
    assert page.evaluate("document.querySelector('.editamount').value") == "77"
    page.evaluate("document.querySelector('.editamount').blur()")
    page.wait_for_function("document.querySelector('.editamount').value === '5'")

    page.evaluate("$('orderfilter').value = 'bed'; renderOrders()")
    assert page.locator("#orders .order").count() == 1
    assert page.locator("#orders .order button[data-to=up]").count() == 0

    # read-only for someone whose role can't order
    page.evaluate("$('orderfilter').value = ''; st.perms = new Set(); renderOrders()")
    assert page.locator(".orderedit .editsave").count() == 0
    assert page.locator("#orders button[data-to]").count() == 0
    assert errors == []
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


SQUAD_FIXTURE = """() => {
  st.perms = new Set(['military']);
  st.canSquads = true;
  st.squads = [{id: 2, name: 'The Axes', alias: '', slots: 3, routine: 'Off duty', orders: ['KILL_LIST'], members: [
    {slot: 0, leader: true, unit: 1, name: 'Urist, captain', away: false},
    {slot: 1, leader: false, unit: 5, name: 'Bomrek, axedwarf', away: false}]},
    {id: 3, name: 'The Shields', alias: '', slots: 2, routine: '', orders: [], members: [
    {slot: 0, leader: true, unit: 6, name: 'Zon, captain', away: false},
    {slot: 1, leader: false, unit: 7, name: 'Lokum, away', away: true}]}];
  window.sent = [];
  send = (m) => window.sent.push(m);
  renderSquads();
  openSide($("squads"));
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_squad_panel_adds_removes_and_renames(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    serve_client(page)
    page.evaluate(SQUAD_FIXTURE)
    assert page.locator("#squads .squad").count() == 2
    assert page.locator(".squad[data-squad='2'] .count").inner_text() == "2/3"

    page.evaluate("document.querySelector('.squad[data-squad=\\'2\\'] .name').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "squad_candidates"}
    assert page.locator(".squadadd").inner_text() == "Loading"
    assert "Off duty" in page.locator(".squadedit").inner_text()
    assert "kill list" in page.locator(".squadedit").inner_text()
    page.evaluate("st.squadCandidates = [{id: 8, name: 'Kadol, miner'}]; renderSquads()")
    page.evaluate("document.querySelector('.squadadd').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "squad_add", "squad": 2, "unit": 8}

    # long DF names wrap inside the sidebar rather than widening it
    assert page.evaluate("""() => {
      st.squads[0].members[1].name = "`Sharpstone' Lalciltumwistandmanymorewordsrunningtogether Dellmachine, Swordsmaster";
      st.squadCandidates = [{id: 8, name: "`Breezyhand' Malstraltestri Squashedflower the very long, Stonecutter"}];
      renderSquads();
      const box = $('squads');
      return box.scrollWidth <= box.clientWidth;
    }""")

    # the leader has no remove button; a member does
    assert page.locator(".member[data-unit='1'] button").count() == 0
    page.evaluate("document.querySelector('.member[data-unit=\\'5\\'] button').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "squad_remove", "squad": 2, "unit": 5}

    page.evaluate("""() => {
      document.querySelector('.squadname').value = 'Iron Axes';
      document.querySelector('.squadsave').click();
    }""")
    assert page.evaluate("window.sent.pop()") == {"type": "rename_squad", "squad": 2, "name": "Iron Axes"}

    # someone away can't be removed, and a full squad offers no add
    page.evaluate("document.querySelector('.squad[data-squad=\\'3\\'] .name').click()")
    assert page.locator(".member[data-unit='7'] button").count() == 0
    assert page.locator(".squadadd").count() == 0

    # a squad update doesn't wipe a half-typed name
    page.evaluate("""() => {
      const box = document.querySelector('.squadname');
      box.focus(); box.value = 'half';
      st.squads = [...st.squads]; renderSquads();
    }""")
    assert page.evaluate("document.querySelector('.squadname').value") == "half"
    page.evaluate("document.querySelector('.squadname').blur()")

    # read-only without the permission
    page.evaluate("st.perms = new Set(); renderSquads()")
    assert page.locator(".squadname").count() == 0 and page.locator(".member button").count() == 0
    assert errors == []
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


NOBLE_FIXTURE = """() => {
  st.perms = new Set(['nobles']);
  st.canNobles = true;
  st.nobles = [
    {id: 17, code: 'MAYOR', title: 'lord', holder: 'Zon, lady', locked: 'this title passes to an heir', duties: ['MEET_WORKERS'], needs: {office: 500}},
    {id: 16, code: 'MANAGER', title: 'manager', holder: 'Urist, manager', locked: '', duties: ['MANAGE_PRODUCTION'], needs: {office: 1}},
    {id: 15, code: 'DOCTOR', title: 'doctor', holder: '', locked: '', duties: [], needs: {}}];
  window.sent = [];
  send = (m) => window.sent.push(m);
  renderNobles();
  openSide($("nobles"));
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_noble_panel_appoints_replaces_and_explains_locks(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    questions = []
    page.on("dialog", lambda dialog: (questions.append(dialog.message), dialog.accept()))
    serve_client(page)
    page.evaluate(NOBLE_FIXTURE)
    assert page.locator("#nobles .noble").count() == 3
    assert page.locator(".noble[data-post='15'] .who").inner_text() == "vacant"

    # a locked post says why and offers nothing
    page.evaluate("document.querySelector('.noble[data-post=\\'17\\'] .name').click()")
    assert "passes to an heir" in page.locator(".nobleedit").inner_text()
    assert page.locator(".nobleappoint").count() == 0
    assert page.evaluate("window.sent.pop()") == {"type": "noble_candidates"}

    page.evaluate("st.nobleCandidates = [{id: 8, name: 'Kadol, miner'}];"
                  "document.querySelector('.noble[data-post=\\'16\\'] .name').click()")
    assert "manage production" in page.locator(".nobleedit").inner_text()
    assert page.locator(".nobleappoint").inner_text() == "Replace"
    page.evaluate("document.querySelector('.nobleappoint').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "appoint_noble", "post": 16, "unit": 8}
    assert "Replace Urist, manager as manager with Kadol, miner" in questions[-1]
    page.evaluate("document.querySelector('.noblevacate').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "vacate_noble", "post": 16}

    page.evaluate("document.querySelector('.noble[data-post=\\'15\\'] .name').click()")
    assert page.locator(".nobleappoint").inner_text() == "Appoint"
    assert page.locator(".noblevacate").count() == 0

    # read-only without the permission
    page.evaluate("st.perms = new Set(); renderNobles()")
    assert page.locator(".nobleappoint").count() == 0
    assert errors == []
    assert page.evaluate("$('nobles').scrollWidth <= $('nobles').clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
