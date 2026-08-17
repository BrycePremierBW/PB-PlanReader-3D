import base64
import io
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import fitz  # PyMuPDF
import pandas as pd
import requests
import streamlit as st

APP_NAME = "PB PlanReader"
DEFAULT_COVERAGE_M2_PER_L = 12.0

ROOT = Path(__file__).resolve().parent
ASSETS_DIR = ROOT / "assets"
LOCAL_DATA_DIR = ROOT / "data"
RENDER_DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
DATA_DIR = RENDER_DATA_DIR if RENDER_DATA_DIR.exists() and os.access(str(RENDER_DATA_DIR), os.W_OK) else LOCAL_DATA_DIR
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

PAINT_KEYWORDS = [
    "paint", "painting", "painter", "coating", "coatings", "primer", "sealer", "undercoat",
    "topcoat", "dulux", "haymes", "taubmans", "resene", "wattyl", "colorbond", "colourbond",
    "colour", "color", "finish", "finishes", "render", "rendered", "cladding", "soffit", "eave",
    "ceiling", "wall", "blockwork", "plasterboard", "fc", "fibre cement", "fiber cement", "door",
    "frame", "skirting", "trim", "gloss", "enamel", "epoxy", "floor coating", "texture", "stain",
]
PAGE_TYPES = {
    "floor_plan": ["floor plan", "ground floor", "level 1", "first floor", "plan"],
    "elevation": ["elevation", "north elevation", "south elevation", "east elevation", "west elevation", "front elevation", "rear elevation"],
    "roof_plan": ["roof plan", "soffit", "reflected ceiling", "ceiling plan"],
    "finishes": ["finish", "finishes", "colour schedule", "color schedule", "materials schedule"],
    "doors_windows": ["door schedule", "window schedule", "louvre", "glazing"],
    "painting_spec": ["painting", "paint systems", "coatings", "sealer", "primer"],
    "site_plan": ["site plan", "locality", "setout", "set out"],
    "specification": ["technical specification", "general requirements", "worksection"],
}


def safe_name(value: str, fallback: str = "file") -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^A-Za-z0-9_.() -]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._-/")
    return value[:120] or fallback


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "$0.00"


def litres_from_area(area_m2: float, coats: float = 2.0) -> float:
    try:
        return round(float(area_m2) * float(coats) / DEFAULT_COVERAGE_M2_PER_L, 2)
    except Exception:
        return 0.0


def job_id_from_name(job_no: str, job_name: str) -> str:
    base = safe_name(f"{job_no}_{job_name}", "job").lower().replace(" ", "_")
    return base or f"job_{int(time.time())}"


def job_dir(job_id: str) -> Path:
    path = JOBS_DIR / safe_name(job_id, "job")
    path.mkdir(parents=True, exist_ok=True)
    (path / "source_files").mkdir(exist_ok=True)
    (path / "converted_images").mkdir(exist_ok=True)
    (path / "exports").mkdir(exist_ok=True)
    return path


def meta_path(job_id: str) -> Path:
    return job_dir(job_id) / "job_meta.json"


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_job(job_id: str) -> Dict[str, Any]:
    return load_json(meta_path(job_id), {})


def save_job(job_id: str, data: Dict[str, Any]) -> None:
    data["job_id"] = job_id
    data["updated_at"] = now_stamp()
    save_json(meta_path(job_id), data)


def list_jobs() -> List[Dict[str, Any]]:
    jobs = []
    for p in sorted(JOBS_DIR.glob("*/job_meta.json")):
        meta = load_json(p, {})
        if meta:
            jobs.append(meta)
    return sorted(jobs, key=lambda j: j.get("updated_at", ""), reverse=True)


def save_uploaded_file(job_id: str, uploaded, subfolder: str = "source_files") -> Path:
    dst_dir = job_dir(job_id) / subfolder
    dst_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_name(uploaded.name, "upload")
    out = dst_dir / filename
    if out.exists():
        stem, suffix = out.stem, out.suffix
        out = dst_dir / f"{stem}_{int(time.time())}{suffix}"
    with open(out, "wb") as f:
        f.write(uploaded.getbuffer())
    return out


