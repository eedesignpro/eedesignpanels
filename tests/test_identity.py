"""Identity is a string policy, so it is tested as one - no board, no KiCad.

These are the rules that decide whether a panel of six boards is six things or
one shorted thing, and every one of them is a pure function of its arguments.
"""

import unittest

from eedesignpanels.identity import (
    collisions,
    instance_index,
    instance_key,
    net_map,
    suffixed,
)


class InstanceIndexTest(unittest.TestCase):
    def test_the_first_cell_is_one_not_zero(self):
        self.assertEqual(instance_index(0, 0, 2), 1)

    def test_the_index_runs_along_a_row_before_stepping_down(self):
        order = [instance_index(c, r, 2) for r in range(3) for c in range(2)]
        self.assertEqual(order, [1, 2, 3, 4, 5, 6])

    def test_it_matches_the_placement_order_it_numbers(self):
        from eedesignpanels.placement import grid_placements

        placements = grid_placements(3, 2, 10.0, 10.0)
        numbers = [instance_index(p.column, p.row, 3) for p in placements]
        self.assertEqual(numbers, [1, 2, 3, 4, 5, 6])

    def test_a_cell_outside_the_grid_is_an_error(self):
        with self.assertRaises(ValueError):
            instance_index(2, 0, 2)
        with self.assertRaises(ValueError):
            instance_index(0, -1, 2)
        with self.assertRaises(ValueError):
            instance_index(0, 0, 0)


class InstanceKeyTest(unittest.TestCase):
    def test_the_key_is_the_grid_cell_and_never_an_offset(self):
        self.assertEqual(instance_key(1, 2), "1|2")

    def test_every_cell_gets_its_own_key(self):
        keys = [instance_key(c, r) for r in range(3) for c in range(2)]
        self.assertEqual(len(set(keys)), 6)


class SuffixTest(unittest.TestCase):
    def test_a_name_takes_the_index(self):
        self.assertEqual(suffixed("AGND", 3), "AGND_3")

    def test_an_empty_name_stays_empty(self):
        self.assertEqual(suffixed("", 3), "")

    def test_a_hierarchical_net_name_keeps_its_path(self):
        self.assertEqual(suffixed("/Sheet/ANT_N", 2), "/Sheet/ANT_N_2")


class NetMapTest(unittest.TestCase):
    def test_every_net_is_renamed_including_the_first_instances(self):
        self.assertEqual(
            net_map(["AGND", "+V"], 1), {"AGND": "AGND_1", "+V": "+V_1"}
        )

    def test_the_empty_net_name_is_never_mapped(self):
        # "" means no net; mapping it would wire every unconnected pad in the
        # copy to one another.
        self.assertEqual(net_map(["", "AGND"], 2), {"AGND": "AGND_2"})

    def test_two_instances_never_share_a_net_name(self):
        first = set(net_map(["AGND"], 1).values())
        second = set(net_map(["AGND"], 2).values())
        self.assertEqual(first & second, set())


class CollisionTest(unittest.TestCase):
    def test_a_clean_source_has_no_collisions(self):
        self.assertEqual(collisions(["AGND", "+V"], 6), [])

    def test_a_name_that_already_looks_suffixed_collides(self):
        self.assertEqual(collisions(["AGND", "AGND_1"], 6), ["AGND"])

    def test_a_collision_beyond_the_instance_count_is_not_one(self):
        self.assertEqual(collisions(["AGND", "AGND_9"], 6), [])

    def test_empty_names_never_collide(self):
        self.assertEqual(collisions(["", "", "AGND"], 6), [])


class InstanceNamespaceTest(unittest.TestCase):
    def test_the_namespace_is_the_one_its_comment_derives(self):
        # The value is written as a literal so it cannot move with a library
        # default. This holds the literal to the text it was derived from.
        import uuid

        from eedesignpanels import identity

        self.assertEqual(
            identity.INSTANCE_UUID_NAMESPACE,
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "https://github.com/eedesignpro/eedesignpanels/panel-instance",
            ),
        )


if __name__ == "__main__":
    unittest.main()
