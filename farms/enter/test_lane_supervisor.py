import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from farms.enter.lane_supervisor import (
    FARM_RESULTS_SENTINEL,
    claim_domain_lease,
    classify_outcome,
    global_start_wait,
    shared_heat_wait,
    trip_shared_heat,
    load_scheduler_state,
    normalize_domains,
    outcome_cooldown_seconds,
    parse_blocked_domains,
    read_terminal_outcome,
    read_terminal_status,
    resolve_blocked_file,
    secure_runtime_paths,
    select_contextual_domain,
    select_domain,
    write_private_json,
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

    def test_structured_terminal_outcome_wins_over_truncated_stdout(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.json"
            path.write_text(json.dumps({
                "version": 1,
                "attempt": 1,
                "ok": False,
                "category": "access_denied",
                "domain": "example.test",
                "stage": "callback",
                "api_key_created": False,
                "nvrouter_pushed": False,
                "at": "2026-08-07T15:00:00Z",
            }))
            self.assertEqual(read_terminal_outcome(path, "[1] FAIL callback..."), "access_denied")

    def test_structured_terminal_status_preserves_acceptance_gates(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.json"
            path.write_text(json.dumps({
                "version": 1, "attempt": 1, "ok": True, "category": "ok",
                "domain": "example.test", "stage": "complete",
                "api_key_created": True, "nvrouter_pushed": True,
                "at": "2026-08-07T15:00:00Z",
            }))
            status = read_terminal_status(path, expected_attempt=1, expected_domain="example.test")
            self.assertTrue(status["api_key_created"])
            self.assertTrue(status["nvrouter_pushed"])

    def test_terminal_status_rejects_semantically_false_success(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.json"
            base = {
                "version": 1, "attempt": 1, "ok": True, "category": "ok",
                "domain": "example.test", "stage": "complete", "api_key_created": True,
                "nvrouter_pushed": True, "at": "2026-08-07T15:00:00Z",
            }
            for change in ({"ok": False}, {"api_key_created": False}, {"nvrouter_pushed": False}):
                with self.subTest(change=change):
                    path.write_text(json.dumps(base | change))
                    with self.assertRaises(ValueError):
                        read_terminal_status(path, expected_attempt=1, expected_domain="example.test")

    def test_terminal_status_rejects_wrong_types_or_identity(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.json"
            base = {
                "version": 1, "attempt": 1, "ok": False, "category": "access_denied",
                "domain": "example.test", "stage": "callback", "api_key_created": False,
                "nvrouter_pushed": False, "at": "2026-08-07T15:00:00Z",
            }
            for change in (
                {"ok": "false"}, {"api_key_created": "false"},
                {"nvrouter_pushed": "false"}, {"attempt": 2}, {"domain": "other.test"},
            ):
                with self.subTest(change=change):
                    path.write_text(json.dumps(base | change))
                    with self.assertRaises(ValueError):
                        read_terminal_status(path, expected_attempt=1, expected_domain="example.test")

    def test_invalid_success_artifact_cannot_fall_back_to_stdout_ok(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.json"
            path.write_text(json.dumps({
                "version": 1, "attempt": 1, "ok": True, "category": "ok",
                "domain": "example.test", "stage": "complete",
                "api_key_created": True, "nvrouter_pushed": False,
                "at": "2026-08-07T15:00:00Z",
            }))
            self.assertEqual(
                read_terminal_outcome(
                    path, "[1] OK account created", expected_attempt=1,
                    expected_domain="example.test",
                ),
                "nvrouter_push_failed",
            )

    def test_missing_or_corrupt_terminal_outcome_falls_back_to_stdout(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.json"
            self.assertEqual(read_terminal_outcome(path, "oauth_error=access_denied"), "access_denied")
            path.write_text("not-json")
            self.assertEqual(read_terminal_outcome(path, "EMAILQU OTP timeout"), "otp_timeout")

    def test_terminal_outcome_rejects_unknown_category_and_secret_fields(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "terminal.json"
            for payload in (
                {"version": 1, "category": "made_up"},
                {"version": 1, "category": "access_denied", "email": "secret@example.test"},
                {"version": 1, "category": "access_denied", "api_key": "secret"},
            ):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload))
                    self.assertEqual(read_terminal_outcome(path, "unexpected transport failure"), "other")

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

    def test_shared_heat_breaker_pauses_both_lanes_after_denial(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "shared-heat.json"
            self.assertEqual(shared_heat_wait(path, now=1000), 0)
            trip_shared_heat(path, 300, now=1000)
            self.assertEqual(shared_heat_wait(path, now=1010), 0)
            trip_shared_heat(path, 300, now=1030)
            self.assertEqual(shared_heat_wait(path, now=1100), 230)
            trip_shared_heat(path, 300, now=1331)
            self.assertEqual(shared_heat_wait(path, now=1335), 0)
            trip_shared_heat(path, 300, now=1340)
            self.assertEqual(shared_heat_wait(path, now=1340), 600)

    def test_cross_lane_domain_lease_prevents_identity_collision(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "leases.json"
            self.assertTrue(claim_domain_lease(path, "same.test", "lane-a", 300, now=1000))
            self.assertFalse(claim_domain_lease(path, "same.test", "lane-b", 300, now=1100))
            self.assertTrue(claim_domain_lease(path, "other.test", "lane-b", 300, now=1100))
            self.assertTrue(claim_domain_lease(path, "same.test", "lane-b", 300, now=1301))

    def test_global_start_gate_calculates_cross_lane_wait_atomically(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "global-start.lock"
            self.assertEqual(global_start_wait(path, 75, now=1000), 0)
            self.assertEqual(global_start_wait(path, 75, now=1030), 45)
            self.assertEqual(global_start_wait(path, 75, now=1075), 0)

    def test_rate_limit_uses_short_contextual_cooldown(self):
        self.assertEqual(outcome_cooldown_seconds("rate_limited", 900, 120), 120)
        self.assertEqual(outcome_cooldown_seconds("access_denied", 900, 120), 900)
        self.assertEqual(outcome_cooldown_seconds("ok", 900, 120), 0)

    def test_contextual_scheduler_prefers_recent_lane_success_without_blacklisting(self):
        queue = ["bad.test", "good.test", "unknown.test"]
        stats = {
            "bad.test": {"ok": 0, "ambiguous": 5, "recent": [0, 0, 0, 0, 0]},
            "good.test": {"ok": 4, "ambiguous": 2, "recent": [1, 0, 1, 1, 1]},
        }
        selected = select_contextual_domain(
            queue, {}, set(), now=1000, stats=stats, attempt=1,
            last_used={}, min_reuse=120, explore_every=5,
        )
        self.assertEqual(selected, "good.test")
        self.assertIn("bad.test", queue)

    def test_contextual_scheduler_explores_on_bounded_cadence(self):
        queue = ["unknown.test", "good.test"]
        stats = {"good.test": {"ok": 8, "ambiguous": 2, "recent": [1, 1, 1, 0, 1]}}
        selected = select_contextual_domain(
            queue, {}, set(), now=1000, stats=stats, attempt=5,
            last_used={}, min_reuse=120, explore_every=5,
        )
        self.assertEqual(selected, "unknown.test")

    def test_contextual_scheduler_explores_low_sample_known_domain(self):
        queue = ["low-sample.test", "good.test"]
        stats = {
            "low-sample.test": {"ok": 0, "ambiguous": 1, "recent": [0]},
            "good.test": {"ok": 8, "ambiguous": 2, "recent": [1, 1, 1, 0, 1]},
        }
        selected = select_contextual_domain(
            queue, {}, set(), now=1000, stats=stats, attempt=5,
            last_used={}, min_reuse=120, explore_every=5,
        )
        self.assertEqual(selected, "low-sample.test")

    def test_contextual_scheduler_honors_reuse_guard_and_cooldown(self):
        queue = ["hot.test", "ready.test"]
        stats = {"hot.test": {"ok": 10, "ambiguous": 0, "recent": [1] * 5}}
        selected = select_contextual_domain(
            queue, {"ready.test": 1100}, set(), now=1000, stats=stats, attempt=1,
            last_used={"hot.test": 950}, min_reuse=120, explore_every=5,
        )
        self.assertIsNone(selected)

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

    def test_blocklist_defaults_to_farm_results_and_accepts_env_override(self):
        self.assertEqual(
            resolve_blocked_file({}),
            FARM_RESULTS_SENTINEL,
        )
        self.assertEqual(
            resolve_blocked_file({"ENTER_BLOCKED_DOMAINS_FILE": "/tmp/custom-blocked.txt"}),
            Path("/tmp/custom-blocked.txt"),
        )

    def test_scheduler_state_rejects_valid_json_with_wrong_shapes(self):
        bad = (
            {"attempt": "bad", "domains": {}, "last_used": {}, "cooldowns": {}},
            {"attempt": 1, "domains": [], "last_used": {}, "cooldowns": {}},
            {"attempt": 1, "domains": {}, "last_used": [], "cooldowns": {}},
            {"attempt": 1, "domains": {}, "last_used": {}, "cooldowns": []},
        )
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(load_scheduler_state(payload), (0, {}, {}, {}))

    def test_scheduler_state_rejects_nonfinite_or_overflow_timestamps(self):
        for value in (float("inf"), 10 ** 10000):
            with self.subTest(kind=type(value).__name__):
                payload = {"attempt": 1, "domains": {}, "last_used": {}, "cooldowns": {"x.test": value}}
                self.assertEqual(load_scheduler_state(payload), (0, {}, {}, {}))

    def test_private_json_write_is_atomic_and_repairs_permissions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            path = root / "stats.json"
            write_private_json(path, {"version": 1})
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"version": 1})
            self.assertFalse(list(root.glob(".stats.json.*")))

    def test_runtime_paths_are_private(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "lane-a-state.json"
            log = root / "production-lane-a.log"
            secure_runtime_paths(root, state, log)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
