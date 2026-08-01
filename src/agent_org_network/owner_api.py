"""Owner-local API ASGI factory; it deliberately exposes no Central controls."""

from __future__ import annotations

from typing import Protocol

from fastapi import FastAPI, Response

from agent_org_network.owner_composition import OwnerComposition


class OwnerApiRunner(Protocol):
    def __call__(self, app: FastAPI, *, host: str, port: int) -> None: ...


def create_owner_api_app(composition: OwnerComposition) -> FastAPI:
    if type(composition) is not OwnerComposition:
        raise TypeError("OwnerComposition required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> Response:  # pyright: ignore[reportUnusedFunction]
        if composition.schema_ready() and composition.paired():
            return Response(content='{"status":"ready"}', media_type="application/json")
        return Response(
            content='{"status":"unavailable"}', media_type="application/json", status_code=503
        )

    return app


def run_owner_api(composition: OwnerComposition, runner: OwnerApiRunner) -> None:
    if type(composition) is not OwnerComposition:
        raise TypeError("OwnerComposition required")
    if composition.config.bind_host != "127.0.0.1":
        raise ValueError("Owner API requires literal loopback")
    runner(
        create_owner_api_app(composition),
        host=composition.config.bind_host,
        port=composition.config.port,
    )


def uvicorn_runner(app: FastAPI, *, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)
