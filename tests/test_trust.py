from __future__ import annotations

from datetime import UTC, datetime

from app.core.clock import FrozenClock
from app.core.security import Principal
from app.database import close_connection, get_connection
from app.trust.service import SCORE_THRESHOLD, TrustService


def login(client, username: str, password: str) -> dict:
    response = client.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456"} if username == "admin" else {"username": username, "password": password, "client_label": "tests"})
    assert response.status_code == 200, response.text
    body = response.json()
    return {"token": body["token"], "headers": {"Authorization": f"Bearer {body['token']}"}}


def make_user(client, admin: dict, username: str, roles: list[str]) -> dict:
    created = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Clerk!23456", "display_name": username, "role_codes": roles},
    )
    assert created.status_code == 201, created.text
    return login(client, username, "Clerk!23456")


def principal(*permissions: str) -> Principal:
    return Principal(user_id=1, username="tester", display_name="测试员", department_id=None, permissions=frozenset(permissions), session_id=1)


def admin_principal() -> Principal:
    return Principal(user_id=1, username="admin", display_name="系统管理员", department_id=None, permissions=frozenset({"*"}), session_id=1)


def register(client, headers: dict, code: str, services: list[str] | None = None, phone: str = "13800138000") -> None:
    response = client.post(
        "/api/trust/vendors",
        headers=headers,
        json={"code": code, "name": f"团队{code}", "contact_name": "队长", "contact_phone": phone, "service_types": services or ["wedding", "funeral"]},
    )
    assert response.status_code == 201, response.text


def complaint_event(code: str, key: str, *, service_type: str = "wedding", severity: str = "severe", source: str = "official", occurred_at: str | None = None) -> dict:
    payload = {
        "vendor_code": code, "event_type": "complaint", "service_type": service_type, "severity": severity,
        "source": source, "detail": "现场服务态度差，客户要求追责", "reporter_contact": "13900139000",
        "idempotency_key": key,
    }
    if occurred_at:
        payload["occurred_at"] = occurred_at
    return payload


# --------------------------------------------------------------- registration
def test_register_requires_manage_permission(client, admin):
    clerk = make_user(client, admin, "trust.clerk", ["clerk"])
    denied = client.post(
        "/api/trust/vendors",
        headers=clerk["headers"],
        json={"code": "team-a", "name": "甲", "service_types": ["wedding"]},
    )
    assert denied.status_code == 403
    register(client, admin["headers"], "team-a")
    listing = client.get("/api/trust/vendors", headers=clerk["headers"])
    assert listing.status_code == 200
    assert listing.json()["items"][0]["code"] == "team-a"


# ------------------------------------------------------------- scoring & window
def test_scoring_accumulates_by_service_type_and_window_and_restricts_only_capability(client, admin):
    register(client, admin["headers"], "team-b")
    # 红事严重投诉（官方通报权重1.5，扣分45）→ 55 分，触发红事隔离
    response = client.post("/api/trust/events", headers=admin["headers"], json=complaint_event("team-b", "event-000001"))
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["duplicate"] is False
    assert len(body["restrictions"]) == 1
    assert body["restrictions"][0]["service_type"] == "wedding"

    wedding = client.post("/api/trust/candidates/query", headers=admin["headers"], json={"service_type": "wedding"})
    funeral = client.post("/api/trust/candidates/query", headers=admin["headers"], json={"service_type": "funeral"})
    assert [item["vendor_code"] for item in wedding.json()["items"]] == []
    assert [item["vendor_code"] for item in wedding.json()["restricted"]] == ["team-b"]
    assert [item["vendor_code"] for item in funeral.json()["items"]] == ["team-b"]
    assert funeral.json()["restricted"] == []

    score = client.get("/api/trust/vendors/team-b/score", headers=admin["headers"]).json()
    assert score["services"]["wedding"]["score"] == 100 - 45
    assert score["services"]["funeral"]["score"] == 100
    assert score["services"]["wedding"]["restriction"] is not None
    assert score["checksum"]


