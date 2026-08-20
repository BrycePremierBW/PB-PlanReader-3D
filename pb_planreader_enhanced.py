"""
Enhanced Plan Reading Module for Premier Brushworks PlanReader.

This module adds advanced PDF analysis capabilities:
1. OCR support for scanned plans (via PyMuPDF4LLM)
2. Vector graphics extraction (walls, dimensions, grid lines)
3. Structured text extraction with coordinates
4. Enhanced page classification
5. Image preprocessing for better OCR accuracy

Integration: Import and call these functions from pb_planreader_3d_app.py
"""

from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


# =============================================================================
# 1. OCR SUPPORT (PyMuPDF4LLM)
# =============================================================================

def extract_text_with_ocr(
    pdf_path: str | Path,
    pages: Optional[List[int]] = None,
    force_ocr: bool = False,
    use_ocr: bool = True,
) -> str:
    """
    Extract text from PDF with automatic OCR for scanned pages.
    
    Args:
        pdf_path: Path to PDF file
        pages: Optional list of page numbers (0-indexed) to process
        force_ocr: Force OCR on all pages (even text-based ones)
        use_ocr: Enable/disable OCR (False = skip scanned pages)
    
    Returns:
        Extracted text in Markdown format
    """
    if pymupdf4llm is None:
        raise RuntimeError(
            "pymupdf4llm is not installed. "
            "Add 'PyMuPDF4LLM>=0.16.0' to requirements.txt"
        )
    
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    
    kwargs = {}
    if pages is not None:
        kwargs["pages"] = pages
    if force_ocr:
        kwargs["force_ocr"] = True
    if not use_ocr:
        kwargs["use_ocr"] = False
    
    # pymupdf4llm auto-detects scanned pages and applies OCR
    md_text = pymupdf4llm.to_markdown(str(pdf_path), **kwargs)
    return md_text


def extract_page_text_with_ocr(
    pdf_path: str | Path,
    page_no: int,
    force_ocr: bool = False,
) -> str:
    """
    Extract text from a single page with OCR if needed.
    
    Args:
        pdf_path: Path to PDF file
        page_no: Page number (1-indexed)
        force_ocr: Force OCR even on text-based pages
    
    Returns:
        Extracted text
    """
    return extract_text_with_ocr(
        pdf_path,
        pages=[page_no - 1],  # Convert to 0-indexed
        force_ocr=force_ocr,
    )


# =============================================================================
# 2. VECTOR GRAPHICS EXTRACTION
# =============================================================================

