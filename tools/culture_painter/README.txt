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
- Lets you edit localized names and SOFTWARE colours.
- Lets you move cultures between culture groups.
- Saves province culture assignments only when you click Save.
- Makes a timestamped backup under tools/culture_painter/backups/.


SOFTWARE COLOURS
----------------
EU4 cultures and culture groups do not have a native RGB colour field.
The colours used by this editor are saved in:

    tools/culture_painter/culture_painter_data.json

They are not written as invalid EU4 color = {...} fields.


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
localisation/*.yml


EXTENSIBILITY
-------------
The map/selection/editor machinery is intentionally separate from the
CultureLayerModel. A future ReligionLayerModel can reuse the same renderer,
province/area/region painting, water locking and undo/redo system while writing
religion = ... instead of culture = ....
