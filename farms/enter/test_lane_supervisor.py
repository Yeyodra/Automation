import unittest
from unittest.mock import Mock, patch

from farms.enter.lane_supervisor import (
    classify_outcome,
    normalize_domains,
    parse_blocked_domains,
    select_domain,
    terminate_process_group,
    validate_lane,
)


class LaneSupervisorTests(unittest.TestCase):
    def test_success_is_ok(self):
        self.assertEqual(classify_outcome("[1] OK user@example.test ok"), "ok")

    def test_only_explicit_domain_rejection_is_blocked(self):
        self.assertEqual(classify_outcome("domain_not_allowed: email domain is not allowed"), "domain_not_allowed")

    def test_ambiguous_failures_are_not_domain_blocks(self):
        cases = {
            "callback oauth_error=access_denied": "access_denied",
            "EMAILQU OTP timeout after 180s": "otp_timeout",
            "risk-aware gateway login not observed": "gateway_missing",
            "unexpected transport failure": "other",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_outcome(message), expected)

    def test_lane_name_rejects_path_components(self):
        self.assertEqual(validate_lane("lane-1"), "lane-1")
        for lane in ("../owned", "/tmp/owned", "a/b", ""):
            with self.subTest(lane=lane), self.assertRaises(ValueError):
                validate_lane(lane)

    def test_domain_queue_is_normalized_and_deduplicated(self):
        self.assertEqual(
            normalize_domains(["A.TEST", "a.test", " b.test ", "bad/path"]),
            ["a.test", "b.test"],
        )

    def test_blocked_domain_parser_ignores_reasons_and_comments(self):
        self.assertEqual(
            parse_blocked_domains("bad.test # explicit rejection\nother.test reason text\n"),
            {"bad.test", "other.test"},
        )

    def test_select_domain_rotates_the_selected_available_item(self):
        queue = ["cool.test", "ready.test", "later.test"]
        selected = select_domain(queue, {"cool.test": 200}, set(), now=100)
        self.assertEqual(selected, "ready.test")
        self.assertEqual(queue, ["cool.test", "later.test", "ready.test"])

    @patch("farms.enter.lane_supervisor.os.killpg")
    def test_timeout_cleanup_kills_descendants_after_leader_exits(self, killpg):
        child = Mock(pid=123)
        child.communicate.return_value = ("done", None)
        killpg.side_effect = [None, None, None]
        output = terminate_process_group(child)
        self.assertEqual(output, "done")
        self.assertEqual(killpg.call_count, 3)


if __name__ == "__main__":
    unittest.main()
