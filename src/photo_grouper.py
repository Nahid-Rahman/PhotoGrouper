"""Photo Grouper - desktop app (tkinter). Build to .exe with build_exe.bat."""
import multiprocessing
import os, queue, sys, threading, traceback, webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import grouper_core as core

APP = "Photo Grouper"

HELP_TEXT = """HOW TO USE
1. Browse: choose the folder with your photos (subfolders are included).
2. Scan: nothing is moved yet. A report opens in your browser with every group side by side.
   The green-bordered photo is the best copy (largest size) and will get a KEEP_ prefix.
3. Move into group folders: each group goes into <your folder>\\_similar_groups\\group_001, group_002 ...
   Look through them and delete the extra copies yourself. Photo Grouper never deletes anything.
4. Undo: puts every moved photo back where it was, with its original name.

MATCHING SETTINGS
- Normal: good for most folders.
- Strict: only very sure matches. Use it if groups contain photos that are not really the same.
- Loose: catches more (e.g. burst shots). Check the report carefully before moving.
- AI option: also catches photos that look the same (crops, filters, burst shots, WhatsApp copies).
  Untick it to find only exact copies and resized/rotated copies. That is faster and needs no internet.
Scanning the same folder again is fast, because results are remembered.

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
        return "Not enough memory", ("The computer ran out of memory. Close other programs, or scan a "
                                     "smaller folder (for example one year at a time).")
    if isinstance(e, PermissionError):
        return "Permission problem", ("Windows did not allow access to a file or folder. Close any program "
                                      "that has these photos open, and make sure the folder is not "
                                      f"read-only.\n\n(Details: {e})")
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "Disk is full", "The disk is full. Free some space and try again."
    if isinstance(e, (FileNotFoundError, NotADirectoryError)):
        return "File not found", ("A file or folder disappeared while working (was a drive unplugged?). "
                                  f"Press Scan again.\n\n(Details: {e})")
    return "Unexpected error", ("Something unexpected went wrong. Please try again. If it keeps happening, "
                                "copy the text from the log box at the bottom and report it.\n\n"
                                f"({type(e).__name__}: {e})")
MODES = {
    "Normal (recommended)": "Normal",
    "Strict - only very sure matches": "Strict",
    "Loose - catch more, check the report": "Loose",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP} {core.__version__}")
        self.geometry("720x560")
        self.minsize(620, 480)
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

        s = ttk.Style(self)
        if "vista" in s.theme_names(): s.theme_use("vista")
        s.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        s.configure("Sub.TLabel", foreground="#555")
        s.configure("Big.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 8))

        pad = dict(padx=16)
        ttk.Label(self, text=APP, style="Title.TLabel").pack(anchor="w", pady=(14, 0), **pad)
        ttk.Label(self, text="Finds duplicate and similar photos and puts each set into its own folder. "
                             "Nothing is deleted, and every move can be undone.",
                  style="Sub.TLabel", wraplength=680).pack(anchor="w", pady=(2, 12), **pad)

        f1 = ttk.LabelFrame(self, text=" 1. Choose a folder ")
        f1.pack(fill="x", **pad)
        self.folder = tk.StringVar()
        self.folder.trace_add("write", lambda *a: self.refresh())
        ttk.Entry(f1, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=(10, 6), pady=10)
        ttk.Button(f1, text="Browse...", command=self.browse).pack(side="left", padx=(0, 10))

        f2 = ttk.LabelFrame(self, text=" 2. Settings ")
        f2.pack(fill="x", pady=10, **pad)
        ttk.Label(f2, text="Matching:").grid(row=0, column=0, sticky="w", padx=(10, 8), pady=(10, 4))
        self.mode = tk.StringVar(value=list(MODES)[0])
        ttk.Combobox(f2, textvariable=self.mode, values=list(MODES), state="readonly", width=40
                     ).grid(row=0, column=1, sticky="w", pady=(10, 4))
        self.ai = tk.BooleanVar(value=True)
        ttk.Checkbutton(f2, variable=self.ai,
                        text="Use AI to also catch photos that look the same (burst shots, crops, filters).\n"
                             "The first time, it downloads an ~89 MB AI model."
                        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 10))

        f3 = ttk.Frame(self)
        f3.pack(fill="x", **pad)
        self.b_scan = ttk.Button(f3, text="Scan", style="Big.TButton", command=self.do_scan)
        self.b_report = ttk.Button(f3, text="Open report", command=self.open_report)
        self.b_move = ttk.Button(f3, text="Move into group folders", style="Big.TButton", command=self.do_move)
        self.b_undo = ttk.Button(f3, text="Undo", command=self.do_undo)
        self.b_cancel = ttk.Button(f3, text="Stop", command=self.do_cancel)
        self.b_help = ttk.Button(f3, text="Help", command=self.show_help)
        for b in (self.b_scan, self.b_report, self.b_move, self.b_undo):
            b.pack(side="left", padx=(0, 8))
        self.b_help.pack(side="right")
        self.b_cancel.pack(side="right", padx=(0, 8))

        self.pb = ttk.Progressbar(self, mode="determinate")
        self.pb.pack(fill="x", pady=(12, 2), **pad)
        self.status = tk.StringVar(value="Choose a folder, then press Scan.")
        ttk.Label(self, textvariable=self.status).pack(anchor="w", **pad)

        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, pady=(8, 14), **pad)
        self.logbox = tk.Text(lf, height=10, wrap="word", font=("Consolas", 9), bg="#fafafa", relief="flat",
                              borderwidth=1, highlightthickness=1, highlightbackground="#ddd")
        sb = ttk.Scrollbar(lf, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set, state="disabled")
        self.logbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.refresh()
        self.after(100, self.poll)

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
        root = self.folder.get().strip()
        ok = bool(root) and os.path.isdir(root)
        idle = not self.busy
        st = lambda b, on: b.state(["!disabled"] if on else ["disabled"])
        st(self.b_scan, ok and idle)
        st(self.b_report, ok and idle and os.path.exists(core.paths(os.path.abspath(root))["report"]) if ok else False)
        st(self.b_move, ok and idle and core.has_plan(root) if ok else False)
        st(self.b_undo, ok and idle and core.has_undo(root) if ok else False)
        st(self.b_cancel, self.busy)

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

    # ---------------------------------------------------------- actions
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

    def do_undo(self):
        root = os.path.abspath(self.folder.get().strip())
        if not messagebox.askyesno(APP, "Put every moved photo back where it was (with its original name)?"):
            return

        def job():
            n = core.undo(root)
            text = f"Restored {n} photos."
            self.log(text); self.q.put(("prog", (0, 1, text)))
        self.run(job)

    def show_help(self):
        w = tk.Toplevel(self)
        w.title(f"{APP} - Help")
        w.geometry("700x560")
        t = tk.Text(w, wrap="word", font=("Segoe UI", 10), padx=14, pady=12, relief="flat")
        sb = ttk.Scrollbar(w, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.insert("1.0", HELP_TEXT)
        for head in ("HOW TO USE", "MATCHING SETTINGS", "IF SOMETHING GOES WRONG", "Where things are saved"):
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
