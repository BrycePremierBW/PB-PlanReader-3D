from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple


def apply(app) -> None:
    """Use Gemini's current header-based auth and prevent credential leakage."""
    if getattr(app, "_pb_gemini_v126_patched", False):
        return

    base_ai_error_hint = app._ai_error_hint

    def _redact_secret(text: Any, secret: str = "") -> str:
        value = str(text or "")
        if secret:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"([?&]key=)[^&\s]+", r"\1[REDACTED]", value, flags=re.IGNORECASE)
        value = re.sub(r"\b(?:AIza|AQ\.)[A-Za-z0-9._-]{12,}\b", "[REDACTED]", value)
        return value

    def _gemini_generate(api_key: str, model: str, prompt: str,
                         blocks: List[Tuple[str, str]], schema: Dict[str, Any],
                         schema_name: str) -> Dict[str, Any]:
        del schema_name
        api_key = str(api_key or "").strip()
        if not api_key:
            raise RuntimeError("Google Gemini API key is not configured.")

        model = str(model or "").strip() or "gemini-2.5-flash"
        parts: List[Dict[str, Any]] = []
        if prompt:
            parts.append({"text": prompt})
        for kind, value in blocks:
            if kind == "text":
                parts.append({"text": value})
            else:
                path = Path(value)
                parts.append({
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": app.base64.b64encode(app._ai_page_bytes(path)).decode("ascii"),
                    }
                })
        parts.append({
            "text": "Return a single valid JSON object matching this schema exactly, with no prose:\n"
                    + json.dumps(schema)
        })
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
                "maxOutputTokens": 8000,
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

        try:
            resp = app.requests.post(url, headers=headers, json=body, timeout=300)
        except Exception as exc:
            raise RuntimeError(f"Gemini request failed: {_redact_secret(exc, api_key)}") from None

        if not getattr(resp, "ok", False):
            status = int(getattr(resp, "status_code", 0) or 0)
            detail = ""
            try:
                payload = resp.json()
                err = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(err, dict):
                    detail = str(err.get("message") or err.get("status") or "")
            except Exception:
                detail = str(getattr(resp, "text", "") or "")
            detail = _redact_secret(detail, api_key).strip()

            if status == 404:
                raise RuntimeError(
                    f"Gemini model endpoint was not found for '{model}'. "
                    "Check GEMINI_MODEL and that the configured Google AI project/key can access this model."
                    + (f" Provider detail: {detail[:500]}" if detail else "")
                )
            if status in {401, 403}:
                raise RuntimeError(
                    "Gemini rejected the configured API key or project permissions. "
                    "Create/rotate the key in Google AI Studio, update GEMINI_API_KEY in Render, and redeploy."
                    + (f" Provider detail: {detail[:500]}" if detail else "")
                )
            if status == 429:
                raise RuntimeError(
                    "Gemini quota or rate limit was reached. Try again later or review the Google AI project quota."
                    + (f" Provider detail: {detail[:500]}" if detail else "")
                )
            raise RuntimeError(
                f"Gemini request failed with HTTP {status or 'error'}."
                + (f" Provider detail: {detail[:500]}" if detail else "")
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Gemini returned an invalid JSON response: {_redact_secret(exc, api_key)}") from None
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            safe_payload = _redact_secret(str(payload)[:1000], api_key)
            raise RuntimeError(f"Gemini returned no usable content: {safe_payload}") from None
        match = re.search(r"\{.*\}", str(text), re.S)
        if not match:
            raise RuntimeError("Gemini did not return JSON matching the requested schema.")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini returned malformed JSON: {exc}") from None

    def _safe_ai_error_hint(exc: Exception) -> str:
        msg = _redact_secret(exc)
        low = msg.lower()
        if "gemini model endpoint was not found" in low or "gemini rejected" in low:
            return msg
        return _redact_secret(base_ai_error_hint(RuntimeError(msg)))

    app._redact_ai_secret = _redact_secret
    app._gemini_generate = _gemini_generate
    app._ai_error_hint = _safe_ai_error_hint
    app._pb_gemini_v126_patched = True
