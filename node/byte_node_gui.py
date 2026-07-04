#!/usr/bin/env python3
"""
Byte Transcode Node — Desktop GUI
===================================
Tdarr-style node application with configuration, logs, and status.
Run: py byte_node_gui.py
"""
import os, sys, json, threading, time, queue, subprocess, tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "byte_node_config.json")
TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")

# v2.11 — reuse the node's own update helpers so the GUI can notify + self-update
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from byte_node_v2 import NODE_VERSION, check_for_update, download_update
except Exception:
    NODE_VERSION = "?"
    def check_for_update(*a, **k): return None
    def download_update(*a, **k): return (False, ["byte_node_v2.py not importable"])

# ─── Dark Theme Colors ───────────────────────────────────────────────────────
C = {
    "bg": "#1a1d23",
    "bg2": "#22262e",
    "bg3": "#2a2f38",
    "card": "#262a33",
    "border": "#363c48",
    "text": "#d0d4dc",
    "text2": "#8890a0",
    "accent": "#e040fb",
    "accent2": "#7c4dff",
    "green": "#66bb6a",
    "yellow": "#ffb74d",
    "red": "#ef5350",
    "blue": "#42a5f5",
    "input_bg": "#1e2128",
    "input_fg": "#d0d4dc",
}


# ─── Default Config ──────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "node_name": "DoVi-5080",
    "server_url": "http://192.168.3.13:5800",
    "gpu": "RTX 5080",
    "poll_interval": 10,
    "path_from": "/media",
    "path_to": "Z:\\",
    "temp_dir": "F:\\Byte_Engine_temp",
    "ffmpeg_path": "",
    "ffprobe_path": "",
    "dovi_tool_path": "",
    "mkvmerge_path": "",
    "start_paused": False,
}


def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                saved = json.load(f)
                cfg = {**DEFAULT_CONFIG, **saved}
                return cfg
    except Exception:
        pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Failed to save config: {e}")


