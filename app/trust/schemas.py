from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ServiceType = Literal["wedding", "funeral", "both"]
EventType = Literal["late", "substitution", "complaint", "positive"]
Severity = Literal["minor", "major", "severe"]
EventSource = Literal["dispatch", "supervisor", "client", "official"]


class VendorCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=120)
    contact_name: str = Field(default="", max_length=60)
    contact_phone: str = Field(default="", max_length=32)
    service_types: list[Literal["wedding", "funeral"]] = Field(default_factory=list, min_length=1)


class EventCreate(BaseModel):
    vendor_code: str = Field(min_length=2, max_length=64)
    event_type: EventType
    service_type: ServiceType
    severity: Severity = "minor"
    occurred_at: str | None = Field(default=None, max_length=40, description="ISO8601 时间，缺省取服务端当前时间")
    source: EventSource
    evidence_ref: str = Field(default="", max_length=200)
    detail: str = Field(default="", max_length=2000)
    reporter_contact: str = Field(default="", max_length=120)
    idempotency_key: str = Field(min_length=6, max_length=160)


class CandidateQuery(BaseModel):
    service_type: Literal["wedding", "funeral"]
    scheduled_at: str | None = Field(default=None, max_length=40)
    limit: int = Field(default=20, ge=1, le=100)


class RecoveryRequest(BaseModel):
    vendor_code: str = Field(min_length=2, max_length=64)
    service_type: ServiceType
    decision: Literal["approved", "rejected"]
    note: str = Field(min_length=2, max_length=1000)


class ManualOverride(BaseModel):
    vendor_code: str = Field(min_length=2, max_length=64)
    service_type: ServiceType
    action: Literal["restrict", "lift"]
    reason: str = Field(min_length=2, max_length=1000)
    effective_until: str | None = Field(default=None, max_length=40)
