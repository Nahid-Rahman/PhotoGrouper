import hashlib, os, time

import numpy as np
import pytest
from PIL import Image, ImageCms

import grouper_core as core
import converter

pytestmark = pytest.mark.skipif(not core.HEIF, reason="pillow-heif not installed")

DATE = "2024:07:12 19:15:44"
OLD = time.mktime((2024, 7, 12, 19, 15, 44, 0, 0, -1))


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def psnr(a, b):
    a = np.asarray(a.convert("RGB"), float); b = np.asarray(b.convert("RGB"), float)
    mse = ((a - b) ** 2).mean()
    return 99.0 if mse == 0 else 10 * np.log10(255 ** 2 / mse)


def make_heic(path, img, orientation=1, alpha=False):
    ex = Image.Exif()
    ex[0x0112] = orientation; ex[0x010F] = "Apple"; ex[0x0110] = "iPhone 15 Pro"
    ex.get_ifd(0x8769)[0x9003] = DATE
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    if alpha:
        img = img.convert("RGBA")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "HEIF", quality=90, exif=ex.tobytes(), icc_profile=icc)
    os.utime(path, (OLD, OLD))
    return icc


@pytest.fixture
def heics(tmp_path, make_photo):
    root = tmp_path / "iphone"
    icc = make_heic(str(root / "IMG_0001.HEIC"), make_photo(11, (1200, 800)))
    make_heic(str(root / "2024" / "IMG_0002.heic"), make_photo(12, (1200, 800)), orientation=6)
    make_heic(str(root / "2024" / "IMG_0003.heif"), make_photo(13, (640, 480)), alpha=True)
    return root, icc


def test_find_heic(heics, tmp_path):
    root, _ = heics
    (root / core.GROUP_DIR).mkdir()
    make_heic(str(root / core.GROUP_DIR / "skip.heic"), Image.new("RGB", (64, 64)))
    (root / "notes.txt").write_text("x")
    found = sorted(os.path.basename(p) for p, _ in converter.find_heic([str(root)]))
    assert found == ["IMG_0001.HEIC", "IMG_0002.heic", "IMG_0003.heif"]
    # picking single files, with a duplicate entry
    one = str(root / "IMG_0001.HEIC")
    assert [p for p, _ in converter.find_heic([one, one])] == [one]


def test_quality_and_metadata_kept(heics):
    root, icc = heics
    src = str(root / "IMG_0001.HEIC")
    before = md5(src)
    r = converter.convert([str(root)], workers=2, log=lambda m: None)
    assert r["ok"] == 3 and r["exists"] == 0 and not r["errors"]

    dst = str(root / "IMG_0001.jpg")
    heic, jpg = Image.open(src), Image.open(dst)
    assert jpg.format == "JPEG" and jpg.size == heic.size == (1200, 800)
    assert psnr(heic, jpg) > 40  # visually identical
    ex = jpg.getexif()
    assert ex.get(0x010F) == "Apple" and ex.get(0x0110) == "iPhone 15 Pro"
    assert ex.get_ifd(0x8769).get(0x9003) == DATE
    assert jpg.info.get("icc_profile") == icc
    assert abs(os.path.getmtime(dst) - OLD) < 2
    assert md5(src) == before  # the HEIC is untouched
    assert os.path.exists(src)


def test_rotated_photo_stays_upright(heics):
    root, _ = heics
    converter.convert([str(root)], workers=2, log=lambda m: None)
    heic = Image.open(root / "2024" / "IMG_0002.heic")
    jpg = Image.open(root / "2024" / "IMG_0002.jpg")
    assert heic.size == (800, 1200)  # stored turned, shown upright
    assert jpg.size == (800, 1200)
    assert jpg.getexif().get(0x0112, 1) == 1  # no second rotation by the viewer
    assert psnr(heic, jpg) > 40


def test_transparent_heic_becomes_white(heics):
    root, _ = heics
    converter.convert([str(root)], workers=2, log=lambda m: None)
    jpg = Image.open(root / "2024" / "IMG_0003.jpg")
    assert jpg.mode == "RGB" and jpg.size == (640, 480)


def test_never_overwrites_and_rerun_skips(heics):
    root, _ = heics
    (root / "IMG_0001.jpg").write_bytes(b"my own file")
    r = converter.convert([str(root)], workers=2, log=lambda m: None)
    assert r["ok"] == 2 and r["exists"] == 1
    assert (root / "IMG_0001.jpg").read_bytes() == b"my own file"
    r2 = converter.convert([str(root)], workers=2, log=lambda m: None)
    assert r2["ok"] == 0 and r2["exists"] == 3
    assert not list(root.rglob("*.part"))


def test_output_folder_keeps_subfolders(heics, tmp_path):
    root, _ = heics
    out = tmp_path / "jpg"
    r = converter.convert([str(root)], out_dir=str(out), workers=2, log=lambda m: None)
    assert r["ok"] == 3
    assert (out / "IMG_0001.jpg").exists()
    assert (out / "2024" / "IMG_0002.jpg").exists()
    assert not (root / "IMG_0001.jpg").exists()


@pytest.mark.parametrize("preset", list(converter.PRESETS))
def test_presets(heics, tmp_path, preset):
    root, _ = heics
    src = str(root / "IMG_0001.HEIC")
    out = tmp_path / preset
    converter.convert([src], out_dir=str(out), preset=preset, workers=1, log=lambda m: None)
    assert psnr(Image.open(src), Image.open(out / "IMG_0001.jpg")) > 38


def test_no_heic_found(tmp_path):
    with pytest.raises(core.FriendlyError):
        converter.convert([str(tmp_path)], log=lambda m: None)
