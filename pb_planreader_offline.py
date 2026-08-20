"""
Offline Plan Reader - No AI Required.

Extracts text, dimensions, walls, and room data from construction plans
using only PyMuPDF, PyMuPDF4LLM (OCR), and OpenCV.

No OpenAI, no Gemini, no API keys needed.
"""

from __future__ import annotations

import re
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import pymupdf4llm
except ImportError:
    pymupdf4llm = None

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

import pandas as pd


# =============================================================================
# DIMENSION PATTERNS (Australian construction standards)
# =============================================================================

# Common dimension formats
DIMENSION_PATTERNS = [
    # Millimeters: 1200, 2400, 350
    (r"\b(\d{2,5})\b", "mm"),
    # Meters: 3.5m, 12.0m
    (r"\b(\d+\.?\d*)\s*m\b", "m"),
    # Feet-inches: 10'-6"
    (r"\b(\d+)['']\s*-?\s*(\d+)['\"]", "ft-in"),
    # Scale ratios: 1:100, 1:50
    (r"1\s*[:/]\s*(\d{2,4})", "scale"),
    # Metric with units: 1200mm, 3.5m
    (r"\b(\d+\.?\d*)\s*(mm|m|cm)\b", "metric"),
]

# Room/area keywords
ROOM_KEYWORDS = [
    "bedroom", "bed", "living", "dining", "kitchen", "bathroom", "bath",
    "ensuite", "laundry", "garage", "carport", "patio", "deck", "veranda",
    "hallway", "hall", "entry", "foyer", "study", "office", "rumpus",
    "theatre", "media", "powder", "toilet", "wc", "store", "storage",
    "utility", "mud", "butlers", "butler", "pantry", "walk-in", "robe",
    "closet", "void", "voids", "mech", "mechanical", "electrical",
    "switchboard", "meter", "hp", "hot water", "air con", "ac",
]

# Material keywords
MATERIAL_KEYWORDS = [
    "render", "brick", "weatherboard", "cladding", "timber", "steel",
    "concrete", "fibre cement", "fibreglass", "metal", "colorbond",
    "zincalume", "galvanized", "stucco", "stone", "veneer", "tile",
    "slate", "corrugated", "flat", "tiling", "ceramic", "porcelain",
    "vinyl", "carpet", "polished", "sealed", "painted", "primer",
    "undercoat", "sealer", "texture", "smooth", "matte", "gloss",
    "satin", "flat", "low sheen", "semi-gloss", "high gloss",
]

# Finish/colour keywords
COLOUR_KEYWORDS = [
    "white", "black", "grey", "gray", "cream", "beige", "tan", "brown",
    "red", "blue", "green", "yellow", "orange", "purple", "pink",
    "charcoal", "anthracite", "monument", "surfmist", "paperbark",
    "cornshell", "dune", "stone", "cottage", "wl", "night sky",
    "deep ocean", "neutral", "natural", "primed", "painted",
]


# =============================================================================
# TEXT EXTRACTION
# =============================================================================

def extract_text_offline(
    pdf_path: str | Path,
    pages: Optional[List[int]] = None,
    use_ocr: bool = True,
) -> Dict[int, str]:
    """
    Extract text from PDF, with OCR for scanned pages.
    
    Args:
        pdf_path: Path to PDF
        pages: Page numbers (1-indexed), None = all
        use_ocr: Enable OCR for scanned pages
    
    Returns:
        Dict mapping page_no -> extracted text
    """
    pdf_path = Path(pdf_path)
    
    if pymupdf4llm is not None and use_ocr:
        # Use PyMuPDF4LLM for OCR-aware extraction
        kwargs = {}
        if pages is not None:
            kwargs["pages"] = [p - 1 for p in pages]  # 0-indexed
        
        md_text = pymupdf4llm.to_markdown(str(pdf_path), **kwargs)
        
        # Split by page markers if available
        result = {}
        if pages:
            for page_no in pages:
                # Try to extract page-specific content
                result[page_no] = md_text[:50000]  # Limit per page
        else:
            result[1] = md_text
        
        return result
    
    elif fitz is not None:
        # Fallback to basic PyMuPDF
        pdf = fitz.open(str(pdf_path))
        result = {}
        
        target_pages = pages or list(range(1, len(pdf) + 1))
        
        for page_no in target_pages:
            if page_no < 1 or page_no > len(pdf):
                continue
            page = pdf[page_no - 1]
            text = page.get_text("text") or ""
            result[page_no] = text
        
        pdf.close()
        return result
    
    else:
        raise RuntimeError("No PDF library available. Install PyMuPDF or pymupdf4llm.")


