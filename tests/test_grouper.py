import hashlib, os, shutil

import pytest
from PIL import ImageOps

import grouper_core as core


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


@pytest.fixture
def album(tmp_path, make_photo):
    """a: original + exact copy + small WhatsApp-style copy; b: original + rotated + mirrored; c: alone."""
    root = tmp_path / "album"
    (root / "Camera").mkdir(parents=True); (root / "WhatsApp").mkdir(); (root / "Edited").mkdir()
    a, b, c = make_photo(1), make_photo(2), make_photo(3)
    a.save(root / "Camera" / "a.jpg", quality=92)
    shutil.copy2(root / "Camera" / "a.jpg", root / "Edited" / "a copy.jpg")
    a.resize((450, 300)).save(root / "WhatsApp" / "a_wa.jpg", quality=60)
    b.save(root / "Camera" / "b.jpg", quality=92)
    b.rotate(90, expand=True).save(root / "Edited" / "b_rotated.jpg", quality=90)
    ImageOps.mirror(b).save(root / "Edited" / "b_mirror.jpg", quality=90)
    c.save(root / "Camera" / "c.jpg", quality=92)
    return root


def snapshot(root):
    return {os.path.relpath(os.path.join(dp, f), root): md5(os.path.join(dp, f))
            for dp, dns, fns in os.walk(root) if core.WORK_DIR not in dp for f in fns}


def test_scan_finds_copies(album):
    r = core.scan(str(album), use_ai=False, workers=2)
    assert r["groups"] == 2
    assert r["photos"] == 6
    assert os.path.exists(r["report"])


def test_scan_moves_nothing(album):
    before = snapshot(album)
    core.scan(str(album), use_ai=False, workers=2)
    assert snapshot(album) == before


def test_move_keeps_best_and_undo_restores(album):
    before = snapshot(album)
    core.scan(str(album), use_ai=False, workers=2)
    moved, skipped, gd = core.move(str(album), log=lambda m: None)
    assert (moved, skipped) == (6, 0)
    keeps = sorted(f for dp, _, fns in os.walk(gd) for f in fns if f.startswith("KEEP_"))
    assert len(keeps) == 2
    assert "KEEP_a_wa.jpg" not in keeps  # the small copy must never be the one kept
    assert core.has_undo(str(album))

    back, recycled = core.undo(str(album))
    assert (back, recycled) == (6, 0)
    assert snapshot(album) == before
    assert not os.path.exists(gd)


def test_find_extras_skips_groups_without_keep(album):
    core.scan(str(album), use_ai=False, workers=2)
    _, _, gd = core.move(str(album), log=lambda m: None)
    extras, size = core.find_extras(str(album))
    assert len(extras) == 4 and size > 0
    assert not any(os.path.basename(p).startswith("KEEP_") for p in extras)

    # rename a group's KEEP_ photo away: that group must be left alone completely
    g1 = os.path.join(gd, "group_001")
    keep = next(f for f in os.listdir(g1) if f.startswith("KEEP_"))
    os.rename(os.path.join(g1, keep), os.path.join(g1, keep[5:].replace(".jpg", "_x.jpg")))
    extras2, _ = core.find_extras(str(album))
    assert not any(p.startswith(g1 + os.sep) for p in extras2)
    assert len(extras2) == len(extras) - (len(os.listdir(g1)) - 1)


def test_recycle_extras_and_undo(album, monkeypatch):
    core.scan(str(album), use_ai=False, workers=2)
    _, _, gd = core.move(str(album), log=lambda m: None)
    sent = []

    def fake_bin(paths):  # stand-in for the Windows Recycle Bin
        sent.extend(paths)
        for p in paths: os.remove(p)

    monkeypatch.setattr(core, "_to_recycle_bin", fake_bin)
    monkeypatch.setattr(core, "_check_recycle_bin", lambda root: None)
    n, freed, left = core.recycle_extras(str(album), log=lambda m: None)
    assert (n, left) == (4, 0) and freed > 0
    assert all(not os.path.basename(p).startswith("KEEP_") for p in sent)
    remaining = [f for dp, _, fns in os.walk(gd) for f in fns]
    assert sorted(remaining) == sorted(f for f in remaining if f.startswith("KEEP_")) and len(remaining) == 2
    assert not core.has_extras(str(album))
    with pytest.raises(core.FriendlyError):
        core.recycle_extras(str(album), log=lambda m: None)

    back, recycled = core.undo(str(album))
    assert (back, recycled) == (2, 4)
    assert (album / "Camera" / "a.jpg").exists() and (album / "Camera" / "b.jpg").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Recycle Bin")
def test_recycle_refused_on_network_path():
    with pytest.raises(core.FriendlyError):
        core._check_recycle_bin(r"\\server\share\photos")


def test_version_is_semver():
    parts = core.__version__.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
