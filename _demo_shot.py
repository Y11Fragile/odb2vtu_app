# -*- coding: utf-8 -*-
"""打开 GUI 并预载一个示例 ODB（用于截图或快速试用）。

    python _demo_shot.py [odb] [输出目录]
"""
import os
import sys
import time

import tkinter as tk

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))
import odb2vtu_gui as G

ODB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "sample", "cube.odb")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "sample", "vtu")

root = tk.Tk()
app = G.App(root)
app.odb_var.set(ODB)
app.outdir_var.set(OUT)
app.prefix_var.set("cube")
app._start_scan()
deadline = time.time() + 120
while time.time() < deadline and not app.scan_data and app.scanning:
    root.update()
    time.sleep(0.05)
if app.scan_data:
    app._check_all_vars(False)
    for name in ("S", "U", "RF"):
        if any(r[0] == name for r in app.var_rows):
            app.var_tree.set_checked("var:" + name, True)
    app._update_var_count()
    app.deform_var.set(True)
    app._log("示例 ODB 已载入：%s" % os.path.basename(ODB), "hint")
root.mainloop()