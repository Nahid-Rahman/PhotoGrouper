"""
grouper_core.py - engine for Photo Grouper.
Finds exact copies, resized/recompressed/rotated/mirrored copies (perceptual hash),
and optionally photos that look the same (CLIP AI via ONNX Runtime).
Nothing is deleted permanently: groups are moved into <folder>\\_similar_groups\\group_XXX,
the best copy gets a KEEP_ prefix, and every move can be undone. Extra copies can be sent to
the Windows Recycle Bin on request.
"""
import base64, csv, hashlib, html, io, os, pickle, shutil, time, urllib.request
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF = True
except Exception:
    HEIF = False
try:
    import rawpy
    RAW = True
except Exception:
    RAW = False

IMG_EXT = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif",
           ".heic", ".heif", ".dng", ".nef", ".cr2", ".cr3", ".arw", ".orf", ".rw2"}
RAW_EXT = {".dng", ".nef", ".cr2", ".cr3", ".arw", ".orf", ".rw2"}
GROUP_DIR = "_similar_groups"
WORK_DIR = "_photo_grouper"
SKIP_DIRS = {GROUP_DIR, WORK_DIR, "_dedupe_tool"}

MODEL_URL = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_quantized.onnx"
MODEL_NAME = "clip_vit_b32_vision_quantized.onnx"
EMB_KEY = "emb_onnx_q"

__version__ = "1.1.0"


class FriendlyError(Exception):
    """An error whose message is safe to show to a normal user as-is."""


PRESETS = {  # name: (hash limit, AI similarity)
    "Strict": (6, 0.96),
    "Normal": (8, 0.94),
    "Loose": (10, 0.92),
}


def app_data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "PhotoGrouper")
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- per-image work
def open_image(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXT:
        if RAW:
            try:
                with rawpy.imread(path) as r:
                    full = (r.sizes.width, r.sizes.height)
                    try:
                        th = r.extract_thumb()
                        if th.format == rawpy.ThumbFormat.JPEG:
                            im = Image.open(io.BytesIO(th.data)); im.load()
                            return ImageOps.exif_transpose(im), full
                    except Exception:
                        pass
                    return Image.fromarray(r.postprocess(half_size=True, use_camera_wb=True)), full
            except Exception:
                pass  # fall through and let Pillow try (some phone DNGs)
    im = Image.open(path)
    full = im.size
    if im.format == "JPEG":
        im.draft("RGB", (600, 600))
    return ImageOps.exif_transpose(im), full


_DCT = None
def phash_bits(img):
    global _DCT
    if _DCT is None:
        k = np.arange(32)
        _DCT = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / 64)
    a = np.asarray(img.convert("L").resize((32, 32), Image.LANCZOS), dtype=np.float64)
    low = (_DCT @ a @ _DCT.T)[:8, :8].flatten()
    return low > np.median(low[1:])


def dihedral(img):
    out = []
    for m in (img, ImageOps.mirror(img)):
        for rot in (0, 90, 180, 270):
            out.append(m.rotate(rot, expand=True) if rot else m)
    return out


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def process(path):
    try:
        st = os.stat(path)
        im, (w, h) = open_image(path)
        im = im.convert("RGB")
        small = im.copy(); small.thumbnail((256, 256))
        hashes = np.stack([phash_bits(t) for t in dihedral(small)])
        th = im.copy(); th.thumbnail((220, 220))
        b1 = io.BytesIO(); th.save(b1, "JPEG", quality=70)
        cs = im.copy(); cs.thumbnail((336, 336))
        b2 = io.BytesIO(); cs.save(b2, "JPEG", quality=90)
        return dict(path=path, size=st.st_size, mtime=st.st_mtime, w=w, h=h, md5=file_md5(path),
                    hashes=hashes, thumb=b1.getvalue(), clipsrc=b2.getvalue(), err=None)
    except Exception as e:
        return dict(path=path, err=f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- AI (ONNX CLIP)
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)


