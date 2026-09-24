from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExposureBatchCreate(BaseModel):
    # 记录使用原始字典，逐行校验由服务层完成：
    # 坏行只返回行号与原因，不影响同批其他已确认行。
    batch_ref: str = Field(..., min_length=1, max_length=80)
    source: str = Field(default="", max_length=80)
    received_at: str | None = Field(default=None, max_length=40)
    records: list[dict[str, Any]] = Field(..., min_length=1)


class IntensityCellIn(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    intensity: float = Field(..., ge=0, le=12)


class IntensityGridReplace(BaseModel):
    scope: str | None = Field(default=None, min_length=1, max_length=40)
    updated_at: str | None = Field(default=None, max_length=40)
    cells: list[IntensityCellIn] = Field(..., min_length=1)
