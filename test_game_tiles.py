"""Host asset discovery, sprite selection, and the HTTP asset boundary."""

import pytest

from game_tiles import GameTileset, building_sprite, item_sprite, terrain_sprite


@pytest.fixture
def graphics_dir(tmp_path):
    root = tmp_path / "data/vanilla/vanilla_environment/graphics"
    root.mkdir(parents=True)
    (root / "images").mkdir()
    (root / "images/floors.png").write_bytes(b"local game image")
    (root / "tile_page_environment.txt").write_text(
        "[OBJECT:TILE_PAGE]\n[TILE_PAGE:FLOORS]\n[FILE:images/floors.png]\n[TILE_DIM:32:32]\n")
    (root / "graphics_tiles.txt").write_text(
        "[OBJECT:GRAPHICS]\n[TILE_GRAPHICS:FLOORS:1:4:STONE_FLOOR_5]\n"
        "[TILE_GRAPHICS:FLOORS:6:2:WATER]\n[TILE_GRAPHICS:FLOORS:0:0:UNUSED]\n")
    return tmp_path, root


def test_discovers_supported_sprites_without_copying_assets(graphics_dir):
    df_dir, root = graphics_dir
    tileset = GameTileset(df_dir)
    manifest = tileset.load()
    assert manifest["available"] is True
    assert manifest["sprites"] == {"STONE_FLOOR_5": ["FLOORS", 1, 4], "WATER": ["FLOORS", 6, 2]}
    assert manifest["pages"] == {"FLOORS": {"url": "/tileset/page/FLOORS", "w": 32, "h": 32}}
    assert tileset.page("FLOORS") == root / "images/floors.png"
    assert tileset.page("../../secret") is None
    assert tileset.page("UNUSED") is None
    (root / "graphics_tiles.txt").unlink()
    assert tileset.load() is manifest


def test_missing_or_incomplete_installation_is_unavailable(tmp_path):
    assert GameTileset(tmp_path).load() == {"available": False, "pages": {}, "sprites": {}}


@pytest.mark.parametrize("filename", ["../secret.png", "images/escape.png", "images/secret.txt"])
def test_page_definitions_cannot_expose_other_files(graphics_dir, filename):
    df_dir, root = graphics_dir
    secret = root.parent / "secret.png"
    secret.write_bytes(b"private")
    (root / "images/escape.png").symlink_to(secret)
    (root / "images/secret.txt").write_text("private")
    (root / "tile_page_environment.txt").write_text(
        f"[TILE_PAGE:FLOORS][FILE:{filename}][TILE_DIM:32:32]")
    tileset = GameTileset(df_dir)
    assert tileset.load()["available"] is False
    assert tileset.page("FLOORS") is None


@pytest.mark.parametrize("shape,material,special,expected", [
    ("WALL", "STONE", "NORMAL", "STONE_WALL_N_S_W_E_1"),
    ("WALL", "STONE", "SMOOTH", "SMOOTHED_STONE_WALL_N_S_W_E"),
    ("WALL", "CONSTRUCTION", "NORMAL", "ROCK_BLOCKS_WALL_N_S_W_E"),
    ("FLOOR", "GRASS_DARK", "NORMAL", "GRASS_5"),
    ("FLOOR", "SOIL", "FURROWED", "FURROWED_SOIL_1"),
    ("STAIR_UPDOWN", "STONE", "NORMAL", "STONE_STAIR_UPDOWN"),
    ("RAMP", "SOIL", "NORMAL", "SOIL_RAMP_WITH_WALL_N_S_E_W"),
    ("EMPTY", "AIR", "NORMAL", None),
    ("SHRUB", "PLANT", "NORMAL", "SHRUB"),
    ("SAPLING", "PLANT", "NORMAL", "SAPLING"),
    ("WALL", "TREE_MATERIAL", "NORMAL", "TREE_TRUNK_PILLAR"),
    ("BRANCH", "TREE_MATERIAL", "NORMAL", "TREE_BRANCH_NSWE"),
])
def test_terrain_sprite_selection(shape, material, special, expected):
    assert terrain_sprite(shape, material, special) == expected


