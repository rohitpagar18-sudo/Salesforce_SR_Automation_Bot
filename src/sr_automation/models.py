from dataclasses import dataclass, field
from typing import Literal

Action = Literal["CANCEL", "REJECT", "CLOSE", "UNKNOWN", "CONFLICT"]

@dataclass(frozen=True)
class ParseResult:
    ticket_id: str
    case_numbers: list[str]
    action: Action
    confidence: str
    reason_code: str
    warnings: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class CaseDecision:
    ticket_id: str
    case_number: str
    requested_action: Action
    record_type: str | None
    current_status: str | None
    outcome: str
    reason: str
    open_activities: int = 0
    matching_closed_activities: int = 0