def extract_drawings(
    page: "fitz.Page",
    min_width: float = 0.0,
    max_width: float = 10.0,
    colors: Optional[List[Tuple[float, ...]]] = None,
) -> List[Dict[str, Any]]:
    """
    Extract vector graphics from a PDF page.
    
    Args:
        page: PyMuPDF page object
        min_width: Minimum line width to include
        max_width: Maximum line width to include
        colors: Optional list of RGB tuples to filter by
    
    Returns:
        List of drawing paths with metadata
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed")
    
    all_drawings = page.get_drawings()
    filtered = []
    
    for path in all_drawings:
        width = path.get("width", 0)
        
        # Filter by line width
        if width < min_width or width > max_width:
            continue
        
        # Filter by color if specified
        if colors:
            path_color = tuple(path.get("color", (0, 0, 0))[:3])
            if path_color not in colors:
                continue
        
        # Extract line segments
        lines = []
        rectangles = []
        curves = []
        
        for item in path.get("items", []):
            if item[0] == "l":  # Line
                lines.append({
                    "start": (float(item[1].x), float(item[1].y)),
                    "end": (float(item[2].x), float(item[2].y)),
                    "length": float(item[1].distance_to(item[2])),
                })
            elif item[0] == "re":  # Rectangle
                rect = item[1]
                rectangles.append({
                    "x0": float(rect.x0),
                    "y0": float(rect.y0),
                    "x1": float(rect.x1),
                    "y1": float(rect.y1),
                    "width": float(rect.width),
                    "height": float(rect.height),
                    "area": float(rect.width * rect.height),
                })
            elif item[0] == "c":  # Curve (Bezier)
                curves.append({
                    "start": (float(item[1].x), float(item[1].y)),
                    "control1": (float(item[2].x), float(item[2].y)),
                    "control2": (float(item[3].x), float(item[3].y)),
                    "end": (float(item[4].x), float(item[4].y)),
                })
        
        filtered.append({
            "color": path.get("color"),
            "fill": path.get("fill"),
            "width": width,
            "dashes": path.get("dashes"),
            "rect": {
                "x0": float(path["rect"].x0),
                "y0": float(path["rect"].y0),
                "x1": float(path["rect"].x1),
                "y1": float(path["rect"].y1),
            },
            "lines": lines,
            "rectangles": rectangles,
            "curves": curves,
            "line_count": len(lines),
            "rect_count": len(rectangles),
            "curve_count": len(curves),
        })
    
    return filtered


def detect_walls(
    page: "fitz.Page",
    min_width: float = 0.5,
    max_width: float = 5.0,
) -> List[Dict[str, Any]]:
    """
    Detect wall lines from vector graphics.
    
    Walls are typically:
    - Black or dark colored lines
    - Medium to thick width (0.5-5.0 points)
    - Longer than 50 points
    
    Args:
        page: PyMuPDF page object
        min_width: Minimum line width for walls
        max_width: Maximum line width for walls
    
    Returns:
        List of detected wall segments
    """
    drawings = extract_drawings(page, min_width=min_width, max_width=max_width)
    
    walls = []
    for drawing in drawings:
        # Filter: dark colors only (black, dark gray, dark blue)
        color = drawing.get("color")
        if color is None:
            continue
        
        r, g, b = color[:3] if len(color) >= 3 else (0, 0, 0)
        
        # Check if color is dark (all channels < 0.3)
        if r > 0.3 or g > 0.3 or b > 0.3:
            continue
        
        # Filter: lines must be longer than 50 points
        for line in drawing.get("lines", []):
            if line["length"] >= 50:
                walls.append({
                    "start": line["start"],
                    "end": line["end"],
                    "length": line["length"],
                    "width": drawing["width"],
                    "color": color,
                })
    
    return walls


def detect_dimensions(
    page: "fitz.Page",
    text_blocks: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Detect dimension annotations (e.g., "1200", "3.5m").
    
    Looks for:
    - Numeric text near thin lines
    - Common dimension patterns
    - Scale indicators
    
    Args:
        page: PyMuPDF page object
        text_blocks: Optional pre-extracted text blocks with positions
    
    Returns:
        List of detected dimensions
    """
    if text_blocks is None:
        text_blocks = extract_text_blocks(page)
    
    # Pattern for dimensions: numbers with optional units
    dim_pattern = re.compile(
        r"(\d+(?:\.\d+)?)\s*(mm|m|cm|ft|in|'-?\d+\"?)",
        re.IGNORECASE
    )
    
    # Pattern for bare numbers (likely dimensions)
    bare_number_pattern = re.compile(r"^\d{2,5}$")
    
    dimensions = []
    
    for block in text_blocks:
        text = block.get("text", "").strip()
        
        # Check for dimension pattern
        match = dim_pattern.search(text)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            dimensions.append({
                "text": text,
                "value": value,
                "unit": unit,
                "position": {
                    "x0": block["x0"],
                    "y0": block["y0"],
                    "x1": block["x1"],
                    "y1": block["y1"],
                },
            })
            continue
        
        # Check for bare numbers (potential dimensions)
        if bare_number_pattern.match(text):
            value = float(text)
            if 10 <= value <= 100000:  # Reasonable dimension range in mm
                dimensions.append({
                    "text": text,
                    "value": value,
                    "unit": "mm",
                    "position": {
                        "x0": block["x0"],
                        "y0": block["y0"],
                        "x1": block["x1"],
                        "y1": block["y1"],
                    },
                    "confidence": "low",
                })
    
    return dimensions


