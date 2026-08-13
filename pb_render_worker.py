"""Standalone PDF page renderer used by PlanReader.

Run in a separate OS process so a crash or memory spike inside the MuPDF
rasteriser (PyMuPDF) only ever kills this worker, never the main Streamlit
app. The main app calls this via ``subprocess`` for each page and treats any
non-zero exit as a per-page render failure that can be reported in the UI
without restarting the session.

Usage:
    python pb_render_worker.py <pdf_path> <page_no_1_based> <zoom> <out_path>

Prints "<width> <height>" on success.
"""
import sys


def _main() -> int:
    if len(sys.argv) != 5:
        print(
            "usage: pb_render_worker.py <pdf_path> <page_no> <zoom> <out_path>",
            file=sys.stderr,
        )
        return 2

    pdf_path, page_no, zoom_text, out_path = sys.argv[1:]
    try:
        import fitz
    except Exception as exc:
        print(f"PyMuPDF is not available in this worker: {exc}", file=sys.stderr)
        return 1

    try:
        zoom = float(zoom_text)
    except ValueError:
        print(f"invalid zoom: {zoom_text!r}", file=sys.stderr)
        return 2

    try:
        document = fitz.open(pdf_path)
        try:
            page = document[int(page_no) - 1]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pixmap.save(out_path)
            print(f"{pixmap.width} {pixmap.height}")
        finally:
            document.close()
    except Exception as exc:
        print(f"render failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
