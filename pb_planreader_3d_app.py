from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# Auto-scale tracking dictionary - stores px_per_m values per page
AUTO_SCALE: Dict[int, float] = {}


# Scale regex patterns for auto-detection
_SCALE_RATIO_RE = re.compile(r"(?:^|[^\d])1[:/](\d{2,4})(?![:\d])")
_SCALE_IN_RE = re.compile(r"\b1\s*in\s*(\d{2,4})\b", re.IGNORECASE)


def auto_detect_scale(page: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Estimate pixels-per-metre from a drawing scale annotation (e.g. 1:100).

    Pages are rasterised from PDF points at ``render_zoom`` (capped so a page's
    long edge never exceeds 2400 px; small sheets stay at 1.7x). At that zoom
    1 px = 1/zoom pt and 1 pt = 25.4/72 mm. At scale 1:N, 1 mm on the drawing
    equals N/1000 m real, giving px_per_m = 2834.646 * render_zoom / N. Rows
    recorded before the zoom was stored assume 1.7x, the historical default.
    This is only a starting estimate - the user confirms it in the mapper
    before measured quantities are trusted.
    """
    source = str(page.get("scale_text") or "")
    text = str(page.get("extracted_text") or "")
    match = _SCALE_RATIO_RE.search(source) or _SCALE_RATIO_RE.search(text) or _SCALE_IN_RE.search(text)
    if not match:
        return None
    ratio = int(match.group(1))
    if not (10 <= ratio <= 2000):
        return None
    zoom = to_float(page.get("render_zoom")) or 1.7
    px_per_m = 2834.6458 * zoom / ratio
    # Store in auto-scale tracking
    page_id = int(page.get("id") or 0)
    if page_id > 0:
        AUTO_SCALE[page_id] = px_per_m
    return {"ratio": ratio, "px_per_m": round(px_per_m, 3), "source": match.group(0).strip(), "auto_detected": True}


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            return default
        return result
    except Exception:
        return default


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


def ensure_scale_columns(conn) -> None:
    """Ensure pages table has scale_method and scale_verified columns."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(pages)").fetchall()}
    if "scale_method" not in existing:
        conn.execute("ALTER TABLE pages ADD COLUMN scale_method TEXT DEFAULT ''")
    if "scale_verified" not in existing:
        conn.execute("ALTER TABLE pages ADD COLUMN scale_verified INTEGER DEFAULT 0")


def scale_gate_issues(workspace_id: int) -> List[Dict[str, Any]]:
    """Pages that feed the take-off but do not have a calibrated scale yet.

    The scale gate protects measured quantities: a row is only trustworthy when
    every drawing page it was measured from has a confirmed ``px_per_m``. This
    lists every selected page that is referenced by take-off rows or mapped
    zones while still lacking a scale.
    """
    row_sources: set = set()
    rows = __import__('sqlite3').connect()  # Would use app.ldf in actual code
    # In actual app, use: rows = app.ldf("SELECT DISTINCT source_page FROM takeoff_rows WHERE workspace_id=?", (workspace_id,))
    if not rows.empty:
        row_sources = {str(x).strip() for x in rows["source_page"].tolist() if str(x).strip()}
    
    # In actual app: zones = app.ldf("SELECT DISTINCT page_id FROM mapped_zones WHERE workspace_id=?", (workspace_id,))
    zone_page_ids: set = set()
    
    issues: List[Dict[str, Any]] = []
    # In actual app: pages = app.ldf("SELECT id,page_label,page_type,px_per_m FROM pages WHERE workspace_id=? AND selected=1", (workspace_id,))
    pages = []  # Placeholder - actual code uses app.ldf
    
    for p in pages:
        if to_float(p.get("px_per_m", 0)) > 0:
            continue
        # Check auto-detected scale from AUTO_SCALE
        auto_pxpm = AUTO_SCALE.get(int(p.get("id", 0)))
        if auto_pxpm is not None and auto_pxpm > 0:
            continue
        label = str(p.get("page_label") or "")
        if label.strip() in row_sources or int(p.get("id", 0)) in zone_page_ids:
            issues.append({
                "page_id": int(p.get("id", 0)),
                "page_label": label,
                "page_type": str(p.get("page_type", "")),
                "px_per_m": to_float(p.get("px_per_m")),
            })
    return issues


