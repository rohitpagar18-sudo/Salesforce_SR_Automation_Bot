import html
import os
import re
import sys
from dataclasses import dataclass
from typing import Literal

import httpx
from dotenv import load_dotenv

load_dotenv()

Action = Literal["CANCEL", "REJECT", "CLOSE", "UNKNOWN", "CONFLICT"]

ACTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "CANCEL": (
        r"\bcancel\b", r"\bcancelled\b", r"\bcanceled\b",
        r"\bcancelling\b", r"\bcanceling\b", r"\bcancellation\b",
        r"\bcancellations\b", r"\bto be cancelled\b",
    ),
    "REJECT": (
        r"\breject\b", r"\brejected\b", r"\brejecting\b",
        r"\brejection\b", r"\brejections\b", r"\bto be rejected\b",
    ),
    "CLOSE": (
        r"\bclose\b", r"\bclosed\b", r"\bclosing\b",
        r"\bclosure\b", r"\bclosures\b", r"\bto be closed\b",
        r"\bbulk closure\b",
    ),
}

NEGATION_PATTERNS: dict[str, tuple[str, ...]] = {
    "CANCEL": (
        r"\bdo not cancel\b", r"\bdon't cancel\b", r"\bnot to cancel\b",
        r"\bshould not be cancelled\b", r"\bno cancellation\b",
        r"\bcancellation not required\b",
    ),
    "REJECT": (
        r"\bdo not reject\b", r"\bdon't reject\b", r"\bnot to reject\b",
        r"\bshould not be rejected\b", r"\bno rejection\b",
    ),
    "CLOSE": (
        r"\bdo not close\b", r"\bdon't close\b", r"\bnot to close\b",
        r"\bshould not be closed\b", r"\bno closure\b",
    ),
}

CASE_PATTERN = re.compile(r"(?<!\d)\d{8}(?!\d)")


@dataclass(frozen=True)
class ActionEvidence:
    action: str
    source: str
    matched_text: str
    negated: bool


