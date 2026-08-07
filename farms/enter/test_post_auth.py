import importlib.util
import ast
import inspect
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_farm():
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    camoufox = types.ModuleType("camoufox")
    async_api = types.ModuleType("camoufox.async_api")
    async_api.AsyncCamoufox = object
    sys.modules["dotenv"] = dotenv
    sys.modules.setdefault("camoufox", camoufox)
    sys.modules.setdefault("camoufox.async_api", async_api)
    path = Path(__file__).with_name("farm.py")
    spec = importlib.util.spec_from_file_location("enter_post_auth_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PostAuthSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.farm = _load_farm()

    def test_v2_setup_requires_verified_user_workspace_completion_and_key(self):
        responses = [
            {"code": 0, "data": None},
            {"code": 0, "data": {"must_verify_email": False, "merge_action": None}},
            {"code": 0, "data": {"workspaces": [{"id": "9007199254740993"}]}},
            {"code": 0, "data": {"flow_version": "v2", "completed": False}},
            {"code": 0, "data": {"success": True, "completed": True, "flow_version": "v2"}},
            {"code": 0, "data": {"id": "key-id", "key": "ek_value", "name": "farm"}},
            {"code": 0, "data": {}},
        ]
        with patch.object(self.farm, "_api_json", side_effect=responses) as api:
            result = self.farm.enter_post_auth_setup("access", "gift")

        self.assertEqual(result["workspace_id"], "9007199254740993")
        self.assertEqual(result["api_key"]["data"]["key"], "ek_value")
        calls = [(c.args[0], c.args[1], c.kwargs.get("body")) for c in api.call_args_list]
        self.assertEqual(
            calls[:6],
            [
                ("POST", "/code/api/v1/referral/claim", None),
                ("GET", "/code/api/v1/users/info", None),
                ("GET", "/code/api/v1/workspaces", None),
                ("GET", "/code/api/v1/onboarding/config", None),
                ("POST", "/code/api/v1/onboarding/complete", {
                    "role": "founder",
                    "industry": "manufacturing",
                    "team_size": "6-20",
                    "build_intent": self.farm.BUILD_INTENT,
                    "agency_service_interest": "in_house",
                }),
                ("POST", "/code/api/v1/workspaces/9007199254740993/api-keys", {
                    "name": self.farm.API_KEY_NAME,
                    "scope": self.farm.API_KEY_SCOPE,
                    "reveal_policy": self.farm.API_KEY_REVEAL,
                }),
            ],
        )

    def test_referral_rejection_is_nonfatal_and_api_key_is_still_created(self):
        responses = [
            RuntimeError("API POST /code/api/v1/referral/claim -> 403"),
            {"code": 0, "data": {"must_verify_email": False, "merge_action": None}},
            {"code": 0, "data": {"workspaces": [{"id": "1"}]}},
            {"code": 0, "data": {"flow_version": "v2", "completed": True}},
            {"code": 0, "data": {"id": "key-id", "key": "ek_value"}},
            {"code": 0, "data": {}},
        ]
        with patch.object(self.farm, "_api_json", side_effect=responses):
            result = self.farm.enter_post_auth_setup("access", "gift")
        self.assertIn("referral_claim_error", result)
        self.assertEqual(result["api_key"]["data"]["key"], "ek_value")

    def test_empty_api_key_or_id_is_rejected(self):
        common = [
            {"code": 0, "data": None},
            {"code": 0, "data": {"must_verify_email": False}},
            {"code": 0, "data": {"workspaces": [{"id": "1"}]}},
            {"code": 0, "data": {"flow_version": "v2", "completed": True}},
        ]
        for key_data in ({"id": "", "key": "ek_value"}, {"id": "key-id", "key": ""}):
            with self.subTest(key_data=key_data):
                with patch.object(self.farm, "_api_json", side_effect=common + [{"code": 0, "data": key_data}]):
                    with self.assertRaises(RuntimeError):
                        self.farm.enter_post_auth_setup("access", "gift")

    def test_auto_link_without_candidate_does_not_block_setup(self):
        responses = [
            {"code": 0, "data": None},
            {"code": 0, "data": {"must_verify_email": False, "merge_action": "auto_link", "merge_candidate_id": None, "merge_block_reason": None}},
            {"code": 0, "data": {"workspaces": [{"id": "1"}]}},
            {"code": 0, "data": {"flow_version": "v2", "completed": True}},
            {"code": 0, "data": {"id": "key-id", "key": "ek_value"}},
            {"code": 0, "data": {}},
        ]
        with patch.object(self.farm, "_api_json", side_effect=responses):
            result = self.farm.enter_post_auth_setup("access", "gift")
        self.assertEqual(result["api_key"]["data"]["key"], "ek_value")

    def test_pending_merge_candidate_is_rejected(self):
        for block_reason in (None, ""):
            with self.subTest(block_reason=block_reason), patch.object(self.farm, "_api_json", side_effect=[
                {"code": 0, "data": None},
                {"code": 0, "data": {"must_verify_email": False, "merge_action": "candidate_pending", "merge_candidate_id": "candidate", "merge_block_reason": block_reason}},
            ]):
                with self.assertRaises(RuntimeError):
                    self.farm.enter_post_auth_setup("access", "gift")

    def test_registration_does_not_log_api_key_material(self):
        source = inspect.getsource(self.farm._do_register_body)
        tree = ast.parse(source)
        logs = [
            ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "alog"
        ]
        self.assertTrue(logs)
        self.assertFalse(any("api_data.get('key')" in log for log in logs))

    def test_runner_environment_wins_over_farm_dotenv(self):
        source = Path(self.farm.__file__).read_text(encoding="utf-8")
        self.assertIn('load_dotenv(_ROOT / ".env", override=False)', source)

    def test_disposable_callback_access_denied_is_not_domain_rejection(self):
        message = (
            "Enter auth callback not reached: host=enter.converge.ai path=/auth/callback "
            "oauth_error=access_denied banner=access_denied"
        )
        for mode in ("emailqu", "rotate", "tempmail"):
            with self.subTest(mode=mode):
                self.assertFalse(self.farm._is_domain_rejection(mode, message))
                self.assertEqual(self.farm._disposable_retry_action(mode, message), "stop")
                self.assertFalse(
                    self.farm._is_domain_rejection(
                        mode,
                        "host=auth.converge.ai path=/u/login/password banner=password_stalled",
                    )
                )

    def test_non_disposable_mode_does_not_blame_domain_for_access_denied(self):
        message = "oauth_error=access_denied banner=access_denied"
        for mode in ("plus_trick", "domain"):
            with self.subTest(mode=mode):
                self.assertFalse(self.farm._is_domain_rejection(mode, message))

    def test_disposable_retry_covers_mailbox_and_risk_transients(self):
        self.assertEqual(
            self.farm._disposable_retry_action("rotate", "ROTATE OTP timeout after 180s"),
            "block",
        )
        self.assertEqual(
            self.farm._disposable_retry_action(
                "rotate", "risk-aware gateway login not observed"
            ),
            "retry",
        )
        self.assertEqual(
            self.farm._disposable_retry_action(
                "plus_trick", "ROTATE OTP timeout after 180s"
            ),
            "stop",
        )

    def test_tempmail_receives_bounded_domain_retries(self):
        self.assertEqual(self.farm._domain_retry_count("tempmail", 8), 4)
        self.assertEqual(self.farm._domain_retry_count("plus_trick", 8), 1)

    def test_production_rotation_is_limited_to_four_mailbox_backends(self):
        providers = self.farm._rotation_candidates()
        self.assertEqual(
            providers,
            ["mail.tm", "tempmail.io", "guerrillamail", "emailqu"],
        )

    def test_guerrillamail_uses_browser_headers(self):
        class Opener:
            requests = []

            def open(inner_self, request, timeout):
                inner_self.requests.append(request)
                return io.BytesIO(json.dumps({
                    "sid_token": "sid", "email_addr": "a@example.test"
                }).encode())

        opener = Opener()
        with patch.object(self.farm.urllib.request, "build_opener", return_value=opener):
            self.farm._create_guerrillamail()
        self.assertEqual(len(opener.requests), 2)
        for request in opener.requests:
            self.assertIn("Mozilla/5.0", request.get_header("User-agent"))
            self.assertEqual(request.get_header("Accept"), "application/json")

    def test_rotation_retries_a_provider_that_returns_blocked_domain(self):
        self.farm.TEMPMAIL_ROTATION = ("tempmail.io",)
        self.farm._gptmail_blocked_domains = {"blocked.test"}
        with patch.object(
            self.farm,
            "_create_tempmail_io",
            side_effect=[("one@blocked.test", "token1"), ("two@fresh.test", "token2")],
        ) as create:
            self.assertEqual(self.farm.create_rotating_inbox(), "two@fresh.test")
        self.assertEqual(create.call_count, 2)

    def test_emailqu_canary_can_pin_a_known_good_apex_domain(self):
        self.farm.EMAILQU_DOMAIN = "known-good.test"
        with patch.object(self.farm, "_emailqu_apex_domains", return_value=["known-good.test"]), \
             patch.object(self.farm, "_emailqu_get", side_effect=[
                 (200, {"username": "Canary-1"}, ""),
                 (200, {"verified": True}, ""),
             ]):
            self.assertEqual(self.farm.create_emailqu_inbox(), "canary1@known-good.test")

    def test_emailqu_custom_prefix_uses_unique_local_part_without_username_api(self):
        with patch.object(self.farm, "EMAILQU_PREFIX", "nazril"), \
             patch.object(self.farm, "EMAILQU_DOMAIN", "known-good.test"), \
             patch.object(self.farm, "_crypto_local_part", return_value="abcdefghij"), \
             patch.object(self.farm, "_emailqu_apex_domains", return_value=["known-good.test"]), \
             patch.object(self.farm, "_emailqu_get", return_value=(200, {"verified": True}, "")) as get:
            self.assertEqual(self.farm.create_emailqu_inbox(), "nazrilabcdefghij@known-good.test")
        self.assertEqual(get.call_count, 1)
        self.assertIn("/api/domain/verify/", get.call_args.args[0])

    def test_emailqu_pin_must_still_be_a_public_apex_domain(self):
        self.farm.EMAILQU_DOMAIN = "sub.example.test"
        with patch.object(self.farm, "_emailqu_apex_domains", return_value=["example.test"]), \
             patch.object(self.farm, "_emailqu_get", return_value=(200, {"username": "canary"}, "")):
            with self.assertRaisesRegex(RuntimeError, "public apex"):
                self.farm.create_emailqu_inbox()

    def test_emailqu_network_failure_retries_through_browser_proxy(self):
        self.farm._proxy_pool = [("socks5://127.0.0.1:40001", "")]
        response = types.SimpleNamespace(
            status_code=200,
            headers={"ETag": "v1"},
            json=lambda: {"success": True},
            raise_for_status=lambda: None,
        )
        with patch.object(self.farm.urllib.request, "urlopen", side_effect=self.farm.urllib.error.URLError("timeout")), \
             patch("requests.get", return_value=response) as get:
            self.assertEqual(self.farm._emailqu_get("/api/random-username"), (200, {"success": True}, "v1"))
        self.assertEqual(get.call_args.kwargs["proxies"]["https"], "socks5h://127.0.0.1:40001")

    def test_real_chrome_backend_is_opt_in_and_uses_isolated_context(self):
        source = inspect.getsource(self.farm.launch_browser)
        self.assertIn('BROWSER_ENGINE == "chrome"', source)
        self.assertIn("playwright.chromium.launch", source)
        self.assertIn('browser.new_page(locale="en-US")', source)
        self.assertNotIn("launch_persistent_context", source)

    def test_camoufox_defaults_to_live_proven_linux_fingerprint(self):
        source = Path(self.farm.__file__).read_text(encoding="utf-8")
        self.assertIn('CAMOUFOX_OS = _env("ENTER_BROWSER_OS", "linux").lower()', source)
        self.assertNotIn('random.choice(["windows", "macos", "linux"])', source)

    def test_emailqu_startup_does_not_require_imap_credentials(self):
        source = inspect.getsource(self.farm.main)
        self.assertIn('elif EMAIL_MODE == "emailqu"', source)
        self.assertLess(source.index('elif EMAIL_MODE == "emailqu"'), source.index("if not IMAP_USER or not IMAP_PASS"))


if __name__ == "__main__":
    unittest.main()
