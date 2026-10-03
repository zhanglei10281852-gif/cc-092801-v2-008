from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

UNIQUE_OCCURRENCE = "UNIQUE constraint failed: trust_events.vendor_id, trust_events.occurrence_key"


class TrustRepository:
    """供应商可信度台账的 SQLite 读写。事件只追加、不改分，保证可回放。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # -- 供应商 ----------------------------------------------------------

    def create_vendor(self, *, code: str, name: str, contact_name: str, phone: str, capabilities: list[str], note: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO trust_vendors(code,name,contact_name,phone,capabilities_json,note,active,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,'1',?,?)",
            (code, name, contact_name, phone, json.dumps(capabilities, ensure_ascii=False), note, now, now),
        )
        return self.require_vendor(int(cursor.lastrowid))

    def vendor_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM trust_vendors WHERE code=?", (code,)).fetchone()

    def vendor_by_id(self, vendor_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM trust_vendors WHERE id=?", (vendor_id,)).fetchone()

    def require_vendor(self, vendor_id: int) -> dict[str, Any]:
        row = self.vendor_by_id(vendor_id)
        if row is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("供应商不存在")
        return dict(row)

    def list_vendors(self, *, active_only: bool = True, capability: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trust_vendors"
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("active=1")
        if capability:
            clauses.append("EXISTS(SELECT 1 FROM json_each(capabilities_json) WHERE value=?)")
            params.append(capability)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    # -- 事件台账 --------------------------------------------------------

    def insert_event(self, data: dict[str, Any]) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO trust_events(vendor_id,event_type,ceremony_type,severity,source,occurrence_key,"
            "occurrence_at,points,weight,policy_version,reporter,evidence_ref,detail_json,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                data["vendor_id"], data["event_type"], data["ceremony_type"], data["severity"], data["source"],
                data["occurrence_key"], data["occurrence_at"], data["points"], data["weight"],
                data["policy_version"], data["reporter"], data["evidence_ref"],
                json.dumps(data["detail"], ensure_ascii=False, sort_keys=True), data["created_at"],
            ),
        )
        return dict(self.connection.execute("SELECT * FROM trust_events WHERE id=?", (cursor.lastrowid,)).fetchone())

    def event_by_occurrence(self, vendor_id: int, occurrence_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM trust_events WHERE vendor_id=? AND occurrence_key=?",
            (vendor_id, occurrence_key),
        ).fetchone()

    def event_by_id(self, vendor_id: int, event_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM trust_events WHERE id=? AND vendor_id=?", (event_id, vendor_id)
        ).fetchone()

    def list_events(self, vendor_id: int, *, ceremony_type: str | None = None) -> list[sqlite3.Row]:
        if ceremony_type:
            return self.connection.execute(
                "SELECT * FROM trust_events WHERE vendor_id=? AND ceremony_type=? ORDER BY occurrence_at,id",
                (vendor_id, ceremony_type),
            ).fetchall()
        return self.connection.execute(
            "SELECT * FROM trust_events WHERE vendor_id=? ORDER BY occurrence_at,id", (vendor_id,)
        ).fetchall()

    def events_for_window(self, vendor_id: int, ceremony_type: str, since: str, until: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM trust_events WHERE vendor_id=? AND ceremony_type=? AND dismissed=0"
            " AND occurrence_at>=? AND occurrence_at<=? ORDER BY occurrence_at,id",
            (vendor_id, ceremony_type, since, until),
        ).fetchall()

    def mark_event_dismissed(self, event_id: int, *, actor: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE trust_events SET dismissed=1,dismissed_by=?,dismissed_reason=? WHERE id=?",
            (actor, reason, event_id),
        )

    # -- 处置决策 --------------------------------------------------------

    def insert_decision(self, data: dict[str, Any]) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO trust_decisions(vendor_id,ceremony_type,kind,reason,event_ids_json,status,"
            "effective_from,expires_at,created_by,created_at,reviewed_at,review_note)"
            " VALUES(?,?,?,?,?,'active',?,?,?,?,?,'')",
            (
                data["vendor_id"], data["ceremony_type"], data["kind"], data["reason"],
                json.dumps(data.get("event_ids", []), ensure_ascii=False),
                data["effective_from"], data.get("expires_at"), data["created_by"], data["created_at"],
                data.get("reviewed_at"),
            ),
        )
        return dict(self.connection.execute("SELECT * FROM trust_decisions WHERE id=?", (cursor.lastrowid,)).fetchone())

    def active_decisions(self, vendor_id: int, ceremony_type: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM trust_decisions WHERE vendor_id=? AND ceremony_type=? AND status='active' ORDER BY id",
            (vendor_id, ceremony_type),
        ).fetchall()

    def supersede_decisions(self, decision_ids: Iterable[int]) -> None:
        ids = list(decision_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.connection.execute(f"UPDATE trust_decisions SET status='superseded' WHERE id IN ({placeholders})", ids)

    def decision_by_id(self, decision_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM trust_decisions WHERE id=?", (decision_id,)).fetchone()

    def record_review(self, decision_id: int, reviewed_at: str, note: str) -> None:
        self.connection.execute(
            "UPDATE trust_decisions SET reviewed_at=?,review_note=? WHERE id=?",
            (reviewed_at, note, decision_id),
        )
