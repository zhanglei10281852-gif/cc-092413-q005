from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.core.clock import to_storage, utc_now
from app.database import get_connection, transaction

FEATURE_TYPES = ("building", "hospital", "school", "government", "shelter", "lifeline", "other")
FACILITY_TYPES = tuple(value for value in FEATURE_TYPES if value != "building")
DEFAULT_SCOPE = "default"
MAX_STRING = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS exposure_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_ref TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    input_digest TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS exposure_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES exposure_batches(id) ON DELETE RESTRICT,
    source_row INTEGER NOT NULL,
    record_key TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    district_code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    feature_type TEXT NOT NULL,
    population INTEGER NOT NULL DEFAULT 0,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, source_row)
);
CREATE INDEX IF NOT EXISTS idx_exposure_records_key ON exposure_records(record_key);
CREATE INDEX IF NOT EXISTS idx_exposure_records_district ON exposure_records(district_code);
CREATE TABLE IF NOT EXISTS exposure_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES exposure_batches(id) ON DELETE CASCADE,
    source_row INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exposure_rejections_batch ON exposure_rejections(batch_id, source_row);
CREATE TABLE IF NOT EXISTS intensity_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL DEFAULT 'default',
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    intensity REAL NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope, latitude, longitude)
);
CREATE INDEX IF NOT EXISTS idx_intensity_cells_scope ON intensity_cells(scope);
"""

RANKED_RECORDS_SQL = """
WITH ranked AS (
    SELECT
        r.id, r.batch_id, b.batch_ref, b.received_at AS batch_received_at,
        b.imported_at AS batch_imported_at, r.source_row, r.record_key,
        r.external_id, r.district_code, r.name, r.feature_type, r.population,
        r.latitude, r.longitude, r.updated_at, r.raw_json,
        ROW_NUMBER() OVER (
            PARTITION BY r.record_key
            ORDER BY COALESCE(NULLIF(r.updated_at, ''), NULLIF(b.received_at, ''), b.imported_at) DESC,
                     r.batch_id DESC, r.id DESC
        ) AS rn
    FROM exposure_records r
    JOIN exposure_batches b ON b.id = r.batch_id
)
SELECT * FROM ranked WHERE rn = 1 ORDER BY district_code, record_key
"""


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _now() -> str:
    return to_storage(utc_now())


def _parse_timestamp(value: Any, field: str) -> tuple[str | None, str | None]:
    """返回 (规范化时间, 错误原因)；空值合法。"""
    if value is None or value == "":
        return "", None
    if not isinstance(value, str):
        return None, f"{field} 必须是 ISO-8601 时间字符串"
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None, f"{field} 时间格式无法解析"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds"), None


def _as_float(value: Any, field: str) -> tuple[float | None, str | None]:
    if isinstance(value, bool) or value is None or value == "":
        return None, f"缺少 {field}" if value is None or value == "" else (None, f"{field} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"{field} 必须是数字"
    if math.isnan(number) or math.isinf(number):
        return None, f"{field} 必须是有限数字"
    return number, None


def _as_int(value: Any, field: str, *, required: bool = True, default: int = 0) -> tuple[int | None, str | None]:
    if value is None or value == "":
        if required:
            return None, f"缺少 {field}"
        return default, None
    if isinstance(value, bool):
        return None, f"{field} 必须是非负整数"
    if isinstance(value, int):
        return value, None
    if isinstance(value, float) and value.is_integer():
        return int(value), None
    if isinstance(value, str):
        try:
            return int(value.strip()), None
        except ValueError:
            return None, f"{field} 必须是非负整数"
    return None, f"{field} 必须是非负整数"


def _clean_text(value: Any, field: str, *, required: bool, max_length: int = MAX_STRING) -> tuple[str, str | None]:
    if value is None:
        return ("", f"缺少 {field}" if required else "")
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return "", f"{field} 必须是字符串"
    text = str(value).strip() if not isinstance(value, str) else value.strip()
    if required and not text:
        return "", f"缺少 {field}"
    if len(text) > max_length:
        return "", f"{field} 长度超过 {max_length}"
    return text, None


def validate_record(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """校验单行记录，返回 (规范化记录, 错误原因列表)。"""
    reasons: list[str] = []

    district_code, error = _clean_text(raw.get("district_code"), "district_code", required=True, max_length=20)
    if error:
        reasons.append(error)

    name, error = _clean_text(raw.get("name"), "name", required=False)
    if error:
        reasons.append(error)

    feature_type, error = _clean_text(raw.get("feature_type"), "feature_type", required=True, max_length=24)
    if error:
        reasons.append(error)
    elif feature_type not in FEATURE_TYPES:
        reasons.append(f"feature_type 必须是 {','.join(FEATURE_TYPES)} 之一")

    external_id, error = _clean_text(raw.get("external_id"), "external_id", required=False, max_length=64)
    if error:
        reasons.append(error)

    latitude = longitude = None
    lat_value, error = _as_float(raw.get("latitude"), "latitude")
    if error:
        reasons.append(error)
    else:
        latitude = lat_value
        if not -90 <= latitude <= 90:
            reasons.append("latitude 超出 [-90, 90] 范围")
    lon_value, error = _as_float(raw.get("longitude"), "longitude")
    if error:
        reasons.append(error)
    else:
        longitude = lon_value
        if not -180 <= longitude <= 180:
            reasons.append("longitude 超出 [-180, 180] 范围")

    population, error = _as_int(raw.get("population"), "population", required=False)
    if error:
        reasons.append(error)
    elif population < 0:
        reasons.append("population 不能为负数")

    updated_at, error = _parse_timestamp(raw.get("updated_at"), "updated_at")
    if error:
        reasons.append(error)

    if reasons:
        return None, reasons

    normalized = {
        "external_id": external_id,
        "district_code": district_code,
        "name": name,
        "feature_type": feature_type,
        "population": population,
        "latitude": latitude,
        "longitude": longitude,
        "updated_at": updated_at,
    }
    return normalized, []


def _record_key(record: dict[str, Any]) -> str:
    if record["external_id"]:
        return "ext:" + record["external_id"]
    fingerprint = json.dumps(
        [record["feature_type"], record["district_code"], record["name"], round(record["latitude"], 6), round(record["longitude"], 6)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "fp:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:32]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


class ExposureService:
    """暴露记录导入、去重和按烈度阈值的分区汇总。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    # ------------------------------------------------------------------ 导入

    def import_batch(
        self,
        batch_ref: str,
        rows: list[dict[str, Any]],
        *,
        received_at: str | None = None,
        source: str = "",
    ) -> dict[str, Any]:
        """导入一个来源批次。坏行被拒绝并记录行号，不影响同批已确认行。"""
        if not isinstance(rows, list) or not rows:
            raise ValueError("records 不能为空")
        received_value, error = _parse_timestamp(received_at, "received_at")
        if error:
            raise ValueError(error)
        source = (source or "").strip()[:80]

        input_digest = hashlib.sha256(
            json.dumps({"received_at": received_value, "rows": rows}, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM exposure_batches WHERE batch_ref=?", (batch_ref,)).fetchone()
            if existing is not None:
                return self._batch_report(connection, dict(existing), idempotent=True)

            now = _now()
            cursor = connection.execute(
                "INSERT INTO exposure_batches(batch_ref,source,received_at,imported_at,input_digest) VALUES(?,?,?,?,?)",
                (batch_ref, source, received_value, now, input_digest),
            )
            batch_id = cursor.lastrowid

            accepted: list[dict[str, Any]] = []
            rejections: list[dict[str, Any]] = []
            duplicates: list[dict[str, Any]] = []
            seen_keys: dict[str, int] = {}

            for index, raw in enumerate(rows, start=1):
                raw = raw if isinstance(raw, dict) else {}
                normalized, reasons = validate_record(raw)
                if reasons:
                    rejections.append({"row": index, "reasons": reasons})
                    continue
                key = _record_key(normalized)
                if key in seen_keys:
                    duplicates.append({"row": index, "record_key": key, "first_row": seen_keys[key]})
                    continue
                seen_keys[key] = index
                connection.execute(
                    "INSERT INTO exposure_records"
                    "(batch_id,source_row,record_key,external_id,district_code,name,feature_type,population,latitude,longitude,updated_at,raw_json,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        index,
                        key,
                        normalized["external_id"],
                        normalized["district_code"],
                        normalized["name"],
                        normalized["feature_type"],
                        normalized["population"],
                        normalized["latitude"],
                        normalized["longitude"],
                        normalized["updated_at"],
                        json.dumps(raw, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                accepted.append({"row": index, "record_key": key})

            connection.execute(
                "UPDATE exposure_batches SET accepted_count=?, rejected_count=?, duplicate_count=? WHERE id=?",
                (len(accepted), len(rejections), len(duplicates), batch_id),
            )
            for item in rejections:
                connection.execute(
                    "INSERT INTO exposure_rejections(batch_id,source_row,reasons_json,raw_json) VALUES(?,?,?,?)",
                    (batch_id, item["row"], json.dumps(item["reasons"], ensure_ascii=False), json.dumps(rows[item["row"] - 1], ensure_ascii=False, sort_keys=True)),
                )

            batch = connection.execute("SELECT * FROM exposure_batches WHERE id=?", (batch_id,)).fetchone()
            report = self._batch_report(connection, dict(batch), idempotent=False)
            report["accepted"] = accepted
            report["rejected"] = rejections
            report["duplicates"] = duplicates
            return report

    def _batch_report(self, connection: sqlite3.Connection, batch: dict[str, Any], *, idempotent: bool) -> dict[str, Any]:
        rejection_rows = connection.execute(
            "SELECT source_row, reasons_json FROM exposure_rejections WHERE batch_id=? ORDER BY source_row",
            (batch["id"],),
        ).fetchall()
        report = {
            "batch_ref": batch["batch_ref"],
            "source": batch["source"],
            "received_at": batch["received_at"],
            "imported_at": batch["imported_at"],
            "accepted_count": batch["accepted_count"],
            "rejected_count": batch["rejected_count"],
            "duplicate_count": batch["duplicate_count"],
            "idempotent": idempotent,
        }
        if idempotent:
            report["rejected"] = [
                {"row": row["source_row"], "reasons": json.loads(row["reasons_json"])} for row in rejection_rows
            ]
        return report

    def list_batches(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT batch_ref,source,received_at,imported_at,accepted_count,rejected_count,duplicate_count"
            " FROM exposure_batches ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_batch(self, batch_ref: str) -> dict[str, Any] | None:
        connection = self.connection
        batch = connection.execute("SELECT * FROM exposure_batches WHERE batch_ref=?", (batch_ref,)).fetchone()
        if batch is None:
            return None
        report = self._batch_report(connection, dict(batch), idempotent=False)
        report["records"] = [
            dict(row)
            for row in connection.execute(
                "SELECT source_row,record_key,external_id,district_code,name,feature_type,population,latitude,longitude,updated_at"
                " FROM exposure_records WHERE batch_id=? ORDER BY source_row",
                (batch["id"],),
            ).fetchall()
        ]
        return report

    def get_record_history(self, record_key: str) -> list[dict[str, Any]]:
        """返回同一设施跨批次的全部原始记录，按导入顺序稳定排序，可追溯。"""
        rows = self.connection.execute(
            "SELECT b.batch_ref,b.source,r.source_row,r.district_code,r.name,r.feature_type,r.population,"
            "r.latitude,r.longitude,r.updated_at,r.raw_json,r.created_at"
            " FROM exposure_records r JOIN exposure_batches b ON b.id=r.batch_id"
            " WHERE r.record_key=? ORDER BY r.batch_id,r.source_row",
            (record_key,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 格网

    def replace_intensity_grid(
        self, cells: list[dict[str, Any]], *, scope: str = DEFAULT_SCOPE, updated_at: str | None = None
    ) -> dict[str, Any]:
        if not cells:
            raise ValueError("cells 不能为空")
        cleaned: list[tuple[float, float, float]] = []
        for index, cell in enumerate(cells, start=1):
            lat, lat_error = _as_float(cell.get("latitude"), "latitude")
            lon, lon_error = _as_float(cell.get("longitude"), "longitude")
            intensity, intensity_error = _as_float(cell.get("intensity"), "intensity")
            reasons = [message for message in (lat_error, lon_error, intensity_error) if message]
            if not reasons:
                if not -90 <= lat <= 90:
                    reasons.append(f"第 {index} 行 latitude 超出范围")
                if not -180 <= lon <= 180:
                    reasons.append(f"第 {index} 行 longitude 超出范围")
            if reasons:
                raise ValueError(f"格网第 {index} 行无效: {'；'.join(reasons)}")
            cleaned.append((round(lat, 6), round(lon, 6), round(intensity, 3)))
        stamp, error = _parse_timestamp(updated_at, "updated_at")
        if error:
            raise ValueError(error)
        stamp = stamp or _now()

        with transaction(immediate=True) as connection:
            connection.execute("DELETE FROM intensity_cells WHERE scope=?", (scope,))
            connection.executemany(
                "INSERT INTO intensity_cells(scope,latitude,longitude,intensity,updated_at) VALUES(?,?,?,?,?)",
                [(scope, lat, lon, intensity, stamp) for lat, lon, intensity in sorted(set(cleaned))],
            )
        return {"scope": scope, "cell_count": len(set(cleaned)), "updated_at": stamp}

    def load_grid_from_computation(self, task_id: int, *, scope: str = DEFAULT_SCOPE) -> dict[str, Any]:
        task = self.connection.execute("SELECT * FROM seismic_computations WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError("computation_not_found")
        if task["status"] != "done":
            raise RuntimeError("computation_not_done")
        result = json.loads(task["result_json"] or "{}")
        points = result.get("points", [])
        cells = [
            {"latitude": point["latitude"], "longitude": point["longitude"], "intensity": point["intensity"]}
            for point in points
        ]
        return self.replace_intensity_grid(cells, scope=scope)

    def _cells(self, scope: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT latitude,longitude,intensity FROM intensity_cells WHERE scope=? ORDER BY latitude,longitude",
                (scope,),
            ).fetchall()
        ]

    # ------------------------------------------------------------------ 汇总

    def summarize(
        self,
        intensity_min: float,
        *,
        scope: str = DEFAULT_SCOPE,
        district_code: str | None = None,
        tolerance_km: float = 11.0,
    ) -> dict[str, Any]:
        cells = self._cells(scope)
        rows = self.connection.execute(RANKED_RECORDS_SQL).fetchall()
        if district_code:
            rows = [row for row in rows if row["district_code"] == district_code]

        high_risk: list[dict[str, Any]] = []
        districts: dict[str, dict[str, Any]] = {}
        matched_total = 0

        for row in rows:
            record = dict(row)
            match = self._match_cell(record["latitude"], record["longitude"], cells, tolerance_km)
            if match is not None:
                matched_total += 1
            record["intensity"] = match["intensity"] if match else None
            record["distance_km"] = round(match["distance_km"], 3) if match else None

            summary = districts.setdefault(
                record["district_code"],
                {
                    "district_code": record["district_code"],
                    "building_count": 0,
                    "facility_count": 0,
                    "population_total": 0,
                    "high_risk": {
                        "building_count": 0,
                        "facility_count": 0,
                        "population_total": 0,
                        "facilities_by_type": {feature: 0 for feature in FACILITY_TYPES},
                        "record_keys": [],
                    },
                },
            )
            is_facility = record["feature_type"] != "building"
            summary["building_count"] += 0 if is_facility else 1
            summary["facility_count"] += 1 if is_facility else 0
            summary["population_total"] += record["population"]

            if match is not None and match["intensity"] >= intensity_min:
                risk = summary["high_risk"]
                risk["building_count"] += 0 if is_facility else 1
                risk["facility_count"] += 1 if is_facility else 0
                risk["population_total"] += record["population"]
                if is_facility:
                    risk["facilities_by_type"][record["feature_type"]] += 1
                risk["record_keys"].append(record["record_key"])
                high_risk.append(
                    {
                        "record_key": record["record_key"],
                        "external_id": record["external_id"],
                        "district_code": record["district_code"],
                        "name": record["name"],
                        "feature_type": record["feature_type"],
                        "population": record["population"],
                        "latitude": record["latitude"],
                        "longitude": record["longitude"],
                        "intensity": match["intensity"],
                        "distance_km": round(match["distance_km"], 3),
                        "matched_cell": {"latitude": match["latitude"], "longitude": match["longitude"]},
                        "batch_ref": record["batch_ref"],
                        "source_row": record["source_row"],
                        "updated_at": record["updated_at"],
                    }
                )

        district_list = sorted(districts.values(), key=lambda item: item["district_code"])
        for summary in district_list:
            summary["high_risk"].pop("record_keys")

        totals = {
            "district_count": len(district_list),
            "building_count": sum(item["building_count"] for item in district_list),
            "facility_count": sum(item["facility_count"] for item in district_list),
            "population_total": sum(item["population_total"] for item in district_list),
            "high_risk_building_count": sum(item["high_risk"]["building_count"] for item in district_list),
            "high_risk_facility_count": sum(item["high_risk"]["facility_count"] for item in district_list),
            "high_risk_population": sum(item["high_risk"]["population_total"] for item in district_list),
        }

        return {
            "scope": scope,
            "intensity_min": intensity_min,
            "tolerance_km": tolerance_km,
            "generated_at": _now(),
            "grid_cell_count": len(cells),
            "districts": district_list,
            "totals": totals,
            "high_risk_records": sorted(
                high_risk, key=lambda item: (item["district_code"], item["record_key"])
            ),
            "quality": self._quality_stats(len(rows), matched_total, scope),
        }

    @staticmethod
    def _match_cell(latitude: float, longitude: float, cells: list[dict[str, Any]], tolerance_km: float):
        best = None
        for cell in cells:
            distance = _haversine_km(latitude, longitude, cell["latitude"], cell["longitude"])
            if best is None or distance < best["distance_km"]:
                best = {
                    "latitude": cell["latitude"],
                    "longitude": cell["longitude"],
                    "intensity": cell["intensity"],
                    "distance_km": distance,
                }
        if best is not None and best["distance_km"] <= tolerance_km:
            return best
        return None

    def _quality_stats(self, current_count: int, matched_total: int, scope: str) -> dict[str, Any]:
        connection = self.connection
        totals = connection.execute(
            "SELECT COALESCE(SUM(accepted_count),0) AS accepted,"
            " COALESCE(SUM(rejected_count),0) AS rejected,"
            " COALESCE(SUM(duplicate_count),0) AS duplicated,"
            " COUNT(*) AS batches FROM exposure_batches"
        ).fetchone()
        rejection_rows = connection.execute(
            "SELECT b.batch_ref, e.source_row, e.reasons_json"
            " FROM exposure_rejections e JOIN exposure_batches b ON b.id=e.batch_id"
            " ORDER BY b.id, e.source_row"
        ).fetchall()
        reasons_count: dict[str, int] = {}
        for row in rejection_rows:
            for reason in json.loads(row["reasons_json"]):
                reasons_count[reason] = reasons_count.get(reason, 0) + 1
        batches = [
            dict(row)
            for row in connection.execute(
                "SELECT batch_ref,source,received_at,imported_at,accepted_count,rejected_count,duplicate_count"
                " FROM exposure_batches ORDER BY id"
            ).fetchall()
        ]
        return {
            "scope": scope,
            "batch_count": totals["batches"],
            "stored_record_count": totals["accepted"],
            "current_record_count": current_count,
            "rejected_row_count": totals["rejected"],
            "duplicate_row_count": totals["duplicated"],
            "grid_matched_count": matched_total,
            "grid_unmatched_count": current_count - matched_total,
            "rejected_by_reason": dict(sorted(reasons_count.items(), key=lambda item: item[0])),
            "rejections": [
                {"batch_ref": row["batch_ref"], "row": row["source_row"], "reasons": json.loads(row["reasons_json"])}
                for row in rejection_rows
            ],
            "batches": batches,
        }