def extract_text_blocks_with_positions(
    page: "fitz.Page",
) -> List[Dict[str, Any]]:
    """
    Extract text blocks with their positions on the page.
    
    Returns:
        List of dicts with x0, y0, x1, y1, text, block_no
    """
    blocks = page.get_text("blocks")
    result = []
    
    for block in blocks:
        x0, y0, x1, y1, text, block_no, block_type = block
        
        if block_type != 0:  # Skip images
            continue
        
        text = text.strip()
        if not text:
            continue
        
        result.append({
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "y1": y1,
            "text": text,
            "block_no": block_no,
            "width": x1 - x0,
            "height": y1 - y0,
            "center_x": (x0 + x1) / 2,
            "center_y": (y0 + y1) / 2,
        })
    
    return result


def extract_words_with_positions(
    page: "fitz.Page",
) -> List[Dict[str, Any]]:
    """
    Extract individual words with their positions.
    
    Returns:
        List of dicts with x0, y0, x1, y1, text, word info
    """
    words = page.get_text("words")
    result = []
    
    for word in words:
        x0, y0, x1, y1, word_text, block_no, line_no, word_no = word
        
        if not word_text.strip():
            continue
        
        result.append({
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "y1": y1,
            "text": word_text,
            "block_no": block_no,
            "line_no": line_no,
            "word_no": word_no,
            "width": x1 - x0,
            "height": y1 - y0,
        })
    
    return result


# =============================================================================
# VECTOR GRAPHICS EXTRACTION
# =============================================================================

def extract_lines(
    page: "fitz.Page",
    min_length: float = 10.0,
    max_length: float = 5000.0,
) -> List[Dict[str, Any]]:
    """
    Extract line segments from vector graphics.
    
    Args:
        page: PyMuPDF page
        min_length: Minimum line length in points
        max_length: Maximum line length in points
    
    Returns:
        List of line dicts with start, end, length, color, width
    """
    drawings = page.get_drawings()
    lines = []
    
    for path in drawings:
        color = path.get("color", (0, 0, 0))
        width = path.get("width", 0)
        fill = path.get("fill")
        dashes = path.get("dashes")
        
        for item in path.get("items", []):
            if item[0] == "l":  # Line
                start, end = item[1], item[2]
                length = float(start.distance_to(end))
                
                if min_length <= length <= max_length:
                    lines.append({
                        "start": (float(start.x), float(start.y)),
                        "end": (float(end.x), float(end.y)),
                        "length": length,
                        "color": color,
                        "width": width,
                        "fill": fill,
                        "dashes": dashes,
                        "angle": math.degrees(math.atan2(
                            end.y - start.y, end.x - start.x
                        )),
                    })
    
    return lines


def extract_rectangles(
    page: "fitz.Page",
    min_area: float = 100.0,
) -> List[Dict[str, Any]]:
    """
    Extract rectangles from vector graphics.
    
    Args:
        page: PyMuPDF page
        min_area: Minimum area in square points
    
    Returns:
        List of rectangle dicts with x0, y0, x1, y1, area, color, fill
    """
    drawings = page.get_drawings()
    rects = []
    
    for path in drawings:
        color = path.get("color", (0, 0, 0))
        fill = path.get("fill")
        width = path.get("width", 0)
        
        for item in path.get("items", []):
            if item[0] == "re":  # Rectangle
                rect = item[1]
                area = float(rect.width * rect.height)
                
                if area >= min_area:
                    rects.append({
                        "x0": float(rect.x0),
                        "y0": float(rect.y0),
                        "x1": float(rect.x1),
                        "y1": float(rect.y1),
                        "width": float(rect.width),
                        "height": float(rect.height),
                        "area": area,
                        "color": color,
                        "fill": fill,
                        "stroke_width": width,
                    })
    
    return rects


