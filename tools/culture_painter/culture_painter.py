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
CountryLayerModel. Countries include reusable templates, starting-force OOBs and rich flag design. Cultures can import HOI4 common/names blocks into EU4 male/female/dynasty pools. A future ReligionLayerModel can reuse the map renderer,
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
import copy
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

# Bundled vanilla client-state / custom-nation symbol atlases supplied with the
# tool. Each atlas contains the same 120 usable symbols in a padded 32 x 4 grid at a
# different resolution.
VANILLA_SYMBOL_DIRNAME = "vanilla_symbols"
VANILLA_SYMBOL_COLUMNS = 32
VANILLA_SYMBOL_ROWS = 4
# Atlas cell 0 is blank/reserved. The supplied sheets contain 120 actual
# client-state symbols in cells 1..120; cells 121..127 are padding.
VANILLA_SYMBOL_ATLAS_OFFSET = 1
VANILLA_SYMBOL_COUNT = 120
VANILLA_SYMBOL_PREFIX = "Vanilla symbol "
VANILLA_SYMBOL_SHEETS = {
    "trade_flags": "client_state_symbols_trade_flags.dds",      # 12 px cells
    "flag_smallest": "client_state_symbols_flag_smallest.dds",  # 16 px cells
    "small": "client_state_symbols_small.dds",                  # 32 px cells
    "medium": "client_state_symbols_medium.dds",                # 40 px cells
    "large": "client_state_symbols_large.dds",                  # 64 px cells
    "mini": "client_state_symbols_mini.dds",                    # 64 px cells
}

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



@dataclass
class HOI4NamelistImport:
    male_names: List[str] = field(default_factory=list)
    female_names: List[str] = field(default_factory=list)
    dynasty_names: List[str] = field(default_factory=list)
    unisex_names: List[str] = field(default_factory=list)
    ignored_callsigns: List[str] = field(default_factory=list)


def _dedupe_names(values: Iterable[str]) -> List[str]:
    """De-duplicate while preserving source order and exact spelling."""
    out: List[str] = []
    seen: Set[str] = set()
    for raw in values:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _clausewitz_list_tokens(text: str) -> List[str]:
    """
    Parse the contents of a simple Clausewitz list block.

    Unlike shlex this deliberately treats apostrophes as ordinary characters,
    because HOI4/EU4 name pools often contain names such as O'Connor. Double
    quotes group names containing whitespace. # comments are ignored.
    """
    out: List[str] = []
    token: List[str] = []
    i = 0
    in_quote = False
    escaped = False

    def flush():
        if token:
            out.append("".join(token))
            token.clear()

    while i < len(text):
        c = text[i]
        if in_quote:
            if escaped:
                token.append(c)
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_quote = False
                flush()
            else:
                token.append(c)
            i += 1
            continue

        if c == '#':
            flush()
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if c == '"':
            flush()
            in_quote = True
            i += 1
            continue
        if c.isspace() or c in "{}=":
            flush()
            i += 1
            continue
        token.append(c)
        i += 1

    flush()
    return _dedupe_names(out)


def _direct_list_values(text: str, key: str) -> List[str]:
    values: List[str] = []
    for block in top_level_blocks(text):
        if block.key != key:
            continue
        values.extend(_clausewitz_list_tokens(text[block.open_brace + 1:block.close_brace]))
    return _dedupe_names(values)


def _looks_like_hoi4_namelist_body(text: str) -> bool:
    keys = {b.key for b in top_level_blocks(text)}
    return bool(keys & {"male", "female", "names", "surnames", "callsigns"})


def parse_hoi4_namelist_block(text: str) -> HOI4NamelistImport:
    """
    Convert a HOI4 common/names-style block into EU4 culture name pools.

    Supported HOI4 forms include both:
        male = { names = { ... } }
        female = { names = { ... } }
        surnames = { ... }

    and unisex/direct forms such as:
        names = { ... }
        surnames = { ... }

    A single outer TAG/default wrapper is automatically unwrapped.  HOI4 allows
    gender-specific surnames; EU4 cultures have one dynasty_names list, so all
    global/male/female surname pools are merged there. Callsigns have no EU4
    culture-namelist counterpart and are reported/ignored.
    """
    body = text
    blocks = top_level_blocks(body)
    if not _looks_like_hoi4_namelist_body(body):
        if len(blocks) == 1:
            wrapper = blocks[0]
            candidate = body[wrapper.open_brace + 1:wrapper.close_brace]
            if _looks_like_hoi4_namelist_body(candidate):
                body = candidate
            else:
                raise ValueError("The selected block contains no HOI4 names/surnames lists.")
        elif len(blocks) > 1:
            raise ValueError(
                "This looks like a whole HOI4 names file with multiple country blocks. "
                "Copy one country's namelist block into a separate text file, or select a file containing one block."
            )
        else:
            raise ValueError("No HOI4 namelist blocks were found.")

    unisex = _direct_list_values(body, "names")
    global_surnames = _direct_list_values(body, "surnames")
    callsigns = _direct_list_values(body, "callsigns")
    male_specific: List[str] = []
    female_specific: List[str] = []
    gendered_surnames: List[str] = []

    for gender_block in top_level_blocks(body):
        if gender_block.key not in {"male", "female"}:
            continue
        gender_text = body[gender_block.open_brace + 1:gender_block.close_brace]
        names = _direct_list_values(gender_text, "names")
        surnames = _direct_list_values(gender_text, "surnames")
        callsigns.extend(_direct_list_values(gender_text, "callsigns"))
        gendered_surnames.extend(surnames)
        if gender_block.key == "male":
            male_specific.extend(names)
        else:
            female_specific.extend(names)

    # Top-level names are unisex in HOI4, so they are valid in both EU4 pools.
    male = _dedupe_names([*unisex, *male_specific])
    female = _dedupe_names([*unisex, *female_specific])
    dynasties = _dedupe_names([*global_surnames, *gendered_surnames])
    callsigns = _dedupe_names(callsigns)

    if not male and not female and not dynasties:
        raise ValueError("The HOI4 block did not contain any usable names or surnames.")

    return HOI4NamelistImport(
        male_names=male,
        female_names=female,
        dynasty_names=dynasties,
        unisex_names=_dedupe_names(unisex),
        ignored_callsigns=callsigns,
    )


def parse_eu4_culture_name_lists(culture_block_text: str) -> Tuple[List[str], List[str], List[str]]:
    """Read direct male_names/female_names/dynasty_names blocks from one culture."""
    first_open = culture_block_text.find("{")
    last_close = culture_block_text.rfind("}")
    if first_open < 0 or last_close <= first_open:
        return [], [], []
    body = culture_block_text[first_open + 1:last_close]
    return (
        _direct_list_values(body, "male_names"),
        _direct_list_values(body, "female_names"),
        _direct_list_values(body, "dynasty_names"),
    )


