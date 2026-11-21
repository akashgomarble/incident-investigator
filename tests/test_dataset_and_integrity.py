"""Dataset shape, determinism, and the 'no hardcoded answers / no ground-truth leakage' guarantees."""
import hashlib
import json
import re
import shutil
from pathlib import Path

from conftest import DATA, ROOT, run
from rca.config import Settings
from rca.llm.fake import FakeLLM
from rca.models import RootCauseCategory
from rca.runner import investigate
from rca.tools.datastore import AGENT_VISIBLE_FILES, DataStore, list_incidents

INCIDENTS = list_incidents(DATA)


def truth(i):
    return json.loads((DATA / "incidents" / i / "ground_truth.json").read_text())


def test_at_least_ten_incidents_with_required_variety():
    assert len(INCIDENTS) >= 10
    tags = {t for i in INCIDENTS for t in truth(i)["tags"]}
    assert {"misleading-report", "conflicting-observations", "tool-failures", "inconclusive", "decoy-deploy", "decoy-config"} <= tags
    assert sum(1 for i in INCIDENTS if DataStore(DATA, i).faults) >= 3                 # incidents with scripted tool failures
    assert sum(1 for i in INCIDENTS if truth(i)["decoys"]) >= 6                        # incidents with a planted decoy
    assert len({truth(i)["category"] for i in INCIDENTS}) >= 7                         # no single dominant failure class


def test_every_incident_is_well_formed():
    cats = {c.value for c in RootCauseCategory}
    for i in INCIDENTS:
        ds, t = DataStore(DATA, i), truth(i)
        assert ds.incident.id == i and ds.logs and ds.metrics["series"]
        assert set(t["acceptable_categories"]) <= cats and t["expected_outcome"] in ("root_cause_identified", "inconclusive")
        change_ids = {d["id"] for d in ds.deployments} | {c["id"] for c in ds.config_changes}
        if t["trigger_change_id"]:
            assert t["trigger_change_id"] in change_ids
        for d in t["decoys"]:
            assert d in change_ids or d.startswith("HIST-")
        known = set(ds.services())
        assert set(t["root_cause_services"]) - {"exchange-partner"} <= known and ds.incident.affected_service in known
        blob = (DATA / "incidents" / i / "logs.jsonl").read_text() + json.dumps(ds.deployments) + json.dumps(ds.config_changes) \
            + json.dumps(list(ds.metrics["series"][next(iter(ds.metrics["series"]))])) + json.dumps([list(v) for v in ds.metrics["series"].values()])
        for marker in t["key_evidence"]:                                               # the evidence needed to solve it really is in the data
            assert marker in blob, f"{i}: key evidence {marker!r} not reachable through any tool"


def test_generator_is_deterministic(tmp_path, monkeypatch):
    import dataset.generate as gen
    monkeypatch.setattr(gen, "OUT", tmp_path)
    gen.main()
    for i in INCIDENTS:
        for f in ("logs.jsonl", "metrics.json", "ground_truth.json"):
            a, b = (DATA / "incidents" / i / f).read_bytes(), (tmp_path / "incidents" / i / f).read_bytes()
            assert hashlib.sha256(a).digest() == hashlib.sha256(b).digest(), f"{i}/{f} differs from the committed dataset"


def test_ground_truth_is_never_visible_to_agents(tmp_path):
    assert "ground_truth.json" not in AGENT_VISIBLE_FILES
    # behavioural proof: the system runs identically with every ground-truth file deleted
    shutil.copytree(DATA, tmp_path / "data")
    for p in (tmp_path / "data").rglob("ground_truth.json"):
        p.unlink()
    a = run(investigate("INC-002", FakeLLM(), settings=Settings(data_dir=tmp_path / "data"), sim_latency_s=0))
    b = run(investigate("INC-002", FakeLLM(), settings=Settings(), sim_latency_s=0))
    assert a.report.ranked_hypotheses[0].title == b.report.ranked_hypotheses[0].title
    assert [c.output for c in a.state.executor.calls] == [c.output for c in b.state.executor.calls]
    # static proof: only the scorer mentions the file in code
    offenders = [p for p in (ROOT / "src").rglob("*.py")
                 if any("ground_truth" in line and not line.strip().startswith(("#", '"""', "NOTE")) and "never opens" not in line
                        for line in p.read_text().splitlines())]
    assert offenders == [], offenders


def test_no_dataset_specific_knowledge_in_agent_code():
    """Prompts and agent logic must not mention any service, change id or incident from the dataset."""
    names = set(DataStore(DATA, INCIDENTS[0]).services())
    ids = set()
    for i in INCIDENTS:
        ds = DataStore(DATA, i)
        ids |= {i} | {d["id"] for d in ds.deployments} | {c["id"] for c in ds.config_changes}
    ids |= {h["id"] for h in DataStore(DATA, INCIDENTS[0]).history}
    forbidden = names | ids
    agent_files = list((ROOT / "src" / "rca" / "agents").glob("*.py")) + [ROOT / "src" / "rca" / f for f in ("baseline.py", "assemble.py", "state.py", "grounding.py", "runner.py")]
    for p in agent_files:
        text = p.read_text()
        hits = sorted(x for x in forbidden if re.search(rf"(?<![\w-]){re.escape(x)}(?![\w-])", text))
        assert hits == [], f"{p.name} mentions dataset entities: {hits}"
