from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.database import close_connection


def _record(district, feature, lat, lon, *, ext="", name="", population=0, updated_at=None):
    row = {
        "external_id": ext,
        "district_code": district,
        "name": name,
        "feature_type": feature,
        "population": population,
        "latitude": lat,
        "longitude": lon,
    }
    if updated_at is not None:
        row["updated_at"] = updated_at
    return row


BATCH_ROWS = [
    _record("510101", "hospital", 30.10, 103.20, ext="H1", name="县医院", population=320),
    _record("510101", "building", 30.10, 103.21, ext="B1", name="安居苑1栋", population=48),
    _record("510102", "school", 30.10, 103.40, ext="S1", name="镇小学", population=600),
    _record("510102", "building", 31.50, 105.00, ext="B2", name="远山民居", population=5),
    # 坏行：缺分区、类型非法、坐标越界、人口为负
    _record("", "building", 30.1, 103.2, ext="BAD1"),
    {"district_code": "510101", "feature_type": "stadium", "latitude": 30.1, "longitude": 103.2},
    {"district_code": "510101", "feature_type": "building", "latitude": 91.0, "longitude": 103.2},
    {"district_code": "510101", "feature_type": "building", "latitude": 30.1, "longitude": 200.0},
    {"district_code": "510101", "feature_type": "building", "latitude": 30.1, "longitude": 103.2, "population": -3},
]

GRID_CELLS = [
    {"latitude": 30.10, "longitude": 103.20, "intensity": 7.5},
    {"latitude": 30.10, "longitude": 103.40, "intensity": 5.0},
]


def _import(client, ref="b-2026-001", rows=None, received_at="2026-09-24T10:00:00+00:00"):
    payload = {"batch_ref": ref, "source": "住建清单", "received_at": received_at, "records": rows or BATCH_ROWS}
    return client.post("/api/seismic/exposure/batches", json=payload)


