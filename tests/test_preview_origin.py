"""Only the preview server's own page may drive it.

The server listens on the loopback address, and its `Host` check stops a page
that points a host name at 127.0.0.1. Neither stops a page on another site in
the same browser from sending a plain POST to `http://127.0.0.1:<port>/save`.
Two checks close that, and both are pinned here:

* **The origin.** A request that names an `Origin` must name this server's own
  address. A foreign site, and `null`, are refused.
* **The session token.** Every POST must carry the token this server wrote into
  the page it served. A POST without it, or with another one, is refused.

A refused request changes nothing: the design file keeps its bytes and the
server keeps running.
"""

import json
import re
import unittest
import urllib.error
import urllib.request

from eedesignpanels.preview import server as server_module
from test_preview import ServerTestCase, digest

TOKEN_TAG = re.compile(r'<meta name=pb-token content="([^"]+)">')


class OriginAndTokenTest(ServerTestCase):
    """Requests from the served page pass. Everything else gets 403."""

    def own_origin(self) -> str:
        return self.url.rstrip("/")

    def headers(self, origin=None, token=None) -> dict:
        sent = {"Content-Type": "application/json"}
        if origin is not None:
            sent["Origin"] = origin
        if token is not None:
            sent[server_module.TOKEN_HEADER] = token
        return sent

    def still_running(self) -> bool:
        status, _ = self.get("state")
        return status == 200

    # -- the page carries the token ------------------------------------------

    def test_both_pages_carry_this_server_s_token(self):
        for path in ("", "panel"):
            with self.subTest(page=path or "prep"):
                _, markup = self.get(path)
                found = TOKEN_TAG.search(markup)
                self.assertIsNotNone(found)
                self.assertEqual(found.group(1), self.server.token)

    def test_the_page_script_sends_the_token_with_every_post(self):
        _, markup = self.get("")
        self.assertIn("'X-Eedesignpanels-Token': PB_TOKEN", markup)
        self.assertNotIn("headers: {'Content-Type': 'application/json'}",
                         markup)

    def test_the_token_is_long_and_never_in_the_state_answer(self):
        self.assertGreaterEqual(len(self.server.token), 32)
        _, state = self.get("state")
        self.assertNotIn(self.server.token, state)

    # -- what passes -----------------------------------------------------------

    def test_the_page_s_own_post_passes(self):
        status, reply = self.post(
            "layers", {"page": "prep", "mode": 0},
            headers=self.headers(self.own_origin(), self.server.token))
        self.assertEqual(status, 200, reply)

    def test_a_client_that_is_not_a_page_passes_with_the_token(self):
        status, _ = self.post(
            "layers", {"page": "prep", "mode": 0},
            headers=self.headers(None, self.server.token))
        self.assertEqual(status, 200)

    # -- what is refused ------------------------------------------------------

    def test_a_post_from_a_foreign_origin_is_refused(self):
        before = digest(self.board_path)
        status, reply = self.post(
            "save", {}, headers=self.headers("https://example.com",
                                             self.server.token))
        self.assertEqual(status, 403)
        self.assertIn("another web page", reply["error"])
        self.assertEqual(digest(self.board_path), before)

    def test_a_post_from_a_null_origin_is_refused(self):
        status, _ = self.post(
            "save", {}, headers=self.headers("null", self.server.token))
        self.assertEqual(status, 403)

    def test_a_post_from_another_loopback_port_is_refused(self):
        port = self.server.server_address[1] + 1
        status, _ = self.post(
            "save", {}, headers=self.headers(f"http://127.0.0.1:{port}",
                                             self.server.token))
        self.assertEqual(status, 403)

    def test_a_post_with_no_token_is_refused(self):
        before = digest(self.board_path)
        status, reply = self.post(
            "save", {}, headers=self.headers(self.own_origin(), None))
        self.assertEqual(status, 403)
        self.assertIn("session token", reply["error"])
        self.assertEqual(digest(self.board_path), before)

    def test_a_post_with_another_token_is_refused(self):
        status, _ = self.post(
            "save", {}, headers=self.headers(self.own_origin(), "not-it"))
        self.assertEqual(status, 403)

    def test_a_shutdown_without_the_token_stops_nothing(self):
        # The attack the assessment named: a plain cross-site POST to
        # /shutdown, as `fetch(..., {mode: 'no-cors'})` would send it.
        status, _ = self.post(
            "shutdown", {}, headers={"Content-Type": "text/plain",
                                     "Origin": "https://example.com"})
        self.assertEqual(status, 403)
        status, _ = self.post("shutdown", {}, headers={
            "Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        self.assertTrue(self.still_running())
        self.assertFalse(self.server.stopping)

    def test_a_large_refused_body_still_gets_its_answer(self):
        request = urllib.request.Request(
            self.url + "save", method="POST",
            data=b"{}" + b" " * (server_module.MAX_BODY_BYTES * 4),
            headers=self.headers("https://example.com", self.server.token),
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        with caught.exception as error:
            self.assertEqual(error.code, 403)
            self.assertFalse(json.loads(error.read().decode("utf-8"))["ok"])

    def test_a_get_from_a_foreign_origin_is_refused(self):
        request = urllib.request.Request(
            self.url, headers={"Origin": "https://example.com"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        with caught.exception as error:
            self.assertEqual(error.code, 403)

    def test_the_host_check_still_stands(self):
        request = urllib.request.Request(
            self.url + "save", method="POST", data=b"{}",
            headers={**self.headers(None, self.server.token),
                     "Host": "attacker.example"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        with caught.exception as error:
            self.assertEqual(error.code, 403)
            self.assertIn("loopback",
                          json.loads(error.read().decode("utf-8"))["error"])


if __name__ == "__main__":
    unittest.main()
