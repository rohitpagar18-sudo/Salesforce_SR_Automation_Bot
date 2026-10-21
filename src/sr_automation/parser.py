import html
import re
from .models import ParseResult

CASE_PATTERN = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
ACTION_PATTERNS = {
    "CANCEL": (r"\bcancel\b", r"\bcancelled\b", r"\bcanceled\b", r"\bcancellation\b", r"\bcancelling\b", r"\bcanceling\b"),
    "REJECT": (r"\breject\b", r"\brejected\b", r"\brejection\b", r"\brejecting\b"),
    "CLOSE": (r"\bclose\b", r"\bclosed\b", r"\bclosure\b", r"\bclosing\b"),
}
NEGATIONS = {
    "CANCEL": (r"\bdo not cancel\b", r"\bdon't cancel\b", r"\bnot to cancel\b", r"\bcancellation not required\b"),
    "REJECT": (r"\bdo not reject\b", r"\bdon't reject\b", r"\bnot to reject\b"),
    "CLOSE": (r"\bdo not close\b", r"\bdon't close\b", r"\bnot to close\b"),
}

def clean_html(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"(?i)<\s*br\s*/?\s*>|<\s*/\s*(?:p|div|li|tr|ol|ul|table)\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*/\s*(?:td|th)\s*>", " | ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text.replace("\u00a0", " ")).strip()

def _actions(text: str) -> tuple[set[str], set[str]]:
    normalized = text.casefold().replace("’", "'")
    active, negated = set(), set()
    for action, patterns in ACTION_PATTERNS.items():
        has_negation = any(re.search(p, normalized) for p in NEGATIONS[action])
        if has_negation: negated.add(action)
        if any(re.search(p, normalized) for p in patterns) and not has_negation: active.add(action)
    return active, negated

def parse_ticket(ticket_id: str, subject: str, description_html: str) -> ParseResult:
    description = clean_html(description_html)
    subject_actions, subject_negated = _actions(subject)
    desc_actions, desc_negated = _actions(description)
    cases = list(dict.fromkeys(CASE_PATTERN.findall(subject + "\n" + description)))
    warnings: list[str] = []
    if subject_negated or desc_negated:
        warnings.append("Negated action wording found; manual review required.")
        return ParseResult(ticket_id, cases, "CONFLICT", "LOW", "NEGATED_OR_MIXED_ACTION", warnings)
    if not cases:
        return ParseResult(ticket_id, [], "UNKNOWN", "LOW", "NO_SR_FOUND", ["No 8-digit Case number found."])
    if len(desc_actions) > 1 or len(subject_actions) > 1:
        return ParseResult(ticket_id, cases, "CONFLICT", "LOW", "MULTIPLE_ACTIONS", warnings)
    if len(desc_actions) == 1:
        action = next(iter(desc_actions))
        if subject_actions and subject_actions != {action}:
            return ParseResult(ticket_id, cases, "CONFLICT", "LOW", "SUBJECT_DESCRIPTION_CONFLICT", warnings)
        confidence = "HIGH" if subject_actions == {action} else "MEDIUM"
        reason = "SUBJECT_DESCRIPTION_MATCH" if confidence == "HIGH" else "DESCRIPTION_ACTION_ONLY"
        return ParseResult(ticket_id, cases, action, confidence, reason, warnings)  # type: ignore[arg-type]
    if len(subject_actions) == 1:
        return ParseResult(ticket_id, cases, next(iter(subject_actions)), "LOW", "SUBJECT_ONLY_CONFIRMATION_REQUIRED", warnings)  # type: ignore[arg-type]
    return ParseResult(ticket_id, cases, "UNKNOWN", "LOW", "NO_ACTION_FOUND", warnings)
