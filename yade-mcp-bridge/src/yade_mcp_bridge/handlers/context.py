# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Server Context for Handler Dependency Injection."""


class ServerContext:
    """Context object providing access to server dependencies for handlers."""

    def __init__(self, taskManager, taskRunner, codeRunner, codeExecutor, runtimeMode, consoleHistory=None):
        self.taskManager = taskManager
        self.taskRunner = taskRunner
        self.codeRunner = codeRunner
        self.codeExecutor = codeExecutor
        self.runtimeMode = runtimeMode
        self.consoleHistory = consoleHistory
