from contextvars import ContextVar

_active_user_features: ContextVar[dict] = ContextVar("active_user_features", default={})
_active_user_key: ContextVar[str | None] = ContextVar("active_user_key", default=None)
