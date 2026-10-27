# -*- coding: utf-8 -*-
"""日语字幕生成器 - tkinter 图形界面。"""
import json
import os
import queue
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import audio, asr, srt_builder, translator
from .config import load_settings, save_settings

APP_TITLE = "日语字幕生成器 (Whisper 识别 + 大模型翻译 → SRT)"

VIDEO_TYPES = [
    ("媒体文件", "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.mpg *.mpeg *.ts "
               "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.wma"),
    ("所有文件", "*.*"),
]


class SubtitleApp:
    def __init__(self, root):
        self.root = root
        self.cfg = load_settings()
        self.queue_items = []          # 待处理视频路径列表
        self.segments = []             # [{start,end,text,zh}]
        self.current_video = None
        self.worker = None
        self.evq = queue.Queue()
        self.busy = False

        root.title(APP_TITLE)
        root.geometry("1100x800")
        root.minsize(960, 700)
        self._build_ui()
        self.root.after(100, self._poll_events)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ============================================================ UI
    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)   # 片段表格
        self.root.rowconfigure(5, weight=1)   # 日志

        # ---- 队列与操作按钮 ----
        f_top = ttk.LabelFrame(self.root, text="待处理影片队列（支持批量，可拖入 mp4/mkv/avi/mp3 等）")
        f_top.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))
        f_top.columnconfigure(1, weight=1)

        self.lb_queue = tk.Listbox(f_top, height=6, selectmode="extended",
                                   font=("Consolas", 10))
        self.lb_queue.grid(row=0, column=0, columnspan=5, sticky="nsew",
                           padx=6, pady=4)

        ttk.Button(f_top, text="添加影片", command=self._add_files).grid(
            row=1, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(f_top, text="移除选中", command=self._remove_selected).grid(
            row=1, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(f_top, text="清空队列", command=self._clear_queue).grid(
            row=1, column=2, padx=4, pady=4, sticky="ew")

        self.lbl_video_info = ttk.Label(f_top, text="尚未选择影片", foreground="#666")
        self.lbl_video_info.grid(row=2, column=0, columnspan=5, sticky="w", padx=6)

        # ---- 选项页 ----
        nb = ttk.Notebook(self.root)
        nb.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self._build_tab_asr(nb)
        self._build_tab_llm(nb)
        self._build_tab_export(nb)

        # ---- 片段预览 ----
        f_seg = ttk.LabelFrame(self.root, text="字幕片段预览（双击某行可编辑日文/中文）")
        f_seg.grid(row=2, column=0, sticky="nsew", padx=8, pady=4)
        f_seg.columnconfigure(0, weight=1)
        f_seg.rowconfigure(0, weight=1)

        cols = ("no", "start", "end", "ja", "zh")
        self.tree = ttk.Treeview(f_seg, columns=cols, show="headings",
                                 selectmode="browse")
        heads = {"no": ("#", 45), "start": ("开始", 100), "end": ("结束", 100),
                 "ja": ("日文", 360), "zh": ("中文", 360)}
        for c in cols:
            text, w = heads[c]
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor="w")
        vs = ttk.Scrollbar(f_seg, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self._open_edit_dialog)

        # ---- 进度与状态 ----
        f_prog = ttk.Frame(self.root)
        f_prog.grid(row=3, column=0, sticky="ew", padx=8, pady=(2, 0))
        f_prog.columnconfigure(1, weight=1)
        self.var_stage = tk.StringVar(value="就绪")
        ttk.Label(f_prog, textvariable=self.var_stage, width=22, anchor="w").grid(
            row=0, column=0, sticky="w")
        self.pbar = ttk.Progressbar(f_prog, mode="determinate", maximum=100)
        self.pbar.grid(row=0, column=1, sticky="ew", padx=6)

        # ---- 操作按钮 ----
        f_btn = ttk.Frame(self.root)
        f_btn.grid(row=4, column=0, sticky="ew", padx=8, pady=6)
        self.btn_run = ttk.Button(f_btn, text="开始生成（识别+翻译+导出）",
                                  command=lambda: self._start_batch("full"))
        self.btn_run.pack(side="left", padx=4)
        self.btn_asr = ttk.Button(f_btn, text="仅识别（生成日文）",
                                  command=lambda: self._start_batch("asr"))
        self.btn_asr.pack(side="left", padx=4)
        self.btn_tr = ttk.Button(f_btn, text="仅翻译当前片段",
                                command=self._translate_current)
        self.btn_tr.pack(side="left", padx=4)
        self.btn_exp = ttk.Button(f_btn, text="导出 SRT",
                                 command=self._export_current)
        self.btn_exp.pack(side="left", padx=4)
        self.btn_open = ttk.Button(f_btn, text="打开输出目录",
                                   command=self._open_output_dir)
        self.btn_open.pack(side="left", padx=4)

        # ---- 日志 ----
        f_log = ttk.LabelFrame(self.root, text="日志")
        f_log.grid(row=5, column=0, sticky="nsew", padx=8, pady=(0, 8))
        f_log.columnconfigure(0, weight=1)
        f_log.rowconfigure(0, weight=1)
        self.txt_log = scrolledtext.ScrolledText(f_log, height=8, state="disabled",
                                                 font=("Consolas", 9), wrap="word")
        self.txt_log.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        self._sync_widgets_from_cfg()

    # ---------------- 识别设置页 ----------------
    def _build_tab_asr(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="识别设置")
        t.columnconfigure(1, weight=1)

        self.var_asr_engine = tk.StringVar()
        self.var_model = tk.StringVar()
        self.var_device = tk.StringVar()
        self.var_lang = tk.StringVar()
        self.var_vad = tk.BooleanVar()
        self.var_vad_thr = tk.DoubleVar()
        self.var_norm = tk.StringVar()
        self.var_cond = tk.BooleanVar()
        self.var_beam = tk.IntVar()
        self.var_cache = tk.BooleanVar()

        r = 0
        ttk.Label(t, text="识别引擎:").grid(row=r, column=0, sticky="w", pady=2)
        self.combo_engine = ttk.Combobox(t, textvariable=self.var_asr_engine,
                                         state="readonly", width=26,
                                         values=["local", "api"])
        self.combo_engine.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(t, text="local=本地Whisper大模型；api=OpenAI兼容云端转写接口",
                  foreground="#888").grid(row=r, column=2, sticky="w", padx=8)

        r += 1
        ttk.Label(t, text="Whisper模型 / API模型名:").grid(row=r, column=0, sticky="w", pady=2)
        self.combo_model = ttk.Combobox(t, textvariable=self.var_model, width=26,
                                        values=asr.MODEL_CHOICES)
        self.combo_model.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(t, text="本地首次运行自动下载（默认走 hf-mirror 镜像）",
                  foreground="#888").grid(row=r, column=2, sticky="w", padx=8)

        r += 1
        ttk.Label(t, text="计算设备:").grid(row=r, column=0, sticky="w", pady=2)
        self.combo_device = ttk.Combobox(t, textvariable=self.var_device,
                                         state="readonly", width=26,
                                         values=["auto", "cuda", "cpu"])
        self.combo_device.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(t, text="auto=有N卡用CUDA，否则CPU", foreground="#888").grid(
            row=r, column=2, sticky="w", padx=8)

        r += 1
        ttk.Label(t, text="源语言:").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(t, textvariable=self.var_lang, width=28).grid(
            row=r, column=1, sticky="w", pady=2)
        ttk.Label(t, text="日语 ja；自动检测留 auto", foreground="#888").grid(
            row=r, column=2, sticky="w", padx=8)

        r += 1
        ttk.Checkbutton(t, text="启用 VAD 静音过滤（跳过无人声片段）",
                        variable=self.var_vad).grid(row=r, column=0, columnspan=2,
                                                    sticky="w", pady=2)
        ttk.Label(t, text="VAD 阈值:").grid(row=r, column=2, sticky="w")
        ttk.Scale(t, from_=0.1, to=0.9, variable=self.var_vad_thr,
                  orient="horizontal", length=120,
                  command=lambda _: self.lbl_vad_val.config(
                      text=f"{self.var_vad_thr.get():.2f}")).grid(row=r, column=3, sticky="w")
        self.lbl_vad_val = ttk.Label(t, text="0.35", width=5)
        self.lbl_vad_val.grid(row=r, column=4, sticky="w")

        r += 1
        ttk.Label(t, text="音频归一化:").grid(row=r, column=0, sticky="w", pady=2)
        self.combo_norm = ttk.Combobox(t, textvariable=self.var_norm, width=26,
                                       state="readonly",
                                       values=["dynaudnorm", "loudnorm", "none"])
        self.combo_norm.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(t, text="音量低/识别不出时调 loudnorm 或 none 试试",
                  foreground="#888").grid(row=r, column=2, sticky="w", padx=8)

        r += 1
        ttk.Checkbutton(t, text="使用上文提示识别（更连贯，个别低信噪比视频可能幻觉循环）",
                        variable=self.var_cond).grid(row=r, column=0, columnspan=2,
                                                     sticky="w", pady=2)
        ttk.Label(t, text="Beam Size:").grid(row=r, column=2, sticky="w")
        ttk.Spinbox(t, from_=1, to=10, textvariable=self.var_beam, width=6).grid(
            row=r, column=3, sticky="w")

        r += 1
        ttk.Checkbutton(t, text="优先使用已有识别缓存 <影片>.ja.json（跳过识别）",
                        variable=self.var_cache).grid(row=r, column=0, columnspan=2,
                                                      sticky="w", pady=2)

    # ---------------- 翻译设置页 ----------------
    def _build_tab_llm(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="翻译设置")
        t.columnconfigure(1, weight=1)

        self.var_base_url = tk.StringVar()
        self.var_model_name = tk.StringVar()
        self.var_api_key = tk.StringVar()
        self.var_temp = tk.DoubleVar()
        self.var_timeout = tk.IntVar()
        self.var_batch = tk.BooleanVar()
        self.var_batch_chars = tk.IntVar()
        self.var_batch_lines = tk.IntVar()
        self.var_skip = tk.BooleanVar()

        r = 0
        ttk.Label(t, text="API 地址:").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(t, textvariable=self.var_base_url, width=46).grid(
            row=r, column=1, sticky="ew", pady=3)
        ttk.Label(t, text="LM Studio: http://127.0.0.1:1234/v1\nOllama: http://127.0.0.1:11434/v1\nDeepSeek/通义等云端服务填对应 /v1 地址",
                  foreground="#888", justify="left").grid(row=r, column=2, sticky="w", padx=8, rowspan=3)

        r += 1
        ttk.Label(t, text="翻译模型 ID:").grid(row=r, column=0, sticky="w", pady=3)
        f = ttk.Frame(t)
        f.grid(row=r, column=1, sticky="ew", pady=3)
        f.columnconfigure(0, weight=1)
        ttk.Entry(f, textvariable=self.var_model_name, width=38).grid(
            row=0, column=0, sticky="ew")
        ttk.Button(f, text="检测在线模型", command=self._detect_model).grid(
            row=0, column=1, padx=4)

        r += 1
        ttk.Label(t, text="API Key:").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(t, textvariable=self.var_api_key, width=46, show="*").grid(
            row=r, column=1, sticky="ew", pady=3)

        r += 1
        ttk.Label(t, text="Temperature:").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Scale(t, from_=0.0, to=1.0, variable=self.var_temp, orient="horizontal",
                  length=180,
                  command=lambda _: self.lbl_temp_val.config(
                      text=f"{self.var_temp.get():.2f}")).grid(row=r, column=1, sticky="w")
        self.lbl_temp_val = ttk.Label(t, text="0.30", width=5)
        self.lbl_temp_val.grid(row=r, column=2, sticky="w")

        r += 1
        ttk.Label(t, text="请求超时(秒):").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Spinbox(t, from_=30, to=900, increment=10, textvariable=self.var_timeout,
                    width=10).grid(row=r, column=1, sticky="w", pady=3)

        r += 1
        ttk.Checkbutton(t, text="批量翻译（更快，推荐）", variable=self.var_batch).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(t, text="每批最大字符:").grid(row=r, column=2, sticky="w")
        ttk.Entry(t, textvariable=self.var_batch_chars, width=8).grid(row=r, column=3, sticky="w")
        ttk.Label(t, text="行数:").grid(row=r, column=4, sticky="w", padx=(8, 0))
        ttk.Entry(t, textvariable=self.var_batch_lines, width=8).grid(row=r, column=5, sticky="w")

        r += 1
        ttk.Checkbutton(t, text="跳过翻译（只出日文）", variable=self.var_skip).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=3)

    # ---------------- 导出设置页 ----------------
    def _build_tab_export(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="导出设置")
        t.columnconfigure(1, weight=1)

        self.var_mode = tk.StringVar()
        self.var_export_dir = tk.StringVar()
        self.var_suffix = tk.StringVar()
        self.var_auto = tk.BooleanVar()

        r = 0
        ttk.Label(t, text="导出格式:").grid(row=r, column=0, sticky="w", pady=3)
        self.combo_mode = ttk.Combobox(t, textvariable=self.var_mode, state="readonly",
                                       width=26,
                                       values=["bilingual", "zh", "ja", "separate"])
        self.combo_mode.grid(row=r, column=1, sticky="w", pady=3)
        ttk.Label(t, text="bilingual=中文上/日文下单文件；zh/ja=单语；separate=日文、中文各一个文件",
                  foreground="#888").grid(row=r, column=2, sticky="w", padx=8)

        r += 1
        ttk.Label(t, text="输出目录:").grid(row=r, column=0, sticky="w", pady=3)
        f = ttk.Frame(t)
        f.grid(row=r, column=1, sticky="ew", pady=3)
        f.columnconfigure(0, weight=1)
        ttk.Entry(f, textvariable=self.var_export_dir, width=46).grid(row=0, column=0, sticky="ew")
        ttk.Button(f, text="浏览…", command=self._browse_export_dir).grid(row=0, column=1, padx=4)
        ttk.Button(f, text="清空(同影片目录)", command=lambda: self.var_export_dir.set("")).grid(
            row=0, column=2, padx=4)

        r += 1
        ttk.Label(t, text="文件名后缀:").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(t, textvariable=self.var_suffix, width=26).grid(row=r, column=1, sticky="w", pady=3)
        ttk.Label(t, text="留空时双语字幕与影片同名（.srt），PotPlayer 自动加载",
                  foreground="#888").grid(row=r, column=2, sticky="w", padx=8)

        r += 1
        ttk.Checkbutton(t, text="处理完成后自动导出 SRT", variable=self.var_auto).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=3)

    # ============================================================ 事件
    def _sync_widgets_from_cfg(self):
        c = self.cfg
        self.var_asr_engine.set(c["asr_engine"])
        self.var_model.set(c["whisper_model"])
        self.var_device.set(c["device"])
        self.var_lang.set(c["language"])
        self.var_vad.set(c["use_vad"])
        self.var_vad_thr.set(c["vad_threshold"])
        self.var_norm.set(c["normalize"])
        self.var_cond.set(c["condition_on_previous_text"])
        self.var_beam.set(c["beam_size"])
        self.var_cache.set(c["use_cache"])
        self.var_base_url.set(c["llm_base_url"])
        self.var_model_name.set(c["llm_model"])
        self.var_api_key.set(c["llm_api_key"])
        self.var_temp.set(c["temperature"])
        self.var_timeout.set(c["llm_timeout"])
        self.var_batch.set(c["batch_translate"])
        self.var_batch_chars.set(c["max_batch_chars"])
        self.var_batch_lines.set(c["max_batch_lines"])
        self.var_skip.set(c["skip_translate"])
        self.var_mode.set(c["export_mode"])
        self.var_export_dir.set(c["export_dir"])
        self.var_suffix.set(c["output_suffix"])
        self.var_auto.set(c["auto_export"])
        self.lbl_vad_val.config(text=f"{c['vad_threshold']:.2f}")
        self.lbl_temp_val.config(text=f"{c['temperature']:.2f}")

    def _read_widgets(self):
        c = self.cfg
        c["asr_engine"] = self.var_asr_engine.get().strip()
        c["whisper_model"] = self.var_model.get().strip() or "large-v3"
        c["device"] = self.var_device.get().strip() or "auto"
        c["language"] = self.var_lang.get().strip() or "ja"
        c["use_vad"] = bool(self.var_vad.get())
        c["vad_threshold"] = self._get_num(self.var_vad_thr, 0.35)
        c["normalize"] = self.var_norm.get().strip() or "dynaudnorm"
        c["condition_on_previous_text"] = bool(self.var_cond.get())
        c["beam_size"] = int(self._get_num(self.var_beam, 5))
        c["use_cache"] = bool(self.var_cache.get())
        c["llm_base_url"] = self.var_base_url.get().strip()
        c["llm_model"] = self.var_model_name.get().strip()
        c["llm_api_key"] = self.var_api_key.get().strip()
        c["temperature"] = self._get_num(self.var_temp, 0.3)
        c["llm_timeout"] = int(self._get_num(self.var_timeout, 180))
        c["batch_translate"] = bool(self.var_batch.get())
        c["max_batch_chars"] = int(self._get_num(self.var_batch_chars, 1500))
        c["max_batch_lines"] = int(self._get_num(self.var_batch_lines, 60))
        c["skip_translate"] = bool(self.var_skip.get())
        c["export_mode"] = self.var_mode.get().strip() or "bilingual"
        c["export_dir"] = self.var_export_dir.get().strip()
        c["output_suffix"] = self.var_suffix.get().strip()
        c["auto_export"] = bool(self.var_auto.get())
        return c

    @staticmethod
    def _get_num(var, default):
        try:
            v = var.get()
            if v in (None, ""):
                return default
            return float(v)
        except Exception:
            return default

    def _add_files(self):
        files = filedialog.askopenfilenames(title="选择影片/音频",
                                            filetypes=VIDEO_TYPES,
                                            initialdir=self.cfg.get("last_dir") or None)
        if not files:
            return
        seen = set(self.queue_items)
        for fp in files:
            if fp not in seen:
                self.queue_items.append(fp)
                seen.add(fp)
        self.cfg["last_dir"] = os.path.dirname(files[0])
        self._refresh_queue()

    def _remove_selected(self):
        sel = list(self.lb_queue.curselection())
        for i in sorted(sel, reverse=True):
            del self.queue_items[i]
        self._refresh_queue()

    def _clear_queue(self):
        self.queue_items = []
        self._refresh_queue()

    def _refresh_queue(self):
        self.lb_queue.delete(0, "end")
        for p in self.queue_items:
            self.lb_queue.insert("end", p)
        self.lbl_video_info.config(
            text=f"队列 {len(self.queue_items)} 个" if self.queue_items else "尚未选择影片")

    def _detect_model(self):
        if self.busy:
            return
        self._read_widgets()
        base = self.cfg["llm_base_url"]
        client = translator.LLMClient(base, self.cfg["llm_model"], self.cfg["llm_api_key"],
                                      self.cfg["temperature"], self.cfg["llm_timeout"])
        self._log(f"[*] 正在探测 {base} ...")
        online, model, err = client.check_online()
        if not online:
            self._log(f"[!] {err}")
            messagebox.showwarning("翻译服务不可用", f"{err}\n\n请确认：\n"
                                  "1) LM Studio 已启动并开启 Local Server（端口 1234）；\n"
                                  "2) 已加载一个适合翻译的模型；\n"
                                  "3) 或改用 Ollama / DeepSeek / 通义 等 OpenAI 兼容服务。")
            return
        if model:
            self.var_model_name.set(model)
            self.cfg["llm_model"] = model
            save_settings(self.cfg)
            self._log(f"[OK] 已自动选择模型: {model}")
        else:
            self._log("[!] 服务在线但未找到合适模型")

    def _browse_export_dir(self):
        d = filedialog.askdirectory(title="选择字幕输出目录",
                                    initialdir=self.var_export_dir.get() or None)
        if d:
            self.var_export_dir.set(d)

    # ============================================================ 日志/进度
    def _log(self, msg):
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def _post(self, ev):
        self.evq.put(ev)

    def _poll_events(self):
        try:
            while True:
                ev = self.evq.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._log(ev[1])
                elif kind == "stage":
                    self.var_stage.set(ev[1])
                elif kind == "prog":
                    self.pbar["value"] = ev[1]
                elif kind == "info":
                    self.lbl_video_info.config(text=ev[1])
                elif kind == "video":
                    self.current_video = ev[1]
                    self.lbl_video_info.config(
                        text=os.path.basename(ev[1]) + "  ← 正在处理")
                elif kind == "segments":
                    self.segments = ev[1]
                    self._refresh_tree()
                elif kind == "model":
                    self.var_model_name.set(ev[1])
                    self.cfg["llm_model"] = ev[1]
                    save_settings(self.cfg)
                elif kind == "busy":
                    self._set_busy(ev[1])
                elif kind == "done":
                    self.var_stage.set(ev[1])
                    self.pbar["value"] = 100
                elif kind == "err":
                    self._log("[X] 发生错误：\n" + ev[1])
                    self.var_stage.set("处理失败，详见日志")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_run, self.btn_asr, self.btn_tr, self.btn_exp):
            b.config(state=state)
        if busy:
            self.pbar["value"] = 0

    # ============================================================ 表格
    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.segments, 1):
            self.tree.insert("", "end", iid=str(i), values=(
                i,
                srt_builder.format_timestamp(s["start"]),
                srt_builder.format_timestamp(s["end"]),
                s.get("text", ""),
                s.get("zh", ""),
            ))

    def _open_edit_dialog(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0]) - 1
        if not (0 <= idx < len(self.segments)):
            return
        seg = self.segments[idx]

        dlg = tk.Toplevel(self.root)
        dlg.title(f"编辑第 {idx + 1} 段字幕")
        dlg.geometry("640x300")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text=f"时间段: {srt_builder.format_timestamp(seg['start'])} --> "
                            f"{srt_builder.format_timestamp(seg['end'])}").pack(
            anchor="w", padx=10, pady=(10, 2))
        ttk.Label(dlg, text="日文:").pack(anchor="w", padx=10)
        t_ja = tk.Text(dlg, height=3, font=("Microsoft YaHei UI", 11))
        t_ja.pack(fill="x", padx=10, pady=2)
        t_ja.insert("1.0", seg.get("text", ""))
        ttk.Label(dlg, text="中文:").pack(anchor="w", padx=10)
        t_zh = tk.Text(dlg, height=3, font=("Microsoft YaHei UI", 11))
        t_zh.pack(fill="x", padx=10, pady=2)
        t_zh.insert("1.0", seg.get("zh", ""))

        def save():
            seg["text"] = t_ja.get("1.0", "end").strip()
            seg["zh"] = t_zh.get("1.0", "end").strip()
            self._refresh_tree()
            dlg.destroy()

        f = ttk.Frame(dlg)
        f.pack(pady=8)
        ttk.Button(f, text="保存", command=save).pack(side="left", padx=6)
        ttk.Button(f, text="取消", command=dlg.destroy).pack(side="left", padx=6)

    # ============================================================ 工作线程
    def _start_batch(self, action):
        if self.busy:
            return
        self._read_widgets()
        save_settings(self.cfg)
        if not self.queue_items:
            messagebox.showinfo("提示", "请先添加影片到队列")
            return
        self._set_busy(True)
        videos = list(self.queue_items)
        self.worker = threading.Thread(target=self._worker_run,
                                       args=(videos, action), daemon=True)
        self.worker.start()

    def _worker_run(self, videos, action):
        try:
            for v in videos:
                self._post(("video", v))
                self._process_one(v, action)
            self._post(("done", "全部完成"))
        except Exception:
            self._post(("err", traceback.format_exc()))
        finally:
            self._post(("busy", False))

    def _process_one(self, video, action):
        c = self.cfg
        self._post(("log", f"\n========== 处理: {os.path.basename(video)} =========="))
        dur = audio.probe_duration(video)
        if dur:
            self._post(("log", f"[*] 影片时长: {int(dur // 60)} 分 {int(dur % 60)} 秒"))

        cache_path = os.path.splitext(video)[0] + ".ja.json"
        segments = None

        # ---------- 1. 识别（或读缓存） ----------
        if action == "full" and c["use_cache"] and os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    segments = json.load(f)
                self._post(("log", f"[*] 已从缓存加载 {len(segments)} 段识别结果"))
            except Exception as e:
                self._post(("log", f"[!] 缓存读取失败，重新识别: {e}"))
                segments = None

        if segments is None:
            segments = self._do_asr(video)
            if segments is None:
                self._post(("log", "[X] 识别失败，跳过该文件"))
                return
            if c["use_cache"]:
                try:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(segments, f, ensure_ascii=False, indent=2)
                    self._post(("log", f"[*] 识别结果已缓存: {cache_path}"))
                except Exception as e:
                    self._post(("log", f"[!] 缓存写入失败: {e}"))

        self._post(("segments", segments))

        # ---------- 2. 翻译 ----------
        if action == "full" and not c["skip_translate"]:
            segments = self._do_translate(video, segments)
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(segments, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        # ---------- 3. 导出 ----------
        if action == "full" and c["auto_export"] and segments:
            self._do_export(segments, video)

    def _do_asr(self, video):
        c = self.cfg
        self._post(("stage", "提取音频…"))
        self._post(("prog", 2))
        try:
            if c["asr_engine"] == "api":
                import tempfile
                tmp_wav = os.path.join(tempfile.gettempdir(),
                                       "jp_sub_%s.wav" % os.path.basename(video).replace(".", "_"))
                audio.decode_to_wav(video, tmp_wav, normalize=c["normalize"])
                self._post(("log", f"[*] 音频已转码: {tmp_wav}"))
                self._post(("stage", "API 转写中…"))
                self._post(("prog", 10))
                eng = asr.ApiTranscriber(c["llm_base_url"], c["llm_api_key"],
                                         c["whisper_model"])
                segs, info = eng.transcribe(tmp_wav, language=c["language"])
                self._post(("log", f"[*] API 转写完成: {len(segs)} 段"))
                return segs

            # 本地 Whisper
            audio_pcm = audio.decode_audio_pcm(video, normalize=c["normalize"])
            self._post(("log", f"[*] 音频解码完成: {len(audio_pcm) / 16000:.0f} 秒"))
            self._post(("stage", "加载 Whisper 模型并识别…"))
            self._post(("prog", 5))
            eng = asr.LocalWhisper(c["whisper_model"], c["device"])
            def prog(i, end_sec, total_sec):
                pct = 8 + int(47 * (end_sec / total_sec)) if total_sec else 8
                self._post(("prog", min(pct, 55)))
                self._post(("stage", f"识别中… {i} 段 ({int(end_sec)}s / {int(total_sec)}s)"))
            segs, info = eng.transcribe(
                audio_pcm, language=c["language"], vad=c["use_vad"],
                vad_threshold=c["vad_threshold"],
                condition_on_previous_text=c["condition_on_previous_text"],
                beam_size=c["beam_size"], progress=prog)
            lang = getattr(info, "language", "?")
            self._post(("log", f"[*] 识别完成: {len(segs)} 段，检测语言={lang}"))
            return segs
        except Exception as e:
            self._post(("log", f"[!] 识别出错: {e}"))
            return None

    def _do_translate(self, video, segments):
        c = self.cfg
        self._post(("stage", "准备翻译…"))
        self._post(("prog", 55))
        client = translator.LLMClient(c["llm_base_url"], c["llm_model"],
                                      c["llm_api_key"], c["temperature"],
                                      c["llm_timeout"])
        if not c["llm_model"]:
            online, model, err = client.check_online()
            if not online:
                self._post(("log", f"[!] 无法连接翻译服务: {err}"))
                self._post(("log", "[!] 已跳过翻译，可稍后点击『仅翻译当前片段』重试"))
                return segments
            if model:
                self._post(("model", model))
                self._post(("log", f"[*] 自动选择模型: {model}"))
            else:
                self._post(("log", "[!] 未找到合适模型，跳过翻译"))
                return segments

        texts = [s.get("text", "") for s in segments]
        if c["batch_translate"]:
            batches = self._make_batches(texts, c["max_batch_chars"],
                                         c["max_batch_lines"])
            self._post(("log", f"[*] 开始批量翻译: {len(texts)} 段 / {len(batches)} 批"))
            done, total = 0, len(texts)
            for b in batches:
                out, err = translator.translate_with_retry(client, b)
                if err:
                    self._post(("log", f"[!] 一批翻译失败: {err}"))
                for i, txt in enumerate(out):
                    segments[done + i]["zh"] = txt
                done += len(b)
                self._post(("prog", min(55 + int(40 * done / total), 95)))
                self._post(("stage", f"翻译中… {done}/{total} 段"))
        else:
            self._post(("log", f"[*] 开始逐条翻译: {len(texts)} 段"))
            for i, s in enumerate(segments, 1):
                out, err = translator.translate_with_retry(client, [s.get("text", "")])
                s["zh"] = out[0] if out else ""
                if err:
                    self._post(("log", f"[!] 第 {i} 段翻译失败: {err}"))
                self._post(("prog", min(55 + int(40 * i / len(texts)), 95)))
                self._post(("stage", f"翻译中… {i}/{len(texts)} 段"))
        self._post(("log", "[OK] 翻译完成"))
        self._post(("segments", segments))
        return segments

    @staticmethod
    def _make_batches(texts, max_chars=None, max_lines=None):
        """按字符数/行数把文本切成批。"""
        max_chars = max_chars or 1500
        max_lines = max_lines or 60
        batches, cur, cur_len = [], [], 0
        for t in texts:
            L = len(t) + 1
            if cur and (cur_len + L > max_chars or len(cur) >= max_lines):
                batches.append(cur)
                cur, cur_len = [], 0
            cur.append(t)
            cur_len += L
        if cur:
            batches.append(cur)
        return batches

    def _translate_current(self):
        if self.busy:
            return
        self._read_widgets()
        save_settings(self.cfg)
        if not self.segments:
            if self.current_video:
                cache = os.path.splitext(self.current_video)[0] + ".ja.json"
                if os.path.exists(cache):
                    try:
                        with open(cache, "r", encoding="utf-8") as f:
                            self.segments = json.load(f)
                        self._refresh_tree()
                        self._log(f"[*] 已从缓存加载 {len(self.segments)} 段")
                    except Exception as e:
                        self._log(f"[!] 缓存读取失败: {e}")
                        return
                else:
                    messagebox.showinfo("提示", "当前没有可翻译的片段，请先『仅识别』")
                    return
            else:
                messagebox.showinfo("提示", "当前没有可翻译的片段，请先添加影片并识别")
                return
        self._set_busy(True)
        video = self.current_video or (self.queue_items[0] if self.queue_items else "")
        self.worker = threading.Thread(target=self._worker_translate_only,
                                       args=(video, list(self.segments)), daemon=True)
        self.worker.start()

    def _worker_translate_only(self, video, segments):
        try:
            out = self._do_translate(video, segments)
            self._post(("segments", out))
            self._post(("done", "翻译完成"))
        except Exception:
            self._post(("err", traceback.format_exc()))
        finally:
            self._post(("busy", False))

    # ============================================================ 导出
    def _export_current(self):
        if self.busy:
            return
        self._read_widgets()
        save_settings(self.cfg)
        if not self.segments:
            messagebox.showinfo("提示", "没有可导出的片段")
            return
        video = self.current_video or (self.queue_items[0] if self.queue_items else "")
        paths = self._do_export(self.segments, video)
        if paths:
            messagebox.showinfo("导出完成", "已导出：\n" + "\n".join(paths))

    def _do_export(self, segments, video):
        c = self.cfg
        mode = c["export_mode"]
        if not video:
            video = os.path.join(c["export_dir"] or ".", "字幕.mp4")
        try:
            paths = srt_builder.default_output_paths(video, mode, c["output_suffix"])
        except Exception:
            paths = []
        out_paths = []
        for i, p in enumerate(paths):
            if c["export_dir"]:
                p = os.path.join(c["export_dir"], os.path.basename(p))
            seg_mode = mode if mode != "separate" else ("zh" if i == 0 else "ja")
            try:
                srt_builder.export_srt(segments, p, seg_mode)
                out_paths.append(p)
                self._post(("log", f"[OK] 已导出: {p}"))
            except Exception as e:
                self._post(("log", f"[X] 导出失败 {p}: {e}"))
        self._post(("prog", 100))
        return out_paths

    def _open_output_dir(self):
        d = self.cfg.get("export_dir") or (os.path.dirname(self.current_video)
                                           if self.current_video else None)
        if not d or not os.path.isdir(d):
            d = self.cfg.get("last_dir") or os.getcwd()
        try:
            os.startfile(d)
        except Exception as e:
            self._log(f"[!] 打开目录失败: {e}")

    def _on_close(self):
        try:
            self._read_widgets()
            save_settings(self.cfg)
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    SubtitleApp(root)
    root.mainloop()