def test_event_older_than_window_does_not_count(client, admin):
    register(client, admin["headers"], "team-old")
    old_time = datetime(2026, 6, 1, tzinfo=UTC).isoformat()
    response = client.post(
        "/api/trust/events",
        headers=admin["headers"],
        json=complaint_event("team-old", "event-old-001", occurred_at=old_time),
    )
    assert response.status_code == 202
    # 默认系统时钟晚于 2026-06-01 超过 90 天，台账已滑出窗口
    score = client.get("/api/trust/vendors/team-old/score", headers=admin["headers"]).json()
    assert score["services"]["wedding"]["score"] == 100
    candidates = client.post("/api/trust/candidates/query", headers=admin["headers"], json={"service_type": "wedding"})
    assert [item["vendor_code"] for item in candidates.json()["items"]] == ["team-old"]


# ----------------------------------------------------------------- dedup/replay
def test_duplicate_event_reports_are_not_counted_twice(client, admin):
    register(client, admin["headers"], "team-c")
    first = client.post("/api/trust/events", headers=admin["headers"], json=complaint_event("team-c", "dup-000001"))
    # 相同幂等键重复上报
    second = client.post("/api/trust/events", headers=admin["headers"], json=complaint_event("team-c", "dup-000001"))
    # 不同幂等键但同一事件指纹（证据相同）重复上报
    third = client.post("/api/trust/events", headers=admin["headers"], json=complaint_event("team-c", "dup-000002"))
    assert second.json()["duplicate"] is True
    assert third.json()["duplicate"] is True

    score = client.get("/api/trust/vendors/team-c/score", headers=admin["headers"]).json()
    assert score["services"]["wedding"]["score"] == 100 - 45
    assert len(score["services"]["wedding"]["ledger"]) == 1

    replay = client.post("/api/trust/scores/replay", headers=admin["headers"])
    assert replay.status_code == 200
    result = replay.json()
    assert result["matched"] is True
    assert result["events_replayed"] == 1

    after_replay = client.get("/api/trust/vendors/team-c/score", headers=admin["headers"]).json()
    assert after_replay["checksum"] == score["checksum"]


# --------------------------------------------------------------- stable ordering
def test_candidate_order_is_stable_score_then_positives_then_code(client, admin):
    register(client, admin["headers"], "alpha")
    register(client, admin["headers"], "bravo")
    register(client, admin["headers"], "charlie")
    # charlie 一次较重迟到（督导权重1.2 → 扣12），分数 88
    client.post("/api/trust/events", headers=admin["headers"], json={
        "vendor_code": "charlie", "event_type": "late", "service_type": "both", "severity": "major",
        "source": "supervisor", "idempotency_key": "order-000001",
    })
    # alpha 一条正向记录，仍为满分但正向数更多
    client.post("/api/trust/events", headers=admin["headers"], json={
        "vendor_code": "alpha", "event_type": "positive", "service_type": "both",
        "source": "client", "idempotency_key": "order-000002",
    })
    first = client.post("/api/trust/candidates/query", headers=admin["headers"], json={"service_type": "wedding"}).json()
    second = client.post("/api/trust/candidates/query", headers=admin["headers"], json={"service_type": "wedding"}).json()
    codes = [item["vendor_code"] for item in first["items"]]
    assert codes == ["alpha", "bravo", "charlie"]
    assert [item["vendor_code"] for item in second["items"]] == codes
    assert first["items"][0]["score"] == 100 and first["items"][0]["positive_events"] == 1