def detect_grid_lines(
    page: "fitz.Page",
    tolerance: float = 2.0,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Detect grid lines (typically dashed, light gray).
    
    Args:
        page: PyMuPDF page object
        tolerance: Tolerance for line alignment (in points)
    
    Returns:
        Dict with 'horizontal' and 'vertical' grid lines
    """
    drawings = extract_drawings(page)
    
    horizontal = []
    vertical = []
    
    for drawing in drawings:
        # Grid lines are typically dashed and light colored
        dashes = drawing.get("dashes")
        color = drawing.get("color")
        
        if dashes is None:
            continue
        
        # Check if color is light gray
        if color:
            r, g, b = color[:3] if len(color) >= 3 else (0, 0, 0)
            if r < 0.5 and g < 0.5 and b < 0.5:
                continue  # Too dark for grid
        
        for line in drawing.get("lines", []):
            start, end = line["start"], line["end"]
            
            # Check if line is horizontal or vertical
            if abs(start[1] - end[1]) < tolerance:  # Horizontal
                horizontal.append({
                    "y": (start[1] + end[1]) / 2,
                    "x0": min(start[0], end[0]),
                    "x1": max(start[0], end[0]),
                    "length": line["length"],
                })
            elif abs(start[0] - end[0]) < tolerance:  # Vertical
                vertical.append({
                    "x": (start[0] + end[0]) / 2,
                    "y0": min(start[1], end[1]),
                    "y1": max(start[1], end[1]),
                    "length": line["length"],
                })
    
    # Sort by position
    horizontal.sort(key=lambda l: l["y"])
    vertical.sort(key=lambda l: l["x"])
    
    return {
        "horizontal": horizontal,
        "vertical": vertical,
        "grid_detected": len(horizontal) > 2 and len(vertical) > 2,
    }


# =============================================================================
# 3. STRUCTURED TEXT EXTRACTION
# =============================================================================

def extract_text_blocks(
    page: "fitz.Page",
    min_chars: int = 1,
) -> List[Dict[str, Any]]:
    """
    Extract text blocks with position information.
    
    Args:
        page: PyMuPDF page object
        min_chars: Minimum characters to include a block
    
    Returns:
        List of text blocks with bounding boxes
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed")
    
    blocks = page.get_text("blocks")
    result = []
    
    for block in blocks:
        x0, y0, x1, y1, text, block_no, block_type = block
        
        if block_type != 0:  # Skip image blocks
            continue
        
        text = text.strip()
        if len(text) < min_chars:
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
    min_len: int = 1,
) -> List[Dict[str, Any]]:
    """
    Extract individual words with their positions.
    
    Args:
        page: PyMuPDF page object
        min_len: Minimum word length
    
    Returns:
        List of words with bounding boxes
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed")
    
    words = page.get_text("words")
    result = []
    
    for word in words:
        x0, y0, x1, y1, word_text, block_no, line_no, word_no = word
        
        if len(word_text) < min_len:
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


def extract_text_by_region(
    page: "fitz.Page",
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> str:
    """
    Extract text from a specific region of the page.
    
    Args:
        page: PyMuPDF page object
        x0, y0, x1, y1: Region coordinates
    
    Returns:
        Text within the region
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed")
    
    rect = fitz.Rect(x0, y0, x1, y1)
    return page.get_text("text", clip=rect)


def find_text_near_point(
    words: List[Dict[str, Any]],
    x: float,
    y: float,
    max_distance: float = 50.0,
) -> List[Dict[str, Any]]:
    """
    Find text near a specific point.
    
    Args:
        words: List of words with positions
        x, y: Target point
        max_distance: Maximum distance to search
    
    Returns:
        List of nearby words sorted by distance
    """
    import math
    
    nearby = []
    for word in words:
        word_cx = (word["x0"] + word["x1"]) / 2
        word_cy = (word["y0"] + word["y1"]) / 2
        dist = math.sqrt((word_cx - x) ** 2 + (word_cy - y) ** 2)
        
        if dist <= max_distance:
            nearby.append({
                **word,
                "distance": dist,
            })
    
    nearby.sort(key=lambda w: w["distance"])
    return nearby


