"""KIS client exception hierarchy."""


class KisError(Exception):
    """Base for all KIS client errors."""


class KisConfigError(KisError):
    """Misconfiguration: missing keys, bad mode, etc."""


class KisAuthError(KisError):
    """Token expired / invalid. Caller should trigger re-issue."""


class KisBusinessError(KisError):
    """traidair returned ok=false (KIS business-level rejection)."""

    def __init__(self, message: str, *, route: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.route = route
        self.payload = payload or {}


class KisTransportError(KisError):
    """HTTP / network / timeout failure after retries exhausted."""


class KisModeMismatchError(KisError):
    """Requested call requires a different mode than the client was configured for."""
