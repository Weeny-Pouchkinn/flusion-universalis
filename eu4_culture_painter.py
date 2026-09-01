#!/usr/bin/env python3
"""
EU4 Culture Painter
===================

Put this script in the ROOT of your EU4 mod and run it with no arguments.

Expected:
    map/provinces.bmp
    map/definition.csv
    history/provinces/*.txt
    common/cultures/*.txt

Input:
    A lossless PNG/BMP/TGA/TIFF painted in large, flat RGB colours.
    It must have exactly the same dimensions as map/provinces.bmp.

Black  (0, 0, 0)       = ignored
White  (255, 255, 255) = ignored

Dependencies:
    Pillow
    numpy

Install once with:
    py -m pip install pillow numpy

EU4 note:
EU4 does not support a "color = ..." property on cultures or culture groups.
The colour values managed by this program are therefore stored as:
    - harmless comments in common/cultures
    - .culture_painter_state.json

They are painter metadata, not invalid EU4 script fields.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import traceback
import unicodedata

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import numpy as np
    from PIL import Image

    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, ttk

except ImportError as exc:
    print()
    print("Missing Python dependency:")
    print(exc)
    print()
    print("Install the required packages with:")
    print("    py -m pip install pillow numpy")
    print()
    raise


APP_TITLE = "EU4 Culture Painter"

STATE_FILENAME = ".culture_painter_state.json"
GENERATED_CULTURES = "zz_lok_culture_painter.txt"
GENERATED_LOCALISATION = "lok_culture_painter_l_english.yml"
BACKUP_DIRECTORY = ".culture_painter_backups"

IGNORED_COLOURS = {
    0x000000,  # black
    0xFFFFFF,  # white
}

# These can be blocks at culture-group scope but are NOT cultures.
GROUP_RESERVED_BLOCKS = {
    "male_names",
    "female_names",
    "dynasty_names",
    "province",
    "country",
}

GROUP_COLOUR_COMMENT = re.compile(
    r"(?mi)^\s*#\s*lok_culture_painter_group_color\s*=\s*"
    r"(#[0-9a-f]{6})\s*$"
)

CULTURE_COLOUR_COMMENT = re.compile(
    r"(?mi)^\s*#\s*lok_culture_painter_source_color\s*=\s*"
    r"(#[0-9a-f]{6})\s*$"
)


# =============================================================================
# DATA
# =============================================================================


@dataclass
class Block:
    key: str
    start: int
    open_brace: int
    close_brace: int
    end: int


@dataclass
class GroupRecord:
    group_id: str
    path: Path
    block: Block
    colour: Optional[str] = None
    cultures: List[str] = field(default_factory=list)


@dataclass
class CultureRecord:
    culture_id: str
    group_id: str
    path: Path
    block: Block
    source_colour: Optional[str] = None


@dataclass
class CultureIndex:
    groups: Dict[str, GroupRecord] = field(default_factory=dict)
    cultures: Dict[str, CultureRecord] = field(default_factory=dict)
    duplicate_groups: Dict[str, List[Path]] = field(default_factory=dict)
    duplicate_cultures: Dict[str, List[Path]] = field(default_factory=dict)


@dataclass
class PaintRegion:
    code: int
    rgb: Tuple[int, int, int]
    hex_colour: str
    provinces: List[int]
    pixels: int


@dataclass
class Choice:
    culture_id: str = ""
    culture_name: str = ""
    group_id: str = ""
    group_name: str = ""
    group_colour: str = "#808080"
    skipped: bool = False


# =============================================================================
# BASIC UTILITIES
# =============================================================================


def read_text(path: Path) -> Tuple[str, str]:
    data = path.read_bytes()

    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"

    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            pass

    return data.decode("utf-8", errors="replace"), "utf-8"


def write_text(path: Path, text: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding, newline="")


def safe_id(value: str) -> str:
    """
    Turn "Great Meonic Culture" into "great_meonic_culture".
    Existing IDs selected from the dropdown are preserved.
    """
    value = value.strip()

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )

    value = value.lower()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value)
    value = value.strip("_")

    if not value:
        value = "unnamed"

    if value[0].isdigit():
        value = "c_" + value

    return value


def pretty_name(identifier: str) -> str:
    return identifier.replace("_", " ").title()


def rgb_to_code(rgb: Tuple[int, int, int]) -> int:
    r, g, b = rgb
    return (r << 16) | (g << 8) | b


def code_to_rgb(code: int) -> Tuple[int, int, int]:
    return (
        (code >> 16) & 255,
        (code >> 8) & 255,
        code & 255,
    )


def rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def parse_colour(text: str) -> str:
    """
    Accept:
        #AA22FF
        AA22FF
        170,34,255
        170 34 255
    """
    text = text.strip()

    if re.fullmatch(r"#?[0-9a-fA-F]{6}", text):
        return "#" + text.lstrip("#").upper()

    pieces = [
        p for p in re.split(r"[\s,;]+", text)
        if p
    ]

    if len(pieces) == 3:
        try:
            values = [int(p) for p in pieces]
        except ValueError:
            values = []

        if len(values) == 3 and all(
            0 <= value <= 255
            for value in values
        ):
            return rgb_to_hex(tuple(values))

    raise ValueError(
        "Use #RRGGBB or three RGB values, "
        "for example 122, 69, 209."
    )


def deterministic_group_colour(group_id: str) -> str:
    """
    Existing vanilla/mod groups have no actual group RGB property.
    If this painter has never seen one before, assign a stable colour
    derived from the ID so the GUI can nevertheless always display one.
    """
    digest = hashlib.sha256(
        group_id.encode("utf-8")
    ).digest()

    rgb = tuple(
        55 + (value % 156)
        for value in digest[:3]
    )

    return rgb_to_hex(rgb)


def line_start(text: str, position: int) -> int:
    found = text.rfind("\n", 0, position)
    return 0 if found == -1 else found + 1


def line_end(text: str, position: int) -> int:
    found = text.find("\n", position)
    return len(text) if found == -1 else found + 1


# =============================================================================
# CLAUSEWITZ BLOCK PARSER
# =============================================================================


def matching_brace(text: str, opening: int) -> int:
    """
    Brace matcher that ignores braces inside:
        # comments
        "strings"
    """
    depth = 0

    in_string = False
    escaped = False
    in_comment = False

    i = opening

    while i < len(text):
        char = text[i]

        if in_comment:
            if char == "\n":
                in_comment = False

        elif in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False

        else:
            if char == "#":
                in_comment = True

            elif char == '"':
                in_string = True

            elif char == "{":
                depth += 1

            elif char == "}":
                depth -= 1

                if depth == 0:
                    return i

        i += 1

    raise ValueError(
        "Unmatched { while reading a Clausewitz file."
    )


def top_level_blocks(
    text: str,
    offset: int = 0,
) -> List[Block]:
    """
    Find:
        something = {
            ...
        }

    only at brace depth 0.
    """

    output: List[Block] = []

    depth = 0
    i = 0

    in_string = False
    escaped = False
    in_comment = False

    while i < len(text):
        char = text[i]

        if in_comment:
            if char == "\n":
                in_comment = False

            i += 1
            continue

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False

            i += 1
            continue

        if char == "#":
            in_comment = True
            i += 1
            continue

        if char == '"':
            in_string = True
            i += 1
            continue

        if char == "{":
            depth += 1
            i += 1
            continue

        if char == "}":
            depth = max(0, depth - 1)
            i += 1
            continue

        if (
            depth == 0
            and (
                char.isalnum()
                or char == "_"
            )
        ):
            token_start = i

            while (
                i < len(text)
                and (
                    text[i].isalnum()
                    or text[i] in "_.:-"
                )
            ):
                i += 1

            key = text[token_start:i]

            j = i

            while (
                j < len(text)
                and text[j].isspace()
            ):
                j += 1

            if (
                j < len(text)
                and text[j] == "="
            ):
                j += 1

                while (
                    j < len(text)
                    and text[j].isspace()
                ):
                    j += 1

                if (
                    j < len(text)
                    and text[j] == "{"
                ):
                    closing = matching_brace(
                        text,
                        j,
                    )

                    output.append(
                        Block(
                            key=key,
                            start=token_start + offset,
                            open_brace=j + offset,
                            close_brace=closing + offset,
                            end=closing + 1 + offset,
                        )
                    )

                    i = closing + 1
                    continue

            continue

        i += 1

    return output


# =============================================================================
# STATE
# =============================================================================


def load_state(mod_root: Path) -> dict:
    path = mod_root / STATE_FILENAME

    blank = {
        "version": 1,
        "group_colours": {},
        "culture_source_colours": {},
        "paint_assignments": {},
    }

    if not path.exists():
        return blank

    try:
        state = json.loads(
            path.read_text(
                encoding="utf-8",
            )
        )
    except Exception:
        return blank

    if not isinstance(state, dict):
        return blank

    state.setdefault("version", 1)
    state.setdefault("group_colours", {})
    state.setdefault(
        "culture_source_colours",
        {},
    )
    state.setdefault(
        "paint_assignments",
        {},
    )

    return state


def save_state(
    mod_root: Path,
    state: dict,
) -> None:
    path = mod_root / STATE_FILENAME

    path.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )


# =============================================================================
# CULTURE FILE PARSING
# =============================================================================


def scan_cultures(
    mod_root: Path,
) -> CultureIndex:
    state = load_state(mod_root)

    state_group_colours = state.get(
        "group_colours",
        {},
    )

    state_culture_colours = state.get(
        "culture_source_colours",
        {},
    )

    directory = (
        mod_root
        / "common"
        / "cultures"
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    index = CultureIndex()

    for path in sorted(
        directory.glob("*.txt"),
        key=lambda p: p.name.lower(),
    ):
        text, _encoding = read_text(path)

        for group_block in top_level_blocks(text):
            group_id = group_block.key

            if group_id in index.groups:
                index.duplicate_groups.setdefault(
                    group_id,
                    [index.groups[group_id].path],
                ).append(path)

                continue

            group_text = text[
                group_block.start:
                group_block.end
            ]

            colour = None

            found_colour = (
                GROUP_COLOUR_COMMENT.search(
                    group_text
                )
            )

            if found_colour:
                colour = (
                    found_colour
                    .group(1)
                    .upper()
                )

            elif group_id in state_group_colours:
                try:
                    colour = parse_colour(
                        str(
                            state_group_colours[
                                group_id
                            ]
                        )
                    )
                except ValueError:
                    pass

            record = GroupRecord(
                group_id=group_id,
                path=path,
                block=group_block,
                colour=colour,
            )

            index.groups[group_id] = record

            inner_start = (
                group_block.open_brace + 1
            )
            inner_end = (
                group_block.close_brace
            )

            inner_text = text[
                inner_start:
                inner_end
            ]

            for culture_block in top_level_blocks(
                inner_text,
                offset=inner_start,
            ):
                culture_id = culture_block.key

                if (
                    culture_id
                    in GROUP_RESERVED_BLOCKS
                ):
                    continue

                if culture_id in index.cultures:
                    index.duplicate_cultures.setdefault(
                        culture_id,
                        [
                            index.cultures[
                                culture_id
                            ].path
                        ],
                    ).append(path)

                    continue

                culture_text = text[
                    culture_block.start:
                    culture_block.end
                ]

                source_colour = None

                found = (
                    CULTURE_COLOUR_COMMENT
                    .search(culture_text)
                )

                if found:
                    source_colour = (
                        found.group(1).upper()
                    )

                elif (
                    culture_id
                    in state_culture_colours
                ):
                    try:
                        source_colour = (
                            parse_colour(
                                str(
                                    state_culture_colours[
                                        culture_id
                                    ]
                                )
                            )
                        )
                    except ValueError:
                        pass

                record.cultures.append(
                    culture_id
                )

                index.cultures[
                    culture_id
                ] = CultureRecord(
                    culture_id=culture_id,
                    group_id=group_id,
                    path=path,
                    block=culture_block,
                    source_colour=source_colour,
                )

    return index


# =============================================================================
# MODIFY CULTURE DEFINITIONS
# =============================================================================


def add_or_update_comment(
    text: str,
    block: Block,
    regex: re.Pattern,
    comment_name: str,
    colour: str,
) -> str:

    piece = text[
        block.start:
        block.end
    ]

    replacement = (
        f"\t# {comment_name} = {colour}"
    )

    if regex.search(piece):
        piece = regex.sub(
            replacement,
            piece,
            count=1,
        )

    else:
        opening = (
            block.open_brace
            - block.start
        )

        insert_at = opening + 1

        piece = (
            piece[:insert_at]
            + "\n"
            + replacement
            + piece[insert_at:]
        )

    return (
        text[:block.start]
        + piece
        + text[block.end:]
    )


def generated_culture_path(
    mod_root: Path,
) -> Path:
    return (
        mod_root
        / "common"
        / "cultures"
        / GENERATED_CULTURES
    )


def ensure_generated_file(
    mod_root: Path,
) -> Path:

    path = generated_culture_path(
        mod_root
    )

    if not path.exists():
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        path.write_text(
            "# Managed by EU4 Culture Painter.\n"
            "# Do not delete while the painter is in use.\n\n",
            encoding="utf-8",
        )

    return path


def ensure_group(
    mod_root: Path,
    group_id: str,
    group_colour: str,
) -> None:

    index = scan_cultures(mod_root)

    if group_id not in index.groups:
        path = ensure_generated_file(
            mod_root
        )

        text, encoding = read_text(path)

        if (
            text
            and not text.endswith("\n")
        ):
            text += "\n"

        text += (
            "\n"
            f"{group_id} = {{\n"
            f"\t# "
            f"lok_culture_painter_group_color "
            f"= {group_colour}\n"
            f"\tgraphical_culture = westerngfx\n"
            f"}}\n"
        )

        write_text(
            path,
            text,
            encoding,
        )

        return

    group = index.groups[group_id]

    text, encoding = read_text(
        group.path
    )

    updated = add_or_update_comment(
        text=text,
        block=group.block,
        regex=GROUP_COLOUR_COMMENT,
        comment_name=(
            "lok_culture_painter_group_color"
        ),
        colour=group_colour,
    )

    if updated != text:
        write_text(
            group.path,
            updated,
            encoding,
        )


def indent_block(
    text: str,
    prefix: str = "\t",
) -> str:

    lines = (
        text.strip("\r\n")
        .splitlines()
    )

    return "\n".join(
        (
            prefix + line
            if line.strip()
            else line
        )
        for line in lines
    )


def insert_culture(
    mod_root: Path,
    group_id: str,
    culture_text: str,
) -> None:

    index = scan_cultures(mod_root)

    group = index.groups[group_id]

    text, encoding = read_text(
        group.path
    )

    closing = group.block.close_brace

    before = text[:closing].rstrip(
        " \t\r\n"
    )

    after = text[closing:]

    inserted = (
        before
        + "\n"
        + indent_block(culture_text)
        + "\n"
        + after
    )

    write_text(
        group.path,
        inserted,
        encoding,
    )


def remove_culture(
    culture: CultureRecord,
) -> str:

    text, encoding = read_text(
        culture.path
    )

    start = line_start(
        text,
        culture.block.start,
    )

    end = line_end(
        text,
        culture.block.end,
    )

    extracted = text[
        culture.block.start:
        culture.block.end
    ]

    updated = (
        text[:start]
        + text[end:]
    )

    write_text(
        culture.path,
        updated,
        encoding,
    )

    return extracted


def ensure_culture(
    mod_root: Path,
    culture_id: str,
    group_id: str,
    source_colour: str,
    group_colour: str,
) -> None:

    ensure_group(
        mod_root,
        group_id,
        group_colour,
    )

    index = scan_cultures(mod_root)

    if (
        culture_id
        in index.duplicate_cultures
    ):
        raise RuntimeError(
            f"Culture '{culture_id}' is "
            "defined more than once. "
            "Refusing to move an ambiguous "
            "culture definition."
        )

    if culture_id not in index.cultures:
        new_block = (
            f"{culture_id} = {{\n"
            f"\t# "
            f"lok_culture_painter_source_color "
            f"= {source_colour}\n"
            f"}}"
        )

        insert_culture(
            mod_root,
            group_id,
            new_block,
        )

    else:
        old = index.cultures[
            culture_id
        ]

        if old.group_id != group_id:
            extracted = remove_culture(
                old
            )

            extracted = (
                CULTURE_COLOUR_COMMENT.sub(
                    "",
                    extracted,
                    count=1,
                )
            )

            insert_culture(
                mod_root,
                group_id,
                extracted.strip(),
            )

    index = scan_cultures(mod_root)

    culture = index.cultures[
        culture_id
    ]

    text, encoding = read_text(
        culture.path
    )

    updated = add_or_update_comment(
        text=text,
        block=culture.block,
        regex=CULTURE_COLOUR_COMMENT,
        comment_name=(
            "lok_culture_painter_source_color"
        ),
        colour=source_colour,
    )

    if updated != text:
        write_text(
            culture.path,
            updated,
            encoding,
        )


# =============================================================================
# MAP / PROVINCE MATCHING
# =============================================================================


def parse_definition(
    path: Path,
) -> Dict[int, int]:

    data = path.read_bytes()

    decoded = None

    for encoding in (
        "utf-8-sig",
        "cp1252",
        "latin-1",
    ):
        try:
            decoded = data.decode(
                encoding
            )
            break
        except UnicodeDecodeError:
            pass

    if decoded is None:
        decoded = data.decode(
            "latin-1",
            errors="replace",
        )

    mapping: Dict[int, int] = {}

    reader = csv.reader(
        decoded.splitlines(),
        delimiter=";",
    )

    for row in reader:
        if len(row) < 4:
            continue

        try:
            province = int(
                row[0].strip()
            )

            red = int(row[1])
            green = int(row[2])
            blue = int(row[3])

        except ValueError:
            continue

        mapping[
            rgb_to_code(
                (red, green, blue)
            )
        ] = province

    return mapping


def image_codes(
    path: Path,
) -> np.ndarray:

    with Image.open(path) as image:
        image = image.convert("RGB")

        array = np.asarray(
            image,
            dtype=np.uint32,
        )

    return (
        (array[:, :, 0] << 16)
        | (array[:, :, 1] << 8)
        | array[:, :, 2]
    )


def analyse_overlay(
    mod_root: Path,
    overlay: Path,
) -> Tuple[
    List[PaintRegion],
    List[str],
]:

    province_path = (
        mod_root
        / "map"
        / "provinces.bmp"
    )

    definition_path = (
        mod_root
        / "map"
        / "definition.csv"
    )

    definition = parse_definition(
        definition_path
    )

    provinces = image_codes(
        province_path
    )

    painted = image_codes(
        overlay
    )

    if provinces.shape != painted.shape:
        raise RuntimeError(
            "Paint map dimensions "
            f"{painted.shape[1]}×"
            f"{painted.shape[0]} "
            "do not match provinces.bmp "
            f"{provinces.shape[1]}×"
            f"{provinces.shape[0]}."
        )

    pairs = (
        provinces.astype(np.uint64)
        << np.uint64(24)
    ) | painted.astype(np.uint64)

    unique, counts = np.unique(
        pairs,
        return_counts=True,
    )

    del pairs

    best: Dict[
        int,
        Tuple[int, int]
    ] = {}

    totals: Dict[
        int,
        int
    ] = {}

    for packed, count in zip(
        unique.tolist(),
        counts.tolist(),
    ):
        province_colour = int(
            packed >> 24
        )

        paint_colour = int(
            packed & 0xFFFFFF
        )

        province_id = definition.get(
            province_colour
        )

        if province_id is None:
            continue

        count = int(count)

        totals[province_id] = (
            totals.get(
                province_id,
                0,
            )
            + count
        )

        previous = best.get(
            province_id
        )

        if (
            previous is None
            or count > previous[1]
        ):
            best[province_id] = (
                paint_colour,
                count,
            )

    by_colour: Dict[
        int,
        List[int]
    ] = {}

    pixels: Dict[
        int,
        int
    ] = {}

    warnings: List[str] = []

    for province_id, (
        paint_colour,
        dominant_pixels,
    ) in best.items():

        if paint_colour in IGNORED_COLOURS:
            continue

        by_colour.setdefault(
            paint_colour,
            [],
        ).append(
            province_id
        )

        pixels[paint_colour] = (
            pixels.get(
                paint_colour,
                0,
            )
            + dominant_pixels
        )

        total = totals[
            province_id
        ]

        dominance = (
            dominant_pixels
            / total
        )

        if dominance < 0.98:
            warnings.append(
                f"Province {province_id}: "
                f"{rgb_to_hex(code_to_rgb(paint_colour))} "
                f"is only "
                f"{dominance:.1%} of its pixels."
            )

    regions: List[
        PaintRegion
    ] = []

    for colour, province_ids in (
        by_colour.items()
    ):
        rgb = code_to_rgb(
            colour
        )

        regions.append(
            PaintRegion(
                code=colour,
                rgb=rgb,
                hex_colour=rgb_to_hex(
                    rgb
                ),
                provinces=sorted(
                    province_ids
                ),
                pixels=pixels.get(
                    colour,
                    0,
                ),
            )
        )

    regions.sort(
        key=lambda r: (
            -len(r.provinces),
            -r.pixels,
            r.code,
        )
    )

    return regions, warnings


# =============================================================================
# PROVINCE HISTORY
# =============================================================================


def province_history_index(
    mod_root: Path,
) -> Dict[int, Path]:

    directory = (
        mod_root
        / "history"
        / "provinces"
    )

    output: Dict[
        int,
        Path
    ] = {}

    if not directory.exists():
        return output

    for path in directory.glob(
        "*.txt"
    ):
        match = re.match(
            r"^(\d+)\b",
            path.stem,
        )

        if match:
            output[
                int(match.group(1))
            ] = path

    return output


def line_depths(
    text: str,
) -> List[int]:

    lines = text.splitlines(
        keepends=True
    )

    output: List[int] = []

    depth = 0
    string = False
    escaped = False
    comment = False

    for line in lines:
        output.append(depth)

        for char in line:
            if comment:
                if char == "\n":
                    comment = False

            elif string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    string = False

            else:
                if char == "#":
                    comment = True

                elif char == '"':
                    string = True

                elif char == "{":
                    depth += 1

                elif char == "}":
                    depth = max(
                        0,
                        depth - 1,
                    )

        comment = False

    return output


def set_province_culture(
    path: Path,
    culture_id: str,
) -> bool:

    text, encoding = read_text(
        path
    )

    lines = text.splitlines(
        keepends=True
    )

    depths = line_depths(text)

    pattern = re.compile(
        r"^"
        r"(?P<indent>\s*)"
        r"culture\s*=\s*"
        r"(?P<value>[^#\r\n]*?)"
        r"(?P<comment>\s*#.*)?"
        r"(?P<newline>\r?\n)?"
        r"$"
    )

    for i, (
        line,
        depth,
    ) in enumerate(
        zip(lines, depths)
    ):
        if depth != 0:
            continue

        match = pattern.match(
            line
        )

        if not match:
            continue

        indent = (
            match.group("indent")
            or ""
        )

        comment = (
            match.group("comment")
            or ""
        )

        newline = (
            match.group("newline")
            or (
                "\n"
                if line.endswith("\n")
                else ""
            )
        )

        new_line = (
            f"{indent}"
            f"culture = {culture_id}"
            f"{comment}"
            f"{newline}"
        )

        if new_line == line:
            return False

        lines[i] = new_line

        write_text(
            path,
            "".join(lines),
            encoding,
        )

        return True

    insert_at = 0

    for i, line in enumerate(
        lines
    ):
        stripped = line.strip()

        if (
            not stripped
            or stripped.startswith("#")
        ):
            insert_at = i + 1
        else:
            break

    lines.insert(
        insert_at,
        f"culture = {culture_id}\n",
    )

    write_text(
        path,
        "".join(lines),
        encoding,
    )

    return True


# =============================================================================
# LOCALISATION
# =============================================================================


def update_localisation(
    mod_root: Path,
    cultures: Dict[str, str],
    groups: Dict[str, str],
) -> Path:

    directory = (
        mod_root
        / "localisation"
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = (
        directory
        / GENERATED_LOCALISATION
    )

    existing: Dict[
        str,
        str
    ] = {}

    if path.exists():
        text, _encoding = read_text(
            path
        )

        for line in text.splitlines():
            match = re.match(
                r'^\s*'
                r'([A-Za-z0-9_.:-]+)'
                r':(?:\d+)?\s+'
                r'"(.*)"\s*$',
                line,
            )

            if match:
                existing[
                    match.group(1)
                ] = match.group(2)

    for identifier, name in (
        cultures.items()
    ):
        existing[
            identifier
        ] = (
            name
            .replace("\\", "\\\\")
            .replace('"', '\\"')
        )

    for identifier, name in (
        groups.items()
    ):
        existing[
            identifier
        ] = (
            name
            .replace("\\", "\\\\")
            .replace('"', '\\"')
        )

    lines = ["l_english:"]

    for key in sorted(
        existing,
        key=str.lower,
    ):
        lines.append(
            f' {key}:0 '
            f'"{existing[key]}"'
        )

    path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8-sig",
    )

    return path


# =============================================================================
# BACKUPS
# =============================================================================


def make_backup(
    mod_root: Path,
    paths: List[Path],
) -> Path:

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    root = (
        mod_root
        / BACKUP_DIRECTORY
        / timestamp
    )

    copied = set()

    for path in paths:
        if not path.exists():
            continue

        resolved = path.resolve()

        if resolved in copied:
            continue

        copied.add(resolved)

        try:
            relative = resolved.relative_to(
                mod_root.resolve()
            )
        except ValueError:
            continue

        destination = (
            root
            / relative
        )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copy2(
            resolved,
            destination,
        )

    return root


# =============================================================================
# GUI
# =============================================================================


class CulturePainter:
    def __init__(
        self,
        root: tk.Tk,
        mod_root: Path,
    ):
        self.root = root
        self.mod_root = mod_root

        self.index = scan_cultures(
            mod_root
        )

        self.state = load_state(
            mod_root
        )

        self.regions: List[
            PaintRegion
        ] = []

        self.choices: Dict[
            int,
            Choice
        ] = {}

        self.overlay_path: Optional[
            Path
        ] = None

        self.map_warnings: List[
            str
        ] = []

        self.position = 0

        self.culture_groups = {
            culture_id:
            culture.group_id
            for (
                culture_id,
                culture,
            ) in self.index.cultures.items()
        }

        self.group_colours: Dict[
            str,
            str
        ] = {}

        for (
            group_id,
            group,
        ) in self.index.groups.items():

            colour = (
                group.colour
                or self.state[
                    "group_colours"
                ].get(group_id)
            )

            if colour:
                try:
                    colour = parse_colour(
                        str(colour)
                    )
                except ValueError:
                    colour = (
                        deterministic_group_colour(
                            group_id
                        )
                    )
            else:
                colour = (
                    deterministic_group_colour(
                        group_id
                    )
                )

            self.group_colours[
                group_id
            ] = colour

        self.culture_names = {
            culture_id:
            pretty_name(culture_id)
            for culture_id
            in self.index.cultures
        }

        self.group_names = {
            group_id:
            pretty_name(group_id)
            for group_id
            in self.index.groups
        }

        self.build_window()
        self.show_start()

    # -------------------------------------------------------------------------
    # BASE WINDOW
    # -------------------------------------------------------------------------

    def build_window(self):
        self.root.title(
            APP_TITLE
        )

        self.root.geometry(
            "900x650"
        )

        self.root.minsize(
            780,
            570,
        )

        self.main = ttk.Frame(
            self.root,
            padding=16,
        )

        self.main.pack(
            fill="both",
            expand=True,
        )

        self.title_var = tk.StringVar(
            value=APP_TITLE
        )

        ttk.Label(
            self.main,
            textvariable=self.title_var,
            font=(
                "Segoe UI",
                18,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        self.subtitle_var = (
            tk.StringVar()
        )

        ttk.Label(
            self.main,
            textvariable=self.subtitle_var,
        ).pack(
            anchor="w",
            pady=(2, 12),
        )

        self.content = ttk.Frame(
            self.main
        )

        self.content.pack(
            fill="both",
            expand=True,
        )

        ttk.Separator(
            self.main
        ).pack(
            fill="x",
            pady=(12, 8),
        )

        self.status_var = (
            tk.StringVar()
        )

        ttk.Label(
            self.main,
            textvariable=self.status_var,
        ).pack(
            fill="x"
        )

    def clear(self):
        for widget in (
            self.content
            .winfo_children()
        ):
            widget.destroy()

    # -------------------------------------------------------------------------
    # START SCREEN
    # -------------------------------------------------------------------------

    def show_start(self):
        self.clear()

        self.title_var.set(
            APP_TITLE
        )

        self.subtitle_var.set(
            f"Mod root: "
            f"{self.mod_root}"
        )

        ttk.Label(
            self.content,
            justify="left",
            text=(
                "Choose your painted "
                "culture map.\n\n"
                "Each unique RGB colour "
                "will be processed once. "
                "Pure black and pure white "
                "are ignored."
            ),
        ).pack(
            anchor="w"
        )

        buttons = ttk.Frame(
            self.content
        )

        buttons.pack(
            anchor="w",
            pady=16,
        )

        ttk.Button(
            buttons,
            text="Choose painted map…",
            command=self.choose_map,
        ).pack(
            side="left"
        )

        ttk.Button(
            buttons,
            text="Rescan cultures",
            command=self.rescan,
        ).pack(
            side="left",
            padx=(8, 0),
        )

        ttk.Label(
            self.content,
            wraplength=800,
            text=(
                "The group colour is "
                "persistent painter "
                "metadata. EU4's "
                "common/cultures format "
                "does not itself contain "
                "a valid group RGB field."
            ),
        ).pack(
            anchor="w",
            pady=(10, 0),
        )

        self.status_var.set(
            f"Found "
            f"{len(self.index.groups)} "
            f"culture groups and "
            f"{len(self.index.cultures)} "
            f"cultures."
        )

    def rescan(self):
        self.index = scan_cultures(
            self.mod_root
        )

        self.state = load_state(
            self.mod_root
        )

        self.culture_groups = {
            culture_id:
            record.group_id
            for culture_id, record
            in self.index.cultures.items()
        }

        # Rebuild the persistent group colour list only from real groups/state.
        self.group_colours = {}
        for group_id, record in self.index.groups.items():
            colour = (
                record.colour
                or self.state.get("group_colours", {}).get(group_id)
                or deterministic_group_colour(group_id)
            )
            try:
                colour = parse_colour(str(colour))
            except ValueError:
                colour = deterministic_group_colour(group_id)
            self.group_colours[group_id] = colour

        self.show_start()

    def choose_map(self):
        filename = (
            filedialog
            .askopenfilename(
                title=(
                    "Choose painted "
                    "culture map"
                ),
                initialdir=str(
                    self.mod_root
                ),
                filetypes=[
                    (
                        "Lossless images",
                        "*.png *.bmp "
                        "*.tga *.tif *.tiff",
                    ),
                    ("PNG", "*.png"),
                    ("Bitmap", "*.bmp"),
                    ("All files", "*.*"),
                ],
            )
        )

        if not filename:
            return

        path = Path(filename)

        if path.suffix.lower() in {
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            messagebox.showerror(
                APP_TITLE,
                "Use a lossless image "
                "(PNG/BMP/TGA/TIFF). "
                "JPEG/WebP can introduce "
                "extra colours.",
            )
            return

        self.status_var.set(
            "Analysing map…"
        )

        self.root.update_idletasks()

        try:
            (
                self.regions,
                self.map_warnings,
            ) = analyse_overlay(
                self.mod_root,
                path,
            )

        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                str(exc),
            )
            return

        if not self.regions:
            messagebox.showwarning(
                APP_TITLE,
                "No usable painted "
                "regions were found.",
            )
            return

        self.overlay_path = path

        self.position = 0
        self.choices = {}

        self.load_previous_choices()

        self.show_editor()

    # -------------------------------------------------------------------------
    # SAVED SESSION
    # -------------------------------------------------------------------------

    def load_previous_choices(self):
        saved = self.state.get(
            "paint_assignments",
            {},
        )

        for region in self.regions:
            record = saved.get(
                region.hex_colour
            )

            if not isinstance(
                record,
                dict,
            ):
                continue

            if record.get(
                "skipped"
            ):
                self.choices[
                    region.code
                ] = Choice(
                    skipped=True
                )
                continue

            culture = str(
                record.get(
                    "culture_id",
                    "",
                )
            )

            group = str(
                record.get(
                    "group_id",
                    "",
                )
            )

            if not culture or not group:
                continue

            colour = record.get(
                "group_colour"
            )

            try:
                colour = parse_colour(
                    str(
                        colour
                        or self.group_colours.get(
                            group
                        )
                        or deterministic_group_colour(
                            group
                        )
                    )
                )

            except ValueError:
                colour = (
                    deterministic_group_colour(
                        group
                    )
                )

            # Loading a previously COMMITTED choice is allowed to persist it.
            self.group_colours[
                group
            ] = colour

            self.culture_groups[
                culture
            ] = group

            self.choices[
                region.code
            ] = Choice(
                culture_id=culture,
                culture_name=str(
                    record.get(
                        "culture_name",
                        pretty_name(
                            culture
                        ),
                    )
                ),
                group_id=group,
                group_name=str(
                    record.get(
                        "group_name",
                        pretty_name(group),
                    )
                ),
                group_colour=colour,
            )

    def persist_choices(self):
        state = load_state(
            self.mod_root
        )

        # Only committed groups exist in self.group_colours.
        state[
            "group_colours"
        ].update(
            self.group_colours
        )

        assignments = state[
            "paint_assignments"
        ]

        for region in self.regions:
            choice = self.choices.get(
                region.code
            )

            if choice is None:
                continue

            if choice.skipped:
                assignments[
                    region.hex_colour
                ] = {
                    "skipped": True
                }

            else:
                assignments[
                    region.hex_colour
                ] = {
                    "culture_id":
                    choice.culture_id,

                    "culture_name":
                    choice.culture_name,

                    "group_id":
                    choice.group_id,

                    "group_name":
                    choice.group_name,

                    "group_colour":
                    choice.group_colour,

                    "skipped": False,
                }

                state[
                    "culture_source_colours"
                ][choice.culture_id] = (
                    region.hex_colour
                )

        save_state(
            self.mod_root,
            state,
        )

        self.state = state

    # -------------------------------------------------------------------------
    # EDITOR
    # -------------------------------------------------------------------------

    def show_editor(self):
        self.clear()

        self.title_var.set(
            "Paint cultures"
        )

        self.subtitle_var.set(
            str(self.overlay_path)
        )

        progress_row = ttk.Frame(
            self.content
        )

        progress_row.pack(
            fill="x"
        )

        self.progress_var = (
            tk.DoubleVar()
        )

        self.progress = (
            ttk.Progressbar(
                progress_row,
                variable=self.progress_var,
                maximum=len(
                    self.regions
                ),
            )
        )

        self.progress.pack(
            side="left",
            fill="x",
            expand=True,
        )

        self.progress_text = (
            tk.StringVar()
        )

        ttk.Label(
            progress_row,
            textvariable=(
                self.progress_text
            ),
            width=14,
            anchor="e",
        ).pack(
            side="right",
            padx=(8, 0),
        )

        grid = ttk.Frame(
            self.content
        )

        grid.pack(
            fill="both",
            expand=True,
            pady=(14, 0),
        )

        grid.columnconfigure(
            1,
            weight=1,
        )

        ttk.Label(
            grid,
            text="Paint colour",
            font=(
                "Segoe UI",
                10,
                "bold",
            ),
        ).grid(
            row=0,
            column=0,
            sticky="nw",
            pady=8,
        )

        swatch_row = ttk.Frame(
            grid
        )

        swatch_row.grid(
            row=0,
            column=1,
            sticky="w",
            pady=8,
        )

        self.paint_swatch = (
            tk.Canvas(
                swatch_row,
                width=110,
                height=58,
                highlightthickness=1,
            )
        )

        self.paint_swatch.pack(
            side="left"
        )

        info = ttk.Frame(
            swatch_row
        )

        info.pack(
            side="left",
            padx=(12, 0),
        )

        self.paint_hex = (
            tk.StringVar()
        )

        self.paint_rgb = (
            tk.StringVar()
        )

        self.paint_provinces = (
            tk.StringVar()
        )

        ttk.Label(
            info,
            textvariable=self.paint_hex,
            font=(
                "Consolas",
                12,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        ttk.Label(
            info,
            textvariable=self.paint_rgb,
        ).pack(
            anchor="w"
        )

        ttk.Label(
            info,
            textvariable=(
                self.paint_provinces
            ),
        ).pack(
            anchor="w"
        )

        ttk.Label(
            grid,
            text="Culture",
            font=(
                "Segoe UI",
                10,
                "bold",
            ),
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=8,
        )

        culture_frame = ttk.Frame(
            grid
        )

        culture_frame.grid(
            row=1,
            column=1,
            sticky="ew",
            pady=8,
        )

        culture_frame.columnconfigure(
            0,
            weight=1,
        )

        self.culture_var = (
            tk.StringVar()
        )

        self.culture_box = (
            ttk.Combobox(
                culture_frame,
                textvariable=(
                    self.culture_var
                ),
                state="normal",
            )
        )

        self.culture_box.grid(
            row=0,
            column=0,
            sticky="ew",
        )

        self.culture_box.bind(
            "<<ComboboxSelected>>",
            self.culture_selected,
        )

        self.culture_box.bind(
            "<KeyRelease>",
            self.culture_typed,
        )

        self.culture_id_label = (
            tk.StringVar()
        )

        ttk.Label(
            culture_frame,
            textvariable=(
                self.culture_id_label
            ),
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(3, 0),
        )

        ttk.Label(
            grid,
            text="Culture group",
            font=(
                "Segoe UI",
                10,
                "bold",
            ),
        ).grid(
            row=2,
            column=0,
            sticky="w",
            pady=8,
        )

        group_frame = ttk.Frame(
            grid
        )

        group_frame.grid(
            row=2,
            column=1,
            sticky="ew",
            pady=8,
        )

        group_frame.columnconfigure(
            0,
            weight=1,
        )

        self.group_var = (
            tk.StringVar()
        )

        self.group_box = (
            ttk.Combobox(
                group_frame,
                textvariable=(
                    self.group_var
                ),
                state="normal",
            )
        )

        self.group_box.grid(
            row=0,
            column=0,
            sticky="ew",
        )

        # FIX: selecting an existing group may load its colour,
        # but typing does NOT create or persist intermediate IDs.
        self.group_box.bind(
            "<<ComboboxSelected>>",
            self.group_selected,
        )

        self.group_box.bind(
            "<KeyRelease>",
            self.group_typed,
        )

        self.group_id_label = (
            tk.StringVar()
        )

        ttk.Label(
            group_frame,
            textvariable=(
                self.group_id_label
            ),
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(3, 0),
        )

        ttk.Label(
            grid,
            text="Group colour",
            font=(
                "Segoe UI",
                10,
                "bold",
            ),
        ).grid(
            row=3,
            column=0,
            sticky="w",
            pady=8,
        )

        colour_row = ttk.Frame(
            grid
        )

        colour_row.grid(
            row=3,
            column=1,
            sticky="w",
            pady=8,
        )

        self.group_swatch = (
            tk.Canvas(
                colour_row,
                width=58,
                height=32,
                highlightthickness=1,
            )
        )

        self.group_swatch.pack(
            side="left"
        )

        self.group_colour_var = (
            tk.StringVar()
        )

        self.group_colour_entry = (
            ttk.Entry(
                colour_row,
                textvariable=(
                    self.group_colour_var
                ),
                width=18,
            )
        )

        self.group_colour_entry.pack(
            side="left",
            padx=(10, 6),
        )

        self.group_colour_entry.bind(
            "<KeyRelease>",
            lambda _e:
            self.refresh_group_swatch(),
        )

        ttk.Button(
            colour_row,
            text="Pick…",
            command=self.pick_colour,
        ).pack(
            side="left"
        )

        ttk.Label(
            colour_row,
            text=(
                "#RRGGBB or "
                "R,G,B"
            ),
        ).pack(
            side="left",
            padx=(10, 0),
        )

        navigation = ttk.Frame(
            self.content
        )

        navigation.pack(
            fill="x",
            pady=(14, 0),
        )

        self.back_button = (
            ttk.Button(
                navigation,
                text="← Back",
                command=self.back,
            )
        )

        self.back_button.pack(
            side="left"
        )

        ttk.Button(
            navigation,
            text="Skip colour",
            command=self.skip,
        ).pack(
            side="left",
            padx=(8, 0),
        )

        ttk.Button(
            navigation,
            text="Save & Next →",
            command=self.save_next,
        ).pack(
            side="left",
            padx=(8, 0),
        )

        ttk.Button(
            navigation,
            text="Finish / Apply",
            command=self.finish,
        ).pack(
            side="right"
        )

        self.refresh_dropdowns()
        self.load_region()

    def refresh_dropdowns(self):
        cultures = sorted(
            set(
                self.index.cultures
            )
            | set(
                self.culture_groups
            ),
            key=str.lower,
        )

        groups = sorted(
            set(
                self.index.groups
            )
            | set(
                self.group_colours
            ),
            key=str.lower,
        )

        self.culture_box[
            "values"
        ] = cultures

        self.group_box[
            "values"
        ] = groups

    def current_region(self):
        return self.regions[
            self.position
        ]

    def resolve_existing(
        self,
        raw: str,
        possibilities,
    ) -> str:

        raw = raw.strip()

        if raw in possibilities:
            return raw

        lower = {
            item.lower(): item
            for item
            in possibilities
        }

        if raw.lower() in lower:
            return lower[
                raw.lower()
            ]

        return safe_id(raw)

    def culture_id(self):
        return self.resolve_existing(
            self.culture_var.get(),
            set(
                self.index.cultures
            )
            | set(
                self.culture_groups
            ),
        )

    def group_id(self):
        return self.resolve_existing(
            self.group_var.get(),
            set(
                self.index.groups
            )
            | set(
                self.group_colours
            ),
        )

    def update_id_labels(self):
        if self.culture_var.get().strip():
            self.culture_id_label.set(
                "Internal ID: "
                + self.culture_id()
            )
        else:
            self.culture_id_label.set(
                ""
            )

        if self.group_var.get().strip():
            self.group_id_label.set(
                "Internal ID: "
                + self.group_id()
            )
        else:
            self.group_id_label.set(
                ""
            )

    def load_region(self):
        region = self.current_region()

        self.progress_var.set(
            self.position + 1
        )

        self.progress_text.set(
            f"{self.position + 1} / "
            f"{len(self.regions)}"
        )

        self.paint_swatch.delete(
            "all"
        )

        self.paint_swatch.create_rectangle(
            0,
            0,
            110,
            58,
            fill=region.hex_colour,
            outline="",
        )

        self.paint_hex.set(
            region.hex_colour
        )

        self.paint_rgb.set(
            "RGB "
            f"{region.rgb[0]}, "
            f"{region.rgb[1]}, "
            f"{region.rgb[2]}"
        )

        self.paint_provinces.set(
            f"{len(region.provinces)} "
            "provinces"
        )

        choice = self.choices.get(
            region.code
        )

        if (
            choice
            and not choice.skipped
        ):
            self.culture_var.set(
                choice.culture_id
            )

            self.group_var.set(
                choice.group_id
            )

            self.group_colour_var.set(
                choice.group_colour
            )

        else:
            self.culture_var.set(
                ""
            )

            self.group_var.set(
                ""
            )

            self.group_colour_var.set(
                region.hex_colour
            )

        self.update_id_labels()
        self.refresh_group_swatch()

        self.back_button.configure(
            state=(
                "normal"
                if self.position > 0
                else "disabled"
            )
        )

        self.status_var.set(
            f"{region.hex_colour}: "
            f"{len(region.provinces)} "
            f"province(s)."
        )

    def culture_selected(
        self,
        _event=None,
    ):
        culture_id = (
            self.culture_id()
        )

        group = (
            self.culture_groups.get(
                culture_id
            )
        )

        if (
            group is None
            and culture_id
            in self.index.cultures
        ):
            group = (
                self.index.cultures[
                    culture_id
                ].group_id
            )

        if group:
            self.group_var.set(
                group
            )

            self.load_group_colour(
                group
            )

        self.update_id_labels()

    def culture_typed(
        self,
        _event=None,
    ):
        self.update_id_labels()

        if not self.culture_var.get().strip():
            return

        culture_id = (
            self.culture_id()
        )

        group = self.culture_groups.get(
            culture_id
        )

        if group:
            self.group_var.set(
                group
            )

            self.load_group_colour(
                group
            )

            self.update_id_labels()

    def group_selected(self, _event=None):
        """
        Selecting an existing group may load its saved painter colour.
        This does not create new groups.
        """
        self.update_id_labels()

        raw = self.group_var.get().strip()
        if not raw:
            return

        group = self.group_id()

        if group in self.group_colours:
            self.group_colour_var.set(
                self.group_colours[group]
            )
        elif group in self.index.groups:
            self.group_colour_var.set(
                deterministic_group_colour(group)
            )

        self.refresh_group_swatch()

    def group_typed(self, _event=None):
        """
        Typing only updates the internal-ID preview.
        It NEVER creates or saves a culture group.
        """
        self.update_id_labels()

    def load_group_colour(
        self,
        group: str,
    ):
        """
        Display a group's colour without creating a new group record.
        """
        if group in self.group_colours:
            colour = self.group_colours[group]

        elif group in self.index.groups:
            colour = deterministic_group_colour(group)

        else:
            colour = self.current_region().hex_colour

        self.group_colour_var.set(
            colour
        )

        self.refresh_group_swatch()

    def refresh_group_swatch(self):
        try:
            colour = parse_colour(
                self.group_colour_var.get()
            )
        except ValueError:
            colour = "#808080"

        self.group_swatch.delete(
            "all"
        )

        self.group_swatch.create_rectangle(
            0,
            0,
            58,
            32,
            fill=colour,
            outline="",
        )

    def pick_colour(self):
        try:
            initial = parse_colour(
                self.group_colour_var.get()
            )
        except ValueError:
            initial = None

        selected = (
            colorchooser.askcolor(
                color=initial,
                title=(
                    "Culture-group colour"
                ),
            )
        )

        if (
            selected
            and selected[1]
        ):
            self.group_colour_var.set(
                selected[1].upper()
            )

            self.refresh_group_swatch()

    # -------------------------------------------------------------------------
    # CHOICES
    # -------------------------------------------------------------------------

    def capture(self) -> Optional[Choice]:
        culture_raw = self.culture_var.get().strip()
        group_raw = self.group_var.get().strip()

        if not culture_raw:
            messagebox.showerror(
                APP_TITLE,
                "Enter or select a culture.",
            )
            return None

        if not group_raw:
            messagebox.showerror(
                APP_TITLE,
                "Enter or select a culture group.",
            )
            return None

        culture_id = self.culture_id()
        group_id = self.group_id()

        try:
            group_colour = parse_colour(
                self.group_colour_var.get()
            )
        except ValueError as exc:
            messagebox.showerror(
                APP_TITLE,
                str(exc),
            )
            return None

        if culture_id in self.index.cultures:
            culture_name = self.culture_names.get(
                culture_id,
                pretty_name(culture_id),
            )
        else:
            culture_name = culture_raw

        if group_id in self.index.groups:
            group_name = self.group_names.get(
                group_id,
                pretty_name(group_id),
            )
        else:
            group_name = group_raw

        # FIX: New groups/cultures become persistent ONLY when the user
        # commits the current choice with Save & Next / Finish.
        self.group_colours[group_id] = group_colour
        self.culture_groups[culture_id] = group_id

        self.culture_names[culture_id] = culture_name
        self.group_names[group_id] = group_name

        self.refresh_dropdowns()

        return Choice(
            culture_id=culture_id,
            culture_name=culture_name,
            group_id=group_id,
            group_name=group_name,
            group_colour=group_colour,
        )

    def save_next(self):
        choice = self.capture()

        if choice is None:
            return

        self.choices[
            self.current_region().code
        ] = choice

        self.persist_choices()

        if (
            self.position
            < len(self.regions) - 1
        ):
            self.position += 1
            self.load_region()

        else:
            self.status_var.set(
                "Last colour reached. "
                "Press Finish / Apply."
            )

    def skip(self):
        self.choices[
            self.current_region().code
        ] = Choice(
            skipped=True
        )

        self.persist_choices()

        if (
            self.position
            < len(self.regions) - 1
        ):
            self.position += 1

        self.load_region()

    def back(self):
        if self.position > 0:
            self.position -= 1
            self.load_region()

    # -------------------------------------------------------------------------
    # APPLY
    # -------------------------------------------------------------------------

    def finish(self):
        if (
            self.culture_var.get().strip()
            or self.group_var.get().strip()
        ):
            choice = self.capture()

            if choice is None:
                return

            self.choices[
                self.current_region().code
            ] = choice

        unchosen = [
            region
            for region in self.regions
            if region.code
            not in self.choices
        ]

        if unchosen:
            answer = (
                messagebox.askyesno(
                    APP_TITLE,
                    f"{len(unchosen)} colour(s) "
                    "have no assignment. "
                    "They will be left "
                    "untouched.\n\n"
                    "Continue?",
                )
            )

            if not answer:
                return

        active = [
            (
                region,
                self.choices[
                    region.code
                ],
            )
            for region in self.regions
            if (
                region.code
                in self.choices
                and not self.choices[
                    region.code
                ].skipped
            )
        ]

        if not active:
            messagebox.showwarning(
                APP_TITLE,
                "Nothing to apply.",
            )
            return

        assigned_groups = {}

        conflicts = []

        for _region, choice in active:
            previous = (
                assigned_groups.get(
                    choice.culture_id
                )
            )

            if (
                previous
                and previous
                != choice.group_id
            ):
                conflicts.append(
                    f"{choice.culture_id}: "
                    f"{previous} / "
                    f"{choice.group_id}"
                )

            assigned_groups[
                choice.culture_id
            ] = choice.group_id

        if conflicts:
            messagebox.showerror(
                APP_TITLE,
                "The same culture was "
                "assigned to multiple "
                "culture groups:\n\n"
                + "\n".join(
                    conflicts
                ),
            )
            return

        try:
            self.apply(active)

        except Exception as exc:
            log = (
                self.mod_root
                / "culture_painter_error.log"
            )

            log.write_text(
                traceback.format_exc(),
                encoding="utf-8",
            )

            messagebox.showerror(
                APP_TITLE,
                "Apply failed:\n\n"
                f"{exc}\n\n"
                "A traceback was written "
                "to culture_painter_error.log.",
            )

    def apply(
        self,
        active,
    ):
        province_files = (
            province_history_index(
                self.mod_root
            )
        )

        before = scan_cultures(
            self.mod_root
        )

        backup_candidates = []

        for region, choice in active:
            if (
                choice.culture_id
                in before.cultures
            ):
                backup_candidates.append(
                    before.cultures[
                        choice.culture_id
                    ].path
                )

            if (
                choice.group_id
                in before.groups
            ):
                backup_candidates.append(
                    before.groups[
                        choice.group_id
                    ].path
                )

            for province in (
                region.provinces
            ):
                path = province_files.get(
                    province
                )

                if path:
                    backup_candidates.append(
                        path
                    )

        backup_candidates.extend([
            generated_culture_path(
                self.mod_root
            ),
            self.mod_root
            / "localisation"
            / GENERATED_LOCALISATION,
            self.mod_root
            / STATE_FILENAME,
        ])

        backup_root = make_backup(
            self.mod_root,
            backup_candidates,
        )

        total = (
            len(
                {
                    choice.culture_id
                    for _, choice
                    in active
                }
            )
            + sum(
                len(region.provinces)
                for region, _choice
                in active
            )
        )

        progress_window = (
            tk.Toplevel(
                self.root
            )
        )

        progress_window.title(
            "Applying cultures"
        )

        progress_window.geometry(
            "540x130"
        )

        progress_label = (
            tk.StringVar(
                value="Starting…"
            )
        )

        ttk.Label(
            progress_window,
            textvariable=(
                progress_label
            ),
            padding=12,
        ).pack(
            fill="x"
        )

        progress = ttk.Progressbar(
            progress_window,
            maximum=max(1, total),
        )

        progress.pack(
            fill="x",
            padx=12,
            pady=6,
        )

        progress_window.transient(
            self.root
        )

        progress_window.grab_set()

        done = 0

        try:
            per_culture = {}

            for region, choice in active:
                per_culture[
                    choice.culture_id
                ] = (
                    region,
                    choice,
                )

            for (
                culture_id,
                (
                    region,
                    choice,
                ),
            ) in per_culture.items():

                progress_label.set(
                    f"Culture "
                    f"{culture_id} → "
                    f"{choice.group_id}"
                )

                ensure_culture(
                    mod_root=self.mod_root,
                    culture_id=culture_id,
                    group_id=choice.group_id,
                    source_colour=(
                        region.hex_colour
                    ),
                    group_colour=(
                        choice.group_colour
                    ),
                )

                done += 1
                progress["value"] = done

                self.root.update_idletasks()

            for _region, choice in active:
                ensure_group(
                    self.mod_root,
                    choice.group_id,
                    choice.group_colour,
                )

            changed = 0
            missing = []

            for region, choice in active:
                for province in (
                    region.provinces
                ):
                    progress_label.set(
                        f"Province "
                        f"{province}: "
                        f"{choice.culture_id}"
                    )

                    path = (
                        province_files.get(
                            province
                        )
                    )

                    if path is None:
                        missing.append(
                            province
                        )

                    else:
                        if set_province_culture(
                            path,
                            choice.culture_id,
                        ):
                            changed += 1

                    done += 1
                    progress["value"] = done

                    if done % 25 == 0:
                        self.root.update_idletasks()

            culture_names = {
                choice.culture_id:
                choice.culture_name
                for _region, choice
                in active
            }

            group_names = {
                choice.group_id:
                choice.group_name
                for _region, choice
                in active
            }

            localisation = (
                update_localisation(
                    self.mod_root,
                    culture_names,
                    group_names,
                )
            )

            state = load_state(
                self.mod_root
            )

            for region, choice in active:
                state[
                    "group_colours"
                ][choice.group_id] = (
                    choice.group_colour
                )

                state[
                    "culture_source_colours"
                ][choice.culture_id] = (
                    region.hex_colour
                )

            state["last_overlay"] = (
                str(self.overlay_path)
            )

            state["last_applied"] = (
                datetime.now()
                .isoformat(
                    timespec="seconds"
                )
            )

            save_state(
                self.mod_root,
                state,
            )

            self.persist_choices()

        finally:
            try:
                progress_window.grab_release()
            except Exception:
                pass

            progress_window.destroy()

        if self.map_warnings:
            warning_file = (
                self.mod_root
                / "culture_painter_map_warnings.txt"
            )

            warning_file.write_text(
                "\n".join(
                    self.map_warnings
                ) + "\n",
                encoding="utf-8",
            )

        message = (
            f"Applied "
            f"{len(active)} "
            f"paint colours.\n\n"
            f"Province history files "
            f"changed: {changed}\n"
            f"Localisation: "
            f"{localisation.relative_to(self.mod_root)}\n"
        )

        if backup_root.exists():
            message += (
                f"Backup: "
                f"{backup_root.relative_to(self.mod_root)}\n"
            )

        if missing:
            message += (
                f"\nNo history file was "
                f"found for "
                f"{len(missing)} "
                f"province ID(s):\n"
                + ", ".join(
                    str(x)
                    for x in missing[:30]
                )
            )

            if len(missing) > 30:
                message += "…"

        if self.map_warnings:
            message += (
                f"\n\n"
                f"{len(self.map_warnings)} "
                "province/paint boundary "
                "warning(s) were written "
                "to "
                "culture_painter_map_warnings.txt."
            )

        self.index = scan_cultures(
            self.mod_root
        )

        messagebox.showinfo(
            APP_TITLE,
            message,
        )

        self.status_var.set(
            "Apply complete."
        )


# =============================================================================
# MAIN
# =============================================================================


def main():
    mod_root = (
        Path(__file__)
        .resolve()
        .parent
    )

    required = [
        mod_root
        / "map"
        / "provinces.bmp",

        mod_root
        / "map"
        / "definition.csv",

        mod_root
        / "history"
        / "provinces",
    ]

    missing = [
        path.relative_to(
            mod_root
        )
        for path in required
        if not path.exists()
    ]

    root = tk.Tk()
    root.withdraw()

    if missing:
        messagebox.showerror(
            APP_TITLE,
            "This script must be in "
            "the root of your EU4 mod.\n\n"
            "Missing:\n"
            + "\n".join(
                f"• {item}"
                for item in missing
            ),
        )

        root.destroy()
        return

    root.deiconify()

    CulturePainter(
        root,
        mod_root,
    )

    root.mainloop()


if __name__ == "__main__":
    main()