def test_http_only_serves_allowlisted_pages(graphics_dir):
    pytest.importorskip("google.protobuf")
    pytest.importorskip("websockets")
    from hub import Hub

    df_dir, root = graphics_dir
    hub = Hub(None, {}, df_dir=df_dir)
    assert hub._static("/tileset/manifest.json").status_code == 200
    response = hub._static("/tileset/page/FLOORS")
    assert response.status_code == 200
    assert response.body == b"local game image"
    assert response.headers["Content-Type"] == "image/png"
    for path in ("/tileset/page/../secret.png", "/tileset/page/%2e%2e/secret.png", "/tileset/page/unknown"):
        assert hub._static(path).status_code == 404
    (root / "images/floors.png").unlink()
    assert hub._static("/tileset/page/FLOORS").status_code == 404


@pytest.fixture
def expanded_graphics(graphics_dir):
    df_dir, root = graphics_dir
    root = root.parent.parent / "vanilla_buildings_graphics/graphics"
    root.mkdir(parents=True)
    (root / "sheet.png").write_bytes(b"sprites")
    (root / "tile_page_buildings.txt").write_text(
        "[TILE_PAGE:WORKSHOPS][FILE:sheet.png][TILE_DIM:32:32]")
    (root / "graphics_workshops.txt").write_text(
        "[TILE_GRAPHICS:WORKSHOPS:0:0:WORKSHOP_CARPENTER:3:0:0]"
        "[TILE_GRAPHICS:WORKSHOPS:1:0:WORKSHOP_CARPENTER:3:1:0]"
        "[TILE_GRAPHICS:WORKSHOPS:2:0:WORKSHOP_CARPENTER:0:0:0]"
        "[TILE_GRAPHICS:WORKSHOPS:3:0:WORKSHOP_CARPENTER_OVERLAY:3:0:0]"
        "[TOOL_GRAPHICS:WORKSHOPS:4:0:ITEM_TOOL_BOOKCASE]"
        "[WEAPON_GRAPHICS:ITEM_WEAPON_AXE_BATTLE][WEAPON_GRAPHICS_DEFAULT:WORKSHOPS:5:0]")
    creatures = root.parent.parent / "vanilla_creatures_graphics/graphics"
    creatures.mkdir(parents=True)
    (creatures / "animals.png").write_bytes(b"animals")
    (creatures / "tile_page_creatures.txt").write_text(
        "[TILE_PAGE:ANIMALS][FILE:animals.png][TILE_DIM:32:32]")
    (creatures / "graphics_creatures.txt").write_text(
        "[CREATURE_GRAPHICS:CAT][DEFAULT:ANIMALS:0:0:AS_IS][CHILD:ANIMALS:1:0:AS_IS:DEFAULT]"
        "[CREATURE_CASTE_GRAPHICS:CHICKEN:MALE][DEFAULT:ANIMALS:2:0:AS_IS]"
        "[CREATURE_CASTE_GRAPHICS:CHICKEN:FEMALE][DEFAULT:ANIMALS:3:0:AS_IS]"
        "[CREATURE_GRAPHICS:GIANT][DEFAULT:ANIMALS:LARGE_IMAGE:0:1:1:2:AS_IS]"
        "[CREATURE_GRAPHICS:HORSE][LAYER_SET:DEFAULT]"
        "[LAYER:BODY:ANIMALS:0:3][CONDITION_NOT_CHILD]"
        "[LAYER:CBODY:ANIMALS:1:3][CONDITION_CHILD]"
        "[CREATURE_GRAPHICS:DWARF][LAYER_SET:DEFAULT]"
        "[LAYER:SHADOW:ANIMALS:4:0][LAYER:LIGHT_BODY:ANIMALS:5:0][CONDITION_CASTE:MALE]"
        "[LAYER:UNSUPPORTED:ANIMALS:6:0][CONDITION_CASTE:FEMALE]"
        "[LAYER:CLOTHING_TORSO_DEFAULT:ANIMALS:7:0]"
        "[CONDITION_ITEM_WORN:BY_CATEGORY:BODY_UPPER:ARMOR:ITEM_ARMOR_SHIRT]"
        "[SHUT_OFF_IF_ITEM_PRESENT:BY_CATEGORY:BODY_UPPER:ARMOR:ITEM_ARMOR_PLATE]"
        "[LAYER:LIGHT_BODY:ANIMALS:8:0][CONDITION_CASTE:FEMALE]")
    return GameTileset(df_dir)


