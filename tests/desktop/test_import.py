"""Smoke test: the desktop control panel module imports cleanly.

Skipped where Tkinter isn't installed (e.g. CI without python3-tk).
"""

from __future__ import annotations

import pytest

pytest.importorskip("tkinter")


def test_desktop_app_imports() -> None:
    import constellation.desktop.app as app

    assert hasattr(app, "ControlApp")
    assert hasattr(app, "run")
