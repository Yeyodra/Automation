import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path


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
    spec = importlib.util.spec_from_file_location("enter_farm_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OfficialGatewayBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.farm = _load_farm()

    def test_official_gateway_urls_are_strictly_classified(self):
        self.assertTrue(
            self.farm._is_enter_login_url(
                "https://enter.converge.ai/auth/login?return_to=%2F&risk_session_id=rs"
            )
        )
        self.assertTrue(
            self.farm._is_enter_callback_url("https://enter.converge.ai/auth/callback?code=secret")
        )
        self.assertFalse(
            self.farm._is_enter_callback_url("https://enter.converge.ai/not-auth/callback?code=secret")
        )
        self.assertFalse(
            self.farm._is_enter_login_url("https://attacker.invalid/auth/login")
        )
        self.assertFalse(self.farm._is_enter_login_url("http://enter.converge.ai/auth/login"))
        self.assertFalse(self.farm._is_enter_login_url("https://enter.converge.ai:444/auth/login"))

    def test_browser_signup_uses_gateway_callback_and_session_boundary(self):
        source = inspect.getsource(self.farm.do_signup_and_oauth)
        session_source = inspect.getsource(self.farm._fetch_gateway_session)

        self.assertNotIn("AUTHORIZE_URL", source)
        self.assertNotIn("generate_pkce_pair", source)
        self.assertNotIn("exchange_code_for_tokens", source)
        self.assertNotIn("_signin_snarf_tokens", source)
        self.assertNotIn("timeout=10", source)
        self.assertIn("_click_official_login_action", source)
        self.assertIn("_is_enter_callback_url", source)
        self.assertIn("await resp.finished()", source)
        self.assertIn("_is_gateway_callback_status", source)
        self.assertNotIn("elif _is_enter_callback_url(frame.url)", source)
        self.assertIn("_is_enter_app_url", source)
        self.assertIn('label="callback_return"', source)
        self.assertIn("_fetch_gateway_session", source)
        self.assertIn("/auth/session?include=access_token", session_source)
        self.assertIn("_parse_gateway_session", session_source)

    def test_registration_has_no_alternate_direct_auth_mode(self):
        source = inspect.getsource(self.farm._do_register_body)
        self.assertNotIn("do_signup_http", source)
        self.assertNotIn('AUTH_MODE == "http"', source)

    def test_post_callback_app_url_is_strictly_classified(self):
        self.assertTrue(self.farm._is_enter_app_url("https://enter.converge.ai/?inviteCode=x"))
        self.assertFalse(self.farm._is_enter_app_url("https://enter.converge.ai/auth/callback?code=x"))
        self.assertFalse(self.farm._is_enter_app_url("https://auth.converge.ai/"))
        self.assertFalse(self.farm._is_enter_app_url("http://enter.converge.ai/"))
        self.assertFalse(self.farm._is_enter_app_url("https://enter.converge.ai:444/"))

    def test_callback_requires_completed_redirect_response(self):
        self.assertTrue(self.farm._is_gateway_callback_status(302))
        self.assertFalse(self.farm._is_gateway_callback_status(200))
        self.assertFalse(self.farm._is_gateway_callback_status(500))

    def test_terminal_auth_reason_redacts_query_values(self):
        reason = self.farm._classify_auth_terminal(
            "https://enter.converge.ai/?error=access_denied&state=secret&code=secret",
            "Something went wrong",
        )
        self.assertEqual(reason, "host=enter.converge.ai path=/ oauth_error=access_denied banner=access_denied")
        self.assertNotIn("secret", reason)

    def test_official_login_helper_waits_for_live_free_credits_cta(self):
        source = inspect.getsource(self.farm._click_official_login_action)
        self.assertIn("Get Free Credits", source)
        self.assertIn("Reject All", source)
        self.assertIn("locator.count()", source)
        self.assertIn("is_visible()", source)


class GatewaySessionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.farm = _load_farm()

    async def test_gateway_session_is_fetched_in_browser_cookie_context(self):
        class Page:
            async def evaluate(self, script):
                self.script = script
                return {
                    "status": 200,
                    "body": '{"user":{"sub":"user-1","isNewUser":true},"accessToken":"token-value","expiresAt":"2026-08-08T00:00:00Z"}',
                }

        page = Page()
        session = await self.farm._fetch_gateway_session(page)

        self.assertIn("credentials: 'include'", page.script)
        self.assertIn("/auth/session?include=access_token", page.script)
        self.assertEqual(session["access_token"], "token-value")

    def test_authenticated_session_is_normalized(self):
        session = self.farm._parse_gateway_session(
            200,
            '{"user":{"sub":"user-1","isNewUser":true},"accessToken":"token-value","expiresAt":"2026-08-08T00:00:00Z"}',
        )

        self.assertEqual(
            session,
            {
                "access_token": "token-value",
                "expires_at": "2026-08-08T00:00:00Z",
                "user": {"sub": "user-1", "isNewUser": True},
            },
        )

    def test_unauthenticated_and_invalid_sessions_are_rejected(self):
        cases = [
            (204, ""),
            (200, "not json"),
            (200, "{}"),
            (200, '{"user":{},"accessToken":"token","expiresAt":"soon"}'),
            (200, '{"user":{"sub":"user-1","isNewUser":false},"accessToken":"token","expiresAt":"soon"}'),
            (200, '{"user":{"sub":"user-1","isNewUser":true},"accessToken":"token","expiresAt":0}'),
            (200, '{"user":{"sub":"user-1","isNewUser":true},"accessToken":"token","expiresAt":{}}'),
            (200, '{"user":{"sub":"user-1"},"accessToken":"","expiresAt":"soon"}'),
            (200, '{"user":{"sub":"user-1"},"accessToken":"token","expiresAt":"   "}'),
        ]

        for status, body in cases:
            with self.subTest(status=status, body=body):
                with self.assertRaises(RuntimeError):
                    self.farm._parse_gateway_session(status, body)


if __name__ == "__main__":
    unittest.main()
