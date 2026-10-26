"""The pool-sizing policy: conservative on CPU, conservative on RAM.

The pool changes when a layer is computed, never what, so nothing here is
about output. What is pinned is the conservatism: half the cores, half the
available memory, never more workers than jobs, and - when the machine will
not say how much memory it has - exactly one, because guessing at RAM is how
a review run takes a workstation down with it.
"""

import unittest

from eedesignpanels.verify.workers import (
    BYTES_PER_CANVAS_PIXEL,
    available_memory_bytes,
    default_worker_count,
)

# The corpus panel's canvas, the measured reference point for the budget.
CORPUS_PIXELS = 7203 * 7904
GIB = 1024**3


class WorkerCountTest(unittest.TestCase):
    def test_cpu_share_is_half_the_cores(self):
        count = default_worker_count(
            CORPUS_PIXELS, job_count=100, cpu_count=32,
            available_bytes=1024 * GIB,
        )
        self.assertEqual(count, 16)

    def test_ram_budget_is_half_of_what_is_available(self):
        per_worker = CORPUS_PIXELS * BYTES_PER_CANVAS_PIXEL
        # Room for exactly three workers at half of available.
        available = per_worker * 6
        count = default_worker_count(
            CORPUS_PIXELS, job_count=100, cpu_count=64,
            available_bytes=available,
        )
        self.assertEqual(count, 3)

    def test_never_more_workers_than_jobs(self):
        count = default_worker_count(
            CORPUS_PIXELS, job_count=5, cpu_count=64,
            available_bytes=1024 * GIB,
        )
        self.assertEqual(count, 5)

    def test_a_bigger_canvas_gets_fewer_workers(self):
        available = 16 * GIB
        small = default_worker_count(
            CORPUS_PIXELS, job_count=100, cpu_count=64,
            available_bytes=available,
        )
        large = default_worker_count(
            CORPUS_PIXELS * 4, job_count=100, cpu_count=64,
            available_bytes=available,
        )
        self.assertLess(large, small)

    def test_unknown_memory_means_serial(self):
        # None is the machine refusing to answer, not zero memory.
        count = default_worker_count(
            CORPUS_PIXELS, job_count=100, cpu_count=64, available_bytes=None
        )
        self.assertEqual(count, 1)

    def test_starved_memory_still_returns_one_worker(self):
        count = default_worker_count(
            CORPUS_PIXELS, job_count=100, cpu_count=64, available_bytes=1
        )
        self.assertEqual(count, 1)

    def test_one_job_never_builds_a_pool(self):
        count = default_worker_count(
            CORPUS_PIXELS, job_count=1, cpu_count=64,
            available_bytes=1024 * GIB,
        )
        self.assertEqual(count, 1)

    def test_no_jobs_never_builds_a_pool(self):
        self.assertEqual(default_worker_count(CORPUS_PIXELS, job_count=0), 1)

    def test_this_machine_answers_the_memory_question(self):
        # The Windows and POSIX paths both go through this call; whichever
        # this suite runs on must produce a plausible positive number, or the
        # auto policy silently degrades to serial everywhere.
        available = available_memory_bytes()
        self.assertIsNotNone(available)
        self.assertGreater(available, 0)


if __name__ == "__main__":
    unittest.main()
