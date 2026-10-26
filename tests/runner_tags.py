"""Tags that let a runner leave the slowest cases out, by name.

The full suite takes about ten minutes, and a development loop wants a shorter
run. A tag here does not skip anything and does not change what a test
asserts, so the full run is unchanged. It only marks a case so
`run_fast_suite.py` can leave it out of the fast run.

The cases worth marking are the ones that start a real KiCad: the zone filler
behind `kicad-cli`, the raster oracle that plots a layer to compare against, and
anything else that pays a process launch. They are 8 to 15 seconds each, and
they are also the tests this project trusts most - a mocked stand-in for KiCad
has looked right while the output was wrong here before, which is why they are
tagged rather than replaced.
"""

# The attribute a tagged test carries. Read by tests/run_fast_suite.py.
RUNS_REAL_KICAD = "eedesignpanels_runs_real_kicad"


def runs_real_kicad(reason: str):
    """Mark a test case or method as one that launches KiCad. Changes nothing.

    *reason* is the one line a reader needs: what KiCad is being asked to do.
    It is recorded on the tag so the fast runner can report what it left out.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            "runs_real_kicad needs a reason - the one line that says what "
            "KiCad is doing here, which is what the fast runner reports."
        )

    def tag(subject):
        setattr(subject, RUNS_REAL_KICAD, reason)
        return subject

    return tag


def tagged_reason(test) -> str | None:
    """The reason a test is tagged, whether the tag sits on it or its class."""
    method = getattr(test, test._testMethodName, None)
    own = getattr(method, RUNS_REAL_KICAD, None)
    if own:
        return own
    return getattr(type(test), RUNS_REAL_KICAD, None)
