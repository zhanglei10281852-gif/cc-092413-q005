"""暴露清单领域：建筑与关键设施导入、去重和分区汇总。"""

from app.exposure.service import ExposureService, ensure_schema

__all__ = ["ExposureService", "ensure_schema"]
