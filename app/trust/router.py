from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import current_principal
from app.core.errors import PermissionDeniedError
from app.core.security import Principal
from app.trust.schemas import CandidateQuery, EventCreate, ManualOverride, RecoveryRequest, VendorCreate
from app.trust.service import TrustService, audit_denied

router = APIRouter(prefix="/api/trust", tags=["供应商可信度"])


def service() -> TrustService:
    return TrustService()


def require(principal: Principal, permission: str, action: str) -> None:
    if not principal.can(permission):
        audit_denied(principal, action)
        raise PermissionDeniedError(f"缺少权限：{permission}")


@router.post("/vendors", status_code=201)
def register_vendor(payload: VendorCreate, principal: Principal = Depends(current_principal)) -> dict:
    require(principal, "trust.manage", "trust.vendor.register")
    return service().register_vendor(principal, payload.model_dump())


@router.get("/vendors")
def list_vendors(principal: Principal = Depends(current_principal)) -> dict:
    require(principal, "trust.read", "trust.vendor.list")
    return {"items": service().list_vendors(principal)}


@router.get("/vendors/{vendor_code}/events")
def list_events(vendor_code: str, principal: Principal = Depends(current_principal)) -> dict:
    require(principal, "trust.read", "trust.event.list")
    return {"items": service().list_events(principal, vendor_code)}


@router.get("/vendors/{vendor_code}/score")
def score_detail(vendor_code: str, principal: Principal = Depends(current_principal)) -> dict:
    require(principal, "trust.read", "trust.score.read")
    return service().score_detail(principal, vendor_code)


@router.post("/events", status_code=202)
def submit_event(payload: EventCreate, principal: Principal = Depends(current_principal)) -> dict:
    require(principal, "trust.event.write", "trust.event.submit")
    return service().submit_event(principal, payload.model_dump())


@router.post("/candidates/query")
def query_candidates(payload: CandidateQuery, principal: Principal = Depends(current_principal)) -> dict:
    require(principal, "trust.read", "trust.candidates.query")
    return service().query_candidates(principal, payload.model_dump())


@router.post("/recovery")
def recover(payload: RecoveryRequest, principal: Principal = Depends(current_principal)) -> dict:
    require(principal, "trust.recover", "trust.recovery")
    return service().recover(principal, payload.model_dump())


@router.post("/overrides")
def manual_override(payload: ManualOverride, principal: Principal = Depends(current_principal)) -> dict:
    require(principal, "trust.manage", "trust.override")
    return service().manual_override(principal, payload.model_dump())


@router.post("/scores/replay")
def replay_scores(principal: Principal = Depends(current_principal)) -> dict:
    require(principal, "trust.manage", "trust.score.replay")
    return service().replay_scores(principal)


@router.get("/audit-summary")
def audit_summary(principal: Principal = Depends(current_principal)) -> dict:
    require(principal, "audit.read", "trust.audit_summary")
    return service().audit_summary(principal)