def file_record(path: Path, file_type: str, category: str = "Uploaded") -> Dict[str, Any]:
    return {
        "name": path.name,
        "path": str(path),
        "file_type": file_type,
        "category": category,
        "uploaded_at": now_stamp(),
        "size_kb": round(path.stat().st_size / 1024, 1) if path.exists() else 0,
    }


def classify_page(text: str) -> str:
    low = (text or "").lower()
    scores = {}
    for key, words in PAGE_TYPES.items():
        scores[key] = sum(1 for w in words if w in low)
    best = max(scores, key=scores.get) if scores else "other"
    return best if scores.get(best, 0) else "other"


def extract_drawing_number(text: str) -> str:
    candidates = re.findall(r"\b(?:A|AR|DA|WD|SK|S|E|M|H|C)[- ]?\d{2,4}(?:\.\d+)?\b", text or "", flags=re.I)
    return candidates[0].upper().replace(" ", "-") if candidates else ""


def title_from_text(text: str, page_type: str) -> str:
    lines = [re.sub(r"\s+", " ", l).strip() for l in (text or "").splitlines()]
    lines = [l for l in lines if 4 <= len(l) <= 90]
    priority = ["ELEVATION", "FLOOR PLAN", "ROOF PLAN", "FINISH", "SCHEDULE", "PAINT", "SITE PLAN", "SECTION"]
    for word in priority:
        for l in lines[:80]:
            if word in l.upper():
                return l
    return page_type.replace("_", " ").title()


def painting_lines(text: str, limit: int = 120) -> List[str]:
    rows = []
    for raw in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line) < 4:
            continue
        low = line.lower()
        if any(k in low for k in PAINT_KEYWORDS):
            rows.append(line[:260])
    # dedupe preserving order
    out = []
    seen = set()
    for r in rows:
        key = r.lower()
        if key not in seen:
            out.append(r)
            seen.add(key)
        if len(out) >= limit:
            break
    return out


def extract_area_candidates(text: str) -> List[Dict[str, Any]]:
    rows = []
    lines = [re.sub(r"\s+", " ", x).strip() for x in (text or "").splitlines()]
    area_re = re.compile(r"(?P<val>\d+(?:\.\d+)?)\s*(?:m2|m²|sqm|sq\.m|square metres|square meters)\b", re.I)
    lm_re = re.compile(r"(?P<val>\d+(?:\.\d+)?)\s*(?:lm|lineal metres|linear metres|l/m)\b", re.I)
    for i, line in enumerate(lines):
        context = " ".join(lines[max(0, i - 1): min(len(lines), i + 2)])[:300]
        for m in area_re.finditer(line):
            rows.append({"source": context, "qty": float(m.group("val")), "unit": "m²"})
        for m in lm_re.finditer(line):
            rows.append({"source": context, "qty": float(m.group("val")), "unit": "lm"})
    return rows[:300]


def infer_project_info(all_text: str, filenames: List[str]) -> Dict[str, str]:
    text = re.sub(r"\s+", " ", all_text or " ")
    first_lines = [l.strip() for l in (all_text or "").splitlines() if l.strip()][:100]
    project = ""
    address = ""
    job_no = ""
    for pat in [r"PROJECT\s*(?:NUMBER|NO\.?|#)?\s*[:\-]?\s*([A-Z0-9\-_.]+)", r"(?:JOB|PROJECT)\s*(?:NO\.?|NUMBER)\s*[:\-]?\s*([A-Z0-9\-_.]+)"]:
        m = re.search(pat, text, re.I)
        if m:
            job_no = m.group(1).strip()
            break
    for line in first_lines[:30]:
        up = line.upper()
        if len(line) > 8 and any(k in up for k in ["CONSTRUCTION", "BUILDING", "PROJECT", "DEVELOPMENT", "SWITCHGEAR", "SUBSTATION"]):
            project = line[:120]
            break
    address_pats = [
        r"AT\s+([^\n\r]{8,120}(?:QLD|QUEENSLAND|NSW|VIC|SA|WA|TAS|NT|ACT)[^\n\r]{0,40})",
        r"\b\d{1,5}\s+[A-Z][A-Za-z0-9 .,'\-/]+(?:ROAD|RD|STREET|ST|AVENUE|AVE|DRIVE|DR|COURT|CT|CRESCENT|CRES|PLACE|PL|LANE|LN)[^\n\r]{0,80}",
    ]
    for pat in address_pats:
        m = re.search(pat, all_text or "", re.I)
        if m:
            address = m.group(1).strip() if m.groups() else m.group(0).strip()
            address = re.sub(r"\s+", " ", address)[:160]
            break
    if not project and filenames:
        project = Path(filenames[0]).stem.replace("_", " ").replace("-", " ").title()
    return {"project_name": project, "site_address": address, "job_no": job_no}