# ------------------------------------------------------------- recovery & guard
def test_recovery_workflow_reject_approve_and_guard_expiry(client, admin):
    close_connection()
    clock = FrozenClock(datetime(2026, 10, 1, tzinfo=UTC))
    service = TrustService(get_connection(), clock)
    admin = admin_principal()
    reviewer = principal("trust.read", "trust.contact.read", "trust.complaint.read", "trust.recover")
    service.register_vendor(admin, {"code": "team-r", "name": "复核团队", "contact_name": "队长", "contact_phone": "13800138000", "service_types": ["wedding"]})
    service.submit_event(admin, complaint_event("team-r", "rec-000001"))
    queried = service.query_candidates(reviewer, {"service_type": "wedding", "scheduled_at": None, "limit": 20})
    assert queried["items"] == [] and len(queried["restricted"]) == 1

    # 复核驳回：保持隔离
    rejected = service.recover(reviewer, {"vendor_code": "team-r", "service_type": "wedding", "decision": "rejected", "note": "证据不足，暂不恢复"})
    assert rejected["restriction"] is None
    assert service.query_candidates(reviewer, {"service_type": "wedding", "scheduled_at": None, "limit": 20})["items"] == []

    # 复核通过：立即恢复，进入30天观察期（分数仍低于阈值也不再自动隔离）
    approved = service.recover(reviewer, {"vendor_code": "team-r", "service_type": "wedding", "decision": "approved", "note": "已完成整改培训"})
    assert approved["restriction"]["status"] == "lifted"
    eligible = service.query_candidates(reviewer, {"service_type": "wedding", "scheduled_at": None, "limit": 20})
    assert [item["vendor_code"] for item in eligible["items"]] == ["team-r"]
    assert eligible["items"][0]["probation"] is True

    # 观察期结束且评分仍未回升：重新自动隔离，需要再次复核
    clock.advance(days=31)
    again = service.query_candidates(reviewer, {"service_type": "wedding", "scheduled_at": None, "limit": 20})
    assert again["items"] == [] and len(again["restricted"]) == 1


def test_recover_without_active_restriction_conflicts(client, admin):
    register(client, admin["headers"], "team-clean")
    response = client.post("/api/trust/recovery", headers=admin["headers"], json={
        "vendor_code": "team-clean", "service_type": "wedding", "decision": "approved", "note": "无隔离也要恢复",
    })
    assert response.status_code == 409


# ------------------------------------------------------------- manual overrides
def test_manual_override_requires_until_and_reason(client, admin):
    register(client, admin["headers"], "team-m")
    missing_until = client.post("/api/trust/overrides", headers=admin["headers"], json={
        "vendor_code": "team-m", "service_type": "wedding", "action": "restrict", "reason": "重大活动保障",
    })
    assert missing_until.status_code == 422
    missing_reason = client.post("/api/trust/overrides", headers=admin["headers"], json={
        "vendor_code": "team-m", "service_type": "wedding", "action": "restrict", "reason": "x", "effective_until": "2026-09-01T00:00:00+00:00",
    })
    # 期限早于当前时间也会被拒绝
    assert missing_reason.status_code == 422

    restricted = client.post("/api/trust/overrides", headers=admin["headers"], json={
        "vendor_code": "team-m", "service_type": "wedding", "action": "restrict",
        "reason": "重大活动保障期间暂停高风险场次", "effective_until": "2026-12-01T00:00:00+00:00",
    })
    assert restricted.status_code == 200
    assert restricted.json()["origin"] == "manual"
    assert restricted.json()["effective_until"] == "2026-12-01T00:00:00+00:00"
    candidates = client.post("/api/trust/candidates/query", headers=admin["headers"], json={"service_type": "wedding"})
    assert candidates.json()["items"] == []

    lifted = client.post("/api/trust/overrides", headers=admin["headers"], json={
        "vendor_code": "team-m", "service_type": "wedding", "action": "lift",
        "reason": "保障期结束，人工解除", "effective_until": "2026-11-01T00:00:00+00:00",
    })
    assert lifted.status_code == 200 and lifted.json()["status"] == "lifted"


