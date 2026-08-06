import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


class _Response:
    status = 200

    def __init__(self, url="https://auth.x.ai/oauth2/device/approve"):
        self.url = url

    async def text(self):
        return "approved"


class _Request:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(url)


class _Context:
    def __init__(self):
        self.request = _Request()

    async def cookies(self):
        return [{"name": "session", "value": "secret", "domain": ".x.ai"}]


class _Page:
    def __init__(self):
        self.context = _Context()


class DeviceHttpApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        camoufox = types.ModuleType("camoufox")
        async_api = types.ModuleType("camoufox.async_api")
        async_api.AsyncCamoufox = object
        sys.modules.setdefault("camoufox", camoufox)
        sys.modules.setdefault("camoufox.async_api", async_api)
        path = Path(__file__).with_name("farm.py")
        spec = importlib.util.spec_from_file_location("grok_farm_under_test", path)
        cls.farm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.farm)

    def test_http_device_approval_uses_browser_request_context(self):
        page = _Page()
        ok, detail = asyncio.run(
            self.farm.try_device_approval_http(page, "ABCD-EFGH")
        )

        self.assertTrue(ok)
        self.assertIn("verify:200", detail)
        self.assertIn("approve:200", detail)
        self.assertEqual(
            [call[0] for call in page.context.request.calls],
            [self.farm.XAI_DEVICE_VERIFY, self.farm.XAI_DEVICE_APPROVE],
        )
        self.assertEqual(
            page.context.request.calls[0][1]["form"], {"user_code": "ABCD-EFGH"}
        )

    def test_http_device_approval_rejects_redirected_login_page(self):
        class LoginResponse(_Response):
            def __init__(self):
                super().__init__("https://accounts.x.ai/sign-in")

        page = _Page()

        async def post(*args, **kwargs):
            return LoginResponse()

        page.context.request.post = post
        ok, detail = asyncio.run(
            self.farm.try_device_approval_http(page, "ABCD-EFGH")
        )
        self.assertFalse(ok)
        self.assertIn("unexpected final URL", detail)

    def test_http_device_approval_falls_back_when_cookies_missing(self):
        class EmptyContext:
            async def cookies(self):
                return []

        class EmptyPage:
            context = EmptyContext()

        ok, detail = asyncio.run(
            self.farm.try_device_approval_http(EmptyPage(), "ABCD-EFGH")
        )
        self.assertFalse(ok)
        self.assertEqual(detail, "no x.ai cookies")


if __name__ == "__main__":
    unittest.main()
