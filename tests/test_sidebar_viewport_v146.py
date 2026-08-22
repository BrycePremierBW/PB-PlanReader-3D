from pb_sidebar_viewport_guard_v146 import VERSION, viewport_css


def test_sidebar_viewport_guard_css():
    css = viewport_css()
    assert VERSION == "1.4.6"
    assert 'data-testid="stSidebar"' in css
    assert 'max-width: calc(100vw - 1rem)' in css
    assert 'data-baseweb="popover"' in css
    assert 'data-baseweb="menu"' in css
    assert '@media (max-width: 768px)' in css
    assert 'overflow-x: hidden' in css
