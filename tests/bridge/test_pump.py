"""Tests for the Qt pump's binding choice: it must drive the binding that
owns the running application, and import nothing on its own."""

import sys
import types
from unittest.mock import MagicMock

import pytest
from yade_mcp_bridge.runtime import pump


def _fake_binding(monkeypatch, name, has_app):
    """Install a fake <name>.QtCore in sys.modules."""
    QtCore = types.ModuleType(name + ".QtCore")
    QtCore.QCoreApplication = MagicMock()
    QtCore.QCoreApplication.instance.return_value = object() if has_app else None
    QtCore.QTimer = MagicMock()
    monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, name + ".QtCore", QtCore)
    return QtCore


@pytest.fixture(autouse=True)
def _no_real_qt(monkeypatch):
    for name in ("PyQt5", "PyQt5.QtCore", "PyQt6", "PyQt6.QtCore"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(pump, "_qtPumpTimer", None)


def test_no_application_means_no_qt_pump(monkeypatch):
    assert pump._liveQtCore() is None
    _fake_binding(monkeypatch, "PyQt5", has_app=False)
    assert pump._liveQtCore() is None
    assert pump.startQtPump(MagicMock(), MagicMock()) is False


def test_picks_the_binding_that_owns_the_app(monkeypatch):
    """PyQt5 importable but idle, PyQt6 owns the application (a Qt6 YADE on a
    machine that also has PyQt5): the pump must use PyQt6."""
    _fake_binding(monkeypatch, "PyQt5", has_app=False)
    qt6 = _fake_binding(monkeypatch, "PyQt6", has_app=True)
    assert pump._liveQtCore() is qt6


def test_timer_runs_on_the_owning_binding(monkeypatch):
    _fake_binding(monkeypatch, "PyQt5", has_app=False)
    qt6 = _fake_binding(monkeypatch, "PyQt6", has_app=True)
    assert pump.startQtPump(MagicMock(), MagicMock()) is True
    timer = qt6.QTimer.return_value
    timer.setInterval.assert_called_once_with(pump._TICK_INTERVAL_MS)
    timer.start.assert_called_once()
    assert pump._qtPumpTimer is timer
