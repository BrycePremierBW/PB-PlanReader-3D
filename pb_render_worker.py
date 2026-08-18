"""Standalone PDF page renderer used by PlanReader.

Run as a child process so a crash or memory spike inside the MuPDF rasteriser
(PyMuPDF) is isolated from the main Streamlit app. The worker stays alive to
avoid repeated Python startup, but deliberately opens/closes the PDF for every
page and empties MuPDF's cache after each render. That trades a little speed for
much lower peak/retained memory on Render.

Usage:
    python pb_render_worker.py <pdf_path>

Commands (one per line on stdin):
    RENDER <page_no_1_based> <zoom> <out_path>
    QUIT

Responses (one line on stdout, flushed):
    OK <width> <height>
    ERR <message>
"""
import gc
import shlex
import sys


def _shrink_store(fitz) -> None:
    try:
        fitz.TOOLS.store_shrink(100)
    except Exception:
        pass
    gc.collect()


def _main() -> int:
    if len(sys.argv) != 2:
        print("usage: pb_render_worker.py <pdf_path>", file=sys.stderr, flush=True)
        return 2

    pdf_path = sys.argv[1]
    try:
        import fitz
    except Exception as exc:
        print(f"ERR PyMuPDF is not available in this worker: {exc}", flush=True)
        return 1

    _shrink_store(fitz)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            print(f"ERR invalid command line: {exc}", flush=True)
            continue
        if not parts:
            continue
        command = parts[0]
        if command == "QUIT":
            break
        if command != "RENDER" or len(parts) != 4:
            print(f"ERR unexpected command: {line[:200]}", flush=True)
            continue

        try:
            page_no = int(parts[1])
            zoom = float(parts[2])
        except ValueError as exc:
            print(f"ERR invalid render arguments: {exc}", flush=True)
            continue
        out_path = parts[3]

        document = None
        page = None
        pixmap = None
        try:
            # Keep MuPDF's cross-page cache from growing inside the same worker.
            _shrink_store(fitz)
            document = fitz.open(pdf_path)
            page = document[page_no - 1]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            width, height = int(pixmap.width), int(pixmap.height)
            pixmap.save(out_path)
            print(f"OK {width} {height}", flush=True)
        except Exception as exc:
            message = str(exc).replace("\n", " ").strip()
            print(f"ERR {message[:300]}", flush=True)
        finally:
            # Explicitly drop the large pixel buffer and all page/document
            # resources before accepting another page from the same plan set.
            pixmap = None
            page = None
            if document is not None:
                try:
                    document.close()
                except Exception:
                    pass
            document = None
            _shrink_store(fitz)

    _shrink_store(fitz)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
