from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.clock import FrozenClock
from app.database import close_connection, get_connection
from app.main import app
from app.trust.service import TrustService, Viewer
from app.trust import policy


ADMIN_PASSWORD = "Admin!23456"
DISP_PASSWORD = "Dispatch!123"
REV_PASSWORD = "Reviewer!12"
CLERK_PASSWORD = "Clerk!12345"


@pytest.fixture()
def auth(client):
    client.post("/api/auth/bootstrap", json={"username": "admin", "password": ADMIN_PASSWORD, "client_label": "t"})
    login = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD, "client_label": "t"})
    admin = {"Authorization": f"Bearer {login.json()['token']}"}
    users = [
        ("dispatcher1", "dispatcher", DISP_PASSWORD, "现场调度员甲"),
        ("reviewer1", "trust_reviewer", REV_PASSWORD, "可信度复核员甲"),
        ("clerk1", "clerk", CLERK_PASSWORD, "综合经办员"),
    ]
    headers: dict[str, dict[str, str]] = {"admin": admin}
    for username, role, password, display in users:
        created = client.post(
            "/api/users",
            json={"username": username, "password": password, "display_name": display, "role_codes": [role]},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        token = client.post("/api/auth/login", json={"username": username, "password": password, "client_label": "t"}).json()["token"]
        headers[role] = {"Authorization": f"Bearer {token}"}
    return headers


def register_vendor(client, headers, code, *, phone="13800138000", caps=("wedding", "funeral")):
    response = client.post(
        "/api/trust/vendors",
        json={"code": code, "name": code, "contact_name": "王经理", "phone": phone, "capabilities": list(caps)},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def event_payload(code, key, *, event_type="late", ceremony="wedding", severity=2, source="gps",
                  when=None, detail=None):
    payload = {
        "vendor_code": code, "event_type": event_type, "ceremony_type": ceremony,
        "severity": severity, "source": source, "occurrence_key": key,
        "occurrence_at": (when or datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }
    if detail is not None:
        payload["detail"] = detail
    return payload


# -- 权限分级 -------------------------------------------------------------

def test_anonymous_and_unprivileged_are_denied(client, auth):
    assert client.get("/api/trust/candidates/wedding").status_code == 401
    # clerk 只有居民/事务类权限，不能查询候选、不能上报
    assert client.get("/api/trust/candidates/wedding", headers=auth["clerk"]).status_code == 403
    assert client.post("/api/trust/events", json=event_payload("t-a", "deny-0001"), headers=auth["clerk"]).status_code == 403
    # 调度员不能登记供应商、不能复核
    register_denied = client.post(
        "/api/trust/vendors",
        json={"code": "t-x", "name": "无效登记", "capabilities": ["wedding"]},
        headers=auth["dispatcher"],
    )
    assert register_denied.status_code == 403
    # 复核员不能人工覆盖
    register_vendor(client, auth["admin"], "t-a")
    override = client.post(
        "/api/trust/overrides",
        json={"vendor_code": "t-a", "ceremony_type": "wedding", "kind": "manual_block",
              "reason": "复核员无权停用供应商资格", "expires_at": (datetime.now(UTC) + timedelta(days=5)).isoformat()},
        headers=auth["trust_reviewer"],
    )
    assert override.status_code == 403


def test_contact_and_complaint_details_follow_tiers(client, auth):
    register_vendor(client, auth["admin"], "t-a", phone="13812345678")
    resp = client.post(
        "/api/trust/events",
        json=event_payload("t-a", "tier-0001", event_type="complaint", severity=2, source="customer_hotline",
                           detail={"customer_note": "司仪迟到且言辞不当，联系电话 13900001111"}),
        headers=auth["dispatcher"],
    )
    assert resp.status_code == 202, resp.text
    # 调度员：完整手机号，但投诉细节被掩码
    dispatcher_scorecard = client.get("/api/trust/vendors/t-a/scorecard", headers=auth["dispatcher"]).json()
    assert dispatcher_scorecard["vendor"]["phone"] == "13812345678"
    events_for_dispatcher = client.get("/api/trust/vendors/t-a/events", headers=auth["dispatcher"]).json()["items"]
    assert events_for_dispatcher[0]["detail"] == {"redacted": True}
    # 复核员：手机号掩码，但可见完整投诉细节与上报人
    reviewer_scorecard = client.get("/api/trust/vendors/t-a/scorecard", headers=auth["trust_reviewer"]).json()
    assert reviewer_scorecard["vendor"]["phone"] == "138****5678"
    events_for_reviewer = client.get("/api/trust/vendors/t-a/events", headers=auth["trust_reviewer"]).json()["items"]
    assert "司仪迟到" in events_for_reviewer[0]["detail"]["customer_note"]
    assert events_for_reviewer[0]["reporter"]


# -- 幂等与评分可解释/可回放 -----------------------------------------------

def test_duplicate_occurrence_is_not_double_scored(client, auth):
    register_vendor(client, auth["admin"], "t-a")
    payload = event_payload("t-a", "dup-0001", event_type="late", severity=2)
    first = client.post("/api/trust/events", json=payload, headers=auth["dispatcher"]).json()
    second = client.post("/api/trust/events", json=payload, headers=auth["dispatcher"]).json()
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert first["event"]["id"] == second["event"]["id"]
    scorecard = client.get("/api/trust/vendors/t-a/scorecard", headers=auth["dispatcher"]).json()
    contributions = scorecard["windows"]["30"]["wedding"]["contributions"]
    assert len(contributions) == 1
    assert contributions[0]["points"] == first["event"]["points"]
    # 事件冻结了策略版本与来源权重，回放可以逐条解释
    assert contributions[0]["policy_version"] == policy.POLICY_VERSION
    assert contributions[0]["weight"] == policy.SOURCE_WEIGHTS["gps"]


def test_scoring_is_replayable_with_frozen_weights(client, auth):
    register_vendor(client, auth["admin"], "t-a")
    base = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    client.post("/api/trust/events", json=event_payload("t-a", "rep-0001", event_type="late", severity=3, when=base), headers=auth["dispatcher"])
    client.post("/api/trust/events", json=event_payload("t-a", "rep-0002", event_type="substitute", severity=1, source="onsite_report", when=base + timedelta(days=2)), headers=auth["dispatcher"])
    client.post("/api/trust/events", json=event_payload("t-a", "rep-0003", event_type="praise", severity=1, when=base + timedelta(days=3)), headers=auth["dispatcher"])

    clock = FrozenClock(base + timedelta(days=10))
    service = TrustService(get_connection(), clock)
    viewer = Viewer(1, "admin", frozenset({"*"}))
    first = service.scorecard("t-a", viewer)
    contributions = first["windows"]["30"]["wedding"]["contributions"]
    # 迟到严重度3：8*2*1=16；换人：6*1*0.8=4.8→5；表扬：-5
    assert [c["points"] for c in contributions] == [16, 5, -5]
    # 同一时钟下重复重放结果完全一致
    second = service.scorecard("t-a", viewer)
    assert first["windows"] == second["windows"]
    # 超过 30 天窗口后扣分自然衰减——偶发异常不会永久影响团队
    aged = TrustService(get_connection(), FrozenClock(base + timedelta(days=40))).scorecard("t-a", viewer)
    assert aged["windows"]["30"]["wedding"]["penalty_total"] == 0
    assert aged["windows"]["30"]["wedding"]["score"] == policy.BASE_SCORE
    # 90 天窗口内仍可追溯历史
    assert aged["windows"]["90"]["wedding"]["penalty_total"] == 21


# -- 阈值触发隔离（仅限制相应能力） ----------------------------------------

def test_evidence_source_weight_changes_points(client, auth):
    register_vendor(client, auth["admin"], "t-a")
    client.post("/api/trust/events", json=event_payload("t-a", "src-0001", event_type="complaint", severity=2, source="online_review"), headers=auth["dispatcher"])
    scorecard = client.get("/api/trust/vendors/t-a/scorecard", headers=auth["dispatcher"]).json()
    points = scorecard["windows"]["30"]["wedding"]["contributions"][0]["points"]
    # 在线评论权重 0.6：15 * 1.5 * 0.6 = 13.5 -> 14，低于 gps 直连证据的 22 分
    assert points == 14
    assert scorecard["windows"]["30"]["wedding"]["cooling"] is False


# -- 阈值触发隔离（仅限制相应能力） ----------------------------------------

def test_threshold_restricts_only_matching_ceremony(client, auth):
    register_vendor(client, auth["admin"], "t-a")
    register_vendor(client, auth["admin"], "t-b")
    # gps 证据：严重投诉 15*1.5=22 分（降温），再叠加一般投诉 15 分，累计 37 >= 30 隔离阈值
    r1 = client.post("/api/trust/events", json=event_payload("t-a", "res-0001", event_type="complaint", severity=2, when=datetime.now(UTC) - timedelta(days=2)), headers=auth["dispatcher"])
    assert r1.status_code == 202 and r1.json()["auto_decision"] is None  # 22 分：仅降温
    r2 = client.post("/api/trust/events", json=event_payload("t-a", "res-0002", event_type="complaint", severity=1, when=datetime.now(UTC) - timedelta(days=1)), headers=auth["dispatcher"])
    decision = r2.json()["auto_decision"]
    assert decision is not None and decision["kind"] == "auto_restrict"
    # 红事被隔离，白事能力不受影响
    wedding = client.get("/api/trust/candidates/wedding", headers=auth["dispatcher"]).json()
    funeral = client.get("/api/trust/candidates/funeral", headers=auth["dispatcher"]).json()
    assert [i["vendor"]["code"] for i in wedding["items"]] == ["t-b"]
    assert {i["vendor"]["code"] for i in funeral["items"]} == {"t-a", "t-b"}
    state = client.get("/api/trust/vendors/t-a/scorecard", headers=auth["dispatcher"]).json()["ceremonies"]["wedding"]
    assert state["blocked"] is True and state["eligible"] is False
    assert any("自动隔离" in reason for reason in state["reasons"])
    assert client.get("/api/trust/vendors/t-a/scorecard", headers=auth["dispatcher"]).json()["ceremonies"]["funeral"]["eligible"] is True


# -- 稳定排序 -------------------------------------------------------------

def test_candidate_order_is_stable_and_explainable(client, auth):
    register_vendor(client, auth["admin"], "t-a")
    register_vendor(client, auth["admin"], "t-b")
    register_vendor(client, auth["admin"], "t-c")
    # 全新供应商：分数相同，按 id 稳定排序
    codes = [i["vendor"]["code"] for i in client.get("/api/trust/candidates/wedding", headers=auth["dispatcher"]).json()["items"]]
    assert codes == ["t-a", "t-b", "t-c"]
    # t-b 出现一次严重迟到（8*2=16 分，进入降温、未达隔离阈值），排序应落到正常团队之后
    client.post("/api/trust/events", json=event_payload("t-b", "ord-0001", event_type="late", severity=3), headers=auth["dispatcher"])
    ranked = client.get("/api/trust/candidates/wedding", headers=auth["dispatcher"]).json()["items"]
    assert [i["vendor"]["code"] for i in ranked] == ["t-a", "t-c", "t-b"]
    assert ranked[-1]["cooling"] is True
    assert any("降温" in r for r in ranked[-1]["reasons"])
    # 多次查询排序完全一致
    again = [i["vendor"]["code"] for i in client.get("/api/trust/candidates/wedding", headers=auth["dispatcher"]).json()["items"]]
    assert again == ["t-a", "t-c", "t-b"]


# -- 人工覆盖必须有期限与原因 ----------------------------------------------

def test_manual_override_requires_reason_and_bounded_expiry(client, auth):
    register_vendor(client, auth["admin"], "t-a")
    base_url = "/api/trust/overrides"
    body = {"vendor_code": "t-a", "ceremony_type": "funeral", "kind": "manual_block"}
    # 缺期限
    assert client.post(base_url, json={**body, "reason": "资质材料待复核暂停派单"}, headers=auth["admin"]).status_code == 422
    # 原因过短
    assert client.post(base_url, json={**body, "reason": "暂停", "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat()}, headers=auth["admin"]).status_code == 422
    # 期限已过
    assert client.post(base_url, json={**body, "reason": "资质材料待复核暂停派单", "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}, headers=auth["admin"]).status_code == 422
    # 超过单次最长期限
    too_long = client.post(base_url, json={**body, "reason": "资质材料待复核暂停派单", "expires_at": (datetime.now(UTC) + timedelta(days=200)).isoformat()}, headers=auth["admin"])
    assert too_long.status_code == 422
    # 合规覆盖生效且到期自动失效
    created = client.post(base_url, json={**body, "reason": "资质材料待复核暂停派单三十天", "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()}, headers=auth["admin"])
    assert created.status_code == 201
    funeral = client.get("/api/trust/candidates/funeral", headers=auth["dispatcher"]).json()
    assert all(i["vendor"]["code"] != "t-a" for i in funeral["items"])
    clock = FrozenClock(datetime.now(UTC) + timedelta(days=31))
    service = TrustService(get_connection(), clock)
    viewer = Viewer(2, "调度员", frozenset({"trust.dispatch"}))
    candidates = service.candidates("funeral", viewer)
    assert any(i["vendor"]["code"] == "t-a" for i in candidates["items"])


# -- 复核恢复：观察期 + 恢复后旧账不重复触发 ---------------------------------

def test_review_probation_then_restore_and_old_events_do_not_retrigger(client, auth):
    register_vendor(client, auth["admin"], "t-a")
    now = datetime.now(UTC)
    client.post("/api/trust/events", json=event_payload("t-a", "rev-0001", event_type="no_show", severity=2, when=now - timedelta(days=3)), headers=auth["dispatcher"])
    assert client.get("/api/trust/candidates/wedding", headers=auth["dispatcher"]).json()["items"] == []
    # 未满观察期不能直接恢复，只能观察期恢复
    early = client.post("/api/trust/reviews", json={"vendor_code": "t-a", "ceremony_type": "wedding", "mode": "restore", "reason": "团队已提交整改证明材料"}, headers=auth["trust_reviewer"])
    assert early.status_code == 409
    probation = client.post(
        "/api/trust/reviews",
        json={"vendor_code": "t-a", "ceremony_type": "wedding", "mode": "probation",
              "reason": "先恢复派单进入十四天观察期", "expires_at": (now + timedelta(days=14)).isoformat()},
        headers=auth["trust_reviewer"],
    )
    assert probation.status_code == 201, probation.text
    items = client.get("/api/trust/candidates/wedding", headers=auth["dispatcher"]).json()["items"]
    assert len(items) == 1 and items[0]["probation"] is True and items[0]["cooling"] is True
    # 观察期内没有新问题，满观察期后正式恢复
    clock = FrozenClock(now + timedelta(days=8))
    service = TrustService(get_connection(), clock)
    viewer = Viewer(3, "复核员", frozenset({"trust.review"}))
    restored = service.review({"vendor_code": "t-a", "ceremony_type": "wedding", "mode": "restore", "reason": "观察期表现稳定正式恢复派单"}, viewer)
    assert restored["kind"] == "restore"
    candidates = service.candidates("wedding", Viewer(2, "调度员", frozenset({"trust.dispatch"})))
    (item,) = candidates["items"]
    assert item["vendor"]["code"] == "t-a" and item["probation"] is False
    # 恢复后仅再来一次严重迟到（16 分），不能因旧账累计再次直接隔离，只进入降温
    clock.advance(days=1)
    service.report_event(event_payload("t-a", "rev-0002", event_type="late", severity=3, when=clock.now() - timedelta(hours=2)), Viewer(2, "调度员", frozenset({"trust.dispatch"})))
    candidates = service.candidates("wedding", Viewer(2, "调度员", frozenset({"trust.dispatch"})))
    assert [i["vendor"]["code"] for i in candidates["items"]] == ["t-a"]
    assert candidates["items"][0]["cooling"] is True


def test_dismissed_event_is_excluded_from_replay(client, auth):
    register_vendor(client, auth["admin"], "t-a")
    client.post("/api/trust/events", json=event_payload("t-a", "dis-0001", event_type="late", severity=2), headers=auth["dispatcher"])
    before = client.get("/api/trust/vendors/t-a/scorecard", headers=auth["trust_reviewer"]).json()
    assert before["windows"]["30"]["wedding"]["penalty_total"] > 0
    event_id = before["windows"]["30"]["wedding"]["contributions"][0]["event_id"]
    dismissed = client.post(
        "/api/trust/events/dismiss",
        json={"vendor_code": "t-a", "event_id": event_id, "reason": "GPS 证据复核确认为系统误报迟到"},
        headers=auth["trust_reviewer"],
    )
    assert dismissed.status_code == 201
    after = client.get("/api/trust/vendors/t-a/scorecard", headers=auth["trust_reviewer"]).json()
    assert after["windows"]["30"]["wedding"]["penalty_total"] == 0
    # 事件本身不删除，保留否决痕迹；调度员再次否决被拒绝
    again = client.post(
        "/api/trust/events/dismiss",
        json={"vendor_code": "t-a", "event_id": event_id, "reason": "重复尝试否决同一事件"},
        headers=auth["trust_reviewer"],
    )
    assert again.status_code == 409
    # 调度员无权否决证据
    denied = client.post(
        "/api/trust/events/dismiss",
        json={"vendor_code": "t-a", "event_id": event_id, "reason": "调度员尝试否决证据"},
        headers=auth["dispatcher"],
    )
    assert denied.status_code == 403


# -- 审计摘要 -------------------------------------------------------------

def test_audit_summary_records_decisions_and_denials(client, auth):
    register_vendor(client, auth["admin"], "t-a")
    payload = event_payload("t-a", "aud-0001")
    client.post("/api/trust/events", json=payload, headers=auth["dispatcher"])
    client.post("/api/trust/events", json=payload, headers=auth["dispatcher"])  # 重复 -> denied
    client.post("/api/trust/events", json=payload, headers=auth["clerk"])  # 越权 -> 403
    summary = client.get("/api/trust/audit-summary?vendor_code=t-a", headers=auth["admin"]).json()
    actions = {row["action"]: row["amount"] for row in summary["audit_counts"] if row["outcome"] == "success"}
    denials = {(row["action"], row["outcome"]): row["amount"] for row in summary["audit_counts"]}
    assert actions["trust.event.report"] == 1
    assert denials[("trust.event.duplicate", "denied")] == 1
    kinds = {(d["kind"], d["status"]) for d in summary["recent_decisions"]}
    assert ("dismiss_event", "active") not in kinds  # 尚未否决
    # 无权限者不能查看审计摘要
    assert client.get("/api/trust/audit-summary", headers=auth["clerk"]).status_code == 403


# -- 重启后状态一致 --------------------------------------------------------

def test_state_survives_restart(client, auth):
    register_vendor(client, auth["admin"], "t-a")
    client.post("/api/trust/events", json=event_payload("t-a", "rst-0001", event_type="no_show", severity=2), headers=auth["dispatcher"])
    client.post(
        "/api/trust/overrides",
        json={"vendor_code": "t-a", "ceremony_type": "funeral", "kind": "manual_cooling",
              "reason": "白事现场反馈不稳定先降温三十天", "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
        headers=auth["admin"],
    )
    # 模拟进程重启：关闭连接与 TestClient，重新打开同一数据库文件
    close_connection()
    with TestClient(app) as restarted:
        login = restarted.post("/api/auth/login", json={"username": "dispatcher1", "password": DISP_PASSWORD, "client_label": "t"})
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        wedding = restarted.get("/api/trust/candidates/wedding", headers=headers).json()
        funeral = restarted.get("/api/trust/candidates/funeral", headers=headers).json()
        assert wedding["items"] == []  # 自动隔离仍然生效
        funeral_item = next(i for i in funeral["items"] if i["vendor"]["code"] == "t-a")
        assert funeral_item["cooling"] is True  # 人工降温仍然生效
    close_connection()
