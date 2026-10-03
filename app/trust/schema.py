"""供应商可信度领域的 SQLite 表结构。"""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS trust_vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trust_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL REFERENCES trust_vendors(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    ceremony_type TEXT NOT NULL CHECK(ceremony_type IN ('wedding','funeral')),
    severity INTEGER NOT NULL CHECK(severity IN (1,2,3)),
    source TEXT NOT NULL,
    occurrence_key TEXT NOT NULL,
    occurrence_at TEXT NOT NULL,
    points INTEGER NOT NULL,
    weight REAL NOT NULL,
    policy_version TEXT NOT NULL,
    reporter TEXT NOT NULL DEFAULT '',
    evidence_ref TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    dismissed INTEGER NOT NULL DEFAULT 0 CHECK(dismissed IN (0,1)),
    dismissed_by TEXT NOT NULL DEFAULT '',
    dismissed_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(vendor_id, occurrence_key)
);
CREATE INDEX IF NOT EXISTS idx_trust_events_vendor ON trust_events(vendor_id, ceremony_type, occurrence_at, id);
CREATE TABLE IF NOT EXISTS trust_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL REFERENCES trust_vendors(id) ON DELETE CASCADE,
    ceremony_type TEXT NOT NULL CHECK(ceremony_type IN ('wedding','funeral')),
    kind TEXT NOT NULL CHECK(kind IN ('auto_restrict','manual_block','manual_cooling','probation_allow','restore','dismiss_event')),
    reason TEXT NOT NULL,
    event_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
    effective_from TEXT NOT NULL,
    expires_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    review_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trust_decisions_vendor ON trust_decisions(vendor_id, ceremony_type, status, id);
"""


def ensure_schema(connection) -> None:
    connection.executescript(SCHEMA)
