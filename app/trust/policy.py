"""供应商可信度评分策略。

策略版本化：每个事件入库时冻结当时的 ``POLICY_VERSION`` 与扣分权重，
回放（replay）时按事件上冻结的权重计算，保证历史评分可复现、不随后续
权重调整而漂移。阈值与窗口集中在本模块，便于审计解释。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 仅升不重用：调整权重/阈值时递增，旧事件仍记录旧版本。
POLICY_VERSION = "trust-policy-2026.10"

# 基础可信度满分；评分在 [0, BASE_SCORE] 之间。
BASE_SCORE = 100

# 滚动统计窗口（天）。
WINDOWS_DAYS = (30, 90)
DEFAULT_WINDOW_DAYS = 90

# 事件类型 -> (基础扣分, 说明)。severity 作为乘数：1/2/3 -> 1/1.5/2。
EVENT_PENALTIES: dict[str, tuple[int, str]] = {
    "late": (8, "迟到"),
    "substitute": (6, "临时换人"),
    "complaint": (15, "服务投诉"),
    "no_show": (30, "爽约缺席"),
    "praise": (-5, "表扬（加分）"),
}

# 证据来源可信度权重：系统直连证据高于单方陈述。
SOURCE_WEIGHTS: dict[str, float] = {
    "gps": 1.0,
    "dispatch_system": 1.0,
    "onsite_report": 0.8,
    "customer_hotline": 0.7,
    "online_review": 0.6,
    "manual": 1.0,
}

SEVERITY_MULTIPLIER = {1: 1.0, 2: 1.5, 3: 2.0}

# 触发阈值：按窗口累计扣分。
# cooling：仍可接单但排序降权；auto_restrict：自动隔离相应场次类型。
COOLING_THRESHOLD = 15
RESTRICT_THRESHOLD = 30
# 单窗口内严重投诉次数也会直接触发隔离（防止单次扣分偏低时漏过）。
RESTRICT_COMPLAINT_COUNT = 2
# 隔离后需要冷却的最短观察期（天），复核恢复前需经过。
COOLING_OBSERVATION_DAYS = 7

CEREMONY_TYPES = ("wedding", "funeral")

# 人工处置 -> 评分附加（记录在决策上，不直接改事件分；恢复后不抹除历史）。
DECISION_KINDS = ("auto_restrict", "manual_block", "manual_cooling", "probation_allow", "restore", "dismiss_event")


@dataclass(frozen=True, slots=True)
class ScoreContribution:
    event_id: int
    event_type: str
    ceremony_type: str
    severity: int
    source: str
    weight: float
    points: int
    window_days: int
    policy_version: str
    occurrence_at: str
    occurrence_key: str


def event_delta(event_type: str, severity: int, source: str) -> tuple[int, float]:
    """返回 (扣分[正为扣、负为加], 来源权重)。"""
    base, _ = EVENT_PENALTIES[event_type]
    multiplier = SEVERITY_MULTIPLIER[severity]
    weight = SOURCE_WEIGHTS[source]
    # 表扬（负基础分）同样乘以系数，向零方向收敛。
    raw = base * multiplier * weight
    points = int(round(raw)) if base > 0 else -min(BASE_SCORE, int(round(-raw)))
    return points, weight


def clamp_score(total_penalty: int) -> int:
    return max(0, min(BASE_SCORE, BASE_SCORE - total_penalty))


def explain_rule(event_type: str) -> str:
    return EVENT_PENALTIES[event_type][1]


def validate_event(event_type: str, ceremony_type: str, severity: int, source: str) -> None:
    from app.core.errors import ValidationError

    if ceremony_type not in CEREMONY_TYPES:
        raise ValidationError("场次类型不合法", context={"allowed": list(CEREMONY_TYPES)})
    if event_type not in EVENT_PENALTIES:
        raise ValidationError("事件类型不合法", context={"allowed": sorted(EVENT_PENALTIES)})
    if severity not in SEVERITY_MULTIPLIER:
        raise ValidationError("严重级别必须为 1、2 或 3")
    if source not in SOURCE_WEIGHTS:
        raise ValidationError("证据来源不合法", context={"allowed": sorted(SOURCE_WEIGHTS)})


def evaluate_thresholds(contributions: list[dict[str, Any]]) -> dict[str, Any]:
    """根据某窗口内的贡献计算是否达到冷却/隔离阈值。"""
    penalty = sum(item["points"] for item in contributions if item["points"] > 0)
    complaint_count = sum(1 for item in contributions if item["event_type"] == "complaint" and item["severity"] >= 2)
    restricted = penalty >= RESTRICT_THRESHOLD or complaint_count >= RESTRICT_COMPLAINT_COUNT
    cooling = penalty >= COOLING_THRESHOLD
    return {
        "penalty": penalty,
        "cooling": cooling,
        "restricted": restricted,
        "complaint_count_severe": complaint_count,
        "cooling_threshold": COOLING_THRESHOLD,
        "restrict_threshold": RESTRICT_THRESHOLD,
    }
