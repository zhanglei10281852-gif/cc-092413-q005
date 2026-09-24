from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ImportRequest(BaseModel):
    batch_id: str = Field(..., min_length=1, max_length=80)
    source: str = Field(default="api", min_length=1, max_length=40)
    updated_at: str | None = Field(default=None, max_length=40)
    bounds: dict[str, float] | None = None
    records: list[Any] = Field(default_factory=list)


class GridPoint(BaseModel):
    latitude: float
    longitude: float
    intensity: float


class AssessRequest(BaseModel):
    intensity_threshold: float = Field(..., gt=0, le=12)
    computation_id: int | None = None
    points: list[GridPoint] | None = None
    grid_step_km: float | None = Field(default=None, gt=0, le=200)
