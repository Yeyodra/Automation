import unittest

from scripts.enter_nvrouter_import import convert


class EnterNvRouterImportTests(unittest.TestCase):
    def test_empty_payload_produces_no_rows(self):
        self.assertEqual(convert({"credentials": []}), [])

    def test_invalid_credentials_are_skipped(self):
        self.assertEqual(convert({"credentials": [{"email": "x@example.test", "data": "{}"}]}), [])

    def test_valid_credential_preserves_workspace(self):
        payload = {
            "credentials": [{
                "email": "x@example.test",
                "priority": 2,
                "data": {
                    "apiKey": "ek_test_value",
                    "providerSpecificData": {"workspaceId": "123"},
                },
            }]
        }
        rows = convert(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "enter-converge")
        self.assertEqual(rows[0]["providerSpecificData"]["workspaceId"], "123")
        self.assertNotEqual(rows[0]["id"], convert(payload)[0]["id"])


if __name__ == "__main__":
    unittest.main()
