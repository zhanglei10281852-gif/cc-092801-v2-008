from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


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


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_trust_demo() -> int:
    from datetime import UTC, datetime, timedelta

    password = "Trust!23456"
    with TestClient(app) as client:
        client.post("/api/auth/bootstrap", json={"username": "admin", "password": password, "client_label": "trust-demo"})
        admin = {"Authorization": f"Bearer {client.post('/api/auth/login', json={'username': 'admin', 'password': password, 'client_label': 'trust-demo'}).json()['token']}"}
        for username, role, pw, display in [
            ("demo-dispatcher", "dispatcher", "Dispatch!123", "演示调度员"),
            ("demo-reviewer", "trust_reviewer", "Reviewer!12", "演示复核员"),
        ]:
            created = client.post("/api/users", json={"username": username, "password": pw, "display_name": display, "role_codes": [role]}, headers=admin)
            if created.status_code not in {201, 409}:
                print(created.text)
                return 1
            token = client.post("/api/auth/login", json={"username": username, "password": pw, "client_label": "trust-demo"}).json()["token"]
            admin[role] = f"Bearer {token}"
        dispatcher = {"Authorization": admin["dispatcher"]}
        reviewer = {"Authorization": admin["trust_reviewer"]}
        for code, phone in [("red-team-a", "13800138001"), ("red-team-b", "13800138002")]:
            r = client.post(
                "/api/trust/vendors",
                json={"code": code, "name": code, "contact_name": "现场负责人", "phone": phone, "capabilities": ["wedding", "funeral"]},
                headers=admin,
            )
            if r.status_code not in {201, 409}:
                print(r.text)
                return 1
        now = datetime.now(UTC)
        events = [
            ("red-team-a", "trust-demo-001", "complaint", 2, "customer_hotline", now - timedelta(days=2)),
            ("red-team-a", "trust-demo-002", "complaint", 2, "onsite_report", now - timedelta(days=1)),
        ]
        for code, key, kind, severity, source, when in events:
            reported = client.post(
                "/api/trust/events",
                json={"vendor_code": code, "event_type": kind, "ceremony_type": "wedding", "severity": severity,
                      "source": source, "occurrence_key": key, "occurrence_at": when.isoformat(),
                      "detail": {"customer_note": "演示用投诉记录"}},
                headers=dispatcher,
            )
            if reported.status_code != 202:
                print(reported.text)
                return 1
        wedding = client.get("/api/trust/candidates/wedding", headers=dispatcher).json()
        probation = client.post(
            "/api/trust/reviews",
            json={"vendor_code": "red-team-a", "ceremony_type": "wedding", "mode": "probation",
                  "reason": "演示：复核后进入十四天观察期", "expires_at": (now + timedelta(days=14)).isoformat()},
            headers=reviewer,
        )
        summary = client.get("/api/trust/audit-summary?vendor_code=red-team-a", headers=admin).json()
    result = {
        "wedding_candidates": [item["vendor"]["code"] for item in wedding["items"]],
        "probation_status": probation.status_code,
        "audit_actions": summary["audit_counts"],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if wedding["items"] and probation.status_code in {201, 409} else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="ceremony-operations", description="红白喜事服务运营平台维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("trust-demo", help="执行供应商可信度事件、隔离与复核演示")
    args = parser.parse_args()
    return {"init-db": command_init, "check-db": command_check, "smoke": command_smoke, "compute-demo": command_compute_demo, "trust-demo": command_trust_demo}[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
