"""
Unit tests for system detector.
"""

from __future__ import annotations

from thwip.detector import SystemDetector


def test_system_detector_scan():
    detector = SystemDetector()
    tools = detector.scan_all()
    assert isinstance(tools, list)
    # Ensure tool objects have valid fields
    for t in tools:
        assert t.name
        assert t.company
        assert t.category
        assert isinstance(t.capabilities, list)
