from __future__ import annotations


def record(name, division, lat, lon, **extra):
    payload = {
        "record_key": name,
        "record_type": "building",
        "name": name,
        "division_code": division,
        "latitude": lat,
        "longitude": lon,
        "population": 100,
    }
    payload.update(extra)
    return payload


BATCH = [
    record("第一小学", "510121001", 30.10, 103.20, record_type="facility", critical_level="critical", population=0),
    record("安居楼1号", "510121001", 30.11, 103.21, population=320),
    record("镇卫生院", "510121002", 30.50, 103.60, record_type="facility", critical_level="key", population=0),
    record("安居楼1号", "510121001", 30.11, 103.21, population=320),  # 批次内重复
    {"record_key": "bad-lat", "record_type": "building", "name": "坏坐标", "division_code": "510121001", "latitude": 999, "longitude": 103.2},
    {"record_key": "bad-type", "record_type": "bridge", "name": "坏类型", "division_code": "510121001", "latitude": 30.1, "longitude": 103.2},
    "not-an-object",
]


def grid_points():
    # 与记录坐标对齐的 10km 网格：510121001 为高烈度，510121002 为低烈度。
    return [
        {"latitude": 30.1, "longitude": 103.2, "intensity": 8.2},
        {"latitude": 30.5, "longitude": 103.6, "intensity": 5.1},
    ]


def _import(client, batch_id="batch-1", rows=None):
    return client.post(
        "/api/exposure/imports",
        json={"batch_id": batch_id, "source": "test", "records": rows if rows is not None else BATCH},
    )


def test_import_accepts_valid_rows_and_reports_bad_line_numbers(client):
    response = _import(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["total_rows"] == 7
    assert body["accepted_rows"] == 4          # 含批次内重复行的去重前合法行
    assert body["rejected_rows"] == 3
    assert body["inserted_rows"] == 3          # 批次内重复只插入一次
    assert body["duplicate_rows"] == 1
    assert [error["line_number"] for error in body["errors"]] == [5, 6, 7]
    assert body["errors"][0]["record_key"] == "bad-lat"
    # 坏行不影响已确认数据
    listing = client.get("/api/exposure/records?division_code=510121001")
    assert listing.status_code == 200
    assert listing.json()["count"] == 2


def test_same_batch_replay_is_idempotent(client):
    first = _import(client).json()
    second = _import(client).json()
    assert second["replayed"] is True
    assert second["content_digest"] == first["content_digest"]
    assert second["inserted_rows"] == first["inserted_rows"]
    assert second["errors"] == first["errors"]
    records = client.get("/api/exposure/records").json()
    assert records["count"] == 3


def test_same_facility_from_two_batches_is_counted_once(client):
    _import(client, "batch-a", [record("跨批设施", "510121001", 30.2, 103.2, record_type="facility")])
    again = _import(client, "batch-b", [record("跨批设施", "510121001", 30.2, 103.2, record_type="facility")])
    assert again.status_code == 201
    body = again.json()
    assert body["inserted_rows"] == 0
    assert body["duplicate_rows"] == 1
    records = client.get("/api/exposure/records").json()["records"]
    assert len(records) == 1
    # 最近批次与更新时间被保留，首次来源批次仍可追溯
    assert records[0]["source_batch_id"] == "batch-b"
    assert records[0]["first_seen_batch_id"] == "batch-a"
    assert records[0]["updated_at"]


def test_repeated_import_then_assess_is_stable(client):
    _import(client)
    def assess():
        return client.post(
            "/api/exposure/assessments",
            json={"intensity_threshold": 6.0, "points": grid_points(), "grid_step_km": 10},
        ).json()
    first = assess()
    _import(client)  # 同批次重放，数据集不变
    second = assess()
    assert second["assessment_key"] == first["assessment_key"]
    assert second["divisions"] == first["divisions"]


def test_division_summary_is_sorted_and_filtered_by_threshold(client):
    _import(client, "div-batch", [
        record("甲校", "510121003", 30.10, 103.20, record_type="facility", critical_level="critical"),
        record("甲楼", "510121001", 30.10, 103.20, population=300),
        record("乙卫生院", "510121002", 30.50, 103.60, record_type="facility", critical_level="key"),
    ])
    body = client.post(
        "/api/exposure/assessments",
        json={"intensity_threshold": 6.0, "points": grid_points(), "grid_step_km": 10},
    ).json()
    codes = [item["division_code"] for item in body["divisions"]]
    assert codes == sorted(codes)
    by_code = {item["division_code"]: item for item in body["divisions"]}
    high = by_code["510121001"]
    assert high["total_records"] == 1
    assert high["high_risk_records"] == 1
    assert high["high_risk_buildings"] == 1
    assert high["high_risk_population"] == 300
    critical = by_code["510121003"]
    assert critical["high_risk_facilities"] == 1
    assert critical["high_risk_critical"] == 1
    low = by_code["510121002"]
    assert low["high_risk_records"] == 0
    quality = body["quality"]
    assert quality["accepted_rows"] == 3
    assert quality["rejected_rows"] == 0
    assert quality["high_risk_records"] == 2


def test_records_can_be_traced_to_source_and_risk(client):
    imported = _import(client, "trace-batch").json()
    assert imported["batch_id"] == "trace-batch"
    body = client.post(
        "/api/exposure/assessments",
        json={"intensity_threshold": 6.0, "points": grid_points(), "grid_step_km": 10},
    ).json()
    high = client.get(
        f"/api/exposure/records?assessment_key={body['assessment_key']}&high_risk_only=true"
    ).json()["records"]
    assert {item["name"] for item in high} == {"第一小学", "安居楼1号"}
    for item in high:
        assert item["matched_intensity"] >= 6.0
        assert item["is_high_risk"] == 1
        assert item["first_seen_batch_id"] == "trace-batch"
        assert item["raw"]["name"]  # 原始记录可追溯
    # 单条记录回溯
    record_id = high[0]["id"]
    detail = client.get(f"/api/exposure/records/{record_id}")
    assert detail.status_code == 200
    assert detail.json()["source_batch_id"] == "trace-batch"


def test_off_grid_record_is_flagged_in_quality(client):
    _import(client, "offgrid", [record("偏远楼", "510121009", 40.0, 120.0, population=10)])
    body = client.post(
        "/api/exposure/assessments",
        json={"intensity_threshold": 6.0, "points": grid_points(), "grid_step_km": 10},
    ).json()
    assert body["quality"]["off_grid_records"] == 1
    assert body["divisions"][0]["high_risk_records"] == 0


def test_import_batch_is_retrievable(client):
    _import(client, "lookup-batch")
    response = client.get("/api/exposure/imports/lookup-batch")
    assert response.status_code == 200
    assert response.json()["batch_id"] == "lookup-batch"
    assert client.get("/api/exposure/imports/missing").status_code == 404


def test_coordinate_bounds_can_be_overridden(client):
    response = client.post(
        "/api/exposure/imports",
        json={
            "batch_id": "overseas",
            "bounds": {"lat_min": -45, "lat_max": -10, "lon_min": 160, "lon_max": 180},
            "records": [record("海外点", "NZ-AKL", -36.85, 174.76)],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["inserted_rows"] == 1


def test_assessment_requires_grid(client):
    response = client.post("/api/exposure/assessments", json={"intensity_threshold": 6.0})
    assert response.status_code == 400