def analyse_pdf(path: Path, render_pages: bool = False, dpi: int = 150) -> Dict[str, Any]:
    doc = fitz.open(path)
    page_records = []
    all_text_parts = []
    paint_snips = []
    area_candidates = []
    converted = []
    conv_dir = path.parent.parent / "converted_images" / path.stem
    conv_dir.mkdir(parents=True, exist_ok=True)
    for idx, page in enumerate(doc):
        text = page.get_text("text") or ""
        all_text_parts.append(text)
        ptype = classify_page(text)
        rec = {
            "file": path.name,
            "page": idx + 1,
            "page_type": ptype,
            "drawing_no": extract_drawing_number(text),
            "title": title_from_text(text, ptype),
            "text_chars": len(text),
            "has_text": len(text.strip()) > 20,
        }
        page_records.append(rec)
        paint_snips.extend([{"file": path.name, "page": idx + 1, "text": x} for x in painting_lines(text, 30)])
        for c in extract_area_candidates(text):
            c.update({"file": path.name, "page": idx + 1})
            area_candidates.append(c)
        if render_pages:
            pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
            img_path = conv_dir / f"{path.stem}_page_{idx+1:03d}.png"
            pix.save(str(img_path))
            converted.append(str(img_path))
    doc.close()
    return {
        "file": path.name,
        "path": str(path),
        "page_count": len(page_records),
        "pages": page_records,
        "all_text": "\n".join(all_text_parts),
        "painting_snippets": paint_snips[:500],
        "area_candidates": area_candidates[:500],
        "converted_images": converted,
    }


def detect_substrate_from_text(text: str) -> Tuple[str, str, str]:
    low = (text or "").lower()
    if any(k in low for k in ["ceiling", "soffit", "eave"]):
        return "Ceilings / soffits", "Ceilings", "Internal" if "ceiling" in low and "soffit" not in low else "External"
    if any(k in low for k in ["door", "frame", "jamb", "skirting", "trim"]):
        return "Woodwork / metalwork", "Woodwork", "Internal"
    if any(k in low for k in ["render", "cladding", "external", "elevation", "facade", "façade"]):
        return "External walls", "Exterior", "External"
    if any(k in low for k in ["epoxy", "floor"]):
        return "Floors", "Floor coating", "Internal"
    if any(k in low for k in ["wall", "blockwork", "plaster", "plasterboard"]):
        return "Internal walls", "Walls", "Internal"
    return "Painting item", "General", "Internal"


