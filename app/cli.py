from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.exposure.service import ExposureService, ensure_schema as ensure_exposure_schema
from app.main import app
from app.seismic.service import ensure_schema as ensure_seismic_schema


def command_init() -> int:
    init_db()
    ensure_seismic_schema()
    ensure_exposure_schema()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    ensure_seismic_schema()
    ensure_exposure_schema()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


# --------------------------------------------------------------------- 暴露清单
_INT_FIELDS = {"population"}
_FLOAT_FIELDS = {"latitude", "longitude"}


def _coerce_csv_row(row: dict[str, str]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in row.items():
        if key is None:
            continue
        key = key.strip()
        value = (value or "").strip()
        if key in _INT_FIELDS:
            try:
                clean[key] = int(value)
            except ValueError:
                clean[key] = value
        elif key in _FLOAT_FIELDS:
            try:
                clean[key] = float(value)
            except ValueError:
                clean[key] = value
        else:
            clean[key] = value
    return clean


def _load_exposure_file(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload, {}
        return list(payload.get("records", [])), {
            key: payload[key]
            for key in ("batch_id", "source", "updated_at", "bounds")
            if key in payload
        }
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [_coerce_csv_row(row) for row in csv.DictReader(handle)], {}
    raise SystemExit(f"不支持的文件类型：{suffix}（仅支持 .json/.csv）")


def command_exposure_import(args: argparse.Namespace) -> int:
    init_db()
    ensure_exposure_schema()
    records, file_options = _load_exposure_file(Path(args.file))
    batch_id = args.batch_id or file_options.get("batch_id")
    if not batch_id:
        raise SystemExit("必须通过 --batch-id 或文件中的 batch_id 指定来源批次")
    result = ExposureService().import_records(
        records,
        batch_id=batch_id,
        source=args.source or file_options.get("source", "cli"),
        updated_at=args.updated_at or file_options.get("updated_at"),
        bounds=file_options.get("bounds"),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_exposure_assess(args: argparse.Namespace) -> int:
    init_db()
    ensure_exposure_schema()
    points = None
    step = args.grid_step_km
    if args.grid:
        payload = json.loads(Path(args.grid).read_text(encoding="utf-8"))
        points = payload["points"] if isinstance(payload, dict) else payload
    result = ExposureService().assess(
        intensity_threshold=args.threshold,
        computation_id=args.computation_id,
        points=points,
        grid_step_km=step,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_exposure_query(args: argparse.Namespace) -> int:
    init_db()
    ensure_exposure_schema()
    service = ExposureService()

    if args.batch:
        result = service.get_batch(args.batch)
        if result is None:
            raise SystemExit("导入批次不存在")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    assessment_key = args.assessment
    if assessment_key == "latest" or (args.high_risk and not assessment_key):
        assessments = service.list_assessments()
        if not assessments:
            raise SystemExit("尚无评估结果")
        assessment_key = assessments[-1]["assessment_key"]

    if assessment_key:
        if args.division or args.high_risk:
            records = service.list_records(
                division_code=args.division,
                assessment_key=assessment_key,
                high_risk_only=args.high_risk,
                limit=args.limit,
            )
            print(json.dumps({"records": records, "count": len(records)}, ensure_ascii=False, indent=2))
            return 0
        try:
            print(json.dumps(service.get_assessment(assessment_key), ensure_ascii=False, indent=2))
        except KeyError:
            raise SystemExit("评估结果不存在") from None
        return 0

    records = service.list_records(division_code=args.division, limit=args.limit)
    print(json.dumps({"records": records, "count": len(records)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="township-service", description="乡镇政务协同服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")

    import_parser = subparsers.add_parser("exposure-import", help="导入建筑/设施暴露清单（JSON 或 CSV）")
    import_parser.add_argument("file", help="记录文件路径（.json/.csv）")
    import_parser.add_argument("--batch-id", help="来源批次编码（幂等键）")
    import_parser.add_argument("--source", help="来源标识，默认 cli 或文件中的 source")
    import_parser.add_argument("--updated-at", help="记录更新时间（ISO8601），默认当前 UTC 时间")
    import_parser.set_defaults(func=command_exposure_import)

    assess_parser = subparsers.add_parser("exposure-assess", help="按烈度阈值生成分区高风险汇总")
    assess_parser.add_argument("--threshold", type=float, required=True, help="烈度阈值")
    assess_parser.add_argument("--grid", help="烈度网格 JSON 文件（points 数组）")
    assess_parser.add_argument("--computation-id", type=int, help="引用已完成的烈度计算任务")
    assess_parser.add_argument("--grid-step-km", type=float, help="网格步长（公里），随 --grid 提供")
    assess_parser.set_defaults(func=command_exposure_assess)

    query_parser = subparsers.add_parser("exposure-query", help="查询导入批次、评估分区摘要或暴露记录")
    query_parser.add_argument("--batch", help="查询指定导入批次的数据质量统计")
    query_parser.add_argument("--assessment", help="评估键，或 latest 查看最近一次分区摘要")
    query_parser.add_argument("--division", help="按分区编码过滤记录")
    query_parser.add_argument("--high-risk", action="store_true", help="仅列出高风险记录")
    query_parser.add_argument("--limit", type=int, default=500)
    query_parser.set_defaults(func=command_exposure_query)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in {"init-db", "check-db", "smoke"}:
        return {"init-db": command_init, "check-db": command_check, "smoke": command_smoke}[args.command]()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
