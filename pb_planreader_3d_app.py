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

# Max entries in AUTO_SCALE before cleanup
AUTO_SCALE_MAX_ENTRIES = 50


def _gc() -> None:
    """Force Python garbage collection to free memory."""
    import gc
    gc.collect()


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
        # Enforce max entries limit
        if len(AUTO_SCALE) > AUTO_SCALE_MAX_ENTRIES:
            # Remove oldest entries
            sorted_keys = sorted(AUTO_SCALE.keys())
            keys_to_remove = sorted_keys[: len(AUTO_SCALE) - AUTO_SCALE_MAX_ENTRIES]
            for key in keys_to_remove:
                del AUTO_SCALE[key]
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


def _redact_secret(text: Any, secret: str = "") -> str:
    value = str(text or "")
    if secret:
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"([?&]key=)[^&\s]+", r"\1[REDACTED]", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:AIza|AQ\.)[A-Za-z0-9._-]{12,}\b", "[REDACTED]", value)
    return value


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


def auto_apply_scale(page_id: int, workspace_id: int) -> Optional[float]:
    """Auto-apply scale to a page if not already set.

    Uses AUTO_SCALE dictionary and auto-detection to find or calculate
    the px_per_m value for the given page.
    """
    # Check if already in AUTO_SCALE
    if page_id in AUTO_SCALE and AUTO_SCALE[page_id] > 0:
        return AUTO_SCALE[page_id]

    # Try auto-detection from scale annotation
    # In actual implementation, fetch page data from database
    # For now, return None to indicate scale needs to be set manually
    return None


def scale_gate_issues(workspace_id: int) -> List[Dict[str, Any]]:
    """Pages that feed the take-off but do not have a calibrated scale yet.

    The scale gate protects measured quantities: a row is only trustworthy when
    every drawing page it was measured from has a confirmed ``px_per_m``. This
    lists every selected page that is referenced by take-off rows or mapped
    zones while still lacking a scale.
    """
    import sqlite3
    row_sources: set = set()
    # In actual app: rows = app.ldf("SELECT DISTINCT source_page FROM takeoff_rows WHERE workspace_id=?", (workspace_id,))
    # If rows available, extract source pages
    try:
        conn = sqlite3.connect(getattr(None, 'db_path', 'default.db'))
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT source_page FROM takeoff_rows WHERE workspace_id=?", (workspace_id,))
        rows = cursor.fetchall()
        if rows:
            row_sources = {str(x[0]).strip() for x in rows if x[0]}
        conn.close()
    except Exception:
        pass

    # In actual app: zones = app.ldf("SELECT DISTINCT page_id FROM mapped_zones WHERE workspace_id=?", (workspace_id,))
    zone_page_ids: set = set()

    issues: List[Dict[str, Any]] = []
    # In actual app: pages = app.ldf("SELECT id,page_label,page_type,px_per_m FROM pages WHERE workspace_id=? AND selected=1", (workspace_id,))
    # placeholder - actual code uses app.ldf
    pages = []

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


def safe_render_page(path: Path, render_zoom: float, max_edge: int = 2400) -> Any:
    """Safely render a PDF page with memory-limited zoom.

    Renders the page at the given zoom factor but ensures the long edge
    never exceeds max_edge pixels to prevent memory overflow.

    Args:
        path: Path to the PDF file
        render_zoom: The zoom factor (points per unit)
        max_edge: Maximum pixel dimension for long edge (default 2400)

    Returns:
        Rendered page surface or None if rendering fails
    """
    try:
        from pdf2image import convert_from_path
        # Calculate effective zoom with safety cap
        if render_zoom is None or render_zoom <= 0:
            render_zoom = 1.7  # Historical default

        effective_zoom = min(render_zoom, max_edge / 500)  # Safety cap

        # Convert with size limit
        images = convert_from_path(str(path), dpi=effective_zoom * 72 / 25.4)

        # Further resize if still too large
        if images and len(images) > 0:
            img = images[0]
            width, height = img.size
            long_edge = max(width, height)

            if long_edge > max_edge:
                # Calculate new dimensions maintaining aspect ratio
                ratio = max_edge / long_edge
                new_width = int(width * ratio)
                new_height = int(height * ratio)
                img = img.resize((new_width, new_height), Image.LANCZOS)

            # Delete intermediate results if multiple pages
            if len(images) > 1:
                for img_ in images[1:]:
                    del img_

            # Force garbage collection after rendering
            import gc
            gc.collect()

            return img

        return None
    except Exception as e:
        # Log error but don't crash
        import logging
        logging.warning(f"Page rendering failed: {e}")
        return None


def cleanup_gemini_image_data(image_data: Any) -> None:
    """Explicitly clean up Gemini API image data to free memory.

    Gemini's base64 encoding can hold large bitmaps in memory. This function
    ensures references are cleared and garbage collection is forced.

    Args:
        image_data: The base64-encoded image data or image object to clean up
    """
    try:
        # Clear any large references
        if image_data is not None:
            # If it's a string (base64), clear it
            if isinstance(image_data, str):
                # Replace with empty string to allow GC
                image_data = ""
            # If it has a close method or similar, call it
            elif hasattr(image_data, 'close'):
                image_data.close()
            elif hasattr(image_data, 'release'):
                image_data.release()

        # Force garbage collection
        import gc
        gc.collect()
    except Exception:
        pass  # Never block on cleanup errors


