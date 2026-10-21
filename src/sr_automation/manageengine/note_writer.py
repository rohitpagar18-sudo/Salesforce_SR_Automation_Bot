import json
from typing import List

import httpx


class ManageEngineNoteWriter:
    def __init__(self, s):
        self.s = s
        self.headers = {
            s.me_header: s.me_token,
            "Accept": "application/vnd.manageengine.sdp.v3+json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def build_note_url(self, ticket_id: str) -> str:
        return (
            f"{self.s.me_url.rstrip('/')}/api/v3/"
            f"requests/{ticket_id}/notes"
        )

    def _sanitize_url(self, url: str) -> str:
        sanitized = url
        if "?" in sanitized:
            sanitized = sanitized.split("?", 1)[0]
        return sanitized

    def build_summary(self, ticket_id: str, environment: str, action: str, case_lines: List[str], success_count:int, manual_count:int, failed_count:int, correlation_id:str) -> dict:
        lines = [f"Automation result for ticket #{ticket_id}", f"Environment: {environment}", f"Action: {action}", "Salesforce results:"]
        lines.extend(case_lines)
        lines.append(f"Successful: {success_count}")
        lines.append(f"Manual review: {manual_count}")
        lines.append(f"Failed: {failed_count}")
        description = "\n".join(lines)
        return {
            "note": {
                "description": description,
                "show_to_requester": False,
                "mark_first_response": False,
                "add_to_linked_requests": False,
            },
            "correlation_id": correlation_id,
        }

    def post_note(self, ticket_id: str, payload: dict, timeout: float = 30.0) -> httpx.Response:
        url = self.build_note_url(ticket_id)
        note_body = payload.get("note") or {}
        description = note_body.get("description") or note_body.get("note") or ""
        input_data = {
            "note": {
                "description": description,
                "show_to_requester": note_body.get("show_to_requester", False),
                "mark_first_response": note_body.get("mark_first_response", False),
                "add_to_linked_requests": note_body.get("add_to_linked_requests", False),
            }
        }
        response = httpx.post(
            url,
            headers=self.headers,
            data={"input_data": json.dumps(input_data)},
            timeout=timeout,
        )
        if response.is_error:
            body = response.text[:500]
            try:
                body = json.dumps(response.json(), ensure_ascii=False)[:500]
            except (TypeError, ValueError):
                body = response.text[:500]
            print(f"HTTP status: {response.status_code}")
            print(f"URL: {self._sanitize_url(url)}")
            print(f"Response: {body}")
        response.raise_for_status()
        return response

    def list_notes(self, ticket_id: str, timeout: float = 30.0) -> List[dict]:
        url = self.build_note_url(ticket_id)
        r = httpx.get(url, headers=self.headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        # Some ManageEngine API returns {"notes": [...]} or {"note": [...]} 
        return data.get("notes") or data.get("note") or []

    def get_note(self, ticket_id: str, note_id: str, timeout: float = 30.0) -> dict | None:
        url = f"{self.s.me_url.rstrip('/')}/api/v3/requests/{ticket_id}/notes/{note_id}"
        try:
            r = httpx.get(url, headers=self.headers, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            # Some APIs wrap note in {"note": {...}}
            if isinstance(data, dict):
                return data.get("note") or data
            return data
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else "UNKNOWN"
            print(f"NOTE_FETCH_FAILED HTTPError status={status}")
            return None
        except Exception as exc:
            print(f"NOTE_FETCH_FAILED {type(exc).__name__} status=<unknown>")
            return None
