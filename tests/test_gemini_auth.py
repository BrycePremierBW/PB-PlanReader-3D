from __future__ import annotations

import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pb_gemini_v126 as pb_gemini
from pb_gemini_v126 import apply


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = headers or {}
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
        self.sleeps = []
        pb_gemini._sleep = lambda seconds: self.sleeps.append(float(seconds))
        self.env = patch.dict(os.environ, {"GEMINI_FALLBACK_MODEL": "gemini-3.5-flash-lite"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
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

    @staticmethod
    def ok_response(value=True):
        return FakeResponse(
            200,
            {"candidates": [{"content": {"parts": [{"text": json.dumps({"ok": value})}]}}]},
        )

    def test_api_key_is_header_not_url(self):
        app, requests = self.make_app(self.ok_response())
        result = app._gemini_generate(
            "AQ.secret-key-value-123456789", "gemini-3.5-flash", "test", [],
            {"type": "object"}, "schema",
        )
        self.assertEqual(result, {"ok": True})
        url, kwargs = requests.calls[0]
        self.assertNotIn("key=", url)
        self.assertNotIn("AQ.secret-key-value-123456789", url)
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "AQ.secret-key-value-123456789")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertNotIn("params", kwargs)
        self.assertNotIn("temperature", kwargs["json"]["generationConfig"])
        self.assertEqual(kwargs["json"]["generationConfig"]["responseMimeType"], "application/json")

    def test_404_error_never_leaks_key(self):
        key = "AQ.secret-key-value-123456789"
        response = FakeResponse(404, {"error": {"message": f"not found; attempted ?key={key}"}})
        app, _ = self.make_app(response)
        with self.assertRaises(RuntimeError) as caught:
            app._gemini_generate(key, "gemini-3.5-flash", "test", [], {"type": "object"}, "schema")
        message = str(caught.exception)
        self.assertIn("model endpoint was not found", message)
        self.assertNotIn(key, message)
        self.assertIn("[REDACTED]", message)

    def test_error_hint_redacts_google_key_patterns(self):
        app, _ = self.make_app(self.ok_response())
        leaked = "404 for https://example.invalid?key=AQ.exposed-secret-abcdefghijklmnop"
        safe = app._ai_error_hint(RuntimeError(leaked))
        self.assertNotIn("AQ.exposed-secret", safe)
        self.assertIn("[REDACTED]", safe)

    def test_malformed_json_is_retried(self):
        responses = [
            FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true,'}]}}]}),
            self.ok_response(),
        ]
        app, requests = self.make_app(responses)
        result = app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(requests.calls), 2)

    def test_consistently_malformed_json_keeps_json_error_label(self):
        responses = [
            FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": '```json\n{"ok": true,}\n``` trailing'}]}}]}),
        ]
        app, _ = self.make_app(responses)
        with self.assertRaises(RuntimeError) as caught:
            app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertIn("could not return valid JSON", str(caught.exception))

    def test_transient_503_is_retried_and_can_recover(self):
        responses = [
            FakeResponse(503, {"error": {"message": "Service Unavailable"}}),
            self.ok_response(),
        ]
        app, requests = self.make_app(responses)
        result = app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(requests.calls), 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_retry_after_header_is_respected_for_503(self):
        responses = [
            FakeResponse(503, {"error": {"message": "Service Unavailable"}}, headers={"Retry-After": "3"}),
            self.ok_response(),
        ]
        app, _ = self.make_app(responses)
        app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertEqual(self.sleeps[0], 3.0)

    def test_persistent_503_uses_fallback_then_raises_availability_error(self):
        responses = [FakeResponse(503, {"error": {"message": "Service Unavailable"}})] * 5
        app, requests = self.make_app(responses)
        with self.assertRaises(RuntimeError) as caught:
            app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        message = str(caught.exception)
        self.assertIn("temporarily unavailable", message)
        self.assertNotIn("could not return valid JSON", message)
        self.assertEqual(len(requests.calls), 5)
        self.assertIn("gemini-3.5-flash-lite", requests.calls[-1][0])
        self.assertEqual(len(self.sleeps), 3)

    def test_fallback_model_recovers_after_primary_503s(self):
        responses = [
            FakeResponse(503, {"error": {"message": "Service Unavailable"}}),
            FakeResponse(503, {"error": {"message": "Service Unavailable"}}),
            FakeResponse(503, {"error": {"message": "Service Unavailable"}}),
            FakeResponse(503, {"error": {"message": "Service Unavailable"}}),
            self.ok_response(),
        ]
        app, requests = self.make_app(responses)
        result = app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(requests.calls), 5)
        self.assertIn("gemini-3.5-flash-lite", requests.calls[-1][0])

    def test_429_rate_limit_waits_then_retries(self):
        responses = [
            FakeResponse(429, {"error": {"message": "Quota exceeded. Please retry in 12.5s."}}),
            self.ok_response(),
        ]
        app, requests = self.make_app(responses)
        result = app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(requests.calls), 2)
        self.assertEqual(self.sleeps[0], 12.5)

    def test_persistent_429_raises_rate_limit_message_without_fallback(self):
        responses = [FakeResponse(429, {"error": {"message": "Quota exceeded. Please retry in 20s."}})]
        app, requests = self.make_app(responses)
        with self.assertRaises(RuntimeError) as caught:
            app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertIn("rate-limited", str(caught.exception))
        self.assertEqual(len(requests.calls), 4)

    def test_error_hint_passes_through_availability_message(self):
        app, _ = self.make_app(self.ok_response())
        hint = app._ai_error_hint(RuntimeError("Gemini is temporarily unavailable after repeated attempts (HTTP 503)."))
        self.assertIn("temporarily unavailable", hint)

    def test_extracts_json_from_fenced_and_trailing_text(self):
        response = FakeResponse(
            200,
            {"candidates": [{"content": {"parts": [{"text": '```json\n{"ok": true, "nested": {"a": 1}}\n```\nDone.'}]}}]},
        )
        app, _ = self.make_app(response)
        result = app._gemini_generate("AQ.k", "gemini-3.5-flash", "t", [], {"type": "object"}, "s")
        self.assertEqual(result, {"ok": True, "nested": {"a": 1}})


if __name__ == "__main__":
    unittest.main()
