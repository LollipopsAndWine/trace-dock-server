"""FastAPI HTTP adapter for the CLI control-plane API."""
from collections import defaultdict, deque
import asyncio
import concurrent.futures
from contextlib import asynccontextmanager
import re
import threading
import time
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import Database
from .service import APIError, Service
from .settings import Settings, get_settings
from .storage import OSS

MAX_BODY_BYTES = 65536
JOB_ID = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
CRC64 = re.compile(r"^(0|[1-9][0-9]{0,19})$")


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateCollectionRequest(APIModel):
    model_config = ConfigDict(
        extra="forbid",
        title="创建采集任务请求",
        json_schema_extra={
            "examples": [{
                "client_operation_id": "01JCREATEOPERATION000000000000",
                "device_id": "device-01JDEVICE0000000000000000",
                "machine_name": "developer-laptop",
                "tool": "codex",
                "cli_version": "0.1.0",
            }],
        },
    )

    client_operation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=JOB_ID.pattern,
        description="客户端生成并持久化的幂等操作 ID；必须与 Idempotency-Key 相同。",
    )
    device_id: str = Field(min_length=1, max_length=256, description="本机设备 ID。")
    machine_name: str = Field(min_length=1, max_length=256, description="本机主机名。")
    tool: Literal["codex", "claude"] = Field(description="要采集的工具。")
    cli_version: str = Field(min_length=1, max_length=256, description="CLI 版本。")


class CollectionPatchRequest(APIModel):
    model_config = ConfigDict(
        extra="forbid",
        title="更新采集任务请求",
        json_schema_extra={
            "examples": [
                {"discovered_count": 12},
                {
                    "state": "collecting",
                    "discovered_count": 12,
                    "selection_mode": "partial",
                    "selected_count": 2,
                    "selection_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "consented_at": "2026-11-17T03:00:00Z",
                },
                {"state": "uploading", "upload_confirmed_at": "2026-11-17T03:05:00Z"},
            ],
        },
    )

    state: Literal[
        "created",
        "collecting",
        "awaiting_upload_confirmation",
        "uploading",
        "resumable",
        "failed",
        "cancelled",
    ] | None = Field(default=None, description="任务状态；实际可用的转移由服务端状态机决定。")
    selection_mode: Literal["all", "partial"] | None = Field(default=None, description="上传范围。")
    selected_count: int | None = Field(default=None, ge=0, description="选中的主会话数量。")
    discovered_count: int | None = Field(default=None, ge=0, description="发现的主会话数量。")
    consented_at: str | None = Field(default=None, description="本机采集授权时间，RFC 3339 格式。")
    upload_confirmed_at: str | None = Field(default=None, description="上传确认时间，RFC 3339 格式。")
    selection_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="按排序后的选择列表计算的 SHA-256。",
    )


class CompleteCollectionRequest(APIModel):
    model_config = ConfigDict(
        extra="forbid",
        title="完成采集任务请求",
        json_schema_extra={
            "examples": [{
                "manifest_key": "trajectory/participant_id=8f2c7d1a4e6b409fa3d5c8127096be34/dt=2026-11-17/job=job-example-123/manifest.json",
                "manifest_crc64": "1234567890",
                "manifest_size_bytes": 12345,
            }],
        },
    )

    manifest_key: str = Field(min_length=1, description="上传到 OSS 的 manifest.json 对象键。")
    manifest_crc64: str = Field(
        pattern=CRC64.pattern,
        description="manifest.json 的 OSS CRC64 值。",
    )
    manifest_size_bytes: int = Field(gt=0, description="manifest.json 的字节数。")


class ErrorResponse(BaseModel):
    model_config = ConfigDict(title="错误响应")

    code: str = Field(description="稳定的错误代码，例如 unauthorized、not_found 或 invalid_request。")


class HealthResponse(BaseModel):
    model_config = ConfigDict(title="健康检查响应")

    status: Literal["ok"] = Field(description="API 进程当前可以响应请求。")


class IdentityResponse(BaseModel):
    model_config = ConfigDict(title="身份验证响应")

    participant_id: str = Field(description="当前 Bearer token 对应的唯一 participant ID；一个邮箱可关联多条 participant 记录。")
    masked_email: str = Field(description="脱敏后的邮箱，仅用于让用户确认当前身份。")


class RulesResponse(BaseModel):
    model_config = ConfigDict(title="任务规则")

    classification_version: str = Field(description="workspace 分类规则版本。")
    secret_version: str = Field(description="敏感信息检测规则版本。")
    large_file_version: str = Field(description="大文件排除规则版本。")
    large_file_threshold_bytes: int = Field(description="按体积排除文件的阈值，单位为字节。")
    whitelist: list[str] = Field(description="优先保留的路径模式。")
    blacklist: list[str] = Field(description="按类型或路径排除的模式。")
    secret_patterns: list[str] = Field(description="附加敏感信息正则；当前必须为空列表。")


