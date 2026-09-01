#!/usr/bin/env python3
"""
EU4 Setup Painter
=================

Install as:
    <EU4 mod>/tools/culture_painter/culture_painter.py

Run with no arguments. The program discovers the mod root automatically.

Dependencies:
    Pillow
    numpy

Design note
-----------
The editor is split into a generic map/selection layer plus CultureLayerModel and
CountryLayerModel. A future ReligionLayerModel can reuse the map renderer,
province / area / region selection, water protection, undo/redo and save workflow.

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
    common/province_names/* (vanilla suppressed via replace_path)
    localisation/*.yml
    localisation/replace/zz_setup_painter_overrides_l_english.yml
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
    from PIL import Image, ImageTk, ImageDraw, ImageOps
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, ttk
except ImportError as exc:
    print("Missing dependency:", exc)
    print("Install with: py -m pip install pillow numpy")
    raise

APP_TITLE = "EU4 Setup Painter"
DATA_FILENAME = "culture_painter_data.json"
COUNTRY_DATA_FILENAME = "country_painter_data.json"
COUNTRY_TAGS_FILENAME = "zz_country_painter_tags.txt"
COUNTRY_LOC_FILENAME = "zz_country_painter_l_english.yml"
OVERRIDE_LOC_FILENAME = "zz_setup_painter_overrides_l_english.yml"
COUNTRY_IDEAS_FILENAME = "zz_country_painter_ideas.txt"
COUNTRY_MANAGED_START = "# === EU4 SETUP PAINTER: COUNTRY SETUP START ==="
COUNTRY_MANAGED_END = "# === EU4 SETUP PAINTER: COUNTRY SETUP END ==="
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
# Total-conversion localisation / descriptor overrides
# =============================================================================

LOC_LINE_RE = re.compile(r'^\s*([A-Za-z0-9_.:-]+):(?:\d+)?\s+"(.*)"\s*$')
PROVINCE_LOC_RE = re.compile(r'^PROV\d+(?:_ADJ)?$')


def descriptor_files_for_mod(mod_root: Path) -> List[Path]:
    """Find descriptor.mod plus the matching outer launcher .mod file when possible."""
    out: List[Path] = []
    descriptor = mod_root / "descriptor.mod"
    if descriptor.exists():
        out.append(descriptor)

    parent = mod_root.parent
    try:
        candidates = list(parent.glob("*.mod"))
    except Exception:
        candidates = []

    root_name = mod_root.name.lower()
    root_norm = str(mod_root.resolve()).replace("\\", "/").rstrip("/").lower()
    for path in candidates:
        try:
            if descriptor.exists() and path.resolve() == descriptor.resolve():
                continue
        except Exception:
            pass
        try:
            text, _ = read_text(path)
        except Exception:
            continue
        matched = path.stem.lower() == root_name
        m = re.search(r'(?mi)^\s*path\s*=\s*"([^"]+)"\s*$', text)
        if m:
            declared = m.group(1).replace("\\", "/").rstrip("/").lower()
            if declared == root_norm or declared.endswith("/" + root_name) or declared == root_name:
                matched = True
        if matched and path not in out:
            out.append(path)
    return out


def has_replace_path(text: str, value: str) -> bool:
    pattern = re.compile(
        r'(?mi)^\s*replace_path\s*=\s*["\']' + re.escape(value) + r'["\']\s*(?:#.*)?$'
    )
    return bool(pattern.search(text))


def ensure_total_conversion_name_paths(mod_root: Path) -> List[Path]:
    """Suppress vanilla culture/tag-specific dynamic province names in this total conversion."""
    required = ("common/province_names",)
    changed: List[Path] = []
    descriptors = descriptor_files_for_mod(mod_root)
    for path in descriptors:
        text, enc = read_text(path)
        missing = [value for value in required if not has_replace_path(text, value)]
        if not missing:
            continue
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n# Managed by EU4 Setup Painter: suppress vanilla dynamic province names.\n"
        for value in missing:
            text += f'replace_path="{value}"\n'
        write_text(path, text, enc)
        changed.append(path)
    if not descriptors:
        raise RuntimeError(
            "No descriptor.mod was found in the mod root. The setup painter needs "
            "replace_path=\"common/province_names\" to stop vanilla dynamic province "
            "names from leaking into a total conversion."
        )
    return changed


def _read_loc_pairs(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text, _ = read_text(path)
    except Exception:
        return out
    for line in text.splitlines():
        m = LOC_LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def write_total_conversion_override_localisation(mod_root: Path) -> Path:
    """Write high-priority painter/province localisation under localisation/replace."""
    loc_root = mod_root / "localisation"
    replace_dir = loc_root / "replace"
    replace_dir.mkdir(parents=True, exist_ok=True)
    target = replace_dir / OVERRIDE_LOC_FILENAME

    values: Dict[str, str] = {}

    # Promote every custom base province name supplied anywhere by the mod.
    if loc_root.exists():
        for path in sorted(loc_root.rglob("*.yml")):
            try:
                if path.resolve() == target.resolve():
                    continue
            except Exception:
                pass
            for key, value in _read_loc_pairs(path).items():
                if PROVINCE_LOC_RE.match(key):
                    values[key] = value

    # Painter-managed culture/group and country/idea names.
    for source in (
        loc_root / MANAGED_LOC_FILENAME,
        loc_root / COUNTRY_LOC_FILENAME,
    ):
        if source.exists():
            values.update(_read_loc_pairs(source))

    # Preserve relevant entries written by an older painter version if their
    # normal-source file has since disappeared.
    if target.exists():
        for key, value in _read_loc_pairs(target).items():
            if key in values:
                continue
            if PROVINCE_LOC_RE.match(key) or COUNTRY_TAG_RE.match(key) or key.endswith(("_ADJ", "_ADJ2")):
                values[key] = value

    lines = ["l_english:"]
    for key in sorted(values, key=lambda x: (0 if PROVINCE_LOC_RE.match(x) else 1, x)):
        lines.append(f' {key}:0 "{values[key]}"')
    target.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return target

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
        return descriptor_files_for_mod(self.mod_root)

    @staticmethod
    def _has_replace_path(text: str, value: str) -> bool:
        return has_replace_path(text, value)

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
        override_loc = self.mod_root / "localisation" / "replace" / OVERRIDE_LOC_FILENAME
        if override_loc.exists():
            paths.add(override_loc)
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
        ensure_total_conversion_name_paths(self.mod_root)
        self._sync_definitions()
        self._write_region_colours()
        self._write_localisation()
        write_total_conversion_override_localisation(self.mod_root)
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
# Country model
# =============================================================================

UNOWNED_TAG = "__UNOWNED__"
COUNTRY_TAG_RE = re.compile(r"^[A-Z]{3}$")
# Known collision-prone / non-working IDs from the EU4 country-creation documentation.
FORBIDDEN_COUNTRY_TAGS = {
    "ADD", "ADM", "AND", "AGE", "ART", "AUX", "CAR", "CAT", "CAV", "CON",
    "DIP", "HAS", "HRE", "INF", "JAM", "MIL", "MIN", "NOT", "NUL", "PRN",
    "RGB", "SUM", "VAL", "VAN", "REB", "PIR", "NAT",
}
DEFAULT_GOVERNMENTS = ["monarchy", "republic", "theocracy", "tribal", "native"]
DEFAULT_TECH_GROUPS = [
    "western", "eastern", "ottoman", "muslim", "indian", "chinese",
    "east_african", "central_african", "south_american", "north_american",
    "mesoamerican", "andean", "aboriginal",
]
DEFAULT_ESTATES = [
    "estate_church", "estate_nobles", "estate_burghers", "estate_cossacks",
    "estate_dhimmi", "estate_brahmins", "estate_jains", "estate_maratha",
    "estate_rajput", "estate_vaisyas", "estate_nomadic_tribes", "estate_qizilbash",
    "estate_ghulams",
]
DEFAULT_PERSONALITIES = [
    "balanced_personality", "bold_fighter_personality", "calm_personality",
    "careful_personality", "craven_personality", "cruel_personality",
    "embezzler_personality", "entrepreneur_personality", "free_thinker_personality",
    "fierce_negotiator_personality", "inspiring_leader_personality",
    "intricate_web_weaver_personality", "lawgiver_personality",
    "midas_touched_personality", "naive_enthusiast_personality",
    "obsessive_perfectionist_personality", "pious_personality",
    "scholar_personality", "secretive_personality", "silver_tongue_personality",
    "strict_personality", "tactical_genius_personality", "well_advised_personality",
    "zealot_personality",
]


def safe_country_tag(value: str) -> str:
    tag = re.sub(r"[^A-Z0-9]", "", value.upper().strip())[:3]
    return tag


def quote_clausewitz(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def remove_top_level_assignment(path: Path, key: str) -> bool:
    if not path.exists():
        return False
    text, enc = read_text(path)
    lines = text.splitlines(keepends=True)
    depths = line_depths(text)
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    kept = [line for line, depth in zip(lines, depths) if not (depth == 0 and pat.match(line))]
    if kept == lines:
        return False
    write_text(path, "".join(kept), enc)
    return True


def ensure_top_level_line(path: Path, exact_line: str) -> bool:
    """Ensure an exact assignment-like line exists at depth zero."""
    text, enc = read_text(path)
    lines = text.splitlines(keepends=True)
    depths = line_depths(text)
    wanted = exact_line.strip()
    for line, depth in zip(lines, depths):
        if depth == 0 and line.split("#", 1)[0].strip() == wanted:
            return False
    if text and not text.endswith("\n"):
        text += "\n"
    text += exact_line.rstrip() + "\n"
    write_text(path, text, enc)
    return True


def replace_managed_section(path: Path, start_marker: str, end_marker: str, body: str) -> None:
    if path.exists():
        text, enc = read_text(path)
    else:
        text, enc = "", "utf-8"
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker) + r"\s*",
        re.DOTALL,
    )
    text = pattern.sub("", text).rstrip() + ("\n\n" if text.strip() else "")
    text += start_marker + "\n" + body.rstrip() + "\n" + end_marker + "\n"
    write_text(path, text, enc)


def parse_localisation_tree(mod_root: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    loc_dir = mod_root / "localisation"
    if not loc_dir.exists():
        return out
    pattern = re.compile(r'^\s*([A-Za-z0-9_.:-]+):(?:\d+)?\s+"(.*)"\s*$')
    for path in sorted(loc_dir.rglob("*.yml")):
        try:
            text, _ = read_text(path)
        except Exception:
            continue
        for line in text.splitlines():
            m = pattern.match(line)
            if m:
                out[m.group(1)] = m.group(2).replace('\\"', '"')
    return out


def scan_child_ids(mod_root: Path, relative_dir: str, reserved: Optional[Set[str]] = None) -> List[str]:
    """Scan IDs nested one level inside top-level blocks (religions/estates)."""
    reserved = reserved or set()
    out: Set[str] = set()
    folder = mod_root / relative_dir
    if not folder.exists():
        return []
    for path in folder.glob("*.txt"):
        try:
            text, _ = read_text(path)
        except Exception:
            continue
        for parent in top_level_blocks(text):
            inner_start = parent.open_brace + 1
            inner = text[inner_start:parent.close_brace]
            for child in top_level_blocks(inner, offset=inner_start):
                if child.key not in reserved:
                    out.add(child.key)
    return sorted(out)


def scan_top_level_ids(mod_root: Path, relative_dir: str) -> List[str]:
    out: Set[str] = set()
    folder = mod_root / relative_dir
    if not folder.exists():
        return []
    for path in folder.glob("*.txt"):
        try:
            text, _ = read_text(path)
        except Exception:
            continue
        out.update(block.key for block in top_level_blocks(text))
    return sorted(out)


def scan_technology_groups(mod_root: Path) -> List[str]:
    candidates = [mod_root / "common" / "technology.txt", mod_root / "common" / "technology"]
    out: Set[str] = set()
    for path in candidates:
        paths = list(path.glob("*.txt")) if path.is_dir() else ([path] if path.exists() else [])
        for p in paths:
            try:
                text, _ = read_text(p)
            except Exception:
                continue
            out.update(block.key for block in top_level_blocks(text))
    return sorted(out)


def scan_country_tags(mod_root: Path) -> Dict[str, str]:
    """Return TAG -> common/countries relative file path."""
    out: Dict[str, str] = {}
    folder = mod_root / "common" / "country_tags"
    if not folder.exists():
        return out
    pat = re.compile(r'^\s*([A-Z0-9]{3})\s*=\s*"([^"]+)"')
    for path in sorted(folder.glob("*.txt")):
        try:
            text, _ = read_text(path)
        except Exception:
            continue
        for line in text.splitlines():
            m = pat.match(line)
            if m and m.group(1) not in out:
                out[m.group(1)] = m.group(2).replace("\\", "/")
    return out


def find_country_history(mod_root: Path, tag: str) -> Optional[Path]:
    folder = mod_root / "history" / "countries"
    if not folder.exists():
        return None
    paths = sorted(folder.glob(f"{tag}*.txt"))
    return paths[0] if paths else None


def read_country_colour(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    text, _ = read_text(path)
    # common/countries files use color = { R G B }
    m = re.search(r"(?mi)^\s*color\s*=\s*\{\s*(\d+)\s+(\d+)\s+(\d+)\s*\}", text)
    if not m:
        return None
    rgb = tuple(max(0, min(255, int(m.group(i)))) for i in (1, 2, 3))
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def set_common_country_colour(path: Path, colour: str, graphical_culture: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text, enc = read_text(path)
    else:
        text, enc = "", "utf-8"
    r, g, b = hex_to_rgb(colour)
    colour_line = f"color = {{ {r} {g} {b} }}"
    pat = re.compile(r"(?mi)^\s*color\s*=\s*\{[^\n\r}]*\}\s*(?:#.*)?$")
    if pat.search(text):
        text = pat.sub(colour_line, text, count=1)
    else:
        text = (text.rstrip() + "\n" if text.strip() else "") + colour_line + "\n"
    if graphical_culture.strip():
        gpat = re.compile(r"(?mi)^\s*graphical_culture\s*=\s*[^\s#]+.*$")
        gline = f"graphical_culture = {graphical_culture.strip()}"
        if gpat.search(text):
            text = gpat.sub(gline, text, count=1)
        else:
            text = gline + "\n" + text
    write_text(path, text, enc)


@dataclass
class CharacterInfo:
    enabled: bool = True
    name: str = ""
    dynasty: str = ""
    adm: int = 3
    dip: int = 3
    mil: int = 3
    age: int = 30
    gender: str = "Male"
    culture: str = ""
    religion: str = ""
    claim: int = 100
    traits: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict, enabled_default: bool = True) -> "CharacterInfo":
        if not isinstance(data, dict):
            return cls(enabled=enabled_default)
        return cls(
            enabled=bool(data.get("enabled", enabled_default)),
            name=str(data.get("name", "")),
            dynasty=str(data.get("dynasty", "")),
            adm=max(0, min(6, int(data.get("adm", 3)))),
            dip=max(0, min(6, int(data.get("dip", 3)))),
            mil=max(0, min(6, int(data.get("mil", 3)))),
            age=max(0, min(120, int(data.get("age", 30)))),
            gender=str(data.get("gender", "Male")),
            culture=str(data.get("culture", "")),
            religion=str(data.get("religion", "")),
            claim=max(0, min(100, int(data.get("claim", 100)))),
            traits=[str(x) for x in data.get("traits", []) if str(x).strip()],
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "name": self.name, "dynasty": self.dynasty,
            "adm": self.adm, "dip": self.dip, "mil": self.mil, "age": self.age,
            "gender": self.gender, "culture": self.culture, "religion": self.religion,
            "claim": self.claim, "traits": list(self.traits),
        }


@dataclass
class NationalIdeasInfo:
    mode: str = "default"  # default/custom
    traditions: str = ""
    ambition: str = ""
    ideas: List[dict] = field(default_factory=lambda: [
        {"name": f"Idea {i}", "modifiers": ""} for i in range(1, 8)
    ])

    @classmethod
    def from_dict(cls, data: dict) -> "NationalIdeasInfo":
        if not isinstance(data, dict):
            return cls()
        ideas = data.get("ideas", [])
        normalized = []
        for i in range(7):
            rec = ideas[i] if i < len(ideas) and isinstance(ideas[i], dict) else {}
            normalized.append({
                "name": str(rec.get("name", f"Idea {i+1}")),
                "modifiers": str(rec.get("modifiers", "")),
            })
        return cls(
            mode="custom" if data.get("mode") == "custom" else "default",
            traditions=str(data.get("traditions", "")),
            ambition=str(data.get("ambition", "")),
            ideas=normalized,
        )

    def to_dict(self) -> dict:
        return {"mode": self.mode, "traditions": self.traditions,
                "ambition": self.ambition, "ideas": self.ideas}


@dataclass
class CountryInfo:
    tag: str
    name: str
    adjective: str
    colour: str
    original: bool = True
    managed: bool = False
    country_file: Optional[str] = None
    history_file: Optional[str] = None
    graphical_culture: str = "westerngfx"
    government: str = "monarchy"
    government_rank: int = 1
    government_reform: str = ""
    stability: int = 0
    capital: int = 0
    religion: str = ""
    primary_culture: str = ""
    accepted_cultures: List[str] = field(default_factory=list)
    technology_group: str = "western"
    custom_tech_levels: bool = False
    adm_tech: int = 3
    dip_tech: int = 3
    mil_tech: int = 3
    start_date: str = "1444.11.11"
    treasury: float = 0.0
    prestige: float = 0.0
    ruler: CharacterInfo = field(default_factory=CharacterInfo)
    heir: CharacterInfo = field(default_factory=lambda: CharacterInfo(enabled=False, age=15, claim=50))
    consort: CharacterInfo = field(default_factory=lambda: CharacterInfo(enabled=False, age=25))
    estate_mode: str = "default"
    estate_shares: Dict[str, float] = field(default_factory=dict)
    national_ideas: NationalIdeasInfo = field(default_factory=NationalIdeasInfo)
    flag_mode: str = "designer"  # designer/upload
    flag_source: str = ""
    flag_pattern: str = "Horizontal bicolor"
    flag_colours: List[str] = field(default_factory=lambda: ["#FFFFFF", "#202060", "#C02020"])
    flag_emblem: str = "None"

    @classmethod
    def from_dict(cls, tag: str, data: dict, fallback: Optional["CountryInfo"] = None) -> "CountryInfo":
        base = fallback or cls(tag, tag, tag, deterministic_colour("country:" + tag))
        if not isinstance(data, dict):
            return base
        def get(key, default):
            return data.get(key, default)
        base.name = str(get("name", base.name))
        base.adjective = str(get("adjective", base.adjective))
        try:
            base.colour = parse_hex_colour(str(get("colour", base.colour)))
        except ValueError:
            pass
        base.original = bool(get("original", base.original))
        base.managed = bool(get("managed", True))
        base.country_file = get("country_file", base.country_file)
        base.history_file = get("history_file", base.history_file)
        base.graphical_culture = str(get("graphical_culture", base.graphical_culture))
        base.government = str(get("government", base.government))
        base.government_rank = max(1, min(3, int(get("government_rank", base.government_rank))))
        base.government_reform = str(get("government_reform", base.government_reform))
        base.stability = max(-3, min(3, int(get("stability", base.stability))))
        base.capital = max(0, int(get("capital", base.capital)))
        base.religion = str(get("religion", base.religion))
        base.primary_culture = str(get("primary_culture", base.primary_culture))
        base.accepted_cultures = [str(x) for x in get("accepted_cultures", base.accepted_cultures)]
        base.technology_group = str(get("technology_group", base.technology_group))
        base.custom_tech_levels = bool(get("custom_tech_levels", base.custom_tech_levels))
        base.adm_tech = max(0, int(get("adm_tech", base.adm_tech)))
        base.dip_tech = max(0, int(get("dip_tech", base.dip_tech)))
        base.mil_tech = max(0, int(get("mil_tech", base.mil_tech)))
        base.start_date = str(get("start_date", base.start_date))
        base.treasury = float(get("treasury", base.treasury))
        base.prestige = float(get("prestige", base.prestige))
        base.ruler = CharacterInfo.from_dict(get("ruler", {}), True)
        base.heir = CharacterInfo.from_dict(get("heir", {}), False)
        base.consort = CharacterInfo.from_dict(get("consort", {}), False)
        base.estate_mode = "custom" if get("estate_mode", "default") == "custom" else "default"
        shares = get("estate_shares", {})
        base.estate_shares = {str(k): float(v) for k, v in shares.items()} if isinstance(shares, dict) else {}
        base.national_ideas = NationalIdeasInfo.from_dict(get("national_ideas", {}))
        base.flag_mode = "upload" if get("flag_mode", "designer") == "upload" else "designer"
        base.flag_source = str(get("flag_source", ""))
        base.flag_pattern = str(get("flag_pattern", base.flag_pattern))
        fc = get("flag_colours", base.flag_colours)
        if isinstance(fc, list) and len(fc) >= 3:
            try:
                base.flag_colours = [parse_hex_colour(str(x)) for x in fc[:3]]
            except ValueError:
                pass
        base.flag_emblem = str(get("flag_emblem", base.flag_emblem))
        return base

    def to_dict(self) -> dict:
        return {
            "name": self.name, "adjective": self.adjective, "colour": self.colour,
            "original": self.original, "managed": self.managed,
            "country_file": self.country_file, "history_file": self.history_file,
            "graphical_culture": self.graphical_culture, "government": self.government,
            "government_rank": self.government_rank, "government_reform": self.government_reform,
            "stability": self.stability, "capital": self.capital, "religion": self.religion,
            "primary_culture": self.primary_culture, "accepted_cultures": self.accepted_cultures,
            "technology_group": self.technology_group,
            "custom_tech_levels": self.custom_tech_levels,
            "adm_tech": self.adm_tech, "dip_tech": self.dip_tech, "mil_tech": self.mil_tech,
            "start_date": self.start_date, "treasury": self.treasury, "prestige": self.prestige,
            "ruler": self.ruler.to_dict(), "heir": self.heir.to_dict(), "consort": self.consort.to_dict(),
            "estate_mode": self.estate_mode, "estate_shares": self.estate_shares,
            "national_ideas": self.national_ideas.to_dict(),
            "flag_mode": self.flag_mode, "flag_source": self.flag_source,
            "flag_pattern": self.flag_pattern, "flag_colours": self.flag_colours,
            "flag_emblem": self.flag_emblem,
        }


def _star_points(cx: float, cy: float, r_outer: float, r_inner: float, points: int = 5) -> List[Tuple[float, float]]:
    import math
    out = []
    for i in range(points * 2):
        angle = -math.pi / 2 + i * math.pi / points
        radius = r_outer if i % 2 == 0 else r_inner
        out.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    return out


def generate_designer_flag(pattern: str, colours: Sequence[str], emblem: str, size: int = 128) -> Image.Image:
    """Bake a Nation-Designer-style 3-colour flag into a normal EU4 tag flag."""
    cols = [hex_to_rgb(c) for c in list(colours)[:3]]
    while len(cols) < 3:
        cols.append((255, 255, 255))
    c1, c2, c3 = cols
    im = Image.new("RGB", (size, size), c1)
    d = ImageDraw.Draw(im)
    s = size
    p = pattern
    if p == "Horizontal bicolor":
        d.rectangle((0, s//2, s, s), fill=c2)
    elif p == "Horizontal tricolor":
        d.rectangle((0, s//3, s, 2*s//3), fill=c2); d.rectangle((0, 2*s//3, s, s), fill=c3)
    elif p == "Vertical bicolor":
        d.rectangle((s//2, 0, s, s), fill=c2)
    elif p == "Vertical tricolor":
        d.rectangle((s//3, 0, 2*s//3, s), fill=c2); d.rectangle((2*s//3, 0, s, s), fill=c3)
    elif p == "Diagonal":
        d.polygon([(0, s), (s, 0), (s, s)], fill=c2)
    elif p == "Quartered":
        d.rectangle((s//2, 0, s, s//2), fill=c2); d.rectangle((0, s//2, s//2, s), fill=c2)
        d.rectangle((s//2, s//2, s, s), fill=c3)
    elif p == "Center cross":
        w = max(10, s//5)
        d.rectangle((s//2-w//2, 0, s//2+w//2, s), fill=c2)
        d.rectangle((0, s//2-w//2, s, s//2+w//2), fill=c2)
        iw = max(4, w//3)
        d.rectangle((s//2-iw//2, 0, s//2+iw//2, s), fill=c3)
        d.rectangle((0, s//2-iw//2, s, s//2+iw//2), fill=c3)
    elif p == "Nordic cross":
        x = int(s * .38); y = s//2; w = max(12, s//5)
        d.rectangle((x-w//2, 0, x+w//2, s), fill=c2); d.rectangle((0, y-w//2, s, y+w//2), fill=c2)
        iw = max(4, w//3)
        d.rectangle((x-iw//2, 0, x+iw//2, s), fill=c3); d.rectangle((0, y-iw//2, s, y+iw//2), fill=c3)
    elif p == "Saltire":
        w = max(8, s//10)
        d.polygon([(0,0),(w,0),(s,s-w),(s,s),(s-w,s),(0,w)], fill=c2)
        d.polygon([(s,0),(s-w,0),(0,s-w),(0,s),(w,s),(s,w)], fill=c2)
    # Solid deliberately does nothing.

    # Emblem is intentionally simple and vector-generated; normal countries need a static TGA.
    ec = c3
    cx = cy = s//2
    if emblem == "Circle":
        r = s//5; d.ellipse((cx-r, cy-r, cx+r, cy+r), fill=ec)
    elif emblem == "Diamond":
        r = s//5; d.polygon([(cx,cy-r),(cx+r,cy),(cx,cy+r),(cx-r,cy)], fill=ec)
    elif emblem == "Star":
        d.polygon(_star_points(cx, cy, s*.22, s*.09), fill=ec)
    elif emblem == "Crescent":
        r = s//5; d.ellipse((cx-r, cy-r, cx+r, cy+r), fill=ec)
        d.ellipse((cx-r//2, cy-r, cx+r+r//3, cy+r), fill=c1)
    elif emblem == "Ring":
        r = s//5; d.ellipse((cx-r,cy-r,cx+r,cy+r), fill=ec)
        r2 = int(r*.58); d.ellipse((cx-r2,cy-r2,cx+r2,cy+r2), fill=c1)
    return im


class CountryLayerModel(LayerModel):
    assignment_key = "owner"

    def __init__(self, mod_root: Path, map_data: MapData, culture_model: CultureLayerModel):
        self.mod_root = mod_root
        self.map_data = map_data
        self.culture_model = culture_model
        self.tool_dir = Path(__file__).resolve().parent
        self.data_path = self.tool_dir / COUNTRY_DATA_FILENAME
        self.localisation = parse_localisation_tree(mod_root)
        self.countries: Dict[str, CountryInfo] = {}
        self.assignments: Dict[int, Optional[str]] = {}
        self.tags = scan_country_tags(mod_root)
        self.religions = self._scan_religions()
        self.estates = self._scan_estates()
        self.personalities = self._scan_personalities()
        self.tech_groups = scan_technology_groups(mod_root) or list(DEFAULT_TECH_GROUPS)
        self.governments = self._scan_governments()
        self.government_reforms = scan_top_level_ids(mod_root, "common/government_reforms")
        self.graphical_cultures = self._scan_graphical_cultures()
        self._load_countries()
        self._load_metadata()
        self._load_assignments()

    def _scan_religions(self) -> List[str]:
        reserved = {"flags_with_emblem_percentage", "crusade_name", "defender_of_faith", "can_form_personal_unions"}
        values = scan_child_ids(self.mod_root, "common/religions", reserved)
        # Also collect starting religions already present in the mod's province history.
        for path in self.map_data.province_history.values():
            try:
                v = read_top_level_assignment(path, "religion")
            except Exception:
                v = None
            if v:
                values.append(v)
        return sorted(set(values))

    def _scan_estates(self) -> List[str]:
        values = scan_top_level_ids(self.mod_root, "common/estates")
        values = [x for x in values if x.startswith("estate_")]
        return sorted(set(values or DEFAULT_ESTATES))

    def _scan_personalities(self) -> List[str]:
        values = scan_top_level_ids(self.mod_root, "common/ruler_personalities")
        # Some personality files group entries; scan children too.
        values += scan_child_ids(self.mod_root, "common/ruler_personalities")
        values = [x for x in values if x.endswith("_personality")]
        return sorted(set(values or DEFAULT_PERSONALITIES))

    def _scan_governments(self) -> List[str]:
        values = set(DEFAULT_GOVERNMENTS)
        # Existing history values are the most trustworthy local options.
        folder = self.mod_root / "history" / "countries"
        if folder.exists():
            for path in folder.glob("*.txt"):
                try:
                    v = read_top_level_assignment(path, "government")
                except Exception:
                    v = None
                if v:
                    values.add(v)
        return sorted(values)

    def _scan_graphical_cultures(self) -> List[str]:
        values: Set[str] = {"westerngfx", "easterngfx", "muslimgfx", "indiangfx", "chinesegfx"}
        folder = self.mod_root / "common" / "countries"
        if folder.exists():
            for path in folder.glob("*.txt"):
                try:
                    text, _ = read_text(path)
                except Exception:
                    continue
                for m in re.finditer(r"(?mi)^\s*graphical_culture\s*=\s*([^\s#]+)", text):
                    values.add(m.group(1))
        return sorted(values)

    def _load_countries(self):
        for tag, relative in sorted(self.tags.items()):
            common_path = self.mod_root / "common" / relative
            if not common_path.exists():
                # common tags generally use countries/Foo.txt relative to common/
                common_path = self.mod_root / "common" / "countries" / Path(relative).name
            colour = read_country_colour(common_path) or deterministic_colour("country:" + tag)
            history = find_country_history(self.mod_root, tag)
            ci = CountryInfo(
                tag=tag,
                name=self.localisation.get(tag, tag),
                adjective=self.localisation.get(tag + "_ADJ", self.localisation.get(tag + "_ADJ2", tag)),
                colour=colour,
                original=True,
                managed=False,
                country_file=str(common_path.relative_to(self.mod_root)).replace("\\", "/") if common_path.exists() else None,
                history_file=str(history.relative_to(self.mod_root)).replace("\\", "/") if history else None,
            )
            if common_path.exists():
                text, _ = read_text(common_path)
                m = re.search(r"(?mi)^\s*graphical_culture\s*=\s*([^\s#]+)", text)
                if m:
                    ci.graphical_culture = m.group(1)
            if history:
                ci.government = read_top_level_assignment(history, "government") or ci.government
                try:
                    ci.government_rank = int(read_top_level_assignment(history, "government_rank") or ci.government_rank)
                except ValueError:
                    pass
                ci.technology_group = read_top_level_assignment(history, "technology_group") or ci.technology_group
                ci.religion = read_top_level_assignment(history, "religion") or ci.religion
                ci.primary_culture = read_top_level_assignment(history, "primary_culture") or ci.primary_culture
                try:
                    ci.capital = int(read_top_level_assignment(history, "capital") or 0)
                except ValueError:
                    pass
                # Accepted cultures may repeat at depth zero.
                try:
                    htext, _ = read_text(history)
                    depths = line_depths(htext)
                    for line, depth in zip(htext.splitlines(), depths):
                        if depth == 0:
                            m = re.match(r"^\s*add_accepted_culture\s*=\s*([^\s#]+)", line)
                            if m:
                                ci.accepted_cultures.append(m.group(1))
                except Exception:
                    pass
            self.countries[tag] = ci

    def _load_metadata(self):
        if not self.data_path.exists():
            return
        try:
            data = json.loads(self.data_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for tag, rec in data.get("countries", {}).items():
            tag = safe_country_tag(tag)
            if not COUNTRY_TAG_RE.fullmatch(tag):
                continue
            base = self.countries.get(tag)
            if base is None:
                base = CountryInfo(tag, tag, tag, deterministic_colour("country:" + tag), original=False, managed=True)
            self.countries[tag] = CountryInfo.from_dict(tag, rec, base)

    def _load_assignments(self):
        for pid in self.map_data.province_ids:
            if pid in self.map_data.water_provinces:
                self.assignments[pid] = None
                continue
            path = self.map_data.province_history.get(pid)
            owner = read_top_level_assignment(path, "owner") if path else None
            self.assignments[pid] = owner
            if owner and owner not in self.countries:
                self.countries[owner] = CountryInfo(
                    owner, self.localisation.get(owner, owner), self.localisation.get(owner + "_ADJ", owner),
                    deterministic_colour("country:" + owner), original=True, managed=False,
                )

    def assignment_for_province(self, province_id: int) -> Optional[str]:
        return self.assignments.get(province_id)

    def owned_provinces(self, tag: str) -> List[int]:
        return sorted(pid for pid, owner in self.assignments.items() if owner == tag and pid not in self.map_data.water_provinces)

    def create_country(self, country: CountryInfo) -> str:
        tag = safe_country_tag(country.tag)
        if not COUNTRY_TAG_RE.fullmatch(tag):
            raise ValueError("Country tag must be exactly three letters A-Z.")
        if tag in self.countries:
            raise ValueError(f"Country tag '{tag}' already exists in this mod.")
        if tag in FORBIDDEN_COUNTRY_TAGS:
            raise ValueError(f"'{tag}' is reserved/collision-prone in EU4 and should not be used as a normal country tag.")
        country.tag = tag
        country.original = False
        country.managed = True
        country.country_file = f"common/countries/ZZ_Painter_{tag}.txt"
        country.history_file = f"history/countries/{tag} - {safe_id(country.name)}.txt"
        self.countries[tag] = country
        return tag

    def update_country(self, tag: str, updated: CountryInfo) -> None:
        if tag not in self.countries:
            raise ValueError("Unknown country.")
        updated.tag = tag
        updated.original = self.countries[tag].original
        updated.managed = True
        updated.country_file = self.countries[tag].country_file or f"common/countries/ZZ_Painter_{tag}.txt"
        updated.history_file = self.countries[tag].history_file or f"history/countries/{tag} - {safe_id(updated.name)}.txt"
        self.countries[tag] = updated

    def store_flag_source(self, tag: str, source: Path) -> str:
        folder = self.tool_dir / "flag_sources"
        folder.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower() if source.suffix else ".png"
        dest = folder / f"{tag}{suffix}"
        shutil.copy2(source, dest)
        try:
            return str(dest.relative_to(self.tool_dir)).replace("\\", "/")
        except ValueError:
            return str(dest)

    def _resolve_flag_source(self, source: str) -> Optional[Path]:
        if not source:
            return None
        p = Path(source)
        if not p.is_absolute():
            p = self.tool_dir / p
        return p if p.exists() else None

    def _write_flag(self, country: CountryInfo) -> None:
        out = self.mod_root / "gfx" / "flags" / f"{country.tag}.tga"
        out.parent.mkdir(parents=True, exist_ok=True)
        if country.flag_mode == "upload":
            source = self._resolve_flag_source(country.flag_source)
            if source is None:
                raise FileNotFoundError(f"Flag image for {country.tag} no longer exists: {country.flag_source}")
            with Image.open(source) as im:
                im = im.convert("RGB")
                # Normal EU4 flags are 128x128. Fit without stretching and fill edges.
                im = ImageOps.fit(im, (128, 128), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        else:
            im = generate_designer_flag(country.flag_pattern, country.flag_colours, country.flag_emblem, 128)
        im.save(out, format="TGA")

    def _write_tag_file(self) -> None:
        path = self.mod_root / "common" / "country_tags" / COUNTRY_TAGS_FILENAME
        lines = ["# Managed by EU4 Setup Painter."]
        for tag, country in sorted(self.countries.items()):
            if not country.managed or country.original:
                continue
            rel = country.country_file or f"common/countries/ZZ_Painter_{tag}.txt"
            try:
                rel_common = Path(rel).relative_to("common")
            except ValueError:
                rel_common = Path("countries") / Path(rel).name
            lines.append(f'{tag} = "{str(rel_common).replace(chr(92), "/")}"')
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, "\n".join(lines) + "\n", "utf-8")

    def _history_body(self, country: CountryInfo) -> str:
        lines: List[str] = []
        lines.append(f"government = {country.government or 'monarchy'}")
        lines.append(f"government_rank = {country.government_rank}")
        if country.government_reform.strip():
            lines.append(f"add_government_reform = {country.government_reform.strip()}")
        if country.technology_group.strip():
            lines.append(f"technology_group = {country.technology_group.strip()}")
        if country.religion.strip():
            lines.append(f"religion = {country.religion.strip()}")
        if country.primary_culture.strip():
            lines.append(f"primary_culture = {country.primary_culture.strip()}")
        for culture in sorted(set(c for c in country.accepted_cultures if c and c != country.primary_culture)):
            lines.append(f"add_accepted_culture = {culture}")
        if country.capital > 0:
            lines.append(f"capital = {country.capital}")
        lines.append("")
        start = country.start_date.strip() or "1444.11.11"
        lines.append(f"{start} = {{")
        lines.append(f"\tadd_stability = {country.stability}")
        if country.treasury:
            lines.append(f"\tadd_treasury = {country.treasury:g}")
        if country.prestige:
            lines.append(f"\tadd_prestige = {country.prestige:g}")
        if country.custom_tech_levels:
            # Effects add technologies; this is therefore explicitly opt-in.
            if country.adm_tech: lines.append(f"\tadd_adm_tech = {country.adm_tech}")
            if country.dip_tech: lines.append(f"\tadd_dip_tech = {country.dip_tech}")
            if country.mil_tech: lines.append(f"\tadd_mil_tech = {country.mil_tech}")
        lines += self._character_effect_lines("ruler", country.ruler, country, indent="\t")
        if country.heir.enabled:
            lines += self._character_effect_lines("heir", country.heir, country, indent="\t")
        if country.consort.enabled:
            lines += self._character_effect_lines("consort", country.consort, country, indent="\t")
        if country.estate_mode == "custom":
            lines.append("\t# Exact estate shares: clear estate land first; crownland is the remainder.")
            lines.append("\tchange_estate_land_share = { estate = all share = -100 }")
            for estate, share in sorted(country.estate_shares.items()):
                if share > 0:
                    lines.append(f"\tchange_estate_land_share = {{ estate = {estate} share = {share:g} }}")
        lines.append("}")
        return "\n".join(lines)

    def _character_effect_lines(self, role: str, ch: CharacterInfo, country: CountryInfo, indent: str) -> List[str]:
        effect = {"ruler": "define_ruler", "heir": "define_heir", "consort": "define_consort"}[role]
        trait_effect = {"ruler": "add_ruler_personality", "heir": "add_heir_personality", "consort": "add_queen_personality"}[role]
        lines = [f"{indent}{effect} = {{"]
        if ch.name.strip(): lines.append(f"{indent}\tname = {quote_clausewitz(ch.name.strip())}")
        if ch.dynasty.strip(): lines.append(f"{indent}\tdynasty = {quote_clausewitz(ch.dynasty.strip())}")
        lines.append(f"{indent}\tage = {ch.age}")
        lines.append(f"{indent}\tadm = {ch.adm}")
        lines.append(f"{indent}\tdip = {ch.dip}")
        lines.append(f"{indent}\tmil = {ch.mil}")
        if role in ("ruler", "heir"):
            lines.append(f"{indent}\tfixed = yes")
        if role == "heir":
            lines.append(f"{indent}\tclaim = {ch.claim}")
        if ch.gender == "Female": lines.append(f"{indent}\tfemale = yes")
        elif ch.gender == "Male": lines.append(f"{indent}\tmale = yes")
        culture = ch.culture.strip() or country.primary_culture.strip()
        religion = ch.religion.strip() or country.religion.strip()
        if culture: lines.append(f"{indent}\tculture = {culture}")
        if religion: lines.append(f"{indent}\treligion = {religion}")
        if role == "consort":
            lines.append(f"{indent}\tcountry_of_origin = ROOT")
        lines.append(f"{indent}}}")
        for trait in ch.traits:
            if trait.strip():
                lines.append(f"{indent}{trait_effect} = {trait.strip()}")
        return lines

    def _write_country_files(self) -> None:
        for tag, country in sorted(self.countries.items()):
            if not country.managed:
                continue
            if not country.country_file:
                country.country_file = f"common/countries/ZZ_Painter_{tag}.txt"
            common_path = self.mod_root / country.country_file
            set_common_country_colour(common_path, country.colour, country.graphical_culture)
            if not country.history_file:
                country.history_file = f"history/countries/{tag} - {safe_id(country.name)}.txt"
            history_path = self.mod_root / country.history_file
            history_path.parent.mkdir(parents=True, exist_ok=True)
            replace_managed_section(history_path, COUNTRY_MANAGED_START, COUNTRY_MANAGED_END, self._history_body(country))
            self._write_flag(country)

    def _write_localisation(self) -> None:
        path = self.mod_root / "localisation" / COUNTRY_LOC_FILENAME
        lines = ["l_english:"]
        for tag, c in sorted(self.countries.items()):
            if not c.managed:
                continue
            name = c.name.replace("\\", "\\\\").replace('"', '\\"')
            adj = c.adjective.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f' {tag}:0 "{name}"')
            lines.append(f' {tag}_ADJ:0 "{adj}"')
            lines.append(f' {tag}_ADJ2:0 "{adj}"')
            if c.national_ideas.mode == "custom":
                for i, rec in enumerate(c.national_ideas.ideas, 1):
                    iid = f"{tag.lower()}_idea_{i}"
                    iname = str(rec.get("name", f"Idea {i}")).replace("\\", "\\\\").replace('"', '\\"')
                    lines.append(f' {iid}:0 "{iname}"')
                    lines.append(f' {iid}_desc:0 ""')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    @staticmethod
    def _modifier_block(text: str, indent: str = "\t\t") -> List[str]:
        lines = []
        for raw in text.splitlines():
            raw = raw.strip()
            if raw:
                lines.append(indent + raw)
        return lines

    def _write_ideas(self) -> None:
        path = self.mod_root / "common" / "ideas" / COUNTRY_IDEAS_FILENAME
        lines = ["# Managed by EU4 Setup Painter.", ""]
        for tag, c in sorted(self.countries.items()):
            if not c.managed or c.national_ideas.mode != "custom":
                continue
            key = f"{tag.lower()}_ideas"
            lines += [f"{key} = {{", "\tstart = {"]
            lines += self._modifier_block(c.national_ideas.traditions)
            lines += ["\t}", "", "\tbonus = {"]
            lines += self._modifier_block(c.national_ideas.ambition)
            lines += ["\t}", "", "\ttrigger = {", f"\t\ttag = {tag}", "\t}", "\tfree = yes", ""]
            for i, rec in enumerate(c.national_ideas.ideas, 1):
                iid = f"{tag.lower()}_idea_{i}"
                lines.append(f"\t{iid} = {{")
                lines += self._modifier_block(str(rec.get("modifiers", "")))
                lines.append("\t}")
            lines += ["}", ""]
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, "\n".join(lines) + "\n", "utf-8")

    def _metadata_dict(self) -> dict:
        return {
            "version": 1,
            "notes": "Country setup metadata for EU4 Setup Painter. Flag designer values are baked to gfx/flags/TAG.tga on Save.",
            "countries": {tag: c.to_dict() for tag, c in sorted(self.countries.items()) if c.managed},
        }

    def save_metadata(self):
        self.tool_dir.mkdir(parents=True, exist_ok=True)
        self.data_path.write_text(json.dumps(self._metadata_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _backup(self, dirty_provinces: Set[int]) -> Path:
        root = self.tool_dir / BACKUP_DIRNAME / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_countries")
        paths: Set[Path] = set()
        for pid in dirty_provinces:
            p = self.map_data.province_history.get(pid)
            if p and p.exists(): paths.add(p)
        managed_candidates = [
            self.data_path,
            self.mod_root / "common" / "country_tags" / COUNTRY_TAGS_FILENAME,
            self.mod_root / "localisation" / COUNTRY_LOC_FILENAME,
            self.mod_root / "localisation" / "replace" / OVERRIDE_LOC_FILENAME,
            self.mod_root / "common" / "ideas" / COUNTRY_IDEAS_FILENAME,
        ]
        paths.update(p for p in managed_candidates if p.exists())
        for c in self.countries.values():
            if not c.managed: continue
            for rel in (c.country_file, c.history_file):
                if rel:
                    p = self.mod_root / rel
                    if p.exists(): paths.add(p)
            flag = self.mod_root / "gfx" / "flags" / f"{c.tag}.tga"
            if flag.exists(): paths.add(flag)
        for descriptor in descriptor_files_for_mod(self.mod_root):
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
        # Auto-fill capitals for managed countries that now own land and do not have one.
        for c in self.countries.values():
            if not c.managed:
                continue
            owned = self.owned_provinces(c.tag)
            if c.capital <= 0 and owned:
                c.capital = owned[0]

        backup = self._backup(dirty_provinces)
        ensure_total_conversion_name_paths(self.mod_root)
        self._write_tag_file()
        self._write_country_files()
        self._write_localisation()
        self._write_ideas()
        write_total_conversion_override_localisation(self.mod_root)
        for pid in sorted(dirty_provinces):
            if pid in self.map_data.water_provinces:
                continue
            path = self.map_data.province_history.get(pid)
            if path is None:
                continue
            owner = self.assignments.get(pid)
            if not owner or owner == UNOWNED_TAG:
                remove_top_level_assignment(path, "owner")
                remove_top_level_assignment(path, "controller")
                continue
            set_top_level_assignment(path, "owner", owner)
            set_top_level_assignment(path, "controller", owner)
            ensure_top_level_line(path, f"add_core = {owner}")
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
        self.wait_visibility(); self.focus_set()

    def pick_colour(self):
        try: initial = parse_hex_colour(self.colour_var.get())
        except Exception: initial = "#808080"
        chosen = colorchooser.askcolor(initialcolor=initial, parent=self)[1]
        if chosen: self.colour_var.set(chosen.upper())

    def ok(self):
        try: colour = parse_hex_colour(self.colour_var.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self); return
        raw_id = self.id_var.get().strip()
        if not raw_id:
            messagebox.showerror(APP_TITLE, "Codename cannot be empty.", parent=self); return
        self.result = {
            "id": safe_id(raw_id),
            "loc_name": self.loc_var.get().strip() or pretty_name(safe_id(raw_id)),
            "colour": colour,
            "group_id": self.group_var.get() if self.group_var else None,
        }
        self.destroy()


class CharacterFrame(ttk.Frame):
    def __init__(self, parent, role: str, initial: CharacterInfo,
                 cultures: Sequence[str], religions: Sequence[str], personalities: Sequence[str],
                 optional: bool = False):
        super().__init__(parent, padding=8)
        self.role = role
        self.optional = optional
        self.cultures = list(cultures)
        self.religions = list(religions)
        self.personalities = list(personalities)
        self.enabled_var = tk.BooleanVar(value=initial.enabled if optional else True)
        row = 0
        if optional:
            ttk.Checkbutton(self, text=f"Have starting {role.lower()}", variable=self.enabled_var,
                            command=self._toggle).grid(row=row, column=0, columnspan=4, sticky="w", pady=(0, 8)); row += 1
        self.columnconfigure(1, weight=1); self.columnconfigure(3, weight=1)
        self.name_var = tk.StringVar(value=initial.name)
        self.dynasty_var = tk.StringVar(value=initial.dynasty)
        self.age_var = tk.IntVar(value=initial.age)
        self.gender_var = tk.StringVar(value=initial.gender)
        self.adm_var = tk.IntVar(value=initial.adm); self.dip_var = tk.IntVar(value=initial.dip); self.mil_var = tk.IntVar(value=initial.mil)
        self.culture_var = tk.StringVar(value=initial.culture)
        self.religion_var = tk.StringVar(value=initial.religion)
        self.claim_var = tk.IntVar(value=initial.claim)
        ttk.Label(self, text="Name").grid(row=row,column=0,sticky="w",pady=3)
        ttk.Entry(self,textvariable=self.name_var).grid(row=row,column=1,sticky="ew",pady=3,padx=(4,12))
        ttk.Label(self,text="Dynasty").grid(row=row,column=2,sticky="w",pady=3)
        ttk.Entry(self,textvariable=self.dynasty_var).grid(row=row,column=3,sticky="ew",pady=3,padx=(4,0)); row += 1
        ttk.Label(self,text="Age").grid(row=row,column=0,sticky="w",pady=3)
        ttk.Spinbox(self,from_=0,to=120,textvariable=self.age_var,width=7).grid(row=row,column=1,sticky="w",pady=3,padx=(4,12))
        ttk.Label(self,text="Gender").grid(row=row,column=2,sticky="w",pady=3)
        ttk.Combobox(self,textvariable=self.gender_var,values=["Male","Female","Unspecified"],state="readonly",width=14).grid(row=row,column=3,sticky="w",pady=3,padx=(4,0)); row += 1
        stats = ttk.Frame(self); stats.grid(row=row,column=0,columnspan=4,sticky="w",pady=5)
        for label,var in (("ADM",self.adm_var),("DIP",self.dip_var),("MIL",self.mil_var)):
            ttk.Label(stats,text=label).pack(side="left",padx=(0,3)); ttk.Spinbox(stats,from_=0,to=6,textvariable=var,width=4).pack(side="left",padx=(0,10))
        if role == "Heir":
            ttk.Label(stats,text="Claim").pack(side="left",padx=(8,3)); ttk.Spinbox(stats,from_=0,to=100,textvariable=self.claim_var,width=5).pack(side="left")
        row += 1
        ttk.Label(self,text="Culture").grid(row=row,column=0,sticky="w",pady=3)
        ttk.Combobox(self,textvariable=self.culture_var,values=self.cultures,state="normal").grid(row=row,column=1,sticky="ew",pady=3,padx=(4,12))
        ttk.Label(self,text="Religion").grid(row=row,column=2,sticky="w",pady=3)
        ttk.Combobox(self,textvariable=self.religion_var,values=self.religions,state="normal").grid(row=row,column=3,sticky="ew",pady=3,padx=(4,0)); row += 1
        ttk.Label(self,text="Traits (Ctrl/Shift for several)").grid(row=row,column=0,columnspan=4,sticky="w",pady=(8,3)); row += 1
        list_frame = ttk.Frame(self); list_frame.grid(row=row,column=0,columnspan=4,sticky="nsew")
        self.rowconfigure(row, weight=1)
        self.trait_list = tk.Listbox(list_frame, selectmode="extended", height=8, exportselection=False)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.trait_list.yview); self.trait_list.configure(yscrollcommand=sb.set)
        self.trait_list.pack(side="left",fill="both",expand=True); sb.pack(side="right",fill="y")
        for t in self.personalities: self.trait_list.insert("end", t)
        selected = set(initial.traits)
        for i,t in enumerate(self.personalities):
            if t in selected: self.trait_list.selection_set(i)
        self._toggle()

    def _toggle(self):
        enabled = self.enabled_var.get() or not self.optional
        state = "normal" if enabled else "disabled"
        for child in self.winfo_children():
            if isinstance(child, ttk.Checkbutton) and self.optional:
                continue
            try: child.configure(state=state)
            except tk.TclError: pass

    def value(self) -> CharacterInfo:
        def clamp(var, lo, hi, fallback):
            try: return max(lo, min(hi, int(var.get())))
            except Exception: return fallback
        traits = [self.personalities[i] for i in self.trait_list.curselection() if i < len(self.personalities)]
        return CharacterInfo(
            enabled=(self.enabled_var.get() if self.optional else True),
            name=self.name_var.get().strip(), dynasty=self.dynasty_var.get().strip(),
            adm=clamp(self.adm_var,0,6,3), dip=clamp(self.dip_var,0,6,3), mil=clamp(self.mil_var,0,6,3),
            age=clamp(self.age_var,0,120,30), gender=self.gender_var.get(),
            culture=self.culture_var.get().strip(), religion=self.religion_var.get().strip(),
            claim=clamp(self.claim_var,0,100,100), traits=traits,
        )


class NationalIdeasDialog(tk.Toplevel):
    def __init__(self, parent, initial: NationalIdeasInfo):
        super().__init__(parent)
        self.title("Custom national ideas")
        self.geometry("760x720"); self.minsize(650,560)
        self.transient(parent); self.grab_set(); self.result = None
        main = ttk.Frame(self,padding=10); main.pack(fill="both",expand=True)
        ttk.Label(main,text="Enter normal EU4 country modifier lines (for example: discipline = 0.05).",wraplength=720).pack(anchor="w")
        canvas = tk.Canvas(main, highlightthickness=0); sb=ttk.Scrollbar(main,orient="vertical",command=canvas.yview); canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y"); canvas.pack(side="left",fill="both",expand=True)
        inner=ttk.Frame(canvas); win=canvas.create_window((0,0),window=inner,anchor="nw")
        inner.bind("<Configure>",lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",lambda e: canvas.itemconfigure(win,width=e.width))
        self.trad = self._text_section(inner,"Traditions / start",initial.traditions,0)
        self.idea_name_vars=[]; self.idea_texts=[]
        row=1
        for i,rec in enumerate(initial.ideas,1):
            box=ttk.LabelFrame(inner,text=f"Idea {i}",padding=6); box.grid(row=row,column=0,sticky="ew",pady=4); box.columnconfigure(1,weight=1)
            nv=tk.StringVar(value=rec.get("name",f"Idea {i}")); self.idea_name_vars.append(nv)
            ttk.Label(box,text="Name").grid(row=0,column=0,sticky="w"); ttk.Entry(box,textvariable=nv).grid(row=0,column=1,sticky="ew",padx=(5,0))
            txt=tk.Text(box,height=3,width=60); txt.grid(row=1,column=0,columnspan=2,sticky="ew",pady=(5,0)); txt.insert("1.0",rec.get("modifiers","")); self.idea_texts.append(txt)
            row+=1
        self.amb = self._text_section(inner,"Ambition / bonus",initial.ambition,row)
        buttons=ttk.Frame(self,padding=(10,5,10,10)); buttons.pack(fill="x")
        ttk.Button(buttons,text="Cancel",command=self.destroy).pack(side="right")
        ttk.Button(buttons,text="OK",command=self.ok).pack(side="right",padx=(0,6))

    def _text_section(self,parent,title,value,row):
        box=ttk.LabelFrame(parent,text=title,padding=6); box.grid(row=row,column=0,sticky="ew",pady=4)
        txt=tk.Text(box,height=4,width=60); txt.pack(fill="x"); txt.insert("1.0",value); return txt

    def ok(self):
        self.result = NationalIdeasInfo(
            mode="custom",
            traditions=self.trad.get("1.0","end").strip(),
            ambition=self.amb.get("1.0","end").strip(),
            ideas=[{"name":v.get().strip() or f"Idea {i+1}","modifiers":t.get("1.0","end").strip()} for i,(v,t) in enumerate(zip(self.idea_name_vars,self.idea_texts))],
        )
        self.destroy()


class CountryDialog(tk.Toplevel):
    PATTERNS = ["Solid","Horizontal bicolor","Horizontal tricolor","Vertical bicolor","Vertical tricolor","Diagonal","Quartered","Center cross","Nordic cross","Saltire"]
    EMBLEMS = ["None","Circle","Diamond","Star","Crescent","Ring"]

    def __init__(self, parent, model: CountryLayerModel, initial: CountryInfo, new_country: bool):
        super().__init__(parent)
        self.model=model; self.initial=initial; self.new_country=new_country; self.result=None; self.upload_selected: Optional[Path]=None
        self.title("New country" if new_country else f"Edit {initial.tag} — {initial.name}")
        self.geometry("900x760"); self.minsize(760,620); self.transient(parent); self.grab_set()
        outer=ttk.Frame(self,padding=10); outer.pack(fill="both",expand=True)
        self.nb=ttk.Notebook(outer); self.nb.pack(fill="both",expand=True)
        self.identity=ttk.Frame(self.nb,padding=10); self.setup=ttk.Frame(self.nb,padding=10); self.court=ttk.Frame(self.nb,padding=5); self.estates_tab=ttk.Frame(self.nb,padding=10); self.ideas_tab=ttk.Frame(self.nb,padding=10)
        self.nb.add(self.identity,text="Identity & flag"); self.nb.add(self.setup,text="Starting setup"); self.nb.add(self.court,text="Ruler / heir / consort"); self.nb.add(self.estates_tab,text="Estates"); self.nb.add(self.ideas_tab,text="National ideas")
        self._identity_tab(); self._setup_tab(); self._court_tab(); self._estates_tab(); self._ideas_tab()
        buttons=ttk.Frame(outer); buttons.pack(fill="x",pady=(8,0))
        ttk.Button(buttons,text="Cancel",command=self.destroy).pack(side="right")
        ttk.Button(buttons,text="OK",command=self.ok).pack(side="right",padx=(0,6))

    def _label_entry(self,parent,row,label,var,width=30):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky="w",pady=4)
        e=ttk.Entry(parent,textvariable=var,width=width); e.grid(row=row,column=1,sticky="ew",pady=4,padx=(6,0)); return e

    def _identity_tab(self):
        f=self.identity; f.columnconfigure(1,weight=1); f.columnconfigure(3,weight=1)
        self.tag_var=tk.StringVar(value=self.initial.tag); self.name_var=tk.StringVar(value=self.initial.name); self.adj_var=tk.StringVar(value=self.initial.adjective); self.country_colour_var=tk.StringVar(value=self.initial.colour); self.gfx_var=tk.StringVar(value=self.initial.graphical_culture)
        ttk.Label(f,text="Country tag (3 letters)").grid(row=0,column=0,sticky="w",pady=4); e=ttk.Entry(f,textvariable=self.tag_var,width=10); e.grid(row=0,column=1,sticky="w",pady=4,padx=(6,0));
        if not self.new_country: e.state(["disabled"])
        ttk.Label(f,text="Name").grid(row=1,column=0,sticky="w",pady=4); ttk.Entry(f,textvariable=self.name_var).grid(row=1,column=1,columnspan=3,sticky="ew",pady=4,padx=(6,0))
        ttk.Label(f,text="Adjective").grid(row=2,column=0,sticky="w",pady=4); ttk.Entry(f,textvariable=self.adj_var).grid(row=2,column=1,columnspan=3,sticky="ew",pady=4,padx=(6,0))
        ttk.Label(f,text="Map colour").grid(row=3,column=0,sticky="w",pady=4); cr=ttk.Frame(f); cr.grid(row=3,column=1,sticky="w",pady=4,padx=(6,0)); ttk.Entry(cr,textvariable=self.country_colour_var,width=14).pack(side="left"); ttk.Button(cr,text="Pick…",command=self.pick_country_colour).pack(side="left",padx=(5,0))
        ttk.Label(f,text="Graphical culture").grid(row=3,column=2,sticky="w",pady=4,padx=(18,0)); ttk.Combobox(f,textvariable=self.gfx_var,values=self.model.graphical_cultures,state="normal").grid(row=3,column=3,sticky="ew",pady=4,padx=(6,0))
        flag=ttk.LabelFrame(f,text="Flag",padding=8); flag.grid(row=4,column=0,columnspan=4,sticky="nsew",pady=(12,0)); flag.columnconfigure(1,weight=1)
        self.flag_mode_var=tk.StringVar(value=self.initial.flag_mode)
        ttk.Radiobutton(flag,text="Nation Designer-style (baked to static TGA)",value="designer",variable=self.flag_mode_var,command=self.refresh_flag_controls).grid(row=0,column=0,columnspan=2,sticky="w")
        ttk.Radiobutton(flag,text="Import PNG/TGA/BMP/JPEG",value="upload",variable=self.flag_mode_var,command=self.refresh_flag_controls).grid(row=1,column=0,columnspan=2,sticky="w")
        self.upload_label=tk.StringVar(value=self.initial.flag_source or "No image selected")
        self.upload_button=ttk.Button(flag,text="Choose image…",command=self.choose_flag); self.upload_button.grid(row=2,column=0,sticky="w",pady=5)
        ttk.Label(flag,textvariable=self.upload_label,wraplength=450).grid(row=2,column=1,sticky="w",padx=(8,0))
        self.pattern_var=tk.StringVar(value=self.initial.flag_pattern); self.emblem_var=tk.StringVar(value=self.initial.flag_emblem)
        ttk.Label(flag,text="Pattern").grid(row=3,column=0,sticky="w",pady=4); self.pattern_box=ttk.Combobox(flag,textvariable=self.pattern_var,values=self.PATTERNS,state="readonly"); self.pattern_box.grid(row=3,column=1,sticky="ew",pady=4)
        ttk.Label(flag,text="Emblem").grid(row=4,column=0,sticky="w",pady=4); self.emblem_box=ttk.Combobox(flag,textvariable=self.emblem_var,values=self.EMBLEMS,state="readonly"); self.emblem_box.grid(row=4,column=1,sticky="ew",pady=4)
        self.flag_colour_vars=[tk.StringVar(value=c) for c in self.initial.flag_colours]
        cframe=ttk.Frame(flag); cframe.grid(row=5,column=0,columnspan=2,sticky="w",pady=5)
        self.flag_colour_entries=[]
        for i,var in enumerate(self.flag_colour_vars,1):
            ttk.Label(cframe,text=f"Colour {i}").pack(side="left",padx=(0,3)); ent=ttk.Entry(cframe,textvariable=var,width=10); ent.pack(side="left"); self.flag_colour_entries.append(ent); ttk.Button(cframe,text="…",width=3,command=lambda v=var:self.pick_var_colour(v)).pack(side="left",padx=(2,8))
        self.flag_preview=tk.Canvas(flag,width=192,height=192,highlightthickness=1); self.flag_preview.grid(row=0,column=2,rowspan=6,padx=(14,0),sticky="n")
        for var in [self.pattern_var,self.emblem_var,*self.flag_colour_vars]: var.trace_add("write",lambda *_:self.update_flag_preview())
        self.refresh_flag_controls(); self.update_flag_preview()

    def pick_country_colour(self): self.pick_var_colour(self.country_colour_var)
    def pick_var_colour(self,var):
        try: init=parse_hex_colour(var.get())
        except Exception: init="#808080"
        c=colorchooser.askcolor(initialcolor=init,parent=self)[1]
        if c: var.set(c.upper())

    def choose_flag(self):
        fn=filedialog.askopenfilename(parent=self,title="Choose flag image",filetypes=[("Images","*.png *.tga *.bmp *.jpg *.jpeg *.tif *.tiff"),("All files","*.*")])
        if fn:
            self.upload_selected=Path(fn); self.upload_label.set(fn); self.update_flag_preview()

    def refresh_flag_controls(self):
        designer=self.flag_mode_var.get()=="designer"
        for w in [self.pattern_box,self.emblem_box,*self.flag_colour_entries]:
            try: w.configure(state="readonly" if isinstance(w,ttk.Combobox) and designer else ("normal" if designer else "disabled"))
            except Exception: pass
        self.upload_button.configure(state="disabled" if designer else "normal")
        self.update_flag_preview()

    def update_flag_preview(self):
        if not hasattr(self,"flag_preview"): return
        try:
            if self.flag_mode_var.get()=="upload":
                source=self.upload_selected or self.model._resolve_flag_source(self.initial.flag_source)
                if source and Path(source).exists():
                    with Image.open(source) as im: img=ImageOps.fit(im.convert("RGB"),(192,192),method=Image.Resampling.LANCZOS)
                else: img=Image.new("RGB",(192,192),(80,80,80))
            else:
                cols=[parse_hex_colour(v.get()) for v in self.flag_colour_vars]
                img=generate_designer_flag(self.pattern_var.get(),cols,self.emblem_var.get(),192)
            self._flag_tk=ImageTk.PhotoImage(img); self.flag_preview.delete("all"); self.flag_preview.create_image(0,0,image=self._flag_tk,anchor="nw")
        except Exception:
            pass

    def _setup_tab(self):
        f=self.setup; f.columnconfigure(1,weight=1); f.columnconfigure(3,weight=1)
        c=self.initial
        self.gov_var=tk.StringVar(value=c.government); self.rank_var=tk.IntVar(value=c.government_rank); self.reform_var=tk.StringVar(value=c.government_reform); self.stab_var=tk.IntVar(value=c.stability); self.capital_var=tk.IntVar(value=c.capital)
        self.religion_var=tk.StringVar(value=c.religion); self.primary_var=tk.StringVar(value=c.primary_culture); self.tech_group_var=tk.StringVar(value=c.technology_group); self.start_date_var=tk.StringVar(value=c.start_date)
        self.treasury_var=tk.DoubleVar(value=c.treasury); self.prestige_var=tk.DoubleVar(value=c.prestige)
        ttk.Label(f,text="Government type").grid(row=0,column=0,sticky="w",pady=4); ttk.Combobox(f,textvariable=self.gov_var,values=self.model.governments,state="normal").grid(row=0,column=1,sticky="ew",padx=(6,12),pady=4)
        ttk.Label(f,text="Government rank (1–3)").grid(row=0,column=2,sticky="w",pady=4); ttk.Spinbox(f,from_=1,to=3,textvariable=self.rank_var,width=6).grid(row=0,column=3,sticky="w",padx=(6,0),pady=4)
        ttk.Label(f,text="Starting reform (optional)").grid(row=1,column=0,sticky="w",pady=4); ttk.Combobox(f,textvariable=self.reform_var,values=self.model.government_reforms,state="normal").grid(row=1,column=1,sticky="ew",padx=(6,12),pady=4)
        ttk.Label(f,text="Starting stability").grid(row=1,column=2,sticky="w",pady=4); ttk.Spinbox(f,from_=-3,to=3,textvariable=self.stab_var,width=6).grid(row=1,column=3,sticky="w",padx=(6,0),pady=4)
        ttk.Label(f,text="Capital province ID").grid(row=2,column=0,sticky="w",pady=4); ttk.Spinbox(f,from_=0,to=99999,textvariable=self.capital_var,width=10).grid(row=2,column=1,sticky="w",padx=(6,12),pady=4)
        ttk.Label(f,text="Start date").grid(row=2,column=2,sticky="w",pady=4); ttk.Entry(f,textvariable=self.start_date_var,width=14).grid(row=2,column=3,sticky="w",padx=(6,0),pady=4)
        ttk.Label(f,text="Religion").grid(row=3,column=0,sticky="w",pady=4); ttk.Combobox(f,textvariable=self.religion_var,values=self.model.religions,state="normal").grid(row=3,column=1,sticky="ew",padx=(6,12),pady=4)
        ttk.Label(f,text="Primary culture").grid(row=3,column=2,sticky="w",pady=4); ttk.Combobox(f,textvariable=self.primary_var,values=sorted(self.model.culture_model.items),state="normal").grid(row=3,column=3,sticky="ew",padx=(6,0),pady=4)
        ttk.Label(f,text="Technology group").grid(row=4,column=0,sticky="w",pady=4); ttk.Combobox(f,textvariable=self.tech_group_var,values=self.model.tech_groups,state="normal").grid(row=4,column=1,sticky="ew",padx=(6,12),pady=4)
        ttk.Label(f,text="Treasury").grid(row=4,column=2,sticky="w",pady=4); ttk.Spinbox(f,from_=-100000,to=100000,increment=10,textvariable=self.treasury_var,width=10).grid(row=4,column=3,sticky="w",padx=(6,0),pady=4)
        ttk.Label(f,text="Prestige").grid(row=5,column=0,sticky="w",pady=4); ttk.Spinbox(f,from_=-100,to=100,increment=5,textvariable=self.prestige_var,width=10).grid(row=5,column=1,sticky="w",padx=(6,12),pady=4)
        # accepted cultures
        ttk.Label(f,text="Accepted cultures").grid(row=6,column=0,sticky="nw",pady=(10,4))
        lf=ttk.Frame(f); lf.grid(row=6,column=1,columnspan=3,sticky="nsew",pady=(10,4)); f.rowconfigure(6,weight=1)
        self.accepted_list=tk.Listbox(lf,selectmode="extended",height=7,exportselection=False); sb=ttk.Scrollbar(lf,orient="vertical",command=self.accepted_list.yview); self.accepted_list.configure(yscrollcommand=sb.set); self.accepted_list.pack(side="left",fill="both",expand=True); sb.pack(side="right",fill="y")
        culture_ids=sorted(self.model.culture_model.items)
        for cid in culture_ids: self.accepted_list.insert("end",cid)
        selected=set(c.accepted_cultures)
        for i,cid in enumerate(culture_ids):
            if cid in selected: self.accepted_list.selection_set(i)
        self.accepted_ids=culture_ids
        tech=ttk.LabelFrame(f,text="Additional starting technology (advanced)",padding=6); tech.grid(row=7,column=0,columnspan=4,sticky="ew",pady=(8,0))
        self.custom_tech_var=tk.BooleanVar(value=c.custom_tech_levels); ttk.Checkbutton(tech,text="Add ADM/DIP/MIL technologies with effects (off by default; values are additive)",variable=self.custom_tech_var).pack(side="left")
        self.admtech_var=tk.IntVar(value=c.adm_tech); self.diptech_var=tk.IntVar(value=c.dip_tech); self.miltech_var=tk.IntVar(value=c.mil_tech)
        for label,var in (("ADM",self.admtech_var),("DIP",self.diptech_var),("MIL",self.miltech_var)):
            ttk.Label(tech,text=label).pack(side="left",padx=(12,3)); ttk.Spinbox(tech,from_=0,to=99,textvariable=var,width=5).pack(side="left")

    def _court_tab(self):
        cultures=sorted(self.model.culture_model.items); religions=self.model.religions; traits=self.model.personalities
        self.court_nb=ttk.Notebook(self.court); self.court_nb.pack(fill="both",expand=True)
        self.ruler_frame=CharacterFrame(self.court_nb,"Ruler",self.initial.ruler,cultures,religions,traits,False); self.heir_frame=CharacterFrame(self.court_nb,"Heir",self.initial.heir,cultures,religions,traits,True); self.consort_frame=CharacterFrame(self.court_nb,"Consort",self.initial.consort,cultures,religions,traits,True)
        self.court_nb.add(self.ruler_frame,text="Ruler"); self.court_nb.add(self.heir_frame,text="Heir (optional)"); self.court_nb.add(self.consort_frame,text="Consort (optional)")

    def _estates_tab(self):
        self.estate_mode_var=tk.StringVar(value=self.initial.estate_mode)
        ttk.Radiobutton(self.estates_tab,text="Default estate shares (let EU4 handle them)",variable=self.estate_mode_var,value="default").pack(anchor="w")
        ttk.Radiobutton(self.estates_tab,text="Custom starting shares",variable=self.estate_mode_var,value="custom").pack(anchor="w")
        ttk.Label(self.estates_tab,text="Shares are percentages. Crownland is 100 minus the sum below. Keep the total ≤ 100.",wraplength=760).pack(anchor="w",pady=(8,4))
        frame=ttk.Frame(self.estates_tab); frame.pack(fill="both",expand=True)
        self.estate_tree=ttk.Treeview(frame,columns=("share",),show="tree headings",selectmode="browse"); self.estate_tree.heading("#0",text="Estate"); self.estate_tree.heading("share",text="Share %"); self.estate_tree.column("#0",width=300); self.estate_tree.column("share",width=100,anchor="e")
        sb=ttk.Scrollbar(frame,orient="vertical",command=self.estate_tree.yview); self.estate_tree.configure(yscrollcommand=sb.set); self.estate_tree.pack(side="left",fill="both",expand=True); sb.pack(side="right",fill="y")
        for estate in self.model.estates: self.estate_tree.insert("", "end", iid=estate, text=estate, values=(self.initial.estate_shares.get(estate,0.0),))
        edit=ttk.Frame(self.estates_tab); edit.pack(fill="x",pady=(6,0)); self.estate_share_var=tk.DoubleVar(value=0.0); ttk.Label(edit,text="Selected estate share").pack(side="left"); ttk.Spinbox(edit,from_=0,to=100,increment=0.5,textvariable=self.estate_share_var,width=8).pack(side="left",padx=5); ttk.Button(edit,text="Set",command=self.set_estate_share).pack(side="left")
        self.estate_tree.bind("<<TreeviewSelect>>",self.estate_selected)

    def estate_selected(self,_e=None):
        sel=self.estate_tree.selection()
        if sel:
            try:self.estate_share_var.set(float(self.estate_tree.item(sel[0],"values")[0]))
            except Exception:pass
    def set_estate_share(self):
        sel=self.estate_tree.selection()
        if not sel:return
        try:v=max(0.0,min(100.0,float(self.estate_share_var.get())))
        except Exception:v=0.0
        self.estate_tree.item(sel[0],values=(v,))

    def _ideas_tab(self):
        self.idea_mode_var=tk.StringVar(value=self.initial.national_ideas.mode); self.idea_config=NationalIdeasInfo.from_dict(self.initial.national_ideas.to_dict())
        ttk.Radiobutton(self.ideas_tab,text="Default national ideas (do not create a tag-specific idea set)",variable=self.idea_mode_var,value="default").pack(anchor="w")
        ttk.Radiobutton(self.ideas_tab,text="Custom tag-specific national ideas",variable=self.idea_mode_var,value="custom").pack(anchor="w")
        ttk.Label(self.ideas_tab,text="Custom ideas are written as a free=yes idea group triggered by this country tag.",wraplength=760).pack(anchor="w",pady=(10,6))
        ttk.Button(self.ideas_tab,text="Edit custom ideas…",command=self.edit_ideas).pack(anchor="w")

    def edit_ideas(self):
        dlg=NationalIdeasDialog(self,self.idea_config); self.wait_window(dlg)
        if dlg.result:
            self.idea_config=dlg.result; self.idea_mode_var.set("custom")

    def ok(self):
        tag=safe_country_tag(self.tag_var.get()) if self.new_country else self.initial.tag
        if not COUNTRY_TAG_RE.fullmatch(tag):
            messagebox.showerror(APP_TITLE,"Country tag must be exactly three letters A-Z.",parent=self); return
        if self.new_country and tag in self.model.countries:
            messagebox.showerror(APP_TITLE,f"Tag {tag} already exists.",parent=self); return
        name=self.name_var.get().strip()
        if not name:
            messagebox.showerror(APP_TITLE,"Country name cannot be empty.",parent=self); return
        try: colour=parse_hex_colour(self.country_colour_var.get()); flag_cols=[parse_hex_colour(v.get()) for v in self.flag_colour_vars]
        except Exception as exc:
            messagebox.showerror(APP_TITLE,str(exc),parent=self); return
        try:
            rank=max(1,min(3,int(self.rank_var.get()))); stab=max(-3,min(3,int(self.stab_var.get()))); capital=max(0,int(self.capital_var.get()))
        except Exception:
            messagebox.showerror(APP_TITLE,"Rank, stability and capital must be numeric.",parent=self); return
        accepted=[self.accepted_ids[i] for i in self.accepted_list.curselection()]
        estate_shares={}
        for iid in self.estate_tree.get_children():
            try:v=float(self.estate_tree.item(iid,"values")[0])
            except Exception:v=0.0
            if v>0: estate_shares[iid]=v
        if self.estate_mode_var.get()=="custom" and sum(estate_shares.values())>100.0001:
            messagebox.showerror(APP_TITLE,f"Estate shares total {sum(estate_shares.values()):.1f}%, above 100%.",parent=self); return
        flag_source=self.initial.flag_source
        if self.flag_mode_var.get()=="upload":
            if self.upload_selected:
                try: flag_source=self.model.store_flag_source(tag,self.upload_selected)
                except Exception as exc:
                    messagebox.showerror(APP_TITLE,f"Could not copy flag source:\n{exc}",parent=self); return
            elif not self.model._resolve_flag_source(flag_source):
                messagebox.showerror(APP_TITLE,"Choose an image for the uploaded flag.",parent=self); return
        ideas=self.idea_config; ideas.mode=self.idea_mode_var.get()
        c=CountryInfo(
            tag=tag,name=name,adjective=self.adj_var.get().strip() or name,colour=colour,
            original=self.initial.original,managed=True,country_file=self.initial.country_file,history_file=self.initial.history_file,
            graphical_culture=self.gfx_var.get().strip() or "westerngfx",government=self.gov_var.get().strip() or "monarchy",
            government_rank=rank,government_reform=self.reform_var.get().strip(),stability=stab,capital=capital,
            religion=self.religion_var.get().strip(),primary_culture=self.primary_var.get().strip(),accepted_cultures=accepted,
            technology_group=self.tech_group_var.get().strip() or "western",custom_tech_levels=self.custom_tech_var.get(),
            adm_tech=max(0,int(self.admtech_var.get())),dip_tech=max(0,int(self.diptech_var.get())),mil_tech=max(0,int(self.miltech_var.get())),
            start_date=self.start_date_var.get().strip() or "1444.11.11",treasury=float(self.treasury_var.get()),prestige=float(self.prestige_var.get()),
            ruler=self.ruler_frame.value(),heir=self.heir_frame.value(),consort=self.consort_frame.value(),
            estate_mode=self.estate_mode_var.get(),estate_shares=estate_shares,national_ideas=ideas,
            flag_mode=self.flag_mode_var.get(),flag_source=flag_source,flag_pattern=self.pattern_var.get(),flag_colours=flag_cols,flag_emblem=self.emblem_var.get(),
        )
        self.result=c; self.destroy()


class SetupPainterApp:
    def __init__(self, root: tk.Tk):
        self.root=root; self.root.title(APP_TITLE); self.root.geometry("1500x900"); self.root.minsize(1050,680)
        script=Path(__file__).resolve()
        # Expected: <mod>/tools/culture_painter/eu4_setup_painter.py (or any filename in that folder)
        self.mod_root=script.parents[2]
        self.map_data: Optional[MapData]=None
        self.culture_model: Optional[CultureLayerModel]=None
        self.country_model: Optional[CountryLayerModel]=None
        self.layer_var=tk.StringVar(value="Cultures"); self.scope_var=tk.StringVar(value="Province"); self.view_var=tk.StringVar(value="Culture"); self.search_var=tk.StringVar(); self.status_var=tk.StringVar(value="Loading…")
        self.selected_id: Optional[str]=None
        self.zoom=.5; self.tk_map=None; self.map_item=None; self.render_after=None; self.hover_pid=0; self.drag_seen:set= set()
        self.dirty={"Cultures":set(),"Countries":set()}; self.undo_stacks={"Cultures":[],"Countries":[]}; self.redo_stacks={"Cultures":[],"Countries":[]}; self.touched:set=set()
        self._build_ui(); self.root.after(50,self._load)

    @property
    def current_model(self):
        return self.culture_model if self.layer_var.get()=="Cultures" else self.country_model

    def _build_ui(self):
        top=ttk.Frame(self.root,padding=(8,7)); top.pack(fill="x")
        ttk.Button(top,text="Save",command=self.save).pack(side="left")
        ttk.Button(top,text="Undo",command=self.undo).pack(side="left",padx=(6,0)); ttk.Button(top,text="Redo",command=self.redo).pack(side="left",padx=(6,14))
        ttk.Label(top,text="Layer:").pack(side="left"); layer=ttk.Combobox(top,textvariable=self.layer_var,values=["Cultures","Countries"],state="readonly",width=12); layer.pack(side="left",padx=(5,14)); layer.bind("<<ComboboxSelected>>",self.layer_changed)
        ttk.Label(top,text="Paint:").pack(side="left"); scope=ttk.Combobox(top,textvariable=self.scope_var,values=["Province","Area","Region"],state="readonly",width=10); scope.pack(side="left",padx=(5,14)); scope.bind("<<ComboboxSelected>>",lambda _e:self.schedule_render())
        ttk.Label(top,text="View:").pack(side="left"); self.view_box=ttk.Combobox(top,textvariable=self.view_var,state="readonly",width=15); self.view_box.pack(side="left",padx=(5,14)); self.view_box.bind("<<ComboboxSelected>>",lambda _e:self.schedule_render())
        ttk.Button(top,text="Zoom −",command=lambda:self.change_zoom(.8)).pack(side="left"); ttk.Button(top,text="Zoom +",command=lambda:self.change_zoom(1.25)).pack(side="left",padx=(5,0))
        ttk.Label(top,textvariable=self.status_var).pack(side="right")
        paned=ttk.Panedwindow(self.root,orient="horizontal"); paned.pack(fill="both",expand=True)
        self.sidebar=ttk.Frame(paned,padding=8,width=350); map_frame=ttk.Frame(paned); paned.add(self.sidebar,weight=0); paned.add(map_frame,weight=1)
        self.sidebar_title=ttk.Label(self.sidebar,text="Cultures",font=("Segoe UI",14,"bold")); self.sidebar_title.pack(anchor="w")
        search=ttk.Entry(self.sidebar,textvariable=self.search_var); search.pack(fill="x",pady=(6,6)); search.bind("<KeyRelease>",lambda _e:self.refresh_tree())
        self.tree=ttk.Treeview(self.sidebar,show="tree",selectmode="browse"); self.tree.pack(fill="both",expand=True); self.tree.bind("<<TreeviewSelect>>",self.tree_selected); self.tree.bind("<Double-1>",lambda _e:self.edit_selected())
        self.buttons=ttk.Frame(self.sidebar); self.buttons.pack(fill="x",pady=(7,0))
        self.selection_label=ttk.Label(self.sidebar,text="Select something to paint.",wraplength=320); self.selection_label.pack(fill="x",pady=(8,0))
        xbar=ttk.Scrollbar(map_frame,orient="horizontal"); ybar=ttk.Scrollbar(map_frame,orient="vertical")
        self.canvas=tk.Canvas(map_frame,background="#1e1e1e",xscrollcommand=xbar.set,yscrollcommand=ybar.set,highlightthickness=0); xbar.config(command=self.canvas.xview); ybar.config(command=self.canvas.yview); xbar.pack(side="bottom",fill="x"); ybar.pack(side="right",fill="y"); self.canvas.pack(side="left",fill="both",expand=True)
        self.canvas.bind("<Button-1>",self.paint_press); self.canvas.bind("<B1-Motion>",self.paint_drag); self.canvas.bind("<ButtonRelease-1>",self.paint_release); self.canvas.bind("<Motion>",self.hover); self.canvas.bind("<MouseWheel>",self.mousewheel); self.canvas.bind("<Button-4>",lambda _e:self.change_zoom(1.15)); self.canvas.bind("<Button-5>",lambda _e:self.change_zoom(.87)); self.canvas.bind("<Button-2>",lambda e:self.canvas.scan_mark(e.x,e.y)); self.canvas.bind("<B2-Motion>",lambda e:self.canvas.scan_dragto(e.x,e.y,gain=1)); self.canvas.bind("<Button-3>",lambda e:self.canvas.scan_mark(e.x,e.y)); self.canvas.bind("<B3-Motion>",lambda e:self.canvas.scan_dragto(e.x,e.y,gain=1))
        self._rebuild_sidebar_buttons(); self._refresh_view_options()

    def _load(self):
        try:
            self.map_data=MapData.load(self.mod_root,self._set_status); self._set_status("Reading cultures…"); self.culture_model=CultureLayerModel(self.mod_root,self.map_data); self._set_status("Reading countries…"); self.country_model=CountryLayerModel(self.mod_root,self.map_data,self.culture_model)
            self.refresh_tree(); self.zoom=min(1.0,max(.15,1000/max(1,self.map_data.width))); self.schedule_render(); self._set_status("Ready")
        except Exception as exc:
            log=Path(__file__).resolve().parent/"eu4_setup_painter_error.log"; log.write_text(traceback.format_exc(),encoding="utf-8"); messagebox.showerror(APP_TITLE,f"Failed to load the mod:\n\n{exc}\n\nSee {log.name} for details."); self._set_status("Load failed")

    def _set_status(self,text): self.status_var.set(text); self.root.update_idletasks()

    def _refresh_view_options(self):
        if self.layer_var.get()=="Cultures":
            self.view_box.configure(values=["Culture","Culture Group"])
            if self.view_var.get() not in ("Culture","Culture Group"): self.view_var.set("Culture")
        else:
            self.view_box.configure(values=["Countries"]); self.view_var.set("Countries")

    def layer_changed(self,_e=None):
        self.selected_id=None; self.search_var.set(""); self.hover_pid=0; self._refresh_view_options(); self._rebuild_sidebar_buttons(); self.refresh_tree(); self.schedule_render()

    def _rebuild_sidebar_buttons(self):
        for w in self.buttons.winfo_children(): w.destroy()
        if self.layer_var.get()=="Cultures":
            ttk.Button(self.buttons,text="New group",command=self.new_group).pack(side="left"); ttk.Button(self.buttons,text="New culture",command=self.new_culture).pack(side="left",padx=(5,0)); ttk.Button(self.buttons,text="Edit",command=self.edit_selected).pack(side="left",padx=(5,0)); self.sidebar_title.configure(text="Cultures")
        else:
            ttk.Button(self.buttons,text="New country",command=self.new_country).pack(side="left"); ttk.Button(self.buttons,text="Edit",command=self.edit_selected).pack(side="left",padx=(5,0)); self.sidebar_title.configure(text="Countries")

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children()); query=self.search_var.get().strip().lower()
        if self.layer_var.get()=="Cultures":
            if not self.culture_model:return
            for gid,group in sorted(self.culture_model.groups.items(),key=lambda x:x[1].loc_name.lower()):
                cultures=[c for c in self.culture_model.items.values() if c.group_id==gid]
                matches_group=query in gid.lower() or query in group.loc_name.lower()
                matches_items=[c for c in cultures if query in c.id.lower() or query in c.loc_name.lower()]
                if query and not matches_group and not matches_items: continue
                gnode=self.tree.insert("","end",iid="g:"+gid,text=f"{group.loc_name}  [{gid}]")
                for c in sorted(cultures,key=lambda x:x.loc_name.lower()):
                    if query and not matches_group and c not in matches_items: continue
                    self.tree.insert(gnode,"end",iid="c:"+c.id,text=f"{c.loc_name}  [{c.id}]")
                self.tree.item(gnode,open=True)
            if self.selected_id and self.tree.exists("c:"+self.selected_id): self.tree.selection_set("c:"+self.selected_id); self.tree.see("c:"+self.selected_id)
        else:
            if not self.country_model:return
            self.tree.insert("","end",iid="n:"+UNOWNED_TAG,text="Unowned / clear ownership")
            for tag,c in sorted(self.country_model.countries.items(),key=lambda x:x[1].name.lower()):
                if query and query not in tag.lower() and query not in c.name.lower(): continue
                suffix=" *" if c.managed else ""
                self.tree.insert("","end",iid="n:"+tag,text=f"{c.name}  [{tag}]{suffix}")
            if self.selected_id and self.tree.exists("n:"+self.selected_id): self.tree.selection_set("n:"+self.selected_id); self.tree.see("n:"+self.selected_id)

    def tree_selected(self,_e=None):
        sel=self.tree.selection()
        if not sel:return
        iid=sel[0]
        if self.layer_var.get()=="Cultures":
            if iid.startswith("c:"):
                cid=iid[2:]; self.selected_id=cid; c=self.culture_model.items[cid]; self.selection_label.configure(text=f"Painting: {c.loc_name} ({cid})\nGroup: {c.group_id}")
            elif iid.startswith("g:"):
                self.selected_id=None; self.selection_label.configure(text="Select a culture inside this group to paint.")
        else:
            tag=iid[2:] if iid.startswith("n:") else iid; self.selected_id=tag
            if tag==UNOWNED_TAG: self.selection_label.configure(text="Painting: Unowned (clears owner/controller)")
            elif tag in self.country_model.countries:
                c=self.country_model.countries[tag]; self.selection_label.configure(text=f"Painting: {c.name} [{tag}]\nColour: {c.colour}")

    # --- Culture editing -----------------------------------------------------
    def new_group(self):
        if not self.culture_model:return
        dlg=EntityDialog(self.root,"New culture group","","","#808080",id_editable=True,colour_label="Group colour (editor + in-game)"); self.root.wait_window(dlg)
        if not dlg.result:return
        try:self.culture_model.create_group(dlg.result["id"],dlg.result["loc_name"],dlg.result["colour"]); self.touched.add("Cultures"); self.refresh_tree(); self.schedule_render()
        except Exception as exc:messagebox.showerror(APP_TITLE,str(exc))

    def new_culture(self):
        if not self.culture_model:return
        groups=sorted(self.culture_model.groups)
        if not groups:messagebox.showinfo(APP_TITLE,"Create a culture group first.");return
        default=groups[0]; sel=self.tree.selection()
        if sel:
            iid=sel[0]
            if iid.startswith("g:"):default=iid[2:]
            elif iid.startswith("c:"):default=self.culture_model.items[iid[2:]].group_id
        dlg=EntityDialog(self.root,"New culture","","","#808080",groups=groups,group_value=default,id_editable=True); self.root.wait_window(dlg)
        if not dlg.result:return
        try:
            cid=self.culture_model.create_item(dlg.result["id"],dlg.result["loc_name"],dlg.result["colour"],dlg.result["group_id"]); self.selected_id=cid; self.touched.add("Cultures"); self.refresh_tree(); self.schedule_render()
        except Exception as exc:messagebox.showerror(APP_TITLE,str(exc))

    # --- Country editing -----------------------------------------------------
    def _suggest_tag(self)->str:
        used=set(self.country_model.countries)
        letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for a in letters:
            for b in letters:
                for c in letters:
                    tag=a+b+c
                    if tag not in used and tag not in FORBIDDEN_COUNTRY_TAGS:
                        return tag
        return "ZZZ"

    def new_country(self):
        if not self.country_model:return
        tag=self._suggest_tag(); cultures=sorted(self.culture_model.items); religions=self.country_model.religions
        initial=CountryInfo(tag=tag,name="New Country",adjective="New",colour=deterministic_colour("country:"+tag),original=False,managed=True,primary_culture=(cultures[0] if cultures else ""),religion=(religions[0] if religions else ""),technology_group=(self.country_model.tech_groups[0] if self.country_model.tech_groups else "western"))
        initial.ruler.culture=initial.primary_culture; initial.ruler.religion=initial.religion
        dlg=CountryDialog(self.root,self.country_model,initial,True); self.root.wait_window(dlg)
        if not dlg.result:return
        try:
            tag=self.country_model.create_country(dlg.result); self.selected_id=tag; self.touched.add("Countries"); self.refresh_tree(); self.schedule_render()
        except Exception as exc:messagebox.showerror(APP_TITLE,str(exc))

    def edit_selected(self):
        sel=self.tree.selection()
        if not sel:return
        iid=sel[0]
        if self.layer_var.get()=="Cultures":
            if iid.startswith("g:"):
                gid=iid[2:]; g=self.culture_model.groups[gid]; dlg=EntityDialog(self.root,"Edit culture group",gid,g.loc_name,g.colour,id_editable=False,colour_label="Group colour (editor + in-game)"); self.root.wait_window(dlg)
                if dlg.result:
                    try:self.culture_model.edit_group(gid,dlg.result["loc_name"],dlg.result["colour"]); self.touched.add("Cultures"); self.refresh_tree(); self.schedule_render()
                    except Exception as exc:messagebox.showerror(APP_TITLE,str(exc))
            elif iid.startswith("c:"):
                cid=iid[2:]; c=self.culture_model.items[cid]; dlg=EntityDialog(self.root,"Edit culture",cid,c.loc_name,c.colour,groups=sorted(self.culture_model.groups),group_value=c.group_id,id_editable=False); self.root.wait_window(dlg)
                if dlg.result:
                    try:self.culture_model.edit_item(cid,dlg.result["loc_name"],dlg.result["colour"],dlg.result["group_id"]); self.touched.add("Cultures"); self.refresh_tree(); self.schedule_render()
                    except Exception as exc:messagebox.showerror(APP_TITLE,str(exc))
        else:
            if not iid.startswith("n:") or iid[2:]==UNOWNED_TAG:return
            tag=iid[2:]; c=self.country_model.countries[tag]; dlg=CountryDialog(self.root,self.country_model,c,False); self.root.wait_window(dlg)
            if dlg.result:
                try:self.country_model.update_country(tag,dlg.result); self.selected_id=tag; self.touched.add("Countries"); self.refresh_tree(); self.schedule_render()
                except Exception as exc:messagebox.showerror(APP_TITLE,str(exc))

    # --- Map interaction -----------------------------------------------------
    def province_at_event(self,event)->int:
        if not self.map_data:return 0
        x=int(self.canvas.canvasx(event.x)/self.zoom); y=int(self.canvas.canvasy(event.y)/self.zoom)
        if not (0<=x<self.map_data.width and 0<=y<self.map_data.height):return 0
        return int(self.map_data.province_raster[y,x])

    def paint_press(self,event):self.drag_seen.clear();self._paint_event(event)
    def paint_drag(self,event):self._paint_event(event)
    def paint_release(self,_event):self.drag_seen.clear()

    def _paint_event(self,event):
        model=self.current_model
        if not model or not self.map_data or not self.selected_id:return
        pid=self.province_at_event(event)
        if pid<=0 or pid in self.map_data.water_provinces:return
        scope=self.scope_var.get(); targets=self.map_data.selection_for(pid,scope)
        marker=(scope,pid if scope=="Province" else hash(tuple(sorted(targets))))
        if marker in self.drag_seen:return
        self.drag_seen.add(marker)
        if not targets:return
        target_value=self.selected_id
        if self.layer_var.get()=="Countries" and target_value==UNOWNED_TAG:target_value=None
        before={p:model.assignments.get(p) for p in targets}; after={p:target_value for p in targets}
        if all(before[p]==target_value for p in targets):return
        for p in targets:model.assignments[p]=target_value;self.dirty[self.layer_var.get()].add(p)
        self.undo_stacks[self.layer_var.get()].append(PaintAction(before,after)); self.redo_stacks[self.layer_var.get()].clear(); self.touched.add(self.layer_var.get()); self.schedule_render()

    def hover(self,event):
        if not self.map_data:return
        pid=self.province_at_event(event)
        if pid==self.hover_pid:return
        self.hover_pid=pid
        if pid<=0:self._set_status("Outside province map");return
        if pid in self.map_data.water_provinces:self._set_status(f"Province {pid} — water (locked)");return
        area=self.map_data.province_to_area.get(pid,"—"); region=self.map_data.area_to_region.get(area,"—") if area!="—" else "—"
        if self.layer_var.get()=="Cultures":
            cid=self.culture_model.assignments.get(pid); label=self.culture_model.items[cid].loc_name if cid in self.culture_model.items else (cid or "No culture")
        else:
            tag=self.country_model.assignments.get(pid); label=(self.country_model.countries[tag].name+f" [{tag}]") if tag in self.country_model.countries else (tag or "Unowned")
        self._set_status(f"Province {pid} | {label} | {area} | {region}")

    def undo(self):
        layer=self.layer_var.get(); model=self.current_model; stack=self.undo_stacks[layer]
        if not model or not stack:return
        action=stack.pop()
        for pid,value in action.before.items():model.assignments[pid]=value;self.dirty[layer].add(pid)
        self.redo_stacks[layer].append(action);self.touched.add(layer);self.schedule_render()

    def redo(self):
        layer=self.layer_var.get();model=self.current_model;stack=self.redo_stacks[layer]
        if not model or not stack:return
        action=stack.pop()
        for pid,value in action.after.items():model.assignments[pid]=value;self.dirty[layer].add(pid)
        self.undo_stacks[layer].append(action);self.touched.add(layer);self.schedule_render()

    def change_zoom(self,factor):
        if not self.map_data:return
        self.zoom=max(.08,min(4.0,self.zoom*factor));self.schedule_render(immediate=True)
    def mousewheel(self,event):self.change_zoom(1.15 if event.delta>0 else .87);return "break"
    def schedule_render(self,immediate=False):
        if self.render_after is not None:
            try:self.root.after_cancel(self.render_after)
            except Exception:pass
        self.render_after=self.root.after(1 if immediate else 45,self.render)

    def render(self):
        self.render_after=None
        if not self.map_data:return
        self._set_status("Rendering…"); max_pid=max(self.map_data.province_ids,default=0); lut=np.zeros((max_pid+1,3),dtype=np.uint8);lut[:]=UNASSIGNED_RGB
        for pid in self.map_data.province_ids:
            if pid>max_pid:continue
            if pid in self.map_data.water_provinces:lut[pid]=WATER_RGB;continue
            if self.layer_var.get()=="Cultures":
                cid=self.culture_model.assignments.get(pid)
                if cid and cid in self.culture_model.items:
                    item=self.culture_model.items[cid]; colour=self.culture_model.groups[item.group_id].colour if self.view_var.get()=="Culture Group" and item.group_id in self.culture_model.groups else item.colour;lut[pid]=hex_to_rgb(colour)
                else:lut[pid]=UNASSIGNED_RGB
            else:
                tag=self.country_model.assignments.get(pid)
                if tag and tag in self.country_model.countries:lut[pid]=hex_to_rgb(self.country_model.countries[tag].colour)
                else:lut[pid]=UNASSIGNED_RGB
        clipped=np.minimum(self.map_data.province_raster,max_pid);rgb=lut[clipped].copy();rgb[self.map_data.province_raster==0]=(20,20,20);rgb[self.map_data.boundary_mask(self.scope_var.get())]=BOUNDARY_RGB
        image=Image.fromarray(rgb,mode="RGB");size=(max(1,int(self.map_data.width*self.zoom)),max(1,int(self.map_data.height*self.zoom)))
        if size!=image.size:image=image.resize(size,Image.Resampling.NEAREST)
        self.tk_map=ImageTk.PhotoImage(image)
        if self.map_item is None:self.map_item=self.canvas.create_image(0,0,image=self.tk_map,anchor="nw")
        else:self.canvas.itemconfigure(self.map_item,image=self.tk_map)
        self.canvas.config(scrollregion=(0,0,size[0],size[1])); total=sum(len(x) for x in self.dirty.values());self._set_status(f"Ready — {total} unsaved province change(s)")

    def save(self):
        if not self.touched:
            messagebox.showinfo(APP_TITLE,"No changes to save.");return
        backups=[]; messages=[]
        try:
            self._set_status("Saving…")
            if "Cultures" in self.touched:
                b=self.culture_model.save(self.dirty["Cultures"]);backups.append(b);messages.append(f"Cultures: {len(self.dirty['Cultures'])} province change(s)")
            if "Countries" in self.touched:
                b=self.country_model.save(self.dirty["Countries"]);backups.append(b);messages.append(f"Countries: {len(self.dirty['Countries'])} province change(s)")
            for layer in list(self.touched):self.dirty[layer].clear();self.undo_stacks[layer].clear();self.redo_stacks[layer].clear()
            self.touched.clear();self._set_status("Saved")
            messagebox.showinfo(APP_TITLE,"Saved.\n\n"+"\n".join(messages)+"\n\nBackups:\n"+"\n".join(str(x) for x in backups))
        except Exception as exc:
            log=Path(__file__).resolve().parent/"eu4_setup_painter_error.log";log.write_text(traceback.format_exc(),encoding="utf-8");messagebox.showerror(APP_TITLE,f"Save failed:\n\n{exc}\n\nSee {log.name} for details.");self._set_status("Save failed")


def main():
    root=tk.Tk(); SetupPainterApp(root); root.mainloop()


if __name__=="__main__":
    main()