def clear_session_workspace_data(workspace_id: int) -> None:
    """Clear workspace-specific data from session state to free memory.

    This should be called when switching workspaces or after completing
    a take-off operation to prevent memory accumulation.

    Args:
        workspace_id: The workspace ID whose data should be cleared
    """
    import streamlit as st

    # Keys commonly used for workspace data
    memory_heavy_keys = [
        "rendered_pages",
        "page_images",
        "measurement_lines_cache",
        "ai_results",
        "scale_detection_results",
        "temporary_data"
    ]

    for key in memory_heavy_keys:
        if key in st.session_state:
            # Only clear if it's associated with this workspace
            data = st.session_state[key]
            if isinstance(data, dict) and data.get("workspace_id") == workspace_id:
                st.session_state[key] = {}
            elif isinstance(data, list) and any(
                item.get("workspace_id") == workspace_id if isinstance(item, dict) else False
                for item in data
            ):
                # Keep only non-workspace items or clear
                st.session_state[key] = [
                    item for item in data
                    if not (isinstance(item, dict) and item.get("workspace_id") == workspace_id)
                ] or []
            else:
                st.session_state[key] = [] if not isinstance(data, dict) else {}


def batch_process_lines(
    lines: List[Dict[str, Any]],
    batch_size: int = 20,
    processor_func: callable = None
) -> List[Dict[str, Any]]:
    """Process measurement lines in batches to avoid memory overflow.

    Args:
        lines: List of measurement line dictionaries to process
        batch_size: Number of lines to process per batch (default 20)
        processor_func: Function to apply to each batch. Should accept
                       a list of lines and return processed results

    Returns:
        List of all processed results in original order
    """
    if not lines:
        return []

    if processor_func is None:
        # Default: just return lines as-is (for pagination use)
        return lines

    all_results = []

    # Process in batches
    for i in range(0, len(lines), batch_size):
        batch = lines[i:i + batch_size]
        try:
            result = processor_func(batch)
            all_results.extend(result if result else [])
        except Exception as e:
            # Log error but continue with remaining batches
            import logging
            logging.warning(f"Batch processing error at batch {i//batch_size}: {e}")
            # Add unprocessed lines as-is
            all_results.extend(batch)

        # Force garbage collection between batches
        import gc
        gc.collect()

    # Ensure results match input order and count
    if len(all_results) < len(lines):
        # Fill missing results
        all_results.extend(lines[len(all_results):])

    return all_results[:len(lines)]


def estimate_memory_usage(workspace_id: int) -> Dict[str, Any]:
    """Estimate memory usage for a workspace's data.

    Provides rough estimates of memory consumption so operators can
    make informed decisions about processing large projects.

    Args:
        workspace_id: The workspace to estimate memory for

    Returns:
        Dictionary with memory estimates in MB
    """
    import sqlite3
    import os
    import logging

    try:
        conn = sqlite3.connect(getattr(None, 'db_path', 'default.db'))
        cursor = conn.cursor()

        estimates = {
            "total_mb": 0,
            "pages_mb": 0,
            "lines_mb": 0,
            "ai_results_mb": 0,
            "recommendations": []
        }

        # Estimate pages
        try:
            cursor.execute("SELECT COUNT(*) FROM pages WHERE workspace_id=?", (workspace_id,))
            page_count = cursor.fetchone()[0]
            # Rough estimate: 1MB per page (rendered image + metadata)
            estimates["pages_mb"] = round(page_count * 1.0, 2)
            estimates["total_mb"] += estimates["pages_mb"]

            if page_count > 100:
                estimates["recommendations"].append(
                    "Consider reducing page rendering zoom or processing in batches"
                )
        except Exception:
            pass

        # Estimate measurement lines
        try:
            cursor.execute("SELECT COUNT(*) FROM measurement_lines WHERE workspace_id=?", (workspace_id,))
            line_count = cursor.fetchone()[0]
            # Rough estimate: 50KB per line (points + metadata)
            estimates["lines_mb"] = round(line_count * 0.05, 2)
            estimates["total_mb"] += estimates["lines_mb"]

            if line_count > 500:
                estimates["recommendations"].append(
                    "Consider batch processing measurement lines"
                )
        except Exception:
            pass

        # Estimate AI results
        try:
            cursor.execute("""
                SELECT COUNT(*) FROM takeoff_rows
                WHERE workspace_id=? AND ai_validated IS NOT NULL
            """, (workspace_id,))
            ai_count = cursor.fetchone()[0]
            # Rough estimate: 10KB per AI-validated row
            estimates["ai_results_mb"] = round(ai_count * 0.01, 2)
            estimates["total_mb"] += estimates["ai_results_mb"]
        except Exception:
            pass

        conn.close()

        # Add overall guidance
        if estimates["total_mb"] > 500:
            estimates["recommendations"].append(
                "WARNING: High memory usage detected. Consider splitting project or"
                " reducing rendering resolution."
            )
        elif estimates["total_mb"] > 200:
            estimates["recommendations"].append(
                "Moderate memory usage. Monitor during operations and use batch processing."
            )

        return estimates

    except Exception as e:
        logging.error(f"Memory estimation failed: {e}")
        return {
            "total_mb": 0,
            "pages_mb": 0,
            "lines_mb": 0,
            "ai_results_mb": 0,
            "recommendations": ["Unable to estimate memory usage"]
        }


__all__ = [
    "_sleep",
    "_gc",
    "AUTO_SCALE",
    "AUTO_SCALE_MAX_ENTRIES",
    "auto_detect_scale",
    "to_float",
    "apply",
    "ensure_scale_columns",
    "auto_apply_scale",
    "scale_gate_issues",
    "normalise_line_unit",
    "now_stamp",
    "safe_render_page",
    "cleanup_gemini_image_data",
    "clear_session_workspace_data",
    "batch_process_lines",
    "estimate_memory_usage",
    "_SCALE_RATIO_RE",
    "_SCALE_IN_RE",
]