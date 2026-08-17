from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def apply(app) -> None:
    """Use Gemini's current header-based auth and prevent credential leakage."""
    if getattr(app, "_pb_gemini_v126_patched", False):
        return

    base_ai_error_hint = app._ai_error_hint

    def _extract_json(text: str):
        cleaned = str(text or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        if cleaned:
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass
        start = cleaned.find("{")
        while start != -1:
            depth = 0
            in_string = False
            escaped = False
            for i in range(start, len(cleaned)):
                char = cleaned[i]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cleaned[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = cleaned.find("{", start + 1)
        raise ValueError("Gemini returned JSON that could not be parsed as a complete object.")

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

        model = str(model or "").strip() or "gemini-3.5-flash"
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
                "maxOutputTokens": 32768,
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

        last_error = ""
        transient_codes = {500, 502, 503, 504}

        def _retry_after(text: str, default: float = 30.0) -> float:
            match = re.search(r"retry in\s+([\d.]+)\s*s", str(text or ""), re.I)
            if match:
                try:
                    return min(float(match.group(1)), 120.0)
                except ValueError:
                    pass
            return default

        for _attempt in range(4):
            try:
                resp = app.requests.post(url, headers=headers, json=body, timeout=300)
            except Exception as exc:
                last_error = f"Gemini request failed: {_redact_secret(exc, api_key)}"
                _sleep(min(2 ** _attempt, 10))
                continue

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

                if status == 429:
                    wait = _retry_after(detail)
                    if _attempt >= 3:
                        raise RuntimeError(
                            f"Gemini is rate-limited (free-tier quota for {model}). Wait about {wait:.0f}s and try again, "
                            "use a paid key, or switch providers. On the free tier gemini-3.5-flash-lite allows far more "
                            "requests per minute for draft take-offs."
                        )
                    last_error = f"Gemini rate-limited; retrying after {wait:.0f}s."
                    _sleep(wait)
                    continue
                if status in transient_codes:
                    last_error = f"Gemini returned HTTP {status}; the provider was temporarily unavailable."
                    _sleep(min(2 ** _attempt, 10))
                    continue
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
            try:
                return _extract_json(str(text))
            except ValueError as exc:
                last_error = str(exc)
                continue
        raise RuntimeError(f"Gemini could not return valid JSON after retries: {last_error}")

    def _safe_ai_error_hint(exc: Exception) -> str:
        msg = _redact_secret(exc)
        low = msg.lower()
        if (
            "gemini model endpoint was not found" in low
            or "gemini rejected" in low
            or "rate-limited" in low
            or "retrying after" in low
        ):
            return msg
        return _redact_secret(base_ai_error_hint(RuntimeError(msg)))

    app._redact_ai_secret = _redact_secret
    app._gemini_generate = _gemini_generate
    app._ai_error_hint = _safe_ai_error_hint
    app._pb_gemini_v126_patched = True
