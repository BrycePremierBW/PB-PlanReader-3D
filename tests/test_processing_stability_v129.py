from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz

import pb_processing_stability_v129 as stability


class _FakeTools:
    def __init__(self):
        self.calls = 0

    def store_shrink(self, percent):
        self.calls += 1
        return percent


class _FakeGC:
    def __init__(self):
        self.calls = 0

    def collect(self):
        self.calls += 1
        return 0


class ProcessingStabilityTests(unittest.TestCase):
    def test_apply_caps_render_size_and_releases_parent_cache(self):
        tools = _FakeTools()
        fake_gc = _FakeGC()
        app = SimpleNamespace(
            fitz=SimpleNamespace(TOOLS=tools),
            gc=fake_gc,
            _PDF_RENDER_LONG_EDGE_PX=2400,
            process_document=lambda *a, **k: (1, "processed"),
            index_document_pages=lambda *a, **k: (1, "indexed"),
        )
        with patch.dict(os.environ, {"PLANREADER_RENDER_LONG_EDGE_PX": "1800"}):
            stability.apply(app)
        self.assertEqual(app._PDF_RENDER_LONG_EDGE_PX, 1800)
        self.assertEqual(app.process_document(1), (1, "processed"))
        self.assertEqual(app.index_document_pages(1), (1, "indexed"))
        self.assertGreaterEqual(tools.calls, 4)
        self.assertGreaterEqual(fake_gc.calls, 4)

    def test_render_worker_can_render_multiple_pages_sequentially(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf_path = root / "sample.pdf"
            doc = fitz.open()
            for i in range(3):
                page = doc.new_page(width=595, height=842)
                page.insert_text((72, 72), f"Page {i + 1}")
            doc.save(pdf_path)
            doc.close()

            worker = Path(__file__).resolve().parents[1] / "pb_render_worker.py"
            proc = subprocess.Popen(
                [sys.executable, "-u", str(worker), str(pdf_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                self.assertIsNotNone(proc.stdin)
                self.assertIsNotNone(proc.stdout)
                for page_no in range(1, 4):
                    out = root / f"page_{page_no}.png"
                    proc.stdin.write(f'RENDER {page_no} 1.0 "{out}"\n')
                    proc.stdin.flush()
                    line = proc.stdout.readline().strip()
                    self.assertTrue(line.startswith("OK "), line)
                    self.assertTrue(out.exists())
                    self.assertGreater(out.stat().st_size, 0)
                proc.stdin.write("QUIT\n")
                proc.stdin.flush()
                self.assertEqual(proc.wait(timeout=20), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
