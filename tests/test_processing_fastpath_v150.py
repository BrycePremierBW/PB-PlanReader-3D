from pathlib import Path
from types import SimpleNamespace

import pb_processing_fastpath_v150 as fast


class FakeProgressTarget:
    def __init__(self):
        self.calls = []
        self.emptied = False

    def progress(self, value, text=None):
        self.calls.append((value, text))
        return self

    def empty(self):
        self.emptied = True


class FakeStreamlit:
    def __init__(self):
        self.targets = []

    def progress(self, value, text=None, *args, **kwargs):
        target = FakeProgressTarget()
        target.calls.append((value, text))
        self.targets.append(target)
        return target


def test_eta_progress_adds_remaining_time_text():
    st = FakeStreamlit()
    app = SimpleNamespace(st=st)
    fast._install_eta_progress(app)
    bar = app.st.progress(0)
    # Backdate the proxy so an ETA can be inferred immediately in the test.
    bar._start -= 10
    bar.progress(0.5)
    assert "remaining" in st.targets[0].calls[-1][1]
    bar.progress(1.0)
    assert "complete" in st.targets[0].calls[-1][1]


def test_complete_document_register_skips_reindexing():
    calls = {"index": 0}

    def lquery(sql, params=()):
        if "FROM documents" in sql:
            return [{"id": 7, "page_count": 2}]
        if "FROM pages" in sql:
            return [
                {"page_no": 1, "page_label": "A101", "page_type": "Floor Plan", "extracted_text": "plan"},
                {"page_no": 2, "page_label": "A201", "page_type": "Elevation", "extracted_text": "elevation"},
            ]
        return []

    def index_document_pages(document_id):
        calls["index"] += 1
        return 2, "Indexed"

    app = SimpleNamespace(lquery=lquery, index_document_pages=index_document_pages)
    fast._install_index_fastpath(app)
    assert app.index_document_pages(7) == (2, "Already indexed")
    assert calls["index"] == 0


def test_incomplete_register_falls_back_to_real_indexer():
    calls = {"index": 0}

    def lquery(sql, params=()):
        if "FROM documents" in sql:
            return [{"id": 7, "page_count": 2}]
        if "FROM pages" in sql:
            return [{"page_no": 1, "page_label": "A101", "page_type": "Floor Plan", "extracted_text": "plan"}]
        return []

    def index_document_pages(document_id):
        calls["index"] += 1
        return 2, "Indexed"

    app = SimpleNamespace(lquery=lquery, index_document_pages=index_document_pages)
    fast._install_index_fastpath(app)
    assert app.index_document_pages(7) == (2, "Indexed")
    assert calls["index"] == 1


def test_ai_page_bytes_cache_reuses_same_file(tmp_path):
    calls = {"encode": 0}
    page = tmp_path / "page.png"
    page.write_bytes(b"fake-png")

    def ai_page_bytes(path: Path, max_long_edge=1600):
        calls["encode"] += 1
        return f"{Path(path).name}:{max_long_edge}".encode()

    app = SimpleNamespace(_ai_page_bytes=ai_page_bytes, _AI_IMAGE_LONG_EDGE_PX=1600)
    fast._install_ai_image_cache(app)
    first = app._ai_page_bytes(page)
    second = app._ai_page_bytes(page)
    assert first == second
    assert calls["encode"] == 1


def test_production_entry_enables_v150_fast_path():
    text = Path("pb_planreader_v133_app.py").read_text(encoding="utf-8")
    assert "apply_processing_fastpath_v150" in text
    assert 'APP_VERSION = "1.5.0"' in text
