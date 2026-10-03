from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from app.core.clock import UTC, Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.privacy import mask_phone
from app.core.security import Principal
from app.database import get_connection, transaction
from app.services.audit import AuditContext, AuditService
from app.trust.store import ensure_schema

BASE_SCORE = 100
SCORE_THRESHOLD = 60
WINDOW_DAYS = 90
REVIEW_GUARD_DAYS = 30
FUTURE_TOLERANCE_SECONDS = 60

PENALTIES: dict[str, dict[str, int]] = {
    "late": {"minor": 5, "major": 10, "severe": 20},
    "substitution": {"minor": 10, "major": 20, "severe": 30},
    "complaint": {"minor": 8, "major": 16, "severe": 30},
}
POSITIVE_DELTA = 10
SOURCE_WEIGHTS = {"dispatch": 1.0, "client": 1.0, "supervisor": 1.2, "official": 1.5}
SOURCE_LABELS = {"dispatch": "调度记录", "client": "客户反馈", "supervisor": "督导核查", "official": "官方通报"}
TYPE_LABELS = {"late": "迟到", "substitution": "临时换人", "complaint": "投诉", "positive": "正向记录"}
SEVERITY_LABELS = {"minor": "一般", "major": "较重", "severe": "严重"}

CONCRETE_TYPES = ("wedding", "funeral")


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("时间格式不是合法的 ISO8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class TrustService:
    """供应商可信度评分、能力隔离、复核恢复与分级可见性。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    # ------------------------------------------------------------------ vendors
    def register_vendor(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("trust.manage")
        now = to_storage(self.clock.now())
        service_types = ",".join(payload["service_types"])
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM trust_vendors WHERE code=?", (payload["code"],)).fetchone():
                raise ConflictError("供应商编码已存在")
            cursor = connection.execute(
                "INSERT INTO trust_vendors(code,name,contact_name,contact_phone,service_types,active,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)",
                (payload["code"], payload["name"], payload.get("contact_name", ""), payload.get("contact_phone", ""), service_types, now, now),
            )
            vendor = dict(connection.execute("SELECT * FROM trust_vendors WHERE id=?", (cursor.lastrowid,)).fetchone())
            self._audit(connection, principal, "trust.vendor.register", payload["code"], after={"code": vendor["code"], "name": vendor["name"]})
        return self._render_vendor(vendor, principal)

    def list_vendors(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("trust.read")
        rows = self.connection.execute("SELECT * FROM trust_vendors ORDER BY code").fetchall()
        return [self._render_vendor(dict(row), principal) for row in rows]

    def _vendor_by_code(self, connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM trust_vendors WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("供应商不存在")
        return row

    # ------------------------------------------------------------------- events
    def submit_event(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("trust.event.write")
        now_value = self.clock.now()
        occurred = _parse_time(payload["occurred_at"]) if payload.get("occurred_at") else now_value
        if occurred > now_value + timedelta(seconds=FUTURE_TOLERANCE_SECONDS):
            raise ValidationError("事件发生时间不能晚于当前时间")
        occurred_text = to_storage(occurred)
        now = to_storage(now_value)
        fingerprint_basis = {
            "event_type": payload["event_type"],
            "service_type": payload["service_type"],
            "severity": payload["severity"],
            "occurred_at": occurred_text,
            "source": payload["source"],
            "evidence_ref": payload.get("evidence_ref", ""),
            "detail": payload.get("detail", "") if not payload.get("evidence_ref") else "",
        }
        fingerprint = digest(fingerprint_basis)
        with transaction(immediate=True) as connection:
            vendor = self._vendor_by_code(connection, payload["vendor_code"])
            existing = connection.execute(
                "SELECT * FROM trust_events WHERE vendor_id=? AND (idempotency_key=? OR event_fingerprint=?)",
                (vendor["id"], payload["idempotency_key"], fingerprint),
            ).fetchone()
            if existing is not None:
                self._audit(
                    connection, principal, "trust.event.submit.duplicate", vendor["code"],
                    metadata={"event_id": existing["id"], "idempotency_key": payload["idempotency_key"]},
                )
                return {"duplicate": True, "event": self._render_event(dict(existing), principal), "ledgers": []}
            cursor = connection.execute(
                "INSERT INTO trust_events(vendor_id,idempotency_key,event_type,service_type,severity,occurred_at,source,evidence_ref,detail,reporter_contact,event_fingerprint,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    vendor["id"], payload["idempotency_key"], payload["event_type"], payload["service_type"],
                    payload["severity"], occurred_text, payload["source"], payload.get("evidence_ref", ""),
                    payload.get("detail", ""), payload.get("reporter_contact", ""), fingerprint, now,
                ),
            )
            event = dict(connection.execute("SELECT * FROM trust_events WHERE id=?", (cursor.lastrowid,)).fetchone())
            ledgers = self._write_ledgers(connection, event)
            restrictions = self._apply_auto_restrictions(connection, vendor, event, ledgers, now_value)
            self._audit(
                connection, principal, "trust.event.submit", vendor["code"],
                after={"event_id": event["id"], "event_type": event["event_type"], "service_type": event["service_type"], "severity": event["severity"], "ledgers": ledgers, "restrictions": [r["id"] for r in restrictions]},
                metadata={"source": event["source"]},
            )
        return {"duplicate": False, "event": self._render_event(event, principal), "ledgers": ledgers, "restrictions": restrictions}

    def list_events(self, principal: Principal, vendor_code: str) -> list[dict[str, Any]]:
        principal.require("trust.read")
        vendor = self._vendor_by_code(self.connection, vendor_code)
        rows = self.connection.execute("SELECT * FROM trust_events WHERE vendor_id=? ORDER BY occurred_at,id", (vendor["id"],)).fetchall()
        return [self._render_event(dict(row), principal) for row in rows]

    # ------------------------------------------------------------------ scoring
    def score_detail(self, principal: Principal, vendor_code: str) -> dict[str, Any]:
        principal.require("trust.read")
        now_value = self.clock.now()
        vendor = self._vendor_by_code(self.connection, vendor_code)
        services: dict[str, Any] = {}
        all_entries: list[tuple[Any, ...]] = []
        with transaction(immediate=True) as connection:
            for service_type in CONCRETE_TYPES:
                entries = self._ledger_window(vendor["id"], service_type, now_value, connection=connection)
                all_entries.extend((service_type, item["event_id"], item["delta"], item["window_start"], item["window_end"]) for item in entries)
                active = self._active_restriction(vendor["id"], service_type, now_value, mutate=True, connection=connection)
                probation = active is None and self._within_recovery_guard(connection, vendor["id"], service_type, now_value)
                services[service_type] = {
                    "score": self._capped_sum(entries),
                    "raw_delta_sum": sum(item["delta"] for item in entries),
                    "threshold": SCORE_THRESHOLD,
                    "probation": probation,
                    "ledger": entries,
                    "restriction": self._restriction_view(active) if active else None,
                }
        return {
            "vendor_code": vendor_code,
            "base_score": BASE_SCORE,
            "window_days": WINDOW_DAYS,
            "as_of": to_storage(now_value),
            "services": services,
            "checksum": digest(sorted(all_entries)),
        }

    def replay_scores(self, principal: Principal) -> dict[str, Any]:
        """从不可变事件表重建评分台账，并校验重建前后校验和一致。"""
        principal.require("trust.manage")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            before_rows = connection.execute(
                "SELECT service_type,event_id,delta,window_start,window_end FROM trust_score_ledgers ORDER BY event_id,service_type"
            ).fetchall()
            before = digest([tuple(row) for row in before_rows])
            connection.execute("DELETE FROM trust_score_ledgers")
            events = connection.execute("SELECT * FROM trust_events ORDER BY occurred_at,id").fetchall()
            rebuilt = 0
            for event_row in events:
                rebuilt += len(self._write_ledgers(connection, dict(event_row)))
            after_rows = connection.execute(
                "SELECT service_type,event_id,delta,window_start,window_end FROM trust_score_ledgers ORDER BY event_id,service_type"
            ).fetchall()
            after = digest([tuple(row) for row in after_rows])
            result = {"before_checksum": before, "after_checksum": after, "matched": before == after, "events_replayed": len(events), "ledger_entries": rebuilt, "replayed_at": now}
            self._audit(connection, principal, "trust.score.replay", None, after=result)
        return result

    # --------------------------------------------------------------- candidates
    def query_candidates(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("trust.read")
        as_of = _parse_time(payload["scheduled_at"]) if payload.get("scheduled_at") else self.clock.now()
        target = payload["service_type"]
        rows = self.connection.execute("SELECT * FROM trust_vendors WHERE active=1 ORDER BY code").fetchall()
        eligible: list[dict[str, Any]] = []
        restricted: list[dict[str, Any]] = []
        with transaction(immediate=True) as connection:
            for row in rows:
                vendor = dict(row)
                if target not in vendor["service_types"].split(","):
                    continue
                active = self._active_restriction(vendor["id"], target, as_of, mutate=True, connection=connection)
                probation = active is None and self._within_recovery_guard(connection, vendor["id"], target, as_of)
                if active is None and not probation:
                    # 无隔离、也不在复核观察期：按当前评分决定是否需要重新隔离
                    created = self._ensure_auto_restriction(connection, vendor["id"], target, as_of, trigger_event_id=None)
                    if created is not None:
                        active_row = connection.execute("SELECT * FROM trust_restrictions WHERE id=?", (created["id"],)).fetchone()
                        active = active_row
                if active:
                    restricted.append({"vendor_code": vendor["code"], "origin": active["origin"], "reason": active["reason"], "effective_until": active["effective_until"]})
                    continue
                entries = self._ledger_window(vendor["id"], target, as_of, connection=connection)
                score = self._capped_sum(entries)
                positives = sum(1 for item in entries if item["delta"] > 0)
                eligible.append({
                    "vendor_code": vendor["code"],
                    "name": vendor["name"],
                    "contact_name": vendor["contact_name"],
                    "contact_phone": self._contact_phone(vendor["contact_phone"], principal),
                    "score": score,
                    "positive_events": positives,
                    "window_events": len(entries),
                    "probation": probation,
                    "score_reasons": [item["reason"] for item in sorted(entries, key=lambda item: (item["window_start"], item["event_id"]))],
                })
        # 稳定排序：评分降序、正向记录降序、编码升序
        eligible.sort(key=lambda item: (-item["score"], -item["positive_events"], item["vendor_code"]))
        return {"service_type": target, "as_of": to_storage(as_of), "threshold": SCORE_THRESHOLD,
                "items": eligible[: payload["limit"]], "restricted": sorted(restricted, key=lambda item: item["vendor_code"])}

    # ----------------------------------------------------------------- recovery
    def recover(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("trust.recover")
        if payload["service_type"] not in CONCRETE_TYPES:
            raise ValidationError("复核恢复必须指定具体场次类型（wedding/funeral）")
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            vendor = self._vendor_by_code(connection, payload["vendor_code"])
            active = self._active_restriction(vendor["id"], payload["service_type"], now_value, mutate=True, connection=connection)
            if active is None:
                raise ConflictError("该能力当前没有生效中的隔离记录，无需复核恢复")
            guard_until = to_storage(now_value + timedelta(days=REVIEW_GUARD_DAYS)) if payload["decision"] == "approved" else None
            cursor = connection.execute(
                "INSERT INTO trust_reviews(vendor_id,restriction_id,decision,note,guard_until,reviewer_user_id,reviewer_name,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (vendor["id"], active["id"], payload["decision"], payload["note"], guard_until, principal.user_id, principal.display_name, now),
            )
            review_id = cursor.lastrowid
            lifted = None
            if payload["decision"] == "approved":
                connection.execute(
                    "UPDATE trust_restrictions SET status='lifted',lifted_at=?,lifted_by=?,lift_reason=?,review_id=? WHERE id=?",
                    (now, principal.display_name, payload["note"], review_id, active["id"]),
                )
                lifted = self._restriction_view(dict(connection.execute("SELECT * FROM trust_restrictions WHERE id=?", (active["id"],)).fetchone()))
            result = {
                "review_id": review_id, "vendor_code": vendor["code"], "service_type": payload["service_type"],
                "decision": payload["decision"], "guard_until": guard_until, "restriction": lifted,
            }
            self._audit(connection, principal, "trust.recovery." + payload["decision"], vendor["code"], after=result)
        return result

    def manual_override(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("trust.manage")
        if payload["service_type"] not in CONCRETE_TYPES:
            raise ValidationError("人工覆盖必须指定具体场次类型（wedding/funeral）")
        if not payload.get("effective_until"):
            raise ValidationError("人工覆盖必须写明期限（effective_until）和原因")
        until = _parse_time(payload["effective_until"])
        now_value = self.clock.now()
        if until <= now_value:
            raise ValidationError("人工覆盖的期限必须晚于当前时间")
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            vendor = self._vendor_by_code(connection, payload["vendor_code"])
            if payload["action"] == "restrict":
                active = self._active_restriction(vendor["id"], payload["service_type"], now_value, mutate=True, connection=connection)
                if active is not None:
                    raise ConflictError("该能力已存在生效中的隔离记录")
                entries = self._ledger_window(vendor["id"], payload["service_type"], now_value, connection=connection)
                cursor = connection.execute(
                    "INSERT INTO trust_restrictions(vendor_id,service_type,status,origin,reason,score_at_creation,trigger_event_ids,created_by,created_at,effective_until)"
                    " VALUES(?,?, 'active','manual',?,?, '[]',?,?,?)",
                    (vendor["id"], payload["service_type"], payload["reason"], self._capped_sum(entries), principal.display_name, now, to_storage(until)),
                )
                restriction = dict(connection.execute("SELECT * FROM trust_restrictions WHERE id=?", (cursor.lastrowid,)).fetchone())
            else:
                active = self._active_restriction(vendor["id"], payload["service_type"], now_value, mutate=True, connection=connection)
                if active is None:
                    raise ConflictError("该能力当前没有生效中的隔离记录")
                # 人工解除同样是一次复核记录：观察期=填写期限，期内不再被自动触发。
                review_cursor = connection.execute(
                    "INSERT INTO trust_reviews(vendor_id,restriction_id,decision,note,guard_until,reviewer_user_id,reviewer_name,created_at) VALUES(?,?,'approved',?,?,?,?,?)",
                    (vendor["id"], active["id"], payload["reason"], to_storage(until), principal.user_id, principal.display_name, now),
                )
                connection.execute(
                    "UPDATE trust_restrictions SET status='lifted',lifted_at=?,lifted_by=?,lift_reason=?,review_id=? WHERE id=?",
                    (now, principal.display_name, payload["reason"], review_cursor.lastrowid, active["id"]),
                )
                restriction = dict(connection.execute("SELECT * FROM trust_restrictions WHERE id=?", (active["id"],)).fetchone())
            result = self._restriction_view(restriction)
            self._audit(connection, principal, "trust.override." + payload["action"], vendor["code"], after=result, metadata={"reason": payload["reason"]})
        return result

    # ------------------------------------------------------------ audit summary
    def audit_summary(self, principal: Principal) -> dict[str, Any]:
        principal.require("audit.read")
        rows = self.connection.execute(
            "SELECT action,outcome,COUNT(*) AS amount FROM audit_events WHERE action LIKE 'trust.%' GROUP BY action,outcome ORDER BY action,outcome"
        ).fetchall()
        breakdown: dict[str, dict[str, int]] = {}
        for row in rows:
            breakdown.setdefault(row["action"], {})[row["outcome"]] = int(row["amount"])
        total = sum(sum(group.values()) for group in breakdown.values())
        return {"total": total, "actions": breakdown}

    # ----------------------------------------------------------------- helpers
    def _write_ledgers(self, connection: sqlite3.Connection, event: dict[str, Any]) -> list[dict[str, Any]]:
        occurred = from_storage(event["occurred_at"])
        assert occurred is not None
        window_start = event["occurred_at"]
        window_end = to_storage(occurred + timedelta(days=WINDOW_DAYS))
        targets = CONCRETE_TYPES if event["service_type"] == "both" else (event["service_type"],)
        delta, reason = self._delta_and_reason(event)
        result: list[dict[str, Any]] = []
        for service_type in targets:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO trust_score_ledgers(vendor_id,event_id,service_type,window_start,window_end,delta,reason,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (event["vendor_id"], event["id"], service_type, window_start, window_end, delta, reason, to_storage(self.clock.now())),
            )
            if cursor.rowcount == 0:
                continue
            result.append({"event_id": event["id"], "service_type": service_type, "delta": delta, "window_start": window_start, "window_end": window_end, "reason": reason})
        return result

    @staticmethod
    def _delta_and_reason(event: dict[str, Any]) -> tuple[int, str]:
        source_weight = SOURCE_WEIGHTS[event["source"]]
        source_label = SOURCE_LABELS[event["source"]]
        type_label = TYPE_LABELS[event["event_type"]]
        if event["event_type"] == "positive":
            delta = POSITIVE_DELTA
            reason = f"{type_label}（{source_label}）+{delta}"
            return delta, reason
        base = PENALTIES[event["event_type"]][event["severity"]]
        delta = -int(round(base * source_weight))
        reason = f"{type_label}·{SEVERITY_LABELS[event['severity']]}（{source_label}权重{source_weight:g}）{delta}"
        return delta, reason

    def _ledger_window(self, vendor_id: int, service_type: str, as_of: datetime, connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        connection = connection or self.connection
        as_of_text = to_storage(as_of)
        rows = connection.execute(
            "SELECT event_id,window_start,window_end,delta,reason FROM trust_score_ledgers"
            " WHERE vendor_id=? AND service_type=? AND window_start<=? AND window_end>? ORDER BY window_start,event_id",
            (vendor_id, service_type, as_of_text, as_of_text),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _capped_sum(entries: list[dict[str, Any]]) -> int:
        return max(0, min(BASE_SCORE, BASE_SCORE + sum(int(item["delta"]) for item in entries)))

    def _active_restriction(self, vendor_id: int, service_type: str, as_of: datetime, *, mutate: bool, connection: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        connection = connection or self.connection
        as_of_text = to_storage(as_of)
        if mutate:
            connection.execute(
                "UPDATE trust_restrictions SET status='expired' WHERE vendor_id=? AND service_type=? AND status='active' AND effective_until IS NOT NULL AND effective_until<=?",
                (vendor_id, service_type, as_of_text),
            )
        return connection.execute(
            "SELECT * FROM trust_restrictions WHERE vendor_id=? AND service_type=? AND status='active' ORDER BY id LIMIT 1",
            (vendor_id, service_type),
        ).fetchone()

    def _apply_auto_restrictions(self, connection: sqlite3.Connection, vendor: sqlite3.Row, event: dict[str, Any], ledgers: list[dict[str, Any]], now_value: datetime) -> list[dict[str, Any]]:
        created: list[dict[str, Any]] = []
        for ledger in ledgers:
            view = self._ensure_auto_restriction(connection, vendor["id"], ledger["service_type"], now_value, trigger_event_id=event["id"])
            if view is not None:
                created.append(view)
        return created

    def _within_recovery_guard(self, connection: sqlite3.Connection, vendor_id: int, service_type: str, now_value: datetime) -> bool:
        latest_guard = connection.execute(
            "SELECT r.guard_until FROM trust_reviews r JOIN trust_restrictions t ON t.id=r.restriction_id"
            " WHERE r.vendor_id=? AND t.service_type=? AND r.decision='approved' AND r.guard_until IS NOT NULL ORDER BY r.id DESC LIMIT 1",
            (vendor_id, service_type),
        ).fetchone()
        return latest_guard is not None and bool(latest_guard["guard_until"]) and from_storage(latest_guard["guard_until"]) > now_value

    def _ensure_auto_restriction(self, connection: sqlite3.Connection, vendor_id: int, service_type: str, now_value: datetime, *, trigger_event_id: int | None) -> dict[str, Any] | None:
        if service_type not in CONCRETE_TYPES:
            raise ValidationError("隔离与恢复只能针对具体场次类型（wedding/funeral）")
        if self._active_restriction(vendor_id, service_type, now_value, mutate=True, connection=connection) is not None:
            return None
        # 复核通过后的观察期内不再被自动触发
        if self._within_recovery_guard(connection, vendor_id, service_type, now_value):
            return None
        entries = self._ledger_window(vendor_id, service_type, now_value, connection=connection)
        score = self._capped_sum(entries)
        if score >= SCORE_THRESHOLD:
            return None
        now = to_storage(now_value)
        triggers = json.dumps([trigger_event_id] if trigger_event_id is not None else [])
        cursor = connection.execute(
            "INSERT INTO trust_restrictions(vendor_id,service_type,status,origin,reason,score_at_creation,trigger_event_ids,created_by,created_at)"
            " VALUES(?,?,'active','auto',?,?,?,'system',?)",
            (vendor_id, service_type, f"滚动{WINDOW_DAYS}天可信度评分 {score} 低于阈值 {SCORE_THRESHOLD}，自动隔离相应场次能力", score, triggers, now),
        )
        return self._restriction_view(dict(connection.execute("SELECT * FROM trust_restrictions WHERE id=?", (cursor.lastrowid,)).fetchone()))

    @staticmethod
    def _restriction_view(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
        view = dict(row)
        if "trigger_event_ids" in view:
            try:
                view["trigger_event_ids"] = json.loads(view["trigger_event_ids"])
            except (TypeError, ValueError):
                pass
        return view

    # --------------------------------------------------------------- visibility
    def _render_vendor(self, vendor: dict[str, Any], principal: Principal) -> dict[str, Any]:
        return {
            "code": vendor["code"],
            "name": vendor["name"],
            "service_types": vendor["service_types"].split(",") if vendor["service_types"] else [],
            "active": bool(vendor["active"]),
            "contact_name": vendor["contact_name"],
            "contact_phone": self._contact_phone(vendor["contact_phone"], principal),
        }

    def _render_event(self, event: dict[str, Any], principal: Principal) -> dict[str, Any]:
        can_complaint = principal.can("trust.complaint.read")
        can_contact = principal.can("trust.contact.read")
        result = {
            "id": event["id"],
            "event_type": event["event_type"],
            "service_type": event["service_type"],
            "severity": event["severity"],
            "occurred_at": event["occurred_at"],
            "source": event["source"],
            "created_at": event["created_at"],
        }
        if event["event_type"] == "complaint" and not can_complaint:
            result.update({"detail": None, "evidence_ref": None, "reporter_contact": None, "detail_hidden": True})
            return result
        result["detail"] = event["detail"]
        result["evidence_ref"] = event["evidence_ref"]
        result["detail_hidden"] = False
        contact = event["reporter_contact"]
        if not contact:
            result["reporter_contact"] = contact
        elif can_contact:
            result["reporter_contact"] = contact
        else:
            result["reporter_contact"] = self._mask_contact(contact)
        return result

    @staticmethod
    def _contact_phone(phone: str, principal: Principal) -> str:
        if not phone:
            return phone
        if principal.can("trust.contact.read"):
            return phone
        return mask_phone(phone) or phone

    @staticmethod
    def _mask_contact(value: str) -> str:
        masked = mask_phone(value)
        return masked if any(char.isdigit() for char in value) else "***"

    def _audit(self, connection: sqlite3.Connection, principal: Principal, action: str, resource_id: str | None, *, after: dict | None = None, metadata: dict | None = None) -> None:
        AuditService(connection, self.clock).record(
            AuditContext(principal.user_id, principal.display_name),
            action=action, resource_type="trust_vendor", resource_id=resource_id,
            outcome="success", after=after, metadata=metadata,
        )


def audit_denied(principal: Principal, action: str, vendor_code: str | None = None) -> None:
    try:
        AuditService(get_connection()).record(
            AuditContext(principal.user_id, principal.display_name),
            action=action, resource_type="trust_vendor", resource_id=vendor_code, outcome="denied",
        )
    except Exception:
        pass
