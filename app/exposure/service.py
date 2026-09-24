from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Any

from app.core.clock import to_storage, utc_now
from app.database import get_connection, transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS exposure_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    received_at TEXT NOT NULL,
    total_rows INTEGER NOT NULL DEFAULT 0,
    accepted_rows INTEGER NOT NULL DEFAULT 0,
    rejected_rows INTEGER NOT NULL DEFAULT 0,
    duplicate_rows INTEGER NOT NULL DEFAULT 0,
    inserted_rows INTEGER NOT NULL DEFAULT 0,
    content_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'confirmed'
);
CREATE TABLE IF NOT EXISTS exposure_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_key TEXT NOT NULL,
    record_type TEXT NOT NULL CHECK(record_type IN ('building','facility')),
    name TEXT NOT NULL,
    division_code TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    population INTEGER NOT NULL DEFAULT 0,
    critical_level TEXT NOT NULL DEFAULT 'normal' CHECK(critical_level IN ('normal','key','critical')),
    raw_json TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL UNIQUE,
    source_batch_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    first_seen_batch_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exposure_division ON exposure_records(division_code);
CREATE INDEX IF NOT EXISTS idx_exposure_batch ON exposure_records(source_batch_id);
CREATE INDEX IF NOT EXISTS idx_exposure_type ON exposure_records(record_type, critical_level);
CREATE TABLE IF NOT EXISTS exposure_import_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    record_key TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_exposure_errors_batch ON exposure_import_errors(batch_id, line_number);
CREATE TABLE IF NOT EXISTS exposure_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_key TEXT NOT NULL UNIQUE,
    computation_id INTEGER,
    intensity_threshold REAL NOT NULL,
    grid_step_km REAL NOT NULL,
    record_digest TEXT NOT NULL,
    grid_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exposure_cell_intensity (
    assessment_id INTEGER NOT NULL REFERENCES exposure_assessments(id) ON DELETE CASCADE,
    lat_cell REAL NOT NULL,
    lon_cell REAL NOT NULL,
    intensity REAL NOT NULL,
    PRIMARY KEY(assessment_id, lat_cell, lon_cell)
);
CREATE TABLE IF NOT EXISTS exposure_record_risks (
    assessment_id INTEGER NOT NULL REFERENCES exposure_assessments(id) ON DELETE CASCADE,
    record_id INTEGER NOT NULL REFERENCES exposure_records(id) ON DELETE CASCADE,
    intensity REAL,
    is_high_risk INTEGER NOT NULL CHECK(is_high_risk IN (0,1)),
    PRIMARY KEY(assessment_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_exposure_risk_assessment ON exposure_record_risks(assessment_id, is_high_risk);
"""

RECORD_TYPES = {"building", "facility"}
CRITICAL_LEVELS = {"normal", "key", "critical"}
_DIVISION_CODE_MAX = 32
_NAME_MAX = 120
_RECORD_KEY_MAX = 80
# 中国县域经纬度的常用有效范围；可通过 import_records 的 bounds 参数覆盖。
DEFAULT_BOUNDS = {"lat_min": 3.0, "lat_max": 54.0, "lon_min": 73.0, "lon_max": 136.0}


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _now() -> str:
    return to_storage(utc_now())


def _content_digest(rows: list[dict[str, Any]]) -> str:
    material = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _fingerprint(record_type: str, name: str, division_code: str, lat: float, lon: float) -> str:
    material = json.dumps(
        [record_type, name.strip(), division_code.strip(), round(lat, 6), round(lon, 6)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ExposureService:
    """暴露记录的幂等导入、去重和基于烈度网格的分区汇总。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    # ------------------------------------------------------------------ 导入
    def import_records(
        self,
        rows: list[dict[str, Any]],
        *,
        batch_id: str,
        source: str = "api",
        updated_at: str | None = None,
        bounds: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """导入一批记录。

        同一 ``batch_id`` 重复调用直接返回首次结果，不产生重复数据。坏行被拒绝并
        返回其在输入中的行号（从 1 开始，对应不含表头时的第一条数据行），其余
        合法行在同一事务内确认。
        """
        if not batch_id or not str(batch_id).strip():
            raise ValueError("batch_id 不能为空")
        batch_id = str(batch_id).strip()

        cached = self.connection.execute(
            "SELECT * FROM exposure_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if cached is not None:
            return self._batch_result(cached, replayed=True)

        effective_bounds = {**DEFAULT_BOUNDS, **(bounds or {})}
        stamped_at = (updated_at or _now()).strip() or _now()
        errors: list[dict[str, Any]] = []
        valid: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()
        intra_batch_duplicates = 0

        for index, raw in enumerate(rows, start=1):
            problem = self._validate_row(raw, effective_bounds)
            if problem is None:
                record = self._normalize(raw)
                fingerprint = _fingerprint(
                    record["record_type"], record["name"], record["division_code"],
                    record["latitude"], record["longitude"],
                )
                record["fingerprint"] = fingerprint
                if fingerprint in seen_fingerprints:
                    intra_batch_duplicates += 1
                else:
                    seen_fingerprints.add(fingerprint)
                    valid.append(record)
            else:
                errors.append(
                    {
                        "line_number": index,
                        "record_key": str((raw or {}).get("record_key") or "")[:_RECORD_KEY_MAX]
                        if isinstance(raw, dict) else "",
                        "reason": problem,
                        "raw_json": json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str),
                    }
                )

        digest = _content_digest(rows)
        now = _now()
        rejected = len(errors)
        accepted = len(rows) - rejected
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO exposure_batches(batch_id,source,received_at,total_rows,accepted_rows,"
                "rejected_rows,duplicate_rows,inserted_rows,content_digest,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id, source, now, len(rows), accepted, rejected, 0, 0, digest, "confirmed"
                ),
            )
            batch_pk = cursor.lastrowid
            inserted = 0
            cross_batch_duplicates = 0
            for record in valid:
                existing = connection.execute(
                    "SELECT id FROM exposure_records WHERE fingerprint=?",
                    (record["fingerprint"],),
                ).fetchone()
                if existing is not None:
                    cross_batch_duplicates += 1
                    connection.execute(
                        "UPDATE exposure_records SET name=?,division_code=?,latitude=?,longitude=?,"
                        "population=?,critical_level=?,raw_json=?,record_key=?,source_batch_id=?,updated_at=? "
                        "WHERE id=?",
                        (
                            record["name"], record["division_code"], record["latitude"],
                            record["longitude"], record["population"], record["critical_level"],
                            record["raw_json"], record["record_key"], batch_id, stamped_at,
                            existing["id"],
                        ),
                    )
                    continue
                connection.execute(
                    "INSERT INTO exposure_records(record_key,record_type,name,division_code,latitude,"
                    "longitude,population,critical_level,raw_json,fingerprint,source_batch_id,"
                    "updated_at,first_seen_batch_id,first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record["record_key"], record["record_type"], record["name"],
                        record["division_code"], record["latitude"], record["longitude"],
                        record["population"], record["critical_level"], record["raw_json"],
                        record["fingerprint"], batch_id, stamped_at, batch_id, now,
                    ),
                )
                inserted += 1
            for item in errors:
                connection.execute(
                    "INSERT INTO exposure_import_errors(batch_id,line_number,record_key,reason,raw_json) "
                    "VALUES(?,?,?,?,?)",
                    (batch_id, item["line_number"], item["record_key"], item["reason"], item["raw_json"]),
                )
            duplicates = intra_batch_duplicates + cross_batch_duplicates
            connection.execute(
                "UPDATE exposure_batches SET duplicate_rows=?,inserted_rows=? WHERE id=?",
                (duplicates, inserted, batch_pk),
            )
            batch_row = connection.execute(
                "SELECT * FROM exposure_batches WHERE id=?", (batch_pk,)
            ).fetchone()
        return self._batch_result(batch_row, replayed=False)

    def _validate_row(self, raw: Any, bounds: dict[str, float]) -> str | None:
        if not isinstance(raw, dict):
            return "记录必须是对象"
        record_type = raw.get("record_type")
        if record_type not in RECORD_TYPES:
            return "record_type 必须是 building 或 facility"
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > _NAME_MAX:
            return "name 缺失或过长"
        division_code = raw.get("division_code")
        if not isinstance(division_code, str) or not division_code.strip() or len(division_code.strip()) > _DIVISION_CODE_MAX:
            return "division_code 缺失或非法"
        lat = raw.get("latitude")
        lon = raw.get("longitude")
        if not isinstance(lat, (int, float)) or isinstance(lat, bool) or not math.isfinite(lat):
            return "latitude 必须是数字"
        if not isinstance(lon, (int, float)) or isinstance(lon, bool) or not math.isfinite(lon):
            return "longitude 必须是数字"
        if not (bounds["lat_min"] <= float(lat) <= bounds["lat_max"]):
            return f"latitude 超出有效范围 {bounds['lat_min']}~{bounds['lat_max']}"
        if not (bounds["lon_min"] <= float(lon) <= bounds["lon_max"]):
            return f"longitude 超出有效范围 {bounds['lon_min']}~{bounds['lon_max']}"
        population = raw.get("population", 0)
        if not isinstance(population, int) or isinstance(population, bool) or population < 0:
            return "population 必须是非负整数"
        critical_level = raw.get("critical_level", "normal")
        if critical_level not in CRITICAL_LEVELS:
            return "critical_level 必须是 normal、key 或 critical"
        record_key = raw.get("record_key", "")
        if record_key is not None and not isinstance(record_key, str):
            return "record_key 必须是字符串"
        if isinstance(record_key, str) and len(record_key) > _RECORD_KEY_MAX:
            return "record_key 过长"
        return None

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        record_key = (raw.get("record_key") or "").strip()
        clean = {
            "record_key": record_key,
            "record_type": raw["record_type"],
            "name": raw["name"].strip(),
            "division_code": raw["division_code"].strip(),
            "latitude": round(float(raw["latitude"]), 6),
            "longitude": round(float(raw["longitude"]), 6),
            "population": int(raw.get("population", 0)),
            "critical_level": raw.get("critical_level", "normal"),
        }
        clean["raw_json"] = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        return clean

    def _batch_result(self, batch_row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
        error_rows = self.connection.execute(
            "SELECT line_number,record_key,reason FROM exposure_import_errors WHERE batch_id=? "
            "ORDER BY line_number,id",
            (batch_row["batch_id"],),
        ).fetchall()
        return {
            "batch_id": batch_row["batch_id"],
            "source": batch_row["source"],
            "received_at": batch_row["received_at"],
            "status": batch_row["status"],
            "replayed": replayed,
            "total_rows": batch_row["total_rows"],
            "accepted_rows": batch_row["accepted_rows"],
            "rejected_rows": batch_row["rejected_rows"],
            "inserted_rows": batch_row["inserted_rows"],
            "duplicate_rows": batch_row["duplicate_rows"],
            "content_digest": batch_row["content_digest"],
            "errors": [dict(item) for item in error_rows],
        }

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        batch_row = self.connection.execute(
            "SELECT * FROM exposure_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch_row is None:
            return None
        return self._batch_result(batch_row, replayed=True)

    # ------------------------------------------------------------------ 评估
    def _record_digest(self, connection: sqlite3.Connection) -> str:
        """当前已确认暴露数据集的确定性摘要（按记录 id 排序的指纹序列）。"""
        rows = connection.execute(
            "SELECT fingerprint FROM exposure_records ORDER BY id"
        ).fetchall()
        material = "\n".join(item["fingerprint"] for item in rows)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def assess(
        self,
        *,
        intensity_threshold: float,
        computation_id: int | None = None,
        points: list[dict[str, Any]] | None = None,
        grid_step_km: float | None = None,
    ) -> dict[str, Any]:
        """按烈度阈值把当前全部确认记录落到烈度网格，生成风险标记与分区汇总。

        网格点可直接给出（``points``），或引用已完成的 ``seismic_computations``
        结果。评估按“确认数据摘要 + 网格摘要 + 阈值 + 步长”内容寻址：相同输入
        重复评估返回同一结果（含相同 assessment_key），保证可重复。
        """
        resolved_points, resolved_step = self._resolve_grid(computation_id, points, grid_step_km)
        if not resolved_points:
            raise ValueError("烈度网格为空，无法评估")
        # 以格网点（而非输入点顺序）构建规范网格，使摘要与汇总对 points 顺序不敏感。
        cell_map: dict[tuple[float, float], float] = {}
        for point in sorted(resolved_points, key=lambda item: (item["latitude"], item["longitude"])):
            key = (round(float(point["latitude"]), 6), round(float(point["longitude"]), 6))
            cell_map.setdefault(key, float(point["intensity"]))
        canonical_cells = [
            {"latitude": lat, "longitude": lon, "intensity": intensity}
            for (lat, lon), intensity in sorted(cell_map.items())
        ]
        grid_digest = hashlib.sha256(
            json.dumps(canonical_cells, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

        with transaction(immediate=True) as connection:
            record_digest = self._record_digest(connection)
            assessment_key = hashlib.sha256(
                "|".join(
                    [record_digest, grid_digest, str(intensity_threshold), str(resolved_step)]
                ).encode("utf-8")
            ).hexdigest()
            existing = connection.execute(
                "SELECT id FROM exposure_assessments WHERE assessment_key=?", (assessment_key,)
            ).fetchone()
            if existing is not None:
                assessment_id = existing["id"]
            else:
                now = _now()
                cursor = connection.execute(
                    "INSERT INTO exposure_assessments(assessment_key,computation_id,"
                    "intensity_threshold,grid_step_km,record_digest,grid_digest,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (assessment_key, computation_id, intensity_threshold, resolved_step,
                     record_digest, grid_digest, now),
                )
                assessment_id = cursor.lastrowid
                connection.executemany(
                    "INSERT INTO exposure_cell_intensity(assessment_id,lat_cell,lon_cell,intensity) "
                    "VALUES(?,?,?,?)",
                    [
                        (assessment_id, cell["latitude"], cell["longitude"], cell["intensity"])
                        for cell in canonical_cells
                    ],
                )
                records = connection.execute("SELECT id,latitude,longitude FROM exposure_records ORDER BY id").fetchall()
                risk_rows = []
                for record in records:
                    intensity = self._nearest_intensity(
                        record["latitude"], record["longitude"], canonical_cells, resolved_step
                    )
                    is_high = 1 if intensity is not None and intensity >= intensity_threshold else 0
                    risk_rows.append((assessment_id, record["id"], intensity, is_high))
                connection.executemany(
                    "INSERT INTO exposure_record_risks(assessment_id,record_id,intensity,is_high_risk) "
                    "VALUES(?,?,?,?)",
                    risk_rows,
                )
        return self.get_assessment(assessment_key)

    @staticmethod
    def _nearest_intensity(
        latitude: float,
        longitude: float,
        cells: list[dict[str, Any]],
        step_km: float,
    ) -> float | None:
        """把记录吸附到最近格网点；超过一个步长视为网格外（无烈度）。"""
        best: float | None = None
        best_distance = float("inf")
        lat_rad = math.radians(latitude)
        for cell in cells:
            dlat = (latitude - cell["latitude"]) * 111.0
            dlon = (longitude - cell["longitude"]) * 111.0 * max(0.2, math.cos(lat_rad))
            distance = math.hypot(dlat, dlon)
            if distance < best_distance:
                best_distance = distance
                best = cell["intensity"]
        return best if best is not None and best_distance <= step_km else None

    def _resolve_grid(
        self,
        computation_id: int | None,
        points: list[dict[str, Any]] | None,
        grid_step_km: float | None,
    ) -> tuple[list[dict[str, Any]], float]:
        if points:
            step = float(grid_step_km) if grid_step_km else 10.0
            if step <= 0:
                raise ValueError("grid_step_km 必须为正数")
            normalized = []
            for point in points:
                if not all(key in point for key in ("latitude", "longitude", "intensity")):
                    raise ValueError("烈度网格点必须包含 latitude、longitude、intensity")
                normalized.append(
                    {
                        "latitude": float(point["latitude"]),
                        "longitude": float(point["longitude"]),
                        "intensity": float(point["intensity"]),
                    }
                )
            return normalized, step
        if computation_id is None:
            raise ValueError("必须提供 computation_id 或烈度网格 points")
        task = self.connection.execute(
            "SELECT status,result_json,grid_step_km FROM seismic_computations WHERE id=?",
            (computation_id,),
        ).fetchone()
        if task is None:
            raise KeyError("computation_not_found")
        if task["status"] != "done":
            raise ValueError("引用的烈度计算尚未完成")
        result = json.loads(task["result_json"] or "{}")
        grid_points = result.get("points") or []
        if not grid_points:
            raise ValueError("烈度计算结果中没有网格点")
        return grid_points, float(task["grid_step_km"])

    def get_assessment(self, assessment_key: str) -> dict[str, Any]:
        assessment = self.connection.execute(
            "SELECT * FROM exposure_assessments WHERE assessment_key=?", (assessment_key,)
        ).fetchone()
        if assessment is None:
            raise KeyError("assessment_not_found")
        return {
            "assessment_key": assessment["assessment_key"],
            "computation_id": assessment["computation_id"],
            "intensity_threshold": assessment["intensity_threshold"],
            "grid_step_km": assessment["grid_step_km"],
            "record_digest": assessment["record_digest"],
            "grid_digest": assessment["grid_digest"],
            "created_at": assessment["created_at"],
            "divisions": self.division_summary(assessment["id"]),
            "quality": self.quality_stats(assessment["id"]),
        }

    def list_assessments(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT assessment_key,computation_id,intensity_threshold,grid_step_km,"
            "record_digest,grid_digest,created_at FROM exposure_assessments ORDER BY id"
        ).fetchall()
        return [dict(item) for item in rows]

    def division_summary(self, assessment_id: int) -> list[dict[str, Any]]:
        """稳定排序（分区编码升序）的高风险分区摘要。"""
        rows = self.connection.execute(
            """
            SELECT r.division_code AS division_code,
                   COUNT(*) AS total_records,
                   SUM(rr.is_high_risk) AS high_risk_records,
                   SUM(CASE WHEN r.record_type='building' THEN rr.is_high_risk ELSE 0 END) AS high_risk_buildings,
                   SUM(CASE WHEN r.record_type='facility' THEN rr.is_high_risk ELSE 0 END) AS high_risk_facilities,
                   SUM(CASE WHEN r.critical_level='critical' THEN rr.is_high_risk ELSE 0 END) AS high_risk_critical,
                   SUM(CASE WHEN r.critical_level IN ('key','critical') THEN rr.is_high_risk ELSE 0 END) AS high_risk_key_facilities,
                   SUM(CASE WHEN rr.is_high_risk=1 THEN r.population ELSE 0 END) AS high_risk_population
            FROM exposure_record_risks rr
            JOIN exposure_records r ON r.id = rr.record_id
            WHERE rr.assessment_id=?
            GROUP BY r.division_code
            ORDER BY r.division_code ASC
            """,
            (assessment_id,),
        ).fetchall()
        return [dict(item) for item in rows]

    def quality_stats(self, assessment_id: int) -> dict[str, Any]:
        assessment = self.connection.execute(
            "SELECT assessment_key FROM exposure_assessments WHERE id=?", (assessment_id,)
        ).fetchone()
        batches = self.connection.execute(
            "SELECT COALESCE(SUM(total_rows),0) AS total_rows,"
            "COALESCE(SUM(accepted_rows),0) AS accepted_rows,"
            "COALESCE(SUM(rejected_rows),0) AS rejected_rows,"
            "COALESCE(SUM(inserted_rows),0) AS inserted_rows,"
            "COALESCE(SUM(duplicate_rows),0) AS duplicate_rows,"
            "COUNT(*) AS batch_count FROM exposure_batches"
        ).fetchone()
        totals = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(is_high_risk) AS high_risk,
                   SUM(CASE WHEN intensity IS NULL THEN 1 ELSE 0 END) AS off_grid
            FROM exposure_record_risks WHERE assessment_id=?
            """,
            (assessment_id,),
        ).fetchone()
        accepted = batches["accepted_rows"]
        rejected = batches["rejected_rows"]
        total_in = batches["total_rows"]
        return {
            "batch_count": batches["batch_count"],
            "total_rows": total_in,
            "accepted_rows": accepted,
            "rejected_rows": rejected,
            "inserted_rows": batches["inserted_rows"],
            "duplicate_rows": batches["duplicate_rows"],
            "records_assessed": totals["total"],
            "high_risk_records": totals["high_risk"],
            "off_grid_records": totals["off_grid"],
            "accept_rate": round(accepted / total_in, 4) if total_in else 1.0,
        }

    # ------------------------------------------------------------------ 追溯
    def list_records(
        self,
        *,
        batch_id: str | None = None,
        division_code: str | None = None,
        assessment_key: str | None = None,
        high_risk_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """按批次/分区列出记录，可附带评估烈度与高风险标记，按分区与记录 id 稳定排序。"""
        sql = "SELECT r.*"
        params: list[Any] = []
        joins = ""
        where = []
        if assessment_key is not None:
            sql += ", rr.intensity AS matched_intensity, rr.is_high_risk AS is_high_risk"
            joins = (
                " JOIN exposure_record_risks rr ON rr.record_id=r.id"
                " JOIN exposure_assessments a ON a.id=rr.assessment_id"
            )
            where.append("a.assessment_key=?")
            params.append(assessment_key)
        else:
            sql += ", NULL AS matched_intensity, NULL AS is_high_risk"
        if batch_id is not None:
            where.append("r.source_batch_id=?")
            params.append(batch_id)
        if division_code is not None:
            where.append("r.division_code=?")
            params.append(division_code)
        if high_risk_only:
            if not assessment_key:
                raise ValueError("high_risk_only 需要指定 assessment_key")
            where.append("rr.is_high_risk=1")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql += " FROM exposure_records r" + joins + clause + " ORDER BY r.division_code ASC, r.id ASC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        rows = self.connection.execute(sql, params).fetchall()
        result = []
        for item in rows:
            record = dict(item)
            record["raw"] = json.loads(record.pop("raw_json") or "{}")
            result.append(record)
        return result

    def get_record(self, record_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM exposure_records WHERE id=?", (record_id,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["raw"] = json.loads(record.pop("raw_json") or "{}")
        return record
