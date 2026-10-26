"""A check reports a disagreement; it does not raise on one."""

import unittest

from eedesignpanels.checks import compare_extents, svg_extent_mm

HEADER = (
    '<svg xmlns="http://www.w3.org/2000/svg" version="1.1"\n'
    '  width="33.9852mm" height="37.4904mm" viewBox="0 0 100 100">'
)


class SvgExtentTest(unittest.TestCase):
    def test_reads_the_millimetre_size_kicad_cli_writes(self):
        self.assertEqual(svg_extent_mm(HEADER), (33.9852, 37.4904))

    def test_a_document_without_a_mm_size_is_an_error(self):
        with self.assertRaises(ValueError):
            svg_extent_mm('<svg width="900" height="900"></svg>')


class CompareExtentsTest(unittest.TestCase):
    def test_an_exact_match_agrees_with_no_deviation(self):
        check = compare_extents((34.0, 37.5), (34.0, 37.5))
        self.assertEqual(check.deviation_mm, (0.0, 0.0))
        self.assertTrue(check.agrees)

    def test_the_arc_approximation_noise_still_agrees(self):
        check = compare_extents((34.0, 37.5), (33.9852, 37.4904))
        self.assertTrue(check.agrees)
        self.assertAlmostEqual(check.deviation_mm[0], -0.0148)

    def test_a_deviation_past_the_tolerance_disagrees(self):
        check = compare_extents((34.0, 37.5), (37.0, 37.5))
        self.assertFalse(check.agrees)
        self.assertIn("DISAGREE", check.summary)

    def test_either_axis_can_disagree_on_its_own(self):
        self.assertFalse(compare_extents((34.0, 37.5), (34.0, 40.0)).agrees)

    def test_the_tolerance_is_a_parameter(self):
        self.assertTrue(compare_extents((34.0, 37.5), (34.5, 37.5), 1.0).agrees)

    def test_a_disagreement_is_reported_not_raised(self):
        check = compare_extents((34.0, 37.5), (99.0, 99.0))
        self.assertIn("predicted", check.summary)
        self.assertIn("rendered", check.summary)


if __name__ == "__main__":
    unittest.main()
