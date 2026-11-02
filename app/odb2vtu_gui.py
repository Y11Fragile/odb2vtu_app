# -*- coding: utf-8 -*-
"""Abaqus ODB -> VTU 转换器（图形界面）

选择 .odb 文件 -> 扫描出分析步与场变量 -> 勾选需要的变量 -> 导出 ParaView 可读的
.vtu（可选 .pvd 时间序列）。

界面本身用普通 Python 3 + tkinter 运行；真正的 ODB 读取交给 Abaqus 自带的
Python 解释器（SMAPython.exe）里的 worker/odb2vtu_worker.py，因为只有它带
odbAccess 模块。两者通过子进程 + 控制台标记通信。
"""

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Abaqus ODB \u2192 VTU 转换器"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR)
WORKER = os.path.join(ROOT_DIR, "worker", "odb2vtu_worker.py")
if not os.path.isfile(WORKER):
    WORKER = os.path.join(APP_DIR, "worker", "odb2vtu_worker.py")
CONFIG_PATH = os.path.join(ROOT_DIR, "odb2vtu_config.json")

CHECK_ON = "\u2611"
CHECK_OFF = "\u2610"


# ---------------------------------------------------------------------------
# 配置与工具链定位
# ---------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_config(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _drive_roots():
    roots = []
    for letter in "CDEFGH":
        root = letter + ":\\"
        if os.path.isdir(root):
            roots.append(root)
    return roots


def find_abaqus_python():
    """返回运行 worker 的命令前缀（可执行文件，可能还跟一个 'python' 参数）。"""
    cfg = load_config()
    override = cfg.get("abaqus_python")
    if override and os.path.isfile(override):
        return [override]

    import glob as _glob
    from shutil import which

    bases = []
    for name in ("abaqus", "abaqus.bat"):
        found = which(name)
        if found:
            bases.append(os.path.dirname(found))
            bases.append(os.path.dirname(os.path.dirname(found)))
            break

    for root in _drive_roots():
        try:
            entries = os.listdir(root)
        except Exception:
            continue
        for entry in entries:
            low = entry.lower()
            if "abaqus" in low or "simulia" in low or "estproducts" in low:
                bases.append(os.path.join(root, entry))

    patterns = ("product/win_b64/code/bin/SMAPython.exe",
                "win_b64/code/bin/SMAPython.exe",
                "*/win_b64/code/bin/SMAPython.exe",
                "*/product/win_b64/code/bin/SMAPython.exe")
    seen = set()
    for base in bases:
        if not base or base.lower() in seen:
            continue
        seen.add(base.lower())
        for pattern in patterns:
            for hit in _glob.glob(os.path.join(base, pattern)):
                if hit.lower().endswith("smapython.exe"):
                    return [hit]
    # 退路：命令行 abaqus python
    found = which("abaqus") or which("abaqus.bat")
    if found:
        return [found, "python"]
    return None


def find_paraview():
    cfg = load_config()
    override = cfg.get("paraview_exe")
    if override and os.path.isfile(override):
        return override
    import glob as _glob
    candidates = []
    for root in _drive_roots():
        for sub in ("Program Files", "Program Files (x86)"):
            candidates += _glob.glob(os.path.join(root, sub, "*araview*", "bin",
                                                  "paraview.exe"))
    return candidates[0] if candidates else None


