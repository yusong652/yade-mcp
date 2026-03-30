"""Server Context for Handler Dependency Injection."""


class ServerContext:
    """Context object providing access to server dependencies for handlers."""

    def __init__(self, task_manager, script_runner, main_executor, runtime_mode="unknown"):
        self.task_manager = task_manager
        self.script_runner = script_runner
        self.main_executor = main_executor
        self.runtime_mode = runtime_mode
