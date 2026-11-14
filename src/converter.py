"""
converter.py - HEIC/HEIF to JPG for Photo Grouper.
Keeps the full resolution, the colours (ICC profile), the photo info (EXIF: date taken, camera,
location; XMP) and the file dates. The HEIC files are never changed or deleted, and an existing
JPG is never overwritten.
"""
import os, time
from concurrent.futures import ProcessPoolExecutor

from PIL import Image

import grouper_core as core  # also registers the HEIF opener

HEIC_EXT = {".heic", ".heif", ".hif"}

# name: (JPEG quality, chroma subsampling: 0 = full colour resolution 4:4:4, 2 = 4:2:0)
PRESETS = {
    "Best": (95, 0),
    "Maximum": (100, 0),
    "Smaller": (88, 2),
}

ORIENTATION, EXIF_IFD, MAKERNOTE = 0x0112, 0x8769, 0x927C


def find_heic(sources):
    """sources: files and/or folders (folders include subfolders).
    Returns a list of (heic_path, base_folder) where base_folder is used to mirror subfolders."""
    out, seen = [], set()
    for s in sources:
        s = os.path.abspath(s)
        if os.path.isdir(s):
            for dp, dns, fns in os.walk(s):
                dns[:] = [d for d in dns if d not in core.SKIP_DIRS]
                for f in fns:
                    if os.path.splitext(f)[1].lower() in HEIC_EXT:
                        out.append((os.path.join(dp, f), s))
        elif os.path.isfile(s) and os.path.splitext(s)[1].lower() in HEIC_EXT:
            out.append((s, os.path.dirname(s)))
    uniq = []
    for p, base in out:
        k = os.path.normcase(p)
        if k not in seen:
            seen.add(k); uniq.append((p, base))
    uniq.sort(key=lambda t: t[0].lower())
    return uniq


def target_path(src, base, out_dir=None):
    stem = os.path.splitext(os.path.basename(src))[0]
    if not out_dir:
        return os.path.join(os.path.dirname(src), stem + ".jpg")
    rel = os.path.relpath(os.path.dirname(src), base)
    return os.path.normpath(os.path.join(out_dir, rel, stem + ".jpg"))


def _set_created(path, ts):
    """Copy the 'Date created' that Windows Explorer shows. Silently does nothing elsewhere."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        h = k.CreateFileW(path, 0x100, 0, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES, OPEN_EXISTING
        if h in (None, wintypes.HANDLE(-1).value):
            return
        ft = int(ts * 10_000_000) + 116444736000000000
        c = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
        k.SetFileTime(h, ctypes.byref(c), None, None)
        k.CloseHandle(h)
    except Exception:
        pass


def _save_jpeg(img, path, quality, subsampling, exif, icc, xmp):
    kw = dict(quality=quality, subsampling=subsampling, optimize=True)
    if icc: kw["icc_profile"] = icc
    if xmp: kw["xmp"] = xmp
    try:
        img.save(path, "JPEG", exif=exif.tobytes(), **kw)
    except ValueError as e:
        if "too long" not in str(e).lower():
            raise
        exif.get_ifd(EXIF_IFD).pop(MAKERNOTE, None)  # camera-maker blob; JPEG allows only 64 KB of EXIF
        img.save(path, "JPEG", exif=exif.tobytes(), **kw)


def convert_one(job):
    """Runs in a worker process. job = (src, dst, quality, subsampling)."""
    src, dst, quality, subsampling = job
    tmp = dst + ".part"
    try:
        if os.path.exists(dst):
            return dict(src=src, dst=dst, status="exists")
        st = os.stat(src)
        with Image.open(src) as im:
            im.load()
            info = dict(im.info)
            exif = im.getexif()
            # pillow-heif has already turned the pixels upright, so the JPG must not rotate them again
            if exif.get(ORIENTATION, 1) != 1:
                exif[ORIENTATION] = 1
            if "A" in im.getbands():
                rgba = im.convert("RGBA")
                img = Image.new("RGB", rgba.size, (255, 255, 255))
                img.paste(rgba, mask=rgba.getchannel("A"))
            else:
                img = im.convert("RGB")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _save_jpeg(img, tmp, quality, subsampling, exif, info.get("icc_profile"), info.get("xmp"))
        if os.path.exists(dst):  # appeared while we worked: never overwrite
            os.remove(tmp)
            return dict(src=src, dst=dst, status="exists")
        os.replace(tmp, dst)
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        _set_created(dst, getattr(st, "st_birthtime", st.st_ctime))
        return dict(src=src, dst=dst, status="ok", in_size=st.st_size, out_size=os.path.getsize(dst))
    except Exception as e:
        try: os.remove(tmp)
        except OSError: pass
        return dict(src=src, dst=dst, status="error", err=f"{type(e).__name__}: {e}")


def convert(sources, out_dir=None, preset="Best", log=print, progress=lambda *a: None,
            cancel=lambda: False, workers=None):
    """Convert every HEIC/HEIF in `sources` to JPG.
    out_dir None: each JPG goes next to its HEIC. Otherwise into out_dir, keeping subfolders.
    Returns dict(ok, exists, errors, in_bytes, out_bytes)."""
    if not core.HEIF:
        raise core.FriendlyError("HEIC support is missing from this copy of Photo Grouper "
                                 "(the pillow-heif library is not installed).")
    if out_dir:
        out_dir = os.path.abspath(out_dir)
        try:
            os.makedirs(out_dir, exist_ok=True)
            test = os.path.join(out_dir, ".write_test")
            with open(test, "w") as f: f.write("ok")
            os.remove(test)
        except OSError as e:
            raise core.FriendlyError("Photo Grouper cannot save files in the output folder. Choose "
                                     f"another folder.\n\n(Details: {e})") from e
    files = find_heic(sources)
    if not files:
        raise core.FriendlyError("No HEIC or HEIF photos were found here.\n\n"
                                 "Choose a folder (subfolders are included) or pick the files directly.")
    quality, sub = PRESETS[preset]
    jobs = [(src, target_path(src, base, out_dir), quality, sub) for src, base in files]
    log(f"Found {len(jobs)} HEIC photos. Converting to JPG ({preset} quality)...")
    res = dict(ok=0, exists=0, errors=[], in_bytes=0, out_bytes=0)
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for k, r in enumerate(ex.map(convert_one, jobs, chunksize=1), 1):
            if cancel():
                ex.shutdown(wait=False, cancel_futures=True); raise KeyboardInterrupt
            if r["status"] == "ok":
                res["ok"] += 1; res["in_bytes"] += r["in_size"]; res["out_bytes"] += r["out_size"]
            elif r["status"] == "exists":
                res["exists"] += 1
                log(f"Skipped, a JPG with this name already exists: {r['dst']}")
            else:
                res["errors"].append((r["src"], r["err"]))
                log(f"Could not convert {r['src']}: {r['err']}")
            el = time.time() - t0
            progress(k, len(jobs), f"Converting {k}/{len(jobs)}  (~{el / k * (len(jobs) - k):.0f}s left)")
    return res