def find_tool(name, custom_path=""):
    """Find a tool executable."""
    if custom_path and os.path.exists(custom_path):
        return custom_path
    # Check tools/ subfolder
    tools_exe = os.path.join(TOOLS_DIR, f"{name}.exe" if sys.platform == "win32" else name)
    if os.path.exists(tools_exe):
        return tools_exe
    # Check PATH
    try:
        r = subprocess.run(["where" if sys.platform == "win32" else "which", name],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().split("\n")[0]
    except:
        pass
    return ""


# ─── Node Engine (background worker) ────────────────────────────────────────
class NodeEngine(threading.Thread):
    """Runs the transcode node in a background thread."""

    def __init__(self, config, log_queue):
        super().__init__(daemon=True)
        self.config = config
        self.log_queue = log_queue
        self.running = False
        self.connected = False
        self.status = "Stopped"
        self.current_job = None
        self.node = None
        self._stop_event = threading.Event()

    def log(self, msg, level="INFO"):
        ts = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] [{level}] {msg}")

    def stop(self):
        self._stop_event.set()
        # Flip the node's flag right away so worker threads stop claiming
        # jobs even before the supervision loop notices the stop event.
        if self.node is not None:
            self.node.running = False
        self.running = False

    def run(self):
        self.running = True
        self.status = "Starting..."
        self.log("Node engine starting...")

        # Make bundled tools (tools\) visible to the node and its subprocesses
        if os.path.isdir(TOOLS_DIR) and TOOLS_DIR not in os.environ.get("PATH", ""):
            os.environ["PATH"] = TOOLS_DIR + os.pathsep + os.environ.get("PATH", "")

        # Import and configure the node
        try:
            # Add script dir to path so we can import byte_node_v2
            script_dir = os.path.dirname(os.path.abspath(__file__))
            if script_dir not in sys.path:
                sys.path.insert(0, script_dir)

            from byte_node_v2 import ByteNode
        except ImportError as e:
            self.log(f"Cannot import byte_node_v2.py: {e}", "ERROR")
            self.log("Make sure byte_node_v2.py is in the same folder as this GUI.", "ERROR")
            self.status = "Error"
            self.running = False
            return

        cfg = self.config

        # v2.7 — GUI fields become real per-node overrides. The node merges
        # these over server settings for every job, so this machine's temp
        # drive and path mapping win even when another node uses different
        # ones.
        overrides = {}
        if cfg.get("path_from"):
            overrides["node_path_remote_prefix"] = cfg["path_from"]
        if cfg.get("path_to"):
            overrides["node_path_local_prefix"] = cfg["path_to"]
        if cfg.get("temp_dir"):
            overrides["node_temp_path"] = cfg["temp_dir"]
            try:
                os.makedirs(cfg["temp_dir"], exist_ok=True)
            except Exception as e:
                self.log(f"Cannot create temp dir {cfg['temp_dir']}: {e}", "ERROR")

        try:
            node = ByteNode(
                server_url=cfg["server_url"],
                name=cfg["node_name"],
                gpu=cfg["gpu"],
                poll_interval=cfg["poll_interval"],
                local_overrides=overrides,
            )
        except Exception as e:
            self.log(f"Failed to create node: {e}", "ERROR")
            self.status = "Error"
            self.running = False
            return
        self.node = node

        if overrides.get("node_path_local_prefix"):
            self.log(f"Path translator: {overrides.get('node_path_remote_prefix', '/media')} → {overrides['node_path_local_prefix']}")
        if overrides.get("node_temp_path"):
            self.log(f"Temp dir: {overrides['node_temp_path']}")

        # Override tool paths if configured
        if cfg.get("ffmpeg_path"):
            node.ffmpeg = cfg["ffmpeg_path"]
        elif find_tool("ffmpeg"):
            node.ffmpeg = find_tool("ffmpeg")
        if cfg.get("ffprobe_path"):
            node.ffprobe = cfg["ffprobe_path"]
        elif find_tool("ffprobe"):
            node.ffprobe = find_tool("ffprobe")
        if cfg.get("dovi_tool_path"):
            node.dovi_tool = cfg["dovi_tool_path"]
        elif find_tool("dovi_tool"):
            node.dovi_tool = find_tool("dovi_tool")
        if cfg.get("mkvmerge_path"):
            node.mkvmerge = cfg["mkvmerge_path"]
        elif find_tool("mkvmerge"):
            node.mkvmerge = find_tool("mkvmerge")

        self.log(f"ffmpeg:    {node.ffmpeg}")
        self.log(f"ffprobe:   {node.ffprobe}")
        self.log(f"dovi_tool: {node.dovi_tool}")
        self.log(f"mkvmerge:  {node.mkvmerge}")

        # Redirect node's log output to our queue (before registration so
        # connection logs are visible in the GUI)
        original_log = node.log
        def gui_log(msg, level="INFO"):
            original_log(msg, level)
            self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] [{level}] {msg}")
        node.log = gui_log

        # Register + spawn workers (non-blocking), retrying until Stop
        while not self._stop_event.is_set():
            if node.start_workers():
                break
            self.status = "Disconnected"
            self.connected = False
            self.log("Failed to connect to server — retrying in 10s...", "WARN")
            for _ in range(20):
                if self._stop_event.is_set():
                    break
                time.sleep(0.5)
        if self._stop_event.is_set():
            node.running = False
            self.status = "Stopped"
            self.running = False
            return

        self.connected = True
        self.status = "Idle"
        self.log(f"Connected to {cfg['server_url']}")

        # Supervision loop — worker threads poll for jobs; here we just
        # mirror node state into the GUI until Stop is pressed.
        while not self._stop_event.is_set():
            try:
                jobs = node.active_jobs
                if jobs:
                    names = [n for n in jobs.values() if n]
                    self.current_job = f"{len(jobs)} job(s): " + ", ".join(names[:2]) if names else f"{len(jobs)} job(s)"
                    self.status = "Processing"
                else:
                    self.current_job = None
                    self.status = "Idle"
            except Exception:
                pass
            time.sleep(1)

        # Stop: flip the node's running flag so all worker/heartbeat threads
        # exit their loops. A job already mid-transcode finishes its current
        # subprocess; no new jobs are claimed.
        node.running = False
        self.current_job = None
        self.status = "Stopped"
        self.connected = False
        self.running = False
        self.log("Node engine stopped. (Active jobs finish their current step; no new jobs will be claimed.)")


