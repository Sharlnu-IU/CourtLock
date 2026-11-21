"""Application factory. Run with: uvicorn --factory courtlock.main:create_app"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from courtlock.api.errors import register_error_handlers
from courtlock.api.routers import bookings, courts, holds
from courtlock.config import Settings, get_settings
from courtlock.db.session import make_engine, make_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="CourtLock",
        version="0.2.0",
        summary="Court booking that cannot double-book.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = make_session_factory(engine)
    register_error_handlers(app)
    app.include_router(holds.router)
    app.include_router(bookings.router)
    app.include_router(courts.router)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