def clip_pre(jpeg_bytes):
    im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    w, h = im.size
    s = 224 / min(w, h)
    im = im.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
    w, h = im.size
    l, t = (w - 224) // 2, (h - 224) // 2
    a = np.asarray(im.crop((l, t, l + 224, t + 224)), dtype=np.float32) / 255.0
    return ((a - MEAN) / STD).transpose(2, 0, 1)


def ensure_model(log, progress):
    path = os.path.join(app_data_dir(), MODEL_NAME)
    if os.path.exists(path) and os.path.getsize(path) > 10_000_000:
        return path
    log("Downloading AI model (one time, ~89 MB)...")
    tmp = path + ".part"
    req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "PhotoGrouper"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk: break
                f.write(chunk); got += len(chunk)
                if total: progress(got, total, f"Downloading AI model {got >> 20}/{total >> 20} MB")
        if total and got < total:
            raise IOError("download was cut off")
    except Exception as e:
        try: os.remove(tmp)
        except OSError: pass
        raise FriendlyError(
            "Could not download the AI model (about 89 MB).\n\n"
            "- Check that this computer is connected to the internet.\n"
            "- An office network, VPN or firewall may be blocking huggingface.co.\n\n"
            "You can also untick the AI option and press Scan again: exact copies and "
            f"resized/rotated copies are still found without AI.\n\n(Details: {e})") from e
    os.replace(tmp, path)
    return path


def load_session(path):
    import onnxruntime as ort
    try:
        return ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    except Exception as e:
        try: os.remove(path)  # most likely a broken download; fetch it again next time
        except OSError: pass
        raise FriendlyError("The AI model file was damaged, so it was removed. Press Scan again "
                            f"to download it fresh.\n\n(Details: {e})") from e


def ai_embed(items, log, progress, cancel):
    todo = [i for i, it in enumerate(items) if EMB_KEY not in it]
    if todo:
        sess = load_session(ensure_model(log, progress))
        inp = sess.get_inputs()[0].name
        outs = [o.name for o in sess.get_outputs()]
        out_name = "image_embeds" if "image_embeds" in outs else outs[0]
        B = 16
        for s in range(0, len(todo), B):
            if cancel(): raise KeyboardInterrupt
            idx = todo[s:s + B]
            x = np.stack([clip_pre(items[i]["clipsrc"]) for i in idx])
            try:
                e = sess.run([out_name], {inp: x})[0]
            except Exception:
                B = 1
                e = np.concatenate([sess.run([out_name], {inp: x[k:k + 1]})[0] for k in range(len(idx))])
            if e.ndim == 3: e = e[:, 0, :]
            e = e / np.linalg.norm(e, axis=1, keepdims=True)
            for i, v in zip(idx, e):
                items[i][EMB_KEY] = v.astype(np.float32)
            progress(min(s + B, len(todo)), len(todo), f"AI checking {min(s + B, len(todo))}/{len(todo)}")
    return np.stack([it[EMB_KEY] for it in items])


# ---------------------------------------------------------------- grouping
class DSU:
    def __init__(s, n): s.p = list(range(n))
    def f(s, x):
        while s.p[x] != x:
            s.p[x] = s.p[s.p[x]]; x = s.p[x]
        return x
    def u(s, a, b):
        a, b = s.f(a), s.f(b)
        if a != b: s.p[b] = a


