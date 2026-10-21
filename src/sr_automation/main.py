import argparse
import html
import json
import re
import time
from dataclasses import asdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import httpx
from .clients import ManageEngineClient, SalesforceClient
from .parser import parse_ticket
from .rules import decide
from .settings import load_closure_rules, load_settings
from .persistence.audit_repository import AuditRepository
from .manageengine.note_writer import ManageEngineNoteWriter
import uuid
from hashlib import sha256
import os

cfg = None
ME_STANDARD_REQUEST_FIELDS = {"technician", "group", "category", "subcategory", "priority", "request_type"}
ME_RELATED_VALIDATION_FIELDS = {"tasks", "checklists", "depends_on_requests", "child_requests"}
ME_CLOSURE_PAYLOAD_FIELDS = {"udf_pick_8104", "udf_pick_13501", "udf_long_15301", "resolution"}


def name(v:object)->str|None:return str(v.get("name")) if isinstance(v,dict) and v.get("name") else None


def extract_created_ms(val):
    # val may be dict with 'value' in epoch ms, or a string
    if isinstance(val, dict):
        v = val.get("value")
    else:
        v = val
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return None


def tags_from_field(val):
    if not val:
        return []
    if isinstance(val, list):
        out = []
        for it in val:
            if isinstance(it, dict):
                # common ManageEngine tag shape could be {"value": "tag"} or {"name":"tag"}
                out.append(str(it.get("value") or it.get("name") or ""))
            else:
                out.append(str(it))
        return [x for x in out if x]
    return [str(val)]

RETRIABLE_AUDIT_STATES = {
    "DETECTED",
    "VALIDATED",
    "SALESFORCE_UPDATE_STARTED",
    "FAILED",
    "UPDATE_RESULT_UNKNOWN",
    "WRITE_BLOCKED_AFTER_UNCERTAIN_RESULT",
    "UPDATE_LIMIT_REACHED",
}
# Historically referenced as both RETRIABLE_AUDIT_STATES and RETRYABLE_AUDIT_STATES
RETRYABLE_AUDIT_STATES = RETRIABLE_AUDIT_STATES

VERIFIED_SUCCESS_OUTCOMES = {
    "SUCCESS",
    "SALESFORCE_VERIFIED",
    "SUCCESS_AFTER_TIMEOUT_RECONCILIATION",
    "ALREADY_IN_TARGET_STATE",
}


def is_success_outcome(outcome: object) -> bool:
    return str(outcome or "").upper() in VERIFIED_SUCCESS_OUTCOMES


def is_manual_outcome(outcome: object) -> bool:
    normalized = str(outcome or "").upper()
    return normalized in {
        "WOULD_UPDATE",
        "SALESFORCE_WRITES_DISABLED",
        "MANUAL_REVIEW",
        "MANUAL_REVIEW_RECORD_TYPE",
        "INVALID_CURRENT_STATUS",
        "OPEN_ACTIVITIES",
        "REJECTION_EVIDENCE_MISSING",
        "CONFIRMATION_REQUIRED",
        "CASE_NOT_FOUND",
        "CASE_NOT_FOUND_ON_REREAD",
        "WRITE_BLOCKED_AFTER_UNCERTAIN_RESULT",
        "UPDATE_LIMIT_REACHED",
        "STALE_RECORD",
        "RECONCILIATION_REQUIRED",
    }


def extract_note_id(current_note: object) -> str:
    if not isinstance(current_note, dict):
        return ""
    nested = current_note.get("note")
    if isinstance(nested, dict):
        note_id = nested.get("id")
        if note_id is not None:
            return str(note_id)
    for key in ("id", "note_id", "noteId"):
        value = current_note.get(key)
        if value is not None:
            return str(value)
    return ""


def normalize_note_text(current_note: object) -> str:
    if not isinstance(current_note, dict):
        current_note = {"description": str(current_note or "")}

    note_content = current_note.get("description")
    if isinstance(note_content, dict):
        note_content = note_content.get("description") or note_content.get("note")
    if note_content is None:
        note_content = current_note.get("note")
        if isinstance(note_content, dict):
            note_content = note_content.get("description") or note_content.get("note")

    text = str(note_content or "")
    text = html.unescape(text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[^>]+>", " ", text)
    text = text.replace("\r", "\n")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def sanitize_endpoint(url: str | None) -> str:
    if not url:
        return "<unknown endpoint>"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return str(url)
    sanitized_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in {"authtoken", "token", "apikey", "api_key", "accesstoken", "access_token", "client_secret", "secret", "authorization"}:
            sanitized_pairs.append((key, "[REDACTED]"))
        else:
            sanitized_pairs.append((key, value))
    sanitized_query = urlencode(sanitized_pairs, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, sanitized_query, ""))


def _safe_note_writer_call(writer: object, method_name: str, *args, **kwargs):
    method = getattr(writer, method_name, None)
    if not callable(method):
        return None
    return method(*args, **kwargs)


