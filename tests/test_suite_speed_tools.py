"""The machinery that made the suite faster, tested like anything else.

A test class shares its setup only when its tests only read it, and
`shared_setup.py`'s fence checks that, so the fence gets
its own tests here - a class that only reads passes, and a class whose test
mutates the shared state fails BY NAME. If this file ever goes red, every
shared-setup class in the suite is unguarded.

The fast runner is tested the same way: it leaves out exactly the tagged cases
and nothing else, and a test file that will not import is never dropped.
"""

import io
import os
import unittest

import run_fast_suite
import runner_tags
from shared_setup import (
    board_fingerprint,
    private_copy,
    shared_setup_must_survive_every_test,
)


class TextBoard:
    """A stand-in with the one method the fingerprint reads."""

    def __init__(self, text):
        self.text = text

    def to_string(self):
        return self.text


def run_quietly(case):
    """Run one TestCase class and hand back its result, printing nothing."""
    runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0)
    return runner.run(unittest.defaultTestLoader.loadTestsFromTestCase(case))


# ---------------------------------------------------------------------------
# the fence
# ---------------------------------------------------------------------------


class FenceTest(unittest.TestCase):
    """`shared_setup_must_survive_every_test` catches a mutating test."""

    def case_that(self, mutates):
        held = {"value": ["a"]}

        @shared_setup_must_survive_every_test(
            lambda cls: tuple(cls.shared["value"])
        )
        class Case(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.shared = held

            def test_reads_the_shared_state(self):
                self.assertEqual(self.shared["value"][0], "a")

            def test_may_or_may_not_change_it(self):
                if mutates:
                    self.shared["value"].append("b")

        return Case

    def test_a_class_that_only_reads_passes(self):
        self.assertTrue(run_quietly(self.case_that(mutates=False)).wasSuccessful())

    def test_a_class_whose_test_mutates_fails_and_names_the_class(self):
        result = run_quietly(self.case_that(mutates=True))
        self.assertFalse(result.wasSuccessful())
        message = "".join(
            text for _case, text in result.errors + result.failures
        )
        self.assertIn("CHANGED that state", message)
        self.assertIn("Case", message)

    def test_the_original_setup_and_teardown_still_run(self):
        seen = []

        @shared_setup_must_survive_every_test(lambda cls: "steady")
        class Case(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                seen.append("setup")

            @classmethod
            def tearDownClass(cls):
                seen.append("teardown")

            def test_nothing(self):
                seen.append("test")

        self.assertTrue(run_quietly(Case).wasSuccessful())
        self.assertEqual(seen, ["setup", "test", "teardown"])

    def test_teardown_still_runs_when_the_fence_fails(self):
        # The resource is released either way; the failure is reported after.
        seen = []
        held = {"value": 1}

        @shared_setup_must_survive_every_test(lambda cls: cls.held["value"])
        class Case(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.held = held

            @classmethod
            def tearDownClass(cls):
                seen.append("teardown")

            def test_changes_it(self):
                self.held["value"] = 2

        self.assertFalse(run_quietly(Case).wasSuccessful())
        self.assertEqual(seen, ["teardown"])

    def test_a_class_with_no_setup_of_its_own_is_still_fenced(self):
        @shared_setup_must_survive_every_test(lambda cls: "steady")
        class Case(unittest.TestCase):
            def test_nothing(self):
                pass

        self.assertTrue(run_quietly(Case).wasSuccessful())


class BoardFingerprintTest(unittest.TestCase):
    """The fingerprint is the board's own bytes, not a count of its items."""

    def test_the_same_bytes_give_the_same_fingerprint(self):
        self.assertEqual(board_fingerprint(TextBoard("x")),
                         board_fingerprint(TextBoard("x")))

    def test_one_character_moved_changes_it(self):
        self.assertNotEqual(board_fingerprint(TextBoard("(at 1 2)")),
                            board_fingerprint(TextBoard("(at 1 3)")))

    def test_it_is_a_plain_hex_digest(self):
        digest = board_fingerprint(TextBoard("x"))
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())
        int(digest, 16)


class PrivateCopyTest(unittest.TestCase):
    """A mutating test's own copy reaches nothing the shared one holds."""

    def test_a_nested_change_never_reaches_the_original(self):
        original = {"loops": [[1.0, 2.0]]}
        taken = private_copy(original)
        taken["loops"][0].append(3.0)
        self.assertEqual(original, {"loops": [[1.0, 2.0]]})

    def test_the_copy_starts_equal(self):
        original = {"loops": [[1.0, 2.0]]}
        self.assertEqual(private_copy(original), original)


# ---------------------------------------------------------------------------
# the tag and the fast runner
# ---------------------------------------------------------------------------


class TagTest(unittest.TestCase):
    """The tag records a reason and changes nothing about the test."""

    def test_it_records_the_reason_on_the_class(self):
        @runner_tags.runs_real_kicad("fills copper")
        class Case(unittest.TestCase):
            def test_nothing(self):
                pass

        self.assertEqual(getattr(Case, runner_tags.RUNS_REAL_KICAD),
                         "fills copper")

    def test_a_tagged_class_still_runs_and_still_passes(self):
        @runner_tags.runs_real_kicad("fills copper")
        class Case(unittest.TestCase):
            def test_nothing(self):
                self.assertTrue(True)

        result = run_quietly(Case)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.skipped), 0)

    def test_a_tag_with_no_reason_is_refused(self):
        with self.assertRaises(ValueError):
            runner_tags.runs_real_kicad("")

    def test_the_reason_is_found_on_the_method_as_well_as_the_class(self):
        class Case(unittest.TestCase):
            @runner_tags.runs_real_kicad("plots a layer")
            def test_tagged(self):
                pass

            def test_plain(self):
                pass

        self.assertEqual(runner_tags.tagged_reason(Case("test_tagged")),
                         "plots a layer")
        self.assertIsNone(runner_tags.tagged_reason(Case("test_plain")))