def test_workshop_layout_preserves_stage_footprint_and_overlay(expanded_graphics):
    manifest = expanded_graphics.load()
    layout = manifest["layouts"]["WORKSHOP_CARPENTER"]
    assert layout == {"w": 2, "h": 1, "tiles": {
        "0,0": ["WORKSHOP_CARPENTER:3:0:0", "WORKSHOP_CARPENTER_OVERLAY:3:0:0"],
        "1,0": ["WORKSHOP_CARPENTER:3:1:0"]}}
    assert "ITEM_TOOL_BOOKCASE" in manifest["sprites"]
    assert manifest["sprites"]["ITEM_WEAPON_AXE_BATTLE"] == ["WORKSHOPS", 5, 0]
    assert expanded_graphics.page("WORKSHOPS").name == "sheet.png"
    assert building_sprite("Workshop/Carpenters") == "WORKSHOP_CARPENTER"
    assert building_sprite("Workshop/SCREW_PRESS") == "WORKSHOP_CUSTOM:SCREW_PRESS"
    assert building_sprite("Civzone/MeetingHall") is None


def test_creature_species_caste_and_large_images(expanded_graphics):
    tileset = expanded_graphics
    assert tileset.unit_sprites("CAT", "FEMALE") == ["CREATURE:CAT::DEFAULT"]
    assert tileset.unit_sprites("CAT", "FEMALE", state="CHILD") == ["CREATURE:CAT::CHILD"]
    assert tileset.unit_sprites("CHICKEN", "FEMALE") == ["CREATURE:CHICKEN:FEMALE:DEFAULT"]
    assert tileset.unit_sprites("CHICKEN", "MALE") == ["CREATURE:CHICKEN:MALE:DEFAULT"]
    assert tileset.manifest["sprites"]["CREATURE:GIANT::DEFAULT"] == ["ANIMALS", 0, 1, 2, 2]
    assert tileset.unit_sprites("UNKNOWN", "MALE") == []
    adult = tileset.unit_sprites("HORSE", "MALE")
    child = tileset.unit_sprites("HORSE", "MALE", state="CHILD")
    assert [tileset.manifest["sprites"][token][1] for token in adult] == [0]
    assert [tileset.manifest["sprites"][token][1] for token in child] == [1]


def test_humanoid_layers_match_caste_and_equipped_clothing(expanded_graphics):
    tileset = expanded_graphics
    def columns(caste, equipment=()):
        return [tileset.manifest["sprites"][token][1] for token in tileset.unit_sprites("DWARF", caste, equipment)]
    assert columns("MALE") == [4, 5]
    assert columns("FEMALE", ["ITEM_ARMOR_SHIRT"]) == [4, 7, 8]
    assert columns("MALE", ["ITEM_ARMOR_SHIRT", "ITEM_ARMOR_PLATE"]) == [4, 5]


@pytest.mark.parametrize("definition,expected", [
    ("PLANT/MUSHROOM_HELMET_PLUMP", "ITEM_PLANT"), ("BOX/BAG", "ITEM_BAG_EMPTY"),
    ("ARMOR/ITEM_ARMOR_SHIRT", "ITEM_ARMOR_SHIRT"), ("BOULDER", "ITEM_BOULDER"),
])
def test_item_sprite_uses_subtype_or_generic_item(definition, expected):
    assert item_sprite(definition) == expected
