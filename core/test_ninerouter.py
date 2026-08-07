import json
import unittest
from unittest.mock import patch

from core.ninerouter import NinerouterPusher


class NativeCommandPushTests(unittest.TestCase):
    def setUp(self):
        self.pusher = NinerouterPusher(
            provider="enter-converge", every_n=3, host="router", user="ubuntu"
        )
        self.pusher.command = "/usr/local/bin/import-enter"
        self.pusher.key = "/key"
        self.batch = [{"email": "safe@example.invalid", "data": "{}"}]

    @patch("core.ninerouter.subprocess.run")
    def test_native_command_success_counts_imported_accounts(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({"ok": True, "accounts": 1})
        run.return_value.stderr = ""

        self.assertTrue(self.pusher._push_command(self.batch))
        self.assertEqual(self.pusher.stats, {"pushed": 1, "failed": 0, "queued": 0})
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["ubuntu@router", "/usr/local/bin/import-enter"])
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["credentials"], self.batch)

    @patch("core.ninerouter.subprocess.run")
    def test_partial_native_import_requeues_exact_batch(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({"ok": True, "accounts": 0, "skipped": 1})
        run.return_value.stderr = ""

        self.assertFalse(self.pusher._push_command(self.batch))
        self.assertEqual(self.pusher.stats, {"pushed": 0, "failed": 1, "queued": 1})
        self.assertEqual(self.pusher._queue, self.batch)

    @patch("core.ninerouter.subprocess.run")
    def test_native_command_failure_requeues_exact_batch(self, run):
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = "denied"

        self.assertFalse(self.pusher._push_command(self.batch))
        self.assertEqual(self.pusher.stats, {"pushed": 0, "failed": 1, "queued": 1})
        self.assertEqual(self.pusher._queue, self.batch)


if __name__ == "__main__":
    unittest.main()