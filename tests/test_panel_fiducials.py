"""Panel fiducials: the library they come from, where they go, and the bytes.

Five subjects, and each one is a defect the user would meet.

**THE LIBRARY IS KICAD'S ANSWER, NOT OURS.** The library is resolved through
the KiCad setup settings, never through a list of this tool's own. The
footprint list comes from the global `fp-lib-table`, the nested tables it
includes and the path variables `kicad_common.json` defines. What is pinned
here is the policy on top of that: which roots may be read, what a failure
SAYS, and the escape hatch that lets a machine with no KiCad settings at all
resolve a library.

**EVERY FOOTPRINT WHOSE NAME SAYS FIDUCIAL IS SELECTABLE:** case-insensitive
`"fiducial"` anywhere in the name, over the whole library, whatever the variant
naming. Not a curated subset and not a prefix - fenced on a fixture library
holding a lower-case name, a mixed-case one and a generated-suffix one.

**THE TOOLING HOLES ARE THE DATUM.** Positions are asserted to the micrometre
against hole plus offset, inward, for every corner and both directions. A corner
whose hole was declined gets no fiducial and says so by name.

**A PLACED FIDUCIAL IS THE LIBRARY'S OWN FOOTPRINT.** Same FPID, same
attributes, same geometry - and uuids that are a function of the config alone,
so two builds of one panel are one panel.

**AND THE BYTES.** No section is the panel built before this stage existed, to
the byte; two builds with fiducials are byte-identical; and a fiducial edit is a
CARRIER edit, so the kept stepped panel serves it and the answer equals a
from-scratch build's.
"""

import json
import re
import tempfile
import unittest
import uuid
from pathlib import Path

from eedesigncore import Board, Footprint

from eedesignpanels import fplib
from eedesignpanels.channels import Rect
from eedesignpanels.config import FiducialsConfig, load_config
from eedesignpanels.preview import actions, page_panel, panelbuild
from eedesignpanels.stages import fiducials, tooling_holes
from eedesignpanels.stages.fiducials import (
    DESIGNATOR_PREFIX,
    DIRECTIONS,
    FLIP_DIRECTION,
    Settings,
    offset_vector,
    plan,
)
from test_panel_page import a_session, controls_of, write_panel_project

# The corpus panel's blank and board grid, origin at zero so the arithmetic
# reads - the same fixture numbers `test_tooling_holes` uses.
BLANK = Rect(min_x=0.0, min_y=0.0, max_x=44.0, max_y=47.5)
GRID = (5.0, 5.0, 39.0, 42.5)

# ONE BACK-SIDE FOOTPRINT, IN KICAD'S OWN BYTES. C2 on the synthetic board was
# turned over by KiCad's own `FOOTPRINT::Flip` and saved by KiCad
# (`data/make_synthetic_board.py`), so the flip has something to be checked
# AGAINST rather than only against what this project expects. See
# `BackSideTest`.
AUTHORED_BACK_SIDE = Path(__file__).parent / "data" / "synthetic_board.kicad_pcb"
AUTHORED_BACK_SIDE_REFERENCE = "C2"

# THE FIXTURE LIBRARY'S NAMES, and the three of them are the cases that matter.
# One ordinary name, one all lower case, one mixed case with a generated suffix
# of the shape a library generator writes - plus one footprint that is not a
# fiducial at all and must never be offered.
FIXTURE_NAMES = (
    "Fiducial_1mm_Mask2mm",
    "fiducial_lowercase_0.5mm",
    "Target_FIDUCIAL_0.4mm_Variant_1789083176376",
    "MountingHole_3.2mm",
)

# One fiducial as a library file: an SMD copper dot with a mask opening, two
# concentric graphics and the four properties KiCad writes. Written by this test
# rather than copied out of the EDA library, which is read-only and whose files
# are nobody's fixture.
MOD_TEMPLATE = """(footprint "{name}"
	(version 20260206)
	(generator "test")
	(layer "F.Cu")
	(descr "{descr}")
	(tags "fixture fiducial")
	(property "Reference" "REF**"
		(at 0 -1 0)
		(layer "F.SilkS")
		(hide yes)
		(effects
			(font
				(size 1 1)
				(thickness 0.15)
			)
		)
	)
	(property "Value" "{name}"
		(at 0 1 0)
		(layer "F.Fab")
		(effects
			(font
				(size 1 1)
				(thickness 0.15)
			)
		)
	)
	(attr smd exclude_from_bom)
	(fp_circle
		(center 0 0)
		(end {radius} 0)
		(stroke
			(width 0.05)
			(type default)
		)
		(fill no)
		(layer "F.CrtYd")
	)
	(pad "" smd circle
		(at 0 0)
		(size {size} {size})
		(layers "F.Cu" "F.Mask")
		(solder_mask_margin 0.25)
	)
	(embedded_fonts no)
)
"""


def a_library(folder: Path, names=FIXTURE_NAMES) -> Path:
    """A `.pretty` directory holding one `.kicad_mod` per name. Returns it."""
    directory = Path(folder) / "Fixture.pretty"
    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        (directory / f"{name}{fplib.FOOTPRINT_SUFFIX}").write_text(
            MOD_TEMPLATE.format(
                name=name, descr=f"fixture footprint {index}",
                radius=f"{0.3 + index * 0.1:g}", size=f"{1.0 + index * 0.5:g}",
            ),
            encoding="utf-8",
        )
    return directory


def the_holes(corners=(True, True, True, True), diameter_mm=3.175):
    """The tooling holes this panel is fixtured by - the fiducials' datum."""
    return tooling_holes.plan(
        BLANK, GRID,
        tooling_holes.Settings(diameter_mm=diameter_mm, corners=corners),
    )


def machine_library():
    """The real default fiducial library, or None when this machine has no KiCad."""
    fplib.forget()
    found = fplib.resolve()
    return found if found.ok else None


# ---------------------------------------------------------------------------
# the library, resolved through KiCad's own settings
# ---------------------------------------------------------------------------


