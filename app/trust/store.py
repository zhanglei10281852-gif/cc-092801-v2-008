from __future__ import annotations

from app.database import get_connection

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS trust_vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    contact_phone TEXT NOT NULL DEFAULT '',
    service_types TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trust_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL REFERENCES trust_vendors(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('late','substitution','complaint','positive')),
    service_type TEXT NOT NULL CHECK(service_type IN ('wedding','funeral','both')),
    severity TEXT NOT NULL DEFAULT 'minor' CHECK(severity IN ('minor','major','severe')),
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('dispatch','supervisor','client','official')),
    evidence_ref TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    reporter_contact TEXT NOT NULL DEFAULT '',
    event_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(vendor_id, idempotency_key),
    UNIQUE(vendor_id, event_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_trust_events_vendor ON trust_events(vendor_id, occurred_at, id);

CREATE TABLE IF NOT EXISTS trust_score_ledgers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL REFERENCES trust_vendors(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES trust_events(id) ON DELETE CASCADE,
    service_type TEXT NOT NULL CHECK(service_type IN ('wedding','funeral')),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(event_id, service_type)
);
CREATE INDEX IF NOT EXISTS idx_trust_ledger_vendor ON trust_score_ledgers(vendor_id, service_type, window_end);

CREATE TABLE IF NOT EXISTS trust_restrictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL REFERENCES trust_vendors(id) ON DELETE CASCADE,
    service_type TEXT NOT NULL CHECK(service_type IN ('wedding','funeral')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','lifted','expired')),
    origin TEXT NOT NULL CHECK(origin IN ('auto','manual')),
    reason TEXT NOT NULL,
    score_at_creation INTEGER NOT NULL,
    trigger_event_ids TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL,
    effective_until TEXT,
    lifted_at TEXT,
    lifted_by TEXT NOT NULL DEFAULT '',
    lift_reason TEXT NOT NULL DEFAULT '',
    review_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trust_restrictions_vendor ON trust_restrictions(vendor_id, status, service_type);

CREATE TABLE IF NOT EXISTS trust_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL REFERENCES trust_vendors(id) ON DELETE CASCADE,
    restriction_id INTEGER REFERENCES trust_restrictions(id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    note TEXT NOT NULL,
    guard_until TEXT,
    reviewer_user_id INTEGER,
    reviewer_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trust_reviews_vendor ON trust_reviews(vendor_id, id);
"""


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)
