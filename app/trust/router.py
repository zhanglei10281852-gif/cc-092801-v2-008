from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection
from app.trust.schemas import DismissEventRequest, EventReport, ManualOverride, ReviewRequest, VendorRegister
from app.trust.service import TrustService, Viewer

router = APIRouter(prefix="/api/trust", tags=["供应商可信度与隔离"])


def viewer_from(principal: Principal) -> Viewer:
    return Viewer(user_id=principal.user_id, name=principal.display_name, permissions=principal.permissions)


def service() -> TrustService:
    return TrustService(get_connection())


@router.post("/vendors", status_code=201)
def register_vendor(payload: VendorRegister, principal: Principal = Depends(current_principal)):
    return service().register_vendor(payload.model_dump(), viewer_from(principal))


@router.post("/events", status_code=202)
def report_event(payload: EventReport, principal: Principal = Depends(current_principal)):
    return service().report_event(payload.model_dump(), viewer_from(principal))


@router.get("/vendors/{vendor_code}/scorecard")
def scorecard(vendor_code: str, principal: Principal = Depends(current_principal)):
    return service().scorecard(vendor_code, viewer_from(principal))


@router.get("/vendors/{vendor_code}/events")
def list_events(
    vendor_code: str,
    ceremony_type: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return service().list_events(vendor_code, viewer_from(principal), ceremony_type=ceremony_type)


@router.get("/candidates/{ceremony_type}")
def candidates(
    ceremony_type: str,
    capability: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return service().candidates(ceremony_type, viewer_from(principal), capability=capability)


@router.post("/overrides", status_code=201)
def manual_override(payload: ManualOverride, principal: Principal = Depends(current_principal)):
    return service().manual_override(payload.model_dump(), viewer_from(principal))


@router.post("/reviews", status_code=201)
def review(payload: ReviewRequest, principal: Principal = Depends(current_principal)):
    return service().review(payload.model_dump(), viewer_from(principal))


@router.post("/events/dismiss", status_code=201)
def dismiss_event(payload: DismissEventRequest, principal: Principal = Depends(current_principal)):
    return service().dismiss_event(payload.model_dump(), viewer_from(principal))


@router.get("/audit-summary")
def audit_summary(
    vendor_code: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
):
    return service().audit_summary(vendor_code, viewer_from(principal), limit=limit)
