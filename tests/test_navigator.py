import datetime
import json

import nebula
from nebula.navigator import model
from nebula.sidecar import read_session_yaml, write_session_yaml, SessionMeta


def _session_with(archive, files):
    s = nebula.new(archive, tags=["demo"], description="a session")
    for name, content in files.items():
        with s.artifact(name) as fn:
            fn.write_text(content)
    s.close()
    return s


# ---------------------------------------------------------------------
# model (no GUI)
# ---------------------------------------------------------------------

def test_list_items_pairs(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x", "proc.graf": "y"})
    items = {i.name: i for i in model.list_items(s.path)}
    assert set(items) == {"raw.csv", "proc.graf"}
    assert all(i.status == model.PAIRED for i in items.values())
    assert items["raw.csv"].has_artifact and items["raw.csv"].has_sidecar
    assert items["raw.csv"].source == "script"


def test_list_items_orphan(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "dropped.dat").write_text("hand-dropped")  # no sidecar
    items = {i.name: i for i in model.list_items(s.path)}
    assert items["dropped.dat"].status == model.ORPHAN
    assert items["dropped.dat"].has_artifact and not items["dropped.dat"].has_sidecar


def test_list_items_stray(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "raw.csv").unlink()  # leaves a stray sidecar
    item = {i.name: i for i in model.list_items(s.path)}["raw.csv"]
    assert item.status == model.STRAY
    assert item.has_sidecar and not item.has_artifact


def test_list_items_drift_only_when_verifying(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "original"})
    (s.path / "raw.csv").write_text("tampered")
    # without checksum verification, still looks paired (presence only)
    assert model.list_items(s.path)[0].status == model.PAIRED
    # with verification, drift is detected
    assert model.list_items(s.path, verify_checksums=True)[0].status == model.DRIFTED


def test_list_sessions_counts_problems(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "orphan.dat").write_text("y")
    sessions = model.list_sessions(archive)
    assert len(sessions) == 1
    assert sessions[0].run_id == s.id
    assert sessions[0].n_items == 2
    assert sessions[0].n_problems == 1


