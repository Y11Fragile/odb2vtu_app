# -*- coding: utf-8 -*-
"""无人工点击的 GUI 端到端自检。

用法:
    python _gui_selftest.py <odb> <outdir> <vars逗号分隔> <帧模式 last|all|自定义串> <binary|ascii>
"""
import os
import sys
import time

import tkinter as tk
from tkinter import messagebox

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
sys.path.insert(0, APP_DIR)
import odb2vtu_gui as G

G.CONFIG_PATH = os.path.join(os.path.dirname(APP_DIR), "_selftest_config.json")

ODB = sys.argv[1]
OUT = sys.argv[2]
WANT = [v.strip() for v in sys.argv[3].split(",") if v.strip()]
FRAMES = sys.argv[4]
MODE = sys.argv[5] if len(sys.argv) > 5 else "binary"

calls = []
for name in ("showinfo", "showwarning", "showerror"):
    setattr(messagebox, name,
            (lambda n: (lambda *a, **k: calls.append((n, a))))(name))

root = tk.Tk()
root.withdraw()
app = G.App(root)
app.odb_var.set(ODB)
app.outdir_var.set(OUT)
app.prefix_var.set("gui")
app.binary_var.set(MODE == "binary")

app._start_scan()
deadline = time.time() + 180
while time.time() < deadline and not app.scan_data and app.scanning:
    root.update()
    time.sleep(0.05)
if not app.scan_data:
    print("SCAN FAILED"); print(app.log.get("1.0", "end")); root.destroy(); sys.exit(1)
print("scan ok: %d vars / %d steps / %d instances"
      % (len(app.var_rows), len(app.scan_data["steps"]), len(app.scan_data["instances"])))
for row in app.var_rows:
    print("   %-22s %-6s %-24s %s" % (row[0], row[1], ",".join(row[2]) or "-",
                                      ",".join(row[3])))

app._check_all_vars(False)
for name in WANT:
    app.var_tree.set_checked("var:" + name, True)
app._update_var_count()
print("selected:", app._selected_vars())

if FRAMES in ("last", "all"):
    app.frame_mode.set(FRAMES)
else:
    app.frame_mode.set("custom")
    app.frame_entry.configure(state="normal")
    app.frame_entry.delete(0, "end")
    app.frame_entry.insert(0, FRAMES)
print("frame indices:", app._frame_indices(app.frame_mode.get(), FRAMES,
                                           int(app.scan_data["steps"][0]["frames"])))

app._start_convert()
deadline = time.time() + 300
while time.time() < deadline and app.busy:
    root.update()
    time.sleep(0.05)
print("busy after:", app.busy, "dialogs:", [c[0] for c in calls])
if os.path.isdir(OUT):
    print("files:", sorted(os.listdir(OUT)))
print("log tail:")
print(app.log.get("1.0", "end").strip()[-900:])
root.destroy()