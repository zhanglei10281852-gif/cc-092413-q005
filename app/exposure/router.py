from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.exposure.schemas import AssessRequest, ImportRequest
from app.exposure.service import ExposureService

router = APIRouter(prefix="/api/exposure", tags=["暴露清单"])


def service() -> ExposureService:
    return ExposureService()


@router.post("/imports", status_code=201)
def import_records(payload: ImportRequest):
    try:
        return service().import_records(
            payload.records,
            batch_id=payload.batch_id,
            source=payload.source,
            updated_at=payload.updated_at,
            bounds=payload.bounds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/imports/{batch_id}")
def get_import(batch_id: str):
    result = service().get_batch(batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    return result


@router.post("/assessments", status_code=201)
def create_assessment(payload: AssessRequest):
    if payload.computation_id is None and not payload.points:
        raise HTTPException(status_code=400, detail="必须提供 computation_id 或烈度网格 points")
    try:
        points = [point.model_dump() for point in payload.points] if payload.points else None
        return service().assess(
            intensity_threshold=payload.intensity_threshold,
            computation_id=payload.computation_id,
            points=points,
            grid_step_km=payload.grid_step_km,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="烈度计算任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/assessments")
def list_assessments():
    return {"assessments": service().list_assessments()}


@router.get("/assessments/{assessment_key}")
def get_assessment(assessment_key: str):
    try:
        return service().get_assessment(assessment_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="评估结果不存在") from exc


@router.get("/records")
def list_records(
    batch_id: str | None = Query(default=None),
    division_code: str | None = Query(default=None),
    assessment_key: str | None = Query(default=None),
    high_risk_only: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=5000),
):
    try:
        records = service().list_records(
            batch_id=batch_id,
            division_code=division_code,
            assessment_key=assessment_key,
            high_risk_only=high_risk_only,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"records": records, "count": len(records)}


@router.get("/records/{record_id}")
def get_record(record_id: int):
    record = service().get_record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return record
