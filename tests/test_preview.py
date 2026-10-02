import pytest

from nebula.navigator import preview

h5py = pytest.importorskip("h5py")
np = pytest.importorskip("numpy")


def test_text_and_csv_and_image(tmp_path):
    (tmp_path / "a.py").write_text("print('hi')\n")
    r = preview.preview_file(tmp_path / "a.py")
    assert r["kind"] == "text" and "print" in r["text"]

    (tmp_path / "t.csv").write_text("a,b\n1,2\n3,4\n")
    r = preview.preview_file(tmp_path / "t.csv")
    assert r["columns"] == ["a", "b"] and r["rows"] == [["1", "2"], ["3", "4"]]

    (tmp_path / "p.PNG").write_bytes(b"\x89PNG fake")
    r = preview.preview_file(tmp_path / "p.PNG")
    assert r["kind"] == "image" and r["uri"].startswith("data:image/png;base64,")

    (tmp_path / "x.bin").write_bytes(b"\x00\x01")
    assert preview.preview_file(tmp_path / "x.bin")["kind"] == "none"
    assert preview.preview_file(tmp_path / "missing.txt")["kind"] == "none"


def test_binary_named_txt_is_refused(tmp_path):
    (tmp_path / "b.txt").write_bytes(b"\x00" * 100)
    assert preview.preview_file(tmp_path / "b.txt")["kind"] == "none"


def test_hdf_and_tome_tree_and_node(tmp_path):
    p = tmp_path / "d.tome"
    with h5py.File(p, "w") as f:
        g = f.create_group("g")
        g.attrs["__pytype__"] = "dict"
        g["x"] = np.arange(600.0).reshape(300, 2)
        g["s"] = 2.5
        g["cube"] = np.zeros((2, 3, 4))
    r = preview.preview_file(p)
    assert r["kind"] == "hdf"
    assert [n["path"] for n in r["nodes"]] == ["/", "/g", "/g/cube", "/g/s", "/g/x"]
    assert r["nodes"][1]["pytype"] == "dict"
    x = preview.hdf_node(p, "/g/x")
    assert x["shape"] == [300, 2] and len(x["rows"]) == 200 and "first 200 of 300" in x["note"]
    assert preview.hdf_node(p, "/g/s")["scalar"] == "2.5"
    assert preview.hdf_node(p, "/g/cube")["columns"] == ["#", "0", "1", "2", "3"]
    grp = preview.hdf_node(p, "/g")
    assert grp["members"] == ["cube", "s", "x"] and grp["attrs"] == [["__pytype__", "dict"]]


def test_corrupt_hdf_does_not_raise(tmp_path):
    (tmp_path / "bad.h5").write_bytes(b"not hdf")
    assert preview.preview_file(tmp_path / "bad.h5")["kind"] == "none"


def test_graf_renders_to_png(tmp_path):
    graf = pytest.importorskip("graf")
    plt = pytest.importorskip("matplotlib.pyplot")
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [1, 4, 9])
    graf.save_graf(fig, str(tmp_path / "f.graf"))
    plt.close(fig)
    r = preview.preview_file(tmp_path / "f.graf")
    assert r["kind"] == "image" and r["uri"].startswith("data:image/png;base64,")
