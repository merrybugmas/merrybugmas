from typing import Annotated, Literal
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Category = Literal["cookie", "gift", "ornament", "hot_choco", "toy"]
Phase = Literal["idle", "analyzing", "modifying", "restarting", "testing", "comparing", "finished", "failed"]
Outcome = Literal["success", "http_error", "timeout", "connection_error", "cancelled"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SearchQuery(Model):
    category: Category
    max_price: Annotated[int, Field(ge=0, le=1000000)]


class Item(Model):
    product_id: Annotated[int, Field(gt=0)]
    name: str
    price: Annotated[int, Field(ge=0)]
    shop_name: str
    location: str


class SearchResponse(Model):
    request_id: ID
    items: list[Item] = Field(max_length=10)


class StartRun(Model):
    scenario_id: Literal["normal", "surge"] | None = None
    request_count: Annotated[int, Field(ge=1, le=1000)] = 1000
    arrival_duration_s: Annotated[float, Field(gt=0)] | None = None
    seed: Annotated[int, Field(ge=0)] = 42
    client_timeout_ms: Annotated[int, Field(ge=1, le=60000)] | None = None
    source_run_id: ID | None = None

    # 신규 실험과 재실험 입력 조건 검증
    @model_validator(mode="after")
    def condition(self):
        if self.source_run_id:
            if self.model_fields_set != {"source_run_id"}:
                raise ValueError("source_run_id must be sent alone")
        elif None in (self.scenario_id, self.arrival_duration_s, self.client_timeout_ms):
            raise ValueError("scenario_id, arrival_duration_s and client_timeout_ms are required")

        return self


class Compare(Model):
    before_run_id: ID
    after_run_id: ID
    criteria_id: ID

    # 서로 다른 비교 대상인지 검증
    @model_validator(mode="after")
    def distinct(self):
        if self.before_run_id == self.after_run_id:
            raise ValueError("Two different run IDs are required")

        return self


class Progress(Model):
    run_id: ID
    operation_id: ID
    message_id: ID
    phase: Phase
    message: Annotated[str, Field(min_length=1, max_length=500)]
    related_run_id: ID | None = None


class Restart(Model):
    run_id: ID
    reason: Annotated[str, Field(max_length=500)] | None = None


class TargetEvent(Model):
    source_event_id: ID
    type: Literal["request.received", "request.queued", "request.processing", "request.succeeded", "request.failed", "request.cancelled"]
    request_id: ID
    customer_id: ID
    occurred_at: datetime
    data: dict

    # 이벤트 시각의 시간대 확인
    @field_validator("occurred_at")
    @classmethod
    def aware(cls, v):
        if v.tzinfo is None:
            raise ValueError("Timezone is required")

        return v

    # 요청 상태별 이벤트 데이터 검증
    @model_validator(mode="after")
    def payload(self):
        d = self.data

        if self.type == "request.received":
            SearchQuery.model_validate(d.get("query", {}))

        if self.type == "request.queued":
            if not isinstance(d.get("queue_size"), int) or d["queue_size"] < 0:
                raise ValueError("queue_size must be nonnegative")

        if self.type == "request.processing":
            self.duration(d, "queue_wait_ms")

        if self.type == "request.succeeded":
            for name in ("queue_wait_ms", "processing_ms", "server_total_ms"):
                self.duration(d, name)

            if not isinstance(d.get("items"), list) or len(d["items"]) > 10:
                raise ValueError("items must be an array of at most 10 products")

            for item in d["items"]:
                Item.model_validate(item)

        if self.type in {"request.failed", "request.cancelled"}:
            if not isinstance(d.get("error_code"), str):
                raise ValueError("error_code is required")

            self.duration(d, "server_total_ms")

            for name in ("queue_wait_ms", "processing_ms"):
                if d.get(name) is not None:
                    self.duration(d, name)

        return self

    # 측정 시간이 유효한 값인지 검증
    @staticmethod
    def duration(d, name):
        import math

        v = d.get(name)

        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f"{name} must be a finite nonnegative duration")


class EventBatch(Model):
    batch_id: ID
    events: list[TargetEvent] = Field(max_length=500)
    final: bool = False

    # 이벤트 배치의 종료 조건 검증
    @model_validator(mode="after")
    def nonempty(self):
        if not self.final and not self.events:
            raise ValueError("Nonfinal event batch must not be empty")

        return self


class Measurement(Model):
    request_id: ID
    customer_id: ID
    query: SearchQuery
    scheduled_offset_ms: Annotated[float, Field(ge=0)]
    sent_offset_ms: Annotated[float, Field(ge=0)]
    elapsed_ms: Annotated[float, Field(ge=0)]
    outcome: Outcome
    http_status: Annotated[int, Field(ge=100, le=599)] | None
    items: list[Item] | None = Field(max_length=10)

    # 고객 결과와 HTTP 응답의 일치 여부 검증
    @model_validator(mode="after")
    def result(self):
        if self.outcome == "success":
            if self.http_status != 200 or self.items is None:
                raise ValueError("Success requires HTTP 200 and items")
        elif self.outcome == "http_error":
            if self.http_status is None or self.http_status == 200:
                raise ValueError("HTTP error requires a non-200 response")
        elif self.http_status is not None:
            raise ValueError("No HTTP status for transport/cancellation failures")

        return self


