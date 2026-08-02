"""Owner-local API ASGI factory; it deliberately exposes no Central controls."""

from __future__ import annotations

import json
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import ValidationError

from agent_org_network.owner_composition import (
    OwnerComposition,
    OwnerPairingRequest,
    OwnerPairingResultProjection,
)


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

    @app.get("/v1/pairing/status")
    def pairing_status() -> Response:  # pyright: ignore[reportUnusedFunction]
        if composition.schema_ready() and composition.paired():
            return Response(
                content=json.dumps(
                    {
                        "status": "paired",
                        "pairing_reference": composition.config.pairing_reference,
                    },
                    separators=(",", ":"),
                ),
                media_type="application/json",
            )
        return Response(
            content='{"status":"unavailable"}',
            media_type="application/json",
            status_code=503,
        )

    @app.post("/v1/pairing/redeem")
    async def pairing_redeem(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        adapter = composition.pairing_adapter
        if adapter is None:
            raise HTTPException(status_code=503, detail="Owner pairing unavailable")
        try:
            if request.headers.get("content-type") != "application/json":
                raise ValueError("JSON required")
            length = request.headers.get("content-length")
            if length is None or not length.isdigit() or not 1 <= int(length) <= 16 * 1024:
                raise ValueError("bounded request required")
            body = await request.body()
            if len(body) != int(length):
                raise ValueError("bounded request required")
            pairing_request = OwnerPairingRequest.model_validate_json(body)
        except (ValidationError, ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="Invalid pairing request") from None
        try:
            value = adapter.pair(composition.config, pairing_request)
            projection = OwnerPairingResultProjection.model_validate(value)
        except Exception:
            # Never serialize adapter/provider errors or any credential material.
            raise HTTPException(status_code=503, detail="Owner pairing unavailable") from None
        return Response(
            content=projection.model_dump_json(),
            media_type="application/json",
            headers={"cache-control": "no-store", "pragma": "no-cache"},
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