def test_list_items_excludes_trash(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    nebula.delete_file(archive, s.id, "raw.csv")  # moves to .trash/
    # nothing left but the trash dir -> no items surfaced
    assert model.list_items(s.path) == []


def test_resolve_prefers_path_for_unregistered(tmp_path):
    root, label = model.resolve(str(tmp_path / "archive"))
    assert root == tmp_path / "archive"


def test_sidecar_display_pretty_json(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    text = model.sidecar_display(s.path / "raw.csv.meta.json")
    assert '"produced_by"' in text
    assert '"source": "script"' in text


def test_sidecar_display_raw_when_unparseable(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    sc = s.path / "raw.csv.meta.json"
    sc.write_text("{not valid json")
    assert model.sidecar_display(sc) == "{not valid json"


def test_importable_sessions_excludes_frozen(tmp_path):
    archive = tmp_path / "archive"
    today = _session_with(archive, {"a.csv": "x"})       # closed today -> appendable
    # hand-place a session closed on a previous day -> frozen
    ym = archive / "data" / "2020"
    ym.mkdir(parents=True)
    d = ym / "S-20-9999"
    d.mkdir()
    write_session_yaml(d, SessionMeta(
        run_id="S-20-9999",
        created=datetime.datetime(2020, 1, 1).astimezone().isoformat(),
        status="closed", description="last year"))

    ids = {s.run_id for s in model.importable_sessions(archive)}
    assert today.id in ids
    assert "S-20-9999" not in ids
    # the frozen session must actually be *seen* by the walk, or this
    # assertion would pass vacuously
    assert "S-20-9999" in {s.run_id for s in model.list_sessions(archive)}


# ---------------------------------------------------------------------
# structured panels (sidecar_info / session_info)
# ---------------------------------------------------------------------

def test_sidecar_info_structured(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    info = model.sidecar_info(s.path / "raw.csv.meta.json")
    assert info["ok"] and info["error"] is None
    assert info["produced_by"]["source"] == "script"
    assert info["sha256"] and info["created"]
    assert '"produced_by"' in info["raw"]   # raw stays available for the { } toggle


def test_sidecar_info_reports_bad_json_without_raising(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    sc = s.path / "raw.csv.meta.json"
    sc.write_text("{not valid json")
    info = model.sidecar_info(sc)
    assert not info["ok"]
    assert "not valid JSON" in info["error"]
    assert info["raw"] == "{not valid json"


def test_sidecar_info_missing_file(tmp_path):
    info = model.sidecar_info(tmp_path / "nope.meta.json")
    assert not info["ok"] and "could not read" in info["error"]


def test_sidecar_info_derived_from_refs(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="d")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("proc.graf", derived_from=["raw.csv"]) as fn:
        fn.write_text("y")
    s.close()
    info = model.sidecar_info(s.path / "proc.graf.meta.json")
    assert [r["ref"] for r in info["derived_from"]] == ["raw.csv"]


def test_session_info_structured(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x", "proc.graf": "yy"})
    info = model.session_info(s.path)
    assert info["ok"] and info["error"] is None
    assert info["run_id"] == s.id
    assert info["tags"] == ["demo"] and info["description"] == "a session"
    assert info["status"] == "closed"
    assert info["n_items"] == 2 and info["n_problems"] == 0
    assert info["appendable"] is True          # closed, but created today
    assert info["size"] == 3 and info["size_human"]
    assert "run_id:" in info["raw"]            # session.yaml source for the toggle


def test_session_info_counts_problems_and_history(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "dropped.dat").write_text("no sidecar")
    meta = read_session_yaml(s.path)
    meta.add_history("import", file="dropped.dat", note="by hand", by="tester")
    write_session_yaml(s.path, meta)

    info = model.session_info(s.path)
    assert info["n_items"] == 2 and info["n_problems"] == 1
    assert [h["action"] for h in info["history"]] == ["import"]
    assert info["history"][0]["file"] == "dropped.dat"


def test_session_info_reports_missing_yaml(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    (s.path / "session.yaml").unlink()
    info = model.session_info(s.path)
    assert not info["ok"] and "could not read" in info["error"]


def test_registered_archives_lists_registry(tmp_path, monkeypatch):
    import nebula.registry as reg
    path = tmp_path / "archives.yaml"
    root = tmp_path / "postdoc"
    root.mkdir()
    path.write_text(f"postdoc:\n  root: {root}\nmissing:\n  root: {tmp_path / 'gone'}\n")
    monkeypatch.setenv("NEBULA_REGISTRY", str(path))
    monkeypatch.setattr(reg, "_default_registry", None)

    got = {a["name"]: a for a in model.registered_archives()}
    assert got["postdoc"]["root"] == str(root) and got["postdoc"]["exists"] is True
    assert got["missing"]["exists"] is False


# ---------------------------------------------------------------------
# artefact search
# ---------------------------------------------------------------------

def _search(archive, query="", **kw):
    return model.search_items(archive, query, **kw)


def test_search_items_matches_filename(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"diode_sweep.csv": "x", "notes.txt": "y"})
    res = _search(archive, "diode")
    assert [h["item"].name for h in res["items"]] == ["diode_sweep.csv"]
    assert res["items"][0]["run_id"] == s.id
    assert res["n_sessions"] == 1 and res["n_scanned"] == 2


def test_search_items_matches_session_tags(tmp_path):
    archive = tmp_path / "archive"
    _session_with(archive, {"a.csv": "x"})            # tagged "demo"
    res = _search(archive, "demo")
    assert [h["item"].name for h in res["items"]] == ["a.csv"]
    # ...but not when tags are excluded from the searched fields
    assert _search(archive, "demo", fields=["filename"])["items"] == []


def test_search_items_terms_are_anded(tmp_path):
    archive = tmp_path / "archive"
    _session_with(archive, {"diode.csv": "x", "laser.csv": "y"})
    assert len(_search(archive, "demo csv")["items"]) == 2      # tag + extension
    assert len(_search(archive, "demo diode")["items"]) == 1
    assert _search(archive, "demo nonesuch")["items"] == []


def test_search_items_matches_origin_of_imported_file(tmp_path):
    from nebula import manual
    archive = tmp_path / "archive"
    s = _session_with(archive, {"a.csv": "x"})
    src = tmp_path / "outside.dat"
    src.write_text("hand-made")
    manual.import_file(archive, s.id, src, origin="emailed by Jane")
    res = _search(archive, "jane")
    assert [h["item"].name for h in res["items"]] == ["outside.dat"]
    assert _search(archive, "jane", fields=["filename", "tags"])["items"] == []


def test_search_items_empty_query_matches_nothing(tmp_path):
    archive = tmp_path / "archive"
    _session_with(archive, {"a.csv": "x"})
    res = _search(archive, "   ")
    assert res["items"] == [] and res["n_scanned"] == 0


def test_search_items_date_bounds(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"a.csv": "x"})
    today = datetime.date.today().isoformat()
    assert len(_search(archive, "", date_from=today, date_to=today)["items"]) == 1
    assert _search(archive, "", date_from="2000-01-01", date_to="2000-01-02")["items"] == []
    # a date bound alone is a valid search, with no text query at all
    assert len(_search(archive, "", date_from="2000-01-01")["items"]) == 1


def test_search_items_date_excludes_undated(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"a.csv": "x"})
    (s.path / "a.csv").unlink()                 # stray sidecar keeps its 'created'
    (s.path / "a.csv.meta.json").write_text('{"created": null, "produced_by": {}}')
    assert _search(archive, "", date_from="2000-01-01")["items"] == []


def test_search_items_limit_truncates(tmp_path):
    archive = tmp_path / "archive"
    _session_with(archive, {f"f{i}.csv": "x" for i in range(5)})
    res = _search(archive, "csv", limit=3)
    assert len(res["items"]) == 3 and res["truncated"] is True


# ---------------------------------------------------------------------
# search query syntax: field:, quoting, case sensitivity, wildcards
# ---------------------------------------------------------------------

def _tagged_sessions(archive):
    """Two sessions, tagged twpa-v6 and twpa-V7 respectively, so tests can
    tell substring/exact/case/wildcard rules apart."""
    s6 = nebula.new(archive, tags=["twpa-v6"], description="run six")
    with s6.artifact("a.csv") as fn:
        fn.write_text("x")
    s6.close()
    s7 = nebula.new(archive, tags=["twpa-V7"], description="run seven")
    with s7.artifact("b.csv") as fn:
        fn.write_text("x")
    s7.close()
    return s6, s7


def test_search_bare_term_is_loose_substring(tmp_path):
    archive = tmp_path / "archive"
    _tagged_sessions(archive)
    # unquoted "twpa" is a substring match, so it hits both tags
    assert len(_search(archive, "twpa")["items"]) == 2
    # and an unquoted exact tag only hits the session with that exact tag,
    # since it isn't a substring of the other one
    assert [h["item"].name for h in _search(archive, "twpa-v6")["items"]] == ["a.csv"]


def test_search_single_quotes_are_exact_case_insensitive(tmp_path):
    archive = tmp_path / "archive"
    _tagged_sessions(archive)
    # exact match: 'twpa' alone does not match either compound tag
    assert _search(archive, "'twpa'")["items"] == []
    # but the exact tag, single-quoted, matches case-insensitively
    assert [h["item"].name for h in _search(archive, "'twpa-v6'")["items"]] == ["a.csv"]
    assert [h["item"].name for h in _search(archive, "'twpa-v7'")["items"]] == ["b.csv"]


def test_search_double_quotes_are_exact_case_sensitive(tmp_path):
    archive = tmp_path / "archive"
    _tagged_sessions(archive)
    assert [h["item"].name for h in _search(archive, '"twpa-v6"')["items"]] == ["a.csv"]
    # wrong case does not match under double quotes
    assert _search(archive, '"twpa-V6"')["items"] == []
    # but does match the session that actually has that case
    assert [h["item"].name for h in _search(archive, '"twpa-V7"')["items"]] == ["b.csv"]


def test_search_wildcards_in_quotes(tmp_path):
    archive = tmp_path / "archive"
    _tagged_sessions(archive)
    # a bare quoted "twpa" hits neither compound tag...
    assert _search(archive, '"twpa"')["items"] == []
    # ...but a wildcarded one reaches both, case-insensitively via '' ...
    assert len(_search(archive, "'twpa*'")["items"]) == 2
    # ...and case-sensitively via "" when the case actually lines up
    assert len(_search(archive, '"twpa-v*"')["items"]) == 1  # only twpa-v6
    assert len(_search(archive, '"twpa-V*"')["items"]) == 1  # only twpa-V7


def test_search_field_prefix_scopes_the_match(tmp_path):
    archive = tmp_path / "archive"
    _tagged_sessions(archive)
    # tag: scopes to the tags field regardless of the fields= filter --
    # naming a field is a deliberate ask the checkboxes shouldn't defeat
    res = _search(archive, "tag:'twpa*'", fields=["filename"])
    assert len(res["items"]) == 2
    res = _search(archive, "tag:'twpa-v6'", fields=[])
    assert [h["item"].name for h in res["items"]] == ["a.csv"]
    # session: matches the run's description exactly
    assert [h["item"].name for h in
            _search(archive, "session:'run six'")["items"]] == ["a.csv"]
    # but not a substring of it, once quoted for an exact match
    assert _search(archive, "session:'run'")["items"] == []


def test_search_field_prefix_requires_a_known_name(tmp_path):
    archive = tmp_path / "archive"
    _tagged_sessions(archive)
    # an unrecognised field name is kept as part of the literal term
    res = _search(archive, "bogus:twpa-v6")
    assert res["items"] == []  # "bogus:twpa-v6" isn't a substring of any field


def test_search_clauses_still_and_together(tmp_path):
    archive = tmp_path / "archive"
    _tagged_sessions(archive)
    assert [h["item"].name for h in
            _search(archive, "tag:'twpa-v6' filename:'a.csv'")["items"]] == ["a.csv"]
    assert _search(archive, "tag:'twpa-v6' filename:'b.csv'")["items"] == []


# ---------------------------------------------------------------------
# CLI: `nebula search` uses the same grammar as the GUI/model
# ---------------------------------------------------------------------

def test_cli_search_matches_and_reports_a_count(tmp_path, capsys):
    from nebula import cli
    archive = tmp_path / "archive"
    _tagged_sessions(archive)

    cli.main(["search", str(archive), "tag:'twpa-v6'"])
    out = capsys.readouterr().out
    assert "a.csv" in out and "b.csv" not in out
    assert "1 match" in out


def test_cli_search_wildcard_and_json(tmp_path, capsys):
    from nebula import cli
    archive = tmp_path / "archive"
    _tagged_sessions(archive)

    cli.main(["search", str(archive), "tag:'twpa*'", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert {i["filename"] for i in payload["items"]} == {"a.csv", "b.csv"}
    assert payload["n_sessions"] == 2


def test_cli_search_no_matches(tmp_path, capsys):
    from nebula import cli
    archive = tmp_path / "archive"
    _tagged_sessions(archive)

    cli.main(["search", str(archive), '"twpa"'])
    assert "no matches" in capsys.readouterr().out


# ---------------------------------------------------------------------
# lineage (both directions of the provenance graph)
# ---------------------------------------------------------------------

def _derived_session(archive):
    """A session where log.txt declares it came from raw.csv."""
    s = nebula.new(archive, description="derived")
    with s.artifact("raw.csv") as fn:
        fn.write_text("x")
    with s.artifact("log.txt", derived_from=["raw.csv"]) as fn:
        fn.write_text("y")
    s.close()
    return s


def test_lineage_upstream(tmp_path):
    archive = tmp_path / "archive"
    s = _derived_session(archive)
    lin = model.lineage(archive, s.path, "log.txt")
    assert lin["run_id"] == s.id
    assert [u["ref"] for u in lin["upstream"]] == ["raw.csv"]
    up = lin["upstream"][0]
    assert up["run_id"] == s.id and up["filename"] == "raw.csv"
    assert up["exists"] is True and up["resolved"] is True
    assert up["path"] == str(s.path / "raw.csv")
    assert lin["downstream"] == []


def test_lineage_downstream(tmp_path):
    """The direction `nebula show` never displays: looking at the source
    file, what came from it."""
    archive = tmp_path / "archive"
    s = _derived_session(archive)
    lin = model.lineage(archive, s.path, "raw.csv")
    assert lin["upstream"] == []
    assert [d["ref"] for d in lin["downstream"]] == [f"{s.id}/log.txt"]
    assert lin["downstream"][0]["exists"] is True
    assert lin["downstream"][0]["same_session"] is True


def test_lineage_downstream_across_sessions(tmp_path):
    archive = tmp_path / "archive"
    src = _session_with(archive, {"raw.csv": "x"})
    other = nebula.new(archive, description="analysis")
    with other.artifact("fit.json", derived_from=[f"{src.id}/raw.csv"]) as fn:
        fn.write_text("{}")
    other.close()

    lin = model.lineage(archive, src.path, "raw.csv")
    assert [d["ref"] for d in lin["downstream"]] == [f"{other.id}/fit.json"]
    assert lin["downstream"][0]["same_session"] is False


def test_lineage_flags_missing_target(tmp_path):
    archive = tmp_path / "archive"
    s = _derived_session(archive)
    (s.path / "raw.csv").unlink()             # the source is gone
    up = model.lineage(archive, s.path, "log.txt")["upstream"][0]
    assert up["exists"] is False and up["resolved"] is True
    assert up["note"] == "file is missing"


def test_lineage_flags_unknown_session(tmp_path):
    archive = tmp_path / "archive"
    s = nebula.new(archive, description="d")
    with s.artifact("a.csv", derived_from=["S-9999/gone.csv"]) as fn:
        fn.write_text("x")
    s.close()
    up = model.lineage(archive, s.path, "a.csv")["upstream"][0]
    assert up["exists"] is False
    assert "S-9999 not found" in up["note"]


def test_lineage_flags_unregistered_archive(tmp_path, monkeypatch):
    import nebula.registry as reg
    monkeypatch.setenv("NEBULA_REGISTRY", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(reg, "_default_registry", None)

    archive = tmp_path / "archive"
    s = nebula.new(archive, description="d")
    with s.artifact("a.csv", derived_from=["elsewhere|S-0001/x.csv"]) as fn:
        fn.write_text("x")
    s.close()
    up = model.lineage(archive, s.path, "a.csv")["upstream"][0]
    assert up["resolved"] is False
    assert "not registered" in up["note"]


def test_lineage_ignores_stale_index(tmp_path):
    """Downstream is a filesystem scan, so a never-built index is fine."""
    archive = tmp_path / "archive"
    s = _derived_session(archive)
    assert not (archive / "index.db").exists()
    assert model.lineage(archive, s.path, "raw.csv")["downstream"]


def test_sidecar_info_marks_assumed_source(tmp_path):
    """A sidecar written before produced_by.source existed must not claim
    'script' as though it were recorded."""
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    sc = s.path / "raw.csv.meta.json"
    data = json.loads(sc.read_text())
    del data["produced_by"]["source"]          # emulate an old sidecar
    sc.write_text(json.dumps(data))

    info = model.sidecar_info(sc)
    assert info["ok"] and info["source_recorded"] is False
    assert info["produced_by"]["source"] == "script"   # still the default

    # A sidecar written by the current code does record it.
    fresh = _session_with(tmp_path / "archive2", {"new.csv": "x"})
    got = model.sidecar_info(fresh.path / "new.csv.meta.json")
    assert got["source_recorded"] is True and got["produced_by"]["source"] == "script"


# ---------------------------------------------------------------------
# derived_from on GUI import + ref validation
# ---------------------------------------------------------------------

def test_import_file_records_derived_from(tmp_path):
    """The bridge op must pass derived_from through, matching the CLI's
    `nebula import --derived-from`."""
    from nebula.navigator import api
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    src = tmp_path / "fit.json"
    src.write_text("{}")

    api.dispatch("import_file", {
        "archive": str(archive), "run_id": s.id, "paths": [str(src)],
        "origin": "by hand", "derived_from": ["raw.csv"], "allow_frozen": True,
    })
    info = model.sidecar_info(s.path / "fit.json.meta.json")
    assert [r["ref"] for r in info["derived_from"]] == ["raw.csv"]
    assert info["produced_by"]["origin"] == "by hand"
    # ...and the new link is visible from the other end straight away
    assert [d["filename"] for d in model.lineage(archive, s.path, "raw.csv")["downstream"]] \
        == ["fit.json"]


def test_import_file_ignores_blank_derived_from(tmp_path):
    from nebula.navigator import api
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    src = tmp_path / "note.txt"
    src.write_text("hi")
    api.dispatch("import_file", {
        "archive": str(archive), "run_id": s.id, "paths": [str(src)],
        "derived_from": ["", "  "], "allow_frozen": True,
    })
    assert model.sidecar_info(s.path / "note.txt.meta.json")["derived_from"] == []


def test_resolve_refs_reports_each_case(tmp_path):
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    got = model.resolve_refs(archive, s.id,
                             ["raw.csv", "nope.csv", "S-9999/x.csv", "a|b|c"])

    assert got[0]["valid"] and got[0]["exists"]
    assert got[1]["valid"] and not got[1]["exists"] and got[1]["note"] == "file is missing"
    assert got[2]["valid"] and "S-9999 not found" in got[2]["note"]
    assert not got[3]["valid"] and got[3]["error"]      # unparseable ref


def test_list_items_carries_build_provenance(tmp_path):
    """The details strip shows repo/commit/entry point without opening the
    sidecar, so list_items has to carry them."""
    archive = tmp_path / "archive"
    s = _session_with(archive, {"raw.csv": "x"})
    sc = s.path / "raw.csv.meta.json"
    data = json.loads(sc.read_text())
    data["produced_by"].update({"repo": "nebula", "commit": "48c4a28c" + "0" * 32,
                                "dirty": True, "entry_point": "examples/run.py"})
    sc.write_text(json.dumps(data))

    it = {i.name: i for i in model.list_items(s.path)}["raw.csv"]
    assert it.repo == "nebula" and it.commit.startswith("48c4a28c")
    assert it.dirty is True and it.entry_point == "examples/run.py"


def test_list_items_counts_derived_from(tmp_path):
    archive = tmp_path / "archive"
    s = _derived_session(archive)
    items = {i.name: i for i in model.list_items(s.path)}
    assert items["log.txt"].n_derived_from == 1
    assert items["raw.csv"].n_derived_from == 0