class CreateCollectionResponse(BaseModel):
    model_config = ConfigDict(title="创建任务响应")

    job_id: str = Field(description="本次采集任务 ID。")
    rules: RulesResponse = Field(description="创建任务时冻结的采集规则。")
    object_prefix: str = Field(description="按稳定参与者 ID 隔离的当前任务 OSS 前缀；不包含邮箱或 Bearer token。")


class CollectionPatchResponse(BaseModel):
    model_config = ConfigDict(title="任务状态响应")

    job_id: str = Field(description="采集任务 ID。")
    state: str = Field(description="更新后的任务状态。")


class CredentialsResponse(BaseModel):
    model_config = ConfigDict(title="临时 OSS 凭证响应")

    endpoint: str = Field(description="CLI 直传 OSS 使用的 HTTPS endpoint。")
    bucket: str = Field(description="目标 OSS bucket。")
    prefix: str = Field(description="临时凭证允许访问的当前 Job 前缀。")
    region: str = Field(description="OSS / STS 地域。")
    access_key_id: str = Field(description="临时 STS AccessKey ID。")
    access_key_secret: str = Field(description="临时 STS AccessKey Secret。")
    security_token: str = Field(description="临时 STS SecurityToken。")
    expiration: str = Field(description="临时凭证过期时间。")
    path_style: bool = Field(description="是否使用 path-style OSS 地址。")


class CollectionSummary(BaseModel):
    model_config = ConfigDict(title="采集统计")

    discovered_count: int = Field(description="发现的主会话数量。")
    selected_count: int = Field(description="用户选择的主会话数量。")
    workspace_exists_count: int = Field(description="存在 workspace 目录的主会话数量。")
    reconstructable_count: int = Field(description="可重建会话数量；当前服务端要求为 0。")
    archive_only_count: int = Field(description="仅可归档、不可重建的会话数量。")
    invalid_count: int = Field(description="被轨迹状态判定为 abnormal 的主会话数量。")
    excluded_count: int = Field(description="因敏感信息或其他规则未生成轨迹的数量。")
    large_file_missing_remote_path_count: int = Field(description="大文件记录缺少 remote_path 的数量。")
    uploaded_bytes: int = Field(description="服务端校验通过的总字节数。")
    verified_object_count: int = Field(description="服务端校验通过的 OSS 对象数量。")


class CompleteCollectionResponse(BaseModel):
    model_config = ConfigDict(title="采集完成回执")

    job_id: str = Field(description="采集任务 ID。")
    participant_id: str = Field(description="参与者 ID。")
    state: Literal["completed"] = Field(description="任务最终状态。")
    verification: Literal["crc64_verified"] = Field(description="对象完整性校验结果。")
    object_prefix: str = Field(description="按稳定参与者 ID 隔离的任务 OSS 前缀。")
    manifest_key: str = Field(description="manifest.json 的 OSS 对象键。")
    manifest_crc64: str = Field(description="manifest.json 的 OSS CRC64。")
    uploaded_bytes: int = Field(description="校验通过的总字节数。")
    verified_object_count: int = Field(description="校验通过的 OSS 对象数量。")
    completed_at: str = Field(description="服务端完成校验的 UTC 时间。")
    summary: CollectionSummary = Field(description="本次采集的统计汇总。")


bearer = HTTPBearer(auto_error=False)


def authorization_value(credentials: HTTPAuthorizationCredentials | None) -> str | None:
    if credentials is None:
        return None
    return f"{credentials.scheme} {credentials.credentials}"


