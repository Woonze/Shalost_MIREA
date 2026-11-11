import sys
import threading
import types
import unittest
from unittest.mock import patch

import main


class ScanStop:
    def __init__(self, iterations):
        self.remaining = iterations

    def is_set(self):
        return self.remaining <= 0

    def wait(self, seconds):
        self.remaining -= 1
        return self.is_set()


class ScannerTests(unittest.TestCase):
    def run_scanner(self, tokens, responses, cooldown=False):
        app = object.__new__(main.App)
        app.stop = threading.Event()
        app.scanning = True
        app.settings = {"cooldown_minutes": 10}
        app.last_success = main.time.time() if cooldown else 0
        app.receiver = None
        app.accounts = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        app.find_presence_button = lambda image: None
        app.short = str
        app.log = lambda tag, message: logs.append((tag, message))
        app.post = lambda kind, value: events.append((kind, value))
        calls, logs, events = [], [], []
        sequence = iter(tokens)
        answers = iter(responses)
        app.approve = lambda token, account: (calls.append((token, account["id"])) or next(answers))
        scanner = types.SimpleNamespace(read_barcodes=lambda image: [types.SimpleNamespace(text=next(sequence))])
        from PIL import ImageGrab
        with patch.object(ImageGrab, "grab", return_value=object()), patch.dict(sys.modules, {"zxingcpp": scanner}):
            app.scan_worker(ScanStop(len(tokens)))
        return app, calls, logs, events

    def test_three_distinct_codes_for_all_accounts(self):
        tokens = [str(main.UUID(int=i)) for i in (1, 1, 2, 3)]
        app, calls, logs, _ = self.run_scanner(tokens, [(True, "")] * 6)
        self.assertEqual(calls, [(token, account) for token in (tokens[0], tokens[2], tokens[3]) for account in ("a", "b")])
        self.assertGreater(app.last_success, 0)
        self.assertTrue(any("3/3" in message for _, message in logs))

    def test_partial_failure_retries_only_failed_account(self):
        token = str(main.UUID(int=1))
        app, calls, _, _ = self.run_scanner([token, token], [(True, ""), (False, "ошибка"), (True, "")])
        self.assertEqual(calls, [(token, "a"), (token, "b"), (token, "b")])
        self.assertEqual(app.last_success, 0)

    def test_unauthorized_stops_scanning_and_identifies_accounts(self):
        token = str(main.UUID(int=1))
        app, calls, logs, events = self.run_scanner([token, token], [(False, "HTTP 401"), (False, "HTTP 401")])
        self.assertEqual(calls, [(token, "a"), (token, "b")])
        self.assertFalse(app.scanning)
        self.assertEqual(sum("HTTP 401" in message for _, message in logs), 2)
        self.assertTrue(any(kind == "status" and "войдите заново" in value for kind, value in events))
        self.assertIn(("scan", False), events)

    def test_cooldown_skips_qr(self):
        app, calls, _, _ = self.run_scanner(["ignored"] * 2, [], cooldown=True)
        self.assertEqual(calls, [])
        self.assertGreater(app.last_success, 0)


if __name__ == "__main__":
    unittest.main()
