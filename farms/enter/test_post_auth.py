import importlib.util
import ast
import inspect
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

    def test_unresolved_user_merge_is_rejected(self):
        with patch.object(self.farm, "_api_json", side_effect=[
            {"code": 0, "data": None},
            {"code": 0, "data": {"must_verify_email": False, "merge_action": "required"}},
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


if __name__ == "__main__":
    unittest.main()
