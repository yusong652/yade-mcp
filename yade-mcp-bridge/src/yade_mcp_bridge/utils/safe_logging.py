# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Gap-free logging handlers.

The L2 timeout path async-raises ``BridgeTimeout`` into the running
thread via ``PyThreadState_SetAsyncExc``, which fires at an arbitrary
bytecode edge. CPython's stdlib ``Handler.handle`` (Python <= 3.10)
acquires the handler lock and registers its release in two separate
opcodes; an async exception landing between them orphans the lock, and
the next thread to log then blocks forever, freezing the bridge.

These subclasses use the ``with self.lock`` form -- a single opcode that
acquires and registers the release atomically, the same fix CPython
shipped in 3.11. On 3.11+ it just mirrors the stdlib (harmless no-op).
"""

import logging


class _GapFreeHandleMixin:
    """``Handler.handle`` guarded by ``with self.lock`` (no async-exc gap).

    Behaviourally identical to the stdlib; mixed in ahead of the concrete
    handler so this ``handle`` wins via MRO.
    """

    def handle(self, record):
        rv = self.filter(record)
        if rv:
            with self.lock:
                self.emit(record)
        return rv


class GapFreeFileHandler(_GapFreeHandleMixin, logging.FileHandler):
    """``logging.FileHandler`` with the gap-free ``handle``."""


class GapFreeStreamHandler(_GapFreeHandleMixin, logging.StreamHandler):
    """``logging.StreamHandler`` with the gap-free ``handle``."""
