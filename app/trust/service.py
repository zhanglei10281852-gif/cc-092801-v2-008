"""供应商可信度评分、隔离与复核服务。

设计要点：
- 事件只追加（append-only），扣分权重随事件冻结；评分通过窗口内事件顺序
  折叠得到，任意时刻可重放，重启后状态一致。
- 隔离只作用于具体场次类型（wedding/funeral），不连带其他能力。
- 人工处置必须写明原因与期限；恢复需要复核动作，事件被否决后不删除，
- 只标记 dismissed，重放时排除，全程留痕。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.database import get_connection, transaction
from app.services.audit import AuditContext, AuditService
from app.trust import policy
from app.trust.repository import TrustRepository

MAX_BLOCK_DAYS = 180
MAX_COOLING_DAYS = 90
MAX_PROBATION_DAYS = 60


@dataclass(frozen=True, slots=True)
class Viewer:
    """调用方的可见性分级，由 Principal 的权限推导。"""

    user_id: int
    name: str
    permissions: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return "*" in self.permissions or "trust.write" in self.permissions

    @property
    def can_dispatch(self) -> bool:
        return self.is_admin or "trust.dispatch" in self.permissions

    @property
    def can_review(self) -> bool:
        return self.is_admin or "trust.review" in self.permissions

    @property
    def can_read(self) -> bool:
        return self.is_admin or bool(self.permissions & {"trust.read", "trust.dispatch", "trust.review"})

    @property
    def tier(self) -> str:
        if self.is_admin:
            return "admin"
        if self.can_review:
            return "reviewer"
        if self.can_dispatch:
            return "dispatcher"
        return "reader"

    def audit_context(self) -> AuditContext:
        return AuditContext(self.user_id, self.name)


class TrustService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = TrustRepository(self.connection)
        self.audit = AuditService(self.connection, self.clock)

    # -- 供应商档案 ------------------------------------------------------

    def register_vendor(self, payload: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        if not viewer.is_admin:
            raise PermissionDeniedError("只有具备 trust.write 权限的人员可以登记供应商")
        capabilities = sorted(set(payload["capabilities"]))
        unknown = [item for item in capabilities if item not in policy.CEREMONY_TYPES]
        if unknown:
            raise ValidationError("能力项只支持 wedding / funeral", context={"unknown": unknown})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = TrustRepository(connection)
            if repository.vendor_by_code(payload["code"]):
                raise ConflictError("供应商编码已存在")
            vendor = repository.create_vendor(
                code=payload["code"], name=payload["name"], contact_name=payload.get("contact_name", ""),
                phone=payload.get("phone", ""), capabilities=capabilities, note=payload.get("note", ""), now=now,
            )
            AuditService(connection, self.clock).record(
                viewer.audit_context(), action="trust.vendor.register", resource_type="trust_vendor",
                resource_id=vendor["id"], after={"code": vendor["code"], "name": vendor["name"], "capabilities": capabilities},
            )
        return self._vendor_view(vendor, viewer)

    # -- 事件上报 --------------------------------------------------------

    def report_event(self, payload: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        if not (viewer.can_dispatch):
            raise PermissionDeniedError("当前账号不能上报可信度事件")
        policy.validate_event(payload["event_type"], payload["ceremony_type"], payload["severity"], payload["source"])
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        occurrence_dt = from_storage(payload["occurrence_at"]) if payload.get("occurrence_at") else now_dt
        if occurrence_dt is None:
            occurrence_dt = now_dt
        if occurrence_dt > now_dt + timedelta(minutes=5):
            raise ValidationError("事件发生时间不能晚于当前时间")
        occurrence_at = to_storage(occurrence_dt)
        points, weight = policy.event_delta(payload["event_type"], payload["severity"], payload["source"])
        with transaction(immediate=True) as connection:
            repository = TrustRepository(connection)
            vendor = repository.vendor_by_code(payload["vendor_code"])
            if vendor is None:
                raise NotFoundError("供应商不存在")
            # 幂等：同一供应商的同一 occurrence_key 只入账一次，重复上报直接返回原事件。
            existing = repository.event_by_occurrence(vendor["id"], payload["occurrence_key"])
            if existing is not None:
                AuditService(connection, self.clock).record(
                    viewer.audit_context(), action="trust.event.duplicate", resource_type="trust_event",
                    resource_id=existing["id"], outcome="denied",
                    metadata={"vendor_id": vendor["id"], "occurrence_key": payload["occurrence_key"]},
                )
                return {"duplicate": True, "event": self._event_view(dict(existing), viewer)}
            event = repository.insert_event({
                "vendor_id": vendor["id"], "event_type": payload["event_type"],
                "ceremony_type": payload["ceremony_type"], "severity": payload["severity"],
                "source": payload["source"], "occurrence_key": payload["occurrence_key"],
                "occurrence_at": occurrence_at, "points": points, "weight": weight,
                "policy_version": policy.POLICY_VERSION, "reporter": viewer.name,
                "evidence_ref": payload.get("evidence_ref", ""), "detail": payload.get("detail", {}),
                "created_at": now,
            })
            decision = self._maybe_auto_restrict(repository, vendor["id"], payload["ceremony_type"], [event["id"]], now)
            AuditService(connection, self.clock).record(
                viewer.audit_context(), action="trust.event.report", resource_type="trust_event",
                resource_id=event["id"], after={"event_type": event["event_type"], "points": event["points"],
                                                "ceremony_type": event["ceremony_type"], "auto_restricted": decision is not None},
            )
            return {"duplicate": False, "event": self._event_view(event, viewer),
                    "auto_decision": self._decision_view(decision, viewer) if decision else None}

    # -- 评分与回放 ------------------------------------------------------

    def scorecard(self, vendor_code: str, viewer: Viewer) -> dict[str, Any]:
        if not viewer.can_read:
            raise PermissionDeniedError("当前账号不能查看可信度评分")
        vendor = self._require_vendor_by_code(vendor_code)
        now_dt = self.clock.now()
        windows = self._compute_windows(vendor["id"], now_dt)
        return {"vendor": self._vendor_view(vendor, viewer), "policy_version": policy.POLICY_VERSION,
                "evaluated_at": to_storage(now_dt), "windows": windows,
                "ceremonies": {kind: self._ceremony_state(vendor["id"], kind, now_dt, windows) for kind in policy.CEREMONY_TYPES}}

    def list_events(self, vendor_code: str, viewer: Viewer, *, ceremony_type: str | None = None) -> dict[str, Any]:
        if not viewer.can_read:
            raise PermissionDeniedError("当前账号不能查看可信度事件")
        if ceremony_type is not None:
            self._validate_ceremony(ceremony_type)
        vendor = self._require_vendor_by_code(vendor_code)
        rows = self.repository.list_events(vendor["id"], ceremony_type=ceremony_type)
        return {"vendor_code": vendor_code, "viewer_tier": viewer.tier,
                "items": [self._event_view(dict(row), viewer) for row in rows]}

    def _compute_windows(self, vendor_id: int, now_dt) -> dict[str, Any]:
        return {str(days): self._replay_window(vendor_id, days=days, now_dt=now_dt) for days in policy.WINDOWS_DAYS}

    def _replay_window(self, vendor_id: int, *, days: int, now_dt) -> dict[str, Any]:
        """按场次类型重放窗口内事件：事件按时间顺序折叠，结果确定可复现。"""
        repository = self.repository
        since = to_storage(now_dt - timedelta(days=days))
        until = to_storage(now_dt)
        reset_by_ceremony = self._restriction_reset_at(repository, vendor_id)
        result: dict[str, Any] = {}
        for ceremony in policy.CEREMONY_TYPES:
            rows = repository.events_for_window(vendor_id, ceremony, since, until)
            contributions = [self._contribution(row) for row in rows]
            evaluation = policy.evaluate_thresholds(contributions)
            reset_at = reset_by_ceremony.get(ceremony)
            restrictable = [item for item in contributions if reset_at is None or item["occurrence_at"] > reset_at]
            restrictable_eval = policy.evaluate_thresholds(restrictable)
            result[ceremony] = {
                "window_days": days, "since": since, "until": until,
                "score": policy.clamp_score(evaluation["penalty"]),
                "penalty_total": evaluation["penalty"],
                "penalty_after_last_reset": restrictable_eval["penalty"],
                "restriction_reset_at": reset_at,
                # 降温只看复核放行后新增的扣分；历史贡献仍完整列出用于解释。
                "cooling": restrictable_eval["cooling"], "threshold_restricted": evaluation["restricted"],
                "severe_complaints": evaluation["complaint_count_severe"],
                "contributions": contributions,
            }
        return result

    @staticmethod
    def _contribution(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["id"], "event_type": row["event_type"], "ceremony_type": row["ceremony_type"],
            "severity": row["severity"], "source": row["source"], "weight": row["weight"],
            "points": row["points"], "policy_version": row["policy_version"],
            "occurrence_at": row["occurrence_at"], "occurrence_key": row["occurrence_key"],
            "evidence_ref": row["evidence_ref"],
        }

    def _restriction_reset_at(self, repository: TrustRepository, vendor_id: int) -> dict[str, str | None]:
        """各场次类型最近一次复核放行（观察期/正式恢复）的时间，即重新计罚起点。"""
        reset: dict[str, str | None] = {}
        for ceremony in policy.CEREMONY_TYPES:
            row = repository.connection.execute(
                "SELECT created_at FROM trust_decisions WHERE vendor_id=? AND ceremony_type=?"
                " AND kind IN ('restore','probation_allow') ORDER BY id DESC LIMIT 1",
                (vendor_id, ceremony),
            ).fetchone()
            reset[ceremony] = row["created_at"] if row else None
        return reset

    # -- 候选供应商（稳定排序 + 能力隔离） --------------------------------

    def candidates(self, ceremony_type: str, viewer: Viewer, *, capability: str | None = None) -> dict[str, Any]:
        if not viewer.can_dispatch:
            raise PermissionDeniedError("当前账号不能查询候选供应商")
        if ceremony_type not in policy.CEREMONY_TYPES:
            raise ValidationError("场次类型不合法")
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        items: list[dict[str, Any]] = []
        for vendor in self.repository.list_vendors(active_only=True, capability=capability):
            caps = json.loads(vendor["capabilities_json"] or "[]")
            if ceremony_type not in caps:
                continue
            windows = self._compute_windows(vendor["id"], now_dt)
            state = self._ceremony_state(vendor["id"], ceremony_type, now_dt, windows)
            if not state["eligible"]:
                continue
            window90 = windows["90"][ceremony_type]
            window30 = windows["30"][ceremony_type]
            items.append({
                "vendor": self._vendor_view(vendor, viewer),
                "score_90d": window90["score"], "score_30d": window30["score"],
                "penalty_30d": window30["penalty_total"],
                "cooling": state["cooling"], "probation": state["probation"],
                "reasons": state["reasons"],
                "_sort": (0 if not state["cooling"] else 1, -window90["score"], -window30["score"],
                          window30["penalty_total"], vendor["id"]),
            })
        items.sort(key=lambda item: item["_sort"])
        for item in items:
            item.pop("_sort", None)
        return {"ceremony_type": ceremony_type, "evaluated_at": now, "policy_version": policy.POLICY_VERSION,
                "count": len(items), "items": items}

    def _ceremony_state(self, vendor_id: int, ceremony: str, now_dt, windows: dict[str, Any] | None = None) -> dict[str, Any]:
        """某场次类型当前的有效处置状态（过期决策自动失效，不需要后台任务）。"""
        now = to_storage(now_dt)
        if windows is None:
            windows = self._compute_windows(vendor_id, now_dt)
        window = windows[str(policy.DEFAULT_WINDOW_DAYS)][ceremony]
        decisions = [dict(row) for row in self.repository.active_decisions(vendor_id, ceremony)]
        in_effect = [d for d in decisions if not d["expires_at"] or d["expires_at"] > now]
        kinds = {d["kind"] for d in in_effect}
        reasons: list[str] = []
        blocked = False
        probation = False
        manual_cooling = False
        if "manual_block" in kinds:
            blocked = True
            reasons.append("人工停用（限期）")
        if "auto_restrict" in kinds:
            blocked = True
            reasons.append("可信度阈值触发自动隔离，待复核")
        if "probation_allow" in kinds:
            probation = True
            reasons.append("复核后观察期")
        if "manual_cooling" in kinds:
            manual_cooling = True
            reasons.append("人工降温（限期）")
        cooling = (not blocked) and (manual_cooling or probation or bool(window["cooling"]))
        if not blocked and window["cooling"] and "人工降温（限期）" not in reasons and not probation:
            reasons.append(f"近{policy.DEFAULT_WINDOW_DAYS}天扣分达到降温阈值")
        return {"eligible": not blocked, "blocked": blocked, "cooling": cooling, "probation": probation,
                "reasons": reasons, "active_decisions": [self._decision_view(d, Viewer(0, "system", frozenset())) for d in in_effect],
                "window_signal": {"cooling": window["cooling"], "threshold_restricted": window["threshold_restricted"],
                                  "penalty_total": window["penalty_total"], "penalty_after_last_reset": window["penalty_after_last_reset"]}}

    # -- 自动隔离 --------------------------------------------------------

    def _maybe_auto_restrict(self, repository: TrustRepository, vendor_id: int, ceremony: str, trigger_event_ids: list[int], now: str) -> dict[str, Any] | None:
        state = self._ceremony_state(vendor_id, ceremony, self.clock.now())
        if state["blocked"]:
            return None
        window = self._compute_windows(vendor_id, self.clock.now())[str(policy.DEFAULT_WINDOW_DAYS)][ceremony]
        # 只统计最近一次复核放行之后的新增扣分，避免恢复后被旧账反复触发。
        reset_at = window["restriction_reset_at"]
        fresh = [item for item in window["contributions"] if reset_at is None or item["occurrence_at"] > reset_at]
        evaluation = policy.evaluate_thresholds(fresh)
        if not evaluation["restricted"]:
            return None
        contributor_ids = [item["event_id"] for item in fresh if item["points"] > 0]
        reason = (f"近{policy.DEFAULT_WINDOW_DAYS}天新增扣分 {evaluation['penalty']} "
                  f"达到隔离阈值 {policy.RESTRICT_THRESHOLD}（严重投诉 {evaluation['complaint_count_severe']} 起）")
        return repository.insert_decision({
            "vendor_id": vendor_id, "ceremony_type": ceremony, "kind": "auto_restrict", "reason": reason,
            "event_ids": sorted(set(contributor_ids + trigger_event_ids)), "effective_from": now,
            "expires_at": None, "created_by": "system", "created_at": now,
        })

    # -- 人工覆盖（必须写期限与原因） -------------------------------------

    def manual_override(self, payload: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        if not viewer.is_admin:
            raise PermissionDeniedError("只有具备 trust.write 权限的人员可以人工覆盖")
        self._validate_ceremony(payload["ceremony_type"])
        kind = payload["kind"]
        if kind not in {"manual_block", "manual_cooling"}:
            raise ValidationError("人工覆盖类型不合法")
        expires_dt = from_storage(payload["expires_at"])
        if expires_dt is None:
            raise ValidationError("人工覆盖必须写明期限 expires_at")
        max_days = MAX_BLOCK_DAYS if kind == "manual_block" else MAX_COOLING_DAYS
        now_dt = self.clock.now()
        if expires_dt <= now_dt:
            raise ValidationError("期限必须晚于当前时间")
        if expires_dt > now_dt + timedelta(days=max_days):
            raise ValidationError(f"单次人工覆盖期限不能超过 {max_days} 天")
        reason = payload.get("reason", "").strip()
        if len(reason) < 4:
            raise ValidationError("人工覆盖必须写明原因")
        now = to_storage(now_dt)
        with transaction(immediate=True) as connection:
            repository = TrustRepository(connection)
            vendor = self._require_vendor_code(connection, payload["vendor_code"])
            decision = repository.insert_decision({
                "vendor_id": vendor["id"], "ceremony_type": payload["ceremony_type"], "kind": kind,
                "reason": reason, "event_ids": [], "effective_from": now,
                "expires_at": to_storage(expires_dt), "created_by": viewer.name, "created_at": now,
            })
            AuditService(connection, self.clock).record(
                viewer.audit_context(), action=f"trust.override.{kind}", resource_type="trust_decision",
                resource_id=decision["id"], after={"vendor_id": vendor["id"], "ceremony_type": payload["ceremony_type"],
                                                    "reason": reason, "expires_at": decision["expires_at"]},
            )
        return self._decision_view(decision, viewer)

    # -- 复核恢复 --------------------------------------------------------

    def review(self, payload: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        if not viewer.can_review:
            raise PermissionDeniedError("当前账号不能执行复核操作")
        self._validate_ceremony(payload["ceremony_type"])
        reason = payload.get("reason", "").strip()
        if len(reason) < 4:
            raise ValidationError("复核操作必须写明原因")
        mode = payload["mode"]
        if mode not in {"restore", "probation"}:
            raise ValidationError("复核模式必须为 restore 或 probation")
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        probation_expires: str | None = None
        if mode == "probation":
            expires_dt = from_storage(payload["expires_at"])
            if expires_dt is None:
                raise ValidationError("观察期恢复必须写明期限 expires_at")
            if not now_dt < expires_dt <= now_dt + timedelta(days=MAX_PROBATION_DAYS):
                raise ValidationError(f"观察期期限必须在当前时间之后且不超过 {MAX_PROBATION_DAYS} 天")
            probation_expires = to_storage(expires_dt)
        with transaction(immediate=True) as connection:
            repository = TrustRepository(connection)
            vendor = self._require_vendor_code(connection, payload["vendor_code"])
            active = [dict(row) for row in repository.active_decisions(vendor["id"], payload["ceremony_type"])]
            removable = [d for d in active if d["kind"] in {"auto_restrict", "manual_block", "manual_cooling", "probation_allow"}]
            if not removable:
                raise ConflictError("该场次类型当前没有可恢复的隔离或降温记录")
            auto = [d for d in removable if d["kind"] in {"auto_restrict", "manual_block"}]
            if mode == "restore" and auto and not self._observation_satisfied(repository, vendor["id"], payload["ceremony_type"], auto, now_dt):
                raise ConflictError(f"隔离生效未满 {policy.COOLING_OBSERVATION_DAYS} 天观察期，可先选择观察期恢复")
            repository.supersede_decisions([d["id"] for d in removable])
            for decision in removable:
                repository.record_review(decision["id"], now, reason)
            kind = "probation_allow" if mode == "probation" else "restore"
            decision = repository.insert_decision({
                "vendor_id": vendor["id"], "ceremony_type": payload["ceremony_type"], "kind": kind,
                "reason": reason, "event_ids": [], "effective_from": now,
                "expires_at": probation_expires, "created_by": viewer.name, "created_at": now,
                "reviewed_at": now,
            })
            AuditService(connection, self.clock).record(
                viewer.audit_context(), action=f"trust.review.{mode}", resource_type="trust_decision",
                resource_id=decision["id"], before={"superseded": [d["id"] for d in removable]},
                after={"vendor_id": vendor["id"], "ceremony_type": payload["ceremony_type"], "reason": reason,
                       "expires_at": probation_expires},
            )
        return self._decision_view(decision, viewer)

    def _observation_satisfied(self, repository: TrustRepository, vendor_id: int, ceremony: str, decisions: list[dict[str, Any]], now_dt) -> bool:
        earliest = min(from_storage(d["effective_from"]) for d in decisions)
        return earliest is not None and now_dt >= earliest + timedelta(days=policy.COOLING_OBSERVATION_DAYS)

    def dismiss_event(self, payload: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        if not viewer.can_review:
            raise PermissionDeniedError("当前账号不能否决事件")
        reason = payload.get("reason", "").strip()
        if len(reason) < 4:
            raise ValidationError("否决事件必须写明原因")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = TrustRepository(connection)
            vendor = self._require_vendor_code(connection, payload["vendor_code"])
            row = repository.event_by_id(vendor["id"], payload["event_id"])
            if row is None:
                raise NotFoundError("事件不存在")
            if row["dismissed"]:
                raise ConflictError("该事件已被否决")
            repository.mark_event_dismissed(row["id"], actor=viewer.name, reason=reason)
            decision = repository.insert_decision({
                "vendor_id": vendor["id"], "ceremony_type": row["ceremony_type"], "kind": "dismiss_event",
                "reason": reason, "event_ids": [row["id"]], "effective_from": now, "expires_at": None,
                "created_by": viewer.name, "created_at": now, "reviewed_at": now,
            })
            AuditService(connection, self.clock).record(
                viewer.audit_context(), action="trust.event.dismiss", resource_type="trust_event",
                resource_id=row["id"], after={"vendor_id": vendor["id"], "reason": reason},
            )
        return self._decision_view(decision, viewer)

    # -- 审计摘要 --------------------------------------------------------

    def audit_summary(self, vendor_code: str | None, viewer: Viewer, *, limit: int = 20) -> dict[str, Any]:
        if not viewer.can_read:
            raise PermissionDeniedError("当前账号不能查看可信度审计摘要")
        connection = self.connection
        vendor_id: int | None = None
        if vendor_code:
            vendor_id = self._require_vendor_by_code(vendor_code)["id"]
            scope = (
                " AND ((resource_type='trust_event' AND resource_id IN (SELECT CAST(id AS TEXT) FROM trust_events WHERE vendor_id=?))"
                " OR (resource_type='trust_decision' AND resource_id IN (SELECT CAST(id AS TEXT) FROM trust_decisions WHERE vendor_id=?))"
                " OR (resource_type='trust_vendor' AND resource_id=CAST(? AS TEXT)))"
            )
            scope_params: list[Any] = [vendor_id, vendor_id, vendor_id]
        else:
            scope, scope_params = "", []
        rows = connection.execute(
            "SELECT action,outcome,COUNT(*) AS amount FROM audit_events"
            " WHERE resource_type IN ('trust_event','trust_decision','trust_vendor')"
            + scope
            + " GROUP BY action,outcome ORDER BY action,outcome",
            scope_params,
        ).fetchall()
        decisions_summary = connection.execute(
            "SELECT ceremony_type,kind,status,COUNT(*) AS amount FROM trust_decisions"
            + (" WHERE vendor_id=?" if vendor_id is not None else "")
            + " GROUP BY ceremony_type,kind,status ORDER BY ceremony_type,kind,status",
            ([vendor_id] if vendor_id is not None else []),
        ).fetchall()
        recent_decisions = connection.execute(
            "SELECT * FROM trust_decisions" + (" WHERE vendor_id=?" if vendor_id is not None else "")
            + " ORDER BY id DESC LIMIT ?",
            ([vendor_id, limit] if vendor_id is not None else [limit]),
        ).fetchall()
        return {
            "policy_version": policy.POLICY_VERSION,
            "audit_counts": [{"action": r["action"], "outcome": r["outcome"], "amount": r["amount"]} for r in rows],
            "decisions_summary": [dict(r) for r in decisions_summary],
            "recent_decisions": [self._decision_view(dict(r), viewer) for r in recent_decisions],
        }

    # -- 视图分级 --------------------------------------------------------

    def _vendor_view(self, vendor: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        from app.core.privacy import mask_phone

        result = {key: vendor[key] for key in ("id", "code", "name", "contact_name", "note", "active")}
        result["capabilities"] = json.loads(vendor.get("capabilities_json") or "[]")
        # 联系方式分级：调度与管理员可见完整手机号；复核/只读看到掩码。
        if viewer.can_dispatch:
            result["phone"] = vendor.get("phone", "")
        else:
            result["phone"] = mask_phone(vendor.get("phone") or "") or vendor.get("phone", "")
        result["contact_visible"] = viewer.can_dispatch
        result["viewer_tier"] = viewer.tier
        return result

    def _event_view(self, event: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        result = {key: event[key] for key in ("id", "vendor_id", "event_type", "ceremony_type", "severity",
                                              "source", "occurrence_key", "occurrence_at", "points", "weight",
                                              "policy_version", "evidence_ref", "dismissed")}
        # 投诉细节分级：复核/管理员可见完整细节；调度/只读仅可见类型与级别，不展示客户叙述。
        if viewer.can_review:
            result["detail"] = json.loads(event.get("detail_json") or "{}")
            result["reporter"] = event.get("reporter", "")
            if event.get("dismissed_reason"):
                result["dismissed_by"] = event["dismissed_by"]
                result["dismissed_reason"] = event["dismissed_reason"]
        else:
            result["detail"] = {"redacted": True}
        return result

    def _decision_view(self, decision: dict[str, Any] | None, viewer: Viewer) -> dict[str, Any] | None:
        if decision is None:
            return None
        result = {key: decision[key] for key in ("id", "vendor_id", "ceremony_type", "kind", "reason", "status",
                                                 "effective_from", "expires_at", "created_by", "created_at",
                                                 "reviewed_at", "review_note")}
        result["event_ids"] = json.loads(decision.get("event_ids_json") or "[]")
        return result

    # -- 辅助 ------------------------------------------------------------

    def _require_vendor_by_code(self, code: str) -> dict[str, Any]:
        vendor = self.repository.vendor_by_code(code)
        if vendor is None:
            raise NotFoundError("供应商不存在")
        return dict(vendor)

    def _require_vendor_code(self, connection: sqlite3.Connection, code: str) -> dict[str, Any]:
        row = TrustRepository(connection).vendor_by_code(code)
        if row is None:
            raise NotFoundError("供应商不存在")
        return dict(row)

    @staticmethod
    def _validate_ceremony(ceremony: str) -> None:
        if ceremony not in policy.CEREMONY_TYPES:
            raise ValidationError("场次类型不合法")
