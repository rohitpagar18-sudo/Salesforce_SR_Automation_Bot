from sr_automation.parser import parse_ticket

def test_bulk_cancel():
    r=parse_ticket("1","Customer Refund - Cancellation","Please cancel Case Number 23422949 23422948 23422947")
    assert r.action=="CANCEL" and r.confidence=="HIGH"
    assert r.case_numbers==["23422949","23422948","23422947"]

def test_conflict():
    r=parse_ticket("2","Cancel 23422949","Please reject 23422949")
    assert r.action=="CONFLICT"

def test_subject_only_is_low():
    r=parse_ticket("3","Close 23422949","Please review this case")
    assert r.confidence=="LOW"