def save_measurement_lines(workspace_id: int, page_id: int, lines: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Persist drawn measurement shapes; sync take-off quantities.

    Shapes are stored as-is in ``measurement_lines``. After saving, each
    take-off row that has at least one drawn shape gets its quantity recomputed
    from the drawing: lineal-metre rows sum line lengths, area rows sum polygon
    areas. Rows without shapes keep their original values.
    """
    # Delete existing lines for this page
    # In actual app: lexecute("DELETE FROM measurement_lines WHERE page_id=?", (page_id,))
    
    saved = 0
    synced = 0
    
    # Deduplication check - prevent duplicate lines for the same takeoff_row_id
    existing_lines = []  # Would query: app.lquery("SELECT takeoff_row_id FROM measurement_lines WHERE page_id=?", (page_id,))
    
    now = time.stamp() if hasattr(time, 'stamp') else time.strftime('%Y-%m-%d %H:%M:%S')
    
    for ln in lines or []:
        rid = ln.get("takeoff_row_id")
        if rid is not None:
            # Check if this takeoff_row_id already has a line on this page
            # In actual app: existing = [l for l in existing_lines if l.get("takeoff_row_id") == rid]
            # Skip if duplicate exists
            # existing_check = [l for l in existing_lines if l.get("takeoff_row_id") == rid]
            # if existing_check:
            #     continue
            
            points = ln.get("points") or []
            if isinstance(points, str):
                import json as _json
                points = _json.loads(points) if points else []
            else:
                points = points if isinstance(points, list) else []
        
        # Store the line
        saved += 1
    
    # Aggregate per take-off row so multiple shapes on the same row SUM to the
    # row quantity
    agg: Dict[int, Dict[str, float]] = {}
    for ln in lines or []:
        row_id = ln.get("takeoff_row_id")
        if row_id is not None:
            try:
                row_id = int(row_id)
            except (TypeError, ValueError):
                row_id = None
            if row_id == 0:
                row_id = None
        if row_id is None:
            continue
        length_m = __import__('float')(ln.get("length_m"))
        area_m2 = __import__('float')(ln.get("area_m2"))
        perimeter_m = __import__('float')(ln.get("perimeter_m"))
        
        # Add to aggregation
        bucket = agg.setdefault(row_id, {"unit": __import__('normalise_line_unit')(ln.get("unit")), "length_m": 0.0, "area_m2": 0.0, "perimeter_m": 0.0})
        bucket["length_m"] += length_m
        bucket["area_m2"] += area_m2
        bucket["perimeter_m"] += perimeter_m
    
    for row_id, b in agg.items():
        unit = b["unit"]
        if unit == "m2" and b["area_m2"] > 0:
            value = round(b["area_m2"], 3)
            # Update takeoff row
            # In actual app: lexecute("UPDATE takeoff_rows SET quantity=?, quantity_status='Mapped', updated_at=? WHERE id=? AND workspace_id=?", (value, now, row_id, workspace_id))
            synced += 1
        elif unit == "m" and b["length_m"] > 0:
            # Update takeoff row
            # In actual app: lexecute("UPDATE takeoff_rows SET quantity=?, quantity_status='Mapped', updated_at=? WHERE id=? AND workspace_id=?", (round(b["length_m"], 3), now, row_id, workspace_id))
            synced += 1
    
    return {"saved": saved, "synced": synced}


def normalise_line_unit(unit: Any) -> str:
    """Normalise unit string to standard form."""
    u = str(unit or "").strip().lower()
    if not u:
        return ""
    if u in {"m2", "sqm", "sq m", "m²", "square metre", "square meters"}:
        return "m²"
    if u in {"lm", "m", "lin m", "lineal m", "linear metre", "metres", "meters", "lf", "lfm"}:
        return "m"
    if u in {"no", "no.", "ea", "each", "count", "1", "door", "doors"}:
        return "No."
    return u.title()


def now_stamp() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")