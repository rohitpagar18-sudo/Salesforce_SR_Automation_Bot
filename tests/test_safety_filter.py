import datetime
from sr_automation.main import extract_created_ms, tags_from_field


# Helpers to build a fake ticket
def make_ticket(created_ms=None, tags=None):
    t = {}
    if created_ms is not None:
        t["created_time"] = created_ms
    if tags is not None:
        t["tags"] = tags
    return t


def test_case_recent_with_correct_tag_all_mode():
    now_ms = int(datetime.datetime.utcnow().timestamp() * 1000)
    ticket = make_ticket(created_ms=now_ms, tags=["SR-AUTOMATION-UAT"])
    assert extract_created_ms(ticket["created_time"]) == now_ms
    assert tags_from_field(ticket["tags"]) == ["SR-AUTOMATION-UAT"]


def test_case_old_with_correct_tag_all_mode():
    now_ms = int(datetime.datetime.utcnow().timestamp() * 1000)
    old_ms = now_ms - (11 * 24 * 60 * 60 * 1000)
    ticket = make_ticket(created_ms=old_ms, tags=["SR-AUTOMATION-UAT"])
    assert extract_created_ms(ticket["created_time"]) == old_ms
    assert tags_from_field(ticket["tags"]) == ["SR-AUTOMATION-UAT"]


def test_tags_case_insensitive():
    t = {"tags": [{"value": "sr-automation-uat"}, "Other"]}
    tags = tags_from_field(t["tags"])
    assert "sr-automation-uat" in [x.casefold() for x in tags]
