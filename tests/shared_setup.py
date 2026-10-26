"""Share one expensive setup across a class, and PROVE no test changed it.

A board shared between tests that one test then mutates corrupts every later
test, and the failure shows up as a geometry bug somewhere else entirely. So a
class that shares its setup also checks that the setup was not changed.

The proof is arithmetic and it runs on its own. A class that shares its setup
wears :func:`shared_setup_must_survive_every_test`, which takes a fingerprint of
the shared thing after `setUpClass` and takes it again after the last test in
the class. A test that changed so much as one coordinate fails the class by
name, at the class's own teardown, with the two fingerprints in the message.

The fingerprint is the board's own serialization, so it covers every byte the
build would write - not a count of items, which would miss a coordinate moved in
place. It costs one serialization at each end of a class (about 0.16 s a side on
the corpus board) and it buys back the per-test load it replaces.

A test that needs to mutate does not share: it takes its own copy through
:func:`private_copy`, which is what the fence leaves it free to do.
"""

import copy
import hashlib

_RECORDED = "eedesignpanels_shared_setup_fingerprint"


def board_fingerprint(board) -> str:
    """Every byte the board would be written as, reduced to one digest."""
    return hashlib.sha256(board.to_string().encode("utf-8")).hexdigest()


def private_copy(board):
    """A board of this test's own, so mutating it can reach nothing else."""
    return copy.deepcopy(board)


def shared_setup_must_survive_every_test(fingerprint):
    """Class decorator: fence the shared state this class built in setUpClass.

    *fingerprint* is called with the class and returns a value that changes
    whenever the shared state changes. Use :func:`board_fingerprint` for a
    board.
    """

    def fence(cls):
        original_setup = cls.setUpClass.__func__
        original_teardown = cls.tearDownClass.__func__

        def setUpClass(inner_cls):
            original_setup(inner_cls)
            setattr(inner_cls, _RECORDED, fingerprint(inner_cls))

        def tearDownClass(inner_cls):
            before = getattr(inner_cls, _RECORDED, None)
            try:
                after = fingerprint(inner_cls)
            finally:
                original_teardown(inner_cls)
            if before != after:
                raise AssertionError(
                    f"{inner_cls.__name__} shares its setUpClass state and a "
                    f"test in it CHANGED that state, so every test after the "
                    f"one that did it read something the class never set up.\n"
                    f"  at setUpClass: {before}\n"
                    f"  after the last test: {after}\n"
                    f"Give the mutating test its own copy - "
                    f"tests.shared_setup.private_copy - or move this class "
                    f"back to a per-test setUp."
                )

        cls.setUpClass = classmethod(setUpClass)
        cls.tearDownClass = classmethod(tearDownClass)
        return cls

    return fence
