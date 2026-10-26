"""The loop's test run: everything except the cases that drive KiCad itself.

The fast run is for the development loop; the full run is the complete check.
This runner is a separate command with a separate name, not a switch on the
full one, because a switch set in one shell window and forgotten lets a fast
run pass for a full one.

    full:  <python> -m unittest discover -s tests
    fast:  <python> tests/run_fast_suite.py

It leaves out exactly the tests tagged `runs_real_kicad` in `runner_tags.py` and
says how many and why. It changes no assertion and skips nothing else.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runner_tags import tagged_reason  # noqa: E402  (after the path insert)


def every_test(suite):
    """Walk a discovered suite down to the individual test cases."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from every_test(item)
        else:
            yield item


def split(suite):
    """`(kept, left_out)` - left_out is the tagged cases and their reasons."""
    kept = unittest.TestSuite()
    left_out = []
    for test in every_test(suite):
        # A discovery failure is a _FailedTest with no real method behind it.
        # It is never tagged and it must always run, so the fast run reports a
        # test file that will not import rather than quietly dropping it.
        reason = None
        if not isinstance(test, unittest.loader._FailedTest):
            reason = tagged_reason(test)
        if reason:
            left_out.append((test.id(), reason))
        else:
            kept.addTest(test)
    return kept, left_out


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    suite = unittest.defaultTestLoader.discover(here)
    kept, left_out = split(suite)

    reasons = sorted({reason for _id, reason in left_out})
    # To stderr, where TextTestRunner writes, so the header stays above the
    # results instead of being reordered by two separate buffers.
    say = sys.stderr
    print(f"FAST RUN - {kept.countTestCases()} tests.", file=say)
    print(f"Left out: {len(left_out)} that start a real KiCad -", file=say)
    for reason in reasons:
        print(f"  {reason}", file=say)
    print("This is NOT the release gate. The gate is:", file=say)
    print(f'  "{sys.executable}" -m unittest discover -s tests', file=say)
    print(file=say)
    say.flush()

    result = unittest.TextTestRunner(verbosity=1).run(kept)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
