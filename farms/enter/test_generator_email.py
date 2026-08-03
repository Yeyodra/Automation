import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("generator_email", Path(__file__).with_name("generator_email.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_extracts_contextual_otp_from_html():
    html = '<div class="mess_bodiyy">Your verification code is <b>482913</b></div>'
    assert m.extract_otp(html) == "482913"


def test_ignores_unrelated_six_digits():
    assert m.extract_otp('<div class="mess_bodiyy">Order 482913 shipped</div>') is None


def test_extracts_page_token():
    html = '<meta name="api-token" content="123.aabb.ccdd">'
    assert m.extract_api_token(html) == "123.aabb.ccdd"


def test_poller_accepts_enter_since_timestamp_argument():
    import inspect
    assert list(inspect.signature(m.poll_otp).parameters) == ["address", "timeout", "since_ts"]


if __name__ == "__main__":
    test_extracts_contextual_otp_from_html()
    test_ignores_unrelated_six_digits()
    test_extracts_page_token()
    test_poller_accepts_enter_since_timestamp_argument()
    print("OK")
