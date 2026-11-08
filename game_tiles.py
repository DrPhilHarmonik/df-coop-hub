"""Read map graphics and creature layers from the host's DF installation."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path


def terrain_sprite(shape: str, material: str, special: str) -> str | None:
    smooth = special in {"SMOOTH", "SMOOTH_DEAD"}
    family = "GRASS" if material.startswith("GRASS_") else "DIRT" if material == "SOIL" else "STONE"
    if shape == "WALL":
        if material == "TREE_MATERIAL":
            return "TREE_TRUNK_PILLAR"
        if material == "CONSTRUCTION":
            return "ROCK_BLOCKS_WALL_N_S_W_E"
        if material == "FROZEN_LIQUID":
            return "SMOOTHED_ICE_WALL_N_S_W_E" if smooth else "ICE_WALL_N_S_W_E"
        if family == "DIRT":
            return "SOIL_WALL_N_S_W_E_1"
        if smooth:
            return "SMOOTHED_STONE_WALL_N_S_W_E"
        if material == "MINERAL":
            return "ORE_VEIN_WALL_N_S_W_E"
        return "STONE_WALL_N_S_W_E_1"
    if shape == "FLOOR":
        if material == "FROZEN_LIQUID":
            return "SMOOTH_ICE_FLOOR" if smooth else "ROUGH_ICE_FLOOR"
        if material in {"POOL", "BROOK", "RIVER"}:
            return "WATER"
        if material == "MAGMA":
            return "MAGMA_1"
        if special == "FURROWED":
            return "FURROWED_SOIL_1"
        if material == "CONSTRUCTION" or smooth:
            return "SMOOTH_FLOOR"
        return "GRASS_5" if family == "GRASS" else f"{family}_FLOOR_5"
    if shape.startswith("STAIR_"):
        return f"{family}_{shape}"
    if shape == "RAMP":
        return f"{'SOIL' if family == 'DIRT' else family}_RAMP_WITH_WALL_N_S_E_W"
    return {"SHRUB": "SHRUB", "SAPLING": "SAPLING", "TREE_SHAPE": "TREE_TRUNK_PILLAR",
            "BRANCH": "TREE_BRANCH_NSWE", "TRUNK_BRANCH": "TREE_TRUNK_NSWE", "TWIG": "TREE_TWIGS_FULL1",
            "FORTIFICATION": "FORTIFICATION", "BOULDER": "BOULDER",
            "PEBBLES": "PEBBLES_FLOOR_5", "BROOK_BED": "WATER", "BROOK_TOP": "WATER"}.get(shape)


SPRITES = {
    sprite for shape in ("WALL", "FLOOR", "STAIR_UP", "STAIR_DOWN", "STAIR_UPDOWN", "RAMP",
                         "FORTIFICATION", "BOULDER", "PEBBLES", "BROOK_BED", "BROOK_TOP",
                         "SHRUB", "SAPLING", "TREE_SHAPE", "BRANCH", "TRUNK_BRANCH", "TWIG")
    for material in ("STONE", "SOIL", "GRASS_LIGHT", "CONSTRUCTION", "FROZEN_LIQUID",
                     "TREE_MATERIAL", "MINERAL", "POOL", "MAGMA")
    for special in ("NORMAL", "SMOOTH", "FURROWED")
    if (sprite := terrain_sprite(shape, material, special))
} | {"WATER", "MAGMA_1"}


BUILDING_SPRITES = {
    "bed": "ITEM_BED", "chair": "ITEM_CHAIR", "table": "ITEM_TABLE", "coffin": "ITEM_COFFIN",
    "door": "ITEM_DOOR_STONE", "hatch": "ITEM_HATCH_COVER_STONE", "floodgate": "ITEM_FLOODGATE_STONE",
    "cabinet": "ITEM_CABINET", "box": "ITEM_BOX", "statue": "ITEM_STATUE_ITEM", "slab": "ITEM_SLAB_BLANK",
    "weaponrack": "ITEM_WEAPON_RACK_STONE_EMPTY", "armorstand": "ITEM_ARMOR_STAND_STONE_EMPTY", "cage": "ITEM_CAGE_WOOD",
    "well": "BLD_WELL", "support": "BLD_SUPPORT_STONE", "stockpile": "STOCKPILE_FLOOR",
    "farmplot": "FARMPLOT", "tradedepot": "TRADE_DEPOT", "bookcase": "ITEM_TOOL_BOOKCASE",
    "tractionbench": "ITEM_TRACTION_BENCH_STONE_ROPE", "gratefloor": "ITEM_GRATE_STONE_FLOOR_CLOSED",
    "gratewall": "ITEM_GRATE_STONE_WALL_CLOSED", "windowglass": "ITEM_WINDOW_GLASS", "windowgem": "ITEM_WINDOW_GEM",
    "displayfurniture": "ITEM_TOOL_PEDESTAL", "offeringplace": "ITEM_TOOL_ALTAR",
    "gearassembly": "GEAR_ASSEMBLY_1", "axlehorizontal": "AXLE_HORIZONTAL_WE_1", "axlevertical": "AXLE_VERTICAL_1",
    "bridge": "BLD_BRIDGE_WOOD_NS_CENTER", "waterwheel": "WATER_WHEEL", "windmill": "WINDMILL_S_1",
    "screwpump": "SCREWPUMP", "traptrackstop": "TRACK_STOP_STONE_NS",
    "nestbox": "ITEM_TOOL_NEST_BOX", "hive": "ITEM_TOOL_HIVE", "animaltrap": "ITEM_ANIMAL_TRAP_WOOD",
    "roaddirt": "BLD_DIRT_ROAD", "roadpaved": "BLD_PAVED_ROAD",
    "siegeenginecatapult": "CATAPULT_N", "siegeengineballista": "BALLISTA_N",
    "siegeengineboltthrower": "BOLT_THROWER_READY_N",
    "constructionwall": "ROCK_BLOCKS_WALL_N_S_W_E", "constructionfloor": "SMOOTH_FLOOR",
    "constructionfortification": "FORTIFICATION", "constructionupstair": "STONE_STAIR_UP",
    "constructiondownstair": "STONE_STAIR_DOWN", "constructionupdownstair": "STONE_STAIR_UPDOWN",
    "constructionramp": "STONE_RAMP_WITH_WALL_N_S_E_W",
    "barsfloor": "BLD_FLOOR_BARS", "barsvertical": "BLD_VERTICAL_BARS_W", "chain": "BLD_CHAIN_ROPE",
    "archerytarget": "BLD_ARCHERY_TARGET_WOOD", "traplever": "LEVER_SET", "trappressureplate": "TRAP_PLATE_READY",
    "trapcagetrap": "TRAP_CAGE", "trapstonefalltrap": "TRAP_STONE", "trapweapontrap": "TRAP_WEAPON",
    "workshopcarpenters": "WORKSHOP_CARPENTER", "workshopmasons": "WORKSHOP_MASON",
    "workshopcraftsdwarfs": "WORKSHOP_CRAFTS", "workshopmechanics": "WORKSHOP_MECHANIC",
    "workshopjewelers": "WORKSHOP_JEWELER", "workshopbowyers": "WORKSHOP_BOWYER",
    "workshopmetalsmithsforge": "WORKSHOP_METALSMITH", "workshopmagmaforge": "WORKSHOP_MAGMAFORGE",
    "workshopleatherworks": "WORKSHOP_LEATHER", "workshopclothiers": "WORKSHOP_CLOTHES",
    "workshopfarmers": "WORKSHOP_FARMER", "workshopfishery": "WORKSHOP_FISHERY",
    "workshopbutchers": "WORKSHOP_BUTCHER", "workshoptanners": "WORKSHOP_TANNER",
    "workshopstill": "WORKSHOP_STILL", "workshoploom": "WORKSHOP_LOOM", "workshopkitchen": "WORKSHOP_KITCHEN",
    "workshopdyers": "WORKSHOP_DYER", "workshopashery": "WORKSHOP_ASHERY", "workshopquern": "WORKSHOP_QUERN",
    "workshopmillstone": "WORKSHOP_MILLSTONE", "workshopkennels": "WORKSHOP_KENNEL",
    "workshopsiege": "WORKSHOP_SIEGE", "furnacewoodfurnace": "FURNACE_WOOD",
    "furnacesmelter": "FURNACE_SMELTER", "furnaceglassfurnace": "FURNACE_GLASS", "furnacekiln": "FURNACE_KILN",
    "furnacemagmasmelter": "FURNACE_SMELTER_LAVA", "furnacemagmaglassfurnace": "FURNACE_GLASS_LAVA",
    "furnacemagmakiln": "FURNACE_KILN_LAVA", "workshopcustomscrewpress": "WORKSHOP_CUSTOM:SCREW_PRESS",
    "workshopcustomsoapmaker": "WORKSHOP_CUSTOM:SOAP_MAKER",
    "workshopscrewpress": "WORKSHOP_CUSTOM:SCREW_PRESS", "workshopsoapmaker": "WORKSHOP_CUSTOM:SOAP_MAKER",
}


@lru_cache(maxsize=512)
def building_sprite(definition: str) -> str | None:
    return BUILDING_SPRITES.get(re.sub(r"[^a-z0-9]", "", definition.lower()))


@lru_cache(maxsize=2048)
def item_sprite(definition: str) -> str:
    kind, _, subtype = definition.partition("/")
    if subtype.startswith("ITEM_"):
        return subtype
    return {"BOX": "ITEM_BAG_EMPTY" if subtype == "BAG" else "ITEM_BOX", "BARREL": "ITEM_BARREL_WOOD_EMPTY",
            "BUCKET": "ITEM_BUCKET_WOOD", "CAGE": "ITEM_CAGE_WOOD", "DOOR": "ITEM_DOOR_STONE",
            "GEM": "ITEM_ROUGH_GEM", "SLAB": "ITEM_SLAB_BLANK", "STATUE": "ITEM_STATUE_ITEM",
            "ARMORSTAND": "ITEM_ARMOR_STAND_STONE_EMPTY", "WEAPONRACK": "ITEM_WEAPON_RACK_STONE_EMPTY"
            }.get(kind, kind if kind.startswith("ITEM_") else f"ITEM_{kind}")


class GameTileset:
    def __init__(self, df_dir: Path):
        self.root = (df_dir / "data/vanilla/vanilla_environment/graphics").resolve()
        self.vanilla = (df_dir / "data/vanilla").resolve()
        self.files: dict[str, Path] = {}
        self.manifest: dict = {"available": False, "pages": {}, "sprites": {}}
        self._loaded = False
        self.creatures: dict[tuple[str, str, str], list[str]] = {}
        self.layers: dict[tuple[str, str], list[dict]] = {}
        self.layout_stages: dict[str, int] = {}
        self._unit_cache: dict[tuple, tuple[str, ...]] = {}

    def load(self) -> dict:
        if self._loaded:
            return self.manifest
        self._loaded = True
        pages = {}
        sources = []
        for package in ("vanilla_environment", "vanilla_items_graphics", "vanilla_buildings_graphics",
                        "vanilla_plants_graphics", "vanilla_creatures_graphics", "vanilla_creatures_extinct_graphics"):
            root = (self.vanilla / package / "graphics").resolve()
            if self.vanilla not in root.parents:
                continue
            for filename in sorted(root.glob("*.txt")):
                if root not in filename.resolve().parents:
                    continue
                try:
                    sources.append((root, filename.read_text(encoding="utf-8")))
                except (OSError, UnicodeError):
                    continue
        for root, source in sources:
            self._read_pages(root, source, pages)
        for _, source in sources:
            for fields in re.findall(r"\[(?:TILE|TOOL|ARMOR|HELM|PANTS|SHOES|GLOVES|SHIELD|AMMO)_GRAPHICS:([^\]]+)\]", source):
                parts = fields.split(":")
                if len(parts) < 4:
                    continue
                page, column, row, *tokens = parts
                token = ":".join(tokens)
                if (token in SPRITES or token.startswith(("ITEM_", "BLD_", "WORKSHOP_", "FURNACE_", "TRADE_DEPOT",
                        "TRAP_", "LEVER_", "STOCKPILE_", "FLOW_", "TREE_", "FARMPLOT", "AXLE_", "GEAR_",
                        "WINDMILL", "WATER_WHEEL", "SCREWPUMP", "BRIDGE_", "TRACK_", "ROLLER_",
                        "STONE_WALL_", "SMOOTHED_STONE_WALL_", "ROCK_BLOCKS_WALL_", "ICE_WALL_",
                        "SMOOTHED_ICE_WALL_", "SOIL_WALL_", "ORE_VEIN_WALL_", "FORTIFICATION_",
                        "STONE_RAMP_WITH_WALL_", "SOIL_RAMP_WITH_WALL_", "GRASS_RAMP_WITH_WALL_",
                        "CATAPULT_", "BALLISTA_", "BOLT_THROWER_"))):
                    if self._add_sprite(token, page, column, row, pages):
                        self._add_layout(tokens, token)
            self._read_creatures(source, pages)
            self._read_item_graphics(source, pages)
        for prefix in ("WORKSHOP_QUERN", "WORKSHOP_MILLSTONE"):
            entry = self.manifest["sprites"].get(f"{prefix}:1:0")
            if entry:
                self.manifest["sprites"][prefix] = entry
        self.manifest["available"] = bool(self.manifest["sprites"])
        return self.manifest

    def _read_pages(self, root, page_text, pages):
        for section in re.split(r"\[TILE_PAGE:", page_text)[1:]:
            name = section.split("]", 1)[0]
            filename = re.search(r"\[FILE:([^\]]+)\]", section)
            dimensions = re.search(r"\[TILE_DIM:(\d+):(\d+)\]", section)
            if not filename or not dimensions:
                continue
            path = (root / filename[1]).resolve()
            width, height = map(int, dimensions.groups())
            if root not in path.parents or path.suffix.lower() not in {".png", ".bmp"}:
                continue
            if not path.is_file() or width <= 0 or height <= 0:
                continue
            pages[name] = (path, width, height)

    def _add_sprite(self, token, page, column, row, pages, width=1, height=1):
        if page not in pages or not str(column).isdigit() or not str(row).isdigit() or width <= 0 or height <= 0:
            return False
        path, tile_width, tile_height = pages[page]
        self.files[page] = path
        self.manifest["pages"][page] = {"url": f"/tileset/page/{page}", "w": tile_width, "h": tile_height}
        entry = [page, int(column), int(row)]
        if width != 1 or height != 1:
            entry += [width, height]
        self.manifest["sprites"].setdefault(token, entry)
        return True

    def _add_layout(self, parts, token):
        if (parts[0].startswith(("WINDMILL_", "WATER_WHEEL_", "CATAPULT_", "BALLISTA_", "BOLT_THROWER_")) and len(parts) == 3
                and all(value.isdigit() for value in parts[1:])):
            self._machine_layout(parts[0], int(parts[1]), int(parts[2]), token)
            return
        if parts[0].startswith("SCREWPUMP_") and len(parts) == 2 and parts[1].isdigit():
            vertical = parts[0].startswith(("SCREWPUMP_N_", "SCREWPUMP_S_"))
            self._machine_layout(parts[0], 0 if vertical else int(parts[1]), int(parts[1]) if vertical else 0, token)
            return
        if len(parts) < 4 or not all(value.isdigit() for value in parts[-3:]):
            return
        prefix = ":".join(parts[:-3])
        if not prefix.startswith(("WORKSHOP_", "FURNACE_", "TRADE_DEPOT")):
            return
        stage, column, row = map(int, parts[-3:])
        overlay = "_OVERLAY" in prefix
        prefix = prefix.replace("_OVERLAY", "")
        previous = self.layout_stages.get(prefix, -1)
        if stage < previous:
            return
        layouts = self.manifest.setdefault("layouts", {})
        if stage > previous:
            self.layout_stages[prefix] = stage
            layouts[prefix] = {"w": 0, "h": 0, "tiles": {}}
        layout = layouts[prefix]
        layout["w"] = max(layout["w"], column + 1)
        layout["h"] = max(layout["h"], row + 1)
        layers = layout["tiles"].setdefault(f"{column},{row}", [])
        if overlay:
            layers.append(token)
        else:
            layers.insert(0, token)

    def _machine_layout(self, prefix, column, row, token):
        layout = self.manifest.setdefault("layouts", {}).setdefault(prefix, {"w": 0, "h": 0, "tiles": {}})
        layout["w"], layout["h"] = max(layout["w"], column + 1), max(layout["h"], row + 1)
        layout["tiles"][f"{column},{row}"] = [token]

    def _read_item_graphics(self, source, pages):
        parent = None
        for fields in re.findall(r"\[([^\]]+)\]", source):
            parts = fields.split(":")
            if parts[0].endswith("_GRAPHICS"):
                parent = parts[1] if len(parts) == 2 and parts[1].startswith("ITEM_") else None
            elif parent and len(parts) >= 4 and parts[0].endswith("_GRAPHICS_DEFAULT"):
                self._add_sprite(parent, parts[1], parts[2], parts[3], pages)

    def _read_creatures(self, source, pages):
        creature, caste, state = None, "", "DEFAULT"
        current_layer = None
        for fields in re.findall(r"\[([^\]]+)\]", source):
            parts = fields.split(":")
            kind = parts[0]
            if kind in {"CREATURE_GRAPHICS", "CREATURE_CASTE_GRAPHICS"}:
                creature, caste = parts[1], parts[2] if len(parts) > 2 else ""
                state = "DEFAULT"
                current_layer = None
            elif creature and kind in {"DEFAULT", "CHILD", "BABY"} and len(parts) >= 4:
                if kind != "DEFAULT" and parts[-1] not in {"DEFAULT", "AS_IS"}:
                    continue
                page, *coords = parts[1:]
                if coords[0] == "LARGE_IMAGE":
                    if len(coords) < 5 or not all(value.isdigit() for value in coords[1:5]):
                        continue
                    column, row, right, bottom = map(int, coords[1:5])
                    width, height = right - column + 1, bottom - row + 1
                else:
                    column, row, width, height = coords[0], coords[1], 1, 1
                token = f"CREATURE:{creature}:{caste}:{kind}"
                if self._add_sprite(token, page, column, row, pages, width, height):
                    self.creatures.setdefault((creature, caste, kind), [token])
            elif kind == "LAYER_SET":
                state = parts[1]
                current_layer = None
            elif creature and kind == "LAYER" and len(parts) >= 5:
                current_layer = None
                name, page, column, row = parts[1:5]
                selected = (name.startswith("LIGHT_") or name == "SHADOW" or name.endswith("_DEFAULT")
                            or name in {"SHORT_UNKEMPT_DBROWN", "BEARD_SHORT_COMBED_DBROWN", "BODY", "CBODY", "MAIN", "BASE"})
                width, height = 1, 1
                if column == "LARGE_IMAGE" and len(parts) >= 8 and all(value.isdigit() for value in parts[4:8]):
                    column, row, right, bottom = map(int, parts[4:8])
                    width, height = right - column + 1, bottom - row + 1
                if selected and str(column).isdigit() and str(row).isdigit():
                    token = f"LAYER:{creature}:{state}:{len(self.layers.get((creature, state), []))}"
                    if self._add_sprite(token, page, column, row, pages, width, height):
                        current_layer = {"token": token, "name": name, "conditions": []}
                        self.layers.setdefault((creature, state), []).append(current_layer)
            elif creature and kind in {"CONDITION_CASTE", "CONDITION_ITEM_WORN", "SHUT_OFF_IF_ITEM_PRESENT",
                                       "CONDITION_CHILD", "CONDITION_NOT_CHILD"}:
                if current_layer is not None:
                    current_layer["conditions"].append(parts)

    def unit_sprites(self, creature: str, caste: str, equipment=(), state="DEFAULT") -> list[str]:
        self.load()
        equipment = set(equipment)
        cache_key = (creature, caste, tuple(sorted(equipment)), state)
        if cache_key in self._unit_cache:
            return list(self._unit_cache[cache_key])
        if len(self._unit_cache) >= 1024:
            self._unit_cache.clear()
        for key in ((creature, caste, state), (creature, "", state),
                    (creature, caste, "DEFAULT"), (creature, "", "DEFAULT")):
            if key in self.creatures:
                self._unit_cache[cache_key] = tuple(self.creatures[key])
                return self.creatures[key]
        result = []
        for layer in self.layers.get((creature, state), self.layers.get((creature, "DEFAULT"), [])):
            matches = True
            for condition in layer["conditions"]:
                kind, *values = condition
                if kind == "CONDITION_CASTE" and caste not in values:
                    matches = False
                elif kind == "CONDITION_CHILD" and state not in {"CHILD", "BABY"}:
                    matches = False
                elif kind == "CONDITION_NOT_CHILD" and state in {"CHILD", "BABY"}:
                    matches = False
                elif kind == "CONDITION_ITEM_WORN" and not equipment.intersection(values[3:]):
                    matches = False
                elif kind == "SHUT_OFF_IF_ITEM_PRESENT" and equipment.intersection(values[3:]):
                    matches = False
            if matches:
                result.append(layer["token"])
        self._unit_cache[cache_key] = tuple(result)
        return result

    def page(self, name: str) -> Path | None:
        self.load()
        return self.files.get(name)
