from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace

from pb_gemini_v126 import apply


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class RequestsRecorder:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class GeminiAuthTests(unittest.TestCase):
    def make_app(self, response):
        requests = RequestsRecorder(response)
        app = SimpleNamespace(
            requests=requests,
            base64=base64,
            _ai_page_bytes=lambda path: b"image",
            _ai_error_hint=lambda exc: str(exc),
        )
        apply(app)
        return app, requests

    def test_api_key_is_header_not_url(self):
        response = FakeResponse(
            200,
            {"candidates": [{"content": {"parts": [{"text": json.dumps({"ok": True})}]}}]},
        )
        app, requests = self.make_app(response)
        result = app._gemini_generate(
            "AQ.secret-key-value-123456789",
            "gemini-2.5-flash",
            "test",
            [],
            {"type": "object"},
            "schema",
        )
        self.assertEqual(result, {"ok": True})
        url, kwargs = requests.calls[0]
        self.assertNotIn("key=", url)
        self.assertNotIn("AQ.secret-key-value-123456789", url)
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "AQ.secret-key-value-123456789")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertNotIn("params", kwargs)

    def test_404_error_never_leaks_key(self):
        key = "AQ.secret-key-value-123456789"
        response = FakeResponse(
            404,
            {"error": {"message": f"not found; attempted ?key={key}"}},
        )
        app, _ = self.make_app(response)
        with self.assertRaises(RuntimeError) as caught:
            app._gemini_generate(key, "gemini-2.5-flash", "test", [], {"type": "object"}, "schema")
        message = str(caught.exception)
        self.assertIn("model endpoint was not found", message)
        self.assertNotIn(key, message)
        self.assertIn("[REDACTED]", message)

    def test_error_hint_redacts_google_key_patterns(self):
        response = FakeResponse(200, {})
        app, _ = self.make_app(response)
        leaked = "404 for https://example.invalid?key=AQ.exposed-secret-abcdefghijklmnop"
        safe = app._ai_error_hint(RuntimeError(leaked))
        self.assertNotIn("AQ.exposed-secret", safe)
        self.assertIn("[REDACTED]", safe)


if __name__ == "__main__":
    unittest.main()