class LibraryResolutionTest(unittest.TestCase):
    """Where the footprint list comes from, and what a failure says."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        fplib.forget()

    def tearDown(self):
        self._folder.cleanup()
        fplib.forget()

    def test_a_pinned_directory_is_the_library(self):
        # THE HEADLESS ESCAPE HATCH. A machine with no KiCad settings resolves
        # a library because the config names the directory.
        directory = a_library(self.folder)
        found = fplib.resolve("Fixture", directory)
        self.assertTrue(found.ok)
        self.assertEqual(found.directory, directory)
        self.assertEqual(found.reason, "")
        self.assertEqual(found.footprints, tuple(sorted(FIXTURE_NAMES)))

    def test_every_name_that_says_fiducial_is_offered(self):
        # Case-insensitive "fiducial"
        # anywhere in the name, over the WHOLE library. A lower-case name, a
        # mixed-case one and a generated suffix are all selectable; a mounting
        # hole in the same library is not.
        found = fplib.resolve("Fixture", a_library(self.folder))
        self.assertEqual(
            found.fiducials,
            (
                "Fiducial_1mm_Mask2mm",
                "Target_FIDUCIAL_0.4mm_Variant_1789083176376",
                "fiducial_lowercase_0.5mm",
            ),
        )
        self.assertNotIn("MountingHole_3.2mm", found.fiducials)

    def test_a_pinned_directory_that_is_not_there_says_so(self):
        found = fplib.resolve("Fixture", self.folder / "nope.pretty")
        self.assertFalse(found.ok)
        self.assertIn("nope.pretty", found.reason)
        self.assertIn("not a directory", found.reason)

    def test_no_table_names_the_directory_it_looked_in(self):
        # THE CI CASE, and the whole of what happens without KiCad: the
        # resolution fails by NAME, saying where it looked.
        found = fplib.resolve(settings=self.folder / "no-kicad-here")
        self.assertFalse(found.ok)
        self.assertIn("no-kicad-here", found.reason)
        self.assertIn("fp-lib-table", found.reason)

    def test_a_nickname_no_table_declares_says_which_nickname(self):
        table = self.folder / "settings"
        table.mkdir()
        (table / "fp-lib-table").write_text(
            '(fp_lib_table\n (version 7)\n)\n', encoding="utf-8"
        )
        found = fplib.resolve("NotDeclared", settings=table)
        self.assertFalse(found.ok)
        self.assertIn("NotDeclared", found.reason)
        self.assertIn("Manage Footprint Libraries", found.reason)

    def test_an_undefined_path_variable_is_named(self):
        # The one failure mode that is worth naming a VARIABLE for: the table
        # is fine, the row is fine, and the user has not set the path.
        table = self.folder / "settings"
        table.mkdir()
        (table / "fp-lib-table").write_text(
            '(fp_lib_table\n (version 7)\n'
            ' (lib (name "Fixture") (type "KiCad")'
            ' (uri "${NO_SUCH_PATH_VAR}/Fixture.pretty")'
            ' (options "") (descr ""))\n)\n',
            encoding="utf-8",
        )
        found = fplib.resolve("Fixture", settings=table)
        self.assertFalse(found.ok)
        self.assertIn("NO_SUCH_PATH_VAR", found.reason)
        self.assertIn("Configure Paths", found.reason)

    def test_the_settings_roots_are_what_kicad_settings_name(self):
        # The confinement widening, and it is exactly the paths the user
        # configured in KiCad - a table row pointing anywhere else is refused.
        table = self.folder / "settings"
        table.mkdir()
        real = self.folder / "workspace"
        real.mkdir()
        (table / "kicad_common.json").write_text(
            json.dumps({"environment": {"vars": {
                "REAL_PATH": str(real),
                "GONE_PATH": str(self.folder / "not-here"),
                "EMPTY_PATH": "  ",
            }}}),
            encoding="utf-8",
        )
        self.assertEqual(fplib.settings_roots(table), (real,))

    def test_no_settings_file_is_no_extra_roots_and_no_error(self):
        self.assertEqual(fplib.settings_roots(self.folder), ())

    def test_the_settings_directory_is_kicads_own(self):
        # Delegated to eedesigncore, which implements common/paths.cpp - named
        # here so a failure can say which directory it looked in.
        self.assertEqual(
            fplib.settings_dir().name, "10.0",
        )
        self.assertEqual(fplib.settings_dir().parent.name, "kicad")

    def test_a_footprint_name_is_a_file_name_and_carries_no_path(self):
        # Validated before use: a config is hand-edited and this name reaches
        # the file system, so the SHAPE is refused before a path is built from
        # it.
        for bad in ("../escape", "a/b", "a\\b", "C:/x", "", ".hidden", None,
                    "x" * 200):
            with self.subTest(name=bad), \
                    self.assertRaises(fplib.LibraryError):
                fplib.checked_name(bad)
        for good in FIXTURE_NAMES:
            self.assertEqual(fplib.checked_name(good), good)

    def test_loading_a_footprint_the_library_does_not_hold_says_so(self):
        found = fplib.resolve("Fixture", a_library(self.folder))
        with self.assertRaises(fplib.LibraryError) as raised:
            fplib.load(found, "NotInHere")
        self.assertIn("NotInHere", str(raised.exception))
        self.assertIn("Fixture", str(raised.exception))

    def test_loading_from_a_library_that_did_not_resolve_repeats_the_reason(self):
        found = fplib.resolve(settings=self.folder / "no-kicad-here")
        with self.assertRaises(fplib.LibraryError) as raised:
            fplib.load(found, "Fiducial_1mm_Mask2mm")
        self.assertEqual(str(raised.exception), found.reason)

    def test_the_cache_gives_the_same_answer_and_forgets_on_request(self):
        directory = a_library(self.folder)
        first = fplib.resolve_cached("Fixture", directory)
        self.assertIs(fplib.resolve_cached("Fixture", directory), first)
        fplib.forget()
        self.assertIsNot(fplib.resolve_cached("Fixture", directory), first)


class MachineLibraryTest(unittest.TestCase):
    """The real library on this machine, through KiCad's real tables.

    SKIPPED where there is no KiCad - which is the CI container, and is the
    behaviour the headless constraint asks for: the core keeps working, the
    resolution reports its own failure, and nothing here pretends otherwise.
    """

    @classmethod
    def setUpClass(cls):
        cls.found = machine_library()
        if cls.found is None:
            raise unittest.SkipTest(
                "this machine's KiCad footprint library table does not "
                f"resolve {fplib.DEFAULT_FIDUCIAL_LIBRARY}"
            )

    def test_the_library_resolves_through_a_nested_table(self):
        # The global table includes another table; the row comes from the
        # INCLUDED one, which is what `library_manager.cpp:118-165` does.
        self.assertTrue(self.found.ok)
        self.assertNotEqual(
            self.found.source_table, fplib.settings_dir() / "fp-lib-table"
        )
        self.assertTrue(self.found.directory.is_dir())

    def test_the_path_variable_in_the_row_was_expanded(self):
        self.assertIn("${", self.found.raw_uri)
        self.assertNotIn("${", str(self.found.directory))

    def test_the_library_offers_fiducials_and_they_all_say_fiducial(self):
        self.assertTrue(self.found.fiducials)
        for name in self.found.fiducials:
            self.assertIn("fiducial", name.lower())

    def test_one_of_them_loads_as_a_footprint(self):
        definition = fplib.load(self.found, self.found.fiducials[0])
        self.assertIsInstance(definition, Footprint)
        self.assertTrue(definition.pads)


# ---------------------------------------------------------------------------
# the placement: measured from the tooling holes, inward
# ---------------------------------------------------------------------------


class OffsetTest(unittest.TestCase):
    """Which way the offset runs, for every corner and both directions."""

    def test_along_the_horizontal_rails_the_offset_runs_in_x(self):
        for corner, expected in (("top_left", 5.0), ("bottom_left", 5.0),
                                 ("top_right", -5.0), ("bottom_right", -5.0)):
            with self.subTest(corner=corner):
                self.assertEqual(
                    offset_vector(corner, DIRECTIONS[0], 5.0), (expected, 0.0)
                )

    def test_along_the_vertical_rails_the_offset_runs_in_y(self):
        for corner, expected in (("top_left", 5.0), ("top_right", 5.0),
                                 ("bottom_left", -5.0), ("bottom_right", -5.0)):
            with self.subTest(corner=corner):
                self.assertEqual(
                    offset_vector(corner, DIRECTIONS[1], 5.0), (0.0, expected)
                )

    def test_the_offset_is_always_inward(self):
        # Outward from a corner hole is off the panel, so there is no sign to
        # get wrong: every vector points at the middle of the blank.
        middle = ((BLANK.min_x + BLANK.max_x) / 2.0,
                  (BLANK.min_y + BLANK.max_y) / 2.0)
        holes = {hole.corner: hole for hole in the_holes().holes}
        for direction in DIRECTIONS:
            for corner, hole in holes.items():
                dx, dy = offset_vector(corner, direction, 5.0)
                with self.subTest(corner=corner, direction=direction):
                    before = (abs(hole.at[0] - middle[0])
                              + abs(hole.at[1] - middle[1]))
                    after = (abs(hole.at[0] + dx - middle[0])
                             + abs(hole.at[1] + dy - middle[1]))
                    self.assertLess(after, before)

    def test_a_direction_nobody_defined_is_refused_by_name(self):
        with self.assertRaises(ValueError) as raised:
            offset_vector("top_left", "diagonally", 5.0)
        self.assertIn("diagonally", str(raised.exception))

    def test_a_corner_nobody_defined_is_refused_by_name(self):
        with self.assertRaises(ValueError) as raised:
            offset_vector("middle", DIRECTIONS[0], 5.0)
        self.assertIn("middle", str(raised.exception))


class PlanTest(unittest.TestCase):
    """Where every fiducial goes, asserted to the micrometre against its hole."""

    def test_each_target_is_its_holes_position_plus_the_offset(self):
        holes = the_holes()
        by_corner = {hole.corner: hole for hole in holes.holes}
        for direction in DIRECTIONS:
            made = plan(holes, BLANK, GRID, Settings(
                footprint="Fiducial_1mm_Mask2mm", offset_mm=2.5,
                direction=direction, corners=(True, True, True, True),
            ))
            for target in made.placed:
                hole = by_corner[target.corner]
                dx, dy = offset_vector(target.corner, direction, 2.5)
                with self.subTest(corner=target.corner, direction=direction):
                    # To the micrometre, and then some: the position is
                    # arithmetic on the hole and nothing else.
                    self.assertAlmostEqual(
                        target.at[0], hole.at[0] + dx, places=9
                    )
                    self.assertAlmostEqual(
                        target.at[1], hole.at[1] + dy, places=9
                    )
                    self.assertEqual(target.datum, hole.designator)
                    self.assertEqual(target.datum_at, hole.at)

    def test_the_designators_run_clockwise_from_the_top_left(self):
        made = plan(the_holes(), BLANK, GRID,
                    Settings(corners=(True, True, True, True)))
        self.assertEqual(
            [target.designator for target in made.placed],
            [f"{DESIGNATOR_PREFIX}{n}" for n in (1, 2, 3, 4)],
        )
        self.assertEqual(
            [target.corner for target in made.placed],
            ["top_left", "top_right", "bottom_right", "bottom_left"],
        )

    def test_three_corners_is_the_orientation_pattern(self):
        made = plan(the_holes(), BLANK, GRID,
                    Settings(corners=(True, True, True, False)))
        self.assertEqual(made.count, 3)
        self.assertTrue(made.orientation_pattern)

    def test_four_corners_is_not(self):
        made = plan(the_holes(), BLANK, GRID,
                    Settings(corners=(True, True, True, True)))
        self.assertFalse(made.orientation_pattern)

    def test_the_shipped_corners_are_the_orientation_pattern(self):
        # The default is the answer that tells a machine which way round the
        # panel went in, which is the point of the control.
        self.assertTrue(Settings().orientation_pattern)
        self.assertEqual(
            Settings().enabled_corners,
            ("top_left", "top_right", "bottom_right"),
        )

    def test_a_corner_with_no_tooling_hole_is_declined_by_name(self):
        # THE DATUM IS THE HOLE, so a corner without one has nothing to
        # measure from - and the run says which corner rather than counting.
        holes = the_holes(corners=(True, True, True, False))
        made = plan(holes, BLANK, GRID,
                    Settings(corners=(True, True, True, True)))
        self.assertEqual(made.count, 3)
        self.assertEqual(len(made.declined), 1)
        self.assertEqual(made.declined[0].corner, "bottom_left")
        self.assertIn("no tooling hole", made.declined[0].reason)

    def test_no_holes_at_all_is_every_corner_declined(self):
        made = plan(None, BLANK, GRID,
                    Settings(corners=(True, True, True, True)))
        self.assertEqual(made.count, 0)
        self.assertEqual(len(made.declined), 4)

    def test_an_offset_that_reaches_the_boards_is_declined_by_name(self):
        # A fiducial on a board instance is a fiducial inside somebody's
        # product. A rail-corner-centre hole cannot get there - the offset runs
        # ALONG a rail band and stays in it - but a hole placed by a FIXED
        # INSET can sit over the board field's own span, and then a cross-rail
        # offset walks straight into it. Ten millimetres in from the left edge
        # puts this hole at x = 10, inside the boards' 5..39 mm span.
        holes = tooling_holes.plan(BLANK, GRID, tooling_holes.Settings(
            placement="fixed_inset", inset_across_mm=10.0, inset_down_mm=2.0,
            corners=(True, False, False, False),
        ))
        self.assertEqual(holes.holes[0].at, (10.0, 2.0))
        made = plan(holes, BLANK, GRID, Settings(
            offset_mm=4.0, direction=DIRECTIONS[1],
            corners=(True, False, False, False),
        ))
        self.assertEqual(made.count, 0)
        self.assertEqual(len(made.declined), 1)
        self.assertIn("on the boards", made.declined[0].reason)
        self.assertIn("other pair of rails", made.declined[0].reason)

    def test_an_offset_along_a_rail_stays_on_that_rail(self):
        # The reason the decline above needs a contrived hole: an offset that
        # runs along a rail band cannot leave it, however long it is, until it
        # runs off the blank. Stated so the guard's narrowness is on purpose.
        made = plan(the_holes(corners=(True, False, False, False)), BLANK, GRID,
                    Settings(offset_mm=20.0, corners=(True, False, False, False)))
        self.assertEqual(made.count, 1)
        self.assertEqual(made.declined, ())

    def test_an_offset_off_the_panel_is_an_error_with_the_coordinates(self):
        made = Settings(offset_mm=60.0, corners=(True, False, False, False))
        with self.assertRaises(ValueError) as raised:
            plan(the_holes(), BLANK, GRID, made)
        self.assertIn("off the panel blank", str(raised.exception))

    def test_settings_have_no_defaults_to_fall_back_on(self):
        # Opt-in: "no section" is answered by not calling the stage, so None
        # is a caller bug and says so rather than inventing a footprint.
        with self.assertRaises(ValueError):
            Settings.of(None)

    def test_settings_read_a_mapping_or_a_config_object(self):
        mapped = Settings.of({
            "footprint": "Fiducial_1mm_Mask2mm",
            "offset_mm": 3.0,
            "direction": DIRECTIONS[1],
            "side": "top",
            "corners": {"bottom_left": True, "top_right": False},
        })
        self.assertEqual(mapped.footprint, "Fiducial_1mm_Mask2mm")
        self.assertEqual(mapped.offset_mm, 3.0)
        self.assertEqual(
            mapped.enabled_corners,
            ("top_left", "bottom_right", "bottom_left"),
        )
        modelled = Settings.of(FiducialsConfig(
            footprint="Fiducial_1mm_Mask2mm", offset_mm=3.0,
        ))
        self.assertEqual(modelled.footprint, "Fiducial_1mm_Mask2mm")
        self.assertEqual(modelled.offset_mm, 3.0)

    def test_a_side_nobody_defined_is_refused_by_name(self):
        with self.assertRaises(ValueError) as raised:
            Settings(side="sideways").sides
        self.assertIn("sideways", str(raised.exception))


# ---------------------------------------------------------------------------
# the placed footprint: the library's own, with identities we chose
# ---------------------------------------------------------------------------


class PlacedFootprintTest(unittest.TestCase):
    """A fiducial on the panel is a placed instance of the library's footprint."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.directory = a_library(self.folder)
        fplib.forget()
        self.settings = Settings(
            footprint="Fiducial_1mm_Mask2mm", library="Fixture",
            library_path=str(self.directory),
            corners=(True, True, True, False),
        )

    def tearDown(self):
        self._folder.cleanup()
        fplib.forget()

    def placed(self, settings=None):
        board = Board()
        made = fiducials.apply(board, BLANK, GRID, settings or self.settings,
                               the_holes())
        return board, made

    def test_the_panel_carries_one_footprint_per_target(self):
        board, made = self.placed()
        self.assertEqual(made.count, 3)
        self.assertEqual(len(board.footprints), 3)

    def test_the_fpid_is_the_library_nickname_and_the_footprint_name(self):
        board, _ = self.placed()
        for footprint in board.footprints:
            self.assertEqual(
                footprint.fp_id, "Fixture:Fiducial_1mm_Mask2mm"
            )

    def test_the_reference_is_the_designator(self):
        board, made = self.placed()
        references = [
            next(p.value for p in footprint.properties
                 if p.name == "Reference")
            for footprint in board.footprints
        ]
        self.assertEqual(
            references, [target.designator for target in made.placed]
        )

    def test_the_position_is_the_targets_own(self):
        board, made = self.placed()
        for footprint, target in zip(board.footprints, made.placed):
            self.assertEqual(footprint.at.position, target.at)
            self.assertEqual(footprint.at.angle, 0.0)

    def test_the_library_decides_the_layers_the_attributes_and_the_geometry(self):
        # A FIDUCIAL REACHES THE ASSEMBLY OUTPUTS, which is the opposite of a
        # tooling hole: nothing here edits what the library author wrote.
        definition = Footprint.from_file(
            self.directory / f"Fiducial_1mm_Mask2mm{fplib.FOOTPRINT_SUFFIX}"
        )
        board, _ = self.placed()
        for footprint in board.footprints:
            self.assertEqual(footprint.layer, definition.layer)
            self.assertEqual(footprint.attributes, definition.attributes)
            self.assertEqual(len(footprint.pads), len(definition.pads))
            self.assertEqual(footprint.pads[0].layers,
                             definition.pads[0].layers)
            self.assertEqual(footprint.pads[0].size, definition.pads[0].size)
            self.assertEqual(footprint.description, definition.description)

    def test_every_identity_is_derived_and_not_minted(self):
        # SAME INPUT, SAME OUTPUT. `inject_footprint` mints random uuids,
        # which is KiCad's fresh-add behaviour and is exactly wrong for a
        # generated panel. Every uuid here is a uuid5 of the fiducial's
        # corner and side.
        first, _ = self.placed()
        second, _ = self.placed()
        self.assertEqual(first.to_string(), second.to_string())
        for footprint in first.footprints:
            self.assertEqual(uuid.UUID(footprint.uuid).version, 5)
            for item in (*footprint.properties, *footprint.drawings,
                         *footprint.pads):
                self.assertEqual(uuid.UUID(item.id).version, 5)

    def test_two_corners_do_not_share_an_identity(self):
        board, _ = self.placed()
        identities = {footprint.uuid for footprint in board.footprints}
        self.assertEqual(len(identities), len(board.footprints))

    def test_moving_the_offset_does_not_rename_anything(self):
        # The key is the corner and the side, never the position: a fiducial
        # nudged 0.5 mm is the same fiducial, and an identity that changed with
        # a coordinate would make every edit a new item in KiCad's eyes.
        near, _ = self.placed()
        far, _ = self.placed(self.settings._replace(offset_mm=4.0))
        self.assertEqual(
            [f.uuid for f in near.footprints], [f.uuid for f in far.footprints]
        )
        self.assertNotEqual(
            [f.at.position for f in near.footprints],
            [f.at.position for f in far.footprints],
        )

    def test_a_config_that_names_no_footprint_writes_nothing(self):
        board = Board()
        with self.assertRaises(fplib.LibraryError) as raised:
            fiducials.apply(board, BLANK, GRID,
                            self.settings._replace(footprint=""), the_holes())
        self.assertIn("no footprint is named", str(raised.exception))
        self.assertEqual(board.footprints, [])

    def test_a_footprint_the_library_does_not_hold_writes_nothing(self):
        board = Board()
        with self.assertRaises(fplib.LibraryError):
            fiducials.apply(board, BLANK, GRID,
                            self.settings._replace(footprint="NotInHere"),
                            the_holes())
        self.assertEqual(board.footprints, [])

    def test_a_declined_corner_reaches_the_warnings_by_name(self):
        board = Board()
        notes = []
        made = fiducials.apply(
            board, BLANK, GRID,
            self.settings._replace(corners=(True, True, True, True)),
            the_holes(corners=(True, True, True, False)), warnings=notes,
        )
        self.assertEqual(made.count, 3)
        self.assertEqual(len(notes), 1)
        self.assertIn("bottom left", notes[0])
        self.assertIn("3 optical target(s) instead of 4", notes[0])

    def test_nothing_placed_at_all_is_said_out_loud(self):
        # A panel whose every corner was refused carries no targets, and that
        # is not something to work out from a count of zero.
        board = Board()
        notes = []
        made = fiducials.apply(board, BLANK, GRID, self.settings, None,
                               warnings=notes)
        self.assertEqual(made.count, 0)
        self.assertEqual(board.footprints, [])
        self.assertTrue(
            any("no optical targets at all" in note for note in notes)
        )


