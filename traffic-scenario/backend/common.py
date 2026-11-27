import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


# 로그에 사용할 UTC 시각 생성
def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# 비교 가능한 JSON 문자열 생성
def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


# 데이터 식별용 해시 생성
def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


class ApiError(Exception):
    # 공통 API 오류 정보 저장
    def __init__(self, status, code, message, request_id=None, details=None):
        self.status, self.code, self.message = status, code, message
        self.request_id, self.details = request_id, details or []


# 공통 오류 응답 처리 등록
def install_errors(app: FastAPI):
    # API 오류 응답 생성
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        rid = exc.request_id or request.headers.get("x-request-id")
        return JSONResponse(status_code=exc.status, content={"error": {
            "code": exc.code, "message": exc.message, "request_id": rid, "details": exc.details,
        }}, headers={"Cache-Control": "no-store", **({"X-Request-ID": rid} if rid else {})})

    # 입력 검증 오류 응답 생성
    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(map(str, e["loc"])), "reason": e["msg"]} for e in exc.errors()]
        return await api_error(request, ApiError(422, "VALIDATION_ERROR", "요청 형식을 확인해 주세요.", details=details))

    # HTTP 오류 응답 생성
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        return await api_error(request, ApiError(exc.status_code, code, str(exc.detail)))

    # 서버 내부 오류 기록
    @app.exception_handler(Exception)
    async def internal(request: Request, exc: Exception):
        logging.getLogger("mbm").exception("Unhandled API error", exc_info=exc)

        return await api_error(request, ApiError(500, "INTERNAL_ERROR", "내부 오류가 발생했습니다."))

    # 최신 상태 조회를 위한 캐시 방지
    @app.middleware("http")
    async def cache_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"

        return response
