from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def apply(app) -> None:
    """Use Gemini header auth, resilient retries, and safe error handling."""
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

    def _provider_detail(resp: Any, api_key: str) -> str:
        detail = ""
        try:
            payload = resp.json()
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("status") or "")
        except Exception:
            detail = str(getattr(resp, "text", "") or "")
        return _redact_secret(detail, api_key).strip()

    def _retry_after_text(text: str, default: float = 30.0) -> float:
        match = re.search(r"retry in\s+([\d.]+)\s*s", str(text or ""), re.I)
        if match:
            try:
                return min(max(float(match.group(1)), 0.0), 120.0)
            except ValueError:
                pass
        return default

    def _retry_delay(resp: Any, detail: str, attempt: int) -> float:
        headers = getattr(resp, "headers", None) or {}
        retry_after = ""
        try:
            retry_after = str(headers.get("Retry-After") or headers.get("retry-after") or "").strip()
        except Exception:
            retry_after = ""
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 120.0)
            except ValueError:
                pass
        hinted = _retry_after_text(detail, default=-1.0)
        if hinted >= 0:
            return hinted
        # Exponential backoff with a small jitter so concurrent jobs do not all
        # hit Gemini again on the same boundary.
        return min((2 ** attempt) + random.uniform(0.0, 0.5), 10.0)

    def _parse_success(resp: Any, api_key: str) -> Dict[str, Any]:
        try:
            payload = resp.json()
        except Exception as exc:
            raise RuntimeError(
                f"Gemini returned an invalid JSON response envelope: {_redact_secret(exc, api_key)}"
            ) from None
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            safe_payload = _redact_secret(str(payload)[:1000], api_key)
            raise RuntimeError(f"Gemini returned no usable content: {safe_payload}") from None
        return _extract_json(str(text))

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
                "maxOutputTokens": 32768,
            },
        }
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        transient_codes = {500, 502, 503, 504}
        primary_attempts = 4
        last_error = ""
        last_kind = ""
        last_status = 0
        last_detail = ""

        def _url(target_model: str) -> str:
            return f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent"

        for attempt in range(primary_attempts):
            try:
                resp = app.requests.post(_url(model), headers=headers, json=body, timeout=300)
            except Exception as exc:
                last_kind = "transient"
                last_status = 0
                last_error = f"Gemini connection failed: {_redact_secret(exc, api_key)}"
                if attempt < primary_attempts - 1:
                    _sleep(min((2 ** attempt) + random.uniform(0.0, 0.5), 10.0))
                continue

            if not getattr(resp, "ok", False):
                status = int(getattr(resp, "status_code", 0) or 0)
                detail = _provider_detail(resp, api_key)

                if status == 429:
                    wait = _retry_delay(resp, detail, attempt)
                    if attempt >= primary_attempts - 1:
                        raise RuntimeError(
                            f"Gemini is rate-limited (quota for {model}). Wait about {wait:.0f}s and try again, "
                            "use a key/project with available quota, or switch AI providers."
                        )
                    last_kind = "rate_limit"
                    last_error = f"Gemini rate-limited; retrying after {wait:.0f}s."
                    _sleep(wait)
                    continue
                if status in transient_codes:
                    last_kind = "transient"
                    last_status = status
                    last_detail = detail
                    last_error = f"Gemini returned HTTP {status}; the provider was temporarily unavailable."
                    if attempt < primary_attempts - 1:
                        _sleep(_retry_delay(resp, detail, attempt))
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
                return _parse_success(resp, api_key)
            except ValueError as exc:
                last_kind = "json"
                last_error = str(exc)
                continue

        # Persistent 5xx/capacity failures can be model-specific. Make one final
        # same-provider attempt with the configured lightweight document model.
        if last_kind == "transient":
            fallback_model = str(
                os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite") or ""
            ).strip()
            if fallback_model and fallback_model != model:
                try:
                    fallback = app.requests.post(
                        _url(fallback_model), headers=headers, json=body, timeout=300
                    )
                except Exception as exc:
                    last_status = 0
                    last_error = (
                        f"Primary model {model} was unavailable and fallback {fallback_model} "
                        f"could not connect: {_redact_secret(exc, api_key)}"
                    )
                else:
                    if getattr(fallback, "ok", False):
                        try:
                            return _parse_success(fallback, api_key)
                        except ValueError as exc:
                            raise RuntimeError(
                                f"Gemini fallback model {fallback_model} responded, but could not return valid JSON: {exc}"
                            ) from None
                    fallback_status = int(getattr(fallback, "status_code", 0) or 0)
                    fallback_detail = _provider_detail(fallback, api_key)
                    if fallback_status in {401, 403}:
                        raise RuntimeError(
                            "Gemini rejected the configured API key or project permissions. "
                            "Check GEMINI_API_KEY and the Google AI project permissions."
                        )
                    if fallback_status == 429:
                        raise RuntimeError(
                            f"Gemini fallback model {fallback_model} is also rate-limited. "
                            "Try again when quota is available or switch AI providers."
                        )
                    if fallback_status not in transient_codes and fallback_status != 404:
                        raise RuntimeError(
                            f"Gemini fallback model {fallback_model} failed with HTTP {fallback_status or 'error'}."
                            + (f" Provider detail: {fallback_detail[:500]}" if fallback_detail else "")
                        )
                    last_status = fallback_status or last_status
                    last_detail = fallback_detail or last_detail
                    last_error = (
                        f"Primary model {model} and fallback {fallback_model} were temporarily unavailable."
                    )

            status_text = f"HTTP {last_status}" if last_status else "a connection error"
            detail_text = f" Provider detail: {last_detail[:300]}" if last_detail else ""
            raise RuntimeError(
                f"Gemini is temporarily unavailable after repeated attempts ({status_text}). "
                "No AI draft was imported from this failed request. Retry the AI read or switch AI providers."
                + detail_text
            )

        if last_kind == "json":
            raise RuntimeError(f"Gemini could not return valid JSON after retries: {last_error}")
        raise RuntimeError(last_error or "Gemini request failed after retries.")

    def _safe_ai_error_hint(exc: Exception) -> str:
        msg = _redact_secret(exc)
        low = msg.lower()
        if (
            "gemini model endpoint was not found" in low
            or "gemini rejected" in low
            or "rate-limited" in low
            or "temporarily unavailable" in low
            or "fallback model" in low
            or "retrying after" in low
        ):
            return msg
        return _redact_secret(base_ai_error_hint(RuntimeError(msg)))

    app._redact_ai_secret = _redact_secret
    app._gemini_generate = _gemini_generate
    app._ai_error_hint = _safe_ai_error_hint
    app._pb_gemini_v126_patched = True
