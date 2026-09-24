from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.seismic.exposure.schemas import ExposureBatchCreate, IntensityGridReplace
from app.seismic.exposure.service import ExposureService

router = APIRouter(prefix="/api/seismic/exposure", tags=["暴露清单"])


def service() -> ExposureService:
    return ExposureService()


@router.post("/batches", status_code=201)
def create_batch(payload: ExposureBatchCreate):
    try:
        return service().import_batch(
            payload.batch_ref,
            payload.records,
            received_at=payload.received_at,
            source=payload.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/batches")
def list_batches():
    return {"batches": service().list_batches()}


@router.get("/batches/{batch_ref}")
def get_batch(batch_ref: str):
    report = service().get_batch(batch_ref)
    if report is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return report


@router.get("/records/{record_key}/history")
def record_history(record_key: str):
    history = service().get_record_history(record_key)
    if not history:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"record_key": record_key, "versions": history}


@router.put("/grids/{scope}")
def replace_grid(scope: str, payload: IntensityGridReplace):
    if payload.scope is not None and scope != payload.scope:
        raise HTTPException(status_code=422, detail="路径与报文中的 scope 不一致")
    try:
        return service().replace_intensity_grid(
            [cell.model_dump() for cell in payload.cells], scope=scope, updated_at=payload.updated_at
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/computations/{task_id}/grid", status_code=201)
def grid_from_computation(task_id: int, scope: str = Query("default", min_length=1, max_length=40)):
    instance = service()
    try:
        return instance.load_grid_from_computation(task_id, scope=scope)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="计算任务不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="计算任务尚未完成") from exc


@router.get("/summary")
def summary(
    intensity_min: float = Query(..., ge=0, le=12, description="高风险烈度阈值"),
    scope: str = Query("default", min_length=1, max_length=40),
    district_code: str | None = Query(None, max_length=20),
    tolerance_km: float = Query(11.0, gt=0, le=100),
):
    return service().summarize(
        intensity_min, scope=scope, district_code=district_code, tolerance_km=tolerance_km
    )
