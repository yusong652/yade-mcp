"""Tests for the bridge PyRunner: its command must survive O.save()/O.load()
in a yade session that never imported the bridge, and the bridge must
recognise (and heal) the engine written by older bridge versions."""

import logging
import sys
import types

import pytest
from yade_mcp_bridge.runtime import pyrunner
from yade_mcp_bridge.runtime.pyrunner import (
    _PYRUNNER_COMMAND,
    installPyrunner,
    isBridgePyrunnerCommand,
)

# Command string written by bridge <= 0.10.0 into saved scenes.
LEGACY_COMMAND = "_mcpPyrunnerTick()  # mcp bridge: DO NOT MODIFY"


class FakeEngine:
    def __init__(self, command=None, iterPeriod=1, dead=False):
        if command is not None:
            self.command = command
        self.iterPeriod = iterPeriod
        self.dead = dead


class _Wrapper:
    """Fresh Python wrapper around a C++-owned engine, like the one boost.python
    hands out on every O.engines access for engines created by O.load()."""

    def __init__(self, target):
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name):
        return getattr(self._target, name)

    def __setattr__(self, name, value):
        setattr(self._target, name, value)


class FakeOmega:
    def __init__(self):
        self._engines = []
        self.runCalls = 0

    @property
    def engines(self):
        return [_Wrapper(e) for e in self._engines]

    @engines.setter
    def engines(self, value):
        self._engines = [getattr(e, "_target", e) for e in value]

    def run(self, *args, **kwargs):
        self.runCalls += 1

    def pause(self):
        pass


def commands(omega):
    return [getattr(e, "command", type(e._target).__name__) for e in omega.engines]


@pytest.fixture
def fake_yade(monkeypatch):
    module = types.ModuleType("yade")
    module.O = FakeOmega()
    module.PyRunner = FakeEngine
    monkeypatch.setitem(sys.modules, "yade", module)
    # installPyrunner binds the tick into __main__; keep the test's __main__ clean.
    monkeypatch.setitem(sys.modules, "__main__", types.ModuleType("__main__"))
    yield module.O


class TestCommandString:
    def test_noop_without_the_bridge(self):
        """Loaded in a plain yade session (no tick in __main__), the command
        must run silently: that is what O.load() + O.run() does to a scene
        saved under the bridge."""
        namespace = {}
        exec(_PYRUNNER_COMMAND, namespace)  # PyRunner exec()s in __main__.__dict__

    def test_calls_the_tick_when_bound(self):
        calls = []
        namespace = {pyrunner._TICK_NAME: lambda: calls.append(1)}
        exec(_PYRUNNER_COMMAND, namespace)
        assert calls == [1]

    def test_single_line(self):
        # PyRunner.command is one statement; the trailing comment must not
        # break it into a second line.
        assert "\n" not in _PYRUNNER_COMMAND

    def test_identifies_current_and_legacy_forms(self):
        assert isBridgePyrunnerCommand(_PYRUNNER_COMMAND)
        assert isBridgePyrunnerCommand(LEGACY_COMMAND)
        assert not isBridgePyrunnerCommand("myRecorder()")
        assert not isBridgePyrunnerCommand(None)


class TestInstall:
    def test_injects_at_front(self, fake_yade):
        fake_yade.engines = [FakeEngine("userTick()")]
        assert installPyrunner(logging.getLogger("test"))
        assert commands(fake_yade) == [_PYRUNNER_COMMAND, "userTick()"]

    def test_heals_legacy_engine_without_duplicating(self, fake_yade):
        """A scene saved by an older bridge carries the legacy command. On
        install it is recognised as ours and rewritten, not added twice."""
        legacy = FakeEngine(LEGACY_COMMAND, iterPeriod=5, dead=True)
        fake_yade.engines = [FakeEngine("userTick()"), legacy]
        installPyrunner(logging.getLogger("test"))
        assert commands(fake_yade) == [_PYRUNNER_COMMAND, "userTick()"]
        assert fake_yade._engines[0] is legacy
        assert legacy.iterPeriod == 1 and legacy.dead is False

    def test_loaded_scene_is_not_duplicated_on_run(self, fake_yade):
        """Regression: with wrapper-per-access engines (O.load), identity
        comparison never matched and every O.run() added another PyRunner."""
        installPyrunner(logging.getLogger("test"))
        fake_yade.engines = [FakeEngine(_PYRUNNER_COMMAND), FakeEngine()]  # as O.load leaves it
        for _ in range(3):
            fake_yade.run(10, True)
        assert commands(fake_yade) == [_PYRUNNER_COMMAND, "FakeEngine"]

    def test_dedupes_extra_bridge_pyrunners(self, fake_yade):
        installPyrunner(logging.getLogger("test"))
        fake_yade.engines = [FakeEngine(), FakeEngine(_PYRUNNER_COMMAND), FakeEngine(LEGACY_COMMAND)]
        fake_yade.run(10, True)
        assert commands(fake_yade) == [_PYRUNNER_COMMAND, "FakeEngine"]

    def test_run_hook_reinjects_after_reassignment(self, fake_yade):
        installPyrunner(logging.getLogger("test"))
        fake_yade.engines = [FakeEngine("userTick()")]  # as a user script would
        fake_yade.run(100, True)
        assert fake_yade.runCalls == 1
        assert commands(fake_yade) == [_PYRUNNER_COMMAND, "userTick()"]

    def test_binds_tick_into_main(self, fake_yade):
        installPyrunner(logging.getLogger("test"))
        assert callable(getattr(sys.modules["__main__"], pyrunner._TICK_NAME))