def detect_walls(
    page: "fitz.Page",
    min_length: float = 50.0,
    min_width: float = 0.3,
    max_width: float = 5.0,
) -> List[Dict[str, Any]]:
    """
    Detect wall lines from vector graphics.
    
    Walls are typically:
    - Black or dark colored
    - Medium width (0.3-5.0 points)
    - Longer than 50 points
    
    Returns:
        List of wall line dicts
    """
    lines = extract_lines(page, min_length=min_length)
    walls = []
    
    for line in lines:
        r, g, b = line["color"][:3] if line["color"] else (0, 0, 0)
        
        # Check if dark colored (walls are usually black/dark)
        if r > 0.4 or g > 0.4 or b > 0.4:
            continue
        
        # Check line width
        if line["width"] < min_width or line["width"] > max_width:
            continue
        
        walls.append(line)
    
    return walls


def detect_grid_lines(
    page: "fitz.Page",
) -> Dict[str, Any]:
    """
    Detect grid lines (typically dashed, light gray).
    
    Returns:
        Dict with horizontal/vertical grid lines and grid_detected flag
    """
    lines = extract_lines(page, min_length=100)
    
    horizontal = []
    vertical = []
    
    for line in lines:
        # Grid lines are typically dashed
        if line["dashes"] is None:
            continue
        
        # Grid lines are typically light gray
        r, g, b = line["color"][:3] if line["color"] else (0, 0, 0)
        if r < 0.5 and g < 0.5 and b < 0.5:
            continue  # Too dark for grid
        
        # Check orientation
        angle = line["angle"] % 180
        
        if angle < 5 or angle > 175:  # Horizontal
            horizontal.append({
                "y": (line["start"][1] + line["end"][1]) / 2,
                "x0": min(line["start"][0], line["end"][0]),
                "x1": max(line["start"][0], line["end"][0]),
                "length": line["length"],
            })
        elif 85 < angle < 95:  # Vertical
            vertical.append({
                "x": (line["start"][0] + line["end"][0]) / 2,
                "y0": min(line["start"][1], line["end"][1]),
                "y1": max(line["start"][1], line["end"][1]),
                "length": line["length"],
            })
    
    horizontal.sort(key=lambda l: l["y"])
    vertical.sort(key=lambda l: l["x"])
    
    return {
        "horizontal": horizontal,
        "vertical": vertical,
        "grid_detected": len(horizontal) > 2 and len(vertical) > 2,
    }


# =============================================================================
# DIMENSION DETECTION
# =============================================================================

