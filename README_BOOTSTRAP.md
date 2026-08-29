# Flusion Universalis — generated EU4 bootstrap

Copy the CONTENTS of `output/mod/` into your `flusion-universalis` mod folder.

Also copy `output/Flusion Universalis.mod` beside the mod folder in the EU4 user mod directory, or ensure your existing external `.mod` file contains the same `replace_path` lines.

## Inputs used

Required:
- `provinces.bmp` — generated province raster
- `province_mapping.json` — generated mapping/impassable/state/region/source-overlap data
- `hoi4_definition.csv` — ORIGINAL LoK HOI4 province definition; its terrain column drives generated `map/terrain.bmp`

Optional but required for a visually complete first map boot:
- `rivers.bmp` — your converted EU4 indexed river map
- `trees.bmp` — your converted EU4 indexed tree map

## Generated automatically

- EU4 `map/provinces.bmp` and `definition.csv`
- EU4 indexed `terrain.bmp`, mechanically inferred from the original HOI4 definition terrain values
- `default.map`, positions, areas, regions, superregion, continent, climate/impassables, adjacency
- technical heightmap, normal map, and seasonal/water colormaps
- one-country province histories and bookmark
- one global bootstrap trade node
- `descriptor.mod` and an external launcher `.mod` template

## One-country guarantee

The mod replaces vanilla `common/country_tags`, but preserves EU4's required special pseudo-country tags `REB`, `PIR`, and `NAT`. `FUS` is the only ordinary country. Vanilla province history, country history, diplomacy, wars, bookmarks, and trade nodes are replaced.

Bootstrap country `FUS` owns only province **426**; all other playable land begins unowned.

## Terrain conversion

The script uses the pixel-weighted source-overlap mapping for each new province and reads the original HOI4 `definition.csv` terrain field. Sea is forced to EU4 terrain index 15 and lakes to 17. The mapping table is near the top of `generate_bootstrap.py` and can be edited later.

Generated `terrain.bmp` is an 8-bit indexed BMP. It relies on vanilla EU4 `map/terrain.txt` index semantics.

## River/tree status

- rivers.bmp: MISSING: rivers.bmp
- trees.bmp: MISSING: trees.bmp

If either is absent, the generated map folder contains `!!!_ADD_MISSING_RIVERS_TREES.txt`.
