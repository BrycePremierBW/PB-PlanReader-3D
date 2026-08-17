"""Standalone PDF page renderer used by PlanReader.

Run as a long-lived child process so a crash or memory spike inside the MuPDF
rasteriser (PyMuPDF) only ever kills this worker, never the main Streamlit
app. The main app starts one worker per PDF and feeds it render jobs over
stdin, so a whole batch costs a single Python/Fitz startup.

Usage:
    python pb_render_worker.py <pdf_path>

Commands (one per line on stdin):
    RENDER <page_no_1_based> <zoom> <out_path>
    QUIT

Responses (one line on stdout, flushed):
    OK <width> <height>
    ERR <message>
"""
import shlex
import sys


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

    try:
        document = fitz.open(pdf_path)
    except Exception as exc:
        print(f"ERR could not open pdf {pdf_path}: {exc}", flush=True)
        return 1

    try:
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
            try:
                page = document[page_no - 1]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                pixmap.save(out_path)
                print(f"OK {pixmap.width} {pixmap.height}", flush=True)
            except Exception as exc:
                message = str(exc).replace("\n", " ").strip()
                print(f"ERR {message[:300]}", flush=True)
    finally:
        document.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