def build_groups(items, hash_lim, use_ai, sim_lim, maxgroup=20, log=print, progress=lambda *a: None,
                 cancel=lambda: False):
    n = len(items)
    if n < 2: return []
    dsu = DSU(n)
    link = [set() for _ in range(n)]
    by_md5 = {}
    for i, it in enumerate(items):
        by_md5.setdefault(it["md5"], []).append(i)
    for v in by_md5.values():
        for j in v[1:]:
            dsu.u(v[0], j); link[v[0]].add("exact"); link[j].add("exact")

    H0 = np.stack([it["hashes"][0] for it in items])
    HA = np.stack([it["hashes"] for it in items])
    for i in range(n):
        if i % 200 == 0:
            if cancel(): raise KeyboardInterrupt
            progress(i, n, f"Comparing {i}/{n}")
        d = (HA[i][:, None, :] != H0[None, :, :]).sum(-1).min(0)
        d[: i + 1] = 99
        for j in np.nonzero(d <= hash_lim)[0]:
            dsu.u(i, int(j)); link[i].add("copy"); link[int(j)].add("copy")

    E = None
    if use_ai:
        E = ai_embed(items, log, progress, cancel)
        for s in range(0, n, 512):
            S = E[s:s + 512] @ E.T
            for a in range(S.shape[0]):
                i = s + a
                row = S[a]; row[: i + 1] = -1
                for j in np.nonzero(row >= sim_lim)[0]:
                    dsu.u(i, int(j)); link[i].add("look"); link[int(j)].add("look")

    groups = {}
    for i in range(n):
        groups.setdefault(dsu.f(i), []).append(i)
    groups = [g for g in groups.values() if len(g) > 1]

    if use_ai and E is not None:
        final, stack = [], [(g, sim_lim) for g in groups]
        while stack:
            g, lim = stack.pop()
            if len(g) <= maxgroup or lim >= 0.99:
                final.append(g); continue
            lim2 = lim + 0.01
            sub = DSU(len(g))
            S = E[g] @ E[g].T
            Hg, H0g = HA[g], H0[g]
            for a in range(len(g)):
                for b in range(a + 1, len(g)):
                    if (S[a, b] >= lim2 or (Hg[a] != H0g[b]).sum(-1).min() <= hash_lim
                            or items[g[a]]["md5"] == items[g[b]]["md5"]):
                        sub.u(a, b)
            parts = {}
            for a in range(len(g)):
                parts.setdefault(sub.f(a), []).append(g[a])
            stack += [(p, lim2) for p in parts.values() if len(p) > 1]
        groups = final

    groups.sort(key=lambda g: min(items[i]["path"].lower() for i in g))
    out = []
    for g in groups:
        g.sort(key=lambda i: (-(items[i]["w"] * items[i]["h"]), -items[i]["size"], items[i]["path"].lower()))
        labels = set().union(*(link[i] for i in g))
        if labels == {"exact"}: why = "Exact copy"
        elif "look" in labels and not (labels & {"copy", "exact"}): why = "Looks the same (AI)"
        elif "look" in labels: why = "Copy + looks the same (AI)"
        else: why = "Same photo (resized/recompressed/rotated)"
        out.append(dict(members=g, why=why))
    return out


# ---------------------------------------------------------------- report / plan
def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024: return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def make_plan(root, items, groups):
    plan = []
    for gi, g in enumerate(groups, 1):
        gdir = os.path.join(root, GROUP_DIR, f"group_{gi:03d}")
        used = set()
        for rank, i in enumerate(g["members"]):
            src = items[i]["path"]
            name = os.path.basename(src)
            if rank == 0: name = "KEEP_" + name
            base, ext = os.path.splitext(name); k = 1
            while name.lower() in used:
                name = f"{base}_{k}{ext}"; k += 1
            used.add(name.lower())
            plan.append(dict(group=gi, src=src, dst=os.path.join(gdir, name), keep=rank == 0))
    return plan


