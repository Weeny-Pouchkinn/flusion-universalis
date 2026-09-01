#!/usr/bin/env python3
"""
EU4 Culture Painter
===================

Install as:
    <EU4 mod>/tools/culture_painter/culture_painter.py

Run with no arguments. The program discovers the mod root automatically.

Dependencies:
    Pillow
    numpy

Design note
-----------
The editor is split into a generic map/selection layer and a culture-specific
LayerModel. A future ReligionLayerModel can reuse the map renderer, province /
area / region selection, water protection, undo/redo and save workflow.

EU4 does not expose a color = {...} field on culture-group definitions. Instead,
the culture map mode takes group colours positionally from:
    common/region_colors/00_region_colors.txt

This editor keeps the culture-group colour shown in the software synchronized with
that in-game positional palette whenever Save is clicked. Individual culture colours
remain editor-only metadata.

IMPORTANT: culture-group palette indexes are global. A total-conversion mod must not
also load vanilla culture groups, or every custom group is shifted to a later palette
slot. On Save, the editor therefore ensures replace_path="common/cultures" and
replace_path="common/region_colors" in the mod descriptor (and the matching local
.mod launcher descriptor when it can identify it).

The editor reads and writes:
    map/provinces.bmp
    map/definition.csv
    map/default.map
    map/area.txt
    map/region.txt
    history/provinces/*.txt
    common/cultures/*.txt
    common/region_colors/00_region_colors.txt
    localisation/*.yml
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import sys
import traceback
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    import numpy as np
    from PIL import Image, ImageTk
    import tkinter as tk
    from tkinter import colorchooser, messagebox, ttk
except ImportError as exc:
    print("Missing dependency:", exc)
    print("Install with: py -m pip install pillow numpy")
    raise

APP_TITLE = "EU4 Culture Painter"
DATA_FILENAME = "culture_painter_data.json"
MANAGED_CULTURES_FILENAME = "zz_culture_painter_managed.txt"
MANAGED_LOC_FILENAME = "zz_culture_painter_l_english.yml"
REGION_COLORS_FILENAME = "00_region_colors.txt"
# EU4 assigns culture-group colours positionally from common/region_colors.
# Community testing reports that palette entry 0 is skipped for culture groups,
# so group 0 uses palette entry 1, group 1 uses entry 2, etc.
REGION_COLOR_GROUP_OFFSET = 1
MIN_REGION_PALETTE_SIZE = 200
BACKUP_DIRNAME = "backups"
WATER_RGB = (48, 76, 108)
UNASSIGNED_RGB = (112, 112, 112)
BOUNDARY_RGB = (28, 28, 28)

GROUP_RESERVED_BLOCKS = {
    "male_names", "female_names", "dynasty_names", "province", "country",
}


# =============================================================================
# Utility / parsing
# =============================================================================

@dataclass
class Block:
    key: str
    start: int
    open_brace: int
    close_brace: int
    end: int


def read_text(path: Path) -> Tuple[str, str]:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8"


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding, newline="")


def safe_id(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.strip())
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = "unnamed"
    if value[0].isdigit():
        value = "c_" + value
    return value


def pretty_name(identifier: str) -> str:
    return identifier.replace("_", " ").title()


def parse_hex_colour(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
        return "#" + value.lstrip("#").upper()
    pieces = [p for p in re.split(r"[\s,;]+", value) if p]
    if len(pieces) == 3:
        vals = [int(p) for p in pieces]
        if all(0 <= x <= 255 for x in vals):
            return "#{:02X}{:02X}{:02X}".format(*vals)
    raise ValueError("Use #RRGGBB or three RGB values such as 120, 80, 210.")


def hex_to_rgb(value: str) -> Tuple[int, int, int]:
    value = parse_hex_colour(value)
    return tuple(int(value[i:i+2], 16) for i in (1, 3, 5))


def deterministic_colour(identifier: str) -> str:
    # Stable, reasonably saturated editor colour without external dependencies.
    h = 2166136261
    for b in identifier.encode("utf-8"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    r = 55 + ((h >> 16) & 0xFF) % 166
    g = 55 + ((h >> 8) & 0xFF) % 166
    b = 55 + (h & 0xFF) % 166
    return f"#{r:02X}{g:02X}{b:02X}"


def rgb_code(r: int, g: int, b: int) -> int:
    return (r << 16) | (g << 8) | b


REGION_RGB_RE = re.compile(
    r"\{\s*(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\s*\}"
)


def parse_region_colour_palette(text: str) -> List[Tuple[int, int, int]]:
    """
    Read the anonymous RGB tuples used by EU4's common/region_colors palette.

    The parser is intentionally tolerant: it finds { R G B } tuples even if a
    mod has wrapped them in another block or added comments/whitespace.
    """
    # Remove comments first so numbers/braces in comments cannot be mistaken for
    # palette entries. Clausewitz # comments end at the newline.
    clean = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    colours: List[Tuple[int, int, int]] = []
    for match in REGION_RGB_RE.finditer(clean):
        rgb = tuple(int(match.group(i)) for i in (1, 2, 3))
        if all(0 <= channel <= 255 for channel in rgb):
            colours.append(rgb)
    return colours


def palette_fallback_colour(index: int) -> Tuple[int, int, int]:
    """Stable fallback RGB for palette entries the mod does not already have."""
    return hex_to_rgb(deterministic_colour(f"region-palette:{index}"))


def matching_brace(text: str, opening: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    for i in range(opening, len(text)):
        c = text[i]
        if in_comment:
            if c == "\n":
                in_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            continue
        if c == "#":
            in_comment = True
        elif c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("Unmatched '{' in Clausewitz file")


def top_level_blocks(text: str, offset: int = 0) -> List[Block]:
    out: List[Block] = []
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_comment:
            if c == "\n":
                in_comment = False
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == "#":
            in_comment = True
            i += 1
            continue
        if c == '"':
            in_string = True
            i += 1
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and (c.isalnum() or c == "_"):
            start = i
            while i < len(text) and (text[i].isalnum() or text[i] in "_.:-"):
                i += 1
            key = text[start:i]
            j = i
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] == "=":
                j += 1
                while j < len(text) and text[j].isspace():
                    j += 1
                if j < len(text) and text[j] == "{":
                    close = matching_brace(text, j)
                    out.append(Block(key, start + offset, j + offset,
                                     close + offset, close + 1 + offset))
                    i = close + 1
                    continue
            continue
        i += 1
    return out


def bare_tokens_at_depth_zero(text: str) -> List[str]:
    """Return bare tokens/numbers not inside nested braces, strings or comments."""
    tokens: List[str] = []
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_comment:
            if c == "\n":
                in_comment = False
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == "#":
            in_comment = True
            i += 1
            continue
        if c == '"':
            in_string = True
            i += 1
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and (c.isalnum() or c in "_.:-"):
            start = i
            while i < len(text) and (text[i].isalnum() or text[i] in "_.:-"):
                i += 1
            token = text[start:i]
            # Ignore assignment LHS/RHS forms at this level.
            j = i
            while j < len(text) and text[j].isspace():
                j += 1
            if j >= len(text) or text[j] != "=":
                tokens.append(token)
            continue
        i += 1
    return tokens


def extract_named_block(text: str, name: str) -> Optional[str]:
    for block in top_level_blocks(text):
        if block.key == name:
            return text[block.open_brace + 1:block.close_brace]
    return None


def extract_integer_set_from_named_block(text: str, name: str) -> Set[int]:
    body = extract_named_block(text, name)
    if body is None:
        return set()
    return {int(x) for x in re.findall(r"\b\d+\b", body)}


def line_depths(text: str) -> List[int]:
    lines = text.splitlines(keepends=True)
    result: List[int] = []
    depth = 0
    in_string = False
    escaped = False
    for line in lines:
        result.append(depth)
        in_comment = False
        for c in line:
            if in_comment:
                continue
            if in_string:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_string = False
                continue
            if c == "#":
                in_comment = True
            elif c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth = max(0, depth - 1)
    return result


def set_top_level_assignment(path: Path, key: str, value: str) -> bool:
    text, enc = read_text(path)
    lines = text.splitlines(keepends=True)
    depths = line_depths(text)
    pat = re.compile(
        rf"^(?P<indent>\s*){re.escape(key)}\s*=\s*(?P<value>[^#\r\n]*?)"
        rf"(?P<comment>\s*#.*)?(?P<newline>\r?\n)?$"
    )
    for i, (line, depth) in enumerate(zip(lines, depths)):
        if depth != 0:
            continue
        m = pat.match(line)
        if not m:
            continue
        newline = m.group("newline") or ("\n" if line.endswith("\n") else "")
        replacement = f"{m.group('indent')}{key} = {value}{m.group('comment') or ''}{newline}"
        if replacement == line:
            return False
        lines[i] = replacement
        write_text(path, "".join(lines), enc)
        return True
    insert_at = 0
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            insert_at = i + 1
        else:
            break
    lines.insert(insert_at, f"{key} = {value}\n")
    write_text(path, "".join(lines), enc)
    return True


def read_top_level_assignment(path: Path, key: str) -> Optional[str]:
    text, _ = read_text(path)
    lines = text.splitlines(keepends=False)
    depths = line_depths(text)
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=\s*([^#\s]+)")
    for line, depth in zip(lines, depths):
        if depth == 0:
            m = pat.match(line)
            if m:
                return m.group(1).strip()
    return None


# =============================================================================
# EU4 map data
# =============================================================================

@dataclass
class MapData:
    mod_root: Path
    width: int
    height: int
    province_raster: np.ndarray
    province_ids: List[int]
    water_provinces: Set[int]
    province_to_area: Dict[int, str]
    area_to_provinces: Dict[str, Set[int]]
    area_to_region: Dict[str, str]
    region_to_provinces: Dict[str, Set[int]]
    province_history: Dict[int, Path]
    _unit_rasters: Dict[str, np.ndarray] = field(default_factory=dict)
    _boundary_masks: Dict[str, np.ndarray] = field(default_factory=dict)

    @classmethod
    def load(cls, mod_root: Path, status=None) -> "MapData":
        def say(msg: str):
            if status:
                status(msg)

        map_dir = mod_root / "map"
        provinces_path = map_dir / "provinces.bmp"
        definition_path = map_dir / "definition.csv"
        default_map_path = map_dir / "default.map"
        area_path = map_dir / "area.txt"
        region_path = map_dir / "region.txt"

        required = [provinces_path, definition_path, default_map_path, area_path, region_path]
        missing = [p for p in required if not p.exists()]
        if missing:
            raise FileNotFoundError("Missing required EU4 map files:\n" + "\n".join(str(x) for x in missing))

        say("Reading definition.csv…")
        colour_to_id: Dict[int, int] = {}
        raw = definition_path.read_bytes()
        decoded = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                decoded = raw.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        if decoded is None:
            decoded = raw.decode("latin-1", errors="replace")
        for row in csv.reader(decoded.splitlines(), delimiter=";"):
            if len(row) < 4:
                continue
            try:
                pid, r, g, b = map(int, row[:4])
            except ValueError:
                continue
            colour_to_id[rgb_code(r, g, b)] = pid

        say("Reading provinces.bmp…")
        with Image.open(provinces_path) as im:
            im = im.convert("RGB")
            arr = np.asarray(im, dtype=np.uint32)
        h, w = arr.shape[:2]
        codes = (arr[:, :, 0] << 16) | (arr[:, :, 1] << 8) | arr[:, :, 2]

        # Vectorised RGB -> province ID conversion.
        keys = np.array(sorted(colour_to_id.keys()), dtype=np.uint32)
        vals = np.array([colour_to_id[int(k)] for k in keys], dtype=np.int32)
        flat = codes.ravel()
        idx = np.searchsorted(keys, flat)
        valid = idx < keys.size
        if keys.size:
            valid &= keys[np.minimum(idx, keys.size - 1)] == flat
        out = np.zeros(flat.shape, dtype=np.int32)
        out[valid] = vals[idx[valid]]
        province_raster = out.reshape((h, w))

        say("Reading water provinces…")
        default_text, _ = read_text(default_map_path)
        water = extract_integer_set_from_named_block(default_text, "sea_starts")
        water |= extract_integer_set_from_named_block(default_text, "lakes")

        say("Reading areas…")
        area_text, _ = read_text(area_path)
        province_to_area: Dict[int, str] = {}
        area_to_provinces: Dict[str, Set[int]] = {}
        for block in top_level_blocks(area_text):
            body = area_text[block.open_brace + 1:block.close_brace]
            pids = {int(t) for t in bare_tokens_at_depth_zero(body) if t.isdigit()}
            if not pids:
                continue
            area_to_provinces[block.key] = pids
            for pid in pids:
                province_to_area[pid] = block.key

        say("Reading regions…")
        region_text, _ = read_text(region_path)
        area_to_region: Dict[str, str] = {}
        region_to_provinces: Dict[str, Set[int]] = {}
        for region_block in top_level_blocks(region_text):
            inner_start = region_block.open_brace + 1
            inner = region_text[inner_start:region_block.close_brace]
            areas: Set[str] = set()
            for child in top_level_blocks(inner, offset=inner_start):
                if child.key != "areas":
                    continue
                body = region_text[child.open_brace + 1:child.close_brace]
                areas.update(t for t in bare_tokens_at_depth_zero(body) if not t.isdigit())
            if not areas:
                continue
            rp: Set[int] = set()
            for area in areas:
                area_to_region[area] = region_block.key
                rp |= area_to_provinces.get(area, set())
            region_to_provinces[region_block.key] = rp

        say("Indexing province history…")
        history_dir = mod_root / "history" / "provinces"
        province_history: Dict[int, Path] = {}
        if history_dir.exists():
            for p in history_dir.glob("*.txt"):
                m = re.match(r"^(\d+)\b", p.stem)
                if m:
                    province_history[int(m.group(1))] = p

        province_ids = sorted(int(x) for x in np.unique(province_raster) if int(x) > 0)
        return cls(mod_root, w, h, province_raster, province_ids, water,
                   province_to_area, area_to_provinces, area_to_region,
                   region_to_provinces, province_history)

    def selection_for(self, province_id: int, scope: str) -> Set[int]:
        if province_id <= 0 or province_id in self.water_provinces:
            return set()
        if scope == "Province":
            return {province_id}
        if scope == "Area":
            area = self.province_to_area.get(province_id)
            pids = self.area_to_provinces.get(area, {province_id}) if area else {province_id}
        elif scope == "Region":
            area = self.province_to_area.get(province_id)
            region = self.area_to_region.get(area) if area else None
            pids = self.region_to_provinces.get(region, {province_id}) if region else {province_id}
        else:
            pids = {province_id}
        return {p for p in pids if p not in self.water_provinces}

    def unit_raster(self, scope: str) -> np.ndarray:
        if scope in self._unit_rasters:
            return self._unit_rasters[scope]
        if scope == "Province":
            raster = self.province_raster
        else:
            mapping: Dict[int, int] = {}
            next_id = 1
            if scope == "Area":
                names = sorted(self.area_to_provinces)
                name_to_num = {n: i + 1 for i, n in enumerate(names)}
                for pid, name in self.province_to_area.items():
                    mapping[pid] = name_to_num[name]
            else:
                names = sorted(self.region_to_provinces)
                name_to_num = {n: i + 1 for i, n in enumerate(names)}
                for pid, area in self.province_to_area.items():
                    region = self.area_to_region.get(area)
                    if region:
                        mapping[pid] = name_to_num[region]
            max_pid = max(self.province_ids, default=0)
            lut = np.zeros(max_pid + 1, dtype=np.int32)
            for pid, uid in mapping.items():
                if pid <= max_pid:
                    lut[pid] = uid
            clipped = np.minimum(self.province_raster, max_pid)
            raster = lut[clipped]
        self._unit_rasters[scope] = raster
        return raster

    def boundary_mask(self, scope: str) -> np.ndarray:
        if scope in self._boundary_masks:
            return self._boundary_masks[scope]
        u = self.unit_raster(scope)
        mask = np.zeros(u.shape, dtype=bool)
        mask[:, 1:] |= u[:, 1:] != u[:, :-1]
        mask[1:, :] |= u[1:, :] != u[:-1, :]
        # Do not draw empty-to-empty boundaries.
        mask &= self.province_raster > 0
        self._boundary_masks[scope] = mask
        return mask


# =============================================================================
# Culture model / extensible LayerModel
# =============================================================================

@dataclass
class GroupInfo:
    id: str
    loc_name: str
    colour: str
    source_path: Optional[Path] = None
    original: bool = True


@dataclass
class ItemInfo:
    id: str
    loc_name: str
    colour: str
    group_id: str
    source_path: Optional[Path] = None
    original_group_id: Optional[str] = None
    original: bool = True


class LayerModel(ABC):
    @property
    @abstractmethod
    def assignment_key(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def assignment_for_province(self, province_id: int) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    def save(self, dirty_provinces: Set[int]) -> Path:
        raise NotImplementedError


class CultureLayerModel(LayerModel):
    assignment_key = "culture"

    def __init__(self, mod_root: Path, map_data: MapData):
        self.mod_root = mod_root
        self.map_data = map_data
        self.tool_dir = Path(__file__).resolve().parent
        self.data_path = self.tool_dir / DATA_FILENAME
        self.groups: Dict[str, GroupInfo] = {}
        self.group_order: List[str] = []
        self.items: Dict[str, ItemInfo] = {}
        self.assignments: Dict[int, Optional[str]] = {}
        self.localisation: Dict[str, str] = {}
        self._loaded_region_palette: List[Tuple[int, int, int]] = []
        self._load_localisation()
        self._load_definitions()
        self._load_region_palette()
        self._load_metadata()
        self._load_assignments()

    def _load_localisation(self):
        loc_dir = self.mod_root / "localisation"
        if not loc_dir.exists():
            return
        pattern = re.compile(r'^\s*([A-Za-z0-9_.:-]+):(?:\d+)?\s+"(.*)"\s*$')
        for path in sorted(loc_dir.rglob("*.yml")):
            try:
                text, _ = read_text(path)
            except Exception:
                continue
            for line in text.splitlines():
                m = pattern.match(line)
                if m:
                    self.localisation[m.group(1)] = m.group(2).replace('\\"', '"')

    def _load_definitions(self):
        culture_dir = self.mod_root / "common" / "cultures"
        culture_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(culture_dir.glob("*.txt"), key=lambda p: p.name):
            text, _ = read_text(path)
            for group_block in top_level_blocks(text):
                gid = group_block.key
                if gid not in self.groups:
                    self.groups[gid] = GroupInfo(
                        id=gid,
                        loc_name=self.localisation.get(gid, pretty_name(gid)),
                        colour=deterministic_colour("group:" + gid),
                        source_path=path,
                        original=True,
                    )
                    self.group_order.append(gid)
                inner_start = group_block.open_brace + 1
                inner = text[inner_start:group_block.close_brace]
                for child in top_level_blocks(inner, offset=inner_start):
                    cid = child.key
                    if cid in GROUP_RESERVED_BLOCKS:
                        continue
                    # Heuristic: culture blocks are child blocks that are not known group options.
                    if cid in self.items:
                        continue
                    self.items[cid] = ItemInfo(
                        id=cid,
                        loc_name=self.localisation.get(cid, pretty_name(cid)),
                        colour=deterministic_colour("culture:" + cid),
                        group_id=gid,
                        source_path=path,
                        original_group_id=gid,
                        original=True,
                    )

    def _region_colours_path(self) -> Path:
        return self.mod_root / "common" / "region_colors" / REGION_COLORS_FILENAME

    def _load_region_palette(self):
        """Load actual in-game culture-group colours when the mod has a palette."""
        path = self._region_colours_path()
        if not path.exists():
            return
        try:
            text, _ = read_text(path)
            palette = parse_region_colour_palette(text)
        except Exception:
            return
        self._loaded_region_palette = palette
        for group_index, gid in enumerate(self.group_order):
            palette_index = REGION_COLOR_GROUP_OFFSET + group_index
            if palette_index >= len(palette):
                break
            r, g, b = palette[palette_index]
            self.groups[gid].colour = f"#{r:02X}{g:02X}{b:02X}"

    def _load_metadata(self):
        if not self.data_path.exists():
            return
        try:
            data = json.loads(self.data_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for gid, record in data.get("groups", {}).items():
            if gid in self.groups:
                g = self.groups[gid]
                g.loc_name = str(record.get("loc_name", g.loc_name))
                try:
                    g.colour = parse_hex_colour(str(record.get("colour", g.colour)))
                except ValueError:
                    pass
        for cid, record in data.get("cultures", {}).items():
            if cid in self.items:
                item = self.items[cid]
                item.loc_name = str(record.get("loc_name", item.loc_name))
                try:
                    item.colour = parse_hex_colour(str(record.get("colour", item.colour)))
                except ValueError:
                    pass
            else:
                gid = str(record.get("group_id", ""))
                if not gid:
                    continue
                if gid not in self.groups:
                    self.groups[gid] = GroupInfo(
                        gid,
                        str(data.get("groups", {}).get(gid, {}).get("loc_name", pretty_name(gid))),
                        str(data.get("groups", {}).get(gid, {}).get("colour", deterministic_colour("group:" + gid))),
                        None,
                        False,
                    )
                self.items[cid] = ItemInfo(
                    cid,
                    str(record.get("loc_name", pretty_name(cid))),
                    str(record.get("colour", deterministic_colour("culture:" + cid))),
                    gid,
                    None,
                    None,
                    False,
                )
        # Group membership in metadata is authoritative for unsaved/managed changes.
        for cid, record in data.get("cultures", {}).items():
            if cid in self.items and record.get("group_id"):
                self.items[cid].group_id = str(record["group_id"])

    def _load_assignments(self):
        for pid in self.map_data.province_ids:
            if pid in self.map_data.water_provinces:
                self.assignments[pid] = None
                continue
            path = self.map_data.province_history.get(pid)
            culture = read_top_level_assignment(path, "culture") if path else None
            self.assignments[pid] = culture
            if culture and culture not in self.items:
                # Preserve unknown culture IDs instead of losing them.
                fallback_group = "unindexed_cultures"
                if fallback_group not in self.groups:
                    self.groups[fallback_group] = GroupInfo(
                        fallback_group, "Unindexed Cultures",
                        deterministic_colour("group:" + fallback_group), None, False,
                    )
                self.items[culture] = ItemInfo(
                    culture, self.localisation.get(culture, pretty_name(culture)),
                    deterministic_colour("culture:" + culture), fallback_group,
                    None, None, False,
                )

    def assignment_for_province(self, province_id: int) -> Optional[str]:
        return self.assignments.get(province_id)

    def create_group(self, group_id: str, loc_name: str, colour: str):
        gid = safe_id(group_id)
        if gid in self.groups:
            raise ValueError(f"Culture group '{gid}' already exists.")
        self.groups[gid] = GroupInfo(gid, loc_name.strip() or pretty_name(gid), parse_hex_colour(colour), None, False)
        return gid

    def create_item(self, culture_id: str, loc_name: str, colour: str, group_id: str):
        cid = safe_id(culture_id)
        if cid in self.items:
            raise ValueError(f"Culture '{cid}' already exists.")
        if group_id not in self.groups:
            raise ValueError("Select an existing culture group first.")
        self.items[cid] = ItemInfo(cid, loc_name.strip() or pretty_name(cid), parse_hex_colour(colour), group_id, None, None, False)
        return cid

    def edit_group(self, gid: str, loc_name: str, colour: str):
        g = self.groups[gid]
        g.loc_name = loc_name.strip() or pretty_name(gid)
        g.colour = parse_hex_colour(colour)

    def edit_item(self, cid: str, loc_name: str, colour: str, group_id: str):
        if group_id not in self.groups:
            raise ValueError("Unknown culture group.")
        item = self.items[cid]
        item.loc_name = loc_name.strip() or pretty_name(cid)
        item.colour = parse_hex_colour(colour)
        item.group_id = group_id

    def _metadata_dict(self) -> dict:
        return {
            "version": 4,
            "culture_definition_mode": "replace_vanilla",
            "region_color_sync": {
                "enabled": True,
                "palette_file": f"common/region_colors/{REGION_COLORS_FILENAME}",
                "culture_group_offset": REGION_COLOR_GROUP_OFFSET,
            },
            "groups": {
                gid: {"loc_name": g.loc_name, "colour": g.colour}
                for gid, g in sorted(self.groups.items())
            },
            "cultures": {
                cid: {"loc_name": c.loc_name, "colour": c.colour, "group_id": c.group_id}
                for cid, c in sorted(self.items.items())
            },
        }

    def save_metadata(self):
        self.tool_dir.mkdir(parents=True, exist_ok=True)
        self.data_path.write_text(json.dumps(self._metadata_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _find_group_block(self, gid: str) -> Optional[Tuple[Path, str, str, Block]]:
        culture_dir = self.mod_root / "common" / "cultures"
        for path in sorted(culture_dir.glob("*.txt"), key=lambda p: p.name):
            text, enc = read_text(path)
            for block in top_level_blocks(text):
                if block.key == gid:
                    return path, text, enc, block
        return None

    def _find_culture_block(self, cid: str) -> Optional[Tuple[Path, str, str, Block, str]]:
        culture_dir = self.mod_root / "common" / "cultures"
        for path in sorted(culture_dir.glob("*.txt"), key=lambda p: p.name):
            text, enc = read_text(path)
            for group in top_level_blocks(text):
                inner_start = group.open_brace + 1
                inner = text[inner_start:group.close_brace]
                for child in top_level_blocks(inner, offset=inner_start):
                    if child.key == cid:
                        return path, text, enc, child, group.key
        return None

    def _ensure_group_exists(self, gid: str):
        if self._find_group_block(gid):
            return
        managed = self.mod_root / "common" / "cultures" / MANAGED_CULTURES_FILENAME
        if managed.exists():
            text, enc = read_text(managed)
        else:
            text, enc = "# Managed by EU4 Culture Painter.\n\n", "utf-8"
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\n{gid} = {{\n\tgraphical_culture = westerngfx\n}}\n"
        write_text(managed, text, enc)

    def _insert_culture_into_group(self, gid: str, block_text: str):
        found = self._find_group_block(gid)
        if not found:
            self._ensure_group_exists(gid)
            found = self._find_group_block(gid)
        assert found is not None
        path, text, enc, group = found
        insert = "\n" + "\n".join("\t" + line if line.strip() else line for line in block_text.strip().splitlines()) + "\n"
        new_text = text[:group.close_brace].rstrip() + insert + text[group.close_brace:]
        write_text(path, new_text, enc)

    def _remove_culture_block(self, found: Tuple[Path, str, str, Block, str]) -> str:
        path, text, enc, block, _gid = found
        start = text.rfind("\n", 0, block.start) + 1
        end_nl = text.find("\n", block.end)
        end = len(text) if end_nl == -1 else end_nl + 1
        extracted = text[block.start:block.end]
        write_text(path, text[:start] + text[end:], enc)
        return extracted

    def _sync_definitions(self):
        # First create missing groups.
        for gid in self.groups:
            self._ensure_group_exists(gid)

        # Then create/move cultures. Existing culture block contents are preserved.
        for cid, item in self.items.items():
            found = self._find_culture_block(cid)
            if found is None:
                self._insert_culture_into_group(item.group_id, f"{cid} = {{\n}}")
                continue
            _path, _text, _enc, _block, current_gid = found
            if current_gid != item.group_id:
                block_text = self._remove_culture_block(found)
                self._insert_culture_into_group(item.group_id, block_text)

    def _culture_group_load_order(self) -> List[str]:
        """Return culture-group IDs in the same filename/block order EU4 loads."""
        culture_dir = self.mod_root / "common" / "cultures"
        order: List[str] = []
        seen: Set[str] = set()
        for path in sorted(culture_dir.glob("*.txt"), key=lambda p: p.name):
            text, _ = read_text(path)
            for block in top_level_blocks(text):
                if block.key not in seen:
                    seen.add(block.key)
                    order.append(block.key)
        return order

    def _write_region_colours(self):
        """
        Synchronize software culture-group colours with EU4's positional
        common/region_colors palette.

        Culture-group #0 is written to palette entry 1, because EU4's culture map
        mode skips the first region-colour entry. The rest of an existing palette
        is preserved. If the mod did not previously contain a palette, a generous
        deterministic tail is generated so area/region map modes still have enough
        entries.
        """
        path = self._region_colours_path()
        existing: List[Tuple[int, int, int]] = []
        if path.exists():
            text, _enc = read_text(path)
            existing = parse_region_colour_palette(text)
        elif self._loaded_region_palette:
            existing = list(self._loaded_region_palette)

        group_order = self._culture_group_load_order()
        required = REGION_COLOR_GROUP_OFFSET + len(group_order)
        # Keep enough entries for all of the mod's geography as well. The file is a
        # shared colour pool used by several map modes, so truncating it is unsafe.
        geography_need = max(
            len(self.map_data.area_to_provinces),
            len(self.map_data.region_to_provinces),
            0,
        ) + 1
        target_size = max(len(existing), required, geography_need, MIN_REGION_PALETTE_SIZE)

        palette = list(existing)
        while len(palette) < target_size:
            palette.append(palette_fallback_colour(len(palette)))

        if not palette:
            palette.append((96, 96, 96))

        # Preserve palette entry 0 when it already exists. It is not assigned to a
        # culture group by the engine. For a newly generated file, use a neutral
        # deterministic value.
        if len(existing) == 0:
            palette[0] = palette_fallback_colour(0)

        for group_index, gid in enumerate(group_order):
            if gid not in self.groups:
                continue
            palette_index = REGION_COLOR_GROUP_OFFSET + group_index
            palette[palette_index] = hex_to_rgb(self.groups[gid].colour)

        lines = [
            "# Managed by EU4 Culture Painter.",
            "# EU4 culture-group colours are positional entries in this palette.",
            f"# Palette entry 0 is reserved; culture groups start at entry {REGION_COLOR_GROUP_OFFSET}.",
            "# The comments below do not affect palette indexing.",
            "",
        ]
        group_for_palette_index = {
            REGION_COLOR_GROUP_OFFSET + i: gid
            for i, gid in enumerate(group_order)
        }
        for index, (r, g, b) in enumerate(palette):
            gid = group_for_palette_index.get(index)
            if gid is not None:
                lines.append(f"# culture_group[{index - REGION_COLOR_GROUP_OFFSET}] = {gid}")
            elif index == 0:
                lines.append("# reserved / skipped by culture-group map mode")
            lines.append(f"color = {{ {r} {g} {b} }}")

        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, "\n".join(lines) + "\n", "utf-8")
        self._loaded_region_palette = palette
        self.group_order = group_order

    def _write_localisation(self):
        loc_dir = self.mod_root / "localisation"
        loc_dir.mkdir(parents=True, exist_ok=True)
        path = loc_dir / MANAGED_LOC_FILENAME
        lines = ["l_english:"]
        for gid, group in sorted(self.groups.items()):
            name = group.loc_name.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f' {gid}:0 "{name}"')
        for cid, item in sorted(self.items.items()):
            name = item.loc_name.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f' {cid}:0 "{name}"')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    def _descriptor_files(self) -> List[Path]:
        """
        Return descriptor files which control this mod.

        descriptor.mod in the mod root is authoritative for the launcher.  For a
        local development mod there is commonly also a sibling <modname>.mod file;
        update it too when it can be identified unambiguously so the change works
        even before the launcher regenerates that file.
        """
        out: List[Path] = []
        descriptor = self.mod_root / "descriptor.mod"
        if descriptor.exists():
            out.append(descriptor)

        parent = self.mod_root.parent
        try:
            candidates = list(parent.glob("*.mod"))
        except Exception:
            candidates = []

        root_name = self.mod_root.name.lower()
        root_norm = str(self.mod_root.resolve()).replace("\\", "/").rstrip("/").lower()

        for path in candidates:
            if path.resolve() == descriptor.resolve() if descriptor.exists() else False:
                continue
            try:
                text, _enc = read_text(path)
            except Exception:
                continue

            matched = path.stem.lower() == root_name
            m = re.search(r'(?mi)^\s*path\s*=\s*"([^"]+)"\s*$', text)
            if m:
                declared = m.group(1).replace("\\", "/").rstrip("/").lower()
                # Absolute path, or the common launcher form mod/<folder>.
                if declared == root_norm or declared.endswith("/" + root_name) or declared == root_name:
                    matched = True

            if matched and path not in out:
                out.append(path)

        return out

    @staticmethod
    def _has_replace_path(text: str, value: str) -> bool:
        pattern = re.compile(
            r'(?mi)^\s*replace_path\s*=\s*["\']' + re.escape(value) + r'["\']\s*(?:#.*)?$'
        )
        return bool(pattern.search(text))

    def _ensure_culture_replace_paths(self) -> List[Path]:
        """
        Culture colours are indexed across *all loaded culture groups*.  If vanilla
        groups are still loaded, group #0 in this tool is not group #0 in the game.
        For this total-conversion painter, make the mod's culture and region-colour
        folders replace vanilla so the palette order we write is the order EU4 uses.
        """
        required = ("common/cultures", "common/region_colors")
        changed: List[Path] = []
        descriptors = self._descriptor_files()

        for path in descriptors:
            text, enc = read_text(path)
            missing = [value for value in required if not self._has_replace_path(text, value)]
            if not missing:
                continue

            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n# Managed by EU4 Culture Painter: required for exact culture-group palette indexing.\n"
            for value in missing:
                text += f'replace_path="{value}"\n'
            write_text(path, text, enc)
            changed.append(path)

        if not descriptors:
            raise RuntimeError(
                "No descriptor.mod was found in the mod root. Exact culture-group colours "
                "require replace_path=\"common/cultures\" so vanilla culture groups do not "
                "shift the palette indexes. Create/fix descriptor.mod and save again."
            )

        return changed

    def _backup(self, dirty_provinces: Set[int]) -> Path:
        root = self.tool_dir / BACKUP_DIRNAME / datetime.now().strftime("%Y%m%d_%H%M%S")
        paths: Set[Path] = set()
        for pid in dirty_provinces:
            p = self.map_data.province_history.get(pid)
            if p and p.exists():
                paths.add(p)
        culture_dir = self.mod_root / "common" / "cultures"
        if culture_dir.exists():
            paths.update(culture_dir.glob("*.txt"))
        loc = self.mod_root / "localisation" / MANAGED_LOC_FILENAME
        if loc.exists():
            paths.add(loc)
        region_colours = self._region_colours_path()
        if region_colours.exists():
            paths.add(region_colours)
        if self.data_path.exists():
            paths.add(self.data_path)
        for descriptor in self._descriptor_files():
            if descriptor.exists():
                paths.add(descriptor)
        for p in paths:
            try:
                rel = p.resolve().relative_to(self.mod_root.resolve())
            except ValueError:
                rel = Path("tools") / "culture_painter" / p.name
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
        return root

    def save(self, dirty_provinces: Set[int]) -> Path:
        backup = self._backup(dirty_provinces)
        # This is essential, not cosmetic: if vanilla culture groups remain loaded,
        # EU4 indexes our groups after them and therefore reads the wrong palette slots.
        self._ensure_culture_replace_paths()
        self._sync_definitions()
        self._write_region_colours()
        self._write_localisation()
        for pid in sorted(dirty_provinces):
            culture = self.assignments.get(pid)
            if not culture or pid in self.map_data.water_provinces:
                continue
            path = self.map_data.province_history.get(pid)
            if path is None:
                continue
            set_top_level_assignment(path, "culture", culture)
        self.save_metadata()
        return backup


# =============================================================================
# Generic editor UI
# =============================================================================

@dataclass
class PaintAction:
    before: Dict[int, Optional[str]]
    after: Dict[int, Optional[str]]


class EntityDialog(tk.Toplevel):
    def __init__(self, parent, title: str, id_value: str, loc_value: str,
                 colour_value: str, groups: Optional[Sequence[str]] = None,
                 group_value: Optional[str] = None, id_editable: bool = True,
                 colour_label: str = "Software colour"):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.result = None
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Codename").grid(row=0, column=0, sticky="w", pady=5)
        self.id_var = tk.StringVar(value=id_value)
        id_entry = ttk.Entry(frame, textvariable=self.id_var, width=34)
        id_entry.grid(row=0, column=1, sticky="ew", pady=5)
        if not id_editable:
            id_entry.state(["disabled"])

        ttk.Label(frame, text="Localized name").grid(row=1, column=0, sticky="w", pady=5)
        self.loc_var = tk.StringVar(value=loc_value)
        ttk.Entry(frame, textvariable=self.loc_var, width=34).grid(row=1, column=1, sticky="ew", pady=5)

        ttk.Label(frame, text=colour_label).grid(row=2, column=0, sticky="w", pady=5)
        colour_row = ttk.Frame(frame)
        colour_row.grid(row=2, column=1, sticky="ew", pady=5)
        self.colour_var = tk.StringVar(value=colour_value)
        ttk.Entry(colour_row, textvariable=self.colour_var, width=20).pack(side="left", fill="x", expand=True)
        ttk.Button(colour_row, text="Pick…", command=self.pick_colour).pack(side="left", padx=(6, 0))

        self.group_var = None
        if groups is not None:
            ttk.Label(frame, text="Culture group").grid(row=3, column=0, sticky="w", pady=5)
            self.group_var = tk.StringVar(value=group_value or (groups[0] if groups else ""))
            ttk.Combobox(frame, textvariable=self.group_var, values=list(groups), state="readonly", width=31).grid(row=3, column=1, sticky="ew", pady=5)

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="OK", command=self.ok).pack(side="right", padx=(0, 6))
        self.bind("<Return>", lambda _e: self.ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.wait_visibility()
        self.focus_set()

    def pick_colour(self):
        try:
            initial = parse_hex_colour(self.colour_var.get())
        except Exception:
            initial = "#808080"
        chosen = colorchooser.askcolor(initialcolor=initial, parent=self)[1]
        if chosen:
            self.colour_var.set(chosen.upper())

    def ok(self):
        try:
            colour = parse_hex_colour(self.colour_var.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self)
            return
        raw_id = self.id_var.get().strip()
        if not raw_id:
            messagebox.showerror(APP_TITLE, "Codename cannot be empty.", parent=self)
            return
        self.result = {
            "id": safe_id(raw_id),
            "loc_name": self.loc_var.get().strip() or pretty_name(safe_id(raw_id)),
            "colour": colour,
            "group_id": self.group_var.get() if self.group_var else None,
        }
        self.destroy()


class CulturePainterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1420x850")
        self.root.minsize(980, 650)

        script = Path(__file__).resolve()
        # <mod>/tools/culture_painter/culture_painter.py
        self.mod_root = script.parents[2]
        self.map_data: Optional[MapData] = None
        self.model: Optional[CultureLayerModel] = None
        self.selected_culture: Optional[str] = None
        self.scope_var = tk.StringVar(value="Province")
        self.view_var = tk.StringVar(value="Culture")
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Loading…")
        self.zoom = 0.5
        self.tk_map = None
        self.map_item = None
        self.render_after = None
        self.dirty_provinces: Set[int] = set()
        self.undo_stack: List[PaintAction] = []
        self.redo_stack: List[PaintAction] = []
        self.drag_seen: Set[Tuple[str, int]] = set()
        self.hover_pid = 0

        self._build_ui()
        self.root.after(50, self._load)

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(8, 7))
        top.pack(fill="x")
        ttk.Button(top, text="Save", command=self.save).pack(side="left")
        ttk.Button(top, text="Undo", command=self.undo).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Redo", command=self.redo).pack(side="left", padx=(6, 14))
        ttk.Label(top, text="Paint:").pack(side="left")
        scope = ttk.Combobox(top, textvariable=self.scope_var,
                             values=["Province", "Area", "Region"], state="readonly", width=11)
        scope.pack(side="left", padx=(5, 14))
        scope.bind("<<ComboboxSelected>>", lambda _e: self.schedule_render())
        ttk.Label(top, text="View:").pack(side="left")
        view = ttk.Combobox(top, textvariable=self.view_var,
                            values=["Culture", "Culture Group"], state="readonly", width=15)
        view.pack(side="left", padx=(5, 14))
        view.bind("<<ComboboxSelected>>", lambda _e: self.schedule_render())
        ttk.Button(top, text="Zoom −", command=lambda: self.change_zoom(0.8)).pack(side="left")
        ttk.Button(top, text="Zoom +", command=lambda: self.change_zoom(1.25)).pack(side="left", padx=(5, 0))
        ttk.Label(top, textvariable=self.status_var).pack(side="right")

        paned = ttk.Panedwindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True)

        sidebar = ttk.Frame(paned, padding=8, width=320)
        map_frame = ttk.Frame(paned)
        paned.add(sidebar, weight=0)
        paned.add(map_frame, weight=1)

        ttk.Label(sidebar, text="Cultures", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        search = ttk.Entry(sidebar, textvariable=self.search_var)
        search.pack(fill="x", pady=(6, 6))
        search.bind("<KeyRelease>", lambda _e: self.refresh_tree())

        self.tree = ttk.Treeview(sidebar, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.tree_selected)
        self.tree.bind("<Double-1>", lambda _e: self.edit_selected())

        buttons = ttk.Frame(sidebar)
        buttons.pack(fill="x", pady=(7, 0))
        ttk.Button(buttons, text="New group", command=self.new_group).pack(side="left")
        ttk.Button(buttons, text="New culture", command=self.new_culture).pack(side="left", padx=(5, 0))
        ttk.Button(buttons, text="Edit", command=self.edit_selected).pack(side="left", padx=(5, 0))

        self.selection_label = ttk.Label(sidebar, text="Select a culture to paint.", wraplength=290)
        self.selection_label.pack(fill="x", pady=(8, 0))

        xbar = ttk.Scrollbar(map_frame, orient="horizontal")
        ybar = ttk.Scrollbar(map_frame, orient="vertical")
        self.canvas = tk.Canvas(map_frame, background="#1e1e1e", xscrollcommand=xbar.set,
                                yscrollcommand=ybar.set, highlightthickness=0)
        xbar.config(command=self.canvas.xview)
        ybar.config(command=self.canvas.yview)
        xbar.pack(side="bottom", fill="x")
        ybar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.bind("<Button-1>", self.paint_press)
        self.canvas.bind("<B1-Motion>", self.paint_drag)
        self.canvas.bind("<ButtonRelease-1>", self.paint_release)
        self.canvas.bind("<Motion>", self.hover)
        self.canvas.bind("<MouseWheel>", self.mousewheel)
        self.canvas.bind("<Button-4>", lambda _e: self.change_zoom(1.15))
        self.canvas.bind("<Button-5>", lambda _e: self.change_zoom(0.87))
        self.canvas.bind("<Button-2>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B2-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<Button-3>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B3-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))

    def _load(self):
        try:
            self.map_data = MapData.load(self.mod_root, self._set_status)
            self._set_status("Reading cultures…")
            self.model = CultureLayerModel(self.mod_root, self.map_data)
            self.refresh_tree()
            self.zoom = min(1.0, max(0.15, 1000 / max(1, self.map_data.width)))
            self.schedule_render()
            self._set_status("Ready")
        except Exception as exc:
            log = Path(__file__).resolve().parent / "culture_painter_error.log"
            log.write_text(traceback.format_exc(), encoding="utf-8")
            messagebox.showerror(APP_TITLE, f"Failed to load the mod:\n\n{exc}\n\nSee {log.name} for details.")
            self._set_status("Load failed")

    def _set_status(self, text: str):
        self.status_var.set(text)
        self.root.update_idletasks()

    def refresh_tree(self):
        if not self.model:
            return
        query = self.search_var.get().strip().lower()
        selected = self.selected_culture
        self.tree.delete(*self.tree.get_children())
        for gid, group in sorted(self.model.groups.items(), key=lambda kv: kv[1].loc_name.lower()):
            cultures = [c for c in self.model.items.values() if c.group_id == gid]
            cultures.sort(key=lambda c: c.loc_name.lower())
            group_match = query in gid.lower() or query in group.loc_name.lower()
            visible_cultures = [c for c in cultures if not query or group_match or query in c.id.lower() or query in c.loc_name.lower()]
            if query and not group_match and not visible_cultures:
                continue
            gnode = self.tree.insert("", "end", iid="g:" + gid,
                                     text=f"{group.loc_name}  [{gid}]", open=bool(query))
            for c in visible_cultures:
                self.tree.insert(gnode, "end", iid="c:" + c.id,
                                 text=f"{c.loc_name}  [{c.id}]")
        if selected and self.tree.exists("c:" + selected):
            self.tree.selection_set("c:" + selected)
            self.tree.see("c:" + selected)

    def tree_selected(self, _event=None):
        sel = self.tree.selection()
        if not sel or not self.model:
            return
        iid = sel[0]
        if iid.startswith("c:"):
            cid = iid[2:]
            self.selected_culture = cid
            c = self.model.items[cid]
            g = self.model.groups.get(c.group_id)
            self.selection_label.config(
                text=f"Brush: {c.loc_name} [{cid}]\nGroup: {g.loc_name if g else c.group_id}"
            )
        else:
            self.selected_culture = None
            self.selection_label.config(text="A culture group is selected. Select a culture beneath it to paint.")

    def new_group(self):
        if not self.model:
            return
        dlg = EntityDialog(
            self.root, "New culture group", "", "", "#808080",
            id_editable=True, colour_label="Group colour (editor + in-game)"
        )
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        try:
            self.model.create_group(dlg.result["id"], dlg.result["loc_name"], dlg.result["colour"])
            self.refresh_tree()
            self.schedule_render()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def new_culture(self):
        if not self.model:
            return
        groups = sorted(self.model.groups)
        if not groups:
            messagebox.showinfo(APP_TITLE, "Create a culture group first.")
            return
        default_group = groups[0]
        sel = self.tree.selection()
        if sel:
            iid = sel[0]
            if iid.startswith("g:"):
                default_group = iid[2:]
            elif iid.startswith("c:"):
                default_group = self.model.items[iid[2:]].group_id
        dlg = EntityDialog(self.root, "New culture", "", "", "#808080",
                           groups=groups, group_value=default_group, id_editable=True)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        try:
            cid = self.model.create_item(dlg.result["id"], dlg.result["loc_name"],
                                         dlg.result["colour"], dlg.result["group_id"])
            self.selected_culture = cid
            self.refresh_tree()
            self.schedule_render()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def edit_selected(self):
        if not self.model:
            return
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("g:"):
            gid = iid[2:]
            g = self.model.groups[gid]
            dlg = EntityDialog(
                self.root, "Edit culture group", gid, g.loc_name, g.colour,
                id_editable=False, colour_label="Group colour (editor + in-game)"
            )
            self.root.wait_window(dlg)
            if dlg.result:
                try:
                    self.model.edit_group(gid, dlg.result["loc_name"], dlg.result["colour"])
                    self.refresh_tree(); self.schedule_render()
                except Exception as exc:
                    messagebox.showerror(APP_TITLE, str(exc))
        elif iid.startswith("c:"):
            cid = iid[2:]
            c = self.model.items[cid]
            dlg = EntityDialog(self.root, "Edit culture", cid, c.loc_name, c.colour,
                               groups=sorted(self.model.groups), group_value=c.group_id,
                               id_editable=False)
            self.root.wait_window(dlg)
            if dlg.result:
                try:
                    self.model.edit_item(cid, dlg.result["loc_name"], dlg.result["colour"], dlg.result["group_id"])
                    self.refresh_tree(); self.schedule_render()
                except Exception as exc:
                    messagebox.showerror(APP_TITLE, str(exc))

    def province_at_event(self, event) -> int:
        if not self.map_data:
            return 0
        x = int(self.canvas.canvasx(event.x) / self.zoom)
        y = int(self.canvas.canvasy(event.y) / self.zoom)
        if not (0 <= x < self.map_data.width and 0 <= y < self.map_data.height):
            return 0
        return int(self.map_data.province_raster[y, x])

    def paint_press(self, event):
        self.drag_seen.clear()
        self._paint_event(event)

    def paint_drag(self, event):
        self._paint_event(event)

    def paint_release(self, _event):
        self.drag_seen.clear()

    def _paint_event(self, event):
        if not self.model or not self.map_data or not self.selected_culture:
            return
        pid = self.province_at_event(event)
        if pid <= 0 or pid in self.map_data.water_provinces:
            return
        scope = self.scope_var.get()
        marker = (scope, pid if scope == "Province" else hash(tuple(sorted(self.map_data.selection_for(pid, scope)))))
        if marker in self.drag_seen:
            return
        self.drag_seen.add(marker)
        targets = self.map_data.selection_for(pid, scope)
        if not targets:
            return
        before = {p: self.model.assignments.get(p) for p in targets}
        after = {p: self.selected_culture for p in targets}
        if all(before[p] == self.selected_culture for p in targets):
            return
        for p in targets:
            self.model.assignments[p] = self.selected_culture
            self.dirty_provinces.add(p)
        self.undo_stack.append(PaintAction(before, after))
        self.redo_stack.clear()
        self.schedule_render()

    def hover(self, event):
        if not self.model or not self.map_data:
            return
        pid = self.province_at_event(event)
        if pid == self.hover_pid:
            return
        self.hover_pid = pid
        if pid <= 0:
            self._set_status("Outside province map")
            return
        if pid in self.map_data.water_provinces:
            self._set_status(f"Province {pid} — water (locked)")
            return
        culture = self.model.assignments.get(pid)
        label = self.model.items[culture].loc_name if culture in self.model.items else (culture or "No culture")
        area = self.map_data.province_to_area.get(pid, "—")
        region = self.map_data.area_to_region.get(area, "—") if area != "—" else "—"
        self._set_status(f"Province {pid} | {label} | {area} | {region}")

    def undo(self):
        if not self.model or not self.undo_stack:
            return
        action = self.undo_stack.pop()
        for pid, value in action.before.items():
            self.model.assignments[pid] = value
            self.dirty_provinces.add(pid)
        self.redo_stack.append(action)
        self.schedule_render()

    def redo(self):
        if not self.model or not self.redo_stack:
            return
        action = self.redo_stack.pop()
        for pid, value in action.after.items():
            self.model.assignments[pid] = value
            self.dirty_provinces.add(pid)
        self.undo_stack.append(action)
        self.schedule_render()

    def change_zoom(self, factor: float):
        if not self.map_data:
            return
        self.zoom = max(0.08, min(4.0, self.zoom * factor))
        self.schedule_render(immediate=True)

    def mousewheel(self, event):
        self.change_zoom(1.15 if event.delta > 0 else 0.87)
        return "break"

    def schedule_render(self, immediate=False):
        if self.render_after is not None:
            try:
                self.root.after_cancel(self.render_after)
            except Exception:
                pass
        self.render_after = self.root.after(1 if immediate else 45, self.render)

    def render(self):
        self.render_after = None
        if not self.map_data or not self.model:
            return
        self._set_status("Rendering…")
        max_pid = max(self.map_data.province_ids, default=0)
        lut = np.zeros((max_pid + 1, 3), dtype=np.uint8)
        lut[:] = UNASSIGNED_RGB
        water = self.map_data.water_provinces
        view_group = self.view_var.get() == "Culture Group"
        for pid in self.map_data.province_ids:
            if pid > max_pid:
                continue
            if pid in water:
                lut[pid] = WATER_RGB
                continue
            cid = self.model.assignments.get(pid)
            if cid and cid in self.model.items:
                item = self.model.items[cid]
                colour = self.model.groups[item.group_id].colour if view_group and item.group_id in self.model.groups else item.colour
                lut[pid] = hex_to_rgb(colour)
            else:
                lut[pid] = UNASSIGNED_RGB
        clipped = np.minimum(self.map_data.province_raster, max_pid)
        rgb = lut[clipped].copy()
        rgb[self.map_data.province_raster == 0] = (20, 20, 20)
        boundary = self.map_data.boundary_mask(self.scope_var.get())
        rgb[boundary] = BOUNDARY_RGB
        image = Image.fromarray(rgb, mode="RGB")
        size = (max(1, int(self.map_data.width * self.zoom)),
                max(1, int(self.map_data.height * self.zoom)))
        if size != image.size:
            image = image.resize(size, Image.Resampling.NEAREST)
        self.tk_map = ImageTk.PhotoImage(image)
        if self.map_item is None:
            self.map_item = self.canvas.create_image(0, 0, image=self.tk_map, anchor="nw")
        else:
            self.canvas.itemconfigure(self.map_item, image=self.tk_map)
        self.canvas.config(scrollregion=(0, 0, size[0], size[1]))
        self._set_status(f"Ready — {len(self.dirty_provinces)} unsaved province change(s)")

    def save(self):
        if not self.model:
            return
        try:
            self._set_status("Saving…")
            backup = self.model.save(self.dirty_provinces)
            count = len(self.dirty_provinces)
            self.dirty_provinces.clear()
            self.undo_stack.clear()
            self.redo_stack.clear()
            self._set_status("Saved")
            messagebox.showinfo(
                APP_TITLE,
                f"Saved culture definitions, in-game group colours, localisation and {count} changed province(s).\n\n"
                f"Backup created at:\n{backup}"
            )
        except Exception as exc:
            log = Path(__file__).resolve().parent / "culture_painter_error.log"
            log.write_text(traceback.format_exc(), encoding="utf-8")
            messagebox.showerror(APP_TITLE, f"Save failed:\n\n{exc}\n\nSee {log.name} for details.")
            self._set_status("Save failed")


def main():
    root = tk.Tk()
    app = CulturePainterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
