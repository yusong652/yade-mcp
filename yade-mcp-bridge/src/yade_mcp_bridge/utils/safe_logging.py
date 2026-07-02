# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Gap-free logging handlers.

When the cooperative CycleInterrupt times out, the bridge force-kills the stuck
worker thread by async-injecting ``AsyncAbort`` (``PyThreadState_SetAsyncExc``),
which can land on any bytecode edge -- including inside a log call. Stdlib
``Handler.handle`` (Python <= 3.10) acquires the shared handler lock and
registers its release in two separate opcodes; an abort landing between them
orphans the lock, and every later log call, on any thread, blocks forever --
freezing the bridge.

These subclasses acquire via ``with self.lock`` -- one atomic step, so the abort
cannot orphan the lock. Same fix CPython shipped in 3.11 (a no-op there).
"""

import logging


class _GapFreeHandleMixin:
    """``handle()`` guarded by ``with self.lock`` (no async-exc gap).

    First in the MRO so it overrides the concrete handler's ``handle``.
    """

    def handle(self, record):
        rv = self.filter(record)
        if rv:
            with self.lock:
                self.emit(record)
        return rv


class GapFreeFileHandler(_GapFreeHandleMixin, logging.FileHandler):
    pass


class GapFreeStreamHandler(_GapFreeHandleMixin, logging.StreamHandler):
    pass
