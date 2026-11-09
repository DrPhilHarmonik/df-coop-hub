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


CITIZEN_FIXTURE = """() => {
  st.canCitizens = true; st.canInspect = true;
  window.sent = [];
  send = (m) => window.sent.push(m);
  window.people = [
    {id: 1, name: "Asa, miner", profession: "Miner", job: "Dig", stress: 5, mood: "Happy", squad: "", x: 10, y: 11, z: 12},
    {id: 2, name: "Bo", profession: "Peasant", job: "", stress: 0, mood: "Miserable", squad: "The Axes", x: 20, y: 21, z: 22},
    {id: 3, name: "Cy", profession: "Baby", job: "", stress: -1, mood: "", squad: ""},
  ];
}"""


def citizen_names(page):
    return page.locator(".citizen .cname").all_inner_texts()


@pytest.mark.parametrize("width", [1280, 390])
def test_citizens_panel_lists_filters_sorts_and_goes_to_someone(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    page.evaluate(CITIZEN_FIXTURE)
    assert page.evaluate("$('citizens').hidden")
    assert page.evaluate("window.sent") == []        # DF isn't asked until it's opened

    page.evaluate("$('citizenstoggle').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "citizens"}
    assert "Looking" in page.locator("#citizens").inner_text()
    page.evaluate("onMessage({type: 'citizens', ok: true, citizens: window.people})")
    assert "3 citizens · 2 idle · 1 unhappy" in page.locator("#citizens").inner_text()
    assert citizen_names(page) == ["Asa", "Bo", "Cy"]
    assert page.locator(".citizen[data-unit='2'] .cmood.bad").count() == 1
    assert "Peasant · No job · The Axes" in page.locator(".citizen[data-unit='2'] .cjob").inner_text()

    page.evaluate("$('citizensort').value = 'mood'; renderCitizens()")
    assert citizen_names(page) == ["Bo", "Asa", "Cy"]
    page.evaluate("$('citizensort').value = 'idle'; renderCitizens()")
    assert citizen_names(page) == ["Bo", "Cy", "Asa"]
    page.evaluate("$('citizensort').value = 'name'; $('citizenfilter').value = 'axes'; renderCitizens()")
    assert citizen_names(page) == ["Bo"]
    page.evaluate("$('citizenfilter').value = 'nobody'; renderCitizens()")
    assert "Nobody matches" in page.locator("#citizens").inner_text()
    page.evaluate("$('citizenfilter').value = ''; renderCitizens(); window.sent.length = 0")

    # someone not on this level yet: go there, then inspect when the hub shows them
    page.evaluate("st.units = []; document.querySelector('.citizen[data-unit=\\'2\\']').click()")
    assert page.evaluate("[st.z, st.camX, st.camY, st.cursor.x, st.cursor.y]") == [22, 20, 21, 20, 21]
    assert page.evaluate("window.sent.filter(m => m.type === 'inspect')") == []
    page.evaluate("onMessage({type: 'units', paused: false, units: [{i: 2, x: 20, y: 21, z: 22, n: 'Bo', c: '#fff'}]})")
    assert page.evaluate("window.sent.filter(m => m.type === 'inspect')") == [{"type": "inspect", "unit": 2}]
    page.evaluate("window.sent.length = 0; onMessage({type: 'units', paused: false, units: []})")
    assert page.evaluate("window.sent") == []

    # someone already in view is inspected at once
    page.evaluate("st.units = [{i: 1, x: 10, y: 11, z: 12}]; document.querySelector('.citizen[data-unit=\\'1\\']').click()")
    assert {"type": "inspect", "unit": 1} in page.evaluate("window.sent")

    # someone on an unseen tile can't be gone to
    z = page.evaluate("st.z")
    page.evaluate("window.sent.length = 0; document.querySelector('.citizen[data-unit=\\'3\\']').click()")
    assert page.evaluate("st.z") == z and page.evaluate("window.sent") == []
    assert "unexplored" in page.locator("#toast").inner_text()

    assert errors == []
    assert page.evaluate("$('citizens').scrollWidth <= $('citizens').clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_citizens_panel_refreshes_only_while_open_and_visible(browser_page):
    page = browser_page
    serve_client(page)
    page.evaluate(CITIZEN_FIXTURE)
    page.evaluate("window.keepInterval = setInterval; setInterval = (fn) => { window.tick = fn; return 1; }")
    page.evaluate("showCitizens(true); window.sent.length = 0; window.tick()")
    assert page.evaluate("window.sent") == [{"type": "citizens"}]
    page.evaluate("""() => {
      window.sent.length = 0;
      Object.defineProperty(document, 'hidden', {value: true, configurable: true});
      window.tick();
    }""")
    assert page.evaluate("window.sent") == []
    page.evaluate("Object.defineProperty(document, 'hidden', {value: false, configurable: true})")
    page.evaluate("showCitizens(false)")
    assert page.evaluate("st.citizenTimer") is None and page.evaluate("$('citizens').hidden")
    assert page.evaluate("$('citizenstoggle').textContent") == "Show"


DETAIL_FIXTURE = """() => {
  st.canWorkDetails = true;
  st.perms = new Set(['labor']);
  st.detailModes = [{id: 'OnlySelectedDoesThis', label: 'Only those picked'}, {id: 'EverybodyDoesThis', label: 'Everybody'},
                    {id: 'NobodyDoesThis', label: 'Nobody'}];
  window.sent = [];
  send = (m) => window.sent.push(m);
  window.detailMsg = {type: 'work_details', ok: true,
    details: [{i: 0, name: 'Miners', mode: 'OnlySelectedDoesThis', no_everybody: true, labors: ['Mine'], members: [8]},
              {i: 1, name: 'Woodcutters', mode: 'OnlySelectedDoesThis', no_everybody: true, labors: ['Cutwood'], members: []},
              {i: 2, name: 'Haulers', mode: 'EverybodyDoesThis', no_everybody: false, labors: ['Haul stone', 'Haul wood'], members: []}],
    people: [{id: 7, name: 'Asa, miner'}, {id: 8, name: 'Bo'}, {id: 9, name: 'Cy'}]};
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_work_details_show_who_and_change_it(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    page.evaluate(DETAIL_FIXTURE)
    assert page.evaluate("$('details').hidden") and page.evaluate("window.sent") == []

    page.evaluate("$('detailstoggle').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "work_details"}
    page.evaluate("onMessage(window.detailMsg)")
    assert page.locator(".detail .name").all_inner_texts() == ["Miners", "Woodcutters", "Haulers"]
    assert page.locator(".detail[data-detail='0'] .who").inner_text() == "1"
    assert page.locator(".detail[data-detail='1'] .who.none").count() == 1       # nobody on it stands out
    assert page.locator(".detail[data-detail='2'] .who").inner_text() == "all"

    page.evaluate("document.querySelector('.detail[data-detail=\\'0\\'] .name').click()")
    # members first; tool details can't be everybody
    assert page.locator(".detailperson span").all_inner_texts() == ["Bo", "Asa", "Cy"]
    assert page.locator(".detailmode option").evaluate_all("o => o.map(x => x.value)") == [
        "OnlySelectedDoesThis", "NobodyDoesThis"]
    assert page.locator(".detailperson input").first.is_checked()
    page.evaluate("document.querySelector('.detailperson input[data-unit=\\'7\\']').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "work_detail_member", "detail": 0, "name": "Miners",
                                                  "unit": 7, "on": True}
    page.evaluate("document.querySelector('.detailperson input[data-unit=\\'8\\']').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "work_detail_member", "detail": 0, "name": "Miners",
                                                  "unit": 8, "on": False}
    page.evaluate("onMessage({type: 'work_detail_result', ok: false, error: 'they already use a tool'})")
    assert "tool" in page.locator("#toast").inner_text()
    assert page.evaluate("window.sent.pop()") == {"type": "work_details"}        # the truth comes back

    page.evaluate("$('detailfilter').value = 'cy'; renderDetails()")
    assert page.locator(".detailperson span").all_inner_texts() == ["Cy"]
    page.evaluate("""() => {
      const box = document.querySelector('.detailmode');
      box.value = 'NobodyDoesThis'; box.onchange();
    }""")
    assert page.evaluate("window.sent.pop()") == {"type": "work_detail_mode", "detail": 0, "name": "Miners",
                                                  "mode": "NobodyDoesThis"}

    # everybody: no tick boxes, and the setting is offered
    page.evaluate("document.querySelector('.detail[data-detail=\\'2\\'] .name').click()")
    assert page.locator(".detailperson").count() == 0
    assert "Everybody does this" in page.locator(".detailedit").inner_text()

    # read-only for someone whose role can't change them
    page.evaluate("st.perms = new Set(); document.querySelector('.detail[data-detail=\\'0\\'] .name').click()")
    assert page.locator(".detailmode").count() == 0
    assert page.locator(".detailperson input").first.is_disabled()
    assert errors == []
    assert page.evaluate("$('details').scrollWidth <= $('details').clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_a_hidden_tab_tells_the_hub(browser_page):
    page = browser_page
    serve_client(page)
    page.evaluate(ORDER_FIXTURE)
    page.evaluate("""() => {
      Object.defineProperty(document, 'hidden', {value: true, configurable: true});
      document.dispatchEvent(new Event('visibilitychange'));
      Object.defineProperty(document, 'hidden', {value: false, configurable: true});
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    assert page.evaluate("window.sent") == [{"type": "visibility", "hidden": True},
                                            {"type": "visibility", "hidden": False}]


@pytest.mark.parametrize("width", [1280, 390])
def test_order_conditions_can_be_changed_removed_and_added(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    page.evaluate(ORDER_FIXTURE)
    page.evaluate("""() => {
      st.canEditConditions = true;
      Object.assign(st.orderCatalog, {
        compares: [{id: 'AtLeast', label: 'at least'}, {id: 'LessThan', label: 'fewer than'}],
        cond_items: [{id: 'BED', label: 'Beds'}, {id: 'DOOR', label: 'Doors'}],
        cond_whens: [{id: 'Completed', label: 'completed'}, {id: 'Activated', label: 'activated'}],
      });
      Object.assign(st.orders[3], {max_workshops: 2,
        conds: ['at least 10 wood', 'after Barrels 0 is completed'],
        cond_info: [{key: 'i:1', cmp: 'AtLeast', val: 10, what: 'wood'}, {key: 'o:100:1'}]});
      renderOrders();
    }""")
    page.evaluate("document.querySelector('.order[data-order=\\'103\\'] .name').click()")
    assert page.locator(".condrow .condwhat").first.inner_text() == "wood"
    assert page.locator(".condrow .condwhat").nth(1).inner_text() == "after Barrels 0 is completed"
    assert page.locator(".editlimit").input_value() == "2"

    page.evaluate("""() => {
      const box = document.querySelector('.condval');
      box.value = '25'; box.onchange();
    }""")
    assert page.evaluate("window.sent.pop()") == {"type": "edit_condition", "order": 103, "key": "i:1",
                                                  "cmp": "AtLeast", "val": 25}
    page.evaluate("""() => {
      const box = document.querySelector('.condcmp');
      box.value = 'LessThan'; box.onchange();
    }""")
    assert page.evaluate("window.sent.pop()") == {"type": "edit_condition", "order": 103, "key": "i:1",
                                                  "cmp": "LessThan", "val": 25}
    # no change, or a number DF wouldn't take, sends nothing
    page.evaluate("""() => {
      const box = document.querySelector('.condval');
      box.value = '10'; document.querySelector('.condcmp').value = 'AtLeast'; box.onchange();
      box.value = '-3'; box.onchange();
    }""")
    assert page.evaluate("window.sent.length") == 0

    page.locator(".condremove").nth(1).evaluate("b => b.click()")
    assert page.evaluate("window.sent.pop()") == {"type": "remove_condition", "order": 103, "key": "o:100:1"}

    page.evaluate("""() => {
      document.querySelector('.addconditem').value = 'DOOR';
      document.querySelector('.addcondcmp').value = 'LessThan';
      document.querySelector('.addcondval').value = '4';
      document.querySelector('.addcondadd').click();
    }""")
    assert page.evaluate("window.sent.pop()") == {"type": "add_condition", "order": 103, "item": "DOOR",
                                                  "cmp": "LessThan", "val": 4}
    # an order can't wait for itself, so it isn't on its own list
    assert "103" not in page.locator(".addcondorder option").evaluate_all("o => o.map(x => x.value)")
    page.evaluate("""() => {
      document.querySelector('.addcondorder').value = '105';
      document.querySelector('.addcondwhen').value = 'Activated';
      document.querySelector('.addcondafter').click();
    }""")
    assert page.evaluate("window.sent.pop()") == {"type": "add_condition", "order": 103, "after": 105, "when": "Activated"}

    page.evaluate("""() => {
      document.querySelector('.editlimit').value = '0';
      document.querySelector('.editsave').click();
    }""")
    assert page.evaluate("window.sent.pop()") == {"type": "edit_order", "order": 103, "max_workshops": 0}
    assert page.evaluate("$('orders').scrollWidth <= $('orders').clientWidth")

    # an old module sends no keys and can't take the new messages
    page.evaluate("""() => {
      st.canEditConditions = false;
      st.orders[3].cond_info = [{key: ''}, {key: ''}];
      renderOrders();
    }""")
    assert page.locator(".condremove, .addcondadd, .editlimit").count() == 0
    assert "at least 10 wood" in page.locator(".orderedit").inner_text()
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


PLACE_FIXTURE = """() => {
  st.perms = new Set(['build']);
  st.canPlaces = true;
  st.stockCategories = [{id: 'food', label: 'Food'}, {id: 'wood', label: 'Wood'}];
  st.place = {kind: 'stockpile', id: 300, number: 1, name: '', tiles: 12, z: 5, x: 1, y: 1,
              categories: ['wood'], barrels: 1, bins: 2, wheelbarrows: 0};
  window.sent = [];
  send = (m) => window.sent.push(m);
  renderPlace();
  openSide($("placeinfo"));
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_place_panel_changes_stockpiles_and_zones(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    questions = []
    page.on("dialog", lambda dialog: (questions.append(dialog.message), dialog.accept()))
    serve_client(page)
    page.evaluate(PLACE_FIXTURE)
    assert "Stockpile #1" in page.locator("#placeinfo").inner_text()
    assert page.evaluate("document.querySelector('[data-category=wood]').checked") is True
    page.evaluate("document.querySelector('[data-category=food]').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "stockpile_category", "id": 300, "category": "food", "on": True}
    assert questions == []
    page.evaluate("document.querySelector('[data-category=wood]').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "stockpile_category", "id": 300, "category": "wood", "on": False}
    assert "finer choices" in questions[-1]

    page.evaluate("""() => {
      document.querySelector('[data-limit=bins]').value = '7';
      document.querySelector('.limitsave').click();
    }""")
    assert page.evaluate("window.sent.pop()") == {"type": "stockpile_containers", "id": 300, "barrels": 1, "bins": 7, "wheelbarrows": 0}
    page.evaluate("""() => {
      document.querySelector('[data-limit=bins]').value = '99';
      document.querySelector('.limitsave').click();
    }""")
    assert page.evaluate("window.sent.length") == 0     # too many for 12 tiles

    # a refresh doesn't wipe a half-typed name
    page.evaluate("""() => {
      const box = document.querySelector('.placename');
      box.focus(); box.value = 'half';
      renderPlace();
    }""")
    assert page.evaluate("document.querySelector('.placename').value") == "half"
    page.evaluate("document.querySelector('.placename').blur(); document.querySelector('.placesave').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "rename_place", "id": 300, "name": "half"}

    # a bedroom asks for its candidates, assigns and frees
    page.evaluate("""() => {
      st.place = {kind: 'zone', id: 58, number: 2, name: '', tiles: 9, z: 5, x: 6, y: 6, zone: 'DiningHall',
                  active: true, ownable: true, owner: 'Urist, miner'};
      renderPlace();
    }""")
    assert page.evaluate("window.sent.pop()") == {"type": "zone_candidates"}
    assert "Dining hall #2" in page.locator("#placeinfo").inner_text()
    page.evaluate("st.zoneCandidates = [{id: 8, name: 'Kadol, cook'}]; renderPlace()")
    page.evaluate("document.querySelector('.zoneassign').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "zone_owner", "id": 58, "unit": 8}
    page.evaluate("document.querySelector('.zonefree').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "zone_owner", "id": 58}
    page.evaluate("document.querySelector('.zoneactive').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "zone_active", "id": 58, "on": False}

    # a pen has no owner; without the permission nothing can be changed
    page.evaluate("st.place = {...st.place, zone: 'Pen', ownable: false, owner: ''}; renderPlace()")
    assert page.locator(".zoneassign").count() == 0
    page.evaluate("st.perms = new Set(); st.place = {...st.place, ownable: true, owner: 'Urist'}; renderPlace()")
    assert page.locator(".zoneassign").count() == 0 and page.locator(".zoneactive").count() == 0
    assert page.locator(".placename").count() == 0
    assert errors == []
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


WORKSHOP_FIXTURE = """() => {
  st.perms = new Set(['order']);
  st.canPlaces = true; st.canWorkshops = true;
  st.zoneCandidates = [{id: 8, name: 'Kadol, mason', adult: true}, {id: 9, name: 'Little one', adult: false}];
  st.place = {kind: 'workshop', id: 400, name: "Stoneworker's Workshop", custom: '', workshop: 'Masons', built: true,
              z: 5, x: 11, y: 11, tiles: 9, general_orders: 5, orders_here: 1, workers: [],
              jobs: [{id: 11, name: 'Make granite blocks', order: 8, worker: 'Urist', suspended: false, repeat: false,
                      now: false, building: false},
                     {id: 12, name: 'Make diorite throne with a very long name that has to wrap somewhere', order: -1,
                      worker: '', suspended: true, repeat: true, now: true, building: false}]};
  window.sent = [];
  send = (m) => window.sent.push(m);
  renderPlace();
  openSide($("placeinfo"));
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_workshop_panel_lists_tasks_and_changes_them(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    questions = []
    page.on("dialog", lambda dialog: (questions.append(dialog.message), dialog.accept()))
    serve_client(page)
    page.evaluate(WORKSHOP_FIXTURE)
    text = page.locator("#placeinfo").inner_text()
    assert "Stoneworker's Workshop" in text and "2 tasks" in text
    assert "Urist working · order #8" in text and "suspended · do now · repeats" in text
    assert "Anyone may work here" in text and "up to 5 general work orders · 1 order tied here" in text
    first = page.locator(".job").nth(0)
    assert [b.inner_text() for b in first.locator("button").all()] == ["Suspend", "Do now", "Repeat", "Cancel"]
    second = page.locator(".job").nth(1)
    assert [b.inner_text() for b in second.locator("button").all()] == ["Resume", "Not first", "Just once", "Cancel"]
    first.locator("[data-action=suspend]").click()
    assert page.evaluate("window.sent.pop()") == {"type": "workshop_job", "id": 400, "job": 11, "action": "suspend"}
    second.locator("[data-action=cancel]").click()
    assert page.evaluate("window.sent.pop()") == {"type": "workshop_job", "id": 400, "job": 12, "action": "cancel"}
    assert "Cancel" in questions[-1]

    # only adults can be picked as the one worker
    assert page.evaluate("[...document.querySelectorAll('.workerpick option')].map(o => o.value)") == ["8"]
    page.click(".workerassign")
    assert page.evaluate("window.sent.pop()") == {"type": "workshop_worker", "id": 400, "unit": 8}
    assert page.locator(".workerfree").count() == 0
    page.evaluate("st.place = {...st.place, workers: [{id: 8, name: 'Kadol, mason'}]}; renderPlace()")
    assert "Only Kadol, mason works here" in page.locator("#placeinfo").inner_text()
    page.click(".workerfree")
    assert page.evaluate("window.sent.pop()") == {"type": "workshop_worker", "id": 400}

    # the workshop being built offers nothing to change on that job
    page.evaluate("""() => {
      st.place = {...st.place, built: false,
                  jobs: [{id: 13, name: 'Construct building', order: -1, worker: '', suspended: false,
                          repeat: false, now: false, building: true}]};
      renderPlace();
    }""")
    assert "being built" in page.locator("#placeinfo").inner_text()
    assert page.locator(".job button").count() == 0

    # read-only without the permission
    page.evaluate(WORKSHOP_FIXTURE)
    page.evaluate("st.perms = new Set(); renderPlace()")
    assert page.locator(".job button").count() == 0 and page.locator(".workerassign").count() == 0
    assert "Make granite blocks" in page.locator("#placeinfo").inner_text()
    assert errors == []
    assert page.evaluate("$('placeinfo').scrollWidth <= $('placeinfo').clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_workshops_are_clickable_only_with_the_new_module(browser_page):
    page = browser_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    page.evaluate("""() => {
      st.perms = new Set(); st.canPlaces = true; st.canWorkshops = false; st.mode = 'look'; st.z = 5; st.units = [];
      st.buildings = [{i: 400, name: 'Masons', kind: 'Workshop', x1: 2, x2: 4, y1: 2, y2: 4, z1: 5, z2: 5},
                      {i: 401, name: 'Smelter', kind: 'Furnace', x1: 6, x2: 8, y1: 2, y2: 4, z1: 5, z2: 5},
                      {i: 7, name: 'Bed', kind: 'Bed', x1: 5, x2: 5, y1: 5, y2: 5, z1: 5, z2: 5}];
      window.sent = [];
      send = (m) => window.sent.push(m);
      st.cursor = {x: 3, y: 3}; pickCorner();
      st.canWorkshops = true;
      pickCorner();
      st.cursor = {x: 7, y: 3}; pickCorner();
      st.cursor = {x: 5, y: 5}; pickCorner();
    }""")
    assert [m["id"] for m in page.evaluate("window.sent")] == [400, 401]
    assert errors == []


def test_clicking_a_tile_inspects_the_place_there_and_cycles_overlaps(browser_page):
    page = browser_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    page.evaluate("""() => {
      st.perms = new Set(); st.canPlaces = true; st.mode = 'look'; st.z = 5; st.units = [];
      st.buildings = [
        {i: 7, name: 'Bed', kind: 'bed', x1: 3, x2: 3, y1: 3, y2: 3, z1: 5, z2: 5},
        {i: 58, name: 'Bedroom', kind: 'building_civzonest', x1: 2, x2: 4, y1: 2, y2: 4, z1: 5, z2: 5},
        {i: 59, name: 'Tomb', kind: 'building_civzonest', x1: 3, x2: 5, y1: 3, y2: 5, z1: 5, z2: 5}];
      window.sent = [];
      send = (m) => window.sent.push(m);
      st.cursor = {x: 3, y: 3};
      pickCorner(); pickCorner(); pickCorner();
      st.cursor = {x: 9, y: 9};
      pickCorner();
    }""")
    assert [m["id"] for m in page.evaluate("window.sent")] == [58, 59, 58]   # never the bed; empty tile sends nothing
    assert errors == []


def test_side_panel_can_be_dragged_wider_and_remembers(browser_page):
    page = browser_page
    page.set_viewport_size({"width": 1280, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    width = lambda: page.locator("#side").bounding_box()["width"]
    assert width() == 240
    map_before = page.evaluate("canvas.width")

    def drag_to(x):
        box = page.locator("#sidedrag").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, 300)
        page.mouse.down()
        page.mouse.move(x, 320, steps=4)
        page.mouse.up()

    drag_to(1280 - 400)
    assert abs(width() - 400) <= 1
    assert page.evaluate("canvas.width") < map_before        # the map gave up the room and stays sharp
    assert page.evaluate("localStorage.getItem('df-coop-side-width')") == "400"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    drag_to(1270)            # too narrow
    assert width() == 200
    drag_to(10)              # too wide
    assert width() == 720 or width() == 1280 * 0.7 // 1

    page.evaluate("setSideWidth(400, true)")
    page.reload()
    page.wait_for_function("document.querySelector('#side').getBoundingClientRect().width === 400")

    # the keyboard resizes it without panning the map
    cam = page.evaluate("st.camX")
    page.focus("#sidedrag")
    page.keyboard.press("ArrowLeft")
    assert width() == 420 and page.evaluate("st.camX") == cam
    page.dblclick("#sidedrag")
    assert width() == 240
    # a window shrinking below a saved width pulls it back in
    page.evaluate("setSideWidth(700, false)")
    page.set_viewport_size({"width": 800, "height": 800})
    page.wait_for_function("document.querySelector('#side').getBoundingClientRect().width <= 560")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []


def test_phone_side_panel_has_no_drag_handle_and_ignores_saved_width(browser_page):
    page = browser_page
    page.set_viewport_size({"width": 390, "height": 800})
    page.add_init_script("localStorage.setItem('df-coop-side-width', '600')")
    serve_client(page)
    assert not page.locator("#sidedrag").is_visible()
    page.evaluate("openSide($('orders'))")
    assert page.locator("#side").bounding_box()["width"] <= 390 * 0.85 + 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1280, 390])
def test_squad_orders_station_kill_and_cancel(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    page.evaluate(SQUAD_FIXTURE)
    page.evaluate("st.canSquadOrders = true; document.querySelector('.squad[data-squad=\\'2\\'] .name').click()")
    sent = page.evaluate("window.sent.splice(0)")
    assert {"type": "squad_targets"} in sent
    assert "Looking for enemies" in page.locator(".squadedit").inner_text()

    # nothing to fight: says so, and offers another look
    page.evaluate("st.squadTargets = []; renderSquads()")
    assert "No enemies in sight" in page.locator(".squadedit").inner_text()
    assert page.locator(".squadkill").count() == 0
    page.evaluate("document.querySelector('.squadlook').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "squad_targets"}

    # one enemy, then all of them
    page.evaluate("st.squadTargets = [{id: 40, name: 'Goblin'}, {id: 41, name: 'Troll'}]; renderSquads()")
    page.evaluate("document.querySelector('.squadkill').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "squad_kill", "squad": 2, "units": [40, 41]}
    page.evaluate("document.querySelector('.squadtarget').value = '41'; document.querySelector('.squadkill').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "squad_kill", "squad": 2, "units": [41]}

    # Station picks a tile on the map, once, then goes back to Look
    page.evaluate("document.querySelector('.squadstation').click()")
    assert page.evaluate("st.mode") == "station"
    assert "stand" in page.evaluate("$('hint').textContent")
    page.evaluate("st.z = 5; st.cursor = {x: 12, y: 13}; pickCorner()")
    assert page.evaluate("window.sent.pop()") == {"type": "squad_station", "squad": 2, "x": 12, "y": 13, "z": 5}
    assert page.evaluate("st.mode") == "look" and page.evaluate("st.stationSquad") is None

    # Esc backs out without sending anything
    page.evaluate("document.querySelector('.squadstation').click(); setMode('look')")
    assert page.evaluate("st.stationSquad") is None and page.evaluate("window.sent.length") == 0

    # a stationed squad shows where, and can be cancelled
    assert page.locator(".squadclear").count() == 0
    page.evaluate("st.squads[0].station = {x: 12, y: 13, z: 5}; st.squads[0].orders = ['MOVE']; renderSquads()")
    assert "Stationed at 12, 13, z5" in page.locator(".squadgo").inner_text()
    assert "Orders: station" in page.locator(".squadedit").inner_text()
    page.evaluate("document.querySelector('.squadclear').click()")
    assert page.evaluate("window.sent.pop()") == {"type": "squad_clear", "squad": 2}
    page.evaluate("document.querySelector('.squadgo').click()")
    assert page.evaluate("[st.camX, st.camY, st.z]") == [12, 13, 5]

    # without the permission, or on an older module: no controls
    page.evaluate("st.perms = new Set(); renderSquads()")
    assert page.locator(".squadstation").count() == 0 and page.locator(".squadclear").count() == 0
    page.evaluate("st.perms = new Set(['military']); st.canSquadOrders = false; renderSquads()")
    assert page.locator(".squadstation").count() == 0
    assert errors == []
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1280, 390])
def test_stocks_panel_groups_filters_and_remembers_what_was_open(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    page.evaluate("""() => {
      st.canStocks = true;
      window.sent = [];
      send = (m) => window.sent.push(m);
      showStocks(true);
      openSide($("stockstoggle").parentElement);
    }""")
    assert page.evaluate("window.sent") == [{"type": "stocks"}]
    assert "counting" in page.locator("#stocks").inner_text()
    page.evaluate("""() => onMessage({type: 'stocks', ok: true, age: 12, groups: [
      {name: 'Raw materials', total: 14306, rows: [{name: 'kapok logs', count: 8312}, {name: 'coal bars', count: 5994}]},
      {name: 'Remains', total: 342, rows: [{name: 'blue peafowl body parts with a long name that must wrap', count: 342}]}]})""")
    text = page.locator("#stocks").inner_text()
    assert "Raw materials" in text and "14,306" in text and "Counted 12 s ago" in text
    assert page.evaluate("[...document.querySelectorAll('.stockgroup')].map(g => g.open)") == [False, False]
    page.click(".stockgroup[data-group='Raw materials'] summary")
    page.fill("#stockfilter", "coal")
    assert page.evaluate("[...document.querySelectorAll('.stockgroup')].map(g => g.dataset.group)") == ["Raw materials"]
    assert page.locator(".stockrow").all_inner_texts() == ["coal bars\n5,994"]
    page.fill("#stockfilter", "zzz")
    assert "nothing matches" in page.locator("#stocks").inner_text()
    page.fill("#stockfilter", "")
    # what was opened stays open when new counts arrive
    page.evaluate("renderStocks()")
    assert page.evaluate("[...document.querySelectorAll('.stockgroup')].map(g => g.open)") == [True, False]
    page.evaluate("showStocks(false)")
    assert page.evaluate("$('stocks').hidden && $('stockfilter').hidden")
    assert errors == []
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_stocks_key_opens_the_panel(browser_page):
    page = browser_page
    serve_client(page)
    page.evaluate("""() => {
      st.canStocks = true; window.sent = [];
      send = (m) => { window.sent.push(m); };
    }""")
    page.keyboard.press("k")
    assert page.evaluate("st.stocksOpen") is True
    assert page.evaluate("window.sent") == [{"type": "stocks"}]


FARM_FIXTURE = """() => {
  st.perms = new Set(['build']);
  st.canPlaces = true; st.canFarms = true;
  const helmets = {id: 173, name: 'plump helmets', seeds: 58};
  st.place = {kind: 'farm', id: 500, name: '', tiles: 16, z: 121, x: 1, y: 1, underground: true, fertilize: false,
              season: 2,
              crops: [{id: 176, name: 'sweet pods'}, {id: 174, name: 'pig tails'}, {id: -1, name: ''},
                      {id: 999, name: 'an old crop'}],
              options: [[helmets, {id: 176, name: 'sweet pods', seeds: 36}],
                        [helmets, {id: 174, name: 'pig tails', seeds: 45}, {id: 175, name: 'quarry bushes', seeds: 0}],
                        [helmets], [helmets]]};
  window.sent = [];
  send = (m) => window.sent.push(m);
  renderPlace();
  openSide($("placeinfo"));
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_farm_panel_picks_a_crop_per_season(browser_page, width):
    page = browser_page
    page.set_viewport_size({"width": width, "height": 800})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    serve_client(page)
    page.evaluate(FARM_FIXTURE)
    text = page.locator("#placeinfo").inner_text()
    assert "Farm plot" in text and "16 tiles · underground" in text and "Autumn (now)" in text
    values = page.evaluate("[...document.querySelectorAll('.farmcrop')].map(s => s.value)")
    assert values == ["176", "174", "-1", "999"]           # a crop that no longer fits still shows
    assert page.locator(".placebox .warn").count() == 0
    page.evaluate("st.place = {...st.place, autofarm: true}; renderPlace()")
    assert "autofarm is on" in page.locator(".placebox .warn").inner_text()
    labels = page.evaluate("[...document.querySelectorAll('.farmcrop')[1].options].map(o => o.textContent)")
    assert labels == ["fallow", "plump helmets (58 seeds)", "pig tails (45 seeds)", "quarry bushes (no seeds)"]
    page.select_option(".farmcrop[data-season='2']", "173")
    assert page.evaluate("window.sent.pop()") == {"type": "farm_crop", "id": 500, "season": 2, "plant": 173}
    page.select_option(".farmcrop[data-season='0']", "-1")
    assert page.evaluate("window.sent.pop()") == {"type": "farm_crop", "id": 500, "season": 0, "plant": -1}
    page.click(".farmfert input")
    assert page.evaluate("window.sent.pop()") == {"type": "farm_fertilize", "id": 500, "on": True}
    page.evaluate("st.perms = new Set(); renderPlace()")
    assert page.locator(".farmcrop").count() == 0
    assert "pig tails" in page.locator("#placeinfo").inner_text() and "fallow" in page.locator("#placeinfo").inner_text()
    assert page.evaluate("document.querySelector('.farmfert input').disabled")
    assert errors == []
    assert page.evaluate("$('placeinfo').scrollWidth <= $('placeinfo').clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_farm_plots_are_clickable_only_with_the_new_module(browser_page):
    page = browser_page
    serve_client(page)
    page.evaluate("""() => {
      st.perms = new Set(); st.canPlaces = true; st.canWorkshops = true; st.canFarms = false; st.mode = 'look';
      st.z = 5; st.units = [];
      st.buildings = [{i: 500, name: 'Farm Plot', kind: 'FarmPlot', x1: 2, x2: 5, y1: 2, y2: 5, z1: 5, z2: 5}];
      window.sent = [];
      send = (m) => window.sent.push(m);
      st.cursor = {x: 3, y: 3}; pickCorner();
      st.canFarms = true; pickCorner();
    }""")
    assert [m["id"] for m in page.evaluate("window.sent")] == [500]