def _redact_sensitive_text(value: object) -> str:
    text = str(value)
    patterns = [
        (r"(?i)(authorization\s*[:=]\s*)(['\"]?)([^\s,;]+)", r"\1\2[REDACTED]"),
        (r"(?i)((?:access_token|refresh_token|client_secret|clientsecret|password|passwd|secret|token)\s*[:=]\s*)(['\"]?)([^\s,;]+)", r"\1\2[REDACTED]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def _sanitize_error_payload(value: object) -> object:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            lower_key = key_text.lower()
            if lower_key in {"authorization", "access_token", "refresh_token", "client_secret", "clientsecret", "token", "password", "passwd", "secret"}:
                sanitized[key_text] = "[REDACTED]"
            elif lower_key.endswith("token") or "secret" in lower_key or "password" in lower_key or "authorization" in lower_key:
                sanitized[key_text] = "[REDACTED]"
            else:
                sanitized[key_text] = _sanitize_error_payload(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_error_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_error_payload(item) for item in value)
    if isinstance(value, set):
        return {_sanitize_error_payload(item) for item in value}
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _extract_salesforce_error_details(payload: object) -> tuple[str | None, str | None, list[str]]:
    sanitized = _sanitize_error_payload(payload)

    def first_error_dict(item):
        if isinstance(item, dict):
            return item
        if isinstance(item, list):
            for entry in item:
                candidate = first_error_dict(entry)
                if candidate is not None:
                    return candidate
        return None

    detail = sanitized
    if isinstance(detail, list):
        detail = first_error_dict(detail) or {}
    elif isinstance(detail, dict):
        nested_error = detail.get("error")
        if isinstance(nested_error, dict):
            detail = nested_error
        elif isinstance(detail.get("errors"), list):
            detail = first_error_dict(detail["errors"]) or detail

    error_code = None
    message = None
    fields: list[str] = []

    if isinstance(detail, dict):
        error_code = detail.get("errorCode") or detail.get("error_code")
        message = detail.get("message")
        fields_value = detail.get("fields") or detail.get("field") or detail.get("invalidFields")
        if isinstance(fields_value, str):
            fields = [fields_value]
        elif isinstance(fields_value, list):
            fields = [str(item) for item in fields_value]
        elif isinstance(fields_value, tuple):
            fields = [str(item) for item in fields_value]
        elif fields_value is not None:
            fields = [str(fields_value)]

        if error_code is None:
            nested_error_code = detail.get("error")
            if isinstance(nested_error_code, dict):
                error_code = nested_error_code.get("errorCode") or nested_error_code.get("error_code")

        if message is None:
            nested_message = detail.get("error")
            if isinstance(nested_message, dict):
                message = nested_message.get("message")

    if message is None and isinstance(payload, str):
        message = _redact_sensitive_text(payload)

    if error_code is None:
        error_code = "UNKNOWN"
    if message is None:
        message = "Salesforce PATCH error"

    return str(error_code), str(message), fields


def print_http_error_details(prefix: str, exc: httpx.HTTPStatusError) -> None:
    response = exc.response
    request = getattr(response, "request", None) or getattr(exc, "request", None)
    endpoint = sanitize_endpoint(str(request.url)) if request is not None else "<unknown endpoint>"
    status_code = getattr(response, "status_code", None) or "<unknown status>"
    payload = {}
    if response is not None:
        try:
            payload = response.json()
        except Exception:
            payload = {}
    print(f"{prefix}: HTTP {status_code}")
    print(f"endpoint={endpoint}")
    print(f"response_json={json.dumps(payload, ensure_ascii=False, sort_keys=True)}")

    messages = payload.get("messages") if isinstance(payload, dict) else None
    if isinstance(messages, list):
        for item in messages:
            if isinstance(item, dict):
                field = item.get("field")
                message = item.get("message")
                if field is not None:
                    print(f"messages[].field={field}")
                if message is not None:
                    print(f"messages[].message={message}")
    elif isinstance(messages, dict):
        for key, value in messages.items():
            print(f"messages[].field={key}")
            print(f"messages[].message={value}")


def resolve_stage2_patch_limit(settings, eligible_case_count: int) -> int:
    cap = getattr(settings, "stage2_absolute_safety_cap", 100)
    cap = max(0, int(cap))
    configured = getattr(settings, "stage2_max_updates", None)
    if configured is None or configured == "":
        allowed = eligible_case_count if cap == 0 else min(eligible_case_count, cap)
    else:
        try:
            configured_limit = max(0, int(configured))
        except (TypeError, ValueError):
            configured_limit = eligible_case_count
        allowed = configured_limit if cap == 0 else min(configured_limit, cap)
    return max(0, allowed)


def build_me_summary_digest(ticket_id: str, action: str, case_results: list[dict]) -> str:
    normalized = []
    for row in case_results:
        normalized.append({
            "case_number": str(row.get("case_number", "")),
            "action": str(row.get("action", "")).upper(),
            "outcome": str(row.get("outcome", "")).upper(),
            "before_status": str(row.get("before_status", "")),
            "after_status": str(row.get("after_status", "")),
        })
    digest_input = {
        "ticket_id": str(ticket_id),
        "action": str(action).upper(),
        "cases": sorted(normalized, key=lambda item: (
            str(item.get("case_number", "")),
            str(item.get("action", "")),
            str(item.get("outcome", "")),
        )),
    }
    canonical = json.dumps(digest_input, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(canonical.encode("utf-8")).hexdigest()[:16]


def ensure_summary_note(ticket_id: str, parsed, case_results: list[dict], me_writer, audit, ticket_action: str | None = None) -> bool:
    action_name = (ticket_action or str(parsed.action).upper())
    digest = build_me_summary_digest(ticket_id, action_name, case_results)
    audits = audit.get_by_ticket(ticket_id)
    digest_exists = any(
        str((a.get("requested_action") or "")).upper() == "ME_SUMMARY_DIGEST"
        and str(a.get("before_status") or "") == digest
        for a in audits
    )
    if digest_exists:
        print(f"ME_NOTE_ALREADY_ADDED for ticket {ticket_id} digest={digest}")
        return True

    payload = me_writer.build_summary(
        ticket_id=ticket_id,
        environment="UAT",
        action=action_name,
        case_lines=[f"{row.get('case_number', '')} | {row.get('action', '')} | {row.get('outcome', '')} | {row.get('before_status', '')} -> {row.get('after_status', '') or row.get('before_status', '')}" for row in case_results],
        success_count=sum(1 for row in case_results if is_success_outcome(row.get("outcome"))),
        manual_count=sum(1 for row in case_results if is_manual_outcome(row.get("outcome"))),
        failed_count=sum(1 for row in case_results if not is_success_outcome(row.get("outcome")) and not is_manual_outcome(row.get("outcome"))),
        correlation_id=str(uuid.uuid4()),
    )
    audit.insert_audit(
        idempotency_key=f"me_digest|{ticket_id}|{digest}",
        ticket_id=ticket_id,
        case_number="",
        requested_action="ME_SUMMARY_DIGEST",
        rule_version="",
        before_status=digest,
        intended_status="",
        after_status="",
        salesforce_id="",
        last_modified_date="",
        processing_state="ME_NOTE_STARTED",
        outcome="ME_NOTE_STARTED",
        correlation_id=str(uuid.uuid4()),
    )
    try:
        resp = me_writer.post_note(ticket_id, payload, timeout=30.0)
        data = resp.json()
        note_id = str(data.get("note", {}).get("id") or data.get("id") or "")
        audit.insert_audit(
            idempotency_key=f"me_digest|{ticket_id}|{digest}",
            ticket_id=ticket_id,
            case_number="",
            requested_action="ME_SUMMARY_DIGEST",
            rule_version="",
            before_status=digest,
            intended_status="",
            after_status="",
            salesforce_id="",
            last_modified_date="",
            processing_state="ME_NOTE_VERIFIED",
            outcome="ME_NOTE_VERIFIED",
            correlation_id=str(uuid.uuid4()),
        )
        if note_id:
            audit.set_me_note(ticket_id=ticket_id, note_id=note_id, state="ME_NOTE_VERIFIED", correlation_id=str(uuid.uuid4()))
        print(f"ME_NOTE_ADDED ticket={ticket_id} note_id={note_id or '<unknown>'}")
        return True
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        audit.insert_audit(
            idempotency_key=f"me_digest|{ticket_id}|{digest}",
            ticket_id=ticket_id,
            case_number="",
            requested_action="ME_SUMMARY_DIGEST",
            rule_version="",
            before_status=digest,
            intended_status="",
            after_status="",
            salesforce_id="",
            last_modified_date="",
            processing_state="ME_NOTE_FAILED",
            outcome="ME_NOTE_FAILED",
            correlation_id=str(uuid.uuid4()),
        )
        print(f"ME_NOTE_FAILED for ticket {ticket_id}: {exc}")
        return False


def note_matches_equivalent(current_note: object, ticket_id: str, action: str, case_numbers: list[str]) -> bool:
    description = normalize_note_text(current_note)
    if not description:
        return False

    lower = description.casefold()
    ticket_id_text = str(ticket_id)
    if f"ticket #{ticket_id_text}" not in lower and f"ticket {ticket_id_text}" not in lower and ticket_id_text not in lower:
        return False
    if action.strip().casefold() not in lower:
        return False

    success_patterns = (
        r"(?<![A-Z])SUCCESS(?![A-Z])",
        r"(?<![A-Z])ALREADY_IN_TARGET_STATE(?![A-Z])",
        r"(?<![A-Z])SUCCESS_AFTER_TIMEOUT_RECONCILIATION(?![A-Z])",
    )
    non_success_patterns = (
        r"(?<![A-Z])WOULD_UPDATE(?![A-Z])",
        r"(?<![A-Z])MANUAL_REVIEW(?![A-Z])",
        r"(?<![A-Z])STALE_RECORD(?![A-Z])",
        r"(?<![A-Z])OPEN_ACTIVITIES(?![A-Z])",
        r"(?<![A-Z])INVALID_CURRENT_STATUS(?![A-Z])",
        r"(?<![A-Z])CASE_NOT_FOUND(?![A-Z])",
        r"(?<![A-Z])REJECTION_EVIDENCE_MISSING(?![A-Z])",
        r"(?<![A-Z])VERIFICATION_FAILED(?![A-Z])",
        r"(?<![A-Z])UPDATE_RESULT_UNKNOWN(?![A-Z])",
        r"(?<![A-Z])WRITE_BLOCKED_AFTER_UNCERTAIN_RESULT(?![A-Z])",
        r"(?<![A-Z])UPDATE_LIMIT_REACHED(?![A-Z])",
    )

    for case_number in case_numbers:
        number = str(case_number).strip()
        if not number:
            continue
        case_number_patterns = (
            number.casefold(),
            f"case number {number}".casefold(),
            f"case_number {number}".casefold(),
            f"case number:{number}".casefold(),
            f"case_number:{number}".casefold(),
        )
        if not any(pattern in lower for pattern in case_number_patterns):
            return False

        case_line_matches = [line for line in description.splitlines() if number.casefold() in line.casefold()]
        if not case_line_matches:
            return False

        has_success = False
        has_non_success = False
        for line in case_line_matches:
            if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in success_patterns):
                has_success = True
            if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in non_success_patterns):
                has_non_success = True

        if has_non_success and not has_success:
            return False
        if not has_success and not any(
            token in line.casefold()
            for line in case_line_matches
            for token in (f"case number {number}".casefold(), f"case number:{number}".casefold(), f"case_number {number}".casefold(), f"case_number:{number}".casefold())
        ):
            return False
        if not has_success and not any(
            "success" in line.casefold() or "already_in_target_state" in line.casefold() for line in case_line_matches
        ):
            return False

        if has_success:
            continue

    return True


def sanitize_udf_value(value):
    if value is None:
        return None
    if isinstance(value, dict):
        if "value" in value:
            return sanitize_udf_value(value.get("value"))
        if "name" in value and len(value) == 1:
            return sanitize_udf_value(value.get("name"))
        return {str(k): sanitize_udf_value(v) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) == 1:
            return sanitize_udf_value(value[0])
        return [sanitize_udf_value(v) for v in value]
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped != "" else None
    if isinstance(value, (int, float, bool)):
        return value
    return str(value)


def extract_ticket_udf_value(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, list):
        if not raw_value:
            return None
        return raw_value[0]
    text = str(raw_value).strip()
    return text if text else None


def udf_is_empty(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, dict):
        if "value" in value:
            return udf_is_empty(value.get("value"))
        return len(value) == 0
    if isinstance(value, list):
        return len(value) == 0 or all(udf_is_empty(v) for v in value)
    return False


def coerce_udf_value(field_name: str, value):
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value", value)
    if str(field_name).startswith("udf_long_"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return value


def _normalize_match_value(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip().casefold()
    return str(value).strip().casefold()


def _coerce_field_spec(value):
    if value is None:
        return None
    if isinstance(value, dict):
        payload = {}
        if value.get("id") is not None:
            payload["id"] = str(value["id"])
        if value.get("name") is not None:
            payload["name"] = str(value["name"])
        return payload or None
    if isinstance(value, str) and value.strip():
        return {"name": value.strip()}
    return None


def _match_rule(rule: dict, ticket: dict | None) -> bool:
    if not isinstance(rule, dict):
        return False
    match_map = rule.get("match") if isinstance(rule.get("match"), dict) else {}
    if not match_map:
        return True
    ticket_data = ticket or {}
    for field_name, expected_value in match_map.items():
        actual_value = ticket_data.get(str(field_name))
        if actual_value is None and "udf_fields" in ticket_data and isinstance(ticket_data.get("udf_fields"), dict):
            actual_value = ticket_data["udf_fields"].get(str(field_name))
        if _normalize_match_value(actual_value) != _normalize_match_value(expected_value):
            return False
    return True


def _get_mandatory_fields(mandatory_fields: object) -> list[str]:
    if isinstance(mandatory_fields, list):
        return [str(item) for item in mandatory_fields if str(item).strip()]
    if isinstance(mandatory_fields, dict):
        for key in ("mandatory_fields", "required_fields", "required", "fields", "to_be_filled"):
            values = mandatory_fields.get(key)
            if isinstance(values, list):
                return [str(item) for item in values if str(item).strip()]
            if isinstance(values, str) and values.strip():
                return [str(values).strip()]
        for key, value in mandatory_fields.items():
            if isinstance(value, list):
                fields = [str(item) for item in value if str(item).strip()]
                if fields:
                    return fields
        if mandatory_fields:
            return [str(key) for key, value in mandatory_fields.items() if value is not None and str(key) != "to_be_filled"]
    return []


def _legacy_rule_udf_spec(rule_spec: object) -> dict:
    if not isinstance(rule_spec, dict):
        return {}
    udf_fields = {}
    for key, value in rule_spec.items():
        if isinstance(key, str) and key.startswith("udf_"):
            udf_fields[key] = value
    if "udf" in rule_spec and isinstance(rule_spec.get("udf"), dict):
        for key, value in rule_spec.get("udf", {}).items():
            if isinstance(key, str) and key.startswith("udf_"):
                udf_fields[key] = value
    return udf_fields


def build_closure_payload(ticket: dict | None, actions, case_count: int | None = None, target_status: str | None = None, target_status_id: str | None = None, count_field: str | None = None, status_comment: str | None = None, resolution_text: str | None = None, closure_profiles: dict | None = None, mandatory_fields: dict | None = None) -> dict:
    if case_count is None:
        raise TypeError("build_closure_payload() missing required argument: case_count")

    action_list = list(actions or [])
    profiles = closure_profiles if isinstance(closure_profiles, dict) else {}
    mandatory = _get_mandatory_fields(mandatory_fields)
    if not mandatory and isinstance(mandatory_fields, dict):
        mandatory = [str(k) for k, v in mandatory_fields.items() if v is not None]

    request: dict = {"status": {"id": str(target_status_id or "4")}}
    if resolution_text is not None:
        request["resolution"] = {"content": str(resolution_text)}
    # status_change_comments intentionally omitted from final payload

    resolved_udf_fields: dict[str, object] = {}
    existing_udfs = ((ticket or {}).get("udf_fields") or {}) if isinstance(ticket, dict) else {}
    field_overrides: dict[str, object] = {}
    for action_name in action_list:
        profile = profiles.get(action_name)
        if not isinstance(profile, dict):
            continue
        for key, value in profile.items():
            if not isinstance(key, str) or not key.startswith("udf_"):
                continue
            if key in field_overrides and field_overrides[key] != value:
                raise ValueError("CLOSURE_CONFIGURATION_REQUIRED: Mixed closure profiles for field " + key)
            field_overrides[key] = value

    for field_name in mandatory:
        field_key = str(field_name)
        if field_key in ("item", "subcategory", "resolution"):
            continue
        current_udf = extract_ticket_udf_value(existing_udfs.get(field_key)) if isinstance(existing_udfs, dict) else None
        if field_key == (count_field or "udf_long_15301"):
            if not udf_is_empty(current_udf):
                resolved_udf_fields[field_key] = coerce_udf_value(field_key, current_udf)
            else:
                resolved_udf_fields[field_key] = int(case_count)
            continue
        if not udf_is_empty(current_udf):
            continue
        if field_key in field_overrides:
            override = field_overrides[field_key]
            if isinstance(override, dict):
                if override.get("id") not in (None, ""):
                    resolved_udf_fields[field_key] = {"id": str(override.get("id"))}
                    continue
                if override.get("name") not in (None, ""):
                    resolved_udf_fields[field_key] = {"name": str(override.get("name"))}
                    continue
            raise ValueError(f"CLOSURE_CONFIGURATION_REQUIRED: Missing mandatory ManageEngine UDF: {field_key}")
        raise ValueError(f"CLOSURE_CONFIGURATION_REQUIRED: Missing mandatory ManageEngine UDF: {field_key}")

    if resolved_udf_fields:
        request["udf_fields"] = resolved_udf_fields
    return {"request": request}


def resolve_closure(ticket: dict | None, case_count: int | None = None, actions=None, rules: dict | None = None, mandatory_fields: dict | None = None, count_field: str | None = None, target_status: str | None = None, target_status_id: str | None = None, resolution_text: str | None = None, status_comment: str | None = None, settings=None) -> dict:
    if case_count is None:
        raise TypeError("resolve_closure() missing required argument: case_count")

    settings = settings or load_settings("config/uat.yaml")
    rules_cfg = settings.closure_rules if isinstance(settings.closure_rules, dict) else {}
    env_name = str(rules_cfg.get("environment") or "uat").strip().lower() or "uat"
    env_cfg = (rules_cfg.get("env") or {}).get(env_name) if isinstance(rules_cfg.get("env"), dict) else {}
    if not isinstance(env_cfg, dict):
        env_cfg = {}

    ticket_item = (ticket or {}).get("item") if isinstance(ticket, dict) else None
    current_item_text = str(ticket_item.get("name") if isinstance(ticket_item, dict) else (ticket_item or "")).strip()
    current_subcategory = (ticket or {}).get("subcategory") if isinstance(ticket, dict) else None
    if isinstance(current_subcategory, dict):
        current_subcategory = current_subcategory.get("name") or current_subcategory.get("value")
    current_subcategory_text = str(current_subcategory or "").strip()
    ticket_udf_fields = ((ticket or {}).get("udf_fields") or {}) if isinstance(ticket, dict) else {}

    mandatory = _get_mandatory_fields(mandatory_fields)
    if not mandatory:
        mandatory = ["item", "subcategory", "resolution"]

    ticket_id = str((ticket or {}).get("id") or "")
    try:
        names_only = [str(x) for x in mandatory]
        print(f"ME_MANDATORY_FIELDS ticket={ticket_id} to_be_filled={names_only}")
    except Exception:
        pass

    def fail_closed(field_name: str):
        return {"mode": "MANUAL", "reason": "CLOSURE_CONFIGURATION_REQUIRED", "missing_fields": [field_name], "matched_rule": None, "previous_item": current_item_text or None, "payload": None, "request": None}

    def manual(reason: str, missing_fields=None):
        return {"mode": "MANUAL", "reason": reason, "missing_fields": missing_fields or [], "matched_rule": None, "previous_item": current_item_text or None, "payload": None, "request": None}

    def standard_field_verified(field_name: str) -> bool:
        if not isinstance(ticket, dict):
            return False
        value = ticket.get(field_name)
        if value is None:
            return False
        if isinstance(value, dict):
            return any(value.get(key) not in (None, "") for key in ("id", "name", "value"))
        if isinstance(value, str):
            return value.strip() != ""
        return True

    def related_validations_present() -> list[str]:
        names = []
        for field_name in mandatory:
            key = str(field_name)
            if key in ME_RELATED_VALIDATION_FIELDS:
                names.append(key)
        return names

    def payload_fields_satisfied() -> list[str]:
        out = []
        for field_name in mandatory:
            key = str(field_name)
            if key == "resolution" and resolution_text not in (None, ""):
                out.append("resolution")
            elif key.startswith("udf_") and key in {"udf_pick_8104", "udf_pick_13501", "udf_long_15301"}:
                out.append(key)
        return out

    standard_verified = [name for name in mandatory if str(name) in ME_STANDARD_REQUEST_FIELDS and standard_field_verified(str(name))]
    related = related_validations_present()
    payload_fields = payload_fields_satisfied()
    if standard_verified:
        print(f"ME_EXISTING_STANDARD_FIELDS_VERIFIED={json.dumps(standard_verified, ensure_ascii=False, sort_keys=True)}")
    if related:
        print(f"ME_RELATED_VALIDATIONS={json.dumps(related, ensure_ascii=False, sort_keys=True)}")
    if payload_fields:
        print(f"ME_CLOSURE_PAYLOAD_FIELDS_SATISFIED={json.dumps(payload_fields, ensure_ascii=False, sort_keys=True)}")

    if isinstance(rules, dict) and isinstance(rules.get("actions"), dict):
        action_map = rules["actions"]
        action_names = list(actions or []) if actions else list(action_map.keys())
        if not action_names:
            action_names = ["CANCEL"]
        if len(action_names) > 1:
            return manual("ME_MANUAL_CLOSURE_REQUIRED", [])
        action_name = action_names[0]
        config = action_map.get(action_name, {}) if isinstance(action_map, dict) else {}
        if not isinstance(config, dict):
            return manual("ME_MANUAL_CLOSURE_REQUIRED", [])
        if str(config.get("mode", "AUTO")).upper() == "MANUAL":
            return manual("ME_MANUAL_CLOSURE_REQUIRED", [])

        matched_rule = None
        for rule in config.get("rules", []) or []:
            if _match_rule(rule, ticket):
                matched_rule = rule
                break
        selected = matched_rule or config.get("default") or {}
        selected_udfs = _legacy_rule_udf_spec(selected)
        if isinstance(matched_rule, dict):
            selected_udfs.update(_legacy_rule_udf_spec(matched_rule))

        bulk_cfg = config.get("bulk") if isinstance(config, dict) else {}
        bulk_threshold = bulk_cfg.get("when_case_count_gt") if isinstance(bulk_cfg, dict) else None
        payload = {"status": {"id": str(target_status_id or "4")}}
        if resolution_text is not None:
            payload["resolution"] = {"content": str(resolution_text)}

        if "item" in mandatory and not current_item_text:
            return manual("ME_MANUAL_CLOSURE_REQUIRED", ["item"])
        if "subcategory" in mandatory and not current_subcategory_text:
            return manual("ME_MANUAL_CLOSURE_REQUIRED", ["subcategory"])
        if isinstance(bulk_cfg, dict) and bulk_threshold is not None and case_count is not None and case_count > int(bulk_threshold):
            bulk_item = bulk_cfg.get("item")
            if isinstance(bulk_item, dict) and bulk_item.get("id") not in (None, ""):
                payload["item"] = {"id": str(bulk_item.get("id")), "name": str(bulk_item.get("name") or bulk_item.get("id"))}

        udf_fields: dict[str, object] = {}
        for field_name in mandatory:
            field_key = str(field_name)
            if field_key in ME_STANDARD_REQUEST_FIELDS or field_key in ME_RELATED_VALIDATION_FIELDS or field_key in {"item", "subcategory"}:
                continue
            if field_key == (count_field or "udf_long_15301"):
                udf_fields[field_key] = str(case_count)
                continue
            if field_key.startswith("udf_"):
                current_udf = extract_ticket_udf_value(ticket_udf_fields.get(field_key))
                if not udf_is_empty(current_udf):
                    continue
                if field_key in selected_udfs:
                    value = selected_udfs[field_key]
                    if isinstance(value, dict):
                        if value.get("id") not in (None, ""):
                            udf_fields[field_key] = {"id": str(value.get("id"))}
                            continue
                        if value.get("name") not in (None, ""):
                            udf_fields[field_key] = {"name": str(value.get("name"))}
                            continue
                    if value not in (None, ""):
                        udf_fields[field_key] = value
                        continue
                if field_key in {"udf_pick_8104", "udf_pick_13501", "udf_long_15301"}:
                    if field_key in selected_udfs:
                        continue
                return manual("CLOSURE_CONFIGURATION_REQUIRED", [field_key])
        if udf_fields:
            payload["udf_fields"] = udf_fields
        if "resolution" not in payload:
            payload["resolution"] = {"content": str(resolution_text or "completed")}
        if "udf_fields" not in payload and any(field_name in {"udf_pick_8104", "udf_pick_13501", "udf_long_15301"} for field_name in mandatory):
            payload["udf_fields"] = {}
        result = {"mode": "AUTO", "reason": "AUTO", "matched_rule": matched_rule, "previous_item": current_item_text or None, "payload": {"request": payload}, "request": payload}
        return result

    mandatory = _get_mandatory_fields(mandatory_fields)
    if env_name == "prod" and not env_cfg:
        return {"mode": "MANUAL", "reason": "CLOSURE_CONFIGURATION_REQUIRED", "missing_fields": ["closure_rules.env.prod"], "matched_rule": None, "previous_item": None, "payload": None, "request": None}

    target_status_id_value = str(target_status_id or settings.me_completion_target_status_id or "4")
    payload = {"status": {"id": str(target_status_id_value)}}
    if resolution_text is not None:
        payload["resolution"] = {"content": str(resolution_text)}
    else:
        payload["resolution"] = {"content": settings.me_completion_resolution_template or "Salesforce automation completed successfully."}
    previous_item = current_item_text or None

    count_field_name = str(count_field or env_cfg.get("count_field") or settings.me_completion_count_field or "udf_long_15301")
    if case_count is not None and count_field_name:
        payload.setdefault("udf_fields", {})[count_field_name] = str(case_count)

    for field_name in mandatory:
        field_key = str(field_name)
        if field_key in ME_STANDARD_REQUEST_FIELDS or field_key in ME_RELATED_VALIDATION_FIELDS or field_key in {"item", "subcategory"}:
            continue
        if field_key.startswith("udf_") and field_key in {"udf_pick_8104", "udf_pick_13501", "udf_long_15301"}:
            pick_fields = env_cfg.get("pick_fields") if isinstance(env_cfg, dict) else {}
            if field_key in pick_fields:
                pick_choice = pick_fields[field_key]
                if isinstance(pick_choice, dict):
                    if pick_choice.get("id") not in (None, ""):
                        payload.setdefault("udf_fields", {})[field_key] = {"id": str(pick_choice.get("id"))}
                        continue
                    if pick_choice.get("name"):
                        payload.setdefault("udf_fields", {})[field_key] = {"name": str(pick_choice.get("name"))}
                        continue
            current_udf = extract_ticket_udf_value(ticket_udf_fields.get(field_key))
            if not udf_is_empty(current_udf):
                continue
            if field_key in {"udf_pick_8104", "udf_pick_13501"}:
                payload.setdefault("udf_fields", {})[field_key] = {"id": "6607"} if field_key == "udf_pick_8104" else {"id": "12698"}
                continue
            if field_key == "udf_long_15301":
                payload.setdefault("udf_fields", {})[field_key] = str(case_count)
                continue
            return fail_closed(field_key)

    if "udf_fields" in payload and not payload["udf_fields"]:
        del payload["udf_fields"]
    return {"mode": "AUTO", "reason": "AUTO", "matched_rule": None, "previous_item": previous_item, "payload": {"request": payload}, "request": payload}


def build_status_update_payload(target_status: str, status_comment: str, ticket: dict | None, mandatory_udf_fields: dict[str, object] | None) -> dict:
    request: dict = {"status": {"id": "4"}}

    ticket_udf_fields = {}
    existing = (ticket or {}).get("udf_fields") if isinstance(ticket, dict) else {}
    if isinstance(existing, dict):
        ticket_udf_fields = {str(k): v for k, v in existing.items()}

    resolved_udf_fields = {}
    mandatory_map = dict((mandatory_udf_fields or {}).items())

    for field_name, config_entry in mandatory_map.items():
        if str(field_name) in ME_STANDARD_REQUEST_FIELDS or str(field_name) in ME_RELATED_VALIDATION_FIELDS or str(field_name) in {"item", "subcategory"}:
            continue
        if not str(field_name).startswith("udf_"):
            continue
        current_value = extract_ticket_udf_value(ticket_udf_fields.get(str(field_name)))
        if not udf_is_empty(current_value):
            resolved_udf_fields[str(field_name)] = coerce_udf_value(field_name, current_value)
            continue

        if isinstance(config_entry, dict):
            fallback_value = config_entry.get("value")
            if fallback_value is None:
                fallback_value = config_entry.get("name")
            if fallback_value is None:
                fallback_value = config_entry.get("id")
        else:
            fallback_value = config_entry

        if str(field_name).startswith("udf_long_"):
            numeric_value = coerce_udf_value(field_name, fallback_value)
            if numeric_value is None or udf_is_empty(numeric_value):
                raise ValueError(f"CLOSURE_CONFIGURATION_REQUIRED: Missing mandatory ManageEngine UDF: {field_name}")
            resolved_udf_fields[str(field_name)] = numeric_value
            continue

        if fallback_value is None:
            raise ValueError(f"CLOSURE_CONFIGURATION_REQUIRED: Missing mandatory ManageEngine UDF: {field_name}")
        resolved_udf_fields[str(field_name)] = fallback_value

    request["udf_fields"] = resolved_udf_fields
    request["resolution"] = {"content": "Salesforce automation completed successfully."}
    return {"request": request}


def _safe_response_body(resp: object) -> dict:
    if resp is None:
        return {}
    try:
        body = resp.json()
    except Exception:
        return {}
    if isinstance(body, dict):
        return body
    return {"value": body}


def print_ticket_udf_snapshot(ticket: dict | None) -> None:
    if not isinstance(ticket, dict):
        print("ManageEngine ticket udf_fields=<none>")
        return
    udf_fields = ticket.get("udf_fields")
    if not isinstance(udf_fields, dict):
        print("ManageEngine ticket udf_fields=<none>")
        return
    sanitized = {str(k): sanitize_udf_value(v) for k, v in udf_fields.items()}
    print(f"ManageEngine ticket udf_fields for ticket {ticket.get('id', '')}: {json.dumps(sanitized, ensure_ascii=False, sort_keys=True)}")


def log_list_notes_result(notes: object | None, exc: Exception | None = None) -> None:
    if exc is not None:
        print(f"LIST_NOTES_FAILED {type(exc).__name__}: {exc}")
        return
    items = notes if isinstance(notes, list) else []
    print(f"LIST_NOTES count={len(items)}")
    if items:
        first = items[0]
        if isinstance(first, dict):
            print(f"LIST_NOTES_FIRST_KEYS={json.dumps(list(first.keys()), ensure_ascii=False, sort_keys=True)}")
        else:
            print(f"LIST_NOTES_FIRST_KEYS={type(first).__name__}")


def run_cycle(config:str)->None:
    s=load_settings(config)
    print(f"DRY_RUN={s.dry_run}")
    print(f"ENABLE_SALESFORCE_WRITES={s.enable_salesforce_writes}")
    print(f"ENABLE_MANAGEENGINE_WRITES={s.enable_manageengine_writes}")
    print(f"MANAGEENGINE_AUTO_CLOSE={s.manageengine_auto_close}")
    print(f"CONFIG_PATH={config}")
    print(f"BUILD_VERSION={__import__('sr_automation').__version__}")
    # Enforce flags per Stage 2 policy
    if s.dry_run:
        mode = "DRY_RUN"
    else:
        if not s.enable_salesforce_writes:
            mode = "SALESFORCE_WRITES_DISABLED"
        else:
            mode = "GUARDED_WRITES"

    me=ManageEngineClient(s); sf=SalesforceClient(s); sf.authenticate()
    audit = AuditRepository(db_url=os.getenv("AUDIT_DB_URL", None))
    summaries = me.list_open_tickets()
    print(
        f"Fetched {len(summaries)} ticket(s) matching "
        "configured Group and Open status."
    )

    def refresh_me_note_verification(ticket_id: str, parsed, existing_notes):
        equivalent_note_exists = False
        note_id = ""
        for note in existing_notes:
            if note_matches_equivalent(note, ticket_id, parsed.action, parsed.case_numbers):
                equivalent_note_exists = True
                note_id = extract_note_id(note)
                if note_id:
                    break
        return equivalent_note_exists, note_id

    outside_group = 0
    outside_template = 0
    outside_status = 0
    processed = 0
    skipped_by_date = 0
    skipped_by_tag = 0
    skipped_by_safety_filter = 0

    for summary in summaries:
        ticket_id = str(summary.get("id") or "")
        if not ticket_id:
            continue

        ticket = me.get_ticket(ticket_id)

        group = name(ticket.get("group"))
        template = name(ticket.get("template"))
        status = name(ticket.get("status"))

        # Normalize comparisons to be case-insensitive and trim whitespace
        def norm(x):
            return x.casefold().strip() if isinstance(x, str) else None

        allowed_groups_norm = {g.casefold().strip() for g in s.allowed_groups}
        allowed_templates_norm = {t.casefold().strip() for t in s.allowed_templates}
        allowed_statuses_norm = {st.casefold().strip() for st in s.ready_statuses}

        if norm(group) not in allowed_groups_norm:
            outside_group += 1
            continue

        if norm(template) not in allowed_templates_norm:
            outside_template += 1
            continue

        if norm(status) not in allowed_statuses_norm:
            outside_status += 1
            continue

        # Apply safety filter before processing

        safety = s.safety_filter_enabled
        safety_mode = s.safety_filter_mode.upper()
        date_ok = True
        tag_ok = True
        if safety:
            # default True if no child filters enabled
            child_date_enabled = s.created_within_days_enabled
            child_tag_enabled = s.required_tag_enabled
            if not (child_date_enabled or child_tag_enabled):
                safety = False
            else:
                # evaluate date
                date_ok = True
                if child_date_enabled:
                    created_field = ticket.get("created_time")
                    created_ms = extract_created_ms(created_field)
                    if created_ms is None:
                        date_ok = False
                    else:
                        # created_ms may be milliseconds epoch
                        import datetime
                        now_ms = int(datetime.datetime.utcnow().timestamp() * 1000)
                        age_ms = now_ms - created_ms
                        allowed_ms = int(s.created_within_days) * 24 * 60 * 60 * 1000
                        date_ok = age_ms <= allowed_ms

                # evaluate tag
                tag_ok = True
                if child_tag_enabled:
                    tags = tags_from_field(ticket.get("tags") or ticket.get("tag") or ticket.get("custom_fields") or [])
                    # also check custom fields: some ManageEngine instances have a 'tags' list under 'request_custom_fields'
                    # normalize to lower-case
                    tags_lower = [t.casefold() for t in tags]
                    req = s.required_tag.casefold()
                    tag_ok = any(req == t for t in tags_lower)

                if safety_mode == "ALL":
                    passed = date_ok and tag_ok
                else:
                    passed = date_ok or tag_ok

                if not passed:
                    skipped_by_safety_filter += 1
                    if not date_ok:
                        skipped_by_date += 1
                    if not tag_ok:
                        skipped_by_tag += 1
                    print(f"Ticket {ticket_id} skipped by safety filter:")
                    print(f"  created_within_days={date_ok}, required_tag={tag_ok}")
                    continue

        processed += 1

        parsed = parse_ticket(ticket_id, str(ticket.get("subject") or ""), str(ticket.get("description") or ""))
        print(f"\nTicket {ticket_id} | {parsed.action} | {parsed.confidence} | {parsed.reason_code}")
        if not parsed.case_numbers:
            print("  MANUAL_REVIEW | No Case number")
            continue

        case_map = sf.cases(parsed.case_numbers)
        task_map = sf.tasks([c["Id"] for c in case_map.values()])
        decisions = decide(ticket_id, parsed, case_map, task_map, s)
        if not s.allow_partial and any(d.outcome != "WOULD_UPDATE" and d.outcome != "ALREADY_IN_TARGET_STATE" for d in decisions):
            print("  WHOLE_TICKET_MANUAL_REVIEW | partial processing disabled")
            continue

        updates_this_run = 0
        attempts_this_run = 0
        salesforce_writes_blocked_for_cycle = False
        cases_found = len(parsed.case_numbers)
        cases_eligible = 0
        for decision in decisions:
            if decision.outcome == "WOULD_UPDATE":
                cases_eligible += 1
        patch_attempts_allowed = resolve_stage2_patch_limit(s, cases_eligible)
        print(f"CASES_FOUND={cases_found}")
        print(f"CASES_ELIGIBLE={cases_eligible}")
        print(f"PATCH_ATTEMPTS_ALLOWED={patch_attempts_allowed}")
        print(f"PATCH_ATTEMPTS_USED={attempts_this_run}")
        final_by_case: dict[str, dict[str, str]] = {}
        def record_final_case_result(case_number: str, outcome: str, before_status: str = "", after_status: str = "") -> None:
            if not case_number:
                return
            final_by_case[str(case_number)] = {
                "outcome": str(outcome or "").upper(),
                "before": str(before_status or ""),
                "after": str(after_status or ""),
            }

        for d in decisions:
            print(f"ACTION_RULE_SELECTED case={d.case_number} action={d.requested_action} rule={d.outcome}")
            print(f"ELIGIBILITY_RESULT case={d.case_number} action={d.requested_action} outcome={d.outcome} reason={d.reason}")
            print("  " + json.dumps(asdict(d), ensure_ascii=False))
            if d.outcome == "ALREADY_IN_TARGET_STATE":
                target = s.target_status.get(d.requested_action)
                idempotency = audit.make_idempotency_key(ticket_id, d.case_number, d.requested_action, s.rule_version)
                existing = audit.get_by_idempotency(idempotency)
                if existing is not None:
                    print(f"  PRESERVE_EXISTING_RESULT | idempotency={idempotency} outcome={existing.get('outcome')}")
                    continue
                record_final_case_result(d.case_number, "ALREADY_IN_TARGET_STATE", d.current_status or "", (d.current_status or target or ""))
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status=d.current_status or target or "", salesforce_id=case_map.get(d.case_number, {}).get("Id", ""),
                                    last_modified_date=case_map.get(d.case_number, {}).get("LastModifiedDate", ""), processing_state="SALESFORCE_VERIFIED", outcome="ALREADY_IN_TARGET_STATE", correlation_id=str(uuid.uuid4()))
                continue
            if d.outcome != "WOULD_UPDATE":
                continue
            target = s.target_status.get(d.requested_action)
            # Build idempotency key
            idempotency = audit.make_idempotency_key(ticket_id, d.case_number, d.requested_action, s.rule_version)
            existing = audit.get_by_idempotency(idempotency)
            if existing:
                existing_state = (existing.get("processing_state") or "").upper()
                existing_outcome = (existing.get("outcome") or "").upper()
                case = case_map.get(d.case_number)
                case_id = case.get("Id") if case else ""
                if case_id:
                    refreshed = sf.refresh_case(case_id)
                    if refreshed:
                        if existing_state == "SALESFORCE_VERIFIED" and existing_outcome in VERIFIED_SUCCESS_OUTCOMES and refreshed.get("Status") == target:
                            print(f"  SKIP_DUPLICATE | idempotency={idempotency}")
                            continue
                        if existing_state in RETRYABLE_AUDIT_STATES:
                            if refreshed.get("Status") == target:
                                if is_manual_outcome(existing_outcome):
                                    print(f"  PRESERVE_MANUAL_OR_UNCERTAIN_RESULT | idempotency={idempotency} outcome={existing_outcome}")
                                    continue
                                print(f"  ALREADY_IN_TARGET_STATE | idempotency={idempotency}")
                                record_final_case_result(d.case_number, "ALREADY_IN_TARGET_STATE", d.current_status or "", refreshed.get("Status") or "")
                                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status=refreshed.get("Status"), salesforce_id=case_id,
                                    last_modified_date=refreshed.get("LastModifiedDate", ""), processing_state="SALESFORCE_VERIFIED", outcome="ALREADY_IN_TARGET_STATE", correlation_id=str(uuid.uuid4()))
                                continue
                            print(f"  RETRY_ALLOWED | idempotency={idempotency} after {existing_state}")
                        elif existing_state == "SALESFORCE_VERIFIED" and existing_outcome in VERIFIED_SUCCESS_OUTCOMES:
                            print(f"  RECONCILIATION_REQUIRED | Salesforce is not in target state for idempotency={idempotency}")
                            audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                requested_action=d.requested_action, rule_version=s.rule_version,
                                before_status=d.current_status or "", intended_status=target,
                                after_status=refreshed.get("Status"), salesforce_id=case_id,
                                last_modified_date=refreshed.get("LastModifiedDate", ""), processing_state="FAILED", outcome="RECONCILIATION_REQUIRED", correlation_id=str(uuid.uuid4()))
                            continue
                    else:
                        print(f"  CASE_NOT_FOUND_ON_REREAD | idempotency={idempotency}")
                        audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                            requested_action=d.requested_action, rule_version=s.rule_version,
                            before_status=d.current_status or "", intended_status=target,
                            after_status="", salesforce_id=case_id, last_modified_date="", processing_state="FAILED", outcome="CASE_NOT_FOUND_ON_REREAD", correlation_id=str(uuid.uuid4()))
                        continue
                elif existing_state == "SALESFORCE_VERIFIED" and existing_outcome in VERIFIED_SUCCESS_OUTCOMES:
                    print(f"  RECONCILIATION_REQUIRED | Verified audit exists without Salesforce case data for idempotency={idempotency}")
                    audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                        requested_action=d.requested_action, rule_version=s.rule_version,
                        before_status=d.current_status or "", intended_status=target,
                        after_status="", salesforce_id="", last_modified_date="", processing_state="FAILED", outcome="RECONCILIATION_REQUIRED", correlation_id=str(uuid.uuid4()))
                    continue

            # If a previous uncertain result blocked writes for this cycle, block remaining
            if salesforce_writes_blocked_for_cycle:
                print("  WRITE_BLOCKED_AFTER_UNCERTAIN_RESULT | Global circuit breaker")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=s.target_status.get(d.requested_action),
                                    after_status="", salesforce_id=case_map.get(d.case_number, {}).get("Id", ""),
                                    last_modified_date=case_map.get(d.case_number, {}).get("LastModifiedDate", ""), processing_state="FAILED", outcome="WRITE_BLOCKED_AFTER_UNCERTAIN_RESULT", correlation_id=str(uuid.uuid4()))
                continue

            # Respect stage2 max updates as attempts (count attempts, not only successful updates)
            if patch_attempts_allowed is not None and attempts_this_run >= patch_attempts_allowed:
                print("  UPDATE_LIMIT_REACHED | Max PATCH attempts reached for this run")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=s.target_status.get(d.requested_action),
                                    after_status="", salesforce_id=case_map.get(d.case_number, {}).get("Id", ""),
                                    last_modified_date=case_map.get(d.case_number, {}).get("LastModifiedDate", ""), processing_state="FAILED", outcome="UPDATE_LIMIT_REACHED", correlation_id=str(uuid.uuid4()))
                continue

            # If DRY_RUN -> report WOULD_UPDATE and record audit as DETECTED
            if mode == "DRY_RUN":
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=s.target_status.get(d.requested_action),
                                    after_status="", salesforce_id=case_map.get(d.case_number, {}).get("Id", ""),
                                    last_modified_date=case_map.get(d.case_number, {}).get("LastModifiedDate", ""),
                                    processing_state="DETECTED", outcome="WOULD_UPDATE", correlation_id=str(uuid.uuid4()))
                continue

            # If writes disabled
            if mode == "SALESFORCE_WRITES_DISABLED":
                print("  SALESFORCE_WRITES_DISABLED | Writes are disabled by env flag")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=s.target_status.get(d.requested_action),
                                    after_status="", salesforce_id=case_map.get(d.case_number, {}).get("Id", ""),
                                    last_modified_date=case_map.get(d.case_number, {}).get("LastModifiedDate", ""),
                                    processing_state="DETECTED", outcome="SALESFORCE_WRITES_DISABLED", correlation_id=str(uuid.uuid4()))
                continue

            # GUARD: Re-fetch Case and Tasks, re-validate LastModifiedDate
            case = case_map.get(d.case_number)
            if not case:
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=s.target_status.get(d.requested_action),
                                    after_status="", salesforce_id="", last_modified_date="", processing_state="FAILED", outcome="CASE_NOT_FOUND", correlation_id=str(uuid.uuid4()))
                continue
            case_id = case.get("Id")
            refreshed = sf.refresh_case(case_id)
            refreshed_tasks = sf.refresh_tasks_for_case(case_id) if hasattr(sf, "refresh_tasks_for_case") else []
            if not refreshed:
                print("  FAILED | Case disappeared on re-read")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=s.target_status.get(d.requested_action),
                                    after_status="", salesforce_id=case_id, last_modified_date="", processing_state="FAILED", outcome="CASE_NOT_FOUND_ON_REREAD", correlation_id=str(uuid.uuid4()))
                continue
            if refreshed.get("LastModifiedDate") != case.get("LastModifiedDate"):
                print("  STALE_RECORD | Case changed since validation")
                record_final_case_result(d.case_number, "STALE_RECORD", d.current_status or "", refreshed.get("Status") or "")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=s.target_status.get(d.requested_action),
                                    after_status=refreshed.get("Status") or "", salesforce_id=case_id, last_modified_date=refreshed.get("LastModifiedDate",""), processing_state="FAILED", outcome="STALE_RECORD", correlation_id=str(uuid.uuid4()))
                continue

            # Re-validate the current Salesforce state before any guarded retry or PATCH.
            record_type = (refreshed.get("RecordType") or {}).get("Name")
            if (record_type or "").casefold() in s.excluded_record_types:
                print("  MANUAL_REVIEW_RECORD_TYPE | Revalidation blocked excluded Record Type")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status=refreshed.get("Status"), salesforce_id=case_id, last_modified_date=refreshed.get("LastModifiedDate",""), processing_state="FAILED", outcome="MANUAL_REVIEW_RECORD_TYPE", correlation_id=str(uuid.uuid4()))
                continue
            if refreshed.get("Status") not in s.eligible_statuses.get(d.requested_action, set()) and refreshed.get("Status") != target:
                print(f"  INVALID_CURRENT_STATUS | Salesforce status {refreshed.get('Status')} is not eligible for {d.requested_action}")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status=refreshed.get("Status"), salesforce_id=case_id, last_modified_date=refreshed.get("LastModifiedDate",""), processing_state="FAILED", outcome="INVALID_CURRENT_STATUS", correlation_id=str(uuid.uuid4()))
                continue

            # Check already in target state
            if str(refreshed.get("Status") or "").strip().casefold() == str(target or "").strip().casefold():
                print(f"SF_ALREADY_IN_TARGET_STATE case={d.case_number} action={d.requested_action} status={target}")
                print("  ALREADY_IN_TARGET_STATE | No patch needed")
                record_final_case_result(d.case_number, "ALREADY_IN_TARGET_STATE", d.current_status or "", refreshed.get("Status") or "")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status=refreshed.get("Status"), salesforce_id=case_id, last_modified_date=refreshed.get("LastModifiedDate",""), processing_state="SALESFORCE_VERIFIED", outcome="ALREADY_IN_TARGET_STATE", correlation_id=str(uuid.uuid4()))
                continue

            # Activity checks per rules (simplified to match existing logic)
            open_count = sum(not bool(t.get("IsClosed")) for t in refreshed_tasks)
            rejected_count = sum(bool(t.get("IsClosed")) and str(t.get("Status","")) .casefold() in {"reject","rejected"} for t in refreshed_tasks)
            if d.requested_action in {"CANCEL","CLOSE"} and open_count and s.activity_requirements.get("require_no_open_activities", False):
                print("  OPEN_ACTIVITIES | Blocking update")
                record_final_case_result(d.case_number, "OPEN_ACTIVITIES", d.current_status or "", refreshed.get("Status") or "")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status=refreshed.get("Status") or "", salesforce_id=case_id, last_modified_date=refreshed.get("LastModifiedDate",""), processing_state="FAILED", outcome="OPEN_ACTIVITIES", correlation_id=str(uuid.uuid4()))
                continue
            if d.requested_action=="REJECT" and s.activity_requirements.get("require_closed_rejection_activity", True) and not rejected_count:
                print("  REJECTION_EVIDENCE_MISSING | No matching closed reject activity")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status="", salesforce_id=case_id, last_modified_date=refreshed.get("LastModifiedDate",""), processing_state="FAILED", outcome="REJECTION_EVIDENCE_MISSING", correlation_id=str(uuid.uuid4()))
                continue

            # Prepare patch fields
            fields = {"Status": target}
            if d.requested_action=="REJECT" and s.comment_field and s.comment_template:
                comment = s.comment_template.replace("{ticket}", ticket_id).replace("{case}", d.case_number)
                fields[s.comment_field] = comment

            # Re-fetch case and tasks immediately before PATCH and re-validate LastModifiedDate
            pre_patch_case = sf.refresh_case(case_id)
            pre_patch_tasks = sf.refresh_tasks_for_case(case_id)
            if not pre_patch_case:
                print("  FAILED | Case disappeared on immediate re-read")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status="", salesforce_id=case_id, last_modified_date="", processing_state="FAILED", outcome="CASE_NOT_FOUND_ON_REREAD", correlation_id=str(uuid.uuid4()))
                continue
            if pre_patch_case.get("LastModifiedDate") != refreshed.get("LastModifiedDate"):
                print("  STALE_RECORD | Case changed between validation and immediate pre-PATCH read")
                record_final_case_result(d.case_number, "STALE_RECORD", d.current_status or "", pre_patch_case.get("Status") or refreshed.get("Status") or "")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status=pre_patch_case.get("Status") or refreshed.get("Status") or "", salesforce_id=case_id, last_modified_date=pre_patch_case.get("LastModifiedDate",""), processing_state="FAILED", outcome="STALE_RECORD", correlation_id=str(uuid.uuid4()))
                continue

            # Start update attempt: increment attempts counter BEFORE PATCH
            attempts_this_run += 1
            print(f"PATCH_ATTEMPTS_USED={attempts_this_run}")
            print(f"SALESFORCE_UPDATE_STARTED case={d.case_number} action={d.requested_action} target={target}")
            print(f"  PATCH_START -> {d.case_number} -> {fields} | attempt {attempts_this_run}")
            audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                requested_action=d.requested_action, rule_version=s.rule_version,
                                before_status=d.current_status or "", intended_status=target,
                                after_status="", salesforce_id=case_id, last_modified_date=pre_patch_case.get("LastModifiedDate",""), processing_state="SALESFORCE_UPDATE_STARTED", outcome="PATCHING", correlation_id=str(uuid.uuid4()))
            try:
                resp = sf.update_case(case_id, fields)
                resp.raise_for_status()
            except httpx.RequestError as exc:
                print(f"  PATCH_TIMEOUT_OR_NETWORK | {exc}")
                # Do NOT retry immediately. Re-authenticate if possible and re-read once.
                try:
                    sf.authenticate()
                except Exception:
                    pass
                post = sf.refresh_case(case_id)
                if post and post.get("Status") == target and (d.requested_action!="REJECT" or post.get(s.comment_field)==fields.get(s.comment_field)):
                    record_final_case_result(d.case_number, "SUCCESS_AFTER_TIMEOUT_RECONCILIATION", d.current_status or "", post.get("Status") or "")
                    audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                        requested_action=d.requested_action, rule_version=s.rule_version,
                                        before_status=d.current_status or "", intended_status=target,
                                        after_status=post.get("Status"), salesforce_id=case_id, last_modified_date=post.get("LastModifiedDate",""), processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS_AFTER_TIMEOUT_RECONCILIATION", correlation_id=str(uuid.uuid4()))
                    updates_this_run += 1
                else:
                    audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                        requested_action=d.requested_action, rule_version=s.rule_version,
                                        before_status=d.current_status or "", intended_status=target,
                                        after_status=post.get("Status") if post else "", salesforce_id=case_id, last_modified_date=post.get("LastModifiedDate","" ) if post else "", processing_state="FAILED", outcome="UPDATE_RESULT_UNKNOWN", correlation_id=str(uuid.uuid4()))
                # Activate global circuit breaker to stop further writes this run
                salesforce_writes_blocked_for_cycle = True
                print("  GLOBAL_CIRCUIT_BREAKER_ACTIVATED | No further PATCH attempts this run")
                continue
            except httpx.HTTPStatusError as exc:
                status_code = getattr(exc.response, "status_code", "<unknown>")
                try:
                    error_body = exc.response.json()
                except ValueError:
                    error_body = exc.response.text if getattr(exc.response, "text", None) else ""
                error_code, message, fields = _extract_salesforce_error_details(error_body)
                sanitized_fields = fields if fields else []
                print(f"  PATCH_FAILED | HTTP {status_code} | case={d.case_number}")
                print(f"  PATCH_RESPONSE | case={d.case_number} errorCode={error_code} message={message} fields={sanitized_fields}")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status="", salesforce_id=case_id, last_modified_date=refreshed.get("LastModifiedDate",""), processing_state="FAILED", outcome=f"HTTP_{status_code}", correlation_id=str(uuid.uuid4()))
                continue

            # Re-read and verify
            post = sf.refresh_case(case_id)
            if post and post.get("Status") == target and (d.requested_action!="REJECT" or post.get(s.comment_field)==fields.get(s.comment_field)):
                record_final_case_result(d.case_number, "SUCCESS", d.current_status or "", post.get("Status") or "")
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status=post.get("Status"), salesforce_id=case_id, last_modified_date=post.get("LastModifiedDate",""), processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id=str(uuid.uuid4()))
                updates_this_run += 1
            else:
                audit.insert_audit(idempotency_key=idempotency, ticket_id=ticket_id, case_number=d.case_number,
                                    requested_action=d.requested_action, rule_version=s.rule_version,
                                    before_status=d.current_status or "", intended_status=target,
                                    after_status=post.get("Status") if post else "", salesforce_id=case_id, last_modified_date=post.get("LastModifiedDate","") if post else "", processing_state="FAILED", outcome="VERIFICATION_FAILED", correlation_id=str(uuid.uuid4()))
                continue
        # After processing all cases for this ticket, prepare ManageEngine note
        me_writer = ManageEngineNoteWriter(s)
        audits = audit.get_by_ticket(ticket_id)
        by_case = {}
        for a in audits:
            cn = a.get("case_number")
            if not cn:
                continue
            by_case[cn] = a

        final_by_case = {}
        for a in audits:
            cn = a.get("case_number")
            if not cn:
                continue
            final_by_case[str(cn)] = {
                "outcome": (a.get("outcome") or "").upper(),
                "before": str(a.get("before_status") or ""),
                "after": str(a.get("after_status") or case_map.get(str(cn), {}).get("Status") or ""),
            }

        case_lines = []
        case_result_rows = []
        success_count = manual_count = failed_count = 0
        for decision in decisions:
            case_number = decision.case_number
            final = final_by_case.get(str(case_number)) if case_number else None
            if final is not None:
                outcome = str(final.get("outcome") or "").upper()
                before_status = final.get("before") or decision.current_status or ""
                after_status = final.get("after") or case_map.get(case_number, {}).get("Status") or decision.current_status or ""
            else:
                outcome = (decision.outcome or "").upper()
                before_status = decision.current_status or ""
                after_status = case_map.get(case_number, {}).get("Status") or decision.current_status or ""

            if is_success_outcome(outcome):
                success_count += 1
            elif is_manual_outcome(outcome):
                manual_count += 1
            else:
                failed_count += 1

            action = decision.requested_action or ""
            if outcome == "ALREADY_IN_TARGET_STATE":
                line = f"{case_number} | {action} | {outcome} | {after_status or before_status}"
            else:
                line = f"{case_number} | {action} | {outcome} | {before_status} -> {after_status or before_status}"
            case_lines.append(line)
            case_result_rows.append({
                "case_number": case_number,
                "action": action,
                "outcome": outcome,
                "before_status": before_status,
                "after_status": after_status,
            })

        all_successful = bool(case_result_rows) and all(is_success_outcome(row["outcome"]) for row in case_result_rows)
        salesforce_target_verified = all_successful
        manual_or_failed = manual_count > 0 or failed_count > 0
        all_cases_verified_for_me_completion = all_successful and not manual_or_failed and not failed_count
        print(f"ALL_CASES_VERIFIED_FOR_ME_COMPLETION={str(all_cases_verified_for_me_completion).lower()}")

        # Decide whether to write note
        # Respect environment flags
        if mode == "DRY_RUN":
            print(f"DRY_RUN: skipping ManageEngine note for ticket {ticket_id}")
        if not s.enable_manageengine_writes:
            print(f"ManageEngine writes disabled: not adding note for ticket {ticket_id}")
            audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_SUMMARY", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_SKIPPED", outcome="MANAGEENGINE_WRITES_DISABLED", correlation_id=str(uuid.uuid4()))
            continue

        if s.manageengine_auto_close:
            me_note_verified = any((a.get("me_note_state") or "").upper() == "ME_NOTE_VERIFIED" or (a.get("processing_state") or "").upper() == "ME_NOTE_VERIFIED" for a in audits)
            if not all_successful or not salesforce_target_verified or manual_or_failed:
                print(f"ME_PARTIAL_RESULT_MANUAL_REVIEW ticket={ticket_id} successful={success_count} manual={manual_count} failed={failed_count}")
                if mode == "DRY_RUN":
                    dry_run_note = me_writer.build_summary(
                        ticket_id=ticket_id,
                        environment="UAT",
                        action=str(parsed.action).upper(),
                        case_lines=case_lines,
                        success_count=success_count,
                        manual_count=manual_count,
                        failed_count=failed_count,
                        correlation_id=str(uuid.uuid4()),
                    )
                    print(f"WOULD_ADD_ME_NOTE ticket={ticket_id}: {json.dumps(dry_run_note, ensure_ascii=False, sort_keys=True)}")
                    print(f"DRY_RUN: skipping ManageEngine note for partial-result ticket {ticket_id}")
                    continue
                audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_SUMMARY", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_CLOSE_FAILED", outcome="ME_PARTIAL_RESULT_MANUAL_REVIEW", correlation_id=str(uuid.uuid4()))
                audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_SUMMARY", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_CLOSE_FAILED", outcome="MANUAL_REVIEW_REQUIRED", correlation_id=str(uuid.uuid4()))
                if not s.enable_manageengine_writes:
                    print(f"ManageEngine writes disabled: not adding note for ticket {ticket_id}")
                else:
                    note_added = ensure_summary_note(ticket_id, parsed, case_result_rows, me_writer, audit)
                    if note_added:
                        me_note_verified = True
                continue
            if s.dry_run and not me_note_verified:
                print("MANAGEENGINE_AUTO_CLOSE is enabled but dry-run is active and no verified note was found; skipping Stage 4")
                continue
            if not s.enable_salesforce_writes or not s.enable_manageengine_writes:
                print("MANAGEENGINE_AUTO_CLOSE is enabled but environment flags prevent closing; skipping Stage 4")
                continue
            try:
                existing_notes = me_writer.list_notes(ticket_id, timeout=30.0)
                log_list_notes_result(existing_notes)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                existing_notes = []
                log_list_notes_result(existing_notes, exc)
            matched_existing_note = None
            try:
                if callable(getattr(me_writer, "get_note", None)):
                    note_candidates = list(existing_notes or [])
                    ids = [str(extract_note_id(n)) for n in note_candidates if extract_note_id(n)]
                    ids = [i for i in ids if i]
                    ids = ids[-10:]
                    for nid in ids:
                        try:
                            fetched = me_writer.get_note(ticket_id, nid, timeout=30.0)
                        except Exception:
                            fetched = None
                        if not fetched:
                            continue
                        text = str((fetched.get("description") or fetched.get("note") or ""))
                        if "WOULD_UPDATE" in text or "MANUAL_REVIEW" in text or "Would update" in text:
                            continue
                        if note_matches_equivalent(fetched, ticket_id, parsed.action, parsed.case_numbers):
                            matched_existing_note = fetched
                            note_id = nid
                            if note_id:
                                audit.set_me_note(ticket_id=ticket_id, note_id=note_id, state="ME_NOTE_VERIFIED", correlation_id=str(uuid.uuid4()))
                                print(f"EXISTING_ME_NOTE_VERIFIED | note_id={note_id}")
                            me_note_verified = True
                            break
                else:
                    for n in existing_notes or []:
                        text = str((n.get("description") or n.get("note") or "")) if isinstance(n, dict) else str(n or "")
                        if "WOULD_UPDATE" in text or "MANUAL_REVIEW" in text or "Would update" in text:
                            continue
                        if note_matches_equivalent(n, ticket_id, parsed.action, parsed.case_numbers):
                            matched_existing_note = n
                            note_id = extract_note_id(n)
                            if note_id:
                                audit.set_me_note(ticket_id=ticket_id, note_id=note_id, state="ME_NOTE_VERIFIED", correlation_id=str(uuid.uuid4()))
                                print(f"EXISTING_ME_NOTE_VERIFIED | note_id={note_id}")
                            me_note_verified = True
                            break
            except Exception:
                pass
            if not me_note_verified:
                me_note_verified = ensure_summary_note(ticket_id, parsed, case_result_rows, me_writer, audit)
            current_ticket = me.get_ticket(ticket_id)
            current_status_name = name(current_ticket.get("status"))
            if current_status_name and str(current_status_name).casefold() in {"closed", "resolved"}:
                print(f"ALREADY_COMPLETED for ticket {ticket_id}")
                audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=s.me_completion_target_status or "Resolved", after_status=current_status_name, salesforce_id="", last_modified_date="", processing_state="ME_RESOLVED_VERIFIED", outcome="ALREADY_COMPLETED", correlation_id=str(uuid.uuid4()))
            else:
                if not all_successful or not salesforce_target_verified or manual_or_failed:
                    print(f"ME_COMPLETE_SKIPPED for ticket {ticket_id}: all_successful={all_successful}, salesforce_target_verified={salesforce_target_verified}, me_note_verified={me_note_verified}, manual_or_failed={manual_or_failed}")
                    audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=s.me_completion_target_status or "Resolved", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_COMPLETE_SKIPPED", outcome="ME_COMPLETE_SKIPPED", correlation_id=str(uuid.uuid4()))
                    continue
                target_status_id = s.me_completion_target_status_id or "4"
                resolution_text = s.me_completion_resolution_template or s.me_closure_resolution_template or "Salesforce automation completed successfully."
                if s.dry_run:
                    payload = {
                        "request": {
                            "status": {"id": str(target_status_id or "4")},
                            "udf_fields": {"udf_pick_8104": {"id": "6607"}, "udf_pick_13501": {"id": "12698"}, "udf_long_15301": str(len(case_lines or []))},
                            "resolution": {"content": resolution_text},
                        }
                    }
                    print(f"WOULD_SEND_ME_STATUS_PAYLOAD={json.dumps(payload, ensure_ascii=False, sort_keys=True)}")
                    continue

                audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_ASSIGN", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_ASSIGNMENT_STARTED", outcome="ME_ASSIGNMENT_STARTED", correlation_id=str(uuid.uuid4()))
                tech = None
                if s.me_technician_id:
                    tech = {"id": s.me_technician_id}
                elif s.me_technician_name:
                    tech = {"name": s.me_technician_name}
                try:
                    if tech and callable(getattr(me, "assign_technician", None)):
                        before_ticket = me.get_ticket(ticket_id)
                        already_assigned = False
                        if isinstance(before_ticket, dict):
                            current_tech = before_ticket.get("technician")
                            if isinstance(current_tech, dict):
                                current_id = str(current_tech.get("id") or "")
                                current_name = str(current_tech.get("name") or "")
                                if s.me_technician_id and current_id == str(s.me_technician_id):
                                    already_assigned = True
                                elif s.me_technician_name and current_name.casefold().strip() == s.me_technician_name.casefold().strip():
                                    already_assigned = True
                        if already_assigned:
                            print(f"ME_ALREADY_ASSIGNED for ticket {ticket_id}: technician_id={s.me_technician_id or s.me_technician_name}")
                            audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_ASSIGN", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_ALREADY_ASSIGNED", outcome="ME_ALREADY_ASSIGNED", correlation_id=str(uuid.uuid4()))
                        else:
                            resp = me.assign_technician(ticket_id, tech)
                            if hasattr(resp, "raise_for_status"):
                                resp.raise_for_status()
                            print(f"ME_ASSIGNED ticket={ticket_id}")
                            audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_ASSIGN", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_ASSIGNED", outcome="ME_ASSIGNED", correlation_id=str(uuid.uuid4()))
                    elif tech:
                        print(f"ME_ASSIGNMENT_SKIPPED for ticket {ticket_id}: client missing assign_technician")
                except (httpx.RequestError, KeyError, TypeError, ValueError, AttributeError, RuntimeError) as exc:
                    print(f"ME_ASSIGN_TIMEOUT_OR_NETWORK for ticket {ticket_id}: {exc}")
                except httpx.HTTPStatusError as exc:
                    print(f"ME_ASSIGN_FAILED for ticket {ticket_id}: HTTP {exc.response.status_code}")

                completion_mode = (s.me_completion_mode or "STATUS_UPDATE").upper()
                target_status = s.me_completion_target_status or "Resolved"
                target_status_id = s.me_completion_target_status_id or "4"
                resolution_text = s.me_completion_resolution_template or s.me_closure_resolution_template or "Salesforce automation completed successfully."
                status_comment_text = s.me_completion_status_comment_template or s.me_closure_comment_template or "Request completed through Salesforce SR automation. Refer to the private automation note for Case details."
                if completion_mode == "STATUS_UPDATE":
                    try:
                        ticket_snapshot = me.get_ticket(ticket_id)
                        mandatory_fields = None
                        get_mandatory_fields = getattr(me, "get_mandatory_fields", None)
                        if callable(get_mandatory_fields):
                            mandatory_fields = get_mandatory_fields(ticket_id, target_status_id)
                        else:
                            mandatory_fields = {}
                        status_input = resolve_closure(
                            ticket=ticket_snapshot,
                            case_count=len(case_lines or []),
                            mandatory_fields=mandatory_fields,
                            target_status=target_status,
                            target_status_id=target_status_id,
                            resolution_text=resolution_text,
                            status_comment=status_comment_text,
                            settings=s,
                        )
                    except ValueError as exc:
                        print(str(exc))
                        audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status="", salesforce_id="", last_modified_date="", processing_state="CLOSURE_CONFIGURATION_REQUIRED", outcome="CLOSURE_CONFIGURATION_REQUIRED", correlation_id=str(uuid.uuid4()))
                        continue
                    except httpx.HTTPStatusError as exc:
                        print_http_error_details("ME_MANDATORY_FIELDS_FAILED", exc)
                        audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status="", salesforce_id="", last_modified_date="", processing_state="CLOSURE_CONFIGURATION_REQUIRED", outcome="CLOSURE_CONFIGURATION_REQUIRED", correlation_id=str(uuid.uuid4()))
                        continue

                    if status_input.get("mode") == "MANUAL":
                        print(f"ME_CLOSURE_MANUAL ticket={ticket_id} reason={status_input.get('reason')} "
                              f"missing={status_input.get('missing_fields')} previous_item={status_input.get('previous_item')}")
                        audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status="", salesforce_id="", last_modified_date="", processing_state="CLOSURE_CONFIGURATION_REQUIRED", outcome=status_input.get("reason") or "CLOSURE_CONFIGURATION_REQUIRED", correlation_id=str(uuid.uuid4()))
                        continue

                    audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status="", salesforce_id="", last_modified_date="", processing_state="ME_STATUS_UPDATE_STARTED", outcome="ME_STATUS_UPDATE_STARTED", correlation_id=str(uuid.uuid4()))
                    try:
                        body = status_input["payload"]
                        request_obj = body.get("request", {}) if isinstance(body, dict) else {}
                        final = {"request": {}}
                        st = request_obj.get("status")
                        if isinstance(st, dict) and st.get("id") not in (None, ""):
                            final["request"]["status"] = {"id": str(st.get("id"))}
                        if request_obj.get("resolution") is not None:
                            final["request"]["resolution"] = {"content": request_obj.get("resolution").get("content")}
                        udfs = request_obj.get("udf_fields") or {}
                        sanitized_udfs = {}
                        if isinstance(udfs, dict):
                            for k, v in udfs.items():
                                if str(k).startswith("udf_long_"):
                                    sanitized_udfs[str(k)] = str(v)
                                else:
                                    sanitized_udfs[str(k)] = v
                        if sanitized_udfs:
                            final["request"]["udf_fields"] = sanitized_udfs
                        request_keys = list(final.get("request", {}).keys()) if isinstance(final, dict) else []
                        udf_keys = list((final.get("request", {}).get("udf_fields") or {}).keys()) if isinstance(final.get("request", {}).get("udf_fields"), dict) else []
                        print(f"ME_STATUS_UPDATE_FIELDS ticket={ticket_id} request_keys={request_keys} udf_fields={udf_keys}")
                        print(f"ME_STATUS_PAYLOAD={json.dumps(final, ensure_ascii=False, sort_keys=True)}")
                        print("ME_STATUS_UPDATE_STARTED")
                        if not callable(getattr(me, "update_request", None)):
                            print(f"ME_STATUS_UPDATE_SKIPPED for ticket {ticket_id}: client missing update_request")
                            audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status="", salesforce_id="", last_modified_date="", processing_state="ME_STATUS_UPDATE_SKIPPED", outcome="ME_STATUS_UPDATE_SKIPPED", correlation_id=str(uuid.uuid4()))
                            continue
                        try:
                            resp = me.update_request(ticket_id, final)
                        except (httpx.RequestError, KeyError, TypeError, ValueError, AttributeError, RuntimeError) as exc:
                            print(f"ME_STATUS_UPDATE_TIMEOUT_OR_NETWORK for ticket {ticket_id}: {exc}")
                            audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status="", salesforce_id="", last_modified_date="", processing_state="ME_STATUS_UPDATE_SKIPPED", outcome="ME_STATUS_UPDATE_SKIPPED", correlation_id=str(uuid.uuid4()))
                            continue
                        print(f"ME_STATUS_UPDATE_HTTP_STATUS={getattr(resp, 'status_code', 'unknown')}")
                        print(f"ME_STATUS_UPDATE_RESPONSE={json.dumps(_safe_response_body(resp), ensure_ascii=False, sort_keys=True)}")
                        if hasattr(resp, "raise_for_status"):
                            try:
                                resp.raise_for_status()
                            except (httpx.HTTPStatusError, KeyError, TypeError, ValueError, AttributeError, RuntimeError) as exc:
                                print(f"ME_STATUS_UPDATE_REJECTED for ticket {ticket_id}: {exc}")
                                audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status="", salesforce_id="", last_modified_date="", processing_state="ME_STATUS_UPDATE_SKIPPED", outcome="ME_STATUS_UPDATE_SKIPPED", correlation_id=str(uuid.uuid4()))
                                continue
                        print("ME_STATUS_UPDATE_SUCCEEDED")
                        if s.me_completion_verify_final_status:
                            updated = me.get_ticket(ticket_id)
                            status_value = updated.get("status") if isinstance(updated, dict) else None
                            status_name = None
                            if isinstance(status_value, dict):
                                status_name = str(status_value.get("name") or "").strip() or None
                                if status_name is None and str(status_value.get("id") or "") == str(target_status_id or "4"):
                                    status_name = str(target_status or "Resolved")
                            else:
                                status_name = name(status_value)
                            sent_udfs = final.get("request", {}).get("udf_fields", {}) or {}
                            udf_ok = True
                            for field_name in sent_udfs:
                                current_udf = extract_ticket_udf_value((updated.get("udf_fields") or {}).get(field_name)) if isinstance(updated, dict) else None
                                if udf_is_empty(current_udf):
                                    udf_ok = False
                                    break
                            if (status_name or "").casefold() == str(target_status).casefold() and udf_ok:
                                audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status=status_name, salesforce_id="", last_modified_date="", processing_state="ME_RESOLVED_VERIFIED", outcome="ME_RESOLVED_VERIFIED", correlation_id=str(uuid.uuid4()))
                                print(f"ME_RESOLVED_VERIFIED for ticket {ticket_id} with status {status_name}")
                                continue
                            audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status=status_name, salesforce_id="", last_modified_date="", processing_state="ME_STATUS_REJECTED", outcome="ME_STATUS_REJECTED", correlation_id=str(uuid.uuid4()))
                            print(f"ME_STATUS_REJECTED for ticket {ticket_id}: status={status_name!r}; expected={target_status!r}; udf_ok={udf_ok}")
                    except httpx.HTTPStatusError as exc:
                        print_http_error_details("ME_STATUS_UPDATE_FAILED", exc)
                        outcome = f"HTTP_{getattr(exc.response, 'status_code', 'UNKNOWN')}" if getattr(exc.response, 'status_code', None) else "HTTP_ERROR"
                        audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status="", salesforce_id="", last_modified_date="", processing_state="STATUS_CONFIGURATION_REQUIRED", outcome=outcome, correlation_id=str(uuid.uuid4()))
                        print("STATUS_CONFIGURATION_REQUIRED: status update rejected by ManageEngine; automatic retry is not allowed.")
                        continue
                    except httpx.RequestError as exc:
                        print(f"ME_STATUS_UPDATE_TIMEOUT_OR_NETWORK for ticket {ticket_id}: {exc}")
                        if s.me_completion_verify_final_status:
                            updated = me.get_ticket(ticket_id)
                            status_name = name(updated.get("status")) if isinstance(updated, dict) else None
                            if (status_name or "").casefold() == str(target_status).casefold():
                                audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_COMPLETION", rule_version=s.rule_version, before_status="", intended_status=target_status, after_status=status_name, salesforce_id="", last_modified_date="", processing_state="ME_RESOLVED_VERIFIED", outcome="SUCCESS_AFTER_TIMEOUT_RECONCILIATION", correlation_id=str(uuid.uuid4()))
                                print(f"ME_RESOLVED_VERIFIED via reconciliation for ticket {ticket_id}")
                                continue
                else:
                    resolution_text = s.me_closure_resolution_template or "Salesforce automation completed successfully."
                    closure_comment_text = s.me_closure_comment_template or "Request completed through Salesforce SR automation. Refer to the private automation note for Case details."
                    closure_info = {"request": {"closure_info": {
                        "requester_ack_resolution": bool(s.me_requester_ack_resolution),
                        "requester_ack_comments": resolution_text,
                        "closure_comments": closure_comment_text,
                        "resolution": resolution_text,
                    }}}
                    if s.me_closure_code_id or s.me_closure_code_name:
                        closure_code = {}
                        if s.me_closure_code_id:
                            closure_code["id"] = s.me_closure_code_id
                        if s.me_closure_code_name:
                            closure_code["name"] = s.me_closure_code_name
                        closure_info["request"]["closure_info"]["closure_code"] = closure_code
                    audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_CLOSE", rule_version=s.rule_version, before_status="", intended_status="Closed", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_CLOSE_STARTED", outcome="ME_CLOSE_STARTED", correlation_id=str(uuid.uuid4()))
                    try:
                        if not callable(getattr(me, "close_ticket", None)):
                            print(f"ME_CLOSE_SKIPPED for ticket {ticket_id}: client missing close_ticket")
                            audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_CLOSE", rule_version=s.rule_version, before_status="", intended_status="Closed", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_CLOSE_SKIPPED", outcome="ME_CLOSE_SKIPPED", correlation_id=str(uuid.uuid4()))
                            continue
                        resp = me.close_ticket(ticket_id, closure_info)
                        if hasattr(resp, "raise_for_status"):
                            resp.raise_for_status()
                    except (httpx.HTTPStatusError, KeyError, TypeError, ValueError, AttributeError, RuntimeError) as exc:
                        print(f"ME_CLOSE_FAILED for ticket {ticket_id}: {exc}")
                        continue
                    updated = me.get_ticket(ticket_id)
                    if (name(updated.get("status")) or "").casefold() == "closed":
                        audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_CLOSE", rule_version=s.rule_version, before_status="", intended_status="Closed", after_status="Closed", salesforce_id="", last_modified_date="", processing_state="ME_CLOSED_VERIFIED", outcome="ME_CLOSED_VERIFIED", correlation_id=str(uuid.uuid4()))
                        print(f"ManageEngine ticket {ticket_id} closed and verified")

        try:
            if callable(getattr(me_writer, "list_notes", None)):
                existing_notes = me_writer.list_notes(ticket_id, timeout=30.0)
                log_list_notes_result(existing_notes)
            else:
                existing_notes = []
                log_list_notes_result(existing_notes)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            existing_notes = []
            log_list_notes_result(existing_notes, exc)
        equivalent_note_exists = any(note_matches_equivalent(n, ticket_id, parsed.action, parsed.case_numbers) for n in existing_notes)
        if equivalent_note_exists:
            matched_note = next((n for n in existing_notes if note_matches_equivalent(n, ticket_id, parsed.action, parsed.case_numbers)), None)
            note_id = extract_note_id(matched_note)
            if note_id:
                audit.set_me_note(ticket_id=ticket_id, note_id=note_id, state="ME_NOTE_VERIFIED", correlation_id=str(uuid.uuid4()))
                print(f"EXISTING_ME_NOTE_VERIFIED | note_id={note_id}")
        already_verified = any((a.get("me_note_state") or "").upper() == "ME_NOTE_VERIFIED" for a in audits) or equivalent_note_exists
        if already_verified:
            print(f"ME_NOTE_ALREADY_ADDED for ticket {ticket_id}")
            continue

        correlation = str(uuid.uuid4())
        if not callable(getattr(me_writer, "build_summary", None)) or not callable(getattr(me_writer, "post_note", None)):
            print(f"ME_SUMMARY_SKIPPED ticket={ticket_id}: note writer lacks summary support")
            continue
        payload = me_writer.build_summary(ticket_id=ticket_id, environment="UAT", action=parsed.action, case_lines=case_lines, success_count=success_count, manual_count=manual_count, failed_count=failed_count, correlation_id=correlation)
        audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_SUMMARY", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_STARTED", outcome="ME_NOTE_STARTED", correlation_id=correlation)
        try:
            resp = me_writer.post_note(ticket_id, payload, timeout=30.0)
            data = resp.json()
            note_id = str(data.get("note", {}).get("id") or data.get("id") or "")
            audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_SUMMARY", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_ADDED", outcome="ME_NOTE_ADDED", correlation_id=correlation)
            audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_SUMMARY", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id=correlation)
            print(f"ME_NOTE_ADDED ticket={ticket_id} note_id={note_id}")
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else "UNKNOWN"
            print(f"ME_NOTE_FAILED for ticket {ticket_id}: HTTP {status_code}")
            try:
                response_json = exc.response.json() if exc.response is not None else None
                if response_json is not None:
                    details = json.dumps(response_json, ensure_ascii=False)[:500]
                elif exc.response is not None:
                    details = exc.response.text[:500]
                else:
                    details = str(exc)
                print(f"ME_NOTE_RESPONSE_DETAILS for ticket {ticket_id}: {details}")
            except (TypeError, ValueError):
                if exc.response is not None:
                    print(f"ME_NOTE_RESPONSE_DETAILS for ticket {ticket_id}: {exc.response.text[:500]}")
            audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_SUMMARY", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_FAILED", outcome=f"HTTP_{status_code}", correlation_id=correlation)
            print(f"ME_NOTE_RESULT_UNKNOWN for ticket {ticket_id}")
            continue
        except httpx.RequestError as exc:
            print(f"ME_NOTE_TIMEOUT_OR_NETWORK for ticket {ticket_id}: {exc}")
            try:
                notes = me_writer.list_notes(ticket_id, timeout=30.0)
                found = False
                for n in notes:
                    content = str((n.get("description") or n.get("note") or "") if isinstance(n, dict) else n or "")
                    if correlation in content:
                        found = True
                        break
                if found:
                    audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_SUMMARY", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="SUCCESS_AFTER_TIMEOUT_RECONCILIATION", correlation_id=correlation)
                    print(f"ME_NOTE_VERIFIED via reconciliation for ticket {ticket_id}")
                else:
                    audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_SUMMARY", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_FAILED", outcome="ME_NOTE_RESULT_UNKNOWN", correlation_id=correlation)
                    print(f"ME_NOTE_RESULT_UNKNOWN for ticket {ticket_id}")
            except Exception:
                audit.insert_audit(idempotency_key=str(uuid.uuid4()), ticket_id=ticket_id, case_number="", requested_action="ME_SUMMARY", rule_version=s.rule_version, before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_FAILED", outcome="ME_NOTE_RESULT_UNKNOWN", correlation_id=correlation)
                print(f"ME_NOTE_RESULT_UNKNOWN for ticket {ticket_id}")
    print()
    print("FILTER SUMMARY")
    print(f"Outside configured Group   : {outside_group}")
    print(f"Outside configured Template: {outside_template}")
    print(f"Outside configured Status  : {outside_status}")
    print(f"Tickets processed by bot   : {processed}")
    print(f"Skipped by date            : {skipped_by_date}")
    print(f"Skipped by tag             : {skipped_by_tag}")
    print(f"Skipped by safety filter   : {skipped_by_safety_filter}")
    if s.dry_run:
        print("\nDRY RUN COMPLETE: No Salesforce or ManageEngine records were changed.")
    else:
        print("\nLIVE RUN COMPLETE")

def main()->None:
    p=argparse.ArgumentParser();p.add_argument("--config",default="config/uat.yaml");p.add_argument("--watch",action="store_true")
    args=p.parse_args()
    while True:
        try:run_cycle(args.config)
        except (httpx.HTTPError,ValueError,RuntimeError,KeyError) as e:print(f"Cycle failed safely: {type(e).__name__}: {e}")
        if not args.watch:break
        seconds=load_settings(args.config).poll_seconds;print(f"Sleeping {seconds} seconds...");time.sleep(seconds)

if __name__=="__main__":main()
