EU4 SETUP PAINTER
=================

INSTALLATION
------------
Put this whole folder here:

    <your EU4 mod>/tools/culture_painter/

This is intentionally the same folder as the older Culture Painter, so your
existing culture_painter_data.json is reused automatically.

Install dependencies once:

    py -m pip install -r requirements.txt

Then run:

    run_culture_painter.bat


LAYERS
------
The top-left Layer dropdown currently contains:

    Cultures
    Countries

The map/brush system is shared between layers. This is deliberate so a Religion
layer can be added later without rebuilding the map editor.

For either layer you may paint:

    Province
    Area
    Region

Ocean and lake provinces from map/default.map remain locked and cannot be
painted.

Changes are kept in memory until Save is clicked. Timestamped backups are made
before mod files are changed.


COUNTRY PAINTER
---------------
Switch Layer to Countries, create/select a country in the left panel, then paint
it onto the map. Existing province ownership is loaded from history/provinces.

"Unowned / clear ownership" removes owner and controller from painted provinces.

When a country owns painted land, Save writes:

    owner = TAG
    controller = TAG
    add_core = TAG

Existing other cores are deliberately preserved; the painter adds the new
owner's core rather than destroying historical/claim cores.

If a managed country's capital is 0 and it owns painted provinces, Save chooses
the lowest owned province ID as its capital automatically.


COUNTRY EDITOR FIELDS
---------------------
Identity:
    - 3-letter country tag (new countries only)
    - localized name
    - adjective
    - political-map RGB colour
    - graphical culture

Starting setup:
    - government type
    - government rank (1/2/3)
    - optional starting government reform
    - stability
    - capital province
    - start date
    - religion
    - primary culture
    - accepted cultures
    - technology group
    - treasury
    - prestige
    - optional ADDITIVE ADM/DIP/MIL technology effects

Court:
    - ruler
    - optional heir
    - optional consort
    - name and dynasty
    - age and gender
    - ADM/DIP/MIL stats (0-6)
    - culture
    - religion
    - personality traits (scrollable multi-select)
    - heir claim

Estates:
    - Default: the painter writes nothing and EU4 handles normal estate shares
    - Custom: exact estate percentages can be entered; crownland is the remainder

National ideas:
    - Default: no tag-specific idea set is generated
    - Custom: traditions + seven ideas + ambition
    - Modifier bodies are ordinary EU4 script lines, e.g. discipline = 0.05


FLAGS
-----
Normal EU4 tags use gfx/flags/TAG.tga. The painter always bakes a 128x128 TGA on
Save.

Two authoring modes are available:

1. Image import
   PNG, TGA, BMP, JPEG and TIFF can be selected. The source is copied into
   tools/culture_painter/flag_sources and is cropped/resized to 128x128 on Save.

2. Nation Designer-style
   Choose a pattern, three colours and an emblem. The Nation Designer itself uses
   three flag-colour channels in gfx/custom_flags. For a normal tagged country,
   however, this painter bakes the chosen composition into the normal static TGA.

Built-in patterns:
    Solid
    Horizontal bicolor / tricolor
    Vertical bicolor / tricolor
    Diagonal
    Quartered
    Center cross
    Nordic cross
    Saltire

Built-in emblems:
    None, Circle, Diamond, Star, Crescent, Ring


FILES GENERATED / EDITED BY THE COUNTRY LAYER
----------------------------------------------
New countries:
    common/country_tags/zz_country_painter_tags.txt
    common/countries/ZZ_Painter_TAG.txt
    history/countries/TAG - <name>.txt
    gfx/flags/TAG.tga

Managed data:
    localisation/zz_country_painter_l_english.yml
    common/ideas/zz_country_painter_ideas.txt
    tools/culture_painter/country_painter_data.json

Province ownership:
    history/provinces/*.txt

Existing countries are not rewritten wholesale. Their common country file gets
its colour/graphical culture updated, and the painter inserts a clearly marked
managed startup section into their history file. Existing unrelated history is
preserved.


IMPORTANT NOTES
---------------
- The painter intentionally restricts newly created tags to three letters. Some
  apparent three-character strings collide with EU4 script keywords; known bad
  tags are rejected.
- Explicit ADM/DIP/MIL technology values are additive effects, because EU4's
  current script interface exposes add_*_tech rather than exact set_*_tech.
  Leave the option off unless you intentionally need extra starting tech.
- Custom national-idea modifier text is written literally. Invalid modifier keys
  will therefore produce EU4 script errors; this is intentional so the tool does
  not artificially restrict modded modifiers.
- Army/navy OOBs, advisors, diplomacy, subjects and starting wars are not part of
  this version. They are separate setup systems rather than properties required
  to define or paint a country.


NAME / LOCALISATION OVERRIDES
-----------------------------
On Save, the painter now builds:

    localisation/replace/zz_setup_painter_overrides_l_english.yml

EU4 gives localisation files in localisation/replace priority over ordinary
vanilla/DLC localisation. The painter copies its managed country/culture names
there and also promotes every PROV<number> / PROV<number>_ADJ key already
provided anywhere in your mod's localisation tree.

The painter also ensures this descriptor rule exists:

    replace_path="common/province_names"

That suppresses vanilla culture/tag-specific dynamic province names, which can
otherwise replace a correct PROV123 base name in-game when dynamic province
names are enabled. Your mod can still add its own common/province_names files
later; replace_path only removes the vanilla/DLC versions.

Do NOT use replace_path="localisation". The special localisation/replace folder
is the correct high-priority mechanism and allows all unrelated vanilla UI text
to remain available.
