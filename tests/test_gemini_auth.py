from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace

import pb_gemini_v126 as pb_gemini
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
    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


class GeminiAuthTests(unittest.TestCase):
    def setUp(self):
        self._orig_sleep = pb_gemini._sleep
        pb_gemini._sleep = lambda _s: None

    def tearDown(self):
        pb_gemini._sleep = self._orig_sleep

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

    def test_malformed_json_is_retried_once(self):
        responses = [
            FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true,'}]}}]}),
            FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}),
        ]
        app, requests = self.make_app(responses)
        result = app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(requests.calls), 2)

    def test_consistently_malformed_json_raises_clear_error(self):
        responses = [
            FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": '```json\n{"ok": true,}\n```\ntrailing'}]}}]}),
            FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "not json at all"}]}}]}),
        ]
        app, _ = self.make_app(responses)
        with self.assertRaises(RuntimeError) as caught:
            app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertIn("could not return valid JSON", str(caught.exception))

    def test_transient_503_is_retried(self):
        responses = [
            FakeResponse(503, {"error": {"message": "Service Unavailable"}}),
            FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": json.dumps({"ok": True})}]}}]}),
        ]
        app, requests = self.make_app(responses)
        result = app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertEqual(result, {"ok": True})
        self.assertGreaterEqual(len(requests.calls), 2)

    def test_persistent_503_raises_clear_error_after_retries(self):
        responses = [FakeResponse(503, {"error": {"message": "Service Unavailable"}})]
        app, requests = self.make_app(responses)
        with self.assertRaises(RuntimeError) as caught:
            app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertIn("could not return valid JSON", str(caught.exception))
        self.assertEqual(len(requests.calls), 4)

    def test_429_rate_limit_waits_then_retries(self):
        responses = [
            FakeResponse(429, {"error": {"message": "Quota exceeded for metric... Please retry in 12.5s."}}),
            FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": json.dumps({"ok": True})}]}}]}),
        ]
        app, requests = self.make_app(responses)
        result = app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertEqual(result, {"ok": True})
        self.assertGreaterEqual(len(requests.calls), 2)

    def test_persistent_429_raises_rate_limit_message(self):
        responses = [FakeResponse(429, {"error": {"message": "Quota exceeded... Please retry in 20s."}})]
        app, requests = self.make_app(responses)
        with self.assertRaises(RuntimeError) as caught:
            app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertIn("rate-limited", str(caught.exception))
        self.assertEqual(len(requests.calls), 4)

    def test_error_hint_passes_through_rate_limit_message(self):
        response = FakeResponse(200, {})
        app, _ = self.make_app(response)
        hint = app._ai_error_hint(
            RuntimeError(
                "Gemini is rate-limited (free-tier quota for gemini-3.5-flash). "
                "Wait about 26s and try again, or use a paid key / switch providers."
            )
        )
        self.assertIn("26s", hint)

    def test_extracts_json_from_fenced_and_trailing_text(self):
        responses = [
            FakeResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": '```json\n{"ok": true, "nested": {"a": 1}}\n```\nDone.'}]}}]},
            ),
        ]
        app, _ = self.make_app(responses)
        result = app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertEqual(result, {"ok": True, "nested": {"a": 1}})


if __name__ == "__main__":
    unittest.main()
