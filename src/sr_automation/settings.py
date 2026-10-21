import os
from dataclasses import dataclass
from pathlib import Path
import yaml
from dotenv import load_dotenv

load_dotenv()


def normalize_optional_str(value):
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


@dataclass(frozen=True)
class Settings:
    me_url: str
    me_token: str
    me_header: str
    sf_url: str
    sf_client_id: str
    sf_client_secret: str
    sf_api_version: str
    dry_run: bool
    poll_seconds: int
    page_size: int
    allowed_groups: set[str]
    allowed_templates: set[str]
    ready_statuses: set[str]
    excluded_record_types: set[str]
    eligible_statuses: dict[str, set[str]]
    allow_partial: bool
    safety_filter_enabled: bool
    safety_filter_mode: str
    created_within_days_enabled: bool
    created_within_days: int
    required_tag_enabled: bool
    required_tag: str
    # Stage 2 configuration
    rule_version: str
    target_status: dict[str, str]
    comment_field: str | None
    comment_template: str | None
    activity_requirements: dict
    enable_salesforce_writes: bool
    enable_manageengine_writes: bool
    manageengine_auto_close: bool
    stage2_max_updates: int | None
    stage2_absolute_safety_cap: int
    require_high_confidence_intent: bool
    # ManageEngine Stage 4 config
    me_technician_name: str | None
    me_technician_id: str | None
    me_completion_mode: str
    me_completion_target_status: str | None
    me_completion_target_status_id: str | None
    me_completion_resolution_template: str | None
    me_completion_status_comment_template: str | None
    me_completion_count_field: str | None
    me_completion_verify_final_status: bool
    closure_rules: dict
    me_closure_resolution_template: str | None
    me_closure_comment_template: str | None
    me_requester_ack_resolution: bool
    me_closure_code_name: str | None
    me_closure_code_id: str | None


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value: raise ValueError(f"Missing environment variable: {name}")
    return value

