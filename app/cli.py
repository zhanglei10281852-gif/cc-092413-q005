from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app
from app.seismic.exposure.service import ExposureService


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
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


def _read_rows(path: Path, fmt: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读取导入文件。JSON 支持 {"batch_ref","source","received_at","records":[...]}，CSV 读取表头行。"""
    if fmt == "csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle)), {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data, {}
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return data["records"], {key: data.get(key) for key in ("batch_ref", "source", "received_at") if data.get(key) is not None}
    raise ValueError("JSON 文件须为记录数组，或包含 records 数组的对象")


def command_exposure_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    rows, meta = _read_rows(path, args.format)
    report = ExposureService().import_batch(
        args.batch_ref or meta.get("batch_ref") or path.stem,
        rows,
        received_at=args.received_at or meta.get("received_at"),
        source=args.source or meta.get("source", "") or "",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_exposure_grid(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if args.format == "csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            cells = list(csv.DictReader(handle))
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cells = payload["cells"] if isinstance(payload, dict) and isinstance(payload.get("cells"), list) else payload
    result = ExposureService().replace_intensity_grid(cells, scope=args.scope)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_exposure_summary(args: argparse.Namespace) -> int:
    result = ExposureService().summarize(
        args.intensity_min, scope=args.scope, district_code=args.district, tolerance_km=args.tolerance_km
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="township-service", description="乡镇政务协同服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")

    import_parser = subparsers.add_parser("exposure-import", help="导入建筑与设施暴露清单（JSON/CSV）")
    import_parser.add_argument("--file", required=True, help="记录文件路径")
    import_parser.add_argument("--format", choices=("json", "csv"), default="json")
    import_parser.add_argument("--batch-ref", help="来源批次编码（默认取 JSON 字段或文件名）")
    import_parser.add_argument("--source", default="", help="来源说明")
    import_parser.add_argument("--received-at", help="来源批次更新时间（ISO-8601）")
    import_parser.set_defaults(handler=command_exposure_import)

    grid_parser = subparsers.add_parser("exposure-grid", help="上传烈度格网（JSON/CSV）")
    grid_parser.add_argument("--file", required=True, help="格网文件路径，列：latitude,longitude,intensity")
    grid_parser.add_argument("--format", choices=("json", "csv"), default="json")
    grid_parser.add_argument("--scope", default="default", help="格网范围标识")
    grid_parser.set_defaults(handler=command_exposure_grid)

    summary_parser = subparsers.add_parser("exposure-summary", help="按烈度阈值输出分区摘要与数据质量统计")
    summary_parser.add_argument("--intensity-min", type=float, required=True, help="高风险烈度阈值")
    summary_parser.add_argument("--scope", default="default")
    summary_parser.add_argument("--district", default=None, help="只查询单个分区编码")
    summary_parser.add_argument("--tolerance-km", type=float, default=11.0, help="记录到格网点的最大匹配距离")
    summary_parser.set_defaults(handler=command_exposure_summary)

    args = parser.parse_args(argv)
    if args.command in {"init-db", "check-db", "smoke"}:
        return {"init-db": command_init, "check-db": command_check, "smoke": command_smoke}[args.command]()
    init_db()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
