"""Photo Grouper - desktop app (tkinter). Build to .exe with build_exe.bat."""
import multiprocessing
import os, queue, sys, threading, traceback, webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import grouper_core as core
import converter

APP = "Photo Grouper"

HELP_TEXT = """FIND DUPLICATES
1. Browse: choose the folder with your photos (subfolders are included).
2. Scan: nothing is moved yet. A report opens in your browser with every group side by side.
   The green-bordered photo is the best copy (largest size) and will get a KEEP_ prefix.
3. Move into group folders: each group goes into <your folder>\\_similar_groups\\group_001, group_002 ...
4. Look through the group folders. Then either delete the extra copies yourself, or press
   "Recycle extra copies": in every group folder, each photo whose name does not start with KEEP_
   goes to the Windows Recycle Bin. To keep one of them too, rename it so it starts with KEEP_ first.
   A group with no KEEP_ photo is left alone. Nothing is ever deleted permanently.
5. Undo: puts every moved photo back where it was, with its original name. Photos that were sent to
   the Recycle Bin can be restored from the Recycle Bin.

MATCHING SETTINGS
- Normal: good for most folders.
- Strict: only very sure matches. Use it if groups contain photos that are not really the same.
- Loose: catches more (e.g. burst shots). Check the report carefully before moving.
- AI option: also catches photos that look the same (crops, filters, burst shots, WhatsApp copies).
  Untick it to find only exact copies and resized/rotated copies. That is faster and needs no internet.
Scanning the same folder again is fast, because results are remembered.

HEIC TO JPG
iPhones save photos as HEIC, which many Windows programs and websites cannot open.
1. Choose a folder (subfolders are included) or pick the HEIC files.
2. Choose where the JPGs go: next to the HEIC files, or into one folder (subfolders are kept).
3. Press Convert.
- Best quality: the JPG looks the same as the HEIC. Full resolution and full colour detail are kept.
  JPG files are often bigger than HEIC, because JPG is an older, less efficient format.
- Maximum quality: even closer to the original, but bigger files. Smaller files: still very good.
- The colours (the colour profile, e.g. iPhone Display P3), date taken, camera, location and the
  file dates are copied.
- The HEIC files are never changed or deleted, and an existing JPG is never overwritten.
  If you convert the same folder again, photos that already have a JPG are skipped.

IF SOMETHING GOES WRONG
- "Windows protected your PC" when opening the app: click "More info" and then "Run anyway".
  This appears for new apps that are not signed by a big company.
- Antivirus blocks or deletes PhotoGrouper.exe: this is a false alarm that is common for small
  free tools. Add it as an exception in your antivirus, or download it again from the official page.
- The app takes a few seconds to open: normal. It unpacks itself each time it starts.
- "Could not download the AI model": check your internet. Office networks or VPNs sometimes block
  huggingface.co. You can untick AI and scan without it.
- "Cannot write inside this folder": the folder is read-only (CD, protected USB drive, some network or
  cloud folders). Copy the photos to a normal folder on your computer first.
- Recycle extra copies is refused on a USB, memory-card or network drive: Windows has no Recycle Bin
  there, so the photos would be gone for good. Delete them by hand if you are sure.
- Some files "could not be read": they are listed at the bottom of the report and are left where they are.
  Usually damaged files, or RAW/DNG formats that are not supported.
- Some photos were "skipped" while moving: they were open in another program (a photo viewer or
  editor). Close it and press Scan, then Move again.
- Scan is slow: the first scan of a big folder can take several minutes (the AI part takes longest).
  You can press Stop at any time. Already-read photos are remembered for next time.
- Photos in OneDrive / Google Drive folders that are "online only" get downloaded during the scan.

Where things are saved
- Report and scan data: <your folder>\\_photo_grouper\\
- AI model: %LOCALAPPDATA%\\PhotoGrouper\\
To remove Photo Grouper completely, delete PhotoGrouper.exe and those two folders.
"""


