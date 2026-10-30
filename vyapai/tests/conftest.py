"""Shared test helpers."""
import contextlib
import os

# Must run BEFORE app.config is imported below: Settings() resolves its env
# defaults at import time, and the default DATABASE_URL points at Postgres,
# which no test machine has. Tests without an explicit DATABASE_URL get a
# throwaway SQLite instead. (Explicitly-set values are never overridden.)
os.environ.setdefault("DATABASE_URL", "sqlite:///./vyapai-test.db")
os.environ.setdefault("APP_ENV", "development")

from app.config import settings


@contextlib.contextmanager
def patch_settings(**kw):
    """Temporarily override one or more app.settings fields.

    Settings is a frozen dataclass, so plain setattr is refused; tests reach
    through object.__setattr__ and always restore afterwards.
    """
    originals = {k: getattr(settings, k) for k in kw}
    for k, v in kw.items():
        object.__setattr__(settings, k, v)
    try:
        yield
    finally:
        for k, v in originals.items():
            object.__setattr__(settings, k, v)
