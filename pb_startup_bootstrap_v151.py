"""PlanReader v1.5.1 cold-start bootstrap.

The main PlanReader module imports the offline reader during module import. The
offline reader in turn imports PyMuPDF4LLM and its layout/ONNX stack, even when
the user only wants the dashboard or an existing take-off. On a cold Render
instance that optional dependency can materially delay the first screen.

This bootstrap installs a tiny proxy module before the main PlanReader stack is
imported. The real ``pb_planreader_offline.py`` module is loaded only when an
offline-reader function is actually called. Public function names stay exactly
the same, so the Offline Plan Reader feature remains available.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path
from typing import Any

VERSION = "1.5.1"
_REAL_NAME = "_pb_planreader_offline_real_v151"
_LOCK = threading.Lock()
_REAL_MODULE: Any = None

_EXPORTED = (
    "analyze_page_offline",
    "generate_takeoff_offline",
    "extract_text_offline",
    "detect_walls",
    "detect_dimensions",
    "detect_scale",
    "detect_rooms",
    "detect_materials",
    "detect_colours",
    "classify_page_offline",
    "generate_report",
)


def _load_real() -> Any:
    global _REAL_MODULE
    if _REAL_MODULE is not None:
        return _REAL_MODULE
    with _LOCK:
        if _REAL_MODULE is not None:
            return _REAL_MODULE
        path = Path(__file__).resolve().with_name("pb_planreader_offline.py")
        spec = importlib.util.spec_from_file_location(_REAL_NAME, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load offline reader from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_REAL_NAME] = module
        spec.loader.exec_module(module)
        _REAL_MODULE = module
        return module


def _proxy(name: str):
    def call(*args, **kwargs):
        return getattr(_load_real(), name)(*args, **kwargs)

    call.__name__ = name
    call.__qualname__ = name
    call.__doc__ = f"Lazy proxy for pb_planreader_offline.{name}."
    return call


def install() -> bool:
    """Install the lazy offline-reader proxy before PlanReader imports it."""
    existing = sys.modules.get("pb_planreader_offline")
    if existing is not None:
        return False
    proxy = types.ModuleType("pb_planreader_offline")
    proxy.__dict__["__all__"] = list(_EXPORTED)
    proxy.__dict__["LAZY_STARTUP_PROXY"] = True
    proxy.__dict__["STARTUP_BOOTSTRAP_VERSION"] = VERSION
    for name in _EXPORTED:
        proxy.__dict__[name] = _proxy(name)
    sys.modules["pb_planreader_offline"] = proxy
    return True


def real_module_loaded() -> bool:
    return _REAL_MODULE is not None
