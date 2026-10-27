"""Stateless HTTP app for bounded synthetic energy simulations."""

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from .interactive import RunInput, presets

WEB = Path(__file__).parent / "web"
MAX_BODY = 16_384
MAX_CONCURRENT = 2
MAX_PER_MINUTE = 60
RUN_TIMEOUT = 15
logger = logging.getLogger(__name__)


async def run_worker(payload):
    with tempfile.TemporaryDirectory(prefix="energy-request-") as workdir:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "src.energy_exchange.interactive",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "ENERGY_RUN_ROOT": workdir,
                "NANDATOWN_HOME": str(Path(workdir) / "town"),
            },
        )
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(json.dumps(payload).encode()), RUN_TIMEOUT
            )
            if process.returncode:
                logger.error("Simulation worker failed (exit %s)", process.returncode)
                raise RuntimeError("Simulation failed")
            return json.loads(stdout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


def create_app():
    app = FastAPI(
        title="Synthetic energy P2P lab",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.active = 0
    app.state.recent = deque()
    app.state.runner = run_worker

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/")
    async def index():
        return FileResponse(WEB / "interactive.html")

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in ("interactive.js", "interactive.css"):
            raise HTTPException(404, "Not found")
        return FileResponse(WEB / name)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "mode": "synthetic", "llm": False}

    @app.get("/api/presets")
    async def preset_inputs():
        return {"presets": presets()}

    @app.post("/api/runs")
    async def run(request: Request):
        if (
            request.headers.get("content-type", "").split(";")[0].strip()
            != "application/json"
        ):
            raise HTTPException(415, "Send application/json")
        # Only this same-origin browser app may submit browser requests. CLI
        # clients without Origin can replay downloaded synthetic inputs.
        origin = request.headers.get("origin")
        expected_origin = os.environ.get(
            "ENERGY_PUBLIC_ORIGIN", str(request.base_url)
        ).rstrip("/")
        if origin and origin.rstrip("/") != expected_origin:
            raise HTTPException(403, "Use the simulation page on this host")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "Simulation input is too large")
        try:
            raw = json.loads(body)
            inputs = RunInput.model_validate(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            errors = (
                [
                    {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                    for e in exc.errors(include_url=False)
                ]
                if isinstance(exc, ValidationError)
                else [{"field": "request", "message": "Enter valid JSON input"}]
            )
            return JSONResponse(status_code=422, content={"detail": errors})
        now = time.monotonic()
        while app.state.recent and now - app.state.recent[0] >= 60:
            app.state.recent.popleft()
        if (
            app.state.active >= MAX_CONCURRENT
            or len(app.state.recent) >= MAX_PER_MINUTE
        ):
            raise HTTPException(
                429,
                "The lab is busy. Please retry shortly.",
                headers={"Retry-After": "5"},
            )
        app.state.active += 1
        app.state.recent.append(now)
        try:
            return await app.state.runner(inputs.model_dump())
        except TimeoutError:
            raise HTTPException(504, "This run timed out. Please retry.") from None
        except (RuntimeError, ValueError):
            raise HTTPException(
                500, "This run could not be verified. Please retry."
            ) from None
        finally:
            app.state.active -= 1

    return app


app = create_app()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
