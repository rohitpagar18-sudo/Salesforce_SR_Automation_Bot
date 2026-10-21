from .models import CaseDecision, ParseResult
from .settings import Settings

def _norm_status(value):
    return str(value or "").strip().casefold()


def decide(ticket_id:str, parsed:ParseResult, cases:dict[str,dict], tasks:dict[str,list[dict]], s:Settings)->list[CaseDecision]:
    results=[]
    if parsed.action in {"UNKNOWN","CONFLICT"} or parsed.confidence == "LOW":
        return [CaseDecision(ticket_id,n,parsed.action,None,None,"MANUAL_REVIEW",parsed.reason_code) for n in parsed.case_numbers]
    reject_requires_task = bool(s.activity_requirements.get("require_closed_rejection_activity", True))
    require_high_confidence = bool(s.activity_requirements.get("require_high_confidence_intent", True))
    for number in parsed.case_numbers:
        case=cases.get(number)
        if not case:
            results.append(CaseDecision(ticket_id,number,parsed.action,None,None,"CASE_NOT_FOUND","Salesforce Case not found"));continue
        rt=((case.get("RecordType") or {}).get("Name"))
        status=case.get("Status"); case_tasks=tasks.get(case["Id"],[])
        open_count=sum(not bool(t.get("IsClosed")) for t in case_tasks)
        rejected_count=sum(bool(t.get("IsClosed")) and str(t.get("Status","")).casefold() in {"reject","rejected"} for t in case_tasks)
        target_status = str(s.target_status.get(parsed.action, "")).strip()
        current_status = str(status or "").strip()
        target_norm = _norm_status(target_status)
        current_norm = _norm_status(current_status)
        if (rt or "").casefold() in s.excluded_record_types:
            outcome,reason="MANUAL_REVIEW_RECORD_TYPE",f"Excluded Record Type: {rt}"
        elif target_norm and current_norm == target_norm:
            outcome,reason="ALREADY_IN_TARGET_STATE",f"Already {target_status}"
        elif status not in s.eligible_statuses.get(parsed.action,set()):
            outcome,reason="INVALID_CURRENT_STATUS",f"{status} is not configured for {parsed.action}"
        elif parsed.action=="REJECT" and require_high_confidence and str(parsed.confidence or "").upper() != "HIGH":
            outcome,reason="CONFIRMATION_REQUIRED","Reject requires high-confidence intent"
        elif parsed.action in {"CANCEL","CLOSE"} and open_count:
            outcome,reason="OPEN_ACTIVITIES",f"{open_count} open Task(s)"
        elif parsed.action=="REJECT" and reject_requires_task and not rejected_count:
            outcome,reason="REJECTION_EVIDENCE_MISSING","No closed Reject/Rejected Task"
        else:
            outcome,reason="WOULD_UPDATE",f"Dry-run eligible for {parsed.action}"
        results.append(CaseDecision(ticket_id,number,parsed.action,rt,status,outcome,reason,open_count,rejected_count))
    return results
