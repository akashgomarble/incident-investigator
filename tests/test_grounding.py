"""Evidence grounding is mechanical: quotes must literally appear in the cited tool output."""
from conftest import make_executor, run, window
from rca.grounding import check_ref, normalize, ref_stats, verify_finding
from rca.models import EvidenceRef, FindingIn


def _ledger():
    ex = make_executor("INC-002")
    r = run(ex.call("dependency", "get_config_changes", window(ex.store, 360)))
    return ex, r.call_id


def test_verbatim_quote_verifies_despite_formatting():
    ex, cid = _ledger()
    assert check_ref(EvidenceRef(call_id=cid, quote="cfg-2207"), ex.record)[0]
    assert check_ref(EvidenceRef(call_id=cid, quote='key: db.pool.max_size, OLD_VALUE = 50'), ex.record)[0]   # punctuation/case/space tolerant
    assert normalize('"old_value": 50,') == "oldvalue50"


def test_fabricated_quote_wrong_call_and_trivial_quote_fail():
    ex, cid = _ledger()
    ok, why = check_ref(EvidenceRef(call_id=cid, quote="db.pool.max_size changed to 500"), ex.record)
    assert not ok and "not found" in why
    assert not check_ref(EvidenceRef(call_id="tc-999", quote="cfg-2207"), ex.record)[0]
    assert not check_ref(EvidenceRef(call_id=cid, quote="5"), ex.record)[0]          # too short to mean anything


def test_finding_verification_and_ref_stats():
    ex, cid = _ledger()
    good = verify_finding("F1", "dependency", "T1", FindingIn(statement="pool shrunk", kind="observation",
                          refs=[EvidenceRef(call_id=cid, quote="cfg-2207"), EvidenceRef(call_id=cid, quote="made up text")]), ex.record)
    bad = verify_finding("F2", "dependency", "T1", FindingIn(statement="hallucinated", kind="observation",
                         refs=[EvidenceRef(call_id=cid, quote="rolled back at noon")]), ex.record)
    none = verify_finding("F3", "dependency", "T1", FindingIn(statement="no refs", kind="inference"), ex.record)
    assert good.verified and "1/2 refs verified" in good.verification_note
    assert not bad.verified and not none.verified and none.verification_note == "no evidence refs"
    assert ref_stats([good, bad, none], ex.record) == (1, 3)


def test_failed_tool_call_can_be_cited_as_an_observation_about_the_outage():
    ex = make_executor("INC-007")
    r = run(ex.call("observability", "query_logs", {"service": "auth-service", **window(ex.store)}))
    assert not r.ok
    assert check_ref(EvidenceRef(call_id=r.call_id, quote="backend unavailable (503)"), ex.record)[0]