# ------------------------------------------------------------ tiered visibility
def test_contact_and_complaint_details_follow_permission_tiers(client, admin):
    register(client, admin["headers"], "team-p")
    client.post("/api/trust/events", headers=admin["headers"], json=complaint_event("team-p", "priv-000001"))
    client.post("/api/trust/events", headers=admin["headers"], json={
        "vendor_code": "team-p", "event_type": "late", "service_type": "wedding", "severity": "minor",
        "source": "dispatch", "detail": "迟到15分钟", "reporter_contact": "13700137000", "idempotency_key": "priv-000002",
    })
    clerk = make_user(client, admin, "priv.clerk", ["clerk"])          # 有联系方式、无投诉细节
    auditor = make_user(client, admin, "priv.auditor", ["auditor"])    # 只有只读，无联系方式、无投诉细节
    reviewer = make_user(client, admin, "priv.reviewer", ["trust_reviewer"])

    def events(headers):
        return client.get("/api/trust/vendors/team-p/events", headers=headers).json()["items"]

    admin_events = events(admin["headers"])
    clerk_events = events(clerk["headers"])
    auditor_events = events(auditor["headers"])
    reviewer_events = events(reviewer["headers"])
    complaint_admin = next(item for item in admin_events if item["event_type"] == "complaint")
    complaint_clerk = next(item for item in clerk_events if item["event_type"] == "complaint")
    complaint_auditor = next(item for item in auditor_events if item["event_type"] == "complaint")
    complaint_reviewer = next(item for item in reviewer_events if item["event_type"] == "complaint")

    assert complaint_admin["detail_hidden"] is False and complaint_admin["reporter_contact"] == "13900139000"
    assert complaint_clerk["detail_hidden"] is True and complaint_clerk["detail"] is None
    assert complaint_auditor["detail_hidden"] is True and complaint_auditor["reporter_contact"] is None
    assert complaint_reviewer["detail_hidden"] is False and complaint_reviewer["reporter_contact"] == "13900139000"

    late_clerk = next(item for item in clerk_events if item["event_type"] == "late")
    late_auditor = next(item for item in auditor_events if item["event_type"] == "late")
    assert late_clerk["detail"] == "迟到15分钟" and late_clerk["reporter_contact"] == "13700137000"
    assert late_auditor["detail"] == "迟到15分钟" and late_auditor["reporter_contact"] == "137****7000"

    def candidates(headers):
        return client.post("/api/trust/candidates/query", headers=headers, json={"service_type": "funeral"}).json()["items"][0]

    assert candidates(clerk["headers"])["contact_phone"] == "13800138000"
    assert candidates(auditor["headers"])["contact_phone"] == "138****8000"
    assert candidates(reviewer["headers"])["contact_phone"] == "13800138000"


def test_permission_denials_are_audited(client, admin):
    clerk = make_user(client, admin, "deny.clerk", ["clerk"])
    denied = client.post("/api/trust/recovery", headers=clerk["headers"], json={
        "vendor_code": "whatever", "service_type": "wedding", "decision": "approved", "note": "无权操作",
    })
    assert denied.status_code == 403
    summary = client.get("/api/trust/audit-summary", headers=admin["headers"])
    assert summary.status_code == 200
    actions = summary.json()["actions"]
    assert actions["trust.recovery"]["denied"] >= 1


# --------------------------------------------------------- persistence/restart
def test_state_survives_connection_restart(client, admin):
    register(client, admin["headers"], "team-s")
    client.post("/api/trust/events", headers=admin["headers"], json=complaint_event("team-s", "persist-0001"))
    score_before = client.get("/api/trust/vendors/team-s/score", headers=admin["headers"]).json()

    close_connection()  # 模拟进程重启：丢弃线程连接后重新打开同一个数据库文件
    service = TrustService(get_connection())
    detail = service.score_detail(admin_principal(), "team-s")
    assert detail["services"]["wedding"]["score"] == score_before["services"]["wedding"]["score"]
    assert detail["services"]["wedding"]["restriction"] is not None
    queried = service.query_candidates(admin_principal(), {"service_type": "wedding", "scheduled_at": None, "limit": 20})
    assert queried["items"] == []
    assert queried["restricted"][0]["vendor_code"] == "team-s"
    assert SCORE_THRESHOLD == 60
