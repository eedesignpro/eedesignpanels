"""The command line writes UTF-8 when its output goes to a pipe or a file.

The build report prints the diameter sign, as in "4x ⌀3.175". On Windows a
redirected stream used the console code page, which has no such character, and
the run stopped with `UnicodeEncodeError` after the panel files were written.
"""

import io
import os
import subprocess
import sys
import unittest

from eedesignpanels import cli

DIAMETER_TEXT = "4x ⌀3.175"


class Utf8OutputTest(unittest.TestCase):

    def test_a_code_page_stream_becomes_utf8(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")
        cli.use_utf8_output([stream])
        stream.write(DIAMETER_TEXT)
        stream.flush()
        self.assertEqual(raw.getvalue(), DIAMETER_TEXT.encode("utf-8"))

    def test_a_stream_that_cannot_change_is_left_alone(self):
        stream = io.StringIO()
        cli.use_utf8_output([stream])
        stream.write(DIAMETER_TEXT)
        self.assertEqual(stream.getvalue(), DIAMETER_TEXT)

    def test_a_redirected_run_prints_the_diameter_sign(self):
        # The same interpreter, its output a pipe, its encoding forced to the
        # Windows code page that refused the character.
        env = {key: value for key, value in os.environ.items()
               if key not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env["PYTHONIOENCODING"] = "cp1252"
        done = subprocess.run(  # noqa: S603 - this interpreter, no shell
            [sys.executable, "-c",
             "from eedesignpanels import cli; cli.use_utf8_output(); "
             f"print({DIAMETER_TEXT!r})"],
            capture_output=True, env=env, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.decode("utf-8").strip(), DIAMETER_TEXT)


if __name__ == "__main__":
    unittest.main()
