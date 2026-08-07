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

    def test_queue_returns_exact_auto_push_result(self):
        self.pusher.every_n = 1
        with patch.object(self.pusher, "_push", return_value=True) as push:
            self.assertTrue(self.pusher.queue(self.batch[0]))
        push.assert_called_once_with(self.batch)

    def test_queue_returns_none_while_batch_is_pending(self):
        self.pusher.every_n = 2
        self.assertIsNone(self.pusher.queue(self.batch[0]))
        self.assertEqual(self.pusher.stats["queued"], 1)

    def test_native_command_rejects_coerced_boolean_counts(self):
        for payload in (
            {"ok": 1, "accounts": True, "skipped": False},
            {"ok": True, "accounts": "1", "skipped": 0},
            {"ok": True, "accounts": 1, "skipped": False},
        ):
            with self.subTest(payload=payload), patch("core.ninerouter.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = json.dumps(payload)
                run.return_value.stderr = ""
                self.assertFalse(self.pusher._push_command(self.batch))

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