def window_no_console():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# 迷你"复选框树"
# ---------------------------------------------------------------------------
class CheckTree(ttk.Frame):
    """ttk.Treeview + 用 ☐/☑ 前缀模拟复选框；父行点击可整组切换。"""

    def __init__(self, master, columns, headings, on_toggle=None, width=360, height=10):
        super().__init__(master)
        self.on_toggle = on_toggle
        self.tree = ttk.Treeview(self, columns=columns, show="tree headings",
                                 selectmode="browse", height=height)
        self.tree.heading("#0", text=headings[0], anchor="w")
        self.tree.column("#0", width=width, stretch=True)
        for col, text in zip(columns, headings[1:]):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=70, stretch=False, anchor="w")
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", self._on_space)
        self.checked = {}

    # -- 数据 -------------------------------------------------------------
    def clear(self):
        for iid in self.tree.get_children(""):
            self.tree.delete(iid)
        self.checked = {}

    def add_row(self, iid, text, values=(), checked=False, parent=""):
        mark = CHECK_ON if checked else CHECK_OFF
        self.tree.insert(parent, "end", iid=iid, text="%s %s" % (mark, text),
                         values=values, open=True)
        self.checked[iid] = bool(checked)

    def add_group(self, iid, title):
        self.tree.insert("", "end", iid=iid, text=title, open=True, values=())
        self.tree.insert(iid, "end", iid=iid + ":__pad", text="", values=())
        self.tree.delete(iid + ":__pad")

    def is_checked(self, iid):
        return self.checked.get(iid, False)

    def set_checked(self, iid, value):
        if iid not in self.checked:
            return
        self.checked[iid] = bool(value)
        self._refresh_marks(iid)

    def _refresh_marks(self, iid):
        text = self.tree.item(iid, "text")
        body = text[2:] if text[:1] in (CHECK_ON, CHECK_OFF) else text
        self.tree.item(iid, text="%s %s"
                      % (CHECK_ON if self.checked.get(iid) else CHECK_OFF, body))

    def group_children(self, parent):
        return list(self.tree.get_children(parent))

    # -- 交互 -------------------------------------------------------------
    def _toggle_row(self, iid):
        if iid in self.checked:
            self.set_checked(iid, not self.is_checked(iid))
        else:
            kids = self.group_children(iid)
            if kids:
                target = not all(self.is_checked(k) for k in kids)
                for kid in kids:
                    self.set_checked(kid, target)
        if self.on_toggle:
            self.on_toggle()

    def _on_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region not in ("tree", "cell"):
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        self._toggle_row(iid)
        return "break"

    def _on_space(self, event):
        for iid in self.tree.selection():
            self._toggle_row(iid)
        return "break"


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class App(object):
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.scan_data = None
        self.scan_path = ""
        self.var_rows = []
        self.job_queue = queue.Queue()
        self.proc = None
        self.busy = False
        self.scanning = False
        self._poll_pending = False
        self.last_outdir = ""
        self.last_pvd = ""

        self.abaqus_cmd = find_abaqus_python()
        self.paraview_exe = find_paraview()

        root.title(APP_TITLE)
        geometry = self.cfg.get("geometry")
        if not geometry:
            geometry = self._default_geometry(root)
        root.geometry(geometry)
        root.minsize(1000, 700)

        self._build_style()
        self._build_ui()
        self._apply_defaults()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- 外观 -------------------------------------------------------------
    @staticmethod
    def _default_geometry(root):
        """Fits the window to the screen instead of a fixed size."""
        try:
            screen_w = root.winfo_screenwidth()
            screen_h = root.winfo_screenheight()
        except Exception:
            return "1160x820+80+40"
        width = min(1240, max(1040, screen_w - 360))
        height = min(900, max(680, screen_h - 220))
        x = max(20, (screen_w - width) // 2)
        y = max(20, (screen_h - height) // 3)
        return "%dx%d+%d+%d" % (width, height, x, y)

    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        base = ("Microsoft YaHei UI", 9)
        self.root.option_add("*Font", base)
        style.configure("Treeview", rowheight=22, font=base)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TButton", padding=(10, 4))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Hint.TLabel", foreground="#5b6570")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))

    # -- 界面 -------------------------------------------------------------
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=(12, 10, 12, 10))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(head, style="Hint.TLabel",
                  text="   选择 ODB \u2192 勾选变量 \u2192 导出 ParaView 可读的 VTU"
                  ).pack(side="left", pady=(6, 0))

        # 1. 文件
        filebox = ttk.LabelFrame(outer, text=" 1. ODB 文件 ", padding=8)
        filebox.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        filebox.columnconfigure(1, weight=1)
        ttk.Label(filebox, text="文件路径").grid(row=0, column=0, padx=(0, 8))
        self.odb_var = tk.StringVar()
        ttk.Entry(filebox, textvariable=self.odb_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(filebox, text="浏览...", command=self._browse_odb, width=9
                   ).grid(row=0, column=2, padx=6)
        self.scan_btn = ttk.Button(filebox, text="扫描内容", style="Accent.TButton",
                                   command=self._start_scan, width=12)
        self.scan_btn.grid(row=0, column=3)
        self.scan_info = ttk.Label(filebox, text="尚未扫描", style="Hint.TLabel")
        self.scan_info.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # 2. 主体
        body = ttk.Frame(outer)
        body.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        body.columnconfigure(0, weight=0, minsize=300)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        outer.rowconfigure(2, weight=3)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        left.rowconfigure(4, weight=1)

        ttk.Label(left, text="分析步").grid(row=0, column=0, sticky="w")
        self.step_tree = CheckTree(left, columns=("frames",), headings=("分析步", "帧数"),
                                   width=230, height=5)
        self.step_tree.grid(row=1, column=0, sticky="nsew", pady=(3, 8))
        self.step_tree.tree.column("frames", width=60, anchor="center")

        fr = ttk.LabelFrame(left, text=" 帧 ", padding=(8, 6))
        fr.grid(row=2, column=0, sticky="ew")
        self.frame_mode = tk.StringVar(value="last")
        ttk.Radiobutton(fr, text="最后一帧", value="last", variable=self.frame_mode
                        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(fr, text="全部帧", value="all", variable=self.frame_mode
                        ).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Radiobutton(fr, text="自定义", value="custom", variable=self.frame_mode
                        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.frame_entry = ttk.Entry(fr, width=16)
        self.frame_entry.insert(0, "0,2-5,last")
        self.frame_entry.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(4, 0))

        ttk.Label(left, text="实例 (instance)").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.inst_tree = CheckTree(left, columns=("cells",), headings=("实例", "单元数"),
                                   width=230, height=4)
        self.inst_tree.grid(row=4, column=0, sticky="nsew", pady=(3, 0))
        self.inst_tree.tree.column("cells", width=60, anchor="center")

        right = ttk.LabelFrame(body, text=" 2. 要导出的场变量 ", padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(right)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(toolbar, text="筛选").pack(side="left")
        self.filter_var = tk.StringVar()
        filt = ttk.Entry(toolbar, textvariable=self.filter_var, width=16)
        filt.pack(side="left", padx=(6, 12))
        filt.bind("<KeyRelease>", lambda e: self._rebuild_var_tree())
        ttk.Button(toolbar, text="全选", width=7,
                   command=lambda: self._check_all_vars(True)).pack(side="left")
        ttk.Button(toolbar, text="清空", width=7,
                   command=lambda: self._check_all_vars(False)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="仅常用 (U,S,RF)", width=16,
                   command=self._check_common_vars).pack(side="left", padx=4)
        self.var_count_label = ttk.Label(toolbar, text="", style="Hint.TLabel")
        self.var_count_label.pack(side="right")

        self.var_tree = CheckTree(right, columns=("pos", "comp", "inst"),
                                  headings=("变量", "位置", "分量", "存在实例"),
                                  width=250, height=14, on_toggle=self._update_var_count)
        self.var_tree.grid(row=1, column=0, sticky="nsew")
        self.var_tree.tree.column("pos", width=60, anchor="center")
        self.var_tree.tree.column("comp", width=170, anchor="w")
        self.var_tree.tree.column("inst", width=200, anchor="w")

        # 3. 选项
        opts = ttk.LabelFrame(outer, text=" 3. 输出选项 ", padding=8)
        opts.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        opts.columnconfigure(1, weight=1)
        opts.columnconfigure(3, weight=1)

        ttk.Label(opts, text="输出目录").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.outdir_var = tk.StringVar()
        ttk.Entry(opts, textvariable=self.outdir_var).grid(row=0, column=1, columnspan=3,
                                                           sticky="ew")
        ttk.Button(opts, text="浏览...", width=9, command=self._browse_outdir
                   ).grid(row=0, column=4, padx=(6, 0))
        ttk.Label(opts, text="文件名前缀").grid(row=0, column=5, sticky="e", padx=(16, 6))
        self.prefix_var = tk.StringVar()
        ttk.Entry(opts, textvariable=self.prefix_var, width=18).grid(row=0, column=6, sticky="w")

        row2 = ttk.Frame(opts)
        row2.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        self.binary_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="二进制 VTU（推荐：体积小、可保留 NaN）",
                        variable=self.binary_var, command=self._sync_precision_state
                        ).pack(side="left")
        ttk.Label(row2, text="ASCII 精度").pack(side="left", padx=(14, 4))
        self.precision_var = tk.StringVar(value="%.12g")
        self.precision_box = ttk.Combobox(row2, textvariable=self.precision_var, width=8,
                                          values=("%.9g", "%.12g", "%.17g"), state="disabled")
        self.precision_box.pack(side="left")
        self.pvd_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="生成 .pvd 时间序列", variable=self.pvd_var
                        ).pack(side="left", padx=(18, 0))

        row3 = ttk.Frame(opts)
        row3.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        self.deform_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="按位移 U 显示变形，放大系数",
                        variable=self.deform_var, command=self._sync_scale_state
                        ).pack(side="left")
        self.scale_var = tk.StringVar(value="1.0")
        self.scale_entry = ttk.Entry(row3, textvariable=self.scale_var, width=8,
                                     state="disabled")
        self.scale_entry.pack(side="left", padx=(6, 0))
        ttk.Label(row3, style="Hint.TLabel",
                  text="   节点量写入 PointData；积分点量按单元取平均写入 CellData"
                  ).pack(side="left", padx=(18, 0))

        # 4. 执行
        action = ttk.Frame(outer)
        action.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        action.columnconfigure(2, weight=1)
        self.convert_btn = ttk.Button(action, text="开始转换", style="Accent.TButton",
                                      command=self._start_convert, width=14)
        self.convert_btn.grid(row=0, column=0)
        self.cancel_btn = ttk.Button(action, text="取消", width=8, command=self._cancel,
                                     state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=(8, 12))
        self.progress = ttk.Progressbar(action, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=2, sticky="ew")
        self.open_dir_btn = ttk.Button(action, text="打开输出目录", width=14,
                                       command=self._open_outdir, state="disabled")
        self.open_dir_btn.grid(row=0, column=3, padx=(12, 0))
        self.pv_btn = ttk.Button(action, text="用 ParaView 打开", width=16,
                                 command=self._open_in_paraview, state="disabled")
        self.pv_btn.grid(row=0, column=4, padx=(6, 0))

        # 5. 日志
        logbox = ttk.LabelFrame(outer, text=" 日志 ", padding=4)
        logbox.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        outer.rowconfigure(5, weight=1)
        logbox.columnconfigure(0, weight=1)
        logbox.rowconfigure(0, weight=1)
        self.log = tk.Text(logbox, height=6, wrap="word", font=("Consolas", 9),
                           background="#fbfcfd", relief="flat", borderwidth=1)
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=lsb.set)
        self.log.tag_configure("info", foreground="#2c3e50")
        self.log.tag_configure("warn", foreground="#b9770e")
        self.log.tag_configure("error", foreground="#c0392b")
        self.log.tag_configure("done", foreground="#1e8449")
        self.log.tag_configure("hint", foreground="#7f8c8d")
        self.log.configure(state="disabled")

        self.status = ttk.Label(outer, text="", style="Hint.TLabel")
        self.status.grid(row=6, column=0, sticky="w", pady=(6, 0))

    # -- 默认值 -----------------------------------------------------------
    def _apply_defaults(self):
        self.outdir_var.set(self.cfg.get("outdir", ""))
        self.binary_var.set(bool(self.cfg.get("binary", True)))
        self.pvd_var.set(bool(self.cfg.get("pvd", True)))
        self.deform_var.set(bool(self.cfg.get("deform", False)))
        self.scale_var.set(str(self.cfg.get("scale", "1.0")))
        self.frame_mode.set(self.cfg.get("frame_mode", "last"))
        self.precision_var.set(self.cfg.get("precision", "%.12g"))
        self._sync_precision_state()
        self._sync_scale_state()

        if self.abaqus_cmd:
            self._log("已找到 Abaqus Python：%s" % " ".join(self.abaqus_cmd), "hint")
        else:
            self._log("未找到 Abaqus Python（SMAPython.exe）。请安装 Abaqus，或在 "
                      "odb2vtu_config.json 里把 \"abaqus_python\" 设为 SMAPython.exe 路径。",
                      "error")
        self._set_status("就绪")

    def _sync_precision_state(self):
        self.precision_box.configure(state="readonly" if not self.binary_var.get()
                                     else "disabled")

    def _sync_scale_state(self):
        self.scale_entry.configure(state="normal" if self.deform_var.get() else "disabled")

    # -- 通用 -------------------------------------------------------------
    def _set_status(self, text):
        self.status.configure(text=text)

    def _log(self, text, tag="info"):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _browse_odb(self):
        path = filedialog.askopenfilename(
            title="选择 Abaqus ODB 文件",
            filetypes=[("Abaqus ODB", "*.odb"), ("所有文件", "*.*")],
            initialdir=self.cfg.get("last_odb_dir") or os.getcwd())
        if path:
            self.odb_var.set(path)
            self.cfg["last_odb_dir"] = os.path.dirname(path)
            if not self.outdir_var.get():
                self.outdir_var.set(os.path.join(os.path.dirname(path), "vtu"))
            if not self.prefix_var.get():
                self.prefix_var.set(os.path.splitext(os.path.basename(path))[0])
            self._start_scan()

    def _browse_outdir(self):
        path = filedialog.askdirectory(title="选择输出目录",
                                       initialdir=self.outdir_var.get() or os.getcwd())
        if path:
            self.outdir_var.set(path)

    # -- 扫描 -------------------------------------------------------------
    def _worker_cmd(self):
        if not self.abaqus_cmd:
            return None
        return list(self.abaqus_cmd) + [WORKER]

    def _start_scan(self):
        if self.busy or self.scanning:
            return
        path = self.odb_var.get().strip().strip('"')
        if not path or not os.path.isfile(path):
            messagebox.showwarning(APP_TITLE, "请先选择一个存在的 .odb 文件。")
            return
        cmd = self._worker_cmd()
        if not cmd:
            messagebox.showerror(APP_TITLE, "未找到 Abaqus Python，无法读取 ODB。")
            return
        if not self.outdir_var.get():
            self.outdir_var.set(os.path.join(os.path.dirname(path), "vtu"))
        if not self.prefix_var.get():
            self.prefix_var.set(os.path.splitext(os.path.basename(path))[0])

        self._clear_log()
        self._log("开始扫描：%s" % path)
        self.scan_info.configure(text="扫描中，请稍候…")
        self.scan_btn.configure(state="disabled")
        self.scanning = True
        threading.Thread(target=self._scan_thread, args=(cmd, path), daemon=True).start()
        self._schedule_poll()

    def _scan_thread(self, cmd, path):
        out_json = os.path.join(tempfile.gettempdir(), "odb2vtu_scan_%d.json" % os.getpid())
        argv = cmd + ["scan", path, out_json]
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    cwd=APP_DIR, creationflags=window_no_console(),
                                    env=self._child_env())
            out, _ = proc.communicate()
            text = out.decode("utf-8", "replace")
            for line in text.splitlines():
                if line.startswith("##ERROR"):
                    self.job_queue.put(("log", (line[7:].strip(), "error")))
                elif line.startswith("##WARN"):
                    self.job_queue.put(("log", (line[6:].strip(), "warn")))
            if proc.returncode == 0 and os.path.isfile(out_json):
                with open(out_json, "r", encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                self.job_queue.put(("scan_done", data))
            else:
                tail = "\n".join([l for l in text.splitlines() if l.strip()][-8:])
                self.job_queue.put(("scan_fail", tail or "扫描失败（无输出）"))
        except Exception:
            self.job_queue.put(("scan_fail", traceback.format_exc()))

    # -- 变量树 -----------------------------------------------------------
    def _populate_scan(self, data):
        self.scan_data = data
        self.scan_path = data.get("odb", "")
        self.scanning = False
        self.scan_btn.configure(state="normal")
        self._clear_log()

        insts = data.get("instances", [])
        steps = data.get("steps", [])
        variables = self._collect_variables(data)
        self.scan_info.configure(
            text="实例 %d 个 · 分析步 %d 个 · 变量 %d 个 · %s"
                 % (len(insts), len(steps), len(variables), os.path.basename(self.scan_path)))
        self._log("扫描完成：%d 个实例，%d 个分析步，%d 个场变量"
                  % (len(insts), len(steps), len(variables)), "done")
        for inst in insts:
            types = ", ".join("%s x%d" % (k, v)
                              for k, v in inst.get("element_types", {}).items())
            self._log("  实例 %-18s %6d 节点  %6d 单元  (%s)"
                      % (inst["name"], inst["nodes"], inst["elements"], types))
        for step in steps:
            self._log("  分析步 %-16s %d 帧  domain=%s"
                      % (step["name"], step.get("frames", 0), step.get("domain", "")))

        self.step_tree.clear()
        for step in steps:
            self.step_tree.add_row("step:%s" % step["name"], step["name"],
                                   (step.get("frames", 0),), checked=True)

        self.inst_tree.clear()
        for inst in insts:
            self.inst_tree.add_row("inst:%s" % inst["name"], inst["name"],
                                   (inst.get("elements", 0),), checked=True)

        self._rebuild_var_tree()

    def _collect_variables(self, data=None):
        data = data or self.scan_data or {}
        rows = {}
        order = []
        for step in data.get("steps", []):
            for var in step.get("variables", []):
                name = var["name"]
                if name not in rows:
                    rows[name] = dict(name=name, pos=var.get("position", ""),
                                      location=var.get("location", ""),
                                      comps=list(var.get("components", [])),
                                      insts=set(var.get("instances", [])))
                    order.append(name)
                rows[name]["insts"].update(var.get("instances", []))
        result = []
        for name in order:
            rec = rows[name]
            result.append((name, rec["location"], rec["comps"], sorted(rec["insts"]),
                           rec["pos"]))
        result.sort(key=lambda r: (0 if r[1] == "node" else 1, r[0].upper()))
        return result

    def _rebuild_var_tree(self):
        prev = dict(self.var_tree.checked)
        first_time = not prev
        text = self.filter_var.get().strip().lower()
        all_vars = self._collect_variables()
        self.var_rows = [v for v in all_vars if not text or text in v[0].lower()]

        tree = self.var_tree
        tree.clear()
        node_vars = [v for v in self.var_rows if v[1] == "node"]
        elem_vars = [v for v in self.var_rows if v[1] != "node"]

        def add_group(gid, title, items):
            if not items:
                return
            tree.add_group(gid, title)
            for name, location, comps, insts, pos in items:
                iid = "var:%s" % name
                checked = prev.get(iid, False)
                if first_time:
                    checked = name.upper() in ("U", "S", "RF")
                where = "\u8282\u70b9" if location == "node" else "\u5355\u5143"
                tree.add_row(iid, name, (where, ",".join(comps) if comps else "-",
                                         ", ".join(insts) or "-"),
                             checked=checked, parent=gid)

        add_group("g_node", "\u8282\u70b9\u53d8\u91cf (PointData)", node_vars)
        add_group("g_cell", "\u5355\u5143\u53d8\u91cf (CellData)", elem_vars)
        if not node_vars and not elem_vars:
            if self.scan_data:
                tree.add_row("hint", "\u6ca1\u6709\u5339\u914d\u7684\u53d8\u91cf", ("", "", ""))
            else:
                tree.add_row("hint",
                             "\u8bf7\u9009\u62e9 ODB \u6587\u4ef6\uff0c\u518d\u70b9\u300c\u626b\u63cf\u5185\u5bb9\u300d",
                             ("", "", ""))
        self._update_var_count()

    def _check_all_vars(self, value):
        for name, location, comps, insts, pos in self.var_rows:
            self.var_tree.set_checked("var:%s" % name, value)
        self._update_var_count()

    def _check_common_vars(self):
        self._check_all_vars(False)
        for name, location, comps, insts, pos in self.var_rows:
            if name.upper() in ("U", "S", "RF", "LE", "PE", "V", "A"):
                self.var_tree.set_checked("var:%s" % name, True)
        self._update_var_count()

    def _update_var_count(self):
        n = sum(1 for row in self.var_rows if self.var_tree.is_checked("var:%s" % row[0]))
        self.var_count_label.configure(text="已选 %d / %d" % (n, len(self.var_rows)))

    def _selected_vars(self):
        return [row[0] for row in self.var_rows
                if self.var_tree.is_checked("var:%s" % row[0])]

    # -- 队列轮询 ---------------------------------------------------------
    def _schedule_poll(self):
        if self._poll_pending:
            return
        self._poll_pending = True
        self.root.after(80, self._poll_queue)

    def _poll_queue(self):
        self._poll_pending = False
        try:
            while True:
                kind, payload = self.job_queue.get_nowait()
                if kind == "log":
                    text, tag = payload
                    self._log(text, tag)
                elif kind == "progress":
                    done, total, text = payload
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self._set_status("转换中 %d/%d  %s" % (done, total, text))
                elif kind == "scan_done":
                    self._populate_scan(payload)
                    self._set_status("扫描完成")
                elif kind == "scan_fail":
                    self.scanning = False
                    self.scan_btn.configure(state="normal")
                    self.scan_info.configure(text="扫描失败")
                    self._log(payload, "error")
                    self._set_status("扫描失败")
                    messagebox.showerror(APP_TITLE, "扫描 ODB 失败：\n%s" % payload)
                elif kind == "job_done":
                    self._finish_job(payload)
                elif kind == "job_fail":
                    self._finish_job(None, payload)
        except queue.Empty:
            pass
        if self.busy or self.scanning:
            self._schedule_poll()

    # -- 转换 -------------------------------------------------------------
    def _frame_indices(self, mode, custom, count):
        if count <= 0:
            return []
        if mode == "all":
            return list(range(count))
        if mode == "last":
            return [count - 1]
        out = []
        for token in custom.replace(" ", "").split(","):
            if not token:
                continue
            if token.lower() == "last":
                out.append(count - 1)
                continue
            m = re.match(r"^(\d+)-(\d+)$", token)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                step = 1 if b >= a else -1
                out.extend(range(a, b + step, step))
            else:
                try:
                    value = int(token)
                except ValueError:
                    continue
                out.append(count - 1 if value < 0 else value)
        unique = list(dict.fromkeys(out))
        return [i for i in unique if 0 <= i < count]

    def _start_convert(self):
        if self.busy or self.scanning:
            return
        if not self.scan_data:
            messagebox.showwarning(APP_TITLE, "请先扫描 ODB 文件。")
            return
        variables = self._selected_vars()
        if not variables:
            messagebox.showwarning(APP_TITLE, "请至少勾选一个场变量。")
            return
        steps = []
        for step in self.scan_data.get("steps", []):
            if not self.step_tree.is_checked("step:%s" % step["name"]):
                continue
            frames = self._frame_indices(self.frame_mode.get(), self.frame_entry.get(),
                                         int(step.get("frames", 0)))
            if frames:
                steps.append(dict(name=step["name"], frames=frames))
        if not steps:
            messagebox.showwarning(APP_TITLE, "请至少选择一个分析步，且帧范围内要有数据。")
            return
        insts = [i["name"] for i in self.scan_data.get("instances", [])
                 if self.inst_tree.is_checked("inst:%s" % i["name"])]
        if not insts:
            messagebox.showwarning(APP_TITLE, "请至少保留一个实例。")
            return
        outdir = self.outdir_var.get().strip()
        if not outdir:
            messagebox.showwarning(APP_TITLE, "请指定输出目录。")
            return
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "无法创建输出目录：\n%s" % exc)
            return
        try:
            scale = float(self.scale_var.get())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "放大系数必须是数字。")
            return

        prefix = self.prefix_var.get().strip() or \
            os.path.splitext(os.path.basename(self.scan_path))[0]
        spec = dict(odb=self.scan_path, outdir=outdir, prefix=prefix, variables=variables,
                    instances=insts, steps=steps, deform=bool(self.deform_var.get()),
                    deform_scale=scale, binary=bool(self.binary_var.get()),
                    precision=self.precision_var.get() or "%.12g",
                    pvd=bool(self.pvd_var.get()))
        spec_path = os.path.join(tempfile.gettempdir(), "odb2vtu_spec.json")
        with open(spec_path, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, indent=2, ensure_ascii=False)

        self._clear_log()
        self._log("导出变量：%s" % ", ".join(variables))
        self._log("输出目录：%s" % outdir)
        self._log("开始转换…")
        self.progress.configure(value=0, maximum=1)
        self.convert_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_dir_btn.configure(state="disabled")
        self.pv_btn.configure(state="disabled")
        self.busy = True
        self.last_outdir = outdir
        self.last_pvd = ""
        threading.Thread(target=self._convert_thread, args=(spec_path, spec),
                         daemon=True).start()
        self._schedule_poll()

    def _convert_thread(self, spec_path, spec):
        cmd = self._worker_cmd()
        argv = cmd + ["convert", spec_path]
        settled = False
        try:
            self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, cwd=APP_DIR,
                                         creationflags=window_no_console(),
                                         env=self._child_env())
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                if line.startswith("##PROGRESS"):
                    parts = line.split(None, 3)
                    try:
                        done, total = int(parts[1]), int(parts[2])
                    except (IndexError, ValueError):
                        done, total = 0, 0
                    text = parts[3] if len(parts) > 3 else ""
                    self.job_queue.put(("progress", (done, total, text)))
                elif line.startswith("##INFO"):
                    self.job_queue.put(("log", (line[6:].strip(), "info")))
                elif line.startswith("##WARN"):
                    self.job_queue.put(("log", ("警告：" + line[6:].strip(), "warn")))
                elif line.startswith("##DONE"):
                    try:
                        payload = json.loads(line[6:].strip())
                    except Exception:
                        payload = {"ok": True, "files": []}
                    payload["outdir"] = self.last_outdir
                    self.job_queue.put(("job_done", payload))
                    settled = True
                elif line.startswith("##ERROR"):
                    self.job_queue.put(("job_fail", line[7:].strip()))
                    settled = True
                else:
                    self.job_queue.put(("log", (line, "hint")))
            code = self.proc.wait()
            if code != 0 and not settled:
                self.job_queue.put(("job_fail", "worker 退出码 %d" % code))
        except Exception:
            self.job_queue.put(("job_fail", traceback.format_exc()))
        finally:
            self.proc = None

    def _finish_job(self, payload, error=None):
        self.busy = False
        self.convert_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.proc = None
        if error:
            self._log("转换失败：%s" % error, "error")
            self._set_status("转换失败")
            messagebox.showerror(APP_TITLE, "转换失败：\n%s" % error)
            return
        files = payload.get("files", []) if payload else []
        outdir = (payload or {}).get("outdir", self.last_outdir)
        for warn in (payload or {}).get("warnings", []):
            self._log("提示：%s" % warn, "warn")
        self._log("完成，共写出 %d 个文件。" % len(files), "done")
        self.progress.configure(value=self.progress.cget("maximum"))
        self._set_status("完成：%d 个文件" % len(files))
        self.open_dir_btn.configure(state="normal" if outdir else "disabled")
        pvds = [f for f in files if str(f).lower().endswith(".pvd")]
        if pvds and outdir:
            self.last_pvd = os.path.join(outdir, pvds[0])
        self.pv_btn.configure(state="normal" if (self.last_pvd and self.paraview_exe)
                              else "disabled")
        if files:
            messagebox.showinfo(APP_TITLE, "转换完成，共 %d 个文件。\n\n%s"
                                % (len(files), "\n".join(files[:8])))

    def _cancel(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _open_outdir(self):
        if self.last_outdir and os.path.isdir(self.last_outdir):
            try:
                os.startfile(self.last_outdir)
            except Exception:
                pass

    def _open_in_paraview(self):
        if self.last_pvd and os.path.isfile(self.last_pvd) and self.paraview_exe:
            try:
                subprocess.Popen([self.paraview_exe, self.last_pvd])
            except Exception as exc:
                messagebox.showerror(APP_TITLE, "无法启动 ParaView：\n%s" % exc)

    # -- 环境 -------------------------------------------------------------
    def _child_env(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _on_close(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.cfg.update(dict(geometry=self.root.geometry(),
                             outdir=self.outdir_var.get(),
                             binary=self.binary_var.get(),
                             pvd=self.pvd_var.get(),
                             deform=self.deform_var.get(),
                             scale=self.scale_var.get(),
                             frame_mode=self.frame_mode.get(),
                             precision=self.precision_var.get()))
        save_config(self.cfg)
        self.root.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()