def build_takeoff_from_analysis(analysis: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    seen = set()
    snippets = analysis.get("painting_snippets", [])
    area_candidates = analysis.get("area_candidates", [])
    # First use area lines that mention paint/finish words.
    for cand in area_candidates:
        source = cand.get("source", "")
        if cand.get("unit") != "m²":
            continue
        if not any(k in source.lower() for k in PAINT_KEYWORDS):
            continue
        substrate, labour_cat, int_ext = detect_substrate_from_text(source)
        key = (substrate, round(float(cand.get("qty", 0)), 2), source[:60])
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "internal_external": int_ext,
            "area_location": f"{Path(cand.get('file','')).stem} p{cand.get('page','')}",
            "substrate": substrate,
            "labour_category": labour_cat,
            "qty_m2": float(cand.get("qty", 0)),
            "lineal_m": 0.0,
            "count": 0,
            "coats": 2,
            "rate_ex_gst": 0.0,
            "labour_hours": 0.0,
            "paint_litres": litres_from_area(float(cand.get("qty", 0)), 2),
            "source_note": source[:250],
            "confidence": "Medium - text quantity found",
        })
    # Then create category rows from paint snippets if no quantities.
    for item in snippets[:150]:
        txt = item.get("text", "")
        substrate, labour_cat, int_ext = detect_substrate_from_text(txt)
        key = (substrate, labour_cat, int_ext, item.get("file"), item.get("page"))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "internal_external": int_ext,
            "area_location": f"{Path(item.get('file','')).stem} p{item.get('page','')}",
            "substrate": substrate,
            "labour_category": labour_cat,
            "qty_m2": 0.0,
            "lineal_m": 0.0,
            "count": 0,
            "coats": 2,
            "rate_ex_gst": 0.0,
            "labour_hours": 0.0,
            "paint_litres": 0.0,
            "source_note": txt[:250],
            "confidence": "Low - item found but quantity needs measure",
        })
    # Always add missing standard painting buckets.
    standard = [
        ("Internal", "Internal walls", "Walls"),
        ("Internal", "Internal ceilings", "Ceilings"),
        ("Internal", "Doors / frames / trim", "Woodwork"),
        ("External", "External walls / render / cladding", "Exterior"),
        ("External", "External soffits / eaves", "Ceilings"),
        ("External", "Downpipes / small gloss items", "Woodwork"),
    ]
    existing_subs = {r["substrate"].lower() for r in rows}
    for int_ext, substrate, labour_cat in standard:
        if substrate.lower() not in existing_subs:
            rows.append({
                "internal_external": int_ext,
                "area_location": "To be measured",
                "substrate": substrate,
                "labour_category": labour_cat,
                "qty_m2": 0.0,
                "lineal_m": 0.0,
                "count": 0,
                "coats": 2,
                "rate_ex_gst": 0.0,
                "labour_hours": 0.0,
                "paint_litres": 0.0,
                "source_note": "Standard painting bucket added for review.",
                "confidence": "Manual quantity required",
            })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["internal_external", "area_location", "substrate", "source_note"]).reset_index(drop=True)
    return df


def run_optional_ai_extract(text: str) -> str:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return "OPENAI_API_KEY is not set. Manual extraction still works."
    model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    prompt = f"""
You are a commercial painting estimator. Extract the useful painting information from the plan/spec text below.
Return concise structured sections:
1. Project details
2. Drawing pages / schedules found
3. Painting substrates
4. Paint systems / colours / sheens
5. Items to include
6. Items to exclude or verify
7. Take-off rows with internal/external, area/location, substrate, qty if stated, unit, notes.
Do not invent quantities. Mark unknown quantities as TO MEASURE.

TEXT:
{text[:50000]}
"""
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You extract painting take-off information from architectural plan/spec text."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            },
            timeout=90,
        )
        if resp.status_code >= 400:
            return f"OpenAI error {resp.status_code}: {resp.text[:1000]}"
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "No AI content returned.")
    except Exception as e:
        return f"OpenAI request failed: {e}"


def df_to_excel_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        for name, df in sheets.items():
            safe_sheet = re.sub(r"[^A-Za-z0-9 _-]", "", name)[:31] or "Sheet"
            df.to_excel(writer, index=False, sheet_name=safe_sheet)
    return out.getvalue()


def file_download_button(path: Path, label: str = "Download"):
    if path.exists():
        st.download_button(label, path.read_bytes(), file_name=path.name, mime="application/octet-stream", key=f"dl_{path}_{time.time()}")


def render_logo():
    logo = ASSETS_DIR / "PB_Logo_Main_PNG.png"
    if logo.exists():
        try:
            b64 = base64.b64encode(logo.read_bytes()).decode()
            st.sidebar.markdown(f"<div class='side-logo'><img src='data:image/png;base64,{b64}' /></div>", unsafe_allow_html=True)
        except Exception:
            pass