class FastRunnerTest(unittest.TestCase):
    """What the loop's run keeps and what it leaves out."""

    def suite_of(self, *cases):
        suite = unittest.TestSuite()
        for case in cases:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
        return suite

    def cases(self):
        @runner_tags.runs_real_kicad("fills copper")
        class Heavy(unittest.TestCase):
            def test_one(self):
                pass

            def test_two(self):
                pass

        class Light(unittest.TestCase):
            def test_one(self):
                pass

        return Heavy, Light

    def test_it_keeps_the_untagged_and_leaves_out_the_tagged(self):
        heavy, light = self.cases()
        kept, left_out = run_fast_suite.split(self.suite_of(heavy, light))
        self.assertEqual(kept.countTestCases(), 1)
        self.assertEqual(len(left_out), 2)
        self.assertTrue(all(reason == "fills copper" for _id, reason in left_out))

    def test_it_leaves_nothing_out_when_nothing_is_tagged(self):
        _heavy, light = self.cases()
        kept, left_out = run_fast_suite.split(self.suite_of(light))
        self.assertEqual(kept.countTestCases(), 1)
        self.assertEqual(left_out, [])

    def test_a_test_file_that_will_not_import_is_never_dropped(self):
        # A discovery failure carries no method to read a tag off. Dropping it
        # would turn a broken test file into a silent pass.
        broken = unittest.TestSuite([
            unittest.loader._FailedTest(
                "test_broken_on_purpose", ImportError("no such thing")
            )
        ])
        kept, left_out = run_fast_suite.split(broken)
        self.assertEqual(kept.countTestCases(), 1)
        self.assertEqual(left_out, [])

    def test_it_walks_nested_suites_to_the_individual_tests(self):
        _heavy, light = self.cases()
        nested = unittest.TestSuite([self.suite_of(light), self.suite_of(light)])
        self.assertEqual(len(list(run_fast_suite.every_test(nested))), 2)

    def test_the_real_suite_splits_into_two_non_empty_halves(self):
        here = os.path.dirname(os.path.abspath(__file__))
        discovered = unittest.defaultTestLoader.discover(here)
        kept, left_out = run_fast_suite.split(discovered)
        self.assertTrue(left_out, "nothing is tagged runs_real_kicad any more")
        self.assertGreater(kept.countTestCases(), len(left_out))
        # Everything discovered ends up on exactly one side of the split.
        self.assertEqual(
            kept.countTestCases() + len(left_out),
            discovered.countTestCases(),
        )
