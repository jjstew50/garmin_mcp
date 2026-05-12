from contextvars import ContextVar

_active_user_features: ContextVar[dict] = ContextVar("active_user_features", default={})
