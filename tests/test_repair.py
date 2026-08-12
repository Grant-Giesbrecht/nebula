"""Ref repair at merge time. See docs/identity-trust-roadmap.md."""
import pytest
import nebula
from nebula import transfer
from nebula.sidecar import read_sidecar, read_session_yaml


def _pair(tmp_path):
    src = tmp_path / "intake"
    transfer.init_archive(src, kind="intake", name="intake", user="grant@ncsu.edu")
    dst = tmp_path / "main"
    transfer.init_archive(dst, kind="standard", name="main", user="grant@ncsu.edu")
    return src, dst


def _sess(archive, files, description="s"):
    s = nebula.new(archive, description=description)
    for name, derived in files:
        with s.artifact(name, derived_from=derived or None) as fn:
            fn.write_text(name)
    s.close()
    return s


def test_a_typoed_filename_is_offered_a_candidate(tmp_path):
    src, dst = _pair(tmp_path)
    a = _sess(src, [("raw.csv", None)])
    _sess(src, [("fit.png", [f"{a.id}/raw.csvv"])])
    plan = transfer.plan_merge(src, dst)
    assert len(plan.repairs) == 1
    r = plan.repairs[0]
    assert r["problem"] == "no such file in that session"
    assert r["candidates"] == [f"{a.id}/raw.csv"]
    assert r["chosen"] is None          # never guesses without being asked


def test_a_typoed_session_id_is_offered_a_candidate(tmp_path):
    src, dst = _pair(tmp_path)
    a = _sess(src, [("raw.csv", None)])
    # A digit that names no session at all. Note a typo that lands on a
    # *real* session is undetectable -- see _plan_repairs.
    bad = a.id[:-1] + "9"
    _sess(src, [("fit.png", [f"{bad}/raw.csv"])])
    plan = transfer.plan_merge(src, dst)
    assert [r["problem"] for r in plan.repairs] == ["no such session"]
    assert plan.repairs[0]["candidates"] == [f"{a.id}/raw.csv"]


def test_a_resolvable_ref_is_never_offered(tmp_path):
    src, dst = _pair(tmp_path)
    a = _sess(src, [("raw.csv", None)])
    _sess(src, [("fit.png", [f"{a.id}/raw.csv"])])
    assert transfer.plan_merge(src, dst).repairs == []


def test_nothing_is_repaired_unless_chosen(tmp_path):
    src, dst = _pair(tmp_path)
    a = _sess(src, [("raw.csv", None)])
    b = _sess(src, [("fit.png", [f"{a.id}/raw.csvv"])])
    plan = transfer.plan_merge(src, dst)
    transfer.merge(src, dst, plan=plan)
    new_b = {s.run_id: s.new_run_id for s in plan.sessions}[b.id]
    from nebula.index import _iter_session_dirs
    d = next(p for p in _iter_session_dirs(dst) if p.name == new_b)
    sc = read_sidecar(d / "fit.png")
    assert sc.derived_from[0]["file"] == "raw.csvv"     # left exactly as written


def test_an_accepted_repair_is_applied_and_then_renamed(tmp_path):
    """The ordering that makes this correct: the candidate is named in
    source terms and must still go through the id reallocation."""
    src, dst = _pair(tmp_path)
    a = _sess(src, [("raw.csv", None)])
    b = _sess(src, [("fit.png", [f"{a.id}/raw.csvv"])])
    plan = transfer.plan_merge(src, dst)
    assert transfer.accept_unambiguous_repairs(plan) == 1
    transfer.merge(src, dst, plan=plan)

    renames = {s.run_id: s.new_run_id for s in plan.sessions}
    from nebula.index import _iter_session_dirs
    d = next(p for p in _iter_session_dirs(dst) if p.name == renames[b.id])
    sc = read_sidecar(d / "fit.png")
    assert sc.derived_from[0]["file"] == "raw.csv"          # repaired
    assert sc.derived_from[0]["session"] == renames[a.id]   # and renamed
    assert renames[a.id] != a.id


def test_ambiguity_is_left_for_a_human(tmp_path):
    src, dst = _pair(tmp_path)
    a = _sess(src, [("raw1.csv", None), ("raw2.csv", None)])
    _sess(src, [("fit.png", [f"{a.id}/raw.csv"])])
    plan = transfer.plan_merge(src, dst)
    assert len(plan.repairs[0]["candidates"]) == 2
    assert transfer.accept_unambiguous_repairs(plan) == 0


def test_related_runs_are_repaired_too(tmp_path):
    src, dst = _pair(tmp_path)
    a = _sess(src, [("raw.csv", None)])
    bad = a.id[:-1] + "9"
    b = nebula.new(src, description="b")
    b.add_related_run(bad)
    b.close()
    plan = transfer.plan_merge(src, dst)
    entry = next(r for r in plan.repairs if r["field"] == "related_runs")
    assert entry["file"] is None
    assert a.id in entry["candidates"]
    # Chosen explicitly: with two sessions in the archive both ids are
    # equally close, so this isolates the apply path from difflib's scoring.
    entry["chosen"] = a.id
    transfer.merge(src, dst, plan=plan)
    renames = {s.run_id: s.new_run_id for s in plan.sessions}
    from nebula.index import _iter_session_dirs
    d = next(p for p in _iter_session_dirs(dst) if p.name == renames[b.id])
    assert read_session_yaml(d).related_runs[0]["session"] == renames[a.id]


def test_adopt_never_offers_repairs(tmp_path):
    """A fragment's refs are its author's statements. Correcting them would
    falsify what they wrote."""
    src = tmp_path / "theirs"
    transfer.init_archive(src, kind="standard", name="theirs", user="j@mit.edu")
    a = _sess(src, [("raw.csv", None)])
    b = _sess(src, [("fit.png", [f"{a.id}/raw.csvv"])])
    frag = tmp_path / "frag"
    transfer.export(src, frag, sessions=[a.id, b.id])
    dst = tmp_path / "mine"
    transfer.init_archive(dst, kind="standard", name="mine", user="grant@ncsu.edu")
    assert transfer.plan_adopt(frag, dst).repairs == []


def test_a_bare_intake_id_reads_as_a_session_not_a_filename():
    """Regression: _SESSION_RE only accepted the S- prefix, so a bare
    "I-26-0001" in an intake archive's related_runs was silently read as a
    file in the current session."""
    from nebula.refs import parse_ref
    got = parse_ref("I-26-0001")
    assert (got.session, got.file) == ("I-26-0001", None)
