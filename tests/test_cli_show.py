"""
`nebula show`'s -u/--uri, -t/--tag, -l/--long flags.

These print per-artifact detail that used to require the Navigator GUI --
a URI, tags/comment, and sha256/size -- straight from `nebula show`, so a
headless machine can still answer "what did I just save, and what's its
URI".
"""

import nebula
from nebula import transfer
from nebula.cli import main
from nebula.registry import get_registry


def _archive(root, *, name="postdoc", user="g@ncsu.edu"):
    transfer.init_archive(root, kind="standard", name=name, user=user)
    get_registry().register_archive(root)
    return root


def _session(root, *, filename="raw.csv", tags=None):
    s = nebula.new(root, description="a run", announce=False)
    with s.artifact(filename, tags=tags or []) as fn:
        fn.write_text("x")
    s.close()
    return s


def test_show_plain_has_none_of_the_new_fields(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    s = _session(root, tags=["RP23D"])
    main(["show", "postdoc", s.id])
    out = capsys.readouterr().out
    assert "raw.csv" in out
    assert "uri:" not in out
    assert "tags: RP23D" not in out
    assert "sha256:" not in out


def test_show_uri_flag_prints_a_uri_per_artifact(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    s = _session(root)
    main(["show", "postdoc", s.id, "-u"])
    out = capsys.readouterr().out
    assert "uri:" in out
    assert "nebula://g@ncsu.edu/postdoc" in out
    assert "raw.csv" in out


def test_show_tag_flag_prints_the_artifacts_own_tags(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    s = _session(root, tags=["RP23D"])
    main(["show", "postdoc", s.id, "--tag"])
    out = capsys.readouterr().out
    assert "tags: RP23D" in out


def test_show_tag_flag_says_none_when_untagged(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    s = _session(root)
    main(["show", "postdoc", s.id, "--tags"])
    out = capsys.readouterr().out
    assert "tags: -" in out


def test_show_long_flag_prints_sha_and_size(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    s = _session(root)
    main(["show", "postdoc", s.id, "-l"])
    out = capsys.readouterr().out
    assert "sha256:" in out
    assert "size:" in out


def test_show_flags_combine(tmp_path, capsys):
    root = _archive(tmp_path / "postdoc")
    s = _session(root, tags=["a", "b"])
    main(["show", "postdoc", s.id, "-u", "-t", "-l"])
    out = capsys.readouterr().out
    assert "uri:" in out
    assert "tags: a, b" in out
    assert "sha256:" in out