def eu4_name_token(value: str) -> str:
    """Quote a culture name token only when Clausewitz syntax requires it."""
    value = str(value)
    if re.fullmatch(r'[^\s#{}="]+', value):
        return value
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def format_eu4_name_list_block(key: str, names: Sequence[str], indent: str) -> str:
    if not names:
        return ""
    tokens = [eu4_name_token(name) for name in names]
    lines = [f"{indent}{key} = {{"]
    current = indent + "\t"
    for token in tokens:
        proposed = current + ((" " if current.strip() else "") + token)
        if len(proposed) > 115 and current.strip():
            lines.append(current.rstrip())
            current = indent + "\t" + token
        else:
            current = proposed
    if current.strip():
        lines.append(current.rstrip())
    lines.append(f"{indent}}}")
    return "\n".join(lines)


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
    sea_provinces: Set[int]
    lake_provinces: Set[int]
    coastal_provinces: Set[int]
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
        sea = extract_integer_set_from_named_block(default_text, "sea_starts")
        lakes = extract_integer_set_from_named_block(default_text, "lakes")
        water = sea | lakes

        # Determine land provinces that touch a true sea province. This is used by
        # the starting-navy editor to reject inland/lake locations. Orthogonal
        # pixel adjacency is enough for EU4 port eligibility in this editor.
        say("Detecting coastal provinces…")
        sea_mask = np.isin(province_raster, np.array(sorted(sea), dtype=np.int32)) if sea else np.zeros(province_raster.shape, dtype=bool)
        coast_ids: Set[int] = set()
        if sea_mask.any():
            pairs = [
                (province_raster[1:, :], sea_mask[:-1, :]),
                (province_raster[:-1, :], sea_mask[1:, :]),
                (province_raster[:, 1:], sea_mask[:, :-1]),
                (province_raster[:, :-1], sea_mask[:, 1:]),
            ]
            for land_side, adjacent_sea in pairs:
                if adjacent_sea.any():
                    vals_here = np.unique(land_side[adjacent_sea])
                    coast_ids.update(int(v) for v in vals_here if int(v) > 0 and int(v) not in water)

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
        return cls(mod_root, w, h, province_raster, province_ids, water, sea, lakes, coast_ids,
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
    # Culture-specific EU4 character name pools.  These are only rewritten when
    # namelist_managed is true, so existing hand-written culture files are left
    # untouched until the user explicitly imports a HOI4 namelist for the culture.
    male_names: List[str] = field(default_factory=list)
    female_names: List[str] = field(default_factory=list)
    dynasty_names: List[str] = field(default_factory=list)
    namelist_managed: bool = False


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
                    male_names, female_names, dynasty_names = parse_eu4_culture_name_lists(
                        text[child.start:child.end]
                    )
                    self.items[cid].male_names = male_names
                    self.items[cid].female_names = female_names
                    self.items[cid].dynasty_names = dynasty_names

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
            if cid not in self.items:
                continue
            if record.get("group_id"):
                self.items[cid].group_id = str(record["group_id"])
            namelist = record.get("namelist")
            if isinstance(namelist, dict) and bool(namelist.get("managed", True)):
                item = self.items[cid]
                item.male_names = _dedupe_names(namelist.get("male_names", []))
                item.female_names = _dedupe_names(namelist.get("female_names", []))
                item.dynasty_names = _dedupe_names(namelist.get("dynasty_names", []))
                item.namelist_managed = True

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

    @staticmethod
    def culture_has_own_namelist(item: ItemInfo) -> bool:
        """Return True when this culture already has a direct culture-specific name pool."""
        return bool(item.male_names or item.female_names or item.dynasty_names or item.namelist_managed)

    def import_hoi4_namelist(self, cid: str, imported: HOI4NamelistImport) -> None:
        if cid not in self.items:
            raise ValueError(f"Unknown culture '{cid}'.")
        item = self.items[cid]
        item.male_names = _dedupe_names(imported.male_names)
        item.female_names = _dedupe_names(imported.female_names)
        item.dynasty_names = _dedupe_names(imported.dynasty_names)
        item.namelist_managed = True

    def import_hoi4_namelist_to_group(
        self, gid: str, imported: HOI4NamelistImport, override_existing: bool = False
    ) -> Tuple[List[str], List[str]]:
        """
        Mass-apply one imported HOI4 namelist to the cultures in a culture group.

        When override_existing is False, cultures that already have any direct
        male/female/dynasty pool are left untouched.  When True, every member
        culture receives the imported pools and existing direct pools are replaced
        on Save.  Returning applied/skipped IDs makes the GUI summary explicit.
        """
        if gid not in self.groups:
            raise ValueError(f"Unknown culture group '{gid}'.")
        members = [item for item in self.items.values() if item.group_id == gid]
        applied: List[str] = []
        skipped: List[str] = []
        for item in members:
            if not override_existing and self.culture_has_own_namelist(item):
                skipped.append(item.id)
                continue
            item.male_names = _dedupe_names(imported.male_names)
            item.female_names = _dedupe_names(imported.female_names)
            item.dynasty_names = _dedupe_names(imported.dynasty_names)
            item.namelist_managed = True
            applied.append(item.id)
        return applied, skipped

    def _metadata_dict(self) -> dict:
        return {
            "version": 6,
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
                cid: {
                    "loc_name": c.loc_name,
                    "colour": c.colour,
                    "group_id": c.group_id,
                    "namelist": ({
                        "managed": True,
                        "male_names": c.male_names,
                        "female_names": c.female_names,
                        "dynasty_names": c.dynasty_names,
                    } if c.namelist_managed else None),
                }
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

    def _write_culture_namelist(self, cid: str, item: ItemInfo) -> None:
        """Replace only the direct EU4 name-pool blocks inside one culture."""
        found = self._find_culture_block(cid)
        if found is None:
            raise RuntimeError(f"Culture '{cid}' has no definition block after synchronization.")
        path, text, enc, culture_block, _gid = found
        inner_start = culture_block.open_brace + 1
        inner_text = text[inner_start:culture_block.close_brace]
        removable = [
            block for block in top_level_blocks(inner_text, offset=inner_start)
            if block.key in {"male_names", "female_names", "dynasty_names"}
        ]

        # Remove complete lines from bottom to top so offsets remain valid.
        for block in sorted(removable, key=lambda b: b.start, reverse=True):
            start = text.rfind("\n", 0, block.start) + 1
            next_nl = text.find("\n", block.end)
            end = len(text) if next_nl == -1 else next_nl + 1
            text = text[:start] + text[end:]

        write_text(path, text, enc)
        # Re-find because deleting old name blocks shifted the culture's closing brace.
        found = self._find_culture_block(cid)
        assert found is not None
        path, text, enc, culture_block, _gid = found
        close_line_start = text.rfind("\n", 0, culture_block.close_brace) + 1
        close_indent = text[close_line_start:culture_block.close_brace]
        child_indent = close_indent + "\t"
        blocks = [
            format_eu4_name_list_block("male_names", item.male_names, child_indent),
            format_eu4_name_list_block("female_names", item.female_names, child_indent),
            format_eu4_name_list_block("dynasty_names", item.dynasty_names, child_indent),
        ]
        generated = "\n\n".join(block for block in blocks if block)
        if generated:
            if close_line_start > 0 and not text[:close_line_start].endswith("\n"):
                generated = "\n" + generated
            generated += "\n"
            text = text[:close_line_start] + generated + text[close_line_start:]
            write_text(path, text, enc)

    def _sync_namelists(self) -> None:
        for cid, item in self.items.items():
            if item.namelist_managed:
                self._write_culture_namelist(cid, item)

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
        self._sync_namelists()
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
class ArmyStackInfo:
    name: str = "1st Army"
    location: int = 0
    infantry: int = 0
    cavalry: int = 0
    artillery: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "ArmyStackInfo":
        if not isinstance(data, dict):
            return cls()
        def n(k):
            try: return max(0, int(data.get(k, 0)))
            except Exception: return 0
        return cls(
            name=str(data.get("name", "1st Army")),
            location=n("location"), infantry=n("infantry"), cavalry=n("cavalry"), artillery=n("artillery"),
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "location": self.location, "infantry": self.infantry,
                "cavalry": self.cavalry, "artillery": self.artillery}


@dataclass
class NavyStackInfo:
    name: str = "1st Fleet"
    location: int = 0
    heavy_ship: int = 0
    light_ship: int = 0
    galley: int = 0
    transport: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "NavyStackInfo":
        if not isinstance(data, dict):
            return cls()
        def n(k):
            try: return max(0, int(data.get(k, 0)))
            except Exception: return 0
        return cls(
            name=str(data.get("name", "1st Fleet")), location=n("location"),
            heavy_ship=n("heavy_ship"), light_ship=n("light_ship"), galley=n("galley"), transport=n("transport"),
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "location": self.location, "heavy_ship": self.heavy_ship,
                "light_ship": self.light_ship, "galley": self.galley, "transport": self.transport}


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
    flag_emblem_source: str = ""
    armies: List[ArmyStackInfo] = field(default_factory=list)
    navies: List[NavyStackInfo] = field(default_factory=list)

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
        base.flag_emblem_source = str(get("flag_emblem_source", base.flag_emblem_source))
        armies = get("armies", [])
        navies = get("navies", [])
        base.armies = [ArmyStackInfo.from_dict(x) for x in armies if isinstance(x, dict)] if isinstance(armies, list) else []
        base.navies = [NavyStackInfo.from_dict(x) for x in navies if isinstance(x, dict)] if isinstance(navies, list) else []
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
            "flag_emblem": self.flag_emblem, "flag_emblem_source": self.flag_emblem_source,
            "armies": [x.to_dict() for x in self.armies],
            "navies": [x.to_dict() for x in self.navies],
        }


def _star_points(cx: float, cy: float, r_outer: float, r_inner: float, points: int = 5) -> List[Tuple[float, float]]:
    import math
    out = []
    for i in range(points * 2):
        angle = -math.pi / 2 + i * math.pi / points
        radius = r_outer if i % 2 == 0 else r_inner
        out.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    return out


def _regular_polygon(cx: float, cy: float, radius: float, points: int, rotation: float = -1.57079632679):
    import math
    return [(cx + math.cos(rotation + i * 2 * math.pi / points) * radius,
             cy + math.sin(rotation + i * 2 * math.pi / points) * radius) for i in range(points)]



def vanilla_symbol_name(index: int) -> str:
    """Return the persistent designer name for a zero-based vanilla symbol."""
    return f"{VANILLA_SYMBOL_PREFIX}{index + 1:03d}"


def vanilla_symbol_index(emblem: str) -> Optional[int]:
    """Decode 'Vanilla symbol 001' .. 'Vanilla symbol 128'."""
    m = re.fullmatch(r"Vanilla symbol\s+(\d{1,3})", str(emblem).strip(), flags=re.IGNORECASE)
    if not m:
        return None
    index = int(m.group(1)) - 1
    return index if 0 <= index < VANILLA_SYMBOL_COUNT else None


def _vanilla_symbol_dir() -> Path:
    return Path(__file__).resolve().parent / VANILLA_SYMBOL_DIRNAME


def _vanilla_symbol_sheet_for_target(target_px: int) -> Optional[Path]:
    """Choose the closest bundled vanilla atlas for the requested emblem size."""
    target_px = max(1, int(target_px))
    if target_px <= 12:
        order = ("trade_flags", "flag_smallest", "small", "medium", "large", "mini")
    elif target_px <= 16:
        order = ("flag_smallest", "small", "medium", "large", "mini", "trade_flags")
    elif target_px <= 32:
        order = ("small", "medium", "large", "mini", "flag_smallest", "trade_flags")
    elif target_px <= 40:
        order = ("medium", "large", "mini", "small", "flag_smallest", "trade_flags")
    else:
        order = ("large", "mini", "medium", "small", "flag_smallest", "trade_flags")

    root = _vanilla_symbol_dir()
    for key in order:
        path = root / VANILLA_SYMBOL_SHEETS[key]
        if path.exists():
            return path
    return None


_VANILLA_SHEET_CACHE: Dict[str, Image.Image] = {}
_VANILLA_SYMBOL_CACHE: Dict[Tuple[str, int], Image.Image] = {}


def _load_cached_vanilla_sheet(sheet_path: Path) -> Optional[Image.Image]:
    """DDS decoding is expensive in Pillow; decode each atlas only once per run."""
    key = str(sheet_path.resolve())
    cached = _VANILLA_SHEET_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with Image.open(sheet_path) as sheet:
            cached = sheet.convert("RGBA")
    except Exception:
        return None
    _VANILLA_SHEET_CACHE[key] = cached
    return cached


def load_vanilla_symbol(index: int, target_px: int = 64) -> Optional[Image.Image]:
    """Crop one symbol from the bundled 32x4 DDS atlas and return RGBA artwork."""
    if not (0 <= int(index) < VANILLA_SYMBOL_COUNT):
        return None
    sheet_path = _vanilla_symbol_sheet_for_target(target_px)
    if sheet_path is None:
        return None

    sheet = _load_cached_vanilla_sheet(sheet_path)
    if sheet is None:
        return None

    atlas_index = int(index) + VANILLA_SYMBOL_ATLAS_OFFSET
    cache_key = (str(sheet_path.resolve()), atlas_index)
    native = _VANILLA_SYMBOL_CACHE.get(cache_key)
    if native is None:
        cell_w = sheet.width // VANILLA_SYMBOL_COLUMNS
        cell_h = sheet.height // VANILLA_SYMBOL_ROWS
        col = atlas_index % VANILLA_SYMBOL_COLUMNS
        row = atlas_index // VANILLA_SYMBOL_COLUMNS
        native = sheet.crop((
            col * cell_w,
            row * cell_h,
            (col + 1) * cell_w,
            (row + 1) * cell_h,
        ))

        # Remove transparent padding but keep the symbol's own antialiasing.
        alpha = native.getchannel("A")
        bbox = alpha.getbbox()
        if bbox:
            native = native.crop(bbox)
        _VANILLA_SYMBOL_CACHE[cache_key] = native

    symbol = native.copy()
    target_px = max(1, int(target_px))
    if symbol.width > target_px or symbol.height > target_px:
        symbol.thumbnail((target_px, target_px), Image.Resampling.LANCZOS)
    elif max(symbol.size) < target_px:
        scale = target_px / max(1, max(symbol.size))
        new_size = (
            max(1, int(round(symbol.width * scale))),
            max(1, int(round(symbol.height * scale))),
        )
        symbol = symbol.resize(new_size, Image.Resampling.LANCZOS)
    return symbol


def generate_designer_flag(pattern: str, colours: Sequence[str], emblem: str, size: int = 128,
                           emblem_source: Optional[Path] = None) -> Image.Image:
    """Bake a rich Nation-Designer-style flag into a normal EU4 128×128 TGA.

    Vanilla Nation Designer patterns are RGB masks. The designer supports the
    tool's vector emblems, custom uploaded images, and the bundled 120-symbol
    client_state_symbols* atlases supplied by the user.
    """
    import math
    cols = [hex_to_rgb(c) for c in list(colours)[:3]]
    while len(cols) < 3:
        cols.append((255, 255, 255))
    c1, c2, c3 = cols
    im = Image.new("RGB", (size, size), c1)
    d = ImageDraw.Draw(im)
    s = size
    p = pattern

    # --- Background patterns -------------------------------------------------
    if p == "Horizontal bicolor":
        d.rectangle((0, s//2, s, s), fill=c2)
    elif p == "Horizontal tricolor":
        d.rectangle((0, s//3, s, 2*s//3), fill=c2); d.rectangle((0, 2*s//3, s, s), fill=c3)
    elif p == "Horizontal 1:2:1":
        d.rectangle((0, s//4, s, 3*s//4), fill=c2); d.rectangle((0, 3*s//4, s, s), fill=c3)
    elif p == "Vertical bicolor":
        d.rectangle((s//2, 0, s, s), fill=c2)
    elif p == "Vertical tricolor":
        d.rectangle((s//3, 0, 2*s//3, s), fill=c2); d.rectangle((2*s//3, 0, s, s), fill=c3)
    elif p == "Vertical 1:2:1":
        d.rectangle((s//4, 0, 3*s//4, s), fill=c2); d.rectangle((3*s//4, 0, s, s), fill=c3)
    elif p == "Diagonal down":
        d.polygon([(0, 0), (s, s), (0, s)], fill=c2)
    elif p in ("Diagonal", "Diagonal up"):
        d.polygon([(0, s), (s, 0), (s, s)], fill=c2)
    elif p == "Diagonal tricolor down":
        w=s*.22; d.polygon([(0,0),(w,0),(s,s-w),(s,s),(s-w,s),(0,w)],fill=c2); d.polygon([(0,0),(w*.45,0),(s,s-w*.45),(s,s),(s-w*.45,s),(0,w*.45)],fill=c3)
    elif p == "Diagonal tricolor up":
        w=s*.22; d.polygon([(s,0),(s-w,0),(0,s-w),(0,s),(w,s),(s,w)],fill=c2); d.polygon([(s,0),(s-w*.45,0),(0,s-w*.45),(0,s),(w*.45,s),(s,w*.45)],fill=c3)
    elif p == "Quartered":
        d.rectangle((s//2,0,s,s//2),fill=c2); d.rectangle((0,s//2,s//2,s),fill=c2); d.rectangle((s//2,s//2,s,s),fill=c3)
    elif p == "Quartered alternating":
        d.rectangle((s//2,0,s,s//2),fill=c2); d.rectangle((0,s//2,s//2,s),fill=c2)
    elif p == "Three quarters":
        d.rectangle((s//2,0,s,s//2),fill=c2); d.rectangle((0,s//2,s,s),fill=c3)
    elif p == "Center cross":
        w=max(10,s//5); d.rectangle((s//2-w//2,0,s//2+w//2,s),fill=c2); d.rectangle((0,s//2-w//2,s,s//2+w//2),fill=c2)
        iw=max(4,w//3); d.rectangle((s//2-iw//2,0,s//2+iw//2,s),fill=c3); d.rectangle((0,s//2-iw//2,s,s//2+iw//2),fill=c3)
    elif p == "Simple cross":
        w=max(10,s//5); d.rectangle((s//2-w//2,0,s//2+w//2,s),fill=c2); d.rectangle((0,s//2-w//2,s,s//2+w//2),fill=c2)
    elif p == "Nordic cross":
        x=int(s*.38); y=s//2; w=max(12,s//5); d.rectangle((x-w//2,0,x+w//2,s),fill=c2); d.rectangle((0,y-w//2,s,y+w//2),fill=c2)
        iw=max(4,w//3); d.rectangle((x-iw//2,0,x+iw//2,s),fill=c3); d.rectangle((0,y-iw//2,s,y+iw//2),fill=c3)
    elif p == "Nordic cross simple":
        x=int(s*.38); y=s//2; w=max(12,s//5); d.rectangle((x-w//2,0,x+w//2,s),fill=c2); d.rectangle((0,y-w//2,s,y+w//2),fill=c2)
    elif p == "Saltire":
        w=max(8,s//10); d.polygon([(0,0),(w,0),(s,s-w),(s,s),(s-w,s),(0,w)],fill=c2); d.polygon([(s,0),(s-w,0),(0,s-w),(0,s),(w,s),(s,w)],fill=c2)
    elif p == "Saltire bordered":
        w=max(14,s//7); wi=max(5,w//3)
        for ww,col in ((w,c2),(wi,c3)):
            d.polygon([(0,0),(ww,0),(s,s-ww),(s,s),(s-ww,s),(0,ww)],fill=col); d.polygon([(s,0),(s-ww,0),(0,s-ww),(0,s),(ww,s),(s,ww)],fill=col)
    elif p == "Cross and saltire":
        w=max(8,s//11); d.rectangle((s//2-w//2,0,s//2+w//2,s),fill=c2); d.rectangle((0,s//2-w//2,s,s//2+w//2),fill=c2)
        ws=max(5,s//18); d.polygon([(0,0),(ws,0),(s,s-ws),(s,s),(s-ws,s),(0,ws)],fill=c3); d.polygon([(s,0),(s-ws,0),(0,s-ws),(0,s),(ws,s),(s,ws)],fill=c3)
    elif p == "Hoist triangle":
        d.polygon([(0,0),(s*.58,s/2),(0,s)],fill=c2)
    elif p == "Fly triangle":
        d.polygon([(s,0),(s*.42,s/2),(s,s)],fill=c2)
    elif p == "Double triangle":
        d.polygon([(0,0),(s*.5,s/2),(0,s)],fill=c2); d.polygon([(s,0),(s*.5,s/2),(s,s)],fill=c3)
    elif p == "Chevron":
        d.polygon([(0,0),(s*.48,s/2),(0,s),(0,s*.78),(s*.27,s/2),(0,s*.22)],fill=c2)
    elif p == "Double chevron":
        d.polygon([(0,0),(s*.46,s/2),(0,s)],fill=c2); d.polygon([(0,s*.17),(s*.28,s/2),(0,s*.83)],fill=c3)
    elif p == "Center diamond":
        d.polygon([(s/2,0),(s,s/2),(s/2,s),(0,s/2)],fill=c2); d.polygon([(s/2,s*.22),(s*.78,s/2),(s/2,s*.78),(s*.22,s/2)],fill=c3)
    elif p == "Center lozenge":
        d.polygon([(s/2,s*.16),(s*.84,s/2),(s/2,s*.84),(s*.16,s/2)],fill=c2)
    elif p == "Upper canton":
        d.rectangle((0,0,s*.48,s*.48),fill=c2)
    elif p == "Lower canton":
        d.rectangle((0,s*.52,s*.48,s),fill=c2)
    elif p == "Canton and stripe":
        d.rectangle((0,s*.5,s,s),fill=c2); d.rectangle((0,0,s*.48,s*.5),fill=c3)
    elif p == "Border":
        w=max(8,s//10); d.rectangle((0,0,s,s),fill=c2); d.rectangle((w,w,s-w,s-w),fill=c1)
    elif p == "Double border":
        w=max(8,s//10); d.rectangle((0,0,s,s),fill=c2); d.rectangle((w,w,s-w,s-w),fill=c3); d.rectangle((2*w,2*w,s-2*w,s-2*w),fill=c1)
    elif p == "Center band horizontal":
        d.rectangle((0,s*.34,s,s*.66),fill=c2); d.rectangle((0,s*.44,s,s*.56),fill=c3)
    elif p == "Center band vertical":
        d.rectangle((s*.34,0,s*.66,s),fill=c2); d.rectangle((s*.44,0,s*.56,s),fill=c3)
    elif p == "Greek cross field":
        w=s*.16; arm=s*.34; d.rectangle((s/2-w/2,s/2-arm,s/2+w/2,s/2+arm),fill=c2); d.rectangle((s/2-arm,s/2-w/2,s/2+arm,s/2+w/2),fill=c2)
    elif p == "Central disk":
        r=s*.32; d.ellipse((s/2-r,s/2-r,s/2+r,s/2+r),fill=c2); r2=s*.18; d.ellipse((s/2-r2,s/2-r2,s/2+r2,s/2+r2),fill=c3)
    elif p == "Sunburst 8":
        center=(s/2,s/2)
        for i in range(8):
            a1=(i-.5)*math.pi/4; a2=(i+.5)*math.pi/4
            col=c2 if i%2==0 else c3
            d.polygon([center,(s/2+math.cos(a1)*s,s/2+math.sin(a1)*s),(s/2+math.cos(a2)*s,s/2+math.sin(a2)*s)],fill=col)
    elif p == "Gyronny 8":
        center=(s/2,s/2); corners=[(0,0),(s/2,0),(s,0),(s,s/2),(s,s),(s/2,s),(0,s),(0,s/2),(0,0)]
        for i in range(8): d.polygon([center,corners[i],corners[i+1]],fill=(c2 if i%2==0 else c3))
    elif p == "Horizontal five bands":
        band=s/5
        for i,col in enumerate((c1,c2,c3,c2,c1)): d.rectangle((0,int(i*band),s,int((i+1)*band)),fill=col)
    elif p == "Vertical five bands":
        band=s/5
        for i,col in enumerate((c1,c2,c3,c2,c1)): d.rectangle((int(i*band),0,int((i+1)*band),s),fill=col)
    # Solid deliberately leaves the first colour untouched.

    # --- Emblems -------------------------------------------------------------
    ec=c3; cx=cy=s/2; r=s*.20; lw=max(3,int(s*.035))
    def thick_line(points, fill=ec, width=lw): d.line(points, fill=fill, width=width, joint="curve")
    if emblem == "Circle":
        d.ellipse((cx-r,cy-r,cx+r,cy+r),fill=ec)
    elif emblem == "Ring":
        d.ellipse((cx-r,cy-r,cx+r,cy+r),fill=ec); rr=r*.60; d.ellipse((cx-rr,cy-rr,cx+rr,cy+rr),fill=c1)
    elif emblem == "Diamond":
        d.polygon([(cx,cy-r),(cx+r,cy),(cx,cy+r),(cx-r,cy)],fill=ec)
    elif emblem in ("Star", "Star 5"):
        d.polygon(_star_points(cx,cy,s*.23,s*.095,5),fill=ec)
    elif emblem == "Star 6":
        d.polygon(_regular_polygon(cx,cy,s*.22,3),fill=ec); d.polygon(_regular_polygon(cx,cy,s*.22,3,rotation=math.pi/2),fill=ec)
    elif emblem == "Star 8":
        d.polygon(_star_points(cx,cy,s*.24,s*.105,8),fill=ec)
    elif emblem == "Crescent":
        rr=s*.22; d.ellipse((cx-rr,cy-rr,cx+rr,cy+rr),fill=ec); d.ellipse((cx-rr*.30,cy-rr,cx+rr*1.25,cy+rr),fill=c1)
    elif emblem == "Crescent & star":
        rr=s*.21; d.ellipse((cx-rr,cy-rr,cx+rr,cy+rr),fill=ec); d.ellipse((cx-rr*.30,cy-rr,cx+rr*1.25,cy+rr),fill=c1); d.polygon(_star_points(cx+rr*.72,cy,s*.075,s*.03,5),fill=ec)
    elif emblem == "Cross":
        w=s*.08; a=s*.24; d.rectangle((cx-w,cy-a,cx+w,cy+a),fill=ec); d.rectangle((cx-a,cy-w,cx+a,cy+w),fill=ec)
    elif emblem == "Latin cross":
        w=s*.065; d.rectangle((cx-w,cy-s*.27,cx+w,cy+s*.28),fill=ec); d.rectangle((cx-s*.22,cy-s*.10,cx+s*.22,cy+s*.03),fill=ec)
    elif emblem == "Maltese cross":
        pts=[]
        for q in range(4):
            ang=q*math.pi/2
            base=[(-.05,-.06),(.05,-.06),(.20,-.20),(.12,0),(.20,.20),(.05,.06),(-.05,.06),(-.20,.20),(-.12,0),(-.20,-.20)]
        d.polygon([(cx,cy-s*.05),(cx+s*.20,cy-s*.22),(cx+s*.10,cy),(cx+s*.22,cy+s*.20),(cx,cy+s*.10),(cx-s*.22,cy+s*.20),(cx-s*.10,cy),(cx-s*.20,cy-s*.22)],fill=ec)
    elif emblem == "Fleur-de-lis":
        d.polygon([(cx,cy-s*.27),(cx+s*.09,cy-s*.08),(cx+s*.20,cy-s*.17),(cx+s*.17,cy+s*.02),(cx+s*.07,cy+s*.08),(cx+s*.05,cy+s*.22),(cx+s*.14,cy+s*.22),(cx+s*.14,cy+s*.29),(cx-s*.14,cy+s*.29),(cx-s*.14,cy+s*.22),(cx-s*.05,cy+s*.22),(cx-s*.07,cy+s*.08),(cx-s*.17,cy+s*.02),(cx-s*.20,cy-s*.17),(cx-s*.09,cy-s*.08)],fill=ec)
    elif emblem == "Crown":
        d.polygon([(cx-s*.25,cy+s*.14),(cx-s*.22,cy-s*.10),(cx-s*.10,cy),(cx,cy-s*.22),(cx+s*.10,cy),(cx+s*.22,cy-s*.10),(cx+s*.25,cy+s*.14)],fill=ec); d.rectangle((cx-s*.25,cy+s*.13,cx+s*.25,cy+s*.22),fill=ec)
    elif emblem == "Shield":
        d.polygon([(cx-s*.23,cy-s*.24),(cx+s*.23,cy-s*.24),(cx+s*.20,cy+s*.08),(cx,cy+s*.30),(cx-s*.20,cy+s*.08)],fill=ec)
    elif emblem == "Castle":
        d.rectangle((cx-s*.22,cy-s*.05,cx+s*.22,cy+s*.24),fill=ec); d.rectangle((cx-s*.27,cy-s*.20,cx-s*.10,cy+s*.24),fill=ec); d.rectangle((cx+s*.10,cy-s*.20,cx+s*.27,cy+s*.24),fill=ec)
        for xx in (cx-s*.25,cx-s*.16,cx+s*.12,cx+s*.21): d.rectangle((xx,cy-s*.27,xx+s*.06,cy-s*.18),fill=ec)
        d.rectangle((cx-s*.05,cy+s*.08,cx+s*.05,cy+s*.24),fill=c1)
    elif emblem == "Tower":
        d.rectangle((cx-s*.14,cy-s*.18,cx+s*.14,cy+s*.26),fill=ec); d.rectangle((cx-s*.19,cy-s*.26,cx-s*.08,cy-s*.14),fill=ec); d.rectangle((cx-s*.04,cy-s*.26,cx+s*.04,cy-s*.14),fill=ec); d.rectangle((cx+s*.08,cy-s*.26,cx+s*.19,cy-s*.14),fill=ec)
    elif emblem == "Anchor":
        thick_line([(cx,cy-s*.24),(cx,cy+s*.20)],width=max(4,int(s*.045))); d.ellipse((cx-s*.07,cy-s*.28,cx+s*.07,cy-s*.14),outline=ec,width=lw); thick_line([(cx-s*.23,cy+s*.08),(cx-s*.12,cy+s*.22),(cx,cy+s*.27),(cx+s*.12,cy+s*.22),(cx+s*.23,cy+s*.08)],width=max(4,int(s*.045))); thick_line([(cx-s*.10,cy-s*.05),(cx+s*.10,cy-s*.05)])
    elif emblem == "Sword":
        thick_line([(cx-s*.15,cy+s*.23),(cx+s*.14,cy-s*.18)],width=max(5,int(s*.055))); d.polygon([(cx+s*.14,cy-s*.18),(cx+s*.22,cy-s*.27),(cx+s*.18,cy-s*.13)],fill=ec); thick_line([(cx-s*.21,cy+s*.10),(cx-s*.05,cy+s*.22)],width=lw)
    elif emblem == "Crossed swords":
        thick_line([(cx-s*.22,cy+s*.23),(cx+s*.18,cy-s*.20)],width=max(4,int(s*.045))); thick_line([(cx+s*.22,cy+s*.23),(cx-s*.18,cy-s*.20)],width=max(4,int(s*.045)))
        d.polygon([(cx+s*.18,cy-s*.20),(cx+s*.25,cy-s*.29),(cx+s*.22,cy-s*.14)],fill=ec); d.polygon([(cx-s*.18,cy-s*.20),(cx-s*.25,cy-s*.29),(cx-s*.22,cy-s*.14)],fill=ec)
    elif emblem == "Axe":
        thick_line([(cx-s*.10,cy+s*.27),(cx+s*.05,cy-s*.22)],width=max(5,int(s*.05))); d.polygon([(cx,cy-s*.18),(cx+s*.24,cy-s*.25),(cx+s*.18,cy-s*.02),(cx+s*.04,cy-s*.06)],fill=ec)
    elif emblem == "Spear":
        thick_line([(cx-s*.12,cy+s*.28),(cx+s*.10,cy-s*.20)],width=max(4,int(s*.035))); d.polygon([(cx+s*.10,cy-s*.20),(cx+s*.18,cy-s*.30),(cx+s*.17,cy-s*.15)],fill=ec)
    elif emblem == "Tree":
        d.rectangle((cx-s*.045,cy, cx+s*.045,cy+s*.27),fill=ec); d.ellipse((cx-s*.22,cy-s*.25,cx+s*.05,cy+s*.06),fill=ec); d.ellipse((cx-s*.04,cy-s*.28,cx+s*.22,cy+s*.04),fill=ec); d.ellipse((cx-s*.14,cy-s*.36,cx+s*.14,cy-s*.03),fill=ec)
    elif emblem == "Sun":
        rr=s*.12; d.ellipse((cx-rr,cy-rr,cx+rr,cy+rr),fill=ec)
        for i in range(12):
            a=i*math.pi/6; thick_line([(cx+math.cos(a)*s*.16,cy+math.sin(a)*s*.16),(cx+math.cos(a)*s*.28,cy+math.sin(a)*s*.28)],width=max(2,int(s*.025)))
    elif emblem == "Moon":
        rr=s*.22; d.ellipse((cx-rr,cy-rr,cx+rr,cy+rr),fill=ec); d.ellipse((cx-rr*.18,cy-rr,cx+rr*1.35,cy+rr),fill=c1)
    elif emblem == "Flame":
        d.polygon([(cx,cy-s*.30),(cx+s*.08,cy-s*.08),(cx+s*.18,cy+s*.05),(cx+s*.10,cy+s*.28),(cx,cy+s*.18),(cx-s*.10,cy+s*.28),(cx-s*.18,cy+s*.05),(cx-s*.05,cy-s*.05)],fill=ec)
    elif emblem == "Gear":
        d.polygon(_regular_polygon(cx,cy,s*.24,12),fill=ec); rr=s*.10; d.ellipse((cx-rr,cy-rr,cx+rr,cy+rr),fill=c1)
    elif emblem == "Wheel":
        d.ellipse((cx-r,cy-r,cx+r,cy+r),outline=ec,width=max(4,int(s*.04)))
        for i in range(8):
            a=i*math.pi/4; thick_line([(cx,cy),(cx+math.cos(a)*r,cy+math.sin(a)*r)],width=max(2,int(s*.025)))
    elif emblem == "Book":
        d.polygon([(cx-s*.25,cy-s*.18),(cx-s*.02,cy-s*.12),(cx-s*.02,cy+s*.24),(cx-s*.25,cy+s*.16)],fill=ec); d.polygon([(cx+s*.25,cy-s*.18),(cx+s*.02,cy-s*.12),(cx+s*.02,cy+s*.24),(cx+s*.25,cy+s*.16)],fill=ec)
    elif emblem == "Heart":
        pts=[(cx,cy+s*.27),(cx-s*.25,cy),(cx-s*.20,cy-s*.17),(cx-s*.08,cy-s*.23),(cx,cy-s*.12),(cx+s*.08,cy-s*.23),(cx+s*.20,cy-s*.17),(cx+s*.25,cy)]
        d.polygon(pts,fill=ec)
    elif emblem == "Skull":
        rr=s*.20; d.ellipse((cx-rr,cy-rr,cx+rr,cy+rr*.8),fill=ec); d.rectangle((cx-s*.12,cy+s*.10,cx+s*.12,cy+s*.25),fill=ec); er=s*.045; d.ellipse((cx-s*.09-er,cy-s*.04-er,cx-s*.09+er,cy-s*.04+er),fill=c1); d.ellipse((cx+s*.09-er,cy-s*.04-er,cx+s*.09+er,cy-s*.04+er),fill=c1)
    elif emblem == "Paw":
        d.ellipse((cx-s*.15,cy-s*.02,cx+s*.15,cy+s*.25),fill=ec)
        for dx,dy in ((-.18,-.12),(-.06,-.22),(.07,-.22),(.19,-.11)):
            rr=s*.065; d.ellipse((cx+dx*s-rr,cy+dy*s-rr,cx+dx*s+rr,cy+dy*s+rr),fill=ec)
    elif emblem == "Cat head":
        d.polygon([(cx-s*.22,cy-s*.11),(cx-s*.24,cy-s*.30),(cx-s*.08,cy-s*.20),(cx+s*.08,cy-s*.20),(cx+s*.24,cy-s*.30),(cx+s*.22,cy-s*.11),(cx+s*.18,cy+s*.22),(cx,cy+s*.30),(cx-s*.18,cy+s*.22)],fill=ec)
    elif emblem == "Mountain":
        d.polygon([(cx-s*.30,cy+s*.23),(cx-s*.04,cy-s*.27),(cx+s*.08,cy-s*.05),(cx+s*.18,cy-s*.18),(cx+s*.30,cy+s*.23)],fill=ec)
    elif emblem == "Wave":
        for off in (-.10,.05,.20): thick_line([(cx-s*.28,cy+s*off*s),(cx-s*.15,cy+(off-.07)*s),(cx,cy+s*off*s),(cx+s*.15,cy+(off-.07)*s),(cx+s*.28,cy+s*off*s)],width=max(3,int(s*.035)))
    elif vanilla_symbol_index(emblem) is not None:
        symbol_index = vanilla_symbol_index(emblem)
        src = load_vanilla_symbol(symbol_index, max(1, int(s * .60)))
        if src is not None:
            overlay = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            overlay.alpha_composite(src, ((s - src.width) // 2, (s - src.height) // 2))
            im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
    elif emblem == "Custom image" and emblem_source and Path(emblem_source).exists():
        with Image.open(emblem_source) as src:
            src=src.convert("RGBA")
            bbox=src.getbbox()
            if bbox: src=src.crop(bbox)
            target=max(1,int(s*.56)); src.thumbnail((target,target),Image.Resampling.LANCZOS)
            overlay=Image.new("RGBA",(s,s),(0,0,0,0)); overlay.alpha_composite(src,((s-src.width)//2,(s-src.height)//2))
            im=Image.alpha_composite(im.convert("RGBA"),overlay).convert("RGB")
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

    def store_emblem_source(self, tag: str, source: Path) -> str:
        folder = self.tool_dir / "emblem_sources"
        folder.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower() if source.suffix else ".png"
        dest = folder / f"{tag}_emblem{suffix}"
        shutil.copy2(source, dest)
        return str(dest.relative_to(self.tool_dir)).replace("\\", "/")

    def _resolve_emblem_source(self, source: str) -> Optional[Path]:
        return self._resolve_flag_source(source)

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
            emblem_source = self._resolve_emblem_source(country.flag_emblem_source)
            im = generate_designer_flag(country.flag_pattern, country.flag_colours, country.flag_emblem, 128, emblem_source)
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

        # Starting forces. EU4 exposes regiment/ship spawn effects but no public
        # army/navy scope or merge-units effect. Consequently each requested stack
        # is represented by its exact composition at one province, while the engine
        # may initially show the spawned regiments/ships as separate selectable units.
        for army in country.armies:
            location = army.location or country.capital
            if location <= 0:
                continue
            lines.append(f"\t# OOB army: {army.name} | inf={army.infantry} cav={army.cavalry} art={army.artillery} location={location}")
            for unit, amount in (("infantry", army.infantry), ("cavalry", army.cavalry), ("artillery", army.artillery)):
                for _ in range(max(0, int(amount))):
                    lines.append(f"\t{unit} = {location}")
        for navy in country.navies:
            location = navy.location or country.capital
            if location <= 0:
                continue
            lines.append(f"\t# OOB navy: {navy.name} | heavy={navy.heavy_ship} light={navy.light_ship} galley={navy.galley} transport={navy.transport} location={location}")
            for unit, amount in (("heavy_ship", navy.heavy_ship), ("light_ship", navy.light_ship), ("galley", navy.galley), ("transport", navy.transport)):
                for _ in range(max(0, int(amount))):
                    lines.append(f"\t{unit} = {location}")

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
            # Stack location 0 means capital. Validate after auto-capital selection
            # so fleets can never silently spawn in an inland province.
            for army in c.armies:
                location = army.location or c.capital
                if location <= 0 or location not in self.map_data.province_ids or location in self.map_data.water_provinces:
                    raise ValueError(f"{c.tag} army '{army.name}' has no valid land starting province.")
            for navy in c.navies:
                location = navy.location or c.capital
                if location <= 0 or location not in self.map_data.coastal_provinces:
                    raise ValueError(f"{c.tag} fleet '{navy.name}' must start in a coastal province. Set an explicit coastal province ID if the capital is inland.")

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


class NamelistImportDialog(tk.Toplevel):
    def __init__(self, parent, culture: ItemInfo, source_path: Path, imported: HOI4NamelistImport):
        super().__init__(parent)
        self.title(f"Import HOI4 namelist — {culture.loc_name}")
        self.geometry("720x570")
        self.minsize(620, 470)
        self.transient(parent)
        self.grab_set()
        self.result = False

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text=f"Culture: {culture.loc_name} [{culture.id}]\nSource: {source_path}",
            font=("Segoe UI", 10, "bold"),
            wraplength=680,
        ).pack(anchor="w")

        summary = (
            f"Male first names: {len(imported.male_names)}    "
            f"Female first names: {len(imported.female_names)}    "
            f"Dynasty/surnames: {len(imported.dynasty_names)}"
        )
        ttk.Label(outer, text=summary).pack(anchor="w", pady=(8, 2))
        if imported.unisex_names:
            ttk.Label(
                outer,
                text=f"HOI4 top-level/unisex names: {len(imported.unisex_names)} — copied into both EU4 male and female pools.",
                wraplength=680,
            ).pack(anchor="w")
        if imported.ignored_callsigns:
            ttk.Label(
                outer,
                text=f"Ignored callsigns: {len(imported.ignored_callsigns)} (EU4 culture namelists have no callsign pool).",
                wraplength=680,
            ).pack(anchor="w")

        ttk.Label(
            outer,
            text=(
                "Importing replaces this culture's own male_names, female_names and dynasty_names blocks. "
                "It does not modify other cultures or culture-group-wide name pools."
            ),
            wraplength=680,
        ).pack(anchor="w", pady=(8, 8))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        for title, values in (
            ("Male", imported.male_names),
            ("Female", imported.female_names),
            ("Dynasties", imported.dynasty_names),
        ):
            frame = ttk.Frame(notebook, padding=8)
            notebook.add(frame, text=f"{title} ({len(values)})")
            text = tk.Text(frame, wrap="word", height=14)
            text.pack(fill="both", expand=True)
            text.insert("1.0", "\n".join(values) if values else "(none)")
            text.configure(state="disabled")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Import into culture", command=self._accept).pack(side="right", padx=(0, 6))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.wait_visibility()
        self.focus_set()

    def _accept(self):
        self.result = True
        self.destroy()


class GroupNamelistImportDialog(tk.Toplevel):
    def __init__(
        self, parent, group: GroupInfo, cultures: Sequence[ItemInfo],
        source_path: Path, imported: HOI4NamelistImport
    ):
        super().__init__(parent)
        self.title(f"Import HOI4 namelist — {group.loc_name}")
        self.geometry("760x640")
        self.minsize(640, 510)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.cultures = list(cultures)
        self.existing_count = sum(
            1 for item in self.cultures
            if CultureLayerModel.culture_has_own_namelist(item)
        )
        self.override_var = tk.BooleanVar(value=False)
        self.effect_var = tk.StringVar()

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text=(
                f"Culture group: {group.loc_name} [{group.id}]\n"
                f"Cultures in group: {len(self.cultures)}\nSource: {source_path}"
            ),
            font=("Segoe UI", 10, "bold"),
            wraplength=710,
        ).pack(anchor="w")

        summary = (
            f"Male first names: {len(imported.male_names)}    "
            f"Female first names: {len(imported.female_names)}    "
            f"Dynasty/surnames: {len(imported.dynasty_names)}"
        )
        ttk.Label(outer, text=summary).pack(anchor="w", pady=(8, 2))
        if imported.unisex_names:
            ttk.Label(
                outer,
                text=f"HOI4 top-level/unisex names: {len(imported.unisex_names)} — copied into both EU4 male and female pools.",
                wraplength=710,
            ).pack(anchor="w")
        if imported.ignored_callsigns:
            ttk.Label(
                outer,
                text=f"Ignored callsigns: {len(imported.ignored_callsigns)} (EU4 culture namelists have no callsign pool).",
                wraplength=710,
            ).pack(anchor="w")

        options = ttk.LabelFrame(outer, text="Group import behaviour", padding=10)
        options.pack(fill="x", pady=(10, 8))
        ttk.Checkbutton(
            options,
            text="Override cultures that already have their own name lists",
            variable=self.override_var,
            command=self._update_effect,
        ).pack(anchor="w")
        ttk.Label(
            options,
            textvariable=self.effect_var,
            wraplength=680,
        ).pack(anchor="w", pady=(6, 0))

        ttk.Label(
            outer,
            text=(
                "The group import is expanded into culture-specific EU4 name pools. "
                "With override disabled, cultures that already define male_names, "
                "female_names or dynasty_names are preserved exactly."
            ),
            wraplength=710,
        ).pack(anchor="w", pady=(0, 8))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        for title, values in (
            ("Male", imported.male_names),
            ("Female", imported.female_names),
            ("Dynasties", imported.dynasty_names),
        ):
            frame = ttk.Frame(notebook, padding=8)
            notebook.add(frame, text=f"{title} ({len(values)})")
            text = tk.Text(frame, wrap="word", height=12)
            text.pack(fill="both", expand=True)
            text.insert("1.0", "\n".join(values) if values else "(none)")
            text.configure(state="disabled")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Import into group", command=self._accept).pack(side="right", padx=(0, 6))
        self.bind("<Escape>", lambda _e: self.destroy())
        self._update_effect()
        self.wait_visibility()
        self.focus_set()

    def _update_effect(self):
        total = len(self.cultures)
        if self.override_var.get():
            self.effect_var.set(
                f"Will import into all {total} culture(s). "
                f"{self.existing_count} culture(s) with existing direct name pools will be replaced."
            )
        else:
            applied = total - self.existing_count
            self.effect_var.set(
                f"Will import into {applied} culture(s) with no direct name pool and preserve/skip "
                f"{self.existing_count} culture(s) that already have one."
            )

    def _accept(self):
        self.result = {"override_existing": bool(self.override_var.get())}
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



def _ordinal(n: int) -> str:
    n = max(1, int(n))
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _split_evenly(total: int, groups: int) -> List[int]:
    """Split an integer total as evenly as possible, putting remainders first."""
    total = max(0, int(total))
    groups = max(0, int(groups))
    if groups <= 0:
        return []
    q, r = divmod(total, groups)
    return [q + (1 if i < r else 0) for i in range(groups)]


def _repeat_locations(candidates: List[int], amount: int, fallback: int = 0) -> List[int]:
    """Cycle through candidate provinces for generated stacks."""
    if amount <= 0:
        return []
    candidates = [int(x) for x in candidates if int(x) > 0]
    if not candidates:
        return [max(0, int(fallback))] * amount
    return [candidates[i % len(candidates)] for i in range(amount)]


class ForceEntryDialog(tk.Toplevel):
    def __init__(self, parent, model: CountryLayerModel, kind: str, initial=None):
        super().__init__(parent)
        self.model=model; self.kind=kind; self.result=None
        self.title("Army stack" if kind=="army" else "Navy stack")
        self.transient(parent); self.grab_set(); self.resizable(False,False)
        f=ttk.Frame(self,padding=12); f.pack(fill="both",expand=True); f.columnconfigure(1,weight=1)
        if kind=="army":
            initial=initial or ArmyStackInfo(); default_name="1st Army"
            fields=(("Infantry","infantry"),("Cavalry","cavalry"),("Artillery","artillery"))
        else:
            initial=initial or NavyStackInfo(); default_name="1st Fleet"
            fields=(("Heavy ships","heavy_ship"),("Light ships","light_ship"),("Galleys","galley"),("Transports","transport"))
        self.name_var=tk.StringVar(value=initial.name or default_name); self.location_var=tk.IntVar(value=initial.location)
        ttk.Label(f,text="Stack name").grid(row=0,column=0,sticky="w",pady=4); ttk.Entry(f,textvariable=self.name_var,width=34).grid(row=0,column=1,sticky="ew",padx=(8,0),pady=4)
        ttk.Label(f,text="Location province ID").grid(row=1,column=0,sticky="w",pady=4); ttk.Spinbox(f,from_=0,to=99999,textvariable=self.location_var,width=10).grid(row=1,column=1,sticky="w",padx=(8,0),pady=4)
        note="Land province; 0 uses the country's capital." if kind=="army" else "Coastal province with a sea port; 0 uses the capital if coastal."
        ttk.Label(f,text=note,wraplength=360).grid(row=2,column=0,columnspan=2,sticky="w",pady=(0,8))
        self.count_vars={}
        row=3
        for label,key in fields:
            v=tk.IntVar(value=getattr(initial,key)); self.count_vars[key]=v
            ttk.Label(f,text=label).grid(row=row,column=0,sticky="w",pady=3); ttk.Spinbox(f,from_=0,to=999,textvariable=v,width=8).grid(row=row,column=1,sticky="w",padx=(8,0),pady=3); row+=1
        b=ttk.Frame(f); b.grid(row=row,column=0,columnspan=2,sticky="e",pady=(10,0)); ttk.Button(b,text="Cancel",command=self.destroy).pack(side="right"); ttk.Button(b,text="OK",command=self.ok).pack(side="right",padx=(0,6))

    def ok(self):
        try: loc=max(0,int(self.location_var.get()))
        except Exception: messagebox.showerror(APP_TITLE,"Province ID must be numeric.",parent=self); return
        vals={}
        for k,v in self.count_vars.items():
            try: vals[k]=max(0,int(v.get()))
            except Exception: vals[k]=0
        if sum(vals.values())<=0:
            messagebox.showerror(APP_TITLE,"Add at least one regiment or ship.",parent=self); return
        if loc and (loc not in self.model.map_data.province_ids or loc in self.model.map_data.water_provinces):
            messagebox.showerror(APP_TITLE,f"Province {loc} is not a valid land province.",parent=self); return
        if self.kind=="navy" and loc and loc not in self.model.map_data.coastal_provinces:
            messagebox.showerror(APP_TITLE,f"Province {loc} is not coastal. EU4 ship-spawn effects require a province with a port.",parent=self); return
        name=self.name_var.get().strip() or ("Army" if self.kind=="army" else "Fleet")
        self.result=ArmyStackInfo(name=name,location=loc,**vals) if self.kind=="army" else NavyStackInfo(name=name,location=loc,**vals)
        self.destroy()



class AutoForceDialog(tk.Toplevel):
    """Generate editable army/fleet rows from total unit counts."""

    def __init__(self, parent, model: CountryLayerModel, country: CountryInfo,
                 current_armies: List[ArmyStackInfo], current_navies: List[NavyStackInfo]):
        super().__init__(parent)
        self.model = model
        self.country = country
        self.current_armies = current_armies
        self.current_navies = current_navies
        self.result = None
        self.title("Auto-generate armies and fleets")
        self.geometry("650x590")
        self.minsize(580, 520)
        self.transient(parent)
        self.grab_set()

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        ttk.Label(
            outer,
            text=(
                "Enter TOTAL regiments/ships and how many logical armies/fleets to split them into. "
                "The totals are divided as evenly as possible; any remainder goes to the earlier stacks. "
                "The generated rows remain fully editable afterwards."
            ),
            wraplength=610,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 10))

        self.replace_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            outer,
            text="Replace the current army/navy list (uncheck to append)",
            variable=self.replace_var,
        ).grid(row=1, column=0, sticky="w", pady=(0, 8))

        self.placement_var = tk.StringVar(value="Spread across owned provinces")
        place = ttk.Frame(outer)
        place.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(place, text="Placement").pack(side="left")
        ttk.Combobox(
            place,
            textvariable=self.placement_var,
            state="readonly",
            width=34,
            values=("Spread across owned provinces", "Capital / default province"),
        ).pack(side="left", padx=(8, 0))

        land = ttk.LabelFrame(outer, text="Armies", padding=10)
        land.grid(row=3, column=0, sticky="ew", pady=5)
        land.columnconfigure(1, weight=1)
        self.army_count_var = tk.IntVar(value=max(1, len(current_armies) or 1))
        self.inf_var = tk.IntVar(value=sum(a.infantry for a in current_armies))
        self.cav_var = tk.IntVar(value=sum(a.cavalry for a in current_armies))
        self.art_var = tk.IntVar(value=sum(a.artillery for a in current_armies))
        self._spin_row(land, 0, "Number of armies", self.army_count_var, 1, 100)
        self._spin_row(land, 1, "Total infantry", self.inf_var, 0, 9999)
        self._spin_row(land, 2, "Total cavalry", self.cav_var, 0, 9999)
        self._spin_row(land, 3, "Total artillery", self.art_var, 0, 9999)

        sea = ttk.LabelFrame(outer, text="Fleets", padding=10)
        sea.grid(row=4, column=0, sticky="ew", pady=5)
        sea.columnconfigure(1, weight=1)
        self.fleet_count_var = tk.IntVar(value=max(1, len(current_navies) or 1))
        self.heavy_var = tk.IntVar(value=sum(n.heavy_ship for n in current_navies))
        self.light_var = tk.IntVar(value=sum(n.light_ship for n in current_navies))
        self.galley_var = tk.IntVar(value=sum(n.galley for n in current_navies))
        self.transport_var = tk.IntVar(value=sum(n.transport for n in current_navies))
        self._spin_row(sea, 0, "Number of fleets", self.fleet_count_var, 1, 100)
        self._spin_row(sea, 1, "Total heavy ships", self.heavy_var, 0, 9999)
        self._spin_row(sea, 2, "Total light ships", self.light_var, 0, 9999)
        self._spin_row(sea, 3, "Total galleys", self.galley_var, 0, 9999)
        self._spin_row(sea, 4, "Total transports", self.transport_var, 0, 9999)

        ttk.Label(
            outer,
            text=(
                "A zero total means 'generate none of that branch'. For fleets, spread placement uses owned coastal "
                "provinces. If none exist, fleets fall back to province 0 and the normal OOB validation will ask you "
                "to choose a valid port before saving."
            ),
            wraplength=610,
        ).grid(row=5, column=0, sticky="ew", pady=(8, 0))

        buttons = ttk.Frame(outer)
        buttons.grid(row=6, column=0, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Generate", command=self.ok).pack(side="right", padx=(0, 6))

    @staticmethod
    def _spin_row(parent, row, label, var, low, high):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Spinbox(parent, from_=low, to=high, textvariable=var, width=9).grid(
            row=row, column=1, sticky="w", padx=(8, 0), pady=3
        )

    def _ival(self, var, label: str) -> int:
        try:
            value = int(var.get())
        except Exception:
            raise ValueError(f"{label} must be a whole number.")
        if value < 0:
            raise ValueError(f"{label} cannot be negative.")
        return value

    def ok(self):
        try:
            armies = self._ival(self.army_count_var, "Number of armies")
            infantry = self._ival(self.inf_var, "Infantry")
            cavalry = self._ival(self.cav_var, "Cavalry")
            artillery = self._ival(self.art_var, "Artillery")
            fleets = self._ival(self.fleet_count_var, "Number of fleets")
            heavy = self._ival(self.heavy_var, "Heavy ships")
            light = self._ival(self.light_var, "Light ships")
            galley = self._ival(self.galley_var, "Galleys")
            transport = self._ival(self.transport_var, "Transports")
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self)
            return

        land_total = infantry + cavalry + artillery
        naval_total = heavy + light + galley + transport
        if land_total == 0:
            armies = 0
        elif armies <= 0:
            messagebox.showerror(APP_TITLE, "Choose at least one army when land-unit totals are non-zero.", parent=self)
            return
        elif armies > land_total:
            messagebox.showerror(APP_TITLE, f"{land_total} land regiments cannot be split into {armies} non-empty armies.", parent=self)
            return

        if naval_total == 0:
            fleets = 0
        elif fleets <= 0:
            messagebox.showerror(APP_TITLE, "Choose at least one fleet when ship totals are non-zero.", parent=self)
            return
        elif fleets > naval_total:
            messagebox.showerror(APP_TITLE, f"{naval_total} ships cannot be split into {fleets} non-empty fleets.", parent=self)
            return

        owned = self.model.owned_provinces(self.country.tag)
        capital = max(0, int(self.country.capital))
        if capital in owned:
            owned = [capital] + [p for p in owned if p != capital]
        coastal = [p for p in owned if p in self.model.map_data.coastal_provinces]

        spread = self.placement_var.get() == "Spread across owned provinces"
        army_locs = _repeat_locations(owned if spread else [], armies, capital)
        # If the capital is inland and placement isn't spread, prefer the first owned port for fleets.
        fleet_fallback = capital if capital in self.model.map_data.coastal_provinces else (coastal[0] if coastal else 0)
        fleet_locs = _repeat_locations(coastal if spread else [], fleets, fleet_fallback)

        nation = self.country.name.strip() or self.country.tag
        new_armies: List[ArmyStackInfo] = []
        if armies:
            infs = _split_evenly(infantry, armies)
            cavs = _split_evenly(cavalry, armies)
            arts = _split_evenly(artillery, armies)
            # Independent even splits can theoretically leave an empty row when counts are sparse.
            # Rebalance by moving one regiment from the fullest row if needed.
            rows = [[infs[i], cavs[i], arts[i]] for i in range(armies)]
            for i, row in enumerate(rows):
                if sum(row) > 0:
                    continue
                donor = max(range(armies), key=lambda j: sum(rows[j]))
                if sum(rows[donor]) <= 1:
                    continue
                k = max(range(3), key=lambda x: rows[donor][x])
                rows[donor][k] -= 1
                row[k] += 1
            for i, row in enumerate(rows):
                new_armies.append(ArmyStackInfo(
                    name=f"{_ordinal(i + 1)} Army of {nation}",
                    location=army_locs[i] if i < len(army_locs) else 0,
                    infantry=row[0], cavalry=row[1], artillery=row[2],
                ))

        new_navies: List[NavyStackInfo] = []
        if fleets:
            heavies = _split_evenly(heavy, fleets)
            lights = _split_evenly(light, fleets)
            galleys = _split_evenly(galley, fleets)
            transports = _split_evenly(transport, fleets)
            rows = [[heavies[i], lights[i], galleys[i], transports[i]] for i in range(fleets)]
            for i, row in enumerate(rows):
                if sum(row) > 0:
                    continue
                donor = max(range(fleets), key=lambda j: sum(rows[j]))
                if sum(rows[donor]) <= 1:
                    continue
                k = max(range(4), key=lambda x: rows[donor][x])
                rows[donor][k] -= 1
                row[k] += 1
            for i, row in enumerate(rows):
                new_navies.append(NavyStackInfo(
                    name=f"{_ordinal(i + 1)} Fleet of {nation}",
                    location=fleet_locs[i] if i < len(fleet_locs) else 0,
                    heavy_ship=row[0], light_ship=row[1], galley=row[2], transport=row[3],
                ))

        if self.replace_var.get():
            armies_out, navies_out = new_armies, new_navies
        else:
            armies_out = [copy.deepcopy(x) for x in self.current_armies] + new_armies
            navies_out = [copy.deepcopy(x) for x in self.current_navies] + new_navies
        self.result = (armies_out, navies_out)
        self.destroy()


class VanillaSymbolDialog(tk.Toplevel):
    """Visual browser for the 128 bundled vanilla client-state symbols."""

    def __init__(self, parent, initial_emblem: str = ""):
        super().__init__(parent)
        self.title("Vanilla client-state symbols")
        self.geometry("820x690")
        self.minsize(650, 520)
        self.transient(parent)
        self.grab_set()
        self.result: Optional[str] = None
        self._photos: List[ImageTk.PhotoImage] = []

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text=(
                "120 vanilla client-state/custom-nation symbols. "
                "These use EU4's 1-based emblem indices (1-120); atlas cell 0 is reserved."
            ),
            wraplength=780,
        ).pack(anchor="w", pady=(0, 8))

        available = _vanilla_symbol_sheet_for_target(64)
        if available is None:
            ttk.Label(
                outer,
                text=(
                    "The vanilla_symbols DDS assets are missing. "
                    "Re-extract the complete tool package rather than replacing only the .py file."
                ),
            ).pack(anchor="w", pady=12)
            ttk.Button(outer, text="Close", command=self.destroy).pack(anchor="e")
            return

        canvas_frame = ttk.Frame(outer)
        canvas_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(canvas_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))

        selected_index = vanilla_symbol_index(initial_emblem)

        columns = 8
        for col in range(columns):
            inner.columnconfigure(col, weight=1)

        for index in range(VANILLA_SYMBOL_COUNT):
            icon = load_vanilla_symbol(index, 52)
            thumb = Image.new("RGBA", (64, 64), (112, 112, 112, 255))
            if icon is not None:
                thumb.alpha_composite(icon, ((64 - icon.width) // 2, (64 - icon.height) // 2))
            photo = ImageTk.PhotoImage(thumb)
            self._photos.append(photo)

            label = f"{index + 1:03d}"
            if index == selected_index:
                label += "  ✓"

            button = ttk.Button(
                inner,
                text=label,
                image=photo,
                compound="top",
                command=lambda i=index: self._select(i),
            )
            button.grid(
                row=index // columns,
                column=index % columns,
                padx=4,
                pady=4,
                sticky="nsew",
            )

        # Wheel scrolling while the pointer is over the browser.
        def on_wheel(event):
            delta = 0
            if getattr(event, "delta", 0):
                delta = -1 if event.delta > 0 else 1
            elif getattr(event, "num", None) == 4:
                delta = -1
            elif getattr(event, "num", None) == 5:
                delta = 1
            if delta:
                canvas.yview_scroll(delta * 3, "units")

        canvas.bind("<MouseWheel>", on_wheel)
        canvas.bind("<Button-4>", on_wheel)
        canvas.bind("<Button-5>", on_wheel)

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")

    def _select(self, index: int) -> None:
        self.result = vanilla_symbol_name(index)
        self.destroy()


class CountryDialog(tk.Toplevel):
    PATTERNS = [
        "Solid", "Horizontal bicolor", "Horizontal tricolor", "Horizontal 1:2:1",
        "Vertical bicolor", "Vertical tricolor", "Vertical 1:2:1",
        "Diagonal", "Diagonal down", "Diagonal up", "Diagonal tricolor down", "Diagonal tricolor up",
        "Quartered", "Quartered alternating", "Three quarters",
        "Simple cross", "Center cross", "Nordic cross simple", "Nordic cross",
        "Saltire", "Saltire bordered", "Cross and saltire",
        "Hoist triangle", "Fly triangle", "Double triangle", "Chevron", "Double chevron",
        "Center diamond", "Center lozenge", "Upper canton", "Lower canton", "Canton and stripe",
        "Border", "Double border", "Center band horizontal", "Center band vertical",
        "Greek cross field", "Central disk", "Sunburst 8", "Gyronny 8",
        "Horizontal five bands", "Vertical five bands",
    ]
    EMBLEMS = [
        "None", "Circle", "Ring", "Diamond", "Star", "Star 5", "Star 6", "Star 8",
        "Crescent", "Crescent & star", "Cross", "Latin cross", "Maltese cross",
        "Fleur-de-lis", "Crown", "Shield", "Castle", "Tower", "Anchor",
        "Sword", "Crossed swords", "Axe", "Spear", "Tree", "Sun", "Moon",
        "Flame", "Gear", "Wheel", "Book", "Heart", "Skull", "Paw", "Cat head",
        "Mountain", "Wave", "Custom image",
    ] + [vanilla_symbol_name(i) for i in range(VANILLA_SYMBOL_COUNT)]

    def __init__(self, parent, model: CountryLayerModel, initial: CountryInfo, new_country: bool):
        super().__init__(parent)
        self.model=model; self.initial=initial; self.new_country=new_country; self.result=None; self.upload_selected: Optional[Path]=None; self.emblem_upload_selected: Optional[Path]=None
        self.armies=[copy.deepcopy(x) for x in initial.armies]; self.navies=[copy.deepcopy(x) for x in initial.navies]
        self.title("New country" if new_country else f"Edit {initial.tag} — {initial.name}")
        self.geometry("900x760"); self.minsize(760,620); self.transient(parent); self.grab_set()
        outer=ttk.Frame(self,padding=10); outer.pack(fill="both",expand=True)
        self.nb=ttk.Notebook(outer); self.nb.pack(fill="both",expand=True)
        self.identity=ttk.Frame(self.nb,padding=10); self.setup=ttk.Frame(self.nb,padding=10); self.court=ttk.Frame(self.nb,padding=5); self.forces_tab=ttk.Frame(self.nb,padding=10); self.estates_tab=ttk.Frame(self.nb,padding=10); self.ideas_tab=ttk.Frame(self.nb,padding=10)
        self.nb.add(self.identity,text="Identity & flag"); self.nb.add(self.setup,text="Starting setup"); self.nb.add(self.court,text="Ruler / heir / consort"); self.nb.add(self.forces_tab,text="Army / navy OOB"); self.nb.add(self.estates_tab,text="Estates"); self.nb.add(self.ideas_tab,text="National ideas")
        self._identity_tab(); self._setup_tab(); self._court_tab(); self._forces_tab(); self._estates_tab(); self._ideas_tab()
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
        self.emblem_label=tk.StringVar(value=self.initial.flag_emblem_source or "No custom emblem image")
        ef=ttk.Frame(flag); ef.grid(row=5,column=0,columnspan=2,sticky="ew",pady=(1,4))
        self.vanilla_symbol_button=ttk.Button(ef,text="Browse vanilla symbols…",command=self.browse_vanilla_symbols); self.vanilla_symbol_button.pack(side="left")
        self.emblem_upload_button=ttk.Button(ef,text="Choose custom emblem…",command=self.choose_emblem); self.emblem_upload_button.pack(side="left",padx=(6,0))
        ttk.Label(ef,textvariable=self.emblem_label,wraplength=300).pack(side="left",padx=(8,0))
        self.flag_colour_vars=[tk.StringVar(value=c) for c in self.initial.flag_colours]
        cframe=ttk.Frame(flag); cframe.grid(row=6,column=0,columnspan=2,sticky="w",pady=5)
        self.flag_colour_entries=[]
        for i,var in enumerate(self.flag_colour_vars,1):
            ttk.Label(cframe,text=f"Colour {i}").pack(side="left",padx=(0,3)); ent=ttk.Entry(cframe,textvariable=var,width=10); ent.pack(side="left"); self.flag_colour_entries.append(ent); ttk.Button(cframe,text="…",width=3,command=lambda v=var:self.pick_var_colour(v)).pack(side="left",padx=(2,8))
        self.flag_preview=tk.Canvas(flag,width=192,height=192,highlightthickness=1); self.flag_preview.grid(row=0,column=2,rowspan=7,padx=(14,0),sticky="n")
        for var in [self.pattern_var,self.emblem_var,*self.flag_colour_vars]: var.trace_add("write",lambda *_:self.update_flag_preview())
        self.refresh_flag_controls(); self.update_flag_preview()

    def pick_country_colour(self): self.pick_var_colour(self.country_colour_var)
    def pick_var_colour(self,var):
        try: init=parse_hex_colour(var.get())
        except Exception: init="#808080"
        c=colorchooser.askcolor(initialcolor=init,parent=self)[1]
        if c: var.set(c.upper())

    def browse_vanilla_symbols(self):
        dlg = VanillaSymbolDialog(self, self.emblem_var.get())
        self.wait_window(dlg)
        if dlg.result:
            self.emblem_var.set(dlg.result)
            self.update_flag_preview()

    def choose_flag(self):
        fn=filedialog.askopenfilename(parent=self,title="Choose flag image",filetypes=[("Images","*.png *.tga *.bmp *.jpg *.jpeg *.tif *.tiff"),("All files","*.*")])
        if fn:
            self.upload_selected=Path(fn); self.upload_label.set(fn); self.update_flag_preview()

    def choose_emblem(self):
        fn=filedialog.askopenfilename(parent=self,title="Choose emblem image",filetypes=[("Images","*.png *.tga *.bmp *.jpg *.jpeg *.tif *.tiff"),("All files","*.*")])
        if fn:
            self.emblem_upload_selected=Path(fn); self.emblem_label.set(fn); self.emblem_var.set("Custom image"); self.update_flag_preview()

    def refresh_flag_controls(self):
        designer=self.flag_mode_var.get()=="designer"
        for w in [self.pattern_box,self.emblem_box,*self.flag_colour_entries]:
            try: w.configure(state="readonly" if isinstance(w,ttk.Combobox) and designer else ("normal" if designer else "disabled"))
            except Exception: pass
        self.upload_button.configure(state="disabled" if designer else "normal")
        self.emblem_upload_button.configure(state="normal" if designer else "disabled")
        self.vanilla_symbol_button.configure(state="normal" if designer else "disabled")
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
                esource=self.emblem_upload_selected or self.model._resolve_emblem_source(self.initial.flag_emblem_source)
                img=generate_designer_flag(self.pattern_var.get(),cols,self.emblem_var.get(),192,esource)
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

    def _forces_tab(self):
        top=ttk.Frame(self.forces_tab); top.pack(fill="x",pady=(0,8))
        ttk.Label(top,text=("Each row is a logical OOB stack you can edit. EU4's public script effects spawn regiments/ships individually; "
                                 "the editor preserves the intended stack composition and location, but the engine has no exposed merge-units effect."),
                  wraplength=650).pack(side="left",fill="x",expand=True)
        ttk.Button(top,text="Auto-generate armies & fleets…",command=self._auto_generate_forces).pack(side="right",padx=(10,0))
        paned=ttk.Panedwindow(self.forces_tab,orient="vertical"); paned.pack(fill="both",expand=True)
        af=ttk.LabelFrame(paned,text="Armies",padding=6); nf=ttk.LabelFrame(paned,text="Navies",padding=6); paned.add(af,weight=1); paned.add(nf,weight=1)
        self.army_tree=ttk.Treeview(af,columns=("loc","inf","cav","art"),show="tree headings",height=7)
        self.army_tree.heading("#0",text="Army"); self.army_tree.heading("loc",text="Province"); self.army_tree.heading("inf",text="Inf"); self.army_tree.heading("cav",text="Cav"); self.army_tree.heading("art",text="Art")
        for col,w in (("#0",260),("loc",80),("inf",60),("cav",60),("art",60)): self.army_tree.column(col,width=w,anchor="w" if col=="#0" else "center")
        self.army_tree.pack(fill="both",expand=True); ab=ttk.Frame(af); ab.pack(fill="x",pady=(5,0)); ttk.Button(ab,text="Add army",command=lambda:self._add_force("army")).pack(side="left"); ttk.Button(ab,text="Edit",command=lambda:self._edit_force("army")).pack(side="left",padx=5); ttk.Button(ab,text="Remove",command=lambda:self._remove_force("army")).pack(side="left")
        self.navy_tree=ttk.Treeview(nf,columns=("loc","heavy","light","galley","transport"),show="tree headings",height=7)
        self.navy_tree.heading("#0",text="Fleet"); self.navy_tree.heading("loc",text="Province"); self.navy_tree.heading("heavy",text="Heavy"); self.navy_tree.heading("light",text="Light"); self.navy_tree.heading("galley",text="Galley"); self.navy_tree.heading("transport",text="Transport")
        for col,w in (("#0",240),("loc",80),("heavy",60),("light",60),("galley",60),("transport",75)): self.navy_tree.column(col,width=w,anchor="w" if col=="#0" else "center")
        self.navy_tree.pack(fill="both",expand=True); nb=ttk.Frame(nf); nb.pack(fill="x",pady=(5,0)); ttk.Button(nb,text="Add fleet",command=lambda:self._add_force("navy")).pack(side="left"); ttk.Button(nb,text="Edit",command=lambda:self._edit_force("navy")).pack(side="left",padx=5); ttk.Button(nb,text="Remove",command=lambda:self._remove_force("navy")).pack(side="left")
        self.army_tree.bind("<Double-1>",lambda _e:self._edit_force("army")); self.navy_tree.bind("<Double-1>",lambda _e:self._edit_force("navy")); self._refresh_forces()

    def _auto_generate_forces(self):
        # Build a temporary country snapshot with current name/capital fields so the dialog
        # reflects edits made in this window even before OK is pressed.
        country = copy.deepcopy(self.initial)
        country.name = self.name_var.get().strip() or country.name
        try:
            country.capital = max(0, int(self.capital_var.get()))
        except Exception:
            pass
        dlg = AutoForceDialog(self, self.model, country, self.armies, self.navies)
        self.wait_window(dlg)
        if dlg.result:
            self.armies, self.navies = dlg.result
            self._refresh_forces()

    def _refresh_forces(self):
        if not hasattr(self,"army_tree"): return
        self.army_tree.delete(*self.army_tree.get_children()); self.navy_tree.delete(*self.navy_tree.get_children())
        for i,a in enumerate(self.armies):
            self.army_tree.insert("","end",iid=f"a{i}",text=a.name,values=(a.location or "Capital",a.infantry,a.cavalry,a.artillery))
        for i,n in enumerate(self.navies):
            self.navy_tree.insert("","end",iid=f"n{i}",text=n.name,values=(n.location or "Capital",n.heavy_ship,n.light_ship,n.galley,n.transport))

    def _force_index(self, kind):
        tree=self.army_tree if kind=="army" else self.navy_tree; sel=tree.selection()
        if not sel: return None
        try: return int(sel[0][1:])
        except Exception: return None

    def _add_force(self,kind):
        dlg=ForceEntryDialog(self,self.model,kind); self.wait_window(dlg)
        if dlg.result:
            (self.armies if kind=="army" else self.navies).append(dlg.result); self._refresh_forces()

    def _edit_force(self,kind):
        idx=self._force_index(kind)
        if idx is None: return
        seq=self.armies if kind=="army" else self.navies
        if idx>=len(seq): return
        dlg=ForceEntryDialog(self,self.model,kind,seq[idx]); self.wait_window(dlg)
        if dlg.result: seq[idx]=dlg.result; self._refresh_forces()

    def _remove_force(self,kind):
        idx=self._force_index(kind)
        if idx is None: return
        seq=self.armies if kind=="army" else self.navies
        if 0<=idx<len(seq): seq.pop(idx); self._refresh_forces()

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
        # Validate starting-force locations. A stack location of 0 means the
        # country's chosen capital (or the automatically selected capital once
        # painted territory exists). Explicit locations must be valid now.
        for army in self.armies:
            loc = army.location or capital
            if loc and (loc not in self.model.map_data.province_ids or loc in self.model.map_data.water_provinces):
                messagebox.showerror(APP_TITLE, f"Army '{army.name}' has invalid land province {loc}.", parent=self); return
        for navy in self.navies:
            loc = navy.location or capital
            if loc and loc not in self.model.map_data.coastal_provinces:
                messagebox.showerror(APP_TITLE, f"Fleet '{navy.name}' must start in a coastal province; province {loc} is not coastal.", parent=self); return

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
        emblem_source=self.initial.flag_emblem_source
        if self.emblem_var.get()=="Custom image":
            if self.emblem_upload_selected:
                try: emblem_source=self.model.store_emblem_source(tag,self.emblem_upload_selected)
                except Exception as exc:
                    messagebox.showerror(APP_TITLE,f"Could not copy emblem source:\n{exc}",parent=self); return
            elif not self.model._resolve_emblem_source(emblem_source):
                messagebox.showerror(APP_TITLE,"Choose an image for the custom emblem.",parent=self); return
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
            flag_mode=self.flag_mode_var.get(),flag_source=flag_source,flag_pattern=self.pattern_var.get(),flag_colours=flag_cols,flag_emblem=self.emblem_var.get(),flag_emblem_source=emblem_source,
            armies=[copy.deepcopy(x) for x in self.armies],navies=[copy.deepcopy(x) for x in self.navies],
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
        self.zoom=.5; self.tk_map=None; self.map_item=None; self.render_after=None; self.hover_pid=0; self.hover_tip_rect=None; self.hover_tip_text=None; self.drag_seen:set= set()
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
        self.canvas.bind("<Button-1>",self.paint_press); self.canvas.bind("<B1-Motion>",self.paint_drag); self.canvas.bind("<ButtonRelease-1>",self.paint_release); self.canvas.bind("<Motion>",self.hover); self.canvas.bind("<Leave>",lambda _e:self._hide_hover_tip()); self.canvas.bind("<MouseWheel>",self.mousewheel); self.canvas.bind("<Button-4>",lambda _e:self.change_zoom(1.15)); self.canvas.bind("<Button-5>",lambda _e:self.change_zoom(.87)); self.canvas.bind("<Button-2>",lambda e:self.canvas.scan_mark(e.x,e.y)); self.canvas.bind("<B2-Motion>",lambda e:self.canvas.scan_dragto(e.x,e.y,gain=1)); self.canvas.bind("<Button-3>",lambda e:self.canvas.scan_mark(e.x,e.y)); self.canvas.bind("<B3-Motion>",lambda e:self.canvas.scan_dragto(e.x,e.y,gain=1))
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
            ttk.Button(self.buttons,text="New group",command=self.new_group).pack(side="left"); ttk.Button(self.buttons,text="New culture",command=self.new_culture).pack(side="left",padx=(5,0)); ttk.Button(self.buttons,text="Edit",command=self.edit_selected).pack(side="left",padx=(5,0)); ttk.Button(self.buttons,text="Import names…",command=self.import_selected_namelist).pack(side="left",padx=(5,0)); self.sidebar_title.configure(text="Cultures")
        else:
            ttk.Button(self.buttons,text="New country",command=self.new_country).pack(side="left"); ttk.Button(self.buttons,text="Copy",command=self.copy_selected_country).pack(side="left",padx=(5,0)); ttk.Button(self.buttons,text="Edit",command=self.edit_selected).pack(side="left",padx=(5,0)); self.sidebar_title.configure(text="Countries")

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
                cid=iid[2:]; self.selected_id=cid; c=self.culture_model.items[cid]
                names_note = f"\nNames: {len(c.male_names)} M / {len(c.female_names)} F / {len(c.dynasty_names)} dynasties" if (c.male_names or c.female_names or c.dynasty_names) else "\nNames: none defined on this culture"
                self.selection_label.configure(text=f"Painting: {c.loc_name} ({cid})\nGroup: {c.group_id}{names_note}")
            elif iid.startswith("g:"):
                gid=iid[2:]; self.selected_id=None
                members=[c for c in self.culture_model.items.values() if c.group_id==gid]
                named=sum(1 for c in members if self.culture_model.culture_has_own_namelist(c))
                self.selection_label.configure(
                    text=f"Culture group: {self.culture_model.groups[gid].loc_name} [{gid}]\n"
                         f"{len(members)} culture(s); {named} with direct name lists.\n"
                         "Select a culture inside this group to paint, or use Import names… for the whole group."
                )
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

    def import_selected_namelist(self):
        if not self.culture_model or self.layer_var.get() != "Cultures":
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select a culture or culture group first.")
            return
        iid = sel[0]
        if not (iid.startswith("c:") or iid.startswith("g:")):
            messagebox.showinfo(APP_TITLE, "Select a culture or culture group first.")
            return

        if iid.startswith("c:"):
            cid = iid[2:]
            culture = self.culture_model.items[cid]
            target_name = culture.loc_name
        else:
            gid = iid[2:]
            group = self.culture_model.groups[gid]
            members = sorted(
                (item for item in self.culture_model.items.values() if item.group_id == gid),
                key=lambda item: item.loc_name.lower(),
            )
            if not members:
                messagebox.showinfo(APP_TITLE, "This culture group has no cultures to receive a namelist.")
                return
            target_name = group.loc_name

        filename = filedialog.askopenfilename(
            parent=self.root,
            title=f"Import HOI4 namelist for {target_name}",
            initialdir=str(self.mod_root),
            filetypes=[("HOI4 / text files", "*.txt"), ("All files", "*.*")],
        )
        if not filename:
            return
        path = Path(filename)
        try:
            text, _enc = read_text(path)
            imported = parse_hoi4_namelist_block(text)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not parse the HOI4 namelist:\n\n{exc}")
            return

        if iid.startswith("c:"):
            dlg = NamelistImportDialog(self.root, culture, path, imported)
            self.root.wait_window(dlg)
            if not dlg.result:
                return
            self.culture_model.import_hoi4_namelist(cid, imported)
            applied = [cid]
            skipped = []
            status_target = culture.loc_name
        else:
            dlg = GroupNamelistImportDialog(self.root, group, members, path, imported)
            self.root.wait_window(dlg)
            if not dlg.result:
                return
            applied, skipped = self.culture_model.import_hoi4_namelist_to_group(
                gid, imported, override_existing=dlg.result["override_existing"]
            )
            status_target = group.loc_name

        self.touched.add("Cultures")
        self.refresh_tree()
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self.tree_selected()
        skip_note = f", {len(skipped)} existing culture(s) preserved" if skipped else ""
        self._set_status(
            f"Imported HOI4 namelist for {status_target}: applied to {len(applied)} culture(s){skip_note}; "
            f"{len(imported.male_names)} male, {len(imported.female_names)} female, "
            f"{len(imported.dynasty_names)} dynasty names — Save to write EU4 culture files."
        )

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

    def copy_selected_country(self):
        if not self.country_model or self.layer_var.get()!="Countries": return
        sel=self.tree.selection()
        if not sel or not sel[0].startswith("n:") or sel[0][2:]==UNOWNED_TAG: return
        source_tag=sel[0][2:]
        if source_tag not in self.country_model.countries: return
        source=copy.deepcopy(self.country_model.countries[source_tag])
        new_tag=self._suggest_tag(); source.tag=new_tag; source.name=f"{source.name} Copy"; source.original=False; source.managed=True; source.country_file=None; source.history_file=None
        # Deliberately copy the country's setup template, not its painted territory.
        dlg=CountryDialog(self.root,self.country_model,source,True); self.root.wait_window(dlg)
        if not dlg.result: return
        try:
            tag=self.country_model.create_country(dlg.result); self.selected_id=tag; self.touched.add("Countries"); self.refresh_tree(); self.schedule_render()
        except Exception as exc: messagebox.showerror(APP_TITLE,str(exc))

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

    def _hide_hover_tip(self):
        if self.hover_tip_rect is not None:
            self.canvas.itemconfigure(self.hover_tip_rect,state="hidden")
        if self.hover_tip_text is not None:
            self.canvas.itemconfigure(self.hover_tip_text,state="hidden")

    def _show_hover_tip(self,event,text):
        x=self.canvas.canvasx(event.x)+14; y=self.canvas.canvasy(event.y)+14
        if self.hover_tip_text is None:
            self.hover_tip_rect=self.canvas.create_rectangle(x,y,x+90,y+25,fill="#111111",outline="#E8E8E8",width=1)
            self.hover_tip_text=self.canvas.create_text(x+6,y+5,text=text,anchor="nw",fill="white",font=("Segoe UI",10,"bold"))
        else:
            self.canvas.itemconfigure(self.hover_tip_text,text=text,state="normal")
            self.canvas.coords(self.hover_tip_text,x+6,y+5)
            bbox=self.canvas.bbox(self.hover_tip_text) or (x,y,x+90,y+25)
            self.canvas.coords(self.hover_tip_rect,bbox[0]-5,bbox[1]-4,bbox[2]+5,bbox[3]+4)
            self.canvas.itemconfigure(self.hover_tip_rect,state="normal")
        self.canvas.tag_raise(self.hover_tip_rect); self.canvas.tag_raise(self.hover_tip_text)

    def hover(self,event):
        if not self.map_data:return
        pid=self.province_at_event(event)
        if pid<=0:
            self._hide_hover_tip()
            if pid!=self.hover_pid:self._set_status("Outside province map")
            self.hover_pid=pid; return
        self._show_hover_tip(event,f"Province {pid}")
        if pid==self.hover_pid:return
        self.hover_pid=pid
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
        if self.hover_tip_rect is not None:
            self.canvas.tag_raise(self.hover_tip_rect); self.canvas.tag_raise(self.hover_tip_text)
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