def test_import_accepts_good_rows_and_reports_bad_row_numbers(client):
    response = _import(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["accepted_count"] == 4
    assert body["rejected_count"] == 5
    assert body["duplicate_count"] == 0
    rejected = body["rejected"]
    assert [item["row"] for item in rejected] == [5, 6, 7, 8, 9]
    reasons = {item["row"]: item["reasons"] for item in rejected}
    assert any("district_code" in reason for reason in reasons[5])
    assert any("feature_type" in reason for reason in reasons[6])
    assert any("latitude" in reason for reason in reasons[7])
    assert any("longitude" in reason for reason in reasons[8])
    assert any("population" in reason for reason in reasons[9])
    # 坏行不影响已确认数据
    batch = client.get("/api/seismic/exposure/batches/b-2026-001").json()
    assert len(batch["records"]) == 4
    assert batch["source"] == "住建清单"
    assert batch["received_at"] == "2026-09-24T10:00:00+00:00"


def test_reimport_same_batch_is_idempotent_and_queries_are_stable(client):
    first = _import(client).json()
    second = _import(client).json()
    assert second["idempotent"] is True
    assert second["accepted_count"] == first["accepted_count"] == 4
    assert second["rejected_count"] == first["rejected_count"] == 5
    assert [item["row"] for item in second["rejected"]] == [5, 6, 7, 8, 9]

    client.put("/api/seismic/exposure/grids/default", json={"cells": GRID_CELLS})
    summary_one = client.get("/api/seismic/exposure/summary?intensity_min=6").json()
    summary_two = client.get("/api/seismic/exposure/summary?intensity_min=6").json()
    for body in (summary_one, summary_two):
        body.pop("generated_at")
    assert summary_one == summary_two
    # 分区摘要按分区编码稳定排序
    codes = [item["district_code"] for item in summary_one["districts"]]
    assert codes == sorted(codes) == ["510101", "510102"]


def test_threshold_summary_dedup_and_traceability(client):
    _import(client)
    # 第二批：同一医院更新人口；批内重复一行；同设施后续批次不应重复计数
    update_rows = [
        _record("510101", "hospital", 30.10, 103.20, ext="H1", name="县医院(扩建)", population=500,
                updated_at="2026-09-25T08:00:00+00:00"),
        _record("510101", "hospital", 30.10, 103.20, ext="H1", name="县医院(扩建)", population=500,
                updated_at="2026-09-25T08:00:00+00:00"),
    ]
    update = _import(client, ref="b-2026-002", rows=update_rows, received_at="2026-09-25T09:00:00+00:00")
    assert update.status_code == 201, update.text
    assert update.json()["accepted_count"] == 1
    assert update.json()["duplicate_count"] == 1

    client.put("/api/seismic/exposure/grids/default", json={"cells": GRID_CELLS})
    summary = client.get("/api/seismic/exposure/summary?intensity_min=6").json()

    district_a = next(item for item in summary["districts"] if item["district_code"] == "510101")
    # 只有最新版本的 H1 被计数：医院 1 家、建筑 1 栋、人口 500+48
    assert district_a["facility_count"] == 1
    assert district_a["building_count"] == 1
    assert district_a["population_total"] == 548
    high = district_a["high_risk"]
    assert high["facility_count"] == 1
    assert high["building_count"] == 1
    assert high["population_total"] == 548
    assert high["facilities_by_type"]["hospital"] == 1

    district_b = next(item for item in summary["districts"] if item["district_code"] == "510102")
    # 学校位于 5.0 度格网，阈值 6 时不属于高风险
    assert district_b["high_risk"]["facility_count"] == 0
    assert district_b["facility_count"] == 1

    # 高风险明细稳定排序且可追溯到来源批次和原行号
    keys = [(item["district_code"], item["record_key"]) for item in summary["high_risk_records"]]
    assert keys == sorted(keys)
    hospital = next(item for item in summary["high_risk_records"] if item["external_id"] == "H1")
    assert hospital["batch_ref"] == "b-2026-002"
    assert hospital["source_row"] == 1
    assert hospital["intensity"] == 7.5

    # 原始记录可追溯：两个批次都保留
    history = client.get(f"/api/seismic/exposure/records/{hospital['record_key']}/history").json()
    versions = history["versions"]
    assert [item["batch_ref"] for item in versions] == ["b-2026-001", "b-2026-002"]
    assert json.loads(versions[0]["raw_json"])["population"] == 320
    assert json.loads(versions[1]["raw_json"])["population"] == 500

    quality = summary["quality"]
    assert quality["batch_count"] == 2
    assert quality["stored_record_count"] == 5  # 4 + 1，第二批重复行未入库
    assert quality["current_record_count"] == 4  # 跨批次去重后当前有效设施为 4 个
    assert quality["rejected_row_count"] == 5
    assert quality["duplicate_row_count"] == 1
    assert quality["grid_matched_count"] == 3
    assert quality["grid_unmatched_count"] == 1
    assert quality["rejections"][0]["batch_ref"] == "b-2026-001"
    assert quality["rejections"][0]["row"] == 5


def test_grid_replace_is_scoped_and_invalid_cell_rejected(client):
    bad = client.put("/api/seismic/exposure/grids/event-x", json={
        "scope": "event-x",
        "cells": [{"latitude": 999, "longitude": 1, "intensity": 7}],
    })
    # pydantic 在边界拦截
    assert bad.status_code == 422

    ok = client.put("/api/seismic/exposure/grids/event-x", json={"cells": GRID_CELLS})
    assert ok.status_code == 200
    assert ok.json()["scope"] == "event-x"
    _import(client)
    # 默认范围无格网：全部未匹配，也没有高风险
    empty = client.get("/api/seismic/exposure/summary?intensity_min=6&scope=default").json()
    assert empty["quality"]["grid_matched_count"] == 0
    assert empty["totals"]["high_risk_building_count"] == 0
    # 具名范围有格网
    scoped = client.get("/api/seismic/exposure/summary?intensity_min=6&scope=event-x").json()
    assert scoped["quality"]["grid_matched_count"] == 3


def test_district_filter_and_tolerance(client):
    _import(client)
    client.put("/api/seismic/exposure/grids/default", json={"cells": GRID_CELLS})
    only_a = client.get("/api/seismic/exposure/summary?intensity_min=6&district_code=510101").json()
    assert [item["district_code"] for item in only_a["districts"]] == ["510101"]
    assert only_a["totals"]["population_total"] == 368


def test_load_grid_from_finished_computation(client):
    created = client.post("/api/seismic/events", json={
        "external_id": "EQ-GRID-1",
        "origin_time": "2026-09-24T12:00:00+00:00",
        "latitude": 30.1,
        "longitude": 103.2,
        "depth_km": 12.0,
        "magnitude": 5.8,
        "magnitude_type": "ML",
        "source": "test",
    })
    event_id = created.json()["id"]
    client.post(f"/api/seismic/events/{event_id}/observations", json={
        "station_code": "SC01", "channel": "HNZ", "observed_at": "2026-09-24T12:00:03+00:00",
        "pga": 0.8, "distance_km": 18,
    })
    task = client.post(f"/api/seismic/events/{event_id}/computations",
                       json={"model_version": "test-1", "grid_step_km": 20, "radius_km": 20}).json()
    claim = client.post("/api/seismic/computations/claim?worker_id=w1").json()["task"]
    client.post(f"/api/seismic/computations/{claim['id']}/calculate?worker_id=w1")

    response = client.post(f"/api/seismic/exposure/computations/{task['id']}/grid?scope=eq-grid-1")
    assert response.status_code == 201, response.text
    assert response.json()["cell_count"] >= 1

    missing = client.post("/api/seismic/exposure/computations/9999/grid")
    assert missing.status_code == 404


def test_cli_import_csv_and_summary(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("TOWNSHIP_DATABASE_PATH", str(db_path))
    close_connection()

    csv_file = tmp_path / "exposure.csv"
    csv_file.write_text(
        "external_id,district_code,name,feature_type,population,latitude,longitude,updated_at\n"
        "H9,510101,县医院,hospital,100,30.10,103.20,2026-09-24T10:00:00+00:00\n"
        "BAD,510101,坏点,building,1,999,103.20,\n",
        encoding="utf-8",
    )
    grid_file = tmp_path / "grid.json"
    grid_file.write_text(json.dumps({"cells": GRID_CELLS}), encoding="utf-8")

    from app.cli import main

    rc = main(["exposure-import", "--file", str(csv_file), "--format", "csv", "--batch-ref", "csv-1"])
    assert rc == 0
    rc = main(["exposure-grid", "--file", str(grid_file)])
    assert rc == 0
    rc = main(["exposure-summary", "--intensity-min", "6"])
    assert rc == 0

    from app.seismic.exposure.service import ExposureService

    report = ExposureService().get_batch("csv-1")
    assert report["accepted_count"] == 1
    assert report["rejected_count"] == 1
    summary = ExposureService().summarize(6.0)
    assert summary["totals"]["high_risk_facility_count"] == 1
    assert summary["districts"][0]["district_code"] == "510101"
    close_connection()


def test_nonexistent_batch_and_record(client):
    assert client.get("/api/seismic/exposure/batches/nope").status_code == 404
    assert client.get("/api/seismic/exposure/records/ext:nope/history").status_code == 404
