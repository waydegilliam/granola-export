class ExportError(Exception):
    """An actionable export failure, safe to display without credentials."""


class AuthError(ExportError):
    pass


class ApiError(ExportError):
    def __init__(self, endpoint: str, status: int | None, message: str):
        self.endpoint = endpoint
        self.status = status
        super().__init__(f"{endpoint}: {message}")


class ShapeError(ExportError):
    pass