def detect_dimensions(
    page: "fitz.Page",
    words: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Detect dimension annotations from text.
    
    Looks for:
    - Numbers with units (1200, 3.5m, 10'-6")
    - Scale indicators (1:100)
    - Dimension text near lines
    
    Returns:
        List of detected dimensions
    """
    if words is None:
        words = extract_words_with_positions(page)
    
    dimensions = []
    
    for word in words:
        text = word["text"].strip()
        
        # Skip very short or very long text
        if len(text) < 2 or len(text) > 20:
            continue
        
        # Check for dimension patterns
        for pattern, unit_type in DIMENSION_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    if unit_type == "scale":
                        value = float(match.group(1))
                        dimensions.append({
                            "text": text,
                            "value": value,
                            "unit": "scale",
                            "scale_ratio": 1.0 / value,
                            "position": {
                                "x0": word["x0"],
                                "y0": word["y0"],
                                "x1": word["x1"],
                                "y1": word["y1"],
                            },
                        })
                    elif unit_type == "ft-in":
                        feet = float(match.group(1))
                        inches = float(match.group(2))
                        total_inches = feet * 12 + inches
                        dimensions.append({
                            "text": text,
                            "value": total_inches * 25.4,  # Convert to mm
                            "unit": "mm",
                            "position": {
                                "x0": word["x0"],
                                "y0": word["y0"],
                                "x1": word["x1"],
                                "y1": word["y1"],
                            },
                        })
                    else:
                        value = float(match.group(1))
                        if unit_type == "m":
                            value *= 1000  # Convert to mm
                        
                        # Validate reasonable range (10mm to 100m)
                        if 10 <= value <= 100000:
                            dimensions.append({
                                "text": text,
                                "value": value,
                                "unit": "mm",
                                "position": {
                                    "x0": word["x0"],
                                    "y0": word["y0"],
                                    "x1": word["x1"],
                                    "y1": word["y1"],
                                },
                            })
                except (ValueError, IndexError):
                    continue
                break
    
    return dimensions


def detect_scale(
    page: "fitz.Page",
    words: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Detect scale indicator on the page.
    
    Looks for:
    - Scale ratios: 1:100, 1:50
    - Scale bars with measurements
    - Text like "SCALE 1:200"
    
    Returns:
        Scale dict or None
    """
    if words is None:
        words = extract_words_with_positions(page)
    
    # Combine all text
    all_text = " ".join(w["text"] for w in words)
    
    # Look for scale patterns
    scale_patterns = [
        (r"scale\s*[:/]?\s*1\s*[:/]\s*(\d{2,4})", "ratio"),
        (r"1\s*[:/]\s*(\d{2,4})", "ratio"),
        (r"(\d+)\s*mm\s*=\s*1\s*m", "metric"),
        (r"(\d+)\s*mm\s*=\s*(\d+)\s*m", "metric"),
    ]
    
    for pattern, scale_type in scale_patterns:
        match = re.search(pattern, all_text, re.IGNORECASE)
        if match:
            if scale_type == "ratio":
                ratio = float(match.group(1))
                return {
                    "type": "ratio",
                    "ratio": ratio,
                    "scale_ratio": 1.0 / ratio,
                    "text": match.group(0),
                }
            elif scale_type == "metric":
                mm = float(match.group(1))
                m = float(match.group(2)) if match.lastindex >= 2 else 1.0
                px_per_m = mm / m
                return {
                    "type": "metric",
                    "mm_per_m": mm,
                    "px_per_m": px_per_m,
                    "text": match.group(0),
                }
    
    return None


# =============================================================================
# ROOM/AREA DETECTION
# =============================================================================

def detect_rooms(
    page: "fitz.Page",
    words: Optional[List[Dict[str, Any]]] = None,
    rectangles: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Detect rooms/areas from text labels and rectangles.
    
    Returns:
        List of room dicts with label, position, area
    """
    if words is None:
        words = extract_words_with_positions(page)
    
    rooms = []
    
    # Look for room labels in text
    for word in words:
        text = word["text"].strip().lower()
        
        # Check if it's a room keyword
        for keyword in ROOM_KEYWORDS:
            if keyword in text:
                rooms.append({
                    "label": word["text"].strip(),
                    "type": "text_label",
                    "position": {
                        "x0": word["x0"],
                        "y0": word["y0"],
                        "x1": word["x1"],
                        "y1": word["y1"],
                    },
                })
                break
    
    # Look for enclosed rectangles (potential rooms)
    if rectangles:
        for rect in rectangles:
            # Rectangles with fills could be rooms
            if rect["fill"] and rect["area"] > 10000:  # > ~100x100 points
                rooms.append({
                    "label": "Enclosed Area",
                    "type": "rectangle",
                    "position": {
                        "x0": rect["x0"],
                        "y0": rect["y0"],
                        "x1": rect["x1"],
                        "y1": rect["y1"],
                    },
                    "area": rect["area"],
                })
    
    return rooms


def detect_materials(
    words: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Detect material references in text.
    
    Returns:
        List of material dicts with keyword, context
    """
    if words is None:
        return []
    
    materials = []
    seen = set()
    
    for word in words:
        text = word["text"].strip().lower()
        
        for keyword in MATERIAL_KEYWORDS:
            if keyword in text and keyword not in seen:
                materials.append({
                    "keyword": keyword,
                    "text": word["text"].strip(),
                    "position": {
                        "x0": word["x0"],
                        "y0": word["y0"],
                        "x1": word["x1"],
                        "y1": word["y1"],
                    },
                })
                seen.add(keyword)
                break
    
    return materials


def detect_colours(
    words: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Detect colour references in text.
    
    Returns:
        List of colour dicts
    """
    if words is None:
        return []
    
    colours = []
    seen = set()
    
    for word in words:
        text = word["text"].strip().lower()
        
        for keyword in COLOUR_KEYWORDS:
            if keyword in text and keyword not in seen:
                colours.append({
                    "keyword": keyword,
                    "text": word["text"].strip(),
                    "position": {
                        "x0": word["x0"],
                        "y0": word["y0"],
                        "x1": word["x1"],
                        "y1": word["y1"],
                    },
                })
                seen.add(keyword)
                break
    
    return colours


# =============================================================================
# PAGE CLASSIFICATION
# =============================================================================

def classify_page_offline(
    page: "fitz.Page",
    text: str,
    file_name: str,
    page_no: int,
) -> Tuple[str, str, Dict[str, Any]]:
    """
    Classify page type using text and visual features (no AI).
    
    Returns:
        Tuple of (page_type, label, metadata)
    """
    lower = f"{file_name} {text}".lower()
    page_type = "Other"
    
    # Keyword patterns for page types
    patterns = [
        ("Title / Drawing Register", [
            "drawing register", "drawing schedule", "title sheet",
            "project title", "job number", "drawing number",
        ]),
        ("Reflected Ceiling Plan", [
            "reflected ceiling", "rcp", "ceiling plan",
        ]),
        ("Floor Plan", [
            "floor plan", "proposed plan", "general arrangement",
            "ground floor", "first floor", "level 1", "level 2",
        ]),
        ("Roof Plan", [
            "roof plan", "roof layout",
        ]),
        ("Elevation", [
            "elevation", "north elev", "south elev", "east elev",
            "west elev", "front elev", "rear elev", "side elev",
        ]),
        ("Section", [
            "section", "cross section", "long section",
        ]),
        ("Door / Window Schedule", [
            "door schedule", "window schedule", "door elevations",
            "window elevations", "joinery schedule",
        ]),
        ("Finishes Schedule", [
            "finish schedule", "finishes schedule", "colour schedule",
            "paint schedule", "material schedule",
        ]),
        ("Specification", [
            "specification", "painting specification",
            "architectural specification", "scope of works",
        ]),
        ("Structural", [
            "structural", "steel framing", "footing", "foundation",
            "beam", "column", "slab",
        ]),
        ("Services", [
            "mechanical", "electrical", "hydraulic", "fire services",
            "plumbing", "hvac", "switchboard",
        ]),
        ("Landscape / Civil", [
            "civil", "landscape", "line marking", "pavement",
            "drainage", "stormwater", "site plan",
        ]),
    ]
    
    for candidate, keywords in patterns:
        if any(kw in lower for kw in keywords):
            page_type = candidate
            break
    
    # Extract drawing number
    drawing_match = re.search(
        r"\b([A-Z]{1,3}\d{2,4}(?:[-/.][A-Z0-9]+)?)\b", text
    )
    label = drawing_match.group(1) if drawing_match else f"Page {page_no}"
    
    # Analyze visual features
    metadata = {}
    
    # Text density
    text_dict = page.get_text("dict")
    total_chars = 0
    for block in text_dict["blocks"]:
        if block["type"] == 0:
            for line in block["lines"]:
                for span in line["spans"]:
                    total_chars += len(span["text"])
    
    page_area = page.rect.width * page.rect.height
    metadata["text_density"] = total_chars / page_area if page_area > 0 else 0
    
    # Line count
    drawings = page.get_drawings()
    line_count = sum(1 for p in drawings for item in p["items"] if item[0] == "l")
    rect_count = sum(1 for p in drawings for item in p["items"] if item[0] == "re")
    metadata["line_count"] = line_count
    metadata["rect_count"] = rect_count
    
    # Image count
    metadata["image_count"] = len(page.get_images())
    
    # Scale detection
    scale = detect_scale(page)
    if scale:
        metadata["scale"] = scale
    
    # Classification hints
    metadata["hints"] = []
    
    if metadata["text_density"] < 0.001 and line_count > 50:
        metadata["hints"].append("Low text + many lines = likely Floor Plan/Elevation")
    if metadata["text_density"] > 0.01 and line_count < 10:
        metadata["hints"].append("High text + few lines = likely Schedule/Specification")
    if metadata["image_count"] > 5:
        metadata["hints"].append("Many images = likely Render page")
    if rect_count > 20:
        metadata["hints"].append("Many rectangles = likely Floor Plan")
    
    return page_type, label, metadata


# =============================================================================
# TAKEOFF GENERATION (No AI)
# =============================================================================

def generate_takeoff_offline(
    pdf_path: str | Path,
    pages: Optional[List[int]] = None,
    use_ocr: bool = True,
    progress_cb: Optional[callable] = None,
) -> pd.DataFrame:
    """
    Generate a painting takeoff from a PDF without AI.
    
    Extracts:
    - Page text and classifications
    - Dimensions
    - Materials
    - Colours
    - Rooms/areas
    - Walls
    
    Returns:
        DataFrame with takeoff rows
    """
    pdf_path = Path(pdf_path)
    
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed")
    
    # Extract text from all pages
    all_text = extract_text_offline(pdf_path, pages=pages, use_ocr=use_ocr)
    
    pdf = fitz.open(str(pdf_path))
    
    if pages is None:
        pages = list(range(1, len(pdf) + 1))
    
    takeoff_rows = []
    total_pages = len(pages)
    
    for i, page_no in enumerate(pages):
        if page_no < 1 or page_no > len(pdf):
            continue
        
        page = pdf[page_no - 1]
        text = all_text.get(page_no, "")
        
        # Classify page
        page_type, label, meta = classify_page_offline(
            page, text, pdf_path.name, page_no
        )
        
        # Extract features
        words = extract_words_with_positions(page)
        dimensions = detect_dimensions(page, words)
        materials = detect_materials(words)
        colours = detect_colours(words)
        rooms = detect_rooms(page, words)
        walls = detect_walls(page)
        scale = detect_scale(page, words)
        
        # Generate takeoff rows based on page type
        if "Floor Plan" in page_type:
            # Internal walls
            if walls:
                takeoff_rows.append({
                    "page_no": page_no,
                    "page_type": page_type,
                    "drawing": label,
                    "section": "Internal",
                    "element": "Walls",
                    "description": f"Internal walls - {len(walls)} wall segments detected",
                    "unit": "lm",
                    "quantity": sum(w["length"] for w in walls),
                    "scale": scale.get("text", "") if scale else "",
                    "notes": f"{len(walls)} walls, {len(dimensions)} dimensions found",
                })
            
            # Rooms
            for room in rooms:
                takeoff_rows.append({
                    "page_no": page_no,
                    "page_type": page_type,
                    "drawing": label,
                    "section": "Internal",
                    "element": room["label"],
                    "description": f"Room label detected",
                    "unit": "m2",
                    "quantity": 0,  # Needs manual measurement
                    "scale": scale.get("text", "") if scale else "",
                    "notes": "Room detected, area to be measured",
                })
        
        elif "Elevation" in page_type:
            # External walls
            if walls:
                takeoff_rows.append({
                    "page_no": page_no,
                    "page_type": page_type,
                    "drawing": label,
                    "section": "External",
                    "element": "Walls",
                    "description": f"External walls - {len(walls)} wall segments",
                    "unit": "m2",
                    "quantity": 0,  # Needs manual measurement
                    "scale": scale.get("text", "") if scale else "",
                    "notes": f"{len(walls)} walls, {len(materials)} materials found",
                })
            
            # Materials
            for mat in materials:
                takeoff_rows.append({
                    "page_no": page_no,
                    "page_type": page_type,
                    "drawing": label,
                    "section": "External",
                    "element": mat["keyword"].title(),
                    "description": f"Material: {mat['text']}",
                    "unit": "m2",
                    "quantity": 0,
                    "scale": scale.get("text", "") if scale else "",
                    "notes": f"Material detected: {mat['keyword']}",
                })
        
        elif "Section" in page_type:
            # Section dimensions
            for dim in dimensions:
                if dim["unit"] == "mm" and 100 <= dim["value"] <= 10000:
                    takeoff_rows.append({
                        "page_no": page_no,
                        "page_type": page_type,
                        "drawing": label,
                        "section": "Section",
                        "element": "Dimension",
                        "description": f"Dimension: {dim['text']}",
                        "unit": "mm",
                        "quantity": dim["value"],
                        "scale": scale.get("text", "") if scale else "",
                        "notes": f"Auto-detected dimension",
                    })
        
        elif "Schedule" in page_type or "Specification" in page_type:
            # Extract schedule items
            for word in words:
                text_lower = word["text"].strip().lower()
                if any(kw in text_lower for kw in ["paint", "coat", "primer", "sealer"]):
                    takeoff_rows.append({
                        "page_no": page_no,
                        "page_type": page_type,
                        "drawing": label,
                        "section": "Schedule",
                        "element": word["text"].strip(),
                        "description": "Schedule item detected",
                        "unit": "item",
                        "quantity": 1,
                        "scale": "",
                        "notes": "From schedule/specification",
                    })
        
        # Report progress
        if progress_cb:
            progress_cb(i + 1, total_pages, page_no)
    
    pdf.close()
    
    if not takeoff_rows:
        # Return empty DataFrame with correct columns
        return pd.DataFrame(columns=[
            "page_no", "page_type", "drawing", "section", "element",
            "description", "unit", "quantity", "scale", "notes",
        ])
    
    return pd.DataFrame(takeoff_rows)


# =============================================================================
# FULL ANALYSIS
# =============================================================================

def analyze_page_offline(
    pdf_path: str | Path,
    page_no: int,
    use_ocr: bool = True,
) -> Dict[str, Any]:
    """
    Comprehensive offline analysis of a single page.
    
    Returns:
        Dict with all extracted data
    """
    pdf_path = Path(pdf_path)
    
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed")
    
    pdf = fitz.open(str(pdf_path))
    
    if page_no < 1 or page_no > len(pdf):
        pdf.close()
        raise ValueError(f"Invalid page number: {page_no}")
    
    page = pdf[page_no - 1]
    
    # Extract text
    text = page.get_text("text") or ""
    
    # Try OCR if text is sparse
    if use_ocr and pymupdf4llm is not None:
        try:
            ocr_text = pymupdf4llm.to_markdown(
                str(pdf_path), pages=[page_no - 1]
            )
            if len(ocr_text) > len(text):
                text = ocr_text
        except Exception:
            pass
    
    # Extract features
    words = extract_words_with_positions(page)
    blocks = extract_text_blocks_with_positions(page)
    lines = extract_lines(page)
    rectangles = extract_rectangles(page)
    walls = detect_walls(page)
    grid = detect_grid_lines(page)
    dimensions = detect_dimensions(page, words)
    scale = detect_scale(page, words)
    rooms = detect_rooms(page, words, rectangles)
    materials = detect_materials(words)
    colours = detect_colours(words)
    
    # Classify
    page_type, label, meta = classify_page_offline(
        page, text, pdf_path.name, page_no
    )
    
    pdf.close()
    
    return {
        "page_no": page_no,
        "file_name": pdf_path.name,
        "page_type": page_type,
        "label": label,
        "classification": meta,
        "text": text,
        "text_length": len(text),
        "word_count": len(words),
        "block_count": len(blocks),
        "line_count": len(lines),
        "rect_count": len(rectangles),
        "wall_count": len(walls),
        "words": words,
        "blocks": blocks,
        "lines": lines,
        "rectangles": rectangles,
        "walls": walls,
        "grid": grid,
        "dimensions": dimensions,
        "scale": scale,
        "rooms": rooms,
        "materials": materials,
        "colours": colours,
    }


def generate_report(
    analysis: Dict[str, Any],
) -> str:
    """
    Generate a human-readable report from analysis.
    
    Args:
        analysis: Output from analyze_page_offline()
    
    Returns:
        Report string
    """
    lines = [
        "=" * 60,
        f"OFFLINE PLAN ANALYSIS - Page {analysis['page_no']}",
        "=" * 60,
        f"File: {analysis['file_name']}",
        f"Type: {analysis['page_type']}",
        f"Label: {analysis['label']}",
        "",
        "--- Text Extraction ---",
        f"Text length: {analysis['text_length']} chars",
        f"Words: {analysis['word_count']}",
        f"Text blocks: {analysis['block_count']}",
        "",
        "--- Vector Graphics ---",
        f"Lines: {analysis['line_count']}",
        f"Rectangles: {analysis['rect_count']}",
        f"Walls detected: {analysis['wall_count']}",
        f"Grid lines: {'Yes' if analysis['grid']['grid_detected'] else 'No'}",
        "",
        "--- Measurements ---",
        f"Dimensions found: {len(analysis['dimensions'])}",
    ]
    
    for dim in analysis["dimensions"][:10]:
        lines.append(f"  - {dim['text']}: {dim['value']:.0f} {dim['unit']}")
    
    if analysis["scale"]:
        lines.append(f"\nScale: {analysis['scale']['text']}")
    
    lines.append("\n--- Rooms/Areas ---")
    for room in analysis["rooms"][:10]:
        lines.append(f"  - {room['label']}")
    
    lines.append("\n--- Materials ---")
    for mat in analysis["materials"][:10]:
        lines.append(f"  - {mat['keyword']}: {mat['text']}")
    
    lines.append("\n--- Colours ---")
    for colour in analysis["colours"][:10]:
        lines.append(f"  - {colour['keyword']}: {colour['text']}")
    
    lines.append("\n--- Walls ---")
    for wall in analysis["walls"][:5]:
        length_m = wall["length"] / (analysis["scale"]["scale_ratio"] * 1000) if analysis["scale"] else wall["length"]
        lines.append(
            f"  - {wall['length']:.0f} pts "
            f"({'~{:.1f}m'.format(length_m) if analysis['scale'] else ''})"
        )
    
    # Summary
    lines.extend([
        "",
        "=" * 60,
        "SUMMARY",
        "=" * 60,
        f"Page type: {analysis['page_type']}",
        f"Scale: {analysis['scale']['text'] if analysis['scale'] else 'Not detected'}",
        f"Total walls: {analysis['wall_count']}",
        f"Total dimensions: {len(analysis['dimensions'])}",
        f"Total rooms: {len(analysis['rooms'])}",
        f"Total materials: {len(analysis['materials'])}",
        f"Total colours: {len(analysis['colours'])}",
    ])
    
    if analysis["classification"].get("hints"):
        lines.append("\nHints:")
        for hint in analysis["classification"]["hints"]:
            lines.append(f"  - {hint}")
    
    return "\n".join(lines)


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python pb_planreader_offline.py <pdf_path> [page_no]")
        print()
        print("Examples:")
        print("  python pb_planreader_offline.py plan.pdf")
        print("  python pb_planreader_offline.py plan.pdf 5")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    page_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    
    print(f"Analyzing {pdf_path}, page {page_no}...")
    print()
    
    analysis = analyze_page_offline(pdf_path, page_no)
    report = generate_report(analysis)
    print(report)
    
    # Also generate takeoff
    print("\n\nGenerating takeoff...")
    takeoff = generate_takeoff_offline(pdf_path, pages=[page_no])
    print(f"\nTakeoff rows: {len(takeoff)}")
    if not takeoff.empty:
        print(takeoff.to_string(index=False))
