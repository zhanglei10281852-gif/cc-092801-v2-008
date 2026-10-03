from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CeremonyType = Literal["wedding", "funeral"]
EventType = Literal["late", "substitute", "complaint", "no_show", "praise"]
EvidenceSource = Literal["gps", "dispatch_system", "onsite_report", "customer_hotline", "online_review", "manual"]


class VendorRegister(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    contact_name: str = Field(default="", max_length=60)
    phone: str = Field(default="", max_length=40)
    capabilities: list[CeremonyType] = Field(default_factory=list, min_length=1)
    note: str = Field(default="", max_length=500)


class EventReport(BaseModel):
    vendor_code: str = Field(min_length=2, max_length=64)
    event_type: EventType
    ceremony_type: CeremonyType
    severity: int = Field(ge=1, le=3)
    source: EvidenceSource
    occurrence_key: str = Field(min_length=6, max_length=160)
    occurrence_at: str | None = Field(default=None, description="ISO8601；缺省为上报时刻")
    evidence_ref: str = Field(default="", max_length=300)
    detail: dict = Field(default_factory=dict)


class ManualOverride(BaseModel):
    vendor_code: str = Field(min_length=2, max_length=64)
    ceremony_type: CeremonyType
    kind: Literal["manual_block", "manual_cooling"]
    reason: str = Field(min_length=4, max_length=1000)
    expires_at: str = Field(description="必须写明期限（ISO8601）")


class ReviewRequest(BaseModel):
    vendor_code: str = Field(min_length=2, max_length=64)
    ceremony_type: CeremonyType
    mode: Literal["restore", "probation"]
    reason: str = Field(min_length=4, max_length=1000)
    expires_at: str | None = Field(default=None, description="观察期恢复时必填")


class DismissEventRequest(BaseModel):
    vendor_code: str = Field(min_length=2, max_length=64)
    event_id: int = Field(ge=1)
    reason: str = Field(min_length=4, max_length=1000)