# =============================================================================
# 4. ENHANCED PAGE CLASSIFICATION
# =============================================================================

def enhanced_classify_page(
    page: "fitz.Page",
    text: str,
    file_name: str,
    page_no: int,
) -> Tuple[str, str, Dict[str, Any]]:
    """
    Enhanced page classification using text, vector graphics, and visual features.
    
    Args:
        page: PyMuPDF page object
        text: Extracted text
        file_name: PDF filename
        page_no: Page number
    
    Returns:
        Tuple of (page_type, label, metadata)
    """
    # Basic classification (existing logic)
    lower = f"{file_name} {text}".lower()
    page_type = "Other"
    
    patterns = [
        ("Title / Drawing Register", ["drawing register", "drawing schedule", "title sheet"]),
        ("Reflected Ceiling Plan", ["reflected ceiling", "rcp", "ceiling plan"]),
        ("Floor Plan", ["floor plan", "proposed plan", "general arrangement"]),
        ("Roof Plan", ["roof plan"]),
        ("Elevation", ["elevation", "north elev", "south elev", "east elev", "west elev"]),
        ("Render / Artist's Impression", [
            "artist's impression", "artists impression", "artists rendering",
            "artist rendering", "3d view", "3d render", "concept image",
            "perspective render", "concept render", "visualisation",
            "visualization", "render", "impression",
        ]),
        ("Section", ["section", "cross section"]),
        ("Door / Window Schedule", ["door schedule", "window schedule", "door elevations"]),
        ("Finishes Schedule", ["finish schedule", "finishes schedule", "colour schedule", "paint schedule"]),
        ("Specification", ["specification", "painting specification", "architectural specification"]),
        ("Structural", ["structural", "steel framing", "footing"]),
        ("Services", ["mechanical", "electrical", "hydraulic", "fire services"]),
        ("Landscape / Civil", ["civil", "landscape", "line marking", "pavement"]),
    ]
    
    for candidate, words in patterns:
        if any(word in lower for word in words):
            page_type = candidate
            break
    
    # Extract drawing number
    drawing_match = re.search(r"\b([A-Z]{1,3}\d{2,4}(?:[-/.][A-Z0-9]+)?)\b", text)
    label = drawing_match.group(1) if drawing_match else f"Page {page_no}"
    
    # Enhanced analysis
    metadata = {}
    
    # Text density analysis
    text_dict = page.get_text("dict")
    total_chars = 0
    for block in text_dict["blocks"]:
        if block["type"] == 0:  # Text block
            for line in block["lines"]:
                for span in line["spans"]:
                    total_chars += len(span["text"])
    
    page_area = page.rect.width * page.rect.height
    text_density = total_chars / page_area if page_area > 0 else 0
    metadata["text_density"] = text_density
    
    # Vector graphics analysis
    drawings = page.get_drawings()
    line_count = sum(1 for p in drawings for item in p["items"] if item[0] == "l")
    rect_count = sum(1 for p in drawings for item in p["items"] if item[0] == "re")
    metadata["line_count"] = line_count
    metadata["rect_count"] = rect_count
    
    # Image count
    image_count = len(page.get_images())
    metadata["image_count"] = image_count
    
    # Classification heuristics
    metadata["classification_hint"] = ""
    
    if text_density < 0.001 and line_count > 50:
        metadata["classification_hint"] = "Low text, many lines - likely Floor Plan or Elevation"
    elif text_density > 0.01 and line_count < 10:
        metadata["classification_hint"] = "High text, few lines - likely Schedule or Specification"
    elif image_count > 5 and text_density < 0.002:
        metadata["classification_hint"] = "Many images, low text - likely Render page"
    elif rect_count > 20 and line_count > 100:
        metadata["classification_hint"] = "Many rectangles and lines - likely Floor Plan"
    
    # Scale detection
    scale_patterns = [
        (r"1\s*[:/]\s*(\d{2,4})", "ratio"),
        (r"scale\s*[:/]?\s*1\s*[:/]\s*(\d{2,4})", "ratio"),
        (r"(\d+)\s*mm\s*=\s*1\s*m", "metric"),
        (r"1\s*in\s*(\d+)", "imperial"),
    ]
    
    for pattern, scale_type in scale_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            metadata["scale_found"] = match.group(0)
            metadata["scale_type"] = scale_type
            break
    
    return page_type, label, metadata