def load_closure_rules(path: str = "config/uat.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        return {}

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    completion = data.get("manageengine", {}).get("completion", {}) if isinstance(data, dict) else {}
    closure_rules = completion.get("closure_rules", {}) if isinstance(completion, dict) else {}
    return closure_rules if isinstance(closure_rules, dict) else {}


def load_settings(path: str = "config/uat.yaml") -> Settings:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    me, sf, actions, bulk = data["manageengine"], data["salesforce"], data["actions"], data["bulk"]
    closure_rules = load_closure_rules(path)
    reject_cfg = (actions.get("reject") or actions.get("REJECT") or {}) if isinstance(actions, dict) else {}
    activity_requirements = dict(data.get("activity_requirements", {}))
    reject_activity_required = bool(reject_cfg.get("require_closed_rejection_activity", reject_cfg.get("activity_must_be_closed", True)))
    activity_requirements.setdefault(
        "require_closed_rejection_activity",
        reject_activity_required,
    )
    activity_requirements.setdefault(
        "require_high_confidence_intent",
        bool(reject_cfg.get("require_high_confidence_intent", True)),
    )
    activity_requirements.setdefault(
        "require_no_open_activities",
        bool((actions.get("cancel") or actions.get("CANCEL") or {}).get("require_no_open_activities", False)),
    )
    stage2_cfg = (data.get("stage2") or {}) if isinstance(data, dict) else {}
    raw_max_updates = stage2_cfg.get("max_salesforce_updates_per_run")
    if raw_max_updates in (None, "", "null", "NULL"):
        stage2_max_updates = None
    else:
        try:
            stage2_max_updates = int(raw_max_updates)
        except (TypeError, ValueError):
            stage2_max_updates = None
    return Settings(
        me_url=required("MANAGEENGINE_BASE_URL").rstrip("/"),
        me_token=required("MANAGEENGINE_API_TOKEN"),
        me_header=os.getenv("MANAGEENGINE_AUTH_HEADER", "authtoken").strip(),
        sf_url=required("SALESFORCE_LOGIN_URL").rstrip("/"),
        sf_client_id=required("SALESFORCE_CLIENT_ID"),
        sf_client_secret=required("SALESFORCE_CLIENT_SECRET"),
        sf_api_version=os.getenv("SALESFORCE_API_VERSION", "v65.0"),
        dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
        poll_seconds=int(me.get("poll_interval_seconds", 60)),
        page_size=int(me.get("page_size", 100)),
        allowed_groups=set(me["allowed_groups"]),
        allowed_templates=set(me["allowed_templates"]),
        ready_statuses=set(me["ready_statuses"]),
        excluded_record_types={x.casefold() for x in sf["excluded_record_types"]},
        eligible_statuses={k.upper(): set(v.get("eligible_statuses", [])) for k, v in actions.items()},
        allow_partial=bool(bulk.get("allow_partial_processing", True)),
        # stage2 and action configuration
        rule_version=str(data.get("rule_version", "0.0.0")),
        target_status={k.upper(): v.get("target_status") for k, v in actions.items()},
        comment_field=str(actions.get("REJECT", {}).get("comment_field")) if actions.get("REJECT") else None,
        comment_template=str(actions.get("REJECT", {}).get("comment_template")) if actions.get("REJECT") else None,
        activity_requirements=activity_requirements,
        enable_salesforce_writes=os.getenv("ENABLE_SALESFORCE_WRITES", "false").lower() == "true",
        enable_manageengine_writes=os.getenv("ENABLE_MANAGEENGINE_WRITES", "false").lower() == "true",
        manageengine_auto_close=os.getenv("MANAGEENGINE_AUTO_CLOSE", "false").lower() == "true",
        stage2_max_updates=stage2_max_updates,
        stage2_absolute_safety_cap=max(1, int(stage2_cfg.get("absolute_safety_cap", 100))),
        require_high_confidence_intent=bool(reject_cfg.get("require_high_confidence_intent", True)),
        # ManageEngine completion settings
        me_technician_name=normalize_optional_str(me.get("technician", {}).get("name")) if me.get("technician") else None,
        me_technician_id=normalize_optional_str(me.get("technician", {}).get("id")) if me.get("technician") else None,
        me_completion_mode=str((me.get("completion", {}) or {}).get("mode", "STATUS_UPDATE")).strip().upper() if me.get("completion") else "STATUS_UPDATE",
        me_completion_target_status=normalize_optional_str((me.get("completion", {}) or {}).get("target_status")) if me.get("completion") else None,
        me_completion_target_status_id=normalize_optional_str((me.get("completion", {}) or {}).get("target_status_id")) if me.get("completion") else None,
        me_completion_resolution_template=normalize_optional_str((me.get("completion", {}) or {}).get("resolution_template")) if me.get("completion") else None,
        me_completion_status_comment_template=normalize_optional_str((me.get("completion", {}) or {}).get("status_comment_template")) if me.get("completion") else None,
        me_completion_count_field=normalize_optional_str((me.get("completion", {}) or {}).get("count_field")) if me.get("completion") else None,
        me_completion_verify_final_status=bool((me.get("completion", {}) or {}).get("verify_final_status", True)) if me.get("completion") else True,
        me_closure_resolution_template=normalize_optional_str(me.get("closure", {}).get("resolution_template")) if me.get("closure") else None,
        me_closure_comment_template=normalize_optional_str(me.get("closure", {}).get("closure_comment_template")) if me.get("closure") else None,
        me_requester_ack_resolution=bool(me.get("closure", {}).get("requester_ack_resolution", False)) if me.get("closure") else False,
        me_closure_code_name=normalize_optional_str((me.get("closure", {}).get("closure_code", {}) or {}).get("name")) if me.get("closure") and isinstance(me.get("closure", {}).get("closure_code"), dict) else None,
        me_closure_code_id=normalize_optional_str((me.get("closure", {}).get("closure_code", {}) or {}).get("id")) if me.get("closure") and isinstance(me.get("closure", {}).get("closure_code"), dict) else None,
        safety_filter_enabled=bool(me.get("safety_filter", {}).get("enabled", False)),
        safety_filter_mode=str(me.get("safety_filter", {}).get("mode", "ALL")),
        created_within_days_enabled=bool(me.get("safety_filter", {}).get("created_within_days", {}).get("enabled", False)),
        created_within_days=int(me.get("safety_filter", {}).get("created_within_days", {}).get("days", 0)),
        required_tag_enabled=bool(me.get("safety_filter", {}).get("required_tag", {}).get("enabled", False)),
        required_tag=str(me.get("safety_filter", {}).get("required_tag", {}).get("value", "")),
        closure_rules=closure_rules,
    )
