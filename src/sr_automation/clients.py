import json
from urllib.parse import quote
import httpx
from .settings import Settings

class ManageEngineClient:
    def __init__(self, s: Settings):
        self.s=s
        self.headers={s.me_header:s.me_token,"Accept":"application/vnd.manageengine.sdp.v3+json"}
    def list_recent(self) -> list[dict]:
        input_data={"list_info":{"row_count":self.s.page_size,"start_index":1,"sort_field":"created_time","sort_order":"desc"}}
        r=httpx.get(f"{self.s.me_url}/api/v3/requests",headers=self.headers,params={"input_data":json.dumps(input_data)},timeout=45.0)
        r.raise_for_status(); return r.json().get("requests",[])

    def list_open_tickets(self) -> list[dict]:
        all_tickets: list[dict] = []
        start_index = 1
        row_count = 100

        max_pages = 20

        for page_number in range(1, max_pages + 1):
            input_data = {
                "list_info": {
                    "row_count": row_count,
                    "start_index": start_index,
                    "sort_field": "created_time",
                    "sort_order": "desc",
                    "get_total_count": True,
                    "fields_required": [
                        "id",
                        "subject",
                        "status",
                        "group",
                        "template",
                    ],
                    "search_criteria": [
                        {
                            "field": "group.name",
                            "condition": "is",
                            "values": sorted(self.s.allowed_groups),
                        },
                        {
                            "field": "status.internal_name",
                            "condition": "is",
                            "logical_operator": "and",
                            "values": sorted(self.s.ready_statuses),
                        },
                    ],
                }
            }

            response = httpx.get(
                f"{self.s.me_url}/api/v3/requests",
                headers=self.headers,
                params={"input_data": json.dumps(input_data)},
                timeout=45.0,
            )

            response.raise_for_status()
            payload = response.json()
            tickets = payload.get("requests", [])
            list_info = payload.get("list_info", {})

            print(
                f"ManageEngine page {page_number}: "
                f"{len(tickets)} matching ticket(s)"
            )

            all_tickets.extend(tickets)

            has_more_rows = bool(list_info.get("has_more_rows", False))
            if not tickets or not has_more_rows:
                break

            start_index += row_count

        return all_tickets
    def get_ticket(self, ticket_id: str) -> dict:
        r=httpx.get(f"{self.s.me_url}/api/v3/requests/{ticket_id}",headers=self.headers,timeout=45.0)
        r.raise_for_status(); return r.json()["request"]
    def get_mandatory_fields(self, ticket_id: str, target_status_id: str) -> dict:
        url = f"{self.s.me_url}/api/v3/requests/{ticket_id}/_get_mandatory_fields"
        payload = {"input_data": json.dumps({"state_id": str(target_status_id)})}
        r = httpx.get(url, headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"}, params=payload, timeout=30.0)
        r.raise_for_status()
        response = r.json()
        if isinstance(response, dict):
            mandatory = response.get("mandatory_fields")
            if isinstance(mandatory, dict):
                return mandatory
        return {}
    def assign_technician(self, ticket_id: str, technician: dict) -> httpx.Response:
        url = f"{self.s.me_url}/api/v3/requests/{ticket_id}/assign"
        data = {"input_data": json.dumps({"request": {"technician": technician}})}
        return httpx.put(url, headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"}, data=data, timeout=30.0)
    def add_resolution(self, ticket_id: str, resolution: dict) -> httpx.Response:
        url = f"{self.s.me_url}/api/v3/requests/{ticket_id}/resolutions"
        data = {"input_data": json.dumps({"resolution": resolution})}
        return httpx.post(url, headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"}, data=data, timeout=30.0)
    def close_ticket(self, ticket_id: str, closure_info: dict) -> httpx.Response:
        url = f"{self.s.me_url}/api/v3/requests/{ticket_id}/close"
        data = {"input_data": json.dumps(closure_info)}
        return httpx.put(url, headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"}, data={"input_data": json.dumps(closure_info)}, timeout=30.0)

    def update_request(self, ticket_id: str, input_data: dict) -> httpx.Response:
        url = f"{self.s.me_url}/api/v3/requests/{ticket_id}"
        data = {"input_data": json.dumps(input_data)}
        return httpx.put(url, headers={**self.headers, "Content-Type": "application/x-www-form-urlencoded"}, data=data, timeout=30.0)

class SalesforceClient:
    def __init__(self,s:Settings):
        self.s=s; self.token=""; self.instance=""
    def authenticate(self)->None:
        r=httpx.post(f"{self.s.sf_url}/services/oauth2/token",data={"grant_type":"client_credentials","client_id":self.s.sf_client_id,"client_secret":self.s.sf_client_secret},timeout=45.0)
        r.raise_for_status(); d=r.json(); self.token=d["access_token"]; self.instance=d["instance_url"].rstrip("/")
    @property
    def headers(self)->dict[str,str]: return {"Authorization":f"Bearer {self.token}","Accept":"application/json"}
    def query(self,soql:str)->list[dict]:
        r=httpx.get(f"{self.instance}/services/data/{self.s.sf_api_version}/query",headers=self.headers,params={"q":soql},timeout=60.0)
        r.raise_for_status(); return r.json().get("records",[])
    def cases(self,numbers:list[str])->dict[str,dict]:
        if not numbers:return {}
        quoted=",".join("'"+n.replace("'","\\'")+"'" for n in numbers)
        soql=("SELECT Id,CaseNumber,Status,RecordType.Name,SR_Type__c,LastModifiedDate "
              f"FROM Case WHERE CaseNumber IN ({quoted})")
        return {r["CaseNumber"]:r for r in self.query(soql)}
    def refresh_case(self,case_id:str)->dict|None:
        soql=("SELECT Id,CaseNumber,Status,RecordType.Name,SR_Type__c,LastModifiedDate "
              f"FROM Case WHERE Id = '{case_id}'")
        recs=self.query(soql)
        return recs[0] if recs else None
    def refresh_tasks_for_case(self,case_id:str)->list[dict]:
        soql=("SELECT Id,WhatId,Subject,Status,IsClosed,Owner.Name,LastModifiedDate "
              f"FROM Task WHERE WhatId = '{case_id}' ORDER BY LastModifiedDate DESC")
        return self.query(soql)
    def update_case(self, case_id: str, fields: dict, timeout: float = 30.0) -> httpx.Response:
        """Patch a Case record. Returns the httpx.Response."""
        url = f"{self.instance}/services/data/{self.s.sf_api_version}/sobjects/Case/{case_id}"
        headers = {**self.headers, "Content-Type": "application/json"}
        return httpx.patch(url, headers=headers, json=fields, timeout=timeout)
    def tasks(self,case_ids:list[str])->dict[str,list[dict]]:
        if not case_ids:return {}
        quoted=",".join("'"+x+"'" for x in case_ids)
        soql=("SELECT Id,WhatId,Subject,Status,IsClosed,Owner.Name,LastModifiedDate "
              f"FROM Task WHERE WhatId IN ({quoted}) ORDER BY LastModifiedDate DESC")
        out:dict[str,list[dict]]={}
        for r in self.query(soql):out.setdefault(r["WhatId"],[]).append(r)
        return out