@dataclass(frozen=True)
class ParseResult:
    ticket_id: str
    case_numbers: list[str]
    resolved_action: Action
    confidence: str
    reason_code: str
    subject_actions: list[str]
    description_actions: list[str]
    evidence: list[ActionEvidence]
    warnings: list[str]


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def clean_html(value: str) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = re.sub(
        r"(?i)<\s*br\s*/?\s*>|<\s*/\s*(?:p|div|li|tr|ol|ul|table)\s*>",
        "\n",
        text,
    )
    text = re.sub(r"(?i)<\s*/\s*(?:td|th)\s*>", " | ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\u00a0", " ").replace("\u200b", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def normalize_text(value: str) -> str:
    text = value.casefold()
    for old, new in {
        "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
        "/": " ", "\\": " ", "_": " ",
    }.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def find_evidence(text: str, source: str) -> list[ActionEvidence]:
    normalized = normalize_text(text)
    result: list[ActionEvidence] = []
    for action, patterns in ACTION_PATTERNS.items():
        negated = any(re.search(p, normalized, re.IGNORECASE) for p in NEGATION_PATTERNS[action])
        for pattern in patterns:
            for match in re.finditer(pattern, normalized, re.IGNORECASE):
                result.append(ActionEvidence(action, source, match.group(0), negated))
    return result


def active_actions(evidence: list[ActionEvidence], source: str) -> set[str]:
    return {item.action for item in evidence if item.source == source and not item.negated}


def resolve_action(evidence: list[ActionEvidence]) -> tuple[Action, str, str, list[str]]:
    subject = active_actions(evidence, "SUBJECT")
    description = active_actions(evidence, "DESCRIPTION")
    negated = {item.action for item in evidence if item.negated}
    warnings: list[str] = []

    if negated:
        warnings.append("Negated action wording found: " + ", ".join(sorted(negated)))
        # Mixed positive and negative wording needs row/per-case parsing.
        if subject or description:
            return "CONFLICT", "LOW", "NEGATED_OR_MIXED_ACTION", warnings

    if len(description) > 1:
        return "CONFLICT", "LOW", "CONFLICTING_DESCRIPTION_ACTIONS", warnings

    if len(description) == 1:
        action = next(iter(description))
        if len(subject) > 1 or (subject and action not in subject):
            return "CONFLICT", "LOW", "SUBJECT_DESCRIPTION_CONFLICT", warnings
        if subject == {action}:
            return action, "HIGH", "SUBJECT_DESCRIPTION_MATCH", warnings  # type: ignore[return-value]
        return action, "MEDIUM", "DESCRIPTION_ACTION_ONLY", warnings  # type: ignore[return-value]

    if len(subject) == 1:
        action = next(iter(subject))
        return action, "LOW", "SUBJECT_ONLY_CONFIRMATION_REQUIRED", warnings  # type: ignore[return-value]

    if len(subject) > 1:
        return "CONFLICT", "LOW", "CONFLICTING_SUBJECT_ACTIONS", warnings

    return "UNKNOWN", "LOW", "NO_ACTION_FOUND", warnings


def parse_ticket(ticket_id: str, subject: str, description_html: str) -> ParseResult:
    description = clean_html(description_html)
    evidence = find_evidence(subject, "SUBJECT") + find_evidence(description, "DESCRIPTION")
    case_numbers = list(dict.fromkeys(CASE_PATTERN.findall(f"{subject}\n{description}")))
    action, confidence, reason_code, warnings = resolve_action(evidence)

    if not case_numbers:
        action = "UNKNOWN"
        confidence = "LOW"
        reason_code = "NO_SR_FOUND"
        warnings.append("No eight-digit Salesforce Case number found.")

    return ParseResult(
        ticket_id=ticket_id,
        case_numbers=case_numbers,
        resolved_action=action,
        confidence=confidence,
        reason_code=reason_code,
        subject_actions=sorted(active_actions(evidence, "SUBJECT")),
        description_actions=sorted(active_actions(evidence, "DESCRIPTION")),
        evidence=evidence,
        warnings=list(dict.fromkeys(warnings)),
    )


def nested_name(value: object) -> str | None:
    return str(value.get("name")) if isinstance(value, dict) and value.get("name") else None


def read_manageengine_ticket(base_url: str, token: str, auth_header: str, ticket_id: str) -> dict:
    response = httpx.get(
        f"{base_url.rstrip('/')}/api/v3/requests/{ticket_id}",
        headers={auth_header: token, "Accept": "application/vnd.manageengine.sdp.v3+json"},
        timeout=30.0,
    )
    response.raise_for_status()
    ticket = response.json().get("request")
    if not isinstance(ticket, dict):
        raise ValueError("ManageEngine response does not contain a request object.")
    return ticket


def print_result(result: ParseResult) -> None:
    print("Ticket parsing: SUCCESS")
    print(f"Ticket ID          : {result.ticket_id}")
    print(f"Case Numbers       : {result.case_numbers}")
    print(f"Subject Actions    : {result.subject_actions}")
    print(f"Description Actions: {result.description_actions}")
    print(f"Resolved Action    : {result.resolved_action}")
    print(f"Confidence         : {result.confidence}")
    print(f"Reason Code        : {result.reason_code}")
    for warning in result.warnings:
        print(f"Warning            : {warning}")
    print("DRY RUN: No systems were changed.")


def main() -> None:
    try:
        ticket_id = required_env("MANAGEENGINE_TEST_TICKET_ID")
        ticket = read_manageengine_ticket(
            required_env("MANAGEENGINE_BASE_URL"),
            required_env("MANAGEENGINE_API_TOKEN"),
            os.getenv("MANAGEENGINE_AUTH_HEADER", "authtoken").strip(),
            ticket_id,
        )

        allowed_groups = {"SalesForce - CRM", "ITMS - SF CRM"}
        allowed_templates = {"SalesForce CRM Issues"}
        allowed_statuses = {"Open"}
        group = nested_name(ticket.get("group"))
        template = nested_name(ticket.get("template"))
        status = nested_name(ticket.get("status"))

        if group not in allowed_groups or template not in allowed_templates or status not in allowed_statuses:
            print("Ticket parsing: SKIPPED")
            print(f"Group: {group} | Template: {template} | Status: {status}")
            print("Reason Code: OUTSIDE_AUTOMATION_SCOPE")
            return

        result = parse_ticket(
            ticket_id,
            str(ticket.get("subject") or ""),
            str(ticket.get("description") or ""),
        )
        print_result(result)

    except httpx.HTTPStatusError as error:
        print(f"ManageEngine ticket read failed. HTTP status: {error.response.status_code}")
        sys.exit(1)
    except httpx.RequestError as error:
        print(f"ManageEngine connection failed: {type(error).__name__}")
        sys.exit(1)
    except (TypeError, ValueError) as error:
        print(f"Ticket could not be processed: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