def write_report(path, root, items, groups, errors, settings):
    extra = sum(items[i]["size"] for g in groups for i in g["members"][1:])
    nfiles = sum(len(g["members"]) for g in groups)
    p = [f"""<!doctype html><html><head><meta charset="utf-8"><title>Photo Grouper report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#f4f4f2;color:#222;margin:0;padding:24px}}
h1{{margin:0 0 4px;font-size:22px}} .sub{{color:#666;margin-bottom:20px;line-height:1.5}}
.g{{background:#fff;border-radius:10px;padding:14px 16px;margin:0 0 16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.gh{{display:flex;gap:12px;align-items:baseline;margin-bottom:10px}} .gh b{{font-size:16px}}
.tag{{font-size:12px;padding:2px 8px;border-radius:99px;background:#eef}}
.row{{display:flex;flex-wrap:wrap;gap:12px}}
.c{{width:220px;font-size:12px;color:#444}} .c img{{width:220px;height:220px;object-fit:contain;background:#eee;border-radius:6px;display:block}}
.k img{{outline:3px solid #2a9d5c}} .k .n{{color:#2a9d5c;font-weight:600}} .n{{word-break:break-all;margin-top:4px}}
</style></head><body>
<h1>{len(groups)} groups, {nfiles} photos</h1>
<div class="sub">Folder: {html.escape(root)} &middot; {html.escape(settings)}<br>
Extra copies take about {human(extra)} &middot; green border = best copy (gets KEEP_ prefix)
{f"&middot; {len(errors)} files could not be read (list at bottom)" if errors else ""}</div>"""]
    for gi, g in enumerate(groups, 1):
        p.append(f'<div class="g"><div class="gh"><b>group_{gi:03d}</b><span class="tag">{g["why"]}</span>'
                 f'<span>{len(g["members"])} photos</span></div><div class="row">')
        for rank, i in enumerate(g["members"]):
            it = items[i]
            p.append(f'<div class="c{" k" if rank == 0 else ""}"><img src="data:image/jpeg;base64,'
                     f'{base64.b64encode(it["thumb"]).decode()}"><div class="n">'
                     f'{html.escape(os.path.relpath(it["path"], root))}</div>'
                     f'<div>{it["w"]}&times;{it["h"]} &middot; {human(it["size"])}</div></div>')
        p.append("</div></div>")
    if errors:
        p.append('<div class="g"><b>Could not read</b><ul>')
        p += [f"<li>{html.escape(os.path.relpath(a, root))} - {html.escape(e)}</li>" for a, e in errors]
        p.append("</ul></div>")
    p.append("</body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(p))


# ---------------------------------------------------------------- top-level actions
def paths(root):
    work = os.path.join(root, WORK_DIR)
    return dict(work=work, cache=os.path.join(work, "cache.pkl"), plan=os.path.join(work, "plan.pkl"),
                log=os.path.join(work, "moved_log.csv"), report=os.path.join(work, "report.html"),
                recycled=os.path.join(work, "recycled_log.csv"))


def scan_files(root):
    files = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        files += [os.path.join(dp, f) for f in fns if os.path.splitext(f)[1].lower() in IMG_EXT]
    return files


def scan(root, preset="Normal", use_ai=True, log=print, progress=lambda *a: None, cancel=lambda: False,
         workers=None):
    """Returns dict(groups, photos, extra_bytes, errors, report)."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise FriendlyError(f"This folder does not exist (or the drive is not connected):\n\n{root}")
    P = paths(root)
    try:
        os.makedirs(P["work"], exist_ok=True)
        test = os.path.join(P["work"], ".write_test")
        with open(test, "w") as f: f.write("ok")
        os.remove(test)
    except OSError as e:
        raise FriendlyError("Photo Grouper cannot write inside this folder, so it cannot save its "
                            "report or move photos.\n\n"
                            "- If it is a CD, a read-only USB drive or a network/cloud folder, copy the "
                            "photos to a normal folder on your computer first.\n"
                            "- Otherwise check the folder's permissions.\n\n"
                            f"(Details: {e})") from e
    hash_lim, sim_lim = PRESETS[preset]
    files = scan_files(root)
    log(f"Found {len(files)} photos in {root}")
    if len(files) < 2:
        raise FriendlyError(f"Found {len(files)} photo(s) in this folder, so there is nothing to compare.\n\n"
                            "Supported types: JPG, PNG, WEBP, HEIC, BMP, TIFF, GIF, DNG and common camera RAW. "
                            "Subfolders are included.")
    cache = {}
    if os.path.exists(P["cache"]):
        try:
            with open(P["cache"], "rb") as f: cache = pickle.load(f)
        except Exception: cache = {}
    items, todo = [], []
    for p in files:
        st = os.stat(p); c = cache.get(p)
        if c and c.get("size") == st.st_size and c.get("mtime") == st.st_mtime:
            items.append(c)
        else:
            todo.append(p)
    errors = []
    if todo:
        log(f"Reading {len(todo)} photos" + (f" ({len(items)} already known)" if items else ""))
        workers = workers or max(1, (os.cpu_count() or 2) - 1)
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for k, r in enumerate(ex.map(process, todo, chunksize=4), 1):
                if cancel():
                    ex.shutdown(wait=False, cancel_futures=True); raise KeyboardInterrupt
                (errors.append((r["path"], r["err"])) if r["err"] else items.append(r))
                if k % 10 == 0 or k == len(todo):
                    el = time.time() - t0
                    progress(k, len(todo), f"Reading {k}/{len(todo)}  (~{el / k * (len(todo) - k):.0f}s left)")
    items.sort(key=lambda it: it["path"].lower())
    groups = build_groups(items, hash_lim, use_ai, sim_lim, log=log, progress=progress, cancel=cancel)
    with open(P["cache"], "wb") as f:
        pickle.dump({it["path"]: it for it in items}, f)
    plan = make_plan(root, items, groups)
    with open(P["plan"], "wb") as f:
        pickle.dump(dict(root=root, plan=plan), f)
    settings = f"{preset} mode, AI {'on' if use_ai else 'off'}"
    write_report(P["report"], root, items, groups, errors, settings)
    nphotos = sum(len(g["members"]) for g in groups)
    extra = sum(items[i]["size"] for g in groups for i in g["members"][1:])
    return dict(groups=len(groups), photos=nphotos, extra_bytes=extra, errors=errors, report=P["report"])


def has_plan(root):
    return os.path.exists(paths(os.path.abspath(root))["plan"])


def has_undo(root):
    return os.path.exists(paths(os.path.abspath(root))["log"])


def move(root, log=print):
    root = os.path.abspath(root); P = paths(root)
    if not os.path.exists(P["plan"]):
        raise FriendlyError("There is nothing to move yet. Press Scan first.")
    with open(P["plan"], "rb") as f:
        data = pickle.load(f)
    if os.path.normcase(data["root"]) != os.path.normcase(root):
        raise FriendlyError("This scan was made for a different folder (it may have been renamed or "
                            "moved). Press Scan again.")
    new = not os.path.exists(P["log"])
    moved = skipped = 0
    with open(P["log"], "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["group", "src", "dst"])
        if new: w.writeheader()
        for p in data["plan"]:
            if not os.path.exists(p["src"]) or os.path.exists(p["dst"]):
                skipped += 1; continue
            try:
                os.makedirs(os.path.dirname(p["dst"]), exist_ok=True)
                shutil.move(p["src"], p["dst"])
            except PermissionError:
                log(f"Skipped (file is open in another program or read-only): {p['src']}")
                skipped += 1; continue
            w.writerow(dict(group=p["group"], src=p["src"], dst=p["dst"])); f.flush(); moved += 1
    os.remove(P["plan"])
    return moved, skipped, os.path.join(root, GROUP_DIR)


def find_extras(root):
    """Photos in the group folders whose name does not start with KEEP_.
    Groups without any KEEP_ photo are left out, so a group is never emptied completely.
    Returns (list of paths, total bytes)."""
    gd = os.path.join(os.path.abspath(root), GROUP_DIR)
    extras = []
    if not os.path.isdir(gd):
        return extras, 0
    for g in sorted(os.listdir(gd)):
        gpath = os.path.join(gd, g)
        if not os.path.isdir(gpath): continue
        files = [f for f in os.listdir(gpath) if os.path.isfile(os.path.join(gpath, f))
                 and os.path.splitext(f)[1].lower() in IMG_EXT]
        if not any(f.upper().startswith("KEEP_") for f in files): continue
        extras += [os.path.join(gpath, f) for f in sorted(files) if not f.upper().startswith("KEEP_")]
    return extras, sum(os.path.getsize(p) for p in extras)


def has_extras(root):
    try:
        return bool(find_extras(root)[0])
    except OSError:
        return False


def _check_recycle_bin(root):
    """Removable and network drives have no Recycle Bin: Windows would delete permanently."""
    if os.name != "nt":
        raise FriendlyError("Sending to the Recycle Bin is only available on Windows.")
    import ctypes
    drive = os.path.splitdrive(os.path.abspath(root))[0]
    if drive.startswith("\\\\"):
        kind = 4
    else:
        kind = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
    if kind in (2, 4, 5):  # DRIVE_REMOVABLE, DRIVE_REMOTE, DRIVE_CDROM
        raise FriendlyError("This folder is on a USB, memory-card or network drive. Windows has no "
                            "Recycle Bin there, so the photos would be deleted for good.\n\n"
                            "Delete the extra copies by hand if you are sure, or copy the photos to "
                            "a folder on this computer first.")


def _to_recycle_bin(paths_):
    """Send files to the Windows Recycle Bin (never a permanent delete)."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 3
    flags = 0x40 | 0x10 | 0x04 | 0x4000  # ALLOWUNDO | NOCONFIRMATION | SILENT | WANTNUKEWARNING
    for s in range(0, len(paths_), 200):
        text = "\0".join(paths_[s:s + 200]) + "\0\0"  # list must end with two NULs
        buf = (ctypes.c_wchar * len(text))(*text)
        op = SHFILEOPSTRUCTW(None, FO_DELETE, ctypes.cast(buf, wintypes.LPCWSTR), None, flags, False, None, None)
        ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


def recycle_extras(root, log=print):
    """Send every photo in the group folders whose name does not start with KEEP_ to the Recycle Bin.
    Returns (number recycled, bytes freed, number left because they could not be removed)."""
    root = os.path.abspath(root); P = paths(root)
    extras, _ = find_extras(root)
    if not extras:
        raise FriendlyError("There are no extra copies to remove. Every group folder only has its "
                            "KEEP_ photo left (or Move has not been done yet).")
    _check_recycle_bin(root)
    sizes = {p: os.path.getsize(p) for p in extras}
    _to_recycle_bin(extras)
    gone = [p for p in extras if not os.path.exists(p)]
    left = len(extras) - len(gone)
    os.makedirs(P["work"], exist_ok=True)
    new = not os.path.exists(P["recycled"])
    with open(P["recycled"], "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new: w.writerow(["path", "time"])
        for p in gone:
            w.writerow([p, time.strftime("%Y-%m-%d %H:%M:%S")])
    for p in extras:
        if p in gone: log(f"Recycle Bin: {os.path.relpath(p, root)}")
        else: log(f"Could not remove (open in another program?): {os.path.relpath(p, root)}")
    return len(gone), sum(sizes[p] for p in gone), left


def recycled_count(root):
    p = paths(os.path.abspath(root))["recycled"]
    if not os.path.exists(p): return 0
    with open(p, newline="", encoding="utf-8") as f:
        return max(0, sum(1 for _ in f) - 1)


def undo(root):
    """Returns (photos put back, photos that are in the Recycle Bin and must be restored from there)."""
    root = os.path.abspath(root); P = paths(root)
    if not os.path.exists(P["log"]):
        raise FriendlyError("There is nothing to undo in this folder.")
    with open(P["log"], newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    back = 0
    for r in reversed(rows):
        if os.path.exists(r["dst"]) and not os.path.exists(r["src"]):
            try:
                os.makedirs(os.path.dirname(r["src"]), exist_ok=True)
                shutil.move(r["dst"], r["src"]); back += 1
            except PermissionError:
                pass
    stamp = int(time.time())
    os.replace(P["log"], P["log"] + f".undone_{stamp}")
    recycled = recycled_count(root)
    if os.path.exists(P["recycled"]):
        os.replace(P["recycled"], P["recycled"] + f".undone_{stamp}")
    gd = os.path.join(root, GROUP_DIR)
    if os.path.isdir(gd):
        for dp, dns, fns in os.walk(gd, topdown=False):
            if not os.listdir(dp): os.rmdir(dp)
    return back, recycled