class FinalSummary(Model):
    planned: Annotated[int, Field(ge=0)]
    sent: Annotated[int, Field(ge=0)]
    dropped: Annotated[int, Field(ge=0)]
    elapsed_ms: Annotated[float, Field(gt=0)]
    termination: Literal["completed", "stopped", "failed"]

    # 예정 요청 수와 실제 전송 수 검증
    @model_validator(mode="after")
    def totals(self):
        if self.planned != self.sent + self.dropped:
            raise ValueError("planned must equal sent + dropped")

        return self


class MeasurementBatch(Model):
    batch_id: ID
    measurements: list[Measurement] = Field(max_length=500)
    final: bool = False
    summary: FinalSummary | None = None

    # 측정 종료 배치의 집계 정보 검증
    @model_validator(mode="after")
    def finish(self):
        if self.final != (self.summary is not None):
            raise ValueError("summary is required only in a final batch")

        if not self.final and not self.measurements:
            raise ValueError("Nonfinal measurement batch must not be empty")

        return self


# 실험 API 응답 모델
class RunConfig(Model):
    scenario_id: Literal["normal", "surge"]
    request_count: int
    arrival_duration_s: float
    seed: int
    client_timeout_ms: int


class RunCreated(Model):
    run_id: ID
    status: Literal["pending"]
    source_run_id: ID | None
    created_at: str
    config: RunConfig
    request_plan_hash: str
    status_url: str


class RunStopped(Model):
    run_id: ID
    status: Literal["stopping"]


class ServerCounts(Model):
    received: int
    queued: int
    processing: int
    succeeded: int
    failed: int
    cancelled: int


class ClientCounts(Model):
    planned: int
    sent: int
    success: int
    failed: int
    pending: int
    dropped: int


class Metrics(Model):
    avg_response_ms: float | None
    p95_response_ms: float | None
    failure_rate: float | None
    success_rps: float | None


class Artifact(Model):
    code_revision: str
    schema_hash: str
    dataset_hash: str
    request_plan_hash: str
    runtime_config_hash: str
    warmup_policy: str


class RunSnapshot(Model):
    run_id: ID
    status: Literal["pending", "running", "stopping", "completed", "stopped", "failed"]
    source_run_id: ID | None
    config: RunConfig
    started_at: str | None
    finished_at: str | None
    last_seq: int
    server_counts: ServerCounts
    client_counts: ClientCounts
    metrics: Metrics
    telemetry_complete: bool
    agent_phase: Phase
    artifact: Artifact


class Event(Model):
    seq: int
    run_id: ID
    type: str
    request_id: ID | None
    customer_id: ID | None
    occurred_at: str
    data: dict


class EventPage(Model):
    events: list[Event]
    next_after_seq: int
    has_more: bool


class RequestDetail(Model):
    run_id: ID
    request_id: ID
    customer_id: ID
    query: SearchQuery
    server_status: str | None
    client_outcome: str | None
    received_at: str | None
    started_at: str | None
    finished_at: str | None
    queue_wait_ms: float | None
    current_wait_ms: float | None = Field(description="대기 이벤트 수신 이후 경과 시간으로 계산하는 화면 표시용 추정치. 최종 대기 시간은 서버 queue_wait_ms.")
    processing_ms: float | None
    server_total_ms: float | None
    client_elapsed_ms: float | None
    http_status: int | None
    error_code: str | None
    items: list[Item] | None


class LogItem(Model):
    seq: int
    occurred_at: str
    level: str
    source: str
    message: str
    request_id: ID | None


class LogPage(Model):
    logs: list[LogItem]
    next_after_seq: int
    has_more: bool


class MetricComparison(Model):
    name: str
    before: float | None
    after: float | None
    delta: float | None
    relative_change: float | None


class ComparisonResult(Model):
    before_run_id: ID
    after_run_id: ID
    criteria_id: ID
    verdict: Literal["improved", "regressed", "inconclusive"]
    reasons: list[str]
    metrics: list[MetricComparison]
    correctness_passed: bool


class ProgressResult(Model):
    accepted: bool
    seq: int
    duplicate: bool


class RestartResult(Model):
    operation_id: ID
    status: Literal["restarting"]


class MeasurementReceipt(Model):
    accepted_count: int
    duplicate_count: int
    final_received: bool


class EventReceipt(MeasurementReceipt):
    last_seq: int


class ErrorDetail(Model):
    field: str
    reason: str


class ErrorContent(Model):
    code: str
    message: str
    request_id: str | None
    details: list[ErrorDetail]


class ErrorResponse(Model):
    error: ErrorContent


ERROR_RESPONSES = {status: {"model": ErrorResponse} for status in (404, 409, 422, 429, 500, 503, 504)}
