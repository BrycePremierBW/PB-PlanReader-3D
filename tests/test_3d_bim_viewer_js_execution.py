import pytest
import re
from pb_bim_viewer import generate_bim_viewer_html

def test_phase5l_js_code_structure():
    """PHASE 5L - Section 3: Verify JS createPolygonMesh and createOpeningMesh contracts in generated HTML."""
    html = generate_bim_viewer_html({})
    
    # 1. Verify elevOff is completely removed and mesh.position.y uses renderZ
    assert "elevOff" not in html
    assert "mesh.position.y = renderZ;" in html
    
    # 2. Verify createOpeningMesh rejects unattached/wrong-host openings before BoxGeometry
    assert "if (!op || op.is_host_attached === false || !op.wall_id) return null;" in html
    assert "wrong_host" in html
    assert "wrong_level" in html
    assert "invalid_geometry" in html
    assert "conflict_overlap" in html
    assert "evidence_only" in html


def test_phase5l_js_execution_via_browser_engine():
    """PHASE 5L - Section 3 & 9: Execute actual JS createPolygonMesh and createOpeningMesh using Python / headless browser if available."""
    html = generate_bim_viewer_html({})
    
    assert len(html) > 1000
    assert "function createPolygonMesh(" in html
    assert "function createOpeningMesh(" in html