class RequestTooLarge(Exception):
    pass


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

        expected_length = None
        if scope.get("method") in {"POST", "PATCH"}:
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            if b"transfer-encoding" in headers:
                response = JSONResponse(status_code=413, content={"code": "request_too_large"})
                await response(scope, receive, send)
                return
            raw_length = headers.get(b"content-length", b"0")
            try:
                expected_length = int(raw_length)
            except ValueError:
                expected_length = -1
            if not 0 <= expected_length <= MAX_BODY_BYTES:
                response = JSONResponse(status_code=413, content={"code": "request_too_large"})
                await response(scope, receive, send)
                return

        received_length = 0

        async def limited_receive():
            nonlocal received_length
            message = await receive()
            if message["type"] == "http.request":
                received_length += len(message.get("body", b""))
                if received_length > MAX_BODY_BYTES:
                    raise RequestTooLarge
                if not message.get("more_body", False) and expected_length is not None and received_length != expected_length:
                    raise RequestTooLarge
            return message

        async def send_no_store(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, limited_receive, send_no_store)
        except RequestTooLarge:
            response = JSONResponse(status_code=413, content={"code": "request_too_large"})
            await response(scope, receive, send)


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
        description="Trace Dock 控制面 API：身份验证、采集任务状态管理和 OSS 上传完成校验。轨迹与 workspace 文件由 CLI 直接上传到私有 OSS。",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=[
            {"name": "系统", "description": "服务健康状态。"},
            {"name": "身份", "description": "参与者身份验证。"},
            {"name": "采集任务", "description": "创建任务并推进本机采集授权和选择状态。"},
            {"name": "上传", "description": "获取临时 OSS 凭证并完成服务端对象校验。"},
        ],
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

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, _error: RequestValidationError):
        return JSONResponse(status_code=400, content={"code": "invalid_request"})

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

    def checked_job(job_id: str):
        if not JOB_ID.fullmatch(job_id):
            raise APIError(404, "not_found")
        return job_id

    def perform(action: str, authorization: str | None, body: dict, job_id: str = "", key: str | None = None):
        p = service.authenticate(authorization)
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

    async def execute(
        action: str,
        body: APIModel | None,
        authorization: str | None,
        job_id: str = "",
        key: str | None = None,
    ):
        return await call_service(
            perform,
            action,
            authorization,
            body.model_dump(exclude_unset=True) if body is not None else {},
            job_id,
            key,
        )

    @app.get(
        "/healthz",
        response_model=HealthResponse,
        summary="健康检查",
        description="供负载均衡和运维检查 API 进程是否可以响应。不会检查数据库、OSS 或 STS 权限。",
        tags=["系统"],
    )
    async def healthz() -> HealthResponse:
        return {"status": "ok"}

    @app.post(
        "/v1/identity/verify",
        response_model=IdentityResponse,
        responses={401: {"model": ErrorResponse}},
        summary="验证参与者身份",
        description="使用 Authorization: Bearer <token> 验证当前参与者，并返回脱敏后的身份信息。此接口不需要请求体。",
        tags=["身份"],
    )
    async def verify_identity(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        rate_limit(request.client.host if request.client else "unknown")
        return await execute("verify", None, authorization_value(credentials), key=None)

    @app.post(
        "/v1/collections",
        response_model=CreateCollectionResponse,
        responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        summary="创建采集任务",
        description="登记客户端、设备和工具信息，创建或幂等恢复一个采集任务，并返回本次任务冻结的规则和 OSS 对象前缀。",
        tags=["采集任务"],
    )
    async def create_collection(
        body: CreateCollectionRequest,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                description="必须与请求体中的 client_operation_id 完全一致，用于安全重试和幂等创建。",
                examples=["01JCREATEOPERATION000000000000"],
                openapi_examples={
                    "default": {
                        "summary": "客户端操作 ID 示例",
                        "value": "01JCREATEOPERATION000000000000",
                    },
                },
            ),
        ],
    ):
        return await execute("create", body, authorization_value(credentials), key=idempotency_key)

    @app.patch(
        "/v1/collections/{job_id}",
        response_model=CollectionPatchResponse,
        responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        summary="更新采集任务",
        description="更新发现数量、选择范围、授权时间和任务状态。服务端会根据任务状态机校验更新是否合法。",
        tags=["采集任务"],
    )
    async def patch_collection(
        job_id: Annotated[str, Path(min_length=1, max_length=128, pattern=JOB_ID.pattern)],
        body: CollectionPatchRequest,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        return await execute("patch", body, authorization_value(credentials), job_id)

    @app.post(
        "/v1/collections/{job_id}/credentials",
        response_model=CredentialsResponse,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        summary="获取临时 OSS 凭证",
        description="在用户确认上传后，为当前采集任务签发仅限其 OSS 前缀的临时 STS 凭证。此接口不需要请求体。",
        tags=["上传"],
    )
    async def collection_credentials(
        job_id: Annotated[str, Path(min_length=1, max_length=128, pattern=JOB_ID.pattern)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        return await execute("credentials", None, authorization_value(credentials), job_id)

    @app.post(
        "/v1/collections/{job_id}/complete",
        response_model=CompleteCollectionResponse,
        responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
        summary="完成并校验采集任务",
        description="读取 OSS 中的 manifest，校验对象大小和 CRC64，并在全部对象通过后返回完成回执。",
        tags=["上传"],
    )
    async def complete_collection(
        job_id: Annotated[str, Path(min_length=1, max_length=128, pattern=JOB_ID.pattern)],
        body: CompleteCollectionRequest,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        return await execute("complete", body, authorization_value(credentials), job_id)

    return app


def main():
    import uvicorn

    settings = get_settings()
    app = create_app(settings)
    print(f"trace-dock API listening on {settings.http_host}:{settings.http_port}", flush=True)
    uvicorn.run(app, host=settings.http_host, port=settings.http_port, access_log=False)


if __name__ == "__main__":
    main()
