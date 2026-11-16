"""FastAPI HTTP adapter for the CLI control-plane API."""
from collections import defaultdict, deque
import asyncio
import concurrent.futures
from contextlib import asynccontextmanager
import json
import re
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import Database
from .service import APIError, Service
from .settings import Settings, get_settings
from .storage import OSS

MAX_BODY_BYTES = 65536
JOB_ID = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class ResponsePolicyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("query_string"):
            response = JSONResponse(status_code=404, content={"code": "not_found"})
            await response(scope, receive, send)
            return

        async def send_no_store(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_no_store)


def create_app(
    settings: Settings | None = None,
    service: Service | None = None,
    offload_service: bool = True,
) -> FastAPI:
    """Create an app, allowing tests to inject a service without loading secrets."""
    if service is None:
        settings = settings or get_settings()
        db = Database(settings.database_url.get_secret_value(), allow_sqlite=settings.trace_dock_dev)
        db.initialize()
        service = Service(db, OSS(settings), settings.token_pepper.get_secret_value())

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            executor.shutdown(wait=True)
            if hasattr(service, "close"):
                service.close()

    app = FastAPI(
        title="Trace Dock API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(ResponsePolicyMiddleware)
    attempts = defaultdict(deque)
    attempts_lock = threading.Lock()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="trace-dock-api")

    @app.exception_handler(APIError)
    async def handle_api_error(_request: Request, error: APIError):
        headers = {"Retry-After": "60"} if error.status == 429 else None
        return JSONResponse(status_code=error.status, content={"code": error.code}, headers=headers)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, _error: Exception):
        return JSONResponse(status_code=503, content={"code": "service_unavailable"})

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: Request, error: StarletteHTTPException):
        code = "not_found" if error.status_code == 404 else "method_not_allowed" if error.status_code == 405 else "invalid_request"
        return JSONResponse(status_code=error.status_code, content={"code": code})

    def rate_limit(client_host: str):
        with attempts_lock:
            now = time.monotonic()
            bucket = attempts[client_host]
            while bucket and bucket[0] < now - 60:
                bucket.popleft()
            if len(bucket) >= 20:
                raise APIError(429, "rate_limited")
            bucket.append(now)
            if len(attempts) > 10000:
                for ip in list(attempts):
                    if not attempts[ip] or attempts[ip][-1] < now - 60:
                        attempts.pop(ip, None)

    def invoke_service(function, *args):
        try:
            return True, function(*args)
        except Exception as error:
            return False, error

    async def call_service(function, *args):
        if offload_service:
            loop = asyncio.get_running_loop()
            succeeded, result = await loop.run_in_executor(executor, invoke_service, function, *args)
        else:
            succeeded, result = invoke_service(function, *args)
        if not succeeded:
            if isinstance(result, APIError):
                headers = {"Retry-After": "60"} if result.status == 429 else None
                return JSONResponse(status_code=result.status, content={"code": result.code}, headers=headers)
            return JSONResponse(status_code=503, content={"code": "service_unavailable"})
        return result

    async def request_body(request: Request) -> bytes:
        length_header = request.headers.get("content-length", "0")
        try:
            length = int(length_header)
        except ValueError as error:
            raise APIError(413, "request_too_large") from error
        if not 0 <= length <= MAX_BODY_BYTES or request.headers.get("transfer-encoding"):
            raise APIError(413, "request_too_large")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY_BYTES:
                raise APIError(413, "request_too_large")
        if len(raw) != length:
            raise APIError(413, "request_too_large")
        return bytes(raw)

    def parse_json_body(raw: bytes) -> dict:
        try:
            body = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise APIError(400, "invalid_request") from error
        if not isinstance(body, dict):
            raise APIError(400, "invalid_json")
        return body

    def checked_job(job_id: str):
        if not JOB_ID.fullmatch(job_id):
            raise APIError(404, "not_found")
        return job_id

    def perform(action: str, authorization: str | None, raw: bytes, job_id: str = "", key: str | None = None):
        p = service.authenticate(authorization)
        body = parse_json_body(raw)
        if action == "verify":
            return service.verify_identity(p)
        if action == "create":
            return service.create(p, body, key)
        if action == "patch":
            return service.patch(p, checked_job(job_id), body)
        if action == "credentials":
            return service.credentials(p, checked_job(job_id))
        if action == "complete":
            return service.complete(p, checked_job(job_id), body)
        raise APIError(404, "not_found")

    async def execute(request: Request, action: str, job_id: str = ""):
        raw = await request_body(request)
        return await call_service(
            perform,
            action,
            request.headers.get("authorization"),
            raw,
            job_id,
            request.headers.get("idempotency-key"),
        )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/identity/verify")
    async def verify_identity(request: Request):
        rate_limit(request.client.host if request.client else "unknown")
        return await execute(request, "verify")

    @app.post("/v1/collections")
    async def create_collection(request: Request):
        return await execute(request, "create")

    @app.patch("/v1/collections/{job_id}")
    async def patch_collection(job_id: str, request: Request):
        return await execute(request, "patch", job_id)

    @app.post("/v1/collections/{job_id}/credentials")
    async def collection_credentials(job_id: str, request: Request):
        return await execute(request, "credentials", job_id)

    @app.post("/v1/collections/{job_id}/complete")
    async def complete_collection(job_id: str, request: Request):
        return await execute(request, "complete", job_id)

    return app


def main():
    import uvicorn

    settings = get_settings()
    app = create_app(settings)
    print(f"trace-dock API listening on {settings.http_host}:{settings.http_port}", flush=True)
    uvicorn.run(app, host=settings.http_host, port=settings.http_port, access_log=False)


if __name__ == "__main__":
    main()
