"""Automation hub core primitives."""

__all__ = [
    "WarpClient",
    "RotateResult",
    "public_ip",
    "build_job_env",
    "load_hub_env",
    "hub_python",
    "BatchProgress",
    "format_step",
    "format_ok",
    "format_fail",
    "make_log_sink",
    "WarpPolicy",
    "default_every_n",
    "stop_all",
    "is_running",
    "active_summary",
]


def __getattr__(name: str):
    if name in ("WarpClient", "RotateResult", "public_ip"):
        from . import warp as _m

        return getattr(_m, name)
    if name in ("build_job_env", "load_hub_env", "hub_python"):
        from . import env as _m

        return getattr(_m, name)
    if name in (
        "BatchProgress",
        "format_step",
        "format_ok",
        "format_fail",
        "make_log_sink",
    ):
        from . import progress as _m

        return getattr(_m, name)
    if name in ("WarpPolicy", "default_every_n", "plan_chunks"):
        from . import warp_policy as _m

        return getattr(_m, name)
    if name in ("stop_all", "is_running", "active_summary", "register", "request_stop"):
        from . import jobctl as _m

        return getattr(_m, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
