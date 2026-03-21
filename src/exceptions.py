import traceback

from globals import logger


class ApiCallError(Exception):
    """Raised when an AI provider API call fails. Carries the HTTP status_code."""

    def __init__(self, message: str, status_code: int | None = None, service: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.service = service
        logger.error(f"[{service}] API call failed (HTTP {status_code}): {message}")
        traceback.print_exc()