def app_css():
    st.markdown(
        """
<style>
:root { --pb-bg:#f4f0ea; --pb-card:#ffffff; --pb-ink:#171717; --pb-muted:#666; --pb-line:#e3ddd4; --pb-accent:#b5a38d; }
.stApp { background: var(--pb-bg); }
.block-container { padding-top: 1.4rem; max-width: 1450px; }
[data-testid="stSidebar"] { background:#111; color:#fff; }
[data-testid="stSidebar"] * { color:#fff; }
[data-testid="stSidebar"] select, [data-testid="stSidebar"] option, [data-testid="stSidebar"] input { color:#111 !important; }
.side-logo { background:#fff; border-radius:16px; padding:12px; margin: 8px 0 18px 0; text-align:center; }
.side-logo img { max-width: 100%; max-height: 95px; object-fit:contain; }
.pb-card { background:rgba(255,255,255,.93); border:1px solid var(--pb-line); border-radius:18px; padding:18px; margin:10px 0; box-shadow:0 8px 24px rgba(0,0,0,.04); }
.pb-card h3 { margin-top:0; }
.metric-row { display:flex; gap:12px; flex-wrap:wrap; }
.metric-box { flex:1 1 150px; background:#fff; border:1px solid var(--pb-line); border-radius:15px; padding:14px; }
.metric-box .big { font-size:28px; font-weight:800; color:#111; }
.metric-box .label { color:#666; font-size:13px; }
.status-good { background:#e8f5ed; color:#0a6b31; border:1px solid #bce3c9; border-radius:999px; padding:3px 10px; font-weight:700; }
.status-warn { background:#fff6df; color:#8a5a00; border:1px solid #f4daa0; border-radius:999px; padding:3px 10px; font-weight:700; }
.status-bad { background:#ffe9e6; color:#9e1f13; border:1px solid #efbeb8; border-radius:999px; padding:3px 10px; font-weight:700; }
.small-muted { color:#666; font-size:13px; }
</style>
        """,
        unsafe_allow_html=True,
    )


def create_or_select_job() -> str:
    jobs = list_jobs()
    st.sidebar.markdown("### Job")
    mode = st.sidebar.radio("Job mode", ["Open existing", "Create new"], label_visibility="collapsed")
    if mode == "Create new" or not jobs:
        with st.sidebar.form("new_job_form"):
            job_no = st.text_input("Job no", value=f"PB-{datetime.now().strftime('%y%m%d')}")
            job_name = st.text_input("Job name", value="New plan import")
            builder = st.text_input("Builder / client", value="")
            address = st.text_input("Site address", value="")
            submitted = st.form_submit_button("Create / open job")
        if submitted:
            jid = job_id_from_name(job_no, job_name)
            meta = load_job(jid) or {}
            meta.update({"job_no": job_no, "job_name": job_name, "builder": builder, "site_address": address, "created_at": meta.get("created_at") or now_stamp()})
            save_job(jid, meta)
            st.session_state["selected_job_id"] = jid
            st.rerun()
    options = {f"{j.get('job_no','')} - {j.get('job_name','')}": j.get("job_id") for j in jobs}
    current = st.session_state.get("selected_job_id")
    labels = list(options.keys())
    default_index = 0
    if current:
        for i, lbl in enumerate(labels):
            if options[lbl] == current:
                default_index = i
    selected_label = st.sidebar.selectbox("Select job", labels, index=default_index if labels else 0)
    jid = options.get(selected_label) if selected_label else None
    if jid:
        st.session_state["selected_job_id"] = jid
        return jid
    return st.session_state.get("selected_job_id", "")