# ─── GUI Application ────────────────────────────────────────────────────────
class ByteNodeGUI:
    def __init__(self):
        self.config = load_config()
        self.log_queue = queue.Queue()
        self.engine = None

        self.root = tk.Tk()
        self.root.title("Byte Transcode Node")
        self.root.geometry("680x780")
        self.root.configure(bg=C["bg"])
        self.root.resizable(True, True)

        # Set icon (optional)
        try:
            self.root.iconbitmap(default="")
        except:
            pass

        self._build_ui()
        self._poll_logs()
        self._update_status()

        # Auto-start if not paused
        if not self.config.get("start_paused", False):
            self.root.after(500, self.start_engine)

    def _build_ui(self):
        root = self.root

        # ─── Header ───
        header = tk.Frame(root, bg=C["bg"], pady=10, padx=16)
        header.pack(fill="x")

        title_frame = tk.Frame(header, bg=C["bg"])
        title_frame.pack(side="left")
        tk.Label(title_frame, text="Byte Transcode Node", font=("Segoe UI", 18, "bold"),
                 fg=C["accent"], bg=C["bg"]).pack(side="left")
        tk.Label(title_frame, text=f"  v{NODE_VERSION}", font=("Segoe UI", 10),
                 fg=C["text2"], bg=C["bg"]).pack(side="left", pady=(6, 0))

        # Exit button
        exit_btn = tk.Button(header, text="Exit", font=("Segoe UI", 10, "bold"),
                             bg=C["red"], fg="white", bd=0, padx=16, pady=4,
                             command=self._on_exit, cursor="hand2", activebackground="#d32f2f")
        exit_btn.pack(side="right")

        # ─── Update banner (v2.11, hidden until a newer version is found) ───
        self.update_bar = tk.Frame(root, bg="#3a2f12", padx=16, pady=8)
        self._update_info = None
        self.update_label = tk.Label(self.update_bar, text="", font=("Segoe UI", 10, "bold"),
                                     fg=C["yellow"], bg="#3a2f12", anchor="w", justify="left")
        self.update_label.pack(side="left", fill="x", expand=True)
        tk.Button(self.update_bar, text="Later", font=("Segoe UI", 9),
                  bg=C["bg3"], fg=C["text"], bd=0, padx=12, pady=3, cursor="hand2",
                  command=self.update_bar.pack_forget).pack(side="right")
        self.update_btn = tk.Button(self.update_bar, text="Update Now", font=("Segoe UI", 9, "bold"),
                                    bg=C["green"], fg="white", bd=0, padx=14, pady=3, cursor="hand2",
                                    activebackground="#4caf50", command=self._do_update)
        self.update_btn.pack(side="right", padx=(0, 8))
        # kick off a background check shortly after launch
        self.root.after(2500, self._check_update_bg)

        # ─── Status indicators ───
        status_frame = tk.Frame(root, bg=C["bg"], padx=16, pady=8)
        status_frame.pack(fill="x")

        self.running_dot = tk.Canvas(status_frame, width=12, height=12, bg=C["bg"], highlightthickness=0)
        self.running_dot.pack(side="left")
        self.running_dot.create_oval(2, 2, 10, 10, fill=C["red"], outline="", tags="dot")
        self.running_label = tk.Label(status_frame, text="Stopped", font=("Segoe UI", 10),
                                       fg=C["text2"], bg=C["bg"])
        self.running_label.pack(side="left", padx=(4, 16))

        self.conn_dot = tk.Canvas(status_frame, width=12, height=12, bg=C["bg"], highlightthickness=0)
        self.conn_dot.pack(side="left")
        self.conn_dot.create_oval(2, 2, 10, 10, fill=C["yellow"], outline="", tags="dot")
        self.conn_label = tk.Label(status_frame, text="Disconnected", font=("Segoe UI", 10),
                                    fg=C["text2"], bg=C["bg"])
        self.conn_label.pack(side="left", padx=(4, 0))

        self.job_label = tk.Label(status_frame, text="", font=("Segoe UI", 9),
                                   fg=C["blue"], bg=C["bg"])
        self.job_label.pack(side="right")

        # ─── Tabs ───
        tab_frame = tk.Frame(root, bg=C["bg"], padx=16, pady=8)
        tab_frame.pack(fill="x")

        self.tab_var = tk.StringVar(value="config")
        for val, label in [("config", "Configuration"), ("logs", "Logs")]:
            rb = tk.Radiobutton(tab_frame, text=label, variable=self.tab_var, value=val,
                                font=("Segoe UI", 11, "bold"), fg=C["text"], bg=C["bg"],
                                selectcolor=C["accent"], activebackground=C["bg"],
                                activeforeground=C["accent"], indicatoron=0,
                                bd=0, padx=16, pady=6, cursor="hand2",
                                command=self._switch_tab)
            rb.pack(side="left", padx=(0, 4))

        # ─── Content frames ───
        self.config_frame = tk.Frame(root, bg=C["bg2"], padx=16, pady=12)
        self.logs_frame = tk.Frame(root, bg=C["bg2"], padx=16, pady=12)

        self._build_config_tab()
        self._build_logs_tab()

        # Show config by default
        self.config_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    def _make_field(self, parent, label, desc, row, default="", browse=False):
        """Create a labeled input field."""
        lbl_frame = tk.Frame(parent, bg=C["bg2"])
        lbl_frame.grid(row=row, column=0, sticky="nw", padx=(0, 16), pady=6)
        tk.Label(lbl_frame, text=label, font=("Segoe UI", 11, "bold"),
                 fg=C["text"], bg=C["bg2"]).pack(anchor="w")
        if desc:
            tk.Label(lbl_frame, text=desc, font=("Segoe UI", 8),
                     fg=C["text2"], bg=C["bg2"]).pack(anchor="w")

        entry_frame = tk.Frame(parent, bg=C["bg2"])
        entry_frame.grid(row=row, column=1, sticky="ew", pady=6)

        entry = tk.Entry(entry_frame, font=("Consolas", 11), bg=C["input_bg"], fg=C["input_fg"],
                         insertbackground=C["accent"], bd=0, highlightthickness=1,
                         highlightcolor=C["accent"], highlightbackground=C["border"])
        entry.insert(0, str(default))
        entry.pack(side="left", fill="x", expand=True, ipady=6, ipadx=8)

        if browse:
            btn = tk.Button(entry_frame, text="Browse", font=("Segoe UI", 9),
                           bg=C["bg3"], fg=C["text2"], bd=0, padx=8, cursor="hand2",
                           command=lambda e=entry: self._browse(e))
            btn.pack(side="left", padx=(6, 0))

        return entry

    def _browse(self, entry):
        path = filedialog.askdirectory() or filedialog.askopenfilename()
        if path:
            entry.delete(0, tk.END)
            entry.insert(0, path)

    def _build_config_tab(self):
        parent = self.config_frame
        parent.columnconfigure(1, weight=1)

        # Buttons row
        btn_frame = tk.Frame(parent, bg=C["bg2"])
        btn_frame.grid(row=0, column=0, columnspan=2, sticky="e", pady=(0, 12))

        for text, color, cmd in [
            ("Save", C["blue"], self._save_config),
            ("Save & Restart", C["accent"], self._save_and_restart),
            ("Open Config", C["bg3"], self._open_config_folder)
        ]:
            tk.Button(btn_frame, text=text, font=("Segoe UI", 10, "bold"),
                      bg=color, fg="white", bd=0, padx=14, pady=4,
                      cursor="hand2", command=cmd,
                      activebackground=color).pack(side="left", padx=(0, 6))

        # Start/Stop button
        self.start_btn = tk.Button(btn_frame, text="Start", font=("Segoe UI", 10, "bold"),
                                    bg=C["green"], fg="white", bd=0, padx=14, pady=4,
                                    cursor="hand2", command=self._toggle_engine,
                                    activebackground=C["green"])
        self.start_btn.pack(side="left", padx=(0, 6))

        # Fields
        cfg = self.config
        self.f_name = self._make_field(parent, "Node Name", "Display name for this node", 1, cfg["node_name"])
        self.f_server = self._make_field(parent, "Server URL", "Byte Transcode server address", 2, cfg["server_url"])
        self.f_gpu = self._make_field(parent, "GPU", "GPU name shown in dashboard", 3, cfg["gpu"])
        self.f_poll = self._make_field(parent, "Poll Interval", "Seconds between job checks", 4, cfg["poll_interval"])

        # Separator
        ttk.Separator(parent, orient="horizontal").grid(row=5, column=0, columnspan=2, sticky="ew", pady=10)

        # Path translator
        pt_frame = tk.Frame(parent, bg=C["bg2"])
        pt_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=6)

        lbl_frame = tk.Frame(pt_frame, bg=C["bg2"])
        lbl_frame.pack(anchor="w")
        tk.Label(lbl_frame, text="Path Translator", font=("Segoe UI", 11, "bold"),
                 fg=C["text"], bg=C["bg2"]).pack(side="left")
        tk.Label(lbl_frame, text="  Map server paths to local Windows paths", font=("Segoe UI", 8),
                 fg=C["text2"], bg=C["bg2"]).pack(side="left")

        pt_inputs = tk.Frame(pt_frame, bg=C["bg2"])
        pt_inputs.pack(fill="x", pady=(4, 0))

        self.f_path_from = tk.Entry(pt_inputs, font=("Consolas", 11), bg=C["input_bg"], fg=C["input_fg"],
                                     insertbackground=C["accent"], bd=0, highlightthickness=1,
                                     highlightcolor=C["accent"], highlightbackground=C["border"], width=25)
        self.f_path_from.insert(0, cfg["path_from"])
        self.f_path_from.pack(side="left", ipady=6, ipadx=8)

        tk.Label(pt_inputs, text="  →  ", font=("Consolas", 14, "bold"),
                 fg=C["accent"], bg=C["bg2"]).pack(side="left")

        self.f_path_to = tk.Entry(pt_inputs, font=("Consolas", 11), bg=C["input_bg"], fg=C["input_fg"],
                                   insertbackground=C["accent"], bd=0, highlightthickness=1,
                                   highlightcolor=C["accent"], highlightbackground=C["border"], width=25)
        self.f_path_to.insert(0, cfg["path_to"])
        self.f_path_to.pack(side="left", ipady=6, ipadx=8)

        # Separator
        ttk.Separator(parent, orient="horizontal").grid(row=7, column=0, columnspan=2, sticky="ew", pady=10)

        self.f_temp = self._make_field(parent, "Temp Directory", "Local SSD for fast transcoding", 8, cfg["temp_dir"], browse=True)
        self.f_ffmpeg = self._make_field(parent, "FFmpeg Path", "Leave blank for auto-detect", 9, cfg["ffmpeg_path"])
        self.f_dovi = self._make_field(parent, "dovi_tool Path", "Leave blank for auto-detect", 10, cfg["dovi_tool_path"])
        self.f_mkvmerge = self._make_field(parent, "mkvmerge Path", "Leave blank for auto-detect", 11, cfg["mkvmerge_path"])

        # Tool status
        status_frame = tk.Frame(parent, bg=C["bg2"])
        status_frame.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.tool_status = tk.Label(status_frame, text="", font=("Consolas", 9),
                                     fg=C["text2"], bg=C["bg2"], justify="left", anchor="w")
        self.tool_status.pack(fill="x")
        self._check_tools()

    def _build_logs_tab(self):
        parent = self.logs_frame

        # Controls
        ctrl = tk.Frame(parent, bg=C["bg2"])
        ctrl.pack(fill="x", pady=(0, 8))

        tk.Button(ctrl, text="Clear", font=("Segoe UI", 9), bg=C["bg3"], fg=C["text2"],
                  bd=0, padx=10, cursor="hand2",
                  command=lambda: self.log_text.delete("1.0", tk.END)).pack(side="left")
        tk.Button(ctrl, text="Copy All", font=("Segoe UI", 9), bg=C["bg3"], fg=C["text2"],
                  bd=0, padx=10, cursor="hand2",
                  command=lambda: [self.root.clipboard_clear(), self.root.clipboard_append(self.log_text.get("1.0", tk.END))]).pack(side="left", padx=(6, 0))

        self.log_count = tk.Label(ctrl, text="0 lines", font=("Segoe UI", 9),
                                   fg=C["text2"], bg=C["bg2"])
        self.log_count.pack(side="right")

        # Log text area
        self.log_text = scrolledtext.ScrolledText(
            parent, font=("Consolas", 10), bg="#0c0e12", fg=C["text2"],
            insertbackground=C["accent"], bd=0, highlightthickness=1,
            highlightbackground=C["border"], wrap="word", state="disabled"
        )
        self.log_text.pack(fill="both", expand=True)

        # Configure tags for colored log levels
        self.log_text.tag_configure("ERROR", foreground=C["red"])
        self.log_text.tag_configure("WARN", foreground=C["yellow"])
        self.log_text.tag_configure("INFO", foreground=C["text2"])
        self.log_text.tag_configure("OK", foreground=C["green"])
        self.log_text.tag_configure("STEP", foreground=C["accent"])

    def _switch_tab(self):
        if self.tab_var.get() == "config":
            self.logs_frame.pack_forget()
            self.config_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        else:
            self.config_frame.pack_forget()
            self.logs_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    def _check_tools(self):
        tools = []
        for name in ["ffmpeg", "ffprobe", "dovi_tool", "mkvmerge"]:
            path = find_tool(name)
            status = f"✓ {name}" if path else f"✗ {name} — not found"
            tools.append(status)
        self.tool_status.configure(text="  |  ".join(tools))

    def _get_config_from_fields(self):
        return {
            "node_name": self.f_name.get().strip(),
            "server_url": self.f_server.get().strip(),
            "gpu": self.f_gpu.get().strip(),
            "poll_interval": int(self.f_poll.get().strip() or "10"),
            "path_from": self.f_path_from.get().strip(),
            "path_to": self.f_path_to.get().strip(),
            "temp_dir": self.f_temp.get().strip(),
            "ffmpeg_path": self.f_ffmpeg.get().strip(),
            "ffprobe_path": self.config.get("ffprobe_path", ""),
            "dovi_tool_path": self.f_dovi.get().strip(),
            "mkvmerge_path": self.f_mkvmerge.get().strip(),
            "start_paused": self.config.get("start_paused", False),
        }

    def _save_config(self):
        self.config = self._get_config_from_fields()
        save_config(self.config)
        self._add_log("[GUI] Configuration saved")

    def _save_and_restart(self):
        self._save_config()
        self.stop_engine()
        self.root.after(500, self.start_engine)

    def _open_config_folder(self):
        folder = os.path.dirname(CONFIG_FILE)
        if sys.platform == "win32":
            os.startfile(folder)
        else:
            subprocess.Popen(["xdg-open", folder])

    def _toggle_engine(self):
        if self.engine and self.engine.running:
            self.stop_engine()
        else:
            self.start_engine()

    def start_engine(self):
        if self.engine and self.engine.running:
            return
        self.config = self._get_config_from_fields()
        save_config(self.config)
        self._add_log("[GUI] Starting node engine...")
        self.engine = NodeEngine(self.config, self.log_queue)
        self.engine.start()
        self.start_btn.configure(text="Stop", bg=C["red"])

    def stop_engine(self):
        if self.engine:
            self._add_log("[GUI] Stopping node engine...")
            self.engine.stop()
            self.engine = None
        self.start_btn.configure(text="Start", bg=C["green"])

    def _add_log(self, msg):
        self.log_text.configure(state="normal")

        # Determine tag
        tag = "INFO"
        if "[ERROR]" in msg:
            tag = "ERROR"
        elif "[WARN]" in msg:
            tag = "WARN"
        elif "[OK]" in msg:
            tag = "OK"
        elif "[Step " in msg:
            tag = "STEP"

        self.log_text.insert(tk.END, msg + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

        # Update line count
        lines = int(self.log_text.index("end-1c").split(".")[0])
        self.log_count.configure(text=f"{lines} lines")

    def _poll_logs(self):
        """Check log queue and display messages."""
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._add_log(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_logs)

    def _update_status(self):
        """Update status indicators."""
        if self.engine and self.engine.running:
            # Running indicator
            self.running_dot.itemconfigure("dot", fill=C["green"])
            self.running_label.configure(text=f"Running — {self.engine.status}", fg=C["green"])

            # Connection indicator
            if self.engine.connected:
                self.conn_dot.itemconfigure("dot", fill=C["green"])
                self.conn_label.configure(text="Connected", fg=C["green"])
            else:
                self.conn_dot.itemconfigure("dot", fill=C["yellow"])
                self.conn_label.configure(text="Connecting...", fg=C["yellow"])

            # Current job
            if self.engine.current_job:
                self.job_label.configure(text=f"▶ {self.engine.current_job}", fg=C["blue"])
            else:
                self.job_label.configure(text="")

            self.start_btn.configure(text="Stop", bg=C["red"])
        else:
            self.running_dot.itemconfigure("dot", fill=C["red"])
            self.running_label.configure(text="Stopped", fg=C["red"])
            self.conn_dot.itemconfigure("dot", fill=C["yellow"])
            self.conn_label.configure(text="Disconnected", fg=C["yellow"])
            self.job_label.configure(text="")
            self.start_btn.configure(text="Start", bg=C["green"])

        self.root.after(1000, self._update_status)

    # ─── Self-update (v2.11) ───
    def _check_update_bg(self):
        """Check GitHub for a newer node version off the UI thread."""
        def work():
            u = check_for_update()
            if u and u.get("available"):
                self.root.after(0, lambda: self._show_update_banner(u))
        threading.Thread(target=work, daemon=True).start()

    def _show_update_banner(self, u):
        self._update_info = u
        notes = (u.get("notes") or "").strip()
        if len(notes) > 90:
            notes = notes[:90] + "…"
        self.update_label.configure(
            text=f"Update available:  v{u['current']} → v{u['latest']}" + (f"   —   {notes}" if notes else ""))
        # show the bar just under the header
        self.update_bar.pack(fill="x", after=self.root.winfo_children()[0])

    def _do_update(self):
        running = bool(self.engine and getattr(self.engine, "running", False))
        msg = "Download the latest node files from GitHub and restart the node?"
        if running:
            msg += ("\n\nThe node is currently RUNNING. It will stop and restart — "
                    "any in-progress transcode will be requeued on the server. "
                    "Best to update when idle.")
        if not messagebox.askyesno("Update Byte Node", msg):
            return
        self.update_btn.configure(state="disabled", text="Updating…")
        self.stop_engine()

        def work():
            ok, msgs = download_update(log_fn=lambda m: self.log_queue.put(
                f"[{time.strftime('%H:%M:%S')}] [INFO] {m}"))
            self.root.after(0, lambda: self._after_update(ok, msgs))
        threading.Thread(target=work, daemon=True).start()

    def _after_update(self, ok, msgs):
        if not ok:
            self.update_btn.configure(state="normal", text="Update Now")
            messagebox.showerror("Update failed",
                                 "Some files could not be downloaded. Your old files were kept "
                                 "(.bak backups exist).\n\n" + "\n".join(msgs[-6:]))
            return
        if messagebox.askyesno("Update complete",
                               "The node was updated. Restart now to run the new version?\n\n"
                               "(If new tools were added, run 'py setup_tools.py' afterward.)"):
            try:
                self.root.destroy()
            except Exception:
                pass
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            self.update_bar.pack_forget()

    def _on_exit(self):
        self.stop_engine()
        self.root.after(300, self.root.destroy)

    def run(self):
        self.root.mainloop()


# ─── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ByteNodeGUI()
    app.run()