def friendly(e):
    """Turn any exception into (title, message) a normal user can understand."""
    if isinstance(e, core.FriendlyError):
        return "Photo Grouper", str(e)
    if isinstance(e, MemoryError):
        return "Not enough memory", ("The computer ran out of memory. Close other programs, or use a "
                                     "smaller folder (for example one year at a time).")
    if isinstance(e, PermissionError):
        return "Permission problem", ("Windows did not allow access to a file or folder. Close any program "
                                      "that has these photos open, and make sure the folder is not "
                                      f"read-only.\n\n(Details: {e})")
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "Disk is full", "The disk is full. Free some space and try again."
    if isinstance(e, (FileNotFoundError, NotADirectoryError)):
        return "File not found", ("A file or folder disappeared while working (was a drive unplugged?). "
                                  f"Try again.\n\n(Details: {e})")
    return "Unexpected error", ("Something unexpected went wrong. Please try again. If it keeps happening, "
                                "copy the text from the log box at the bottom and report it.\n\n"
                                f"({type(e).__name__}: {e})")


MODES = {
    "Normal (recommended)": "Normal",
    "Strict - only very sure matches": "Strict",
    "Loose - catch more, check the report": "Loose",
}
QUALITIES = {
    "Best quality (recommended)": "Best",
    "Maximum quality - bigger files": "Maximum",
    "Smaller files - still very good": "Smaller",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP} {core.__version__}")
        self.geometry("780x660")
        self.minsize(700, 600)
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            for ico in (os.path.join(getattr(sys, "_MEIPASS", here), "icon.ico"),
                        os.path.join(here, "..", "assets", "icon.ico")):
                if os.path.exists(ico):
                    self.iconbitmap(ico); break
        except Exception:
            pass
        self.q = queue.Queue()
        self.busy = False
        self.cancel_flag = False
        self.last_report = None
        self.conv_files = None  # list of picked HEIC files, or None when a folder is used

        s = ttk.Style(self)
        if "vista" in s.theme_names(): s.theme_use("vista")
        s.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        s.configure("Sub.TLabel", foreground="#555")
        s.configure("Big.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 8))
        s.configure("TNotebook.Tab", padding=(14, 5))

        pad = dict(padx=16)
        ttk.Label(self, text=APP, style="Title.TLabel").pack(anchor="w", pady=(14, 0), **pad)
        ttk.Label(self, text="Finds duplicate and similar photos, and turns iPhone HEIC photos into JPG. "
                             "Nothing is deleted permanently.",
                  style="Sub.TLabel", wraplength=740).pack(anchor="w", pady=(2, 10), **pad)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="x", **pad)
        self.tabs.add(self._dupes_tab(self.tabs), text="Find duplicates")
        self.tabs.add(self._heic_tab(self.tabs), text="HEIC to JPG")

        self.pb = ttk.Progressbar(self, mode="determinate")
        self.pb.pack(fill="x", pady=(12, 2), **pad)
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        self.status = tk.StringVar(value="Choose a folder, then press Scan.")
        ttk.Label(bottom, textvariable=self.status).pack(side="left")
        self.b_help = ttk.Button(bottom, text="Help", command=self.show_help)
        self.b_cancel = ttk.Button(bottom, text="Stop", command=self.do_cancel)
        self.b_help.pack(side="right")
        self.b_cancel.pack(side="right", padx=(0, 8))

        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, pady=(8, 14), **pad)
        self.logbox = tk.Text(lf, height=8, wrap="word", font=("Consolas", 9), bg="#fafafa", relief="flat",
                              borderwidth=1, highlightthickness=1, highlightbackground="#ddd")
        sb = ttk.Scrollbar(lf, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set, state="disabled")
        self.logbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.refresh()
        self.after(100, self.poll)

    # ---------------------------------------------------------- tabs
    def _dupes_tab(self, parent):
        t = ttk.Frame(parent, padding=(10, 10, 10, 12))
        f1 = ttk.LabelFrame(t, text=" 1. Choose a folder ")
        f1.pack(fill="x")
        self.folder = tk.StringVar()
        self.folder.trace_add("write", lambda *a: self.refresh())
        ttk.Entry(f1, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=(10, 6), pady=10)
        ttk.Button(f1, text="Browse...", command=self.browse).pack(side="left", padx=(0, 10))

        f2 = ttk.LabelFrame(t, text=" 2. Settings ")
        f2.pack(fill="x", pady=10)
        ttk.Label(f2, text="Matching:").grid(row=0, column=0, sticky="w", padx=(10, 8), pady=(10, 4))
        self.mode = tk.StringVar(value=list(MODES)[0])
        ttk.Combobox(f2, textvariable=self.mode, values=list(MODES), state="readonly", width=40
                     ).grid(row=0, column=1, sticky="w", pady=(10, 4))
        self.ai = tk.BooleanVar(value=True)
        ttk.Checkbutton(f2, variable=self.ai,
                        text="Use AI to also catch photos that look the same (burst shots, crops, filters).\n"
                             "The first time, it downloads an ~89 MB AI model."
                        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 10))

        f3 = ttk.Frame(t)
        f3.pack(fill="x")
        self.b_scan = ttk.Button(f3, text="Scan", style="Big.TButton", command=self.do_scan)
        self.b_report = ttk.Button(f3, text="Open report", command=self.open_report)
        self.b_move = ttk.Button(f3, text="Move into group folders", style="Big.TButton", command=self.do_move)
        self.b_recycle = ttk.Button(f3, text="Recycle extra copies", command=self.do_recycle)
        self.b_undo = ttk.Button(f3, text="Undo", command=self.do_undo)
        for b in (self.b_scan, self.b_report, self.b_move, self.b_recycle, self.b_undo):
            b.pack(side="left", padx=(0, 8))
        return t

    def _heic_tab(self, parent):
        t = ttk.Frame(parent, padding=(10, 10, 10, 12))
        f1 = ttk.LabelFrame(t, text=" 1. HEIC photos ")
        f1.pack(fill="x")
        self.conv_src = tk.StringVar()
        self.conv_src.trace_add("write", lambda *a: self._conv_src_typed())
        ttk.Entry(f1, textvariable=self.conv_src).pack(side="left", fill="x", expand=True, padx=(10, 6), pady=10)
        ttk.Button(f1, text="Folder...", command=self.pick_heic_folder).pack(side="left", padx=(0, 6))
        ttk.Button(f1, text="Files...", command=self.pick_heic_files).pack(side="left", padx=(0, 10))

        f2 = ttk.LabelFrame(t, text=" 2. Save the JPGs ")
        f2.pack(fill="x", pady=10)
        self.conv_where = tk.StringVar(value="next")
        self.conv_out = tk.StringVar()
        self.conv_where.trace_add("write", lambda *a: self.refresh())
        self.conv_out.trace_add("write", lambda *a: self.refresh())
        ttk.Radiobutton(f2, text="Next to the HEIC files", variable=self.conv_where, value="next"
                        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 2))
        ttk.Radiobutton(f2, text="In this folder:", variable=self.conv_where, value="folder"
                        ).grid(row=1, column=0, sticky="w", padx=(10, 6), pady=(2, 8))
        self.e_out = ttk.Entry(f2, textvariable=self.conv_out)
        self.e_out.grid(row=1, column=1, sticky="ew", pady=(2, 8))
        self.b_out = ttk.Button(f2, text="Browse...", command=self.pick_out_folder)
        self.b_out.grid(row=1, column=2, padx=(6, 10), pady=(2, 8))
        f2.columnconfigure(1, weight=1)
        ttk.Label(f2, text="Quality:").grid(row=2, column=0, sticky="w", padx=(10, 6), pady=(0, 6))
        self.conv_q = tk.StringVar(value=list(QUALITIES)[0])
        ttk.Combobox(f2, textvariable=self.conv_q, values=list(QUALITIES), state="readonly", width=34
                     ).grid(row=2, column=1, sticky="w", pady=(0, 6))
        ttk.Label(f2, style="Sub.TLabel", wraplength=700,
                  text="Full resolution, colours, date taken, camera and location are kept. "
                       "The HEIC files are not changed or deleted."
                  ).grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))

        f3 = ttk.Frame(t)
        f3.pack(fill="x")
        self.b_convert = ttk.Button(f3, text="Convert to JPG", style="Big.TButton", command=self.do_convert)
        self.b_convert.pack(side="left")
        return t

    # ---------------------------------------------------------- helpers
    def log(self, msg): self.q.put(("log", msg))
    def progress(self, a, b, text): self.q.put(("prog", (a, b, text)))

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", val + "\n"); self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "prog":
                    a, b, text = val
                    self.pb.configure(maximum=max(b, 1), value=a); self.status.set(text)
                elif kind == "done":
                    self.busy = False; self.pb.configure(value=0); self.refresh()
                    if val: val()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def refresh(self):
        if not hasattr(self, "b_convert"):
            return  # still building the window
        root = self.folder.get().strip()
        ok = bool(root) and os.path.isdir(root)
        idle = not self.busy
        st = lambda b, on: b.state(["!disabled"] if on else ["disabled"])
        st(self.b_scan, ok and idle)
        st(self.b_report, ok and idle and os.path.exists(core.paths(os.path.abspath(root))["report"]) if ok else False)
        st(self.b_move, ok and idle and core.has_plan(root) if ok else False)
        st(self.b_recycle, ok and idle and core.has_extras(root) if ok else False)
        st(self.b_undo, ok and idle and core.has_undo(root) if ok else False)
        st(self.b_cancel, self.busy)
        out_ok = self.conv_where.get() == "next" or bool(self.conv_out.get().strip())
        st(self.e_out, self.conv_where.get() == "folder")
        st(self.b_out, self.conv_where.get() == "folder")
        st(self.b_convert, idle and bool(self.heic_sources()) and out_ok)

    def browse(self):
        d = filedialog.askdirectory(title="Choose the folder with your photos")
        if d: self.folder.set(os.path.normpath(d))

    def run(self, fn):
        self.busy = True; self.cancel_flag = False; self.refresh()

        def worker():
            after = None
            try:
                after = fn()
            except KeyboardInterrupt:
                self.log("Stopped."); self.q.put(("prog", (0, 1, "Stopped.")))
            except Exception as e:
                title, msg = friendly(e)
                self.log("ERROR: " + msg.split("\n")[0])
                if not isinstance(e, core.FriendlyError):
                    self.log(traceback.format_exc())
                self.q.put(("prog", (0, 1, title)))
                after = lambda: messagebox.showerror(title, msg)
            self.q.put(("done", after))
        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------- duplicates
    def do_scan(self):
        root = os.path.abspath(self.folder.get().strip())
        mode, ai = MODES[self.mode.get()], self.ai.get()

        def job():
            r = core.scan(root, preset=mode, use_ai=ai, log=self.log, progress=self.progress,
                          cancel=lambda: self.cancel_flag)
            self.last_report = r["report"]
            text = (f"Done: {r['groups']} group{'s' if r['groups'] != 1 else ''} ({r['photos']} photos). "
                    f"Extra copies use about {core.human(r['extra_bytes'])}. Nothing moved yet.")
            self.log(text)
            if r["errors"]: self.log(f"{len(r['errors'])} files could not be read (listed in the report).")
            self.q.put(("prog", (0, 1, text)))
            if r["groups"]:
                return lambda: (webbrowser.open("file:///" + r["report"].replace("\\", "/")),
                                messagebox.showinfo(APP, text + "\n\nThe report is open in your browser. "
                                                    "If it looks right, press 'Move into group folders'."))
            return lambda: messagebox.showinfo(APP, "No duplicate or similar photos found.")
        self.run(job)

    def open_report(self):
        rep = core.paths(os.path.abspath(self.folder.get().strip()))["report"]
        if os.path.exists(rep): webbrowser.open("file:///" + rep.replace("\\", "/"))

    def do_move(self):
        root = os.path.abspath(self.folder.get().strip())
        if not messagebox.askyesno(APP, "Move every group into its own folder inside\n\n"
                                        f"{os.path.join(root, core.GROUP_DIR)}\n\n"
                                        "The best copy in each group gets a KEEP_ prefix. "
                                        "You can undo this later. Continue?"):
            return

        def job():
            moved, skipped, gd = core.move(root, log=self.log)
            text = f"Moved {moved} photos into group folders." + (f" ({skipped} skipped)" if skipped else "")
            self.log(text); self.q.put(("prog", (0, 1, text)))
            return lambda: (os.startfile(gd) if hasattr(os, "startfile") else None)
        self.run(job)

    def do_recycle(self):
        root = os.path.abspath(self.folder.get().strip())
        try:
            extras, size = core.find_extras(root)
        except OSError as e:
            title, msg = friendly(e); messagebox.showerror(title, msg); return
        if not extras:
            self.refresh(); return
        if not messagebox.askyesno(APP, f"Send {len(extras)} extra cop{'y' if len(extras) == 1 else 'ies'} "
                                        f"({core.human(size)}) to the Recycle Bin?\n\n"
                                        "In every group folder, each photo whose name does not start with "
                                        "KEEP_ goes to the Recycle Bin. To keep one of them too, rename it "
                                        "so it starts with KEEP_ first.\n\n"
                                        "Groups without a KEEP_ photo are left alone. You can get the photos "
                                        "back from the Recycle Bin.", icon="warning"):
            return

        def job():
            n, freed, left = core.recycle_extras(root, log=self.log)
            text = f"Sent {n} extra copies to the Recycle Bin ({core.human(freed)})."
            if left: text += f" {left} could not be removed (open in another program?)."
            self.log(text); self.q.put(("prog", (0, 1, text)))
            return lambda: messagebox.showinfo(APP, text)
        self.run(job)

    def do_undo(self):
        root = os.path.abspath(self.folder.get().strip())
        if not messagebox.askyesno(APP, "Put every moved photo back where it was (with its original name)?"):
            return

        def job():
            n, recycled = core.undo(root)
            text = f"Restored {n} photos."
            if recycled:
                text += (f" {recycled} extra copies were sent to the Recycle Bin earlier. "
                         "To get them back, open the Recycle Bin and restore them.")
            self.log(text); self.q.put(("prog", (0, 1, text)))
            if recycled:
                return lambda: messagebox.showinfo(APP, text)
        self.run(job)

    # ---------------------------------------------------------- HEIC to JPG
    def heic_sources(self):
        if self.conv_files:
            return self.conv_files
        p = self.conv_src.get().strip()
        return [p] if p and os.path.isdir(p) else []

    def _conv_src_typed(self):
        if self.conv_files and self.conv_src.get() != self._files_label:
            self.conv_files = None  # user typed over the file list
        self.refresh()

    def pick_heic_folder(self):
        d = filedialog.askdirectory(title="Choose the folder with your HEIC photos")
        if d:
            self.conv_files = None
            self.conv_src.set(os.path.normpath(d))

    def pick_heic_files(self):
        fs = filedialog.askopenfilenames(title="Choose HEIC photos",
                                         filetypes=[("HEIC photos", "*.heic *.heif *.hif"), ("All files", "*.*")])
        if fs:
            fs = [os.path.normpath(f) for f in fs]
            self._files_label = (os.path.basename(fs[0]) if len(fs) == 1
                                 else f"{len(fs)} files selected ({os.path.basename(fs[0])}, ...)")
            self.conv_files = fs
            self.conv_src.set(self._files_label)

    def pick_out_folder(self):
        d = filedialog.askdirectory(title="Choose where to save the JPGs")
        if d: self.conv_out.set(os.path.normpath(d))

    def do_convert(self):
        sources = list(self.heic_sources())
        out_dir = os.path.abspath(self.conv_out.get().strip()) if self.conv_where.get() == "folder" else None
        preset = QUALITIES[self.conv_q.get()]

        def job():
            r = converter.convert(sources, out_dir=out_dir, preset=preset, log=self.log, progress=self.progress,
                                  cancel=lambda: self.cancel_flag)
            text = f"Converted {r['ok']} photo{'s' if r['ok'] != 1 else ''} to JPG."
            if r["ok"]:
                text += f" ({core.human(r['in_bytes'])} HEIC -> {core.human(r['out_bytes'])} JPG)"
            if r["exists"]: text += f" {r['exists']} skipped because the JPG already exists."
            if r["errors"]: text += f" {len(r['errors'])} could not be converted (see the log)."
            self.log(text); self.q.put(("prog", (0, 1, text)))
            folder = out_dir or (sources[0] if os.path.isdir(sources[0]) else os.path.dirname(sources[0]))
            return lambda: (messagebox.showinfo(APP, text),
                            os.startfile(folder) if r["ok"] and hasattr(os, "startfile") else None)
        self.run(job)

    # ---------------------------------------------------------- misc
    def show_help(self):
        w = tk.Toplevel(self)
        w.title(f"{APP} - Help")
        w.geometry("720x600")
        t = tk.Text(w, wrap="word", font=("Segoe UI", 10), padx=14, pady=12, relief="flat")
        sb = ttk.Scrollbar(w, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.insert("1.0", HELP_TEXT)
        for head in ("FIND DUPLICATES", "MATCHING SETTINGS", "HEIC TO JPG", "IF SOMETHING GOES WRONG",
                     "Where things are saved"):
            i = t.search(head, "1.0", "end")
            if i: t.tag_add("h", i, f"{i} lineend")
        t.tag_configure("h", font=("Segoe UI", 11, "bold"), spacing1=6, spacing3=4)
        t.configure(state="disabled")
        sb.pack(side="right", fill="y"); t.pack(fill="both", expand=True)
        ttk.Button(w, text="Close", command=w.destroy).pack(pady=8)

    def do_cancel(self):
        self.cancel_flag = True
        self.status.set("Stopping...")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    App().mainloop()
