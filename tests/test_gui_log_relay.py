"""Tests for the GUI log relay.

The download code runs in a worker thread and emits log messages; Tk may only
be touched from the main thread. These tests pin down that messages survive the
queue, reach the log section, are buffered for a log window opened later, and
that OnlyFans messages are addressed to the OnlyFans log section.

Regression: the "Console Log" window stayed empty while OnlyFans downloads ran,
because the log window was only wired into the Fansly sections.
"""

import threading
import unittest

from gui.handlers import EventHandlers, OnlyFansEventHandlers


class FakeLogTarget:
    """Stands in for LogSection / LogWindow."""

    def __init__(self):
        self.messages = []

    def add_log(self, message, level="info"):
        self.messages.append((message, level))


class FakeWindow:
    """Minimal Tk stand-in: records scheduled callbacks."""

    def __init__(self):
        self.scheduled = []
        self.log_window = None

    def after(self, delay_ms, callback):
        self.scheduled.append(callback)

    def run_pending(self):
        pending, self.scheduled = self.scheduled, []
        for callback in pending:
            callback()


class FakeState:
    def __init__(self):
        self.config = None


def make_handlers(kind="of"):
    window = FakeWindow()
    handlers = (OnlyFansEventHandlers if kind == "of" else EventHandlers)(
        FakeState(), window
    )
    log_target = FakeLogTarget()
    handlers.set_sections({
        "log": log_target,
        "status": {"label": FakeLogTarget()},
    })
    return handlers, window, log_target


class LogRelayTests(unittest.TestCase):
    def test_message_from_worker_thread_reaches_log_section(self):
        handlers, window, log_target = make_handlers()

        thread = threading.Thread(target=handlers.on_log, args=("boom", "error"))
        thread.start()
        thread.join()

        self.assertEqual(log_target.messages, [])  # not on the worker thread
        window.run_pending()

        self.assertEqual(log_target.messages, [("boom", "error")])
        self.assertEqual(handlers.unread_errors, 1)

    def test_history_is_replayed_into_late_log_window(self):
        handlers, window, log_target = make_handlers()

        handlers.on_log("starting", "info")
        handlers.on_log("something failed", "error")
        window.run_pending()

        late_window = FakeLogTarget()
        handlers.replay_history_into(late_window)

        self.assertEqual(
            late_window.messages,
            [("starting", "info"), ("something failed", "error")],
        )

    def test_poller_reschedules_itself(self):
        handlers, window, log_target = make_handlers()

        window.run_pending()

        self.assertEqual(len(window.scheduled), 1)

    def test_onlyfans_and_fansly_share_relay_behaviour(self):
        for kind in ("of", "fansly"):
            with self.subTest(kind=kind):
                handlers, window, log_target = make_handlers(kind)

                handlers.on_log("hola", "warning")
                window.run_pending()

                self.assertEqual(log_target.messages, [("hola", "warning")])
                self.assertEqual(handlers.unread_warnings, 1)


if __name__ == "__main__":
    unittest.main()
