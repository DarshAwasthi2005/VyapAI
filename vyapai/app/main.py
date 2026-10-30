from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import routes_auth, routes_b2b, routes_b2c, routes_channels, webhooks
from app.config import settings
from app.db.session import init_db
from app.services import auth as authsvc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vyapai")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.app_env == "development":
        init_db()
        logger.info("Dev DB tables ensured via create_all(). Use `alembic upgrade head` in production.")
    else:
        if settings.app_env == "production" and not settings.whatsapp_app_secret:
            raise RuntimeError(
                "WHATSAPP_APP_SECRET is not set. Inbound webhook signature "
                "verification would be silently skipped — refusing to start "
                "an unauthenticated WhatsApp endpoint in production."
            )
        logger.info("Production mode: schema is managed by Alembic migrations, not create_all().")
    yield


app = FastAPI(
    title="VyapAI",
    description="Dual B2B/B2C vernacular commerce agent platform",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(authsvc.AuthError)
async def _auth_error_to_http(request, exc: authsvc.AuthError):
    """Safety net: auth problems are API responses, never server faults.
    Endpoints convert AuthError locally, so this only fires for one that
    missed its wrap — the caller still gets 401/403/503 instead of a 500."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_env == "development" else [],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok", "env": settings.app_env}


app.include_router(routes_auth.router)
app.include_router(routes_b2b.router)
app.include_router(routes_b2c.router)
app.include_router(routes_channels.router)
app.include_router(webhooks.router)

# Serve the dashboard SPA (onboarding → dashboard) at " /" — mounted LAST so
# every API route and /health above matches first (Starlette matches routes
# in registration order).
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
