"""Errors that end a fleet command. Raised, never swallowed: the CLI turns them into an exit code."""


class FleetError(Exception):
    """A refusal or failure. The CLI prints `fleet: <message>` to stderr and exits with `code`."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class FleetExit(Exception):
    """A non-error early exit with its own code and an optional stderr message printed as is."""

    def __init__(self, code, message=""):
        super().__init__(message)
        self.code = code
        self.message = message