# =============================================================================
# 5. IMAGE PREPROCESSING
# =============================================================================

def preprocess_image_for_ocr(
    image_path: str | Path,
    output_path: Optional[str | Path] = None,
) -> str:
    """
    Preprocess image for better OCR accuracy.
    
    Args:
        image_path: Input image path
        output_path: Optional output path (overwrites input if None)
    
    Returns:
        Path to processed image
    """
    if cv2 is None or np is None:
        raise RuntimeError(
            "OpenCV is not installed. "
            "Add 'opencv-python-headless>=4.14.0' to requirements.txt"
        )
    
    image_path = Path(image_path)
    if output_path is None:
        output_path = image_path
    else:
        output_path = Path(output_path)
    
    # Read image
    img = cv2.imread(str(image_path))
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {image_path}")
    
    # Convert to grayscale
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img
    
    # Adaptive thresholding (handles uneven lighting)
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    
    # Denoise
    denoised = cv2.medianBlur(thresh, 3)
    
    # Save processed image
    cv2.imwrite(str(output_path), denoised)
    
    return str(output_path)


def preprocess_page_image(
    image_bytes: bytes,
) -> bytes:
    """
    Preprocess page image bytes for better OCR.
    
    Args:
        image_bytes: Input image as bytes
    
    Returns:
        Processed image as bytes
    """
    if cv2 is None or np is None:
        return image_bytes  # Return as-is if OpenCV not available
    
    # Decode image
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    if img is None:
        return image_bytes
    
    # Convert to grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # Adaptive thresholding
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    
    # Encode back to bytes
    _, buffer = cv2.imencode(".png", thresh)
    return buffer.tobytes()


# =============================================================================
# 6. INTEGRATION HELPERS
# =============================================================================

