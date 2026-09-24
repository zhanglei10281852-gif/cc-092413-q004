from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from app.core.errors import DomainError
from app.seismic.schemas import (
    ComputeRequest,
    EventCreate,
    EventPatch,
    ObservationCreate,
    ParameterVersionAction,
    ParameterVersionCreate,
    ParameterVersionDraftUpdate,
)
from app.seismic.service import SeismicService

router = APIRouter(prefix="/api/seismic", tags=["地震科学计算"])


def service() -> SeismicService:
    return SeismicService()


def _if_match(value: str | None) -> str | None:
    """规范化 If-Match 头：ETag 允许带引号。"""
    if value is None:
        return None
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value or None


@router.post("/events", status_code=201)
def create_event(payload: EventCreate):
    try:
        return service().create_event(payload.model_dump(), actor=payload.source)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="external_id 已存在") from exc
        raise


@router.get("/events/{event_id}")
def get_event(
    event_id: int,
    include_observations: bool = Query(True),
    parameter_version: int | None = Query(
        default=None,
        ge=1,
        description="按指定参数版本回放震源参数；不传时返回当前生效值",
    ),
):
    instance = service()
    try:
        value = instance.get_event(event_id, include_observations, parameter_version)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "context": exc.context}) from exc
    if value is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return value


@router.patch("/events/{event_id}")
def patch_event(event_id: int, payload: EventPatch):
    try:
        return service().patch_event(event_id, payload.model_dump(exclude_unset=True), actor="operator")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "context": exc.context}) from exc


@router.get("/events/{event_id}/parameter-versions")
def list_parameter_versions(event_id: int):
    try:
        return service().list_parameter_versions(event_id)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "context": exc.context}) from exc


@router.post("/events/{event_id}/parameter-versions", status_code=201)
def create_parameter_version(event_id: int, payload: ParameterVersionCreate):
    data = payload.model_dump()
    actor = data.pop("created_by")
    try:
        return service().create_parameter_version(event_id, data, actor=actor)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "context": exc.context}) from exc


@router.get("/events/{event_id}/parameter-versions/current")
def current_parameter_version(event_id: int):
    try:
        return service().get_current_parameter_version(event_id)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "context": exc.context}) from exc


@router.get("/events/{event_id}/parameter-versions/{version}")
def get_parameter_version(event_id: int, version: int):
    try:
        return service().get_parameter_version(event_id, version)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "context": exc.context}) from exc


@router.patch("/parameter-versions/{version_id}")
def update_parameter_version(
    version_id: int,
    payload: ParameterVersionDraftUpdate,
    if_match: str | None = Header(default=None, description="草稿当前 content_hash，用于检测并发修改"),
):
    data = payload.model_dump(exclude_unset=True)
    actor = data.pop("operator", "system")
    try:
        return service().update_draft_parameter_version(
            version_id, data, actor=actor, expected_hash=_if_match(if_match)
        )
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "context": exc.context}) from exc


@router.post("/parameter-versions/{version_id}/publish")
def publish_parameter_version(
    version_id: int,
    payload: ParameterVersionAction = ParameterVersionAction(),
    if_match: str | None = Header(default=None, description="发布前校验的 content_hash"),
):
    try:
        return service().publish_parameter_version(
            version_id,
            actor=payload.operator,
            reason=payload.reason,
            expected_hash=_if_match(if_match),
        )
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "context": exc.context}) from exc


@router.post("/parameter-versions/{version_id}/revoke")
def revoke_parameter_version(
    version_id: int,
    payload: ParameterVersionAction,
    if_match: str | None = Header(default=None, description="撤销前校验的 content_hash"),
):
    try:
        return service().revoke_parameter_version(
            version_id,
            actor=payload.operator,
            reason=payload.reason,
            expected_hash=_if_match(if_match),
        )
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "context": exc.context}) from exc


@router.post("/events/{event_id}/observations", status_code=201)
def add_observation(event_id: int, payload: ObservationCreate):
    try:
        return service().add_observation(event_id, payload.model_dump(), actor="station")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc


@router.post("/events/{event_id}/computations", status_code=202)
def enqueue(event_id: int, payload: ComputeRequest):
    try:
        return service().enqueue_computation(
            event_id,
            payload.model_version,
            payload.grid_step_km,
            payload.radius_km,
            payload.requested_by,
            payload.parameter_version_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="事件不存在") from exc
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message, "context": exc.context}) from exc


@router.post("/computations/claim")
def claim(worker_id: str = Query(..., min_length=1)):
    task = service().claim_task(worker_id)
    return {"task": task}


@router.post("/computations/{task_id}/calculate")
def calculate(task_id: int, worker_id: str = Query(..., min_length=1)):
    try:
        return service().calculate_task(task_id, worker_id)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail="任务不属于该工作者或不存在") from exc


@router.get("/computations/{task_id}")
def get_computation(task_id: int):
    row = service().get_computation(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return row