def process_uploads_page(job_id: str):
    job = load_job(job_id)
    st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
    st.subheader("Upload plans / specs")
    st.caption("Upload multiple PDFs/images. This app extracts text, classifies drawing pages, renders pages to images, and builds a draft painting take-off.")
    uploads = st.file_uploader(
        "Upload plan/spec files",
        type=["pdf", "png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
    )
    col1, col2, col3 = st.columns(3)
    with col1:
        render_pdf_pages = st.checkbox("Convert PDF pages to PNG", value=True)
    with col2:
        dpi = st.select_slider("Image quality", options=[100, 150, 200, 250], value=150)
    with col3:
        run_ai = st.checkbox("Run optional AI summary", value=False, help="Only runs if OPENAI_API_KEY is set.")
    if st.button("Import files and gather plan information", type="primary", disabled=not uploads):
        files = job.get("files", [])
        analyses = job.get("analyses", [])
        all_text = []
        progress = st.progress(0)
        for i, up in enumerate(uploads or []):
            saved = save_uploaded_file(job_id, up, "source_files")
            ext = saved.suffix.lower()
            files.append(file_record(saved, ext.lstrip("."), "Plan/spec import"))
            if ext == ".pdf":
                with st.spinner(f"Reading {saved.name}..."):
                    result = analyse_pdf(saved, render_pages=render_pdf_pages, dpi=int(dpi))
                    analyses.append({k: v for k, v in result.items() if k != "all_text"})
                    all_text.append(result.get("all_text", ""))
                    for img_path in result.get("converted_images", []):
                        files.append(file_record(Path(img_path), "png", "Converted drawing image"))
            elif ext in [".png", ".jpg", ".jpeg", ".webp"]:
                # Keep images as mapper-ready drawings.
                img_dst = job_dir(job_id) / "converted_images" / saved.name
                if saved != img_dst:
                    shutil.copyfile(saved, img_dst)
                files.append(file_record(img_dst, ext.lstrip("."), "Drawing image"))
            progress.progress((i + 1) / max(len(uploads), 1))
        combined_text = "\n".join(all_text)
        inferred = infer_project_info(combined_text, [u.name for u in uploads])
        for k, v in inferred.items():
            if v and not job.get(k):
                job[k] = v
        job["files"] = files
        job["analyses"] = analyses
        job["last_combined_text"] = combined_text[:200000]
        # Build take-off rows from all analyses.
        combined_analysis = {"painting_snippets": [], "area_candidates": []}
        for a in analyses:
            combined_analysis["painting_snippets"].extend(a.get("painting_snippets", []))
            combined_analysis["area_candidates"].extend(a.get("area_candidates", []))
        df = build_takeoff_from_analysis(combined_analysis)
        job["takeoff_rows"] = df.to_dict("records")
        if run_ai:
            job["ai_summary"] = run_optional_ai_extract(combined_text)
        save_job(job_id, job)
        st.success("Import complete. Plan information, page list, converted images and draft take-off were saved.")
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def overview_page(job_id: str):
    job = load_job(job_id)
    analyses = job.get("analyses", [])
    files = job.get("files", [])
    pages = []
    snippets = []
    areas = []
    for a in analyses:
        pages.extend(a.get("pages", []))
        snippets.extend(a.get("painting_snippets", []))
        areas.extend(a.get("area_candidates", []))
    st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
    st.title("PB PlanReader")
    st.caption("Clean Render package for gathering painting information from uploaded plans/specs.")
    st.markdown("<div class='metric-row'>", unsafe_allow_html=True)
    for label, value in [
        ("Files", len(files)),
        ("PDF pages read", len(pages)),
        ("Painting lines found", len(snippets)),
        ("Area/lineal quantities found", len(areas)),
        ("Take-off rows", len(job.get("takeoff_rows", []))),
    ]:
        st.markdown(f"<div class='metric-box'><div class='big'>{value}</div><div class='label'>{label}</div></div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
    st.subheader("Job details found")
    c1, c2 = st.columns(2)
    with c1:
        job_no = st.text_input("Job no", value=job.get("job_no", ""))
        job_name = st.text_input("Job name / project", value=job.get("job_name") or job.get("project_name", ""))
    with c2:
        builder = st.text_input("Builder / client", value=job.get("builder", ""))
        address = st.text_input("Site address", value=job.get("site_address", ""))
    if st.button("Save job details"):
        job.update({"job_no": job_no, "job_name": job_name, "builder": builder, "site_address": address})
        save_job(job_id, job)
        st.success("Saved.")
    st.markdown("</div>", unsafe_allow_html=True)

    if pages:
        st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
        st.subheader("Drawing / page register gathered from PDFs")
        df = pd.DataFrame(pages)
        st.dataframe(df, width="stretch", height=320)
        st.markdown("</div>", unsafe_allow_html=True)
    if snippets:
        st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
        st.subheader("Painting / finish lines found")
        st.dataframe(pd.DataFrame(snippets).head(200), width="stretch", height=320)
        st.markdown("</div>", unsafe_allow_html=True)
    if job.get("ai_summary"):
        st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
        st.subheader("Optional AI summary")
        st.write(job.get("ai_summary"))
        st.markdown("</div>", unsafe_allow_html=True)


def takeoff_page(job_id: str):
    job = load_job(job_id)
    rows = job.get("takeoff_rows", [])
    st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
    st.subheader("Painting take-off draft")
    st.caption("This is a working take-off table. Quantities found in the PDF text are brought in; missing quantities stay at 0 for manual measurement/review.")
    if not rows:
        st.warning("No take-off rows yet. Upload plans/specs first.")
        st.markdown("</div>", unsafe_allow_html=True)
        return
    df = pd.DataFrame(rows)
    required_cols = [
        "internal_external", "area_location", "substrate", "labour_category", "qty_m2", "lineal_m", "count",
        "coats", "rate_ex_gst", "labour_hours", "paint_litres", "source_note", "confidence"
    ]
    for col in required_cols:
        if col not in df.columns:
            df[col] = 0.0 if col in ["qty_m2", "lineal_m", "count", "coats", "rate_ex_gst", "labour_hours", "paint_litres"] else ""
    edited = st.data_editor(
        df[required_cols],
        width="stretch",
        num_rows="dynamic",
        height=520,
        column_config={
            "qty_m2": st.column_config.NumberColumn("m²", min_value=0.0, step=1.0),
            "lineal_m": st.column_config.NumberColumn("Lineal m", min_value=0.0, step=1.0),
            "count": st.column_config.NumberColumn("Count", min_value=0.0, step=1.0),
            "coats": st.column_config.NumberColumn("Coats", min_value=0.0, step=1.0),
            "rate_ex_gst": st.column_config.NumberColumn("Rate ex GST", min_value=0.0, step=1.0),
            "labour_hours": st.column_config.NumberColumn("Labour hrs", min_value=0.0, step=1.0),
            "paint_litres": st.column_config.NumberColumn("Paint litres", min_value=0.0, step=1.0),
        },
        key=f"takeoff_editor_{job_id}",
    )
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("Recalculate paint litres", type="secondary"):
            edited["paint_litres"] = edited.apply(lambda r: litres_from_area(r.get("qty_m2", 0), r.get("coats", 2)), axis=1)
            job["takeoff_rows"] = edited.to_dict("records")
            save_job(job_id, job)
            st.success("Paint litres recalculated.")
            st.rerun()
    with col2:
        if st.button("Save take-off", type="primary"):
            job["takeoff_rows"] = edited.to_dict("records")
            save_job(job_id, job)
            st.success("Take-off saved.")
    with col3:
        edited["value_ex_gst"] = pd.to_numeric(edited["qty_m2"], errors="coerce").fillna(0) * pd.to_numeric(edited["rate_ex_gst"], errors="coerce").fillna(0)
        excel = df_to_excel_bytes({"Takeoff": edited, "Files": pd.DataFrame(job.get("files", []))})
        st.download_button("Download Excel", excel, file_name=f"{safe_name(job.get('job_name','takeoff'))}_takeoff.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    edited["value_ex_gst"] = pd.to_numeric(edited["qty_m2"], errors="coerce").fillna(0) * pd.to_numeric(edited["rate_ex_gst"], errors="coerce").fillna(0)
    st.markdown("### Totals")
    a, b, c, d = st.columns(4)
    a.metric("Internal m²", round(edited.loc[edited["internal_external"].astype(str).str.lower().str.contains("internal"), "qty_m2"].sum(), 2))
    b.metric("External m²", round(edited.loc[edited["internal_external"].astype(str).str.lower().str.contains("external"), "qty_m2"].sum(), 2))
    c.metric("Paint litres", round(pd.to_numeric(edited["paint_litres"], errors="coerce").fillna(0).sum(), 2))
    d.metric("Value ex GST", money(edited["value_ex_gst"].sum()))
    st.markdown("</div>", unsafe_allow_html=True)


def images_page(job_id: str):
    job = load_job(job_id)
    st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
    st.subheader("Converted plan/elevation images")
    st.caption("These are rendered directly from the PDF pages and can be used as clean backgrounds for the mapper.")
    imgs = [Path(f.get("path", "")) for f in job.get("files", []) if f.get("category") in ["Converted drawing image", "Drawing image"]]
    imgs = [p for p in imgs if p.exists()]
    if not imgs:
        st.warning("No converted drawing images yet. Upload PDFs and tick 'Convert PDF pages to PNG'.")
    else:
        cols = st.columns(3)
        for i, img_path in enumerate(imgs[:120]):
            with cols[i % 3]:
                st.image(str(img_path), caption=img_path.name, width="stretch")
                file_download_button(img_path, "Download image")
    st.markdown("</div>", unsafe_allow_html=True)


def files_page(job_id: str):
    job = load_job(job_id)
    st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
    st.subheader("File manager")
    files = job.get("files", [])
    if not files:
        st.info("No files uploaded.")
        st.markdown("</div>", unsafe_allow_html=True)
        return
    df = pd.DataFrame(files)
    st.dataframe(df[[c for c in ["name", "category", "file_type", "size_kb", "uploaded_at", "path"] if c in df.columns]], width="stretch", height=340)
    st.markdown("### Remove files attached to the wrong job")
    choices = {f"{i+1}. {f.get('name')} - {f.get('category')}": i for i, f in enumerate(files)}
    selected = st.multiselect("Select files to remove from this job", list(choices.keys()))
    delete_physical = st.checkbox("Also delete physical file from storage", value=False)
    if st.button("Remove selected files", disabled=not selected):
        remove_idx = {choices[x] for x in selected}
        kept = []
        for i, rec in enumerate(files):
            if i in remove_idx:
                p = Path(rec.get("path", ""))
                if delete_physical and p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
            else:
                kept.append(rec)
        job["files"] = kept
        save_job(job_id, job)
        st.success("Selected file records removed.")
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def export_page(job_id: str):
    job = load_job(job_id)
    st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
    st.subheader("Export plan import pack")
    pages, snippets, areas = [], [], []
    for a in job.get("analyses", []):
        pages.extend(a.get("pages", []))
        snippets.extend(a.get("painting_snippets", []))
        areas.extend(a.get("area_candidates", []))
    sheets = {
        "Job Details": pd.DataFrame([job]),
        "Files": pd.DataFrame(job.get("files", [])),
        "Drawing Pages": pd.DataFrame(pages),
        "Painting Lines Found": pd.DataFrame(snippets),
        "Quantities Found": pd.DataFrame(areas),
        "Takeoff Draft": pd.DataFrame(job.get("takeoff_rows", [])),
    }
    excel = df_to_excel_bytes({k: v for k, v in sheets.items() if not v.empty})
    st.download_button("Download complete Excel import pack", excel, file_name=f"{safe_name(job.get('job_name','planreader'))}_planreader_export.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.markdown("</div>", unsafe_allow_html=True)


def settings_page(job_id: str):
    st.markdown("<div class='pb-card'>", unsafe_allow_html=True)
    st.subheader("Storage / reset")
    st.write(f"Data folder: `{DATA_DIR}`")
    st.write(f"Current job folder: `{job_dir(job_id)}`")
    if st.checkbox("Show danger controls"):
        if st.button("Delete this job and all imported files", type="secondary"):
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
            st.session_state.pop("selected_job_id", None)
            st.success("Job deleted.")
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def main():
    st.set_page_config(page_title=APP_NAME, page_icon="🎨", layout="wide")
    app_css()
    render_logo()
    st.sidebar.title("PB PlanReader")
    st.sidebar.caption("Clean plan import + painting take-off package")
    job_id = create_or_select_job()
    if not job_id:
        st.info("Create a job in the sidebar to start.")
        return
    menu = st.sidebar.radio(
        "Menu",
        [
            "Upload Plans",
            "Extracted Info",
            "Take-off Draft",
            "Converted Images",
            "File Manager",
            "Export",
            "Settings",
        ],
    )
    if menu == "Upload Plans":
        process_uploads_page(job_id)
        overview_page(job_id)
    elif menu == "Extracted Info":
        overview_page(job_id)
    elif menu == "Take-off Draft":
        takeoff_page(job_id)
    elif menu == "Converted Images":
        images_page(job_id)
    elif menu == "File Manager":
        files_page(job_id)
    elif menu == "Export":
        export_page(job_id)
    elif menu == "Settings":
        settings_page(job_id)


if __name__ == "__main__":
    main()
