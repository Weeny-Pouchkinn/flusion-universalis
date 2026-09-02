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

Built-in designer library:
    40+ backgrounds including horizontal/vertical/diagonal stripes, bordered
    crosses, Nordic crosses, saltires, chevrons, cantons, lozenges, quarters,
    gyronny, sunbursts, borders, bands and five-band designs.

    35+ vector emblems including stars, crescents, crosses, fleur-de-lis, crown,
    shield, castles, weapons, anchor, tree, sun/moon, gear, book, heart, skull,
    paw/cat motifs, mountain and wave.

    "Custom image" lets you import your own emblem image and overlays it on the
    generated flag.

    The complete 120-symbol vanilla client-state/custom-nation emblem set is
    also bundled under tools/culture_painter/vanilla_symbols/. Click
    "Browse vanilla symbols..." to choose visually by EU4 emblem index (1-120). The DDS atlas itself is a padded 32 x 4 grid with a reserved blank cell at index 0.
    The package includes the supplied trade_flags, flag_smallest, small, medium,
    large and mini DDS resolutions; the painter automatically chooses an
    appropriate atlas when baking or previewing a flag. Vanilla-symbol choices
    are saved as stable values such as "Vanilla symbol 001".


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
- Army/navy OOB editor: each country can contain multiple starting army/fleet
  entries with a province and composition. Province 0 means the capital. Fleets
  are validated against coastal provinces. The current EU4 scripting interface
  exposes starting unit spawn effects by province, so stack names are retained as
  editor metadata/comments while composition and location are the game-effective
  part.
- The country list has Copy, which clones the selected country's setup (court,
  flag, estates, ideas and OOB) into a new tag. Painted territory is intentionally
  not copied, making this useful as a country template.
- Hovering the map displays a persistent tooltip with the province ID, and the
  status line also shows the province/area/region context.
- Advisors, diplomacy, subjects and starting wars are still separate future setup
  systems.


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

OOB AUTO-GENERATOR
------------------
The country editor's Army / navy OOB tab now has "Auto-generate armies & fleets...".
Enter total infantry/cavalry/artillery and total heavy/light/galley/transport ships,
plus the desired number of logical armies/fleets. The tool splits each total as evenly
as possible, names the results "1st Army of <Nation>", "2nd Army of <Nation>", etc.,
and leaves every generated row editable.

Placement can use the capital/default province or spread armies across owned land
provinces and fleets across owned coastal provinces. You can replace the current OOB
or append generated stacks to it.

EU4 ENGINE LIMITATION: the public EU4 script API exposes infantry/cavalry/artillery and
ship spawn effects, but no army/navy object creation or merge-units effect. Therefore
exact scripted starting compositions still enter the game as individual spawned units;
the painter stores them as logical stacks but cannot force the engine to merge them into
one selectable army/fleet at scenario load. This is an EU4 scripting limitation rather
than an editor-side data-model limitation.


HOI4 NAMELIST IMPORT FOR CULTURES
---------------------------------
Select a culture in the Cultures layer and click "Import names...". Choose a
text file containing either a HOI4 common/names block body, for example:

    male = { names = { Alex Bob } }
    female = { names = { Alice Beth } }
    surnames = { Smith Jones }

or one complete wrapper such as:

    TAG = {
        names = { Alex Casey }      # unisex
        surnames = { Smith Jones }
    }

The importer understands HOI4's gendered and unisex forms:
    - male -> names       becomes EU4 male_names
    - female -> names     becomes EU4 female_names
    - top-level names     is unisex, so it is added to both EU4 pools
    - all top-level/male/female surnames are merged into EU4 dynasty_names
    - callsigns are ignored because EU4 culture definitions have no callsign pool

A preview dialog shows exactly what will be imported before anything changes.
The import stays in memory until the main Save button is clicked. On Save the
painter replaces only that culture's direct male_names, female_names and
dynasty_names blocks; other culture properties and other cultures are preserved.
The imported lists are also stored in culture_painter_data.json so later saves
remain deterministic.

EU4 also permits name pools at culture-group scope. This importer intentionally
writes them at culture scope, because it is meant to assign one HOI4 namelist to
one culture. Existing group-level pools, if you have deliberately created any,
are not removed by the importer.

- HOI4 namelists can be imported onto one culture or mass-applied to a whole culture group, with an option to preserve or override cultures that already define their own name pools.