def analyze_page_comprehensive(
    pdf_path: str | Path,
    page_no: int,
) -> Dict[str, Any]:
    """
    Comprehensive page analysis combining all methods.
    
    Args:
        pdf_path: Path to PDF file
        page_no: Page number (1-indexed)
    
    Returns:
        Dict with all extracted data
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed")
    
    pdf_path = Path(pdf_path)
    pdf = fitz.open(str(pdf_path))
    
    if page_no < 1 or page_no > len(pdf):
        pdf.close()
        raise ValueError(f"Invalid page number: {page_no}")
    
    page = pdf[page_no - 1]  # Convert to 0-indexed
    
    result = {
        "page_no": page_no,
        "file_name": pdf_path.name,
        "page_width": page.rect.width,
        "page_height": page.rect.height,
    }
    
    # 1. Extract text (with OCR if needed)
    try:
        text_with_ocr = extract_page_text_with_ocr(pdf_path, page_no)
        result["text_ocr"] = text_with_ocr
    except Exception as e:
        result["text_ocr_error"] = str(e)
    
    # 2. Extract text blocks with positions
    text_blocks = extract_text_blocks(page)
    result["text_blocks"] = text_blocks
    result["text_block_count"] = len(text_blocks)
    
    # 3. Extract words with positions
    words = extract_words_with_positions(page)
    result["words"] = words
    result["word_count"] = len(words)
    
    # 4. Extract vector graphics
    drawings = extract_drawings(page)
    result["drawings"] = drawings
    result["drawing_count"] = len(drawings)
    
    # 5. Detect walls
    walls = detect_walls(page)
    result["walls"] = walls
    result["wall_count"] = len(walls)
    
    # 6. Detect dimensions
    dimensions = detect_dimensions(page, text_blocks)
    result["dimensions"] = dimensions
    result["dimension_count"] = len(dimensions)
    
    # 7. Detect grid lines
    grid = detect_grid_lines(page)
    result["grid"] = grid
    result["grid_detected"] = grid["grid_detected"]
    
    # 8. Enhanced classification
    text = page.get_text("text") or ""
    page_type, label, meta = enhanced_classify_page(
        page, text, pdf_path.name, page_no
    )
    result["page_type"] = page_type
    result["label"] = label
    result["classification_metadata"] = meta
    
    pdf.close()
    return result


def generate_analysis_summary(analysis: Dict[str, Any]) -> str:
    """
    Generate human-readable summary of page analysis.
    
    Args:
        analysis: Output from analyze_page_comprehensive()
    
    Returns:
        Summary string
    """
    lines = [
        f"=== Page {analysis['page_no']} Analysis ===",
        f"File: {analysis['file_name']}",
        f"Size: {analysis['page_width']:.0f} x {analysis['page_height']:.0f} points",
        f"Type: {analysis.get('page_type', 'Unknown')}",
        f"Label: {analysis.get('label', 'N/A')}",
        "",
        "--- Text Extraction ---",
        f"Text blocks: {analysis.get('text_block_count', 0)}",
        f"Words: {analysis.get('word_count', 0)}",
        "",
        "--- Vector Graphics ---",
        f"Drawings: {analysis.get('drawing_count', 0)}",
        f"Walls detected: {analysis.get('wall_count', 0)}",
        f"Dimensions found: {analysis.get('dimension_count', 0)}",
        f"Grid lines: {'Yes' if analysis.get('grid_detected') else 'No'}",
    ]
    
    # Add dimension details
    dimensions = analysis.get("dimensions", [])
    if dimensions:
        lines.append("")
        lines.append("--- Dimensions ---")
        for dim in dimensions[:10]:  # Show first 10
            lines.append(f"  {dim['text']} = {dim['value']} {dim.get('unit', '')}")
    
    # Add classification metadata
    meta = analysis.get("classification_metadata", {})
    if meta.get("classification_hint"):
        lines.append("")
        lines.append(f"Hint: {meta['classification_hint']}")
    
    if meta.get("scale_found"):
        lines.append(f"Scale: {meta['scale_found']}")
    
    return "\n".join(lines)


# =============================================================================
# 7. BATCH PROCESSING
# =============================================================================

def analyze_document_comprehensive(
    pdf_path: str | Path,
    pages: Optional[List[int]] = None,
    progress_cb: Optional[callable] = None,
) -> List[Dict[str, Any]]:
    """
    Analyze multiple pages in a document.
    
    Args:
        pdf_path: Path to PDF file
        pages: Optional list of page numbers (1-indexed). None = all pages.
        progress_cb: Optional callback(current, total, page_no)
    
    Returns:
        List of analysis results
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed")
    
    pdf_path = Path(pdf_path)
    pdf = fitz.open(str(pdf_path))
    
    if pages is None:
        pages = list(range(1, len(pdf) + 1))
    
    results = []
    total = len(pages)
    
    for i, page_no in enumerate(pages):
        try:
            analysis = analyze_page_comprehensive(pdf_path, page_no)
            results.append(analysis)
        except Exception as e:
            results.append({
                "page_no": page_no,
                "error": str(e),
            })
        
        if progress_cb:
            progress_cb(i + 1, total, page_no)
    
    pdf.close()
    return results


# =============================================================================
# EXAMPLE USAGE
# =============================================================================

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python pb_planreader_enhanced.py <pdf_path> [page_no]")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    page_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    
    print(f"Analyzing {pdf_path}, page {page_no}...")
    print()
    
    analysis = analyze_page_comprehensive(pdf_path, page_no)
    summary = generate_analysis_summary(analysis)
    print(summary)