# ---------------------------------------------------------------------------
# the back side: KiCad's own flip, checked step by step
# ---------------------------------------------------------------------------


def mirrored(item) -> bool:
    """Does this text item carry KiCad's `(justify mirror)`?"""
    attributes = getattr(item, "attributes", None)
    return bool(attributes is not None and attributes.mirror)


def every_layer(footprint) -> list:
    """Every layer name the placed footprint mentions, anywhere in it."""
    found = [footprint.layer]
    for pad in footprint.pads:
        found += list(pad.layers or ())
    found += [prop.layer for prop in footprint.properties]
    found += [getattr(drawing, "layer", None) for drawing in footprint.drawings]
    return [name for name in found if name]


class BackSideTest(unittest.TestCase):
    """A back-side target is `FOOTPRINT::Flip`, and every step of it is checked.

    **VERIFIED BEFORE IT WAS TRUSTED.** The flip comes from the board library,
    and a primitive is not trusted because it exists. Every assertion below is
    one operation of KiCad's own `FOOTPRINT::Flip`, read out of
    `pcbnew/footprint.cpp:2931-3000` in the 10.0.3 clone:

    * mirror the placement about the centre and negate the orientation, then
      turn a half circle for a `left_right` flip - so a footprint flipped about
      its own anchor does not move and 0 degrees becomes 180;
    * take the layer through the BOARD's opposite-layer table;
    * flip every pad in the footprint-local frame (`pad.cpp:1476`), which sends
      `F.Cu`/`F.Mask` to `B.Cu`/`B.Mask` and negates the local Y;
    * flip every field and graphic, which sets `mirror` on each text
      (`pcb_text.cpp:478-495`, `SetMirrored( !IsMirrored() )`).

    The last class of assertion is the one that matters most: the result is
    compared against what KiCad ITSELF writes for a back-side footprint, taken
    off a KiCad-authored board rather than off our own expectations.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.directory = a_library(self.folder)
        fplib.forget()
        self.settings = Settings(
            footprint="Fiducial_1mm_Mask2mm", library="Fixture",
            library_path=str(self.directory),
            corners=(True, True, True, False),
        )

    def tearDown(self):
        self._folder.cleanup()
        fplib.forget()

    def placed(self, side: str):
        board = Board()
        made = fiducials.apply(board, BLANK, GRID,
                               self.settings._replace(side=side), the_holes())
        return board, made

    def one_of_each_side(self):
        """One front target and one back target, from two one-sided runs."""
        front, _ = self.placed("F.Cu")
        back, _ = self.placed("B.Cu")
        return front.footprints[0], back.footprints[0]

    # -- the placement ------------------------------------------------------

    def test_the_footprint_layer_crosses_to_the_back(self):
        _, back = self.one_of_each_side()
        self.assertEqual(back.layer, "B.Cu")

    def test_the_coordinate_does_not_move(self):
        # KiCad flips ONE selected item about that item's own anchor
        # (`edit_tool.cpp:2843-2849`), so a back target sits exactly where its
        # front twin would - which is what a machine reading the panel from
        # either side is told to expect.
        front, back = self.one_of_each_side()
        self.assertEqual(back.at.position, front.at.position)

    def test_the_orientation_is_negated_and_then_turned_a_half_circle(self):
        front, back = self.one_of_each_side()
        self.assertEqual(front.at.angle, 0.0)
        self.assertEqual(back.at.angle, 180.0)

    # -- the children -------------------------------------------------------

    def test_every_pad_crosses_to_the_back_layers(self):
        front, back = self.one_of_each_side()
        self.assertEqual(list(front.pads[0].layers), ["F.Cu", "F.Mask"])
        self.assertEqual(list(back.pads[0].layers), ["B.Cu", "B.Mask"])

    def test_the_pad_keeps_its_shape_and_its_size(self):
        # A flip is a transform, not a redefinition: the copper dot the library
        # author drew is the copper dot the panel gets.
        front, back = self.one_of_each_side()
        self.assertEqual(back.pads[0].size, front.pads[0].size)
        self.assertEqual(back.pads[0].shape, front.pads[0].shape)
        self.assertEqual(back.pads[0].pad_type, front.pads[0].pad_type)

    def test_the_pad_local_position_has_its_y_negated(self):
        front, back = self.one_of_each_side()
        self.assertEqual(
            back.pads[0].position,
            (front.pads[0].position[0], -front.pads[0].position[1]),
        )

    def test_every_field_is_mirrored_and_on_a_back_layer(self):
        front, back = self.one_of_each_side()
        self.assertTrue(front.properties)
        for before, after in zip(front.properties, back.properties):
            self.assertFalse(mirrored(before))
            self.assertTrue(mirrored(after))
            self.assertEqual(after.layer, before.layer.replace("F.", "B.", 1))
            self.assertEqual(after.position,
                             (before.position[0], -before.position[1]))

    def test_every_graphic_crosses_to_the_back(self):
        front, back = self.one_of_each_side()
        self.assertTrue(front.drawings)
        for before, after in zip(front.drawings, back.drawings):
            self.assertEqual(getattr(after, "layer", None),
                             getattr(before, "layer", "").replace("F.", "B.", 1))

    def test_not_one_front_layer_survives_anywhere_in_it(self):
        # The catch-all. An item kind this fixture does not carry today would
        # fail here rather than reach a fabricator on the wrong side.
        _, back = self.one_of_each_side()
        self.assertEqual(
            [name for name in every_layer(back) if name.startswith("F.")], []
        )

    def test_it_matches_what_kicad_writes_for_a_back_side_footprint(self):
        # THE ORACLE IS KICAD'S OWN BYTES. C2 in `AUTHORED_BACK_SIDE` is a
        # footprint KiCad flipped and KiCad wrote, so it is the shape a flipped
        # footprint has to have: a `B.Cu` footprint layer, pads on `B.*` only,
        # and `mirror` set on every field.
        board = Board.from_file(AUTHORED_BACK_SIDE)
        authored = next(
            footprint for footprint in board.footprints
            if any(prop.name == "Reference"
                   and prop.value == AUTHORED_BACK_SIDE_REFERENCE
                   for prop in footprint.properties)
        )
        self.assertEqual(authored.layer, "B.Cu")
        _, ours = self.one_of_each_side()
        self.assertEqual(ours.layer, authored.layer)
        self.assertTrue(
            all(layer.startswith("B.") for layer in authored.pads[0].layers)
        )
        self.assertTrue(
            all(layer.startswith("B.") for layer in ours.pads[0].layers)
        )
        self.assertTrue(all(mirrored(p) for p in authored.properties))
        self.assertTrue(all(mirrored(p) for p in ours.properties))

    def test_the_front_side_is_untouched_by_any_of_this(self):
        # The flip runs on the PLACED copy. A top-side run has to be the panel
        # it was before the flip existed, to the byte.
        first, _ = self.placed("F.Cu")
        for footprint in first.footprints:
            self.assertEqual(footprint.layer, "F.Cu")
            self.assertEqual(footprint.at.angle, 0.0)
            self.assertFalse(any(mirrored(p) for p in footprint.properties))

    # -- both sides ---------------------------------------------------------

    def test_both_puts_one_target_on_each_side_of_every_corner(self):
        board, made = self.placed("both")
        self.assertEqual(made.count, 6)
        self.assertEqual([target.side for target in made.placed],
                         ["F.Cu"] * 3 + ["B.Cu"] * 3)
        self.assertEqual(
            [target.corner for target in made.placed],
            ["top_left", "top_right", "bottom_right"] * 2,
        )
        self.assertEqual([fp.layer for fp in board.footprints],
                         ["F.Cu"] * 3 + ["B.Cu"] * 3)

    def test_both_sides_share_a_coordinate_corner_for_corner(self):
        _, made = self.placed("both")
        top = {t.corner: t.at for t in made.placed if t.side == "F.Cu"}
        bottom = {t.corner: t.at for t in made.placed if t.side == "B.Cu"}
        self.assertEqual(top, bottom)

    def test_every_designator_on_a_two_sided_panel_is_its_own(self):
        _, made = self.placed("both")
        names = [target.designator for target in made.placed]
        self.assertEqual(names, [f"{DESIGNATOR_PREFIX}{n}" for n in range(1, 7)])

    def test_three_corners_on_both_sides_is_still_the_orientation_pattern(self):
        _, made = self.placed("both")
        self.assertTrue(made.orientation_pattern)

    # -- the identities -----------------------------------------------------

    def test_the_two_sides_of_one_corner_are_two_identities(self):
        board, _ = self.placed("both")
        identities = {footprint.uuid for footprint in board.footprints}
        self.assertEqual(len(identities), 6)

    def test_every_identity_on_a_flipped_target_is_still_derived(self):
        board, _ = self.placed("B.Cu")
        for footprint in board.footprints:
            self.assertEqual(uuid.UUID(footprint.uuid).version, 5)
            for item in (*footprint.properties, *footprint.drawings,
                         *footprint.pads):
                self.assertEqual(uuid.UUID(item.id).version, 5)

    def test_two_builds_of_a_two_sided_panel_are_one_panel(self):
        first, _ = self.placed("both")
        second, _ = self.placed("both")
        self.assertEqual(first.to_string(), second.to_string())

    def test_a_declined_corner_is_declined_on_both_sides_by_name(self):
        board = Board()
        notes = []
        made = fiducials.apply(
            board, BLANK, GRID,
            self.settings._replace(side="both",
                                   corners=(True, True, True, True)),
            the_holes(corners=(True, True, True, False)), warnings=notes,
        )
        self.assertEqual(made.count, 6)
        self.assertEqual(len(made.declined), 2)
        self.assertEqual({refused.side for refused in made.declined},
                         {"F.Cu", "B.Cu"})
        self.assertEqual(len(notes), 2)


# ---------------------------------------------------------------------------
# the config section
# ---------------------------------------------------------------------------


class ConfigSectionTest(unittest.TestCase):
    """The section is the switch, and every value in it is validated."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.config_path = write_panel_project(self.folder)
        self.data = json.loads(self.config_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self._folder.cleanup()

    def loaded(self, section=None):
        data = json.loads(json.dumps(self.data))
        if section is not None:
            data["panel"]["fiducials"] = section
        self.config_path.write_text(json.dumps(data, indent=2),
                                    encoding="utf-8")
        return load_config(self.config_path)

    def test_a_config_that_never_heard_of_fiducials_asks_for_none(self):
        # The section is always a
        # `FiducialsConfig`, defaulting to `enabled=False` - never `None` -
        # so `fiducial_settings` is the one place that reads whether the
        # stage runs.
        config = self.loaded()
        self.assertFalse(config.panel.fiducials.enabled)
        self.assertIsNone(config.panel.fiducial_settings)

    def test_the_section_present_is_the_stage_running(self):
        # A section being present does not switch the stage on: `enabled` is
        # the only switch.
        config = self.loaded({"enabled": True,
                              "footprint": "Fiducial_1mm_Mask2mm"})
        self.assertIsNotNone(config.panel.fiducial_settings)
        self.assertEqual(config.panel.fiducials.footprint,
                         "Fiducial_1mm_Mask2mm")

    def test_the_defaults_are_the_stated_ones(self):
        section = FiducialsConfig()
        self.assertFalse(section.enabled)
        self.assertEqual(section.footprint, "")
        self.assertEqual(section.library, fplib.DEFAULT_FIDUCIAL_LIBRARY)
        self.assertIsNone(section.library_path)
        self.assertEqual(section.offset_mm, 5.0)
        self.assertEqual(section.direction, "along_top_and_bottom_rails")
        self.assertEqual(section.side, "F.Cu")
        self.assertTrue(section.corners.top_left)
        self.assertFalse(section.corners.bottom_left)

    def test_a_footprint_name_carrying_a_path_is_refused(self):
        from eedesignpanels.config import ConfigError
        for bad in ("../escape", "lib/name", "C:/x"):
            with self.subTest(name=bad), self.assertRaises(ConfigError):
                self.loaded({"footprint": bad})

    def test_an_empty_library_nickname_is_refused(self):
        from eedesignpanels.config import ConfigError
        with self.assertRaises(ConfigError):
            self.loaded({"footprint": "Fiducial_1mm_Mask2mm", "library": ""})

    def test_a_direction_or_side_the_stage_cannot_build_is_refused(self):
        from eedesignpanels.config import ConfigError
        for section in ({"direction": "diagonally"}, {"side": "sideways"},
                        {"offset_mm": 0.0}, {"offset_mm": -1.0},
                        {"corners": {"middle": True}}, {"nonsense": 1}):
            with self.subTest(section=section), self.assertRaises(ConfigError):
                self.loaded({"footprint": "Fiducial_1mm_Mask2mm", **section})

    def test_every_side_the_stage_knows_loads(self):
        # All three sides build, because the board library can flip a
        # footprint. The setting uses KiCad's own copper layer names:
        # `F.Cu`, `B.Cu` or `both`.
        for side in ("F.Cu", "B.Cu", "both"):
            with self.subTest(side=side):
                config = self.loaded(
                    {"footprint": "Fiducial_1mm_Mask2mm", "side": side}
                )
                self.assertEqual(config.panel.fiducials.side, side)

    def test_the_side_setting_carries_all_three_words(self):
        from eedesignpanels.config import FiducialsConfig as Section
        self.assertEqual(
            Section.model_fields["side"].annotation.__args__,
            ("F.Cu", "B.Cu", "both"),
        )

    def test_an_empty_footprint_loads_because_the_switch_writes_it_first(self):
        # The group's switch writes the section before the user has picked
        # from the dropdown, so "" is a valid FILE and an invalid BUILD.
        config = self.loaded({"footprint": ""})
        self.assertEqual(config.panel.fiducials.footprint, "")


# ---------------------------------------------------------------------------
# the page: the group, the dropdown, and the reason there is none
# ---------------------------------------------------------------------------


class FiducialGroupTest(unittest.TestCase):
    """Step 2's fiducials group, read off the bytes the page emits."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        cls.folder = Path(cls._folder.name)
        cls.directory = a_library(cls.folder)
        cls.config_path = write_panel_project(cls.folder / "project")
        data = json.loads(cls.config_path.read_text(encoding="utf-8"))
        data["panel"]["fiducials"] = {
            "enabled": True,
            "footprint": "Fiducial_1mm_Mask2mm",
            "library": "Fixture",
            "library_path": str(cls.directory),
        }
        cls.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        fplib.forget()
        cls.session = a_session(cls.folder / "project", cls.config_path)
        cls.session.build_panel({})
        cls.markup = cls.session.panel_page()
        cls.controls = controls_of(cls.markup)
        cls.params = page_panel.params_json(cls.session.config)

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()
        fplib.forget()

    def test_the_group_is_on_the_page_with_its_own_switch(self):
        self.assertIn("id=pb-fid ", self.controls)
        self.assertIn("id=pb-fid-on", self.controls)
        self.assertIn("Fiducials", self.controls)

    def test_the_dropdown_offers_what_the_library_offers(self):
        # THE OPTION LIST ARRIVES FROM THE SERVER, and it is the resolver's
        # answer: every name that says fiducial, the lower-case one included.
        select = re.search(
            r"<select id=pb-fid-fp.*?</select>", self.controls, re.S
        )
        self.assertIsNotNone(select)
        offered = re.findall(r'<option value="([^"]*)"', select.group(0))
        self.assertEqual(
            offered,
            ["Fiducial_1mm_Mask2mm",
             "Target_FIDUCIAL_0.4mm_Variant_1789083176376",
             "fiducial_lowercase_0.5mm"],
        )
        self.assertNotIn("MountingHole_3.2mm", offered)

    def test_a_lower_case_fiducial_name_is_selectable(self):
        # Fenced on its own: the filter is
        # case-insensitive and a lower-case name reaches the dropdown.
        self.assertIn("fiducial_lowercase_0.5mm", self.params["fiducial_options"])
        self.assertIn('value="fiducial_lowercase_0.5mm"', self.controls)

    def test_the_page_is_told_where_the_list_came_from(self):
        self.assertEqual(self.params["fiducial_reason"], "")
        self.assertEqual(self.params["fiducial_directory"],
                         str(self.directory))
        self.assertTrue(self.params["fiducials_on"])

    def test_every_settable_fiducial_value_is_reported_back(self):
        # One table, both ways: everything the page may set is something the
        # page is told the value of, or a control could never be filled in.
        for name in (*actions.SETTABLE, *actions.CARRIER_SECTIONS):
            if name.startswith("fiducial"):
                with self.subTest(param=name):
                    self.assertIn(name, self.params)

    def test_the_group_and_the_switch_are_wired_in_the_page_script(self):
        self.assertIn("['pb-fid-on', 'fiducials_on', 'pb-fid-body']",
                      page_panel.PANEL_JS)
        self.assertIn("['pb-fid-fp', 'fiducial_footprint', 'name']",
                      page_panel.PANEL_JS)
        self.assertIn("FIDUCIAL_FIELDS", page_panel.PANEL_JS)

    def test_a_library_that_does_not_resolve_shows_the_reason_in_the_group(self):
        folder = Path(tempfile.mkdtemp(dir=self.folder))
        config_path = write_panel_project(folder)
        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["panel"]["fiducials"] = {
            "footprint": "Fiducial_1mm_Mask2mm",
            "library": "Fixture",
            "library_path": str(folder / "gone.pretty"),
        }
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        fplib.forget()
        params = page_panel.params_json(load_config(config_path))
        self.assertEqual(params["fiducial_options"], [])
        self.assertIn("gone.pretty", params["fiducial_reason"])
        # ...and the page has somewhere to put it.
        self.assertIn("id=pb-fid-why", self.controls)
        fplib.forget()

    def test_the_flip_direction_is_named_for_the_page_to_say(self):
        self.assertEqual(self.params["fiducial_flip_direction"],
                         FLIP_DIRECTION)

    def test_the_side_dropdown_offers_all_three_and_none_is_blocked(self):
        # A back-side target can be built, so all three words are choices and
        # nothing on the page blocks one.
        for word in ("F.Cu", "B.Cu", "both"):
            self.assertIn(f'value="{word}"', self.controls)
        self.assertNotIn("fiducial_side_blocked", page_panel.PANEL_JS)
        self.assertNotIn("FOOTPRINT::Flip", self.controls)


# ---------------------------------------------------------------------------
# the actions: the switch, the fields, and what each refuses
# ---------------------------------------------------------------------------


class FiducialActionTest(unittest.TestCase):
    """The group switch writes a section; a field needs one to be there."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.directory = a_library(self.folder)
        self.config_path = write_panel_project(self.folder / "project")
        self.data = json.loads(self.config_path.read_text(encoding="utf-8"))
        fplib.forget()

    def tearDown(self):
        self._folder.cleanup()
        fplib.forget()

    def set(self, name, value, data=None):
        return actions.set_param(data or self.data, self.config_path,
                                 name, value)

    def with_section(self):
        data = json.loads(json.dumps(self.data))
        data["panel"]["fiducials"] = {
            "enabled": True,
            "footprint": "Fiducial_1mm_Mask2mm",
            "library": "Fixture",
            "library_path": str(self.directory),
        }
        return data

    def test_the_switch_writes_the_section_and_takes_it_away(self):
        # `enabled` is the section's switch: off keeps the section and writes
        # `enabled: false`.
        on = self.set("fiducials_on", True)
        self.assertTrue(on.changed)
        self.assertIsNotNone(on.config.panel.fiducial_settings)
        off = self.set("fiducials_on", False, on.data)
        self.assertTrue(off.changed)
        self.assertIn("fiducials", off.data["panel"])
        self.assertFalse(off.data["panel"]["fiducials"]["enabled"])
        self.assertIsNone(off.config.panel.fiducial_settings)

    def test_the_switch_states_every_number_it_turned_on(self):
        # `panel.json` fully determines the panel, so the switch writes the
        # defaults out rather than an empty object.
        section = self.set("fiducials_on", True).data["panel"]["fiducials"]
        for key in ("footprint", "library", "offset_mm", "direction", "side",
                    "corners"):
            self.assertIn(key, section)

    def test_the_switch_reaches_for_a_footprint_the_library_offers(self):
        # Turning the group on has to give the user a panel with targets on
        # it, not a banner - so the switch picks the library's first fiducial.
        found = machine_library()
        if found is None:
            self.skipTest("no KiCad footprint library table on this machine")
        section = self.set("fiducials_on", True).data["panel"]["fiducials"]
        self.assertEqual(section["footprint"], found.fiducials[0])

    def test_a_field_of_a_stage_that_is_off_materialises_the_section(self):
        # Tuning a field of a stage nobody switched on writes the section -
        # `enabled: false` and all - so the value is kept and the stage stays
        # off.
        for name, value in (("fiducial_offset_mm", 3.0),
                            ("fiducial_side", "F.Cu"),
                            ("fiducial_top_left", False),
                            ("fiducial_footprint", "Fiducial_1mm_Mask2mm")):
            with self.subTest(param=name):
                changed = self.set(name, value)
                self.assertTrue(changed.changed)
                self.assertFalse(changed.data["panel"]["fiducials"]["enabled"])
                self.assertIsNone(changed.config.panel.fiducial_settings)

    def test_every_field_lands_where_the_config_keeps_it(self):
        data = self.with_section()
        for name, value, read in (
            ("fiducial_offset_mm", 3.5, lambda c: c.panel.fiducials.offset_mm),
            ("fiducial_direction", "along_left_and_right_rails",
             lambda c: c.panel.fiducials.direction),
            ("fiducial_side", "F.Cu", lambda c: c.panel.fiducials.side),
            ("fiducial_bottom_left", True,
             lambda c: c.panel.fiducials.corners.bottom_left),
            ("fiducial_footprint", "fiducial_lowercase_0.5mm",
             lambda c: c.panel.fiducials.footprint),
        ):
            with self.subTest(param=name):
                changed = self.set(name, value, data)
                self.assertEqual(read(changed.config), value)

    def test_the_footprint_field_takes_a_name_and_nothing_else(self):
        data = self.with_section()
        for bad in (1.0, True, None, ["a"], "../escape", "a/b"):
            with self.subTest(bad=bad), \
                    self.assertRaises(actions.ActionError):
                self.set("fiducial_footprint", bad, data)

    def test_a_side_or_direction_the_page_never_offered_is_refused(self):
        data = self.with_section()
        for name, bad in (("fiducial_side", "sideways"),
                          ("fiducial_direction", "diagonally"),
                          ("fiducial_side", 1)):
            with self.subTest(param=name, bad=bad), \
                    self.assertRaises(actions.ActionError):
                self.set(name, bad, data)

    def test_a_corner_takes_a_switch_and_not_a_number(self):
        data = self.with_section()
        with self.assertRaises(actions.ActionError):
            self.set("fiducial_top_left", 1, data)


# ---------------------------------------------------------------------------
# the bytes
# ---------------------------------------------------------------------------


def panel_text(config, source: Board) -> str:
    """The panel this config builds, as file text. The byte-identity gate."""
    return panelbuild.derive(config, source, fill_rails=False).outcome \
        .board.to_string()


class FiducialByteTest(unittest.TestCase):
    """Off is the panel built before the stage existed; on is reproducible."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.directory = a_library(self.folder)
        self.config_path = write_panel_project(self.folder / "project")
        self.data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.source = Board.from_file(
            load_config(self.config_path).source_board_path
        )
        fplib.forget()

    def tearDown(self):
        self._folder.cleanup()
        fplib.forget()

    def config(self, section=None):
        data = json.loads(json.dumps(self.data))
        if section is not None:
            data["panel"]["fiducials"] = {
                "enabled": True,
                "footprint": "Fiducial_1mm_Mask2mm",
                "library": "Fixture",
                "library_path": str(self.directory),
                **section,
            }
        self.config_path.write_text(json.dumps(data, indent=2),
                                    encoding="utf-8")
        return load_config(self.config_path)

    def test_no_section_is_byte_for_byte_the_panel_built_before_the_stage(self):
        # A config written before fiducials existed cannot have moved: the
        # section arriving is the stage arriving, and nothing else changed.
        plain = panel_text(self.config(), self.source)
        declined = panel_text(
            self.config({"corners": {"top_left": False, "top_right": False,
                                     "bottom_right": False,
                                     "bottom_left": False}}),
            self.source,
        )
        self.assertEqual(plain, declined)
        self.assertNotIn(DESIGNATOR_PREFIX, plain)

    def test_switching_the_stage_on_and_off_again_returns_the_same_bytes(self):
        before = panel_text(self.config(), self.source)
        on = actions.set_param(json.loads(json.dumps(self.data)),
                              self.config_path, "fiducials_on", True)
        off = actions.set_param(on.data, self.config_path,
                                "fiducials_on", False)
        self.assertEqual(panel_text(off.config, self.source), before)

    def test_two_builds_with_fiducials_are_byte_identical(self):
        config = self.config({})
        first = panel_text(config, self.source)
        second = panel_text(config, self.source)
        self.assertEqual(first, second)
        self.assertIn(f"{DESIGNATOR_PREFIX}1", first)

    def test_the_panel_carries_one_footprint_per_target(self):
        config = self.config({})
        built = panelbuild.derive(config, self.source, fill_rails=False).outcome
        made = built.panelize.fiducials
        self.assertEqual(made.count, 3)
        placed = [
            footprint for footprint in built.board.footprints
            if footprint.fp_id.startswith("Fixture:")
        ]
        self.assertEqual(len(placed), 3)

    def test_the_positions_are_the_tooling_holes_plus_the_offset(self):
        # The position check, on a real build: to the micrometre against
        # the holes the same build placed.
        config = self.config({"offset_mm": 4.0})
        built = panelbuild.derive(config, self.source, fill_rails=False).outcome
        holes = {hole.corner: hole for hole in built.panelize.holes.holes}
        for target in built.panelize.fiducials.placed:
            dx, dy = offset_vector(target.corner, DIRECTIONS[0], 4.0)
            hole = holes[target.corner]
            with self.subTest(corner=target.corner):
                self.assertAlmostEqual(target.at[0], hole.at[0] + dx, places=9)
                self.assertAlmostEqual(target.at[1], hole.at[1] + dy, places=9)

    def test_both_directions_build_and_differ(self):
        across = panel_text(self.config({"direction": DIRECTIONS[0]}),
                            self.source)
        down = panel_text(self.config({"direction": DIRECTIONS[1]}),
                          self.source)
        self.assertNotEqual(across, down)

    def test_a_fiducial_edit_is_a_carrier_edit_and_the_cache_serves_it(self):
        # THE CACHE'S OWN RULE (`panelbuild.CARRIER_SECTIONS`): a section joins
        # the exclusion list only when it can change nothing the stepped panel
        # holds, and the proof is that a kept stepped panel gives the same bytes
        # a from-scratch build does.
        first = panelbuild.derive(self.config({}), self.source,
                                  fill_rails=False)
        self.assertIsNotNone(first.cache)
        moved = self.config({"offset_mm": 3.0})
        self.assertEqual(
            panelbuild.layout_fingerprint(moved),
            panelbuild.layout_fingerprint(self.config({})),
        )
        cached = panelbuild.derive(moved, first.text and self.source,
                                   fill_rails=False, snapshot=None,
                                   cache=None)
        held = panelbuild.derive(moved, self.source, fill_rails=False,
                                 snapshot=first.cache.snapshot,
                                 cache=first.cache)
        self.assertEqual(held.outcome.board.to_string(),
                         cached.outcome.board.to_string())

    def test_the_switch_alone_does_not_move_the_stepped_panel(self):
        # The fingerprint is what decides whether the six copies are rebuilt.
        # Turning the whole stage on must not, or a fiducial edit would cost a
        # full build for nothing.
        self.assertEqual(
            panelbuild.layout_fingerprint(self.config()),
            panelbuild.layout_fingerprint(self.config({})),
        )


if __name__ == "__main__":
    unittest.main()
