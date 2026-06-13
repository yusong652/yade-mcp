# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Server Context for Handler Dependency Injection."""


class ServerContext:
    """Context object providing access to server dependencies for handlers."""

    def __init__(self, task_manager, script_runner, main_executor, runtime_mode="unknown", console_history=None):
        self.task_manager = task_manager
        self.script_runner = script_runner
        self.main_executor = main_executor
        self.runtime_mode = runtime_mode
        self.console_history = console_history
