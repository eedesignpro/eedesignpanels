"""Which pages are open, and what it means when one stops being open.

**ONE OPEN TAB IS ENOUGH.** The session lives while at least one of its pages
is open, and closing the LAST one stops it. Leaving a server running behind no
tab at all is a process nobody can see and nobody stops, and that is the only
case worth stopping for.

Closing one tab never takes the other down: step 2 is opened from step 1 when
it is wanted, so a session with one tab is a whole session, and stopping on any
close would make the page the user is still reading vanish with no
explanation.

**The event stream is the presence signal.** Each page holds one, and the
server watches the socket rather than the clock: a closed tab drops the TCP
connection, which is noticed in the same breath. Nothing else has to be
invented - the stream is already open on both pages for the change push.

**A dropped stream is not a close, yet.** A reload and an in-tab navigation
drop it exactly the way a close does, and the difference is only visible
afterwards: the page that reloaded comes back. So a drop starts a GRACE WINDOW
and a close is declared only if nothing reconnects for that page inside it -
which is why a page says WHICH STEP it is when it subscribes (`?page=prep`),
so a reconnect can be matched to the page that went away rather than to any
page at all.

:data:`GRACE_SECONDS` is five. A reload re-establishes the stream in a few
hundred milliseconds on this project's own pages; five seconds carries a slow
first paint on a large panel with room to spare, and it is short enough that a
user who closes a tab watches the server stop rather than wondering whether it
will. It is a constructor argument so a test can state a shorter one instead
of sleeping through this one.

Two streams for one step is an ordinary state - a duplicate tab, or the moment
in a reload when the new page connects before the old socket is reaped - so
presence is COUNTED, and a step is gone only when its count reaches zero.

Nothing here talks to a socket or to a session: it is bookkeeping over an
injected clock, which is what makes the whole rule testable without waiting for
any of it.
"""

import threading

# The two pages one session serves, in the order the job is done in. A stream
# that names anything else is served and never tracked: an unknown client is
# not one of the user's tabs, and it may not stop the session by leaving.
STEP_PAGES = ("prep", "panel")

# How long a page has to come back before its dropped stream is read as a
# closed tab. See the module docstring for why it is this number.
GRACE_SECONDS = 5.0


class Lifecycle:
    """Presence per step, and the one question: has a tab actually closed?

    *pages* are the step names that participate; *grace* is the reconnect
    window in seconds. Every method takes the current monotonic time, so the
    clock belongs to the caller and a test never sleeps.
    """

    def __init__(self, pages=STEP_PAGES, grace: float = GRACE_SECONDS):
        self.pages = tuple(pages)
        self.grace = float(grace)
        self._streams = {page: 0 for page in self.pages}
        self._dropped = {}
        self._seen = set()
        self._closed = None
        self._lock = threading.Lock()

    def tracks(self, page: str) -> bool:
        """Is *page* one of the steps whose presence decides the session?"""
        return page in self._streams

    def opened(self, page: str, now: float) -> bool:
        """One more stream for *page*. Cancels any grace window it was in."""
        with self._lock:
            if page not in self._streams:
                return False
            self._streams[page] += 1
            self._dropped.pop(page, None)
            self._seen.add(page)
            return True

    def dropped(self, page: str, now: float) -> bool:
        """One fewer stream for *page*. Its last one starts the grace window.

        A grace window on one page is not a session ending: :meth:`due` also
        asks whether any OTHER page is still holding a stream.
        """
        with self._lock:
            if page not in self._streams:
                return False
            self._streams[page] = max(0, self._streams[page] - 1)
            if self._streams[page] == 0:
                self._dropped[page] = now
            return True

    def present(self) -> dict:
        """Which steps have a page holding a stream right now.

        This is what a step link asks before it opens anything: a step that is
        already open is jumped to, never opened a second time.
        """
        with self._lock:
            return {page: count > 0 for page, count in self._streams.items()}

    def seen(self, page: str) -> bool:
        """Has *page* ever connected? A page that never opened cannot close."""
        with self._lock:
            return page in self._seen

    @property
    def closed(self):
        """The step whose close was declared, or None. Latched, so it is once."""
        return self._closed

    def due(self, now: float):
        """The step whose close ended the session, or None.

        THE LAST TAB, not any tab: the answer is a step name
        only when NO page is holding a stream and the most recent drop has sat
        out the whole grace window. The window is measured from that most recent
        drop, so closing step 2 an hour ago does not shorten step 1's chance to
        come back from a reload.

        Latched: the first answer is the only answer, because the shutdown it
        triggers happens once and a watchdog asking again a quarter of a second
        later must not start a second one.
        """
        with self._lock:
            if self._closed is not None:
                return None
            if any(count > 0 for count in self._streams.values()):
                return None
            last, when = None, None
            for page in self.pages:
                at = self._dropped.get(page)
                if at is None:
                    continue
                if when is None or at > when:
                    last, when = page, at
            # No drop at all means no page has ever been open, and a page that
            # never opened cannot close.
            if when is None or now - when < self.grace:
                return None
            self._closed = last
            return last
