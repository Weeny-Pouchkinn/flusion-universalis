EU4 CULTURE PAINTER
===================

INSTALLATION
------------
Place this entire folder here:

    <your EU4 mod>/tools/culture_painter/

So that culture_painter.py is at:

    <your EU4 mod>/tools/culture_painter/culture_painter.py

Install dependencies once:

    py -m pip install -r requirements.txt

Then double-click:

    run_culture_painter.bat


WHAT IT DOES
------------
- Builds a culture map from your actual EU4 provinces.bmp and province history.
- Paints by Province, Area, or Region.
- Water/lake provinces listed in map/default.map are locked and cannot be painted.
- Shows either culture colours or culture-group colours.
- Lets you create culture groups and cultures.
- Lets you edit localized names and colours.
- Lets you move cultures between culture groups.
- Saves province culture assignments only when you click Save.
- Makes a timestamped backup under tools/culture_painter/backups/.


CULTURE-GROUP COLOURS IN EU4
----------------------------
EU4 does not use a color = {...} field inside a culture-group definition.
Instead, culture groups obtain their map-mode colour positionally from:

    common/region_colors/00_region_colors.txt

When you click Save, this tool synchronizes every culture group's colour in the
editor with the corresponding in-game palette entry.

IMPORTANT FOR TOTAL CONVERSIONS
-------------------------------
The mapping is global across every culture group loaded by the game. If vanilla
culture groups are still loaded, all of this mod's group indexes are shifted and
the game will display the wrong colours. This can also create duplicate culture
IDs such as "english".

To prevent that, Save automatically ensures these lines exist in descriptor.mod:

    replace_path="common/cultures"
    replace_path="common/region_colors"

For a local development mod, the tool also updates the matching sibling .mod
launcher descriptor when it can identify it unambiguously. Those descriptor files
are included in the timestamped backup before being changed.

The mapping is positional after vanilla replacement: culture groups are read in
common/cultures file/block order. EU4 skips palette entry 0 for culture groups, so
the first culture group uses palette entry 1, the second uses entry 2, and so on.

The tool preserves existing unused palette entries. If your mod does not already
contain common/region_colors/00_region_colors.txt, it creates a sufficiently large
palette and fills non-culture-group slots with stable fallback colours.

Individual CULTURE colours are still editor-only: EU4 does not expose an
independent RGB field for individual cultures in this system.


CONTROLS
--------
Left click / left-drag : paint using the selected culture
Middle/right drag      : pan map
Mouse wheel            : zoom
Toolbar                 : Save, Undo, Redo, paint scope, colour view

Double-click a culture/group in the list to edit it.


FILES USED
----------
map/provinces.bmp
map/definition.csv
map/default.map
map/area.txt
map/region.txt
history/provinces/*.txt
common/cultures/*.txt
common/region_colors/00_region_colors.txt
localisation/*.yml

Editor metadata is stored in:

tools/culture_painter/culture_painter_data.json


EXTENSIBILITY
-------------
The map/selection/editor machinery is intentionally separate from the
CultureLayerModel. A future ReligionLayerModel can reuse the same renderer,
province/area/region painting, water locking and undo/redo system while writing
religion = ... instead of culture = ....
