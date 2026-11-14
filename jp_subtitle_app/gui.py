# -*- coding: utf-8 -*-
"""日语字幕生成器 - tkinter 图形界面。"""
import json
import os
import queue
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import audio, asr, review, srt_builder, translator
from .config import PRESET_DEFAULT, PRESETS, load_settings, save_settings

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
        # 高度按屏幕自适应，避免日志区被挤出屏幕外
        try:
            sh = root.winfo_screenheight()
            h = max(720, min(900, sh - 120))
        except Exception:
            h = 860
        root.geometry(f"1180x{h}")
        root.minsize(1020, 680)
        self._build_ui()
        self.root.after(120, self._log_startup_hint)
        self.root.after(100, self._poll_events)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ============================================================ UI
    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)   # 中部 PanedWindow（片段+日志）

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
        self._build_tab_help(nb)
        self._build_tab_llm(nb)
        self._build_tab_review(nb)
        self._build_tab_export(nb)

        # ---- 中部：片段预览 + 日志（纵向 PanedWindow，可拖拽分隔条调节） ----
        self.pw = ttk.PanedWindow(self.root, orient="vertical")
        self.pw.grid(row=2, column=0, sticky="nsew", padx=8, pady=4)

        # ---- 片段预览 ----
        f_seg = ttk.LabelFrame(self.pw, text="字幕片段预览（双击某行可编辑日文/中文）")
        self.pw.add(f_seg)
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
        self.btn_review = ttk.Button(f_btn, text="审查并修正字幕（音轨做参照）",
                                     command=self._review_current)
        self.btn_review.pack(side="left", padx=4)
        self.btn_exp = ttk.Button(f_btn, text="导出 SRT",
                                 command=self._export_current)
        self.btn_exp.pack(side="left", padx=4)
        self.btn_open = ttk.Button(f_btn, text="打开输出目录",
                                   command=self._open_output_dir)
        self.btn_open.pack(side="left", padx=4)

        # ---- 日志（PanedWindow 下半区，初始 256px，可拖拽调节） ----
        f_log = ttk.LabelFrame(self.pw, text="日志（拖拽上下两个区域之间的分隔条可自由调节高度）")
        self.pw.add(f_log)
        f_log.columnconfigure(0, weight=1)
        f_log.rowconfigure(0, weight=1)
        self.txt_log = scrolledtext.ScrolledText(f_log, height=12, state="disabled",
                                                 font=("Consolas", 9), wrap="word")
        self.txt_log.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        self._sync_widgets_from_cfg()
        self.root.after(200, self._set_initial_split)

    def _set_initial_split(self):
        """初始化分隔条位置：日志区高度约 256px。"""
        try:
            h = self.pw.winfo_height()
            if h > 200:
                self.pw.sashpos(0, max(120, h - 266))
        except Exception:
            pass

    def _log_startup_hint(self):
        """启动时打印当前识别参数，并对可能削弱轻声识别的配置给出提示。"""
        c = self.cfg
        self._log("[*] 当前识别参数："
                  f"VAD {c.get('vad_threshold', 0.25):.2f}"
                  f"（最短静音 {c.get('vad_min_silence_ms', 400)}ms / "
                  f"留白 {c.get('vad_speech_pad_ms', 300)}ms）"
                  f" · no_speech {c.get('no_speech_threshold', 0.75):.2f}"
                  f" · 置信度 {c.get('log_prob_threshold', -1.2):.2f}"
                  f" · 增益 {c.get('gain_db', 0):+g}dB"
                  f" · 降噪 {'开' if c.get('denoise') else '关'}"
                  f" · 归一化 {c.get('normalize')}"
                  f" · beam {c.get('beam_size')}"
                  f" · 日语提示 {'开' if c.get('japanese_prompt') else '关'}")
        if c.get("normalize") == "none":
            self._log("[!] 音频归一化为 none：轻声对话不会被抬升，小声影片容易漏识别。"
                      "建议改为 dynaudnorm，或在「识别设置」点“轻声 / 含糊对话”预设。")
        if c.get("condition_on_previous_text"):
            self._log("[!] “使用上文提示识别”已开启：低信噪比影片可能出现整句重复，"
                      "若发现重复请关闭。")
        if c.get("whisper_model") in ("tiny", "base"):
            self._log(f"[!] 当前模型为 {c.get('whisper_model')}：日语含糊发音识别能力较弱，"
                      "建议至少 medium，追求准确用 large-v3。")

    # ---------------- 可滚动容器 ----------------
    def _scroll_host(self, parent):
        """把父容器变成可上下滚动的区域，返回内容 Frame。

        识别参数较多，窗口高度有限，用滚动避免控件被裁掉。
        """
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        vs = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=10)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vs.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))

        def on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", on_wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    # ---------------- 识别设置页 ----------------
    def _build_tab_asr(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="识别设置")
        t = self._scroll_host(tab)
        # 控件列不参与拉伸，末列吃掉多余宽度：提示文字紧跟控件，而不是被推到右半边
        t.columnconfigure(1, weight=0)
        t.columnconfigure(5, weight=1)

        self.var_asr_engine = tk.StringVar()
        self.var_model = tk.StringVar()
        self.var_device = tk.StringVar()
        self.var_lang = tk.StringVar()
        self.var_vad = tk.BooleanVar()
        self.var_vad_thr = tk.DoubleVar()
        self.var_vad_silence = tk.IntVar()
        self.var_vad_pad = tk.IntVar()
        self.var_norm = tk.StringVar()
        self.var_gain = tk.DoubleVar()
        self.var_denoise = tk.BooleanVar()
        self.var_no_speech = tk.DoubleVar()
        self.var_logprob = tk.DoubleVar()
        self.var_jp_prompt = tk.BooleanVar()
        self.var_cond = tk.BooleanVar()
        self.var_beam = tk.IntVar()
        self.var_cache = tk.BooleanVar()
        self.var_preset = tk.StringVar()

        hint_fg = "#888"
        r = 0

        def hint(parent, text, row, col=2):
            ttk.Label(parent, text=text, foreground=hint_fg).grid(
                row=row, column=col, sticky="w", padx=8)

        ttk.Label(t, text="识别引擎:").grid(row=r, column=0, sticky="w", pady=2)
        self.combo_engine = ttk.Combobox(t, textvariable=self.var_asr_engine,
                                         state="readonly", width=26,
                                         values=["local", "api"])
        self.combo_engine.grid(row=r, column=1, sticky="w", pady=2)
        hint(t, "local=本地 Whisper 大模型；api=OpenAI 兼容云端转写", r)
        r += 1

        ttk.Label(t, text="Whisper模型 / API模型名:").grid(row=r, column=0, sticky="w", pady=2)
        self.combo_model = ttk.Combobox(t, textvariable=self.var_model, width=26,
                                        values=asr.MODEL_CHOICES)
        self.combo_model.grid(row=r, column=1, sticky="w", pady=2)
        hint(t, "large-v3 最准；distil-large-v3 快但略弱于 large-v3", r)
        r += 1

        ttk.Label(t, text="计算设备:").grid(row=r, column=0, sticky="w", pady=2)
        self.combo_device = ttk.Combobox(t, textvariable=self.var_device,
                                         state="readonly", width=26,
                                         values=["auto", "cuda", "cpu"])
        self.combo_device.grid(row=r, column=1, sticky="w", pady=2)
        hint(t, "auto=有 N 卡走 CUDA，否则 CPU", r)
        r += 1

        ttk.Label(t, text="源语言:").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(t, textvariable=self.var_lang, width=28).grid(
            row=r, column=1, sticky="w", pady=2)
        hint(t, "日语 ja；不确定才用 auto", r)
        r += 1

        # ---- 预设：一键套用一组相互配合的参数 ----
        ttk.Label(t, text="参数预设:").grid(row=r, column=0, sticky="w", pady=(6, 2))
        f_pre = ttk.Frame(t)
        f_pre.grid(row=r, column=1, columnspan=3, sticky="w", pady=(6, 2))
        self.combo_preset = ttk.Combobox(f_pre, textvariable=self.var_preset, width=22,
                                         state="readonly",
                                         values=list(PRESETS.keys()))
        self.combo_preset.grid(row=0, column=0, sticky="w")
        ttk.Button(f_pre, text="套用预设", command=self._apply_preset).grid(
            row=0, column=1, padx=(6, 2))
        ttk.Button(f_pre, text="恢复推荐默认值", command=self._reset_defaults).grid(
            row=0, column=2, padx=2)
        r += 1
        ttk.Label(t, text="轻声对话常被漏掉就选“轻声 / 含糊对话”；各参数的原理见“参数说明”页",
                  foreground=hint_fg).grid(row=r, column=0, columnspan=4,
                                           sticky="w", pady=(0, 6))
        r += 1

        # ---- VAD ----
        ttk.Checkbutton(t, text="启用 VAD 静音过滤（只把有人声的片段送去识别）",
                        variable=self.var_vad).grid(row=r, column=0, columnspan=2,
                                                    sticky="w", pady=2)
        f_vad = ttk.Frame(t)
        f_vad.grid(row=r, column=2, columnspan=3, sticky="w")
        ttk.Label(f_vad, text="阈值:").grid(row=0, column=0, sticky="w")
        ttk.Scale(f_vad, from_=0.05, to=0.9, variable=self.var_vad_thr,
                  orient="horizontal", length=130,
                  command=lambda _: self.lbl_vad_val.config(
                      text=f"{self.var_vad_thr.get():.2f}")).grid(
            row=0, column=1, sticky="w", padx=6)
        self.lbl_vad_val = ttk.Label(f_vad, text="0.25", width=5)
        self.lbl_vad_val.grid(row=0, column=2, sticky="w")
        r += 1
        ttk.Label(t, text="VAD 判定结束的最短静音 / 语音前后留白:",
                  foreground=hint_fg).grid(row=r, column=0, sticky="w")
        f_vad2 = ttk.Frame(t)
        f_vad2.grid(row=r, column=1, sticky="w")
        ttk.Spinbox(f_vad2, from_=100, to=2000, increment=50,
                    textvariable=self.var_vad_silence, width=7).grid(row=0, column=0)
        ttk.Label(f_vad2, text="ms  /").grid(row=0, column=1, padx=2)
        ttk.Spinbox(f_vad2, from_=0, to=800, increment=50,
                    textvariable=self.var_vad_pad, width=7).grid(row=0, column=2)
        ttk.Label(f_vad2, text="ms").grid(row=0, column=3, padx=2)
        hint(t, "轻声被切掉字头时把留白加大", r)
        r += 1

        # ---- 音频预处理 ----
        ttk.Label(t, text="音频增益(dB):").grid(row=r, column=0, sticky="w", pady=2)
        f_gain = ttk.Frame(t)
        f_gain.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Spinbox(f_gain, from_=-6, to=24, increment=1,
                    textvariable=self.var_gain, width=6).grid(row=0, column=0)
        ttk.Checkbutton(f_gain, text="轻度降噪(afftdn)",
                        variable=self.var_denoise).grid(row=0, column=1, padx=(10, 0))
        hint(t, "小声影片 +6~+12；音效很重的片子关降噪", r, col=2)
        r += 1

        ttk.Label(t, text="音频归一化:").grid(row=r, column=0, sticky="w", pady=2)
        self.combo_norm = ttk.Combobox(t, textvariable=self.var_norm, width=26,
                                       state="readonly",
                                       values=["dynaudnorm", "loudnorm", "none"])
        self.combo_norm.grid(row=r, column=1, sticky="w", pady=2)
        hint(t, "dynaudnorm=逐窗拉平（默认）；loudnorm=整体偏小声时用", r)
        r += 1

        # ---- 丢弃阈值 ----
        ttk.Label(t, text="no_speech 阈值 / 置信度阈值:").grid(row=r, column=0, sticky="w", pady=2)
        f_thr = ttk.Frame(t)
        f_thr.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Spinbox(f_thr, from_=0.3, to=0.99, increment=0.05,
                    textvariable=self.var_no_speech, width=6).grid(row=0, column=0)
        ttk.Label(f_thr, text=" / ").grid(row=0, column=1)
        ttk.Spinbox(f_thr, from_=-3.0, to=0.0, increment=0.1,
                    textvariable=self.var_logprob, width=6).grid(row=0, column=2)
        hint(t, "调高前项 / 调低后项 = 更少丢句", r)
        r += 1

        # ---- 解码策略 ----
        ttk.Checkbutton(t, text="日语提示词偏置（改善汉字/假名书写与含糊发音）",
                        variable=self.var_jp_prompt).grid(row=r, column=0, columnspan=2,
                                                          sticky="w", pady=2)
        f_beam = ttk.Frame(t)
        f_beam.grid(row=r, column=2, columnspan=3, sticky="w")
        ttk.Label(f_beam, text="Beam Size:").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(f_beam, from_=1, to=10, textvariable=self.var_beam, width=6).grid(
            row=0, column=1, padx=6)
        r += 1

        ttk.Checkbutton(t, text="使用上文提示识别（更连贯，个别低信噪比视频可能幻觉循环）",
                        variable=self.var_cond).grid(row=r, column=0, columnspan=4,
                                                     sticky="w", pady=2)
        r += 1

        ttk.Checkbutton(t, text="优先使用已有识别缓存 <影片>.ja.json（跳过识别）",
                        variable=self.var_cache).grid(row=r, column=0, columnspan=4,
                                                      sticky="w", pady=2)
        r += 1

        self.var_split = tk.BooleanVar()
        self.var_gap = tk.DoubleVar()
        ttk.Checkbutton(t, text="静音断句（停顿超过阈值时该句字幕结束，空白处不挂字幕）",
                        variable=self.var_split).grid(row=r, column=0, columnspan=2,
                                                      sticky="w", pady=2)
        f_gap = ttk.Frame(t)
        f_gap.grid(row=r, column=2, columnspan=3, sticky="w")
        ttk.Label(f_gap, text="静音阈值(秒):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(f_gap, from_=0.3, to=2.0, increment=0.1, textvariable=self.var_gap,
                    width=6).grid(row=0, column=1, padx=6)

        # 统一给第一列标签/勾选加右侧留白，避免文字被相邻控件边框挤掉
        for w in t.winfo_children():
            try:
                if int(w.grid_info().get("column", -1)) == 0:
                    w.grid_configure(padx=(0, 10))
            except Exception:
                pass

    # ---------------- 参数说明页 ----------------
    HELP_TEXT = """识别参数说明（重点：为什么小声/含糊的对话会漏，以及每个参数该怎么调）

【VAD 静音过滤 · 开关】
作用：先用声学模型判断“哪一段有人声”，只把有人声的区间交给 Whisper。
      好处是避免在纯音乐/环境音里凭空编字幕，也省算力。
关掉会怎样：整条音轨都送去识别，音效多的地方容易冒出莫名字幕，速度也变慢。

【VAD 阈值（0.05 ~ 0.90，默认 0.25）】
作用：判定“这段是人声”的概率门槛。这是漏掉轻声对话的第一号元凶。
调低（0.25 → 0.18）：更多气声、呢喃、压低嗓子的台词会被判为人声 → 漏句明显减少；
      代价是噪音、翻页声也可能被当成语音，偶尔多出短伪字幕。
调高（0.25 → 0.40+）：只认明显人声，字幕更干净，但小声说话会整段消失。
建议：普通影片 0.25；轻声/含糊影片 0.18~0.22；背景音乐嘈杂 0.28~0.35。

【VAD 判定结束的最短静音（100~2000ms，默认 400）】
作用：一段人声结束后，静音持续多久才认定“这句完了”，并切出下一个语音段。
调大：一句话被当成一段，语速慢、句中停顿多的台词不容易被切断；
      但两句之间很短的停顿会把两句话并成一段。
调小：切得更细，短促对话不容易被埋掉；但可能把一句话拆成几段碎片。
建议：默认 400ms；对话节奏很快时 300ms。

【语音前后留白（0~800ms，默认 300）】
作用：截出来的语音段，左右各多保留多少毫秒。轻声开头的第一个字往往低于阈值，
      留白不足就会把字头/字尾切掉，听感上“这句少半个字”。
调大：字头字尾更完整 → 含糊台词识别率提高；代价是相邻段可能重叠
      （本程序导出前会自动钳掉重叠，不会出现两条字幕同时压屏）。
建议：默认 300ms；常见“开头丢字”时 400~500ms。

【音频增益 dB（-6 ~ 24，默认 +6）】
作用：解码时先把整体音量抬起来，再做归一化。很多影片对话轨录得很轻，
      直接送进 Whisper 时语音特征太弱，模型干脆当没人说话。
调大：轻声对话更清楚；过大（>12dB）会把底噪、口水声一起放大，反而更糊。
调小/归零：影片本身音量正常，或增益后出现“全是噪点”的伪字幕时。
建议：一般 +6；明显小声的影片 +8~+12；音轨本来就响用 0。

【轻度降噪 afftdn · 开关（默认开）】
作用：用 FFT 抑制稳定的背景底噪（空调声、磁带嘶嘶声、远处车流），
      提升“吐字不清”的可辨识度。
建议：对白为主的影片保持开启；音乐剧、音效重的片子建议关闭，
      否则人声质感被削、识别反而变差。

【音频归一化（dynaudnorm / loudnorm / none）】
dynaudnorm：逐段小窗口把音量拉到同一水平，最适合“忽大忽小”的影片（默认）。
loudnorm：按 EBU R128 整体响度标准化到 -16 LUFS，适合整片偏小声的情况；
      它对动态的处理更平滑，但会把音乐也一起拉响。
none：不处理，保留原始动态；只在怀疑处理本身导致识别异常时才用
      （此模式下若同时加了增益，程序会自动串一级限幅防止削顶）。

【no_speech 阈值（0.30 ~ 0.99，默认 0.75）】
作用：Whisper 对每一段会算一个“这段其实没人说话”的概率；
      当该概率高于此阈值且这段置信度又很低时，整段文本会被丢弃。
调高（0.75 → 0.90）：模型更难把一段判成静音 → 轻声台词保得住；
      代价是极安静处可能冒出“谢谢观看”“字幕由…提供”这类幻觉句。
调低（0.75 → 0.50）：字幕更干净，但真实的轻声道白容易被整段静音化。
建议：默认 0.75；漏句严重时的主力调节项之一，可到 0.85~0.90。

【置信度阈值 log_prob_threshold（-3.0 ~ 0，默认 -1.2）】
作用：一段文字的平均对数概率低于该值时被视为“不可靠”，参与上面的丢弃判断。
调低（-1.2 → -1.8）：保留更多“听得见但模型没把握”的台词，召回↑、准确率↓；
调高（-1.2 → -0.8）：只留模型很有把握的句子，字幕更准但更少。
建议：默认 -1.2；吐字含糊、方言口音重时 -1.5~-1.8。

【日语提示词偏置 · 开关（默认开）】
作用：识别前先给模型一句日语语境（例：これは日本語の会話です。），
      让它按日语的书写习惯输出汉字+假名，含糊音节更容易被“补”成合理日语，
      也能减少把日语误判成中文/韩文的情况。
注意：极少数情况下会诱导重复同一句（幻觉循环）；一旦出现就关掉它。

【使用上文提示 condition_on_previous_text（默认关）】
关：每段独立识别，上一段错了不会连锁污染下文（推荐）。
开：上下文更连贯，专有名词前后一致；但低信噪比影片容易整段重复、越滚越错。

【Beam Size（1~10，默认 5）】
作用：解码时同时保留多少条候选路径。
调大（5 → 8）：准确率小幅提升，尤其含糊音节；耗时增加约 30%~50%。
调小（5 → 1）：最快，但更容易写错。
建议：默认 5；重要影片且不赶时间用 8。

【静音断句 · 开关 + 静音阈值（0.3~2.0 秒，默认 0.6）】
作用：一句话内部停顿超过该秒数时，把它拆成两条字幕；
      拆开后中间那段静音期屏幕上不挂任何字幕（解决“长停顿还挂着上一句”）。
调小（0.6 → 0.4）：更容易断开，节奏快、停顿短的片子也不会一条字幕挂很久；
      但可能把一句话拆得偏碎。
调大（0.6 → 0.8）：长句更完整，适合慢节奏文艺片。
注意：需要重新识别（旧缓存没有词级时间戳，静音断句不生效）。

【参数之间会互相影响，记住三条经验】
1. 先解决“有没有送进去”：VAD 阈值↓、语音留白↑、音频增益↑、降噪开。
2. 再解决“敢不敢留下”：no_speech 阈值↑、置信度阈值↓。
3. 最后解决“准不准”：Beam Size↑、日语提示词开、模型换 large-v3。
“轻声 / 含糊对话”预设就是按这个顺序打包好的，可直接套用。
"""

    def _build_tab_help(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="参数说明")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        txt = scrolledtext.ScrolledText(tab, wrap="word", font=("Microsoft YaHei UI", 10),
                                        padx=12, pady=10, relief="flat",
                                        background="#fbfbfb")
        txt.grid(row=0, column=0, sticky="nsew")
        txt.insert("1.0", self.HELP_TEXT)
        txt.config(state="disabled")

    # ---------------- 预设 ----------------
    def _set_vars_from_preset(self, name):
        p = PRESETS.get(name)
        if not p:
            return
        if "use_vad" in p:
            self.var_vad.set(bool(p["use_vad"]))
        if "vad_threshold" in p:
            self.var_vad_thr.set(float(p["vad_threshold"]))
            self.lbl_vad_val.config(text=f"{float(p['vad_threshold']):.2f}")
        if "vad_min_silence_ms" in p:
            self.var_vad_silence.set(int(p["vad_min_silence_ms"]))
        if "vad_speech_pad_ms" in p:
            self.var_vad_pad.set(int(p["vad_speech_pad_ms"]))
        if "no_speech_threshold" in p:
            self.var_no_speech.set(float(p["no_speech_threshold"]))
        if "log_prob_threshold" in p:
            self.var_logprob.set(float(p["log_prob_threshold"]))
        if "gain_db" in p:
            self.var_gain.set(float(p["gain_db"]))
        if "denoise" in p:
            self.var_denoise.set(bool(p["denoise"]))
        if "normalize" in p:
            self.var_norm.set(str(p["normalize"]))
        if "japanese_prompt" in p:
            self.var_jp_prompt.set(bool(p["japanese_prompt"]))
        if "condition_on_previous_text" in p:
            self.var_cond.set(bool(p["condition_on_previous_text"]))
        if "beam_size" in p:
            self.var_beam.set(int(p["beam_size"]))
        if "silence_gap_sec" in p:
            self.var_gap.set(float(p["silence_gap_sec"]))
        self.var_preset.set(name)

    def _apply_preset(self):
        name = self.var_preset.get().strip()
        if name not in PRESETS:
            messagebox.showinfo("提示", "请先选择一个预设")
            return
        self._set_vars_from_preset(name)
        self._read_widgets()
        save_settings(self.cfg)
        self._log(f"[*] 已套用参数预设: {name}")

    def _reset_defaults(self):
        self._set_vars_from_preset(PRESET_DEFAULT)
        self._read_widgets()
        save_settings(self.cfg)
        self._log("[*] 识别参数已恢复为推荐默认值")

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

    # ---------------- 字幕审查设置页 ----------------
    def _build_tab_review(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="审查修正")
        t = self._scroll_host(tab)
        t.columnconfigure(1, weight=0)
        t.columnconfigure(5, weight=1)

        self.var_rv_cache = tk.BooleanVar()
        self.var_rv_text = tk.BooleanVar()
        self.var_rv_timing = tk.BooleanVar()
        self.var_rv_missing = tk.BooleanVar()
        self.var_rv_split = tk.BooleanVar()
        self.var_rv_overlap = tk.DoubleVar()
        self.var_rv_pad = tk.DoubleVar()
        self.var_rv_gap = tk.DoubleVar()
        self.var_rv_minmiss = tk.DoubleVar()
        self.var_rv_lines = tk.IntVar()
        self.var_rv_chars = tk.IntVar()
        self.var_rv_suffix = tk.StringVar()

        hint_fg = "#888"
        r = 0

        def hint(parent, text, row, col=2):
            ttk.Label(parent, text=text, foreground=hint_fg).grid(
                row=row, column=col, sticky="w", padx=8)

        ttk.Label(t, text="审查依据: 影片音轨 → Whisper 听写 → 大模型对照校对",
                  font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=r, column=0, columnspan=4, sticky="w", pady=(0, 6))
        r += 1
        ttk.Checkbutton(t, text="优先用已有识别缓存 <影片>.ja.json 做参照（跳过重新识别音轨）",
                        variable=self.var_rv_cache).grid(row=r, column=0, columnspan=4,
                                                         sticky="w", pady=2)
        r += 1
        ttk.Label(t, text="缓存不存在时会自动重新识别；想要最准就保持不勾选。",
                  foreground=hint_fg).grid(row=r, column=0, columnspan=4,
                                           sticky="w", pady=(0, 8))
        r += 1

        ttk.Label(t, text="允许修正:").grid(row=r, column=0, sticky="w", pady=2)
        f_fix = ttk.Frame(t)
        f_fix.grid(row=r, column=1, columnspan=4, sticky="w", pady=2)
        ttk.Checkbutton(f_fix, text="日文/中文文本",
                        variable=self.var_rv_text).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(f_fix, text="时间轴（含整体偏移）",
                        variable=self.var_rv_timing).grid(row=0, column=1, padx=(14, 0))
        ttk.Checkbutton(f_fix, text="补漏句",
                        variable=self.var_rv_missing).grid(row=0, column=2, padx=(14, 0))
        ttk.Checkbutton(f_fix, text="按句切分",
                        variable=self.var_rv_split).grid(row=0, column=3, padx=(14, 0))
        r += 1
        ttk.Label(t, text="时间轴与切句由本地规则按音轨语音边界处理；模型只负责改文本。",
                  foreground=hint_fg).grid(row=r, column=0, columnspan=5,
                                           sticky="w", pady=(0, 8))
        r += 1

        ttk.Label(t, text="匹配阈值:").grid(row=r, column=0, sticky="w", pady=2)
        f_m = ttk.Frame(t)
        f_m.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Spinbox(f_m, from_=0.10, to=0.90, increment=0.05,
                    textvariable=self.var_rv_overlap, width=6).grid(row=0, column=0)
        ttk.Label(f_m, text="  时间轴余量(秒):").grid(row=0, column=1)
        ttk.Spinbox(f_m, from_=0.0, to=1.0, increment=0.05,
                    textvariable=self.var_rv_pad, width=6).grid(row=0, column=2)
        hint(t, "阈值越低越容易把字幕和语音段配成同一条；余量是修正后前后多留的时间", r)
        r += 1

        ttk.Label(t, text="切句静音阈值(秒):").grid(row=r, column=0, sticky="w", pady=2)
        f_g = ttk.Frame(t)
        f_g.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Spinbox(f_g, from_=0.3, to=2.0, increment=0.1,
                    textvariable=self.var_rv_gap, width=6).grid(row=0, column=0)
        ttk.Label(f_g, text="  补句最短时长(秒):").grid(row=0, column=1)
        ttk.Spinbox(f_g, from_=0.2, to=2.0, increment=0.1,
                    textvariable=self.var_rv_minmiss, width=6).grid(row=0, column=2)
        hint(t, "句间静音超过该值才认为是一条字幕跨了两句；太短的语音段不补句", r)
        r += 1

        ttk.Label(t, text="每批条数 / 字符:").grid(row=r, column=0, sticky="w", pady=2)
        f_b = ttk.Frame(t)
        f_b.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Spinbox(f_b, from_=5, to=60, increment=5,
                    textvariable=self.var_rv_lines, width=6).grid(row=0, column=0)
        ttk.Label(f_b, text=" / ").grid(row=0, column=1)
        ttk.Spinbox(f_b, from_=400, to=4000, increment=200,
                    textvariable=self.var_rv_chars, width=7).grid(row=0, column=2)
        hint(t, "本地小模型建议 15~20 条一批；云端大模型可以更大", r)
        r += 1

        ttk.Label(t, text="输出文件名后缀:").grid(row=r, column=0, sticky="w", pady=2)
        f_s = ttk.Frame(t)
        f_s.grid(row=r, column=1, sticky="w", pady=2)
        ttk.Entry(f_s, textvariable=self.var_rv_suffix, width=12).grid(row=0, column=0)
        ttk.Label(f_s, text=".srt").grid(row=0, column=1)
        hint(t, "原字幕不动；<原名>+后缀+.srt。留空则用 .fixed", r)
        r += 1

        ttk.Label(t, text="用法: 队列里放影片 → 点底部「审查并修正字幕」→ 自动找同目录同名 .srt；\n"
                          "没找到会弹窗让你选。修正结果写入新文件，可继续用「导出 SRT」按当前格式另存。",
                  foreground=hint_fg, justify="left").grid(row=r, column=0, columnspan=5,
                                                           sticky="w", pady=(10, 0))

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
        self.var_vad_silence.set(int(c.get("vad_min_silence_ms", 400)))
        self.var_vad_pad.set(int(c.get("vad_speech_pad_ms", 300)))
        self.var_no_speech.set(float(c.get("no_speech_threshold", 0.75)))
        self.var_logprob.set(float(c.get("log_prob_threshold", -1.2)))
        self.var_gain.set(float(c.get("gain_db", 6.0)))
        self.var_denoise.set(bool(c.get("denoise", True)))
        self.var_jp_prompt.set(bool(c.get("japanese_prompt", True)))
        self.var_norm.set(c["normalize"])
        self.var_cond.set(c["condition_on_previous_text"])
        self.var_beam.set(c["beam_size"])
        self.var_cache.set(c["use_cache"])
        self.var_split.set(c["split_at_silence"])
        self.var_gap.set(c["silence_gap_sec"])
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
        self.var_rv_cache.set(bool(c.get("review_use_cache_ref", False)))
        self.var_rv_text.set(bool(c.get("review_fix_text", True)))
        self.var_rv_timing.set(bool(c.get("review_fix_timing", True)))
        self.var_rv_missing.set(bool(c.get("review_add_missing", True)))
        self.var_rv_split.set(bool(c.get("review_split_sentences", True)))
        self.var_rv_overlap.set(float(c.get("review_min_overlap", 0.35)))
        self.var_rv_pad.set(float(c.get("review_time_pad_sec", 0.25)))
        self.var_rv_gap.set(float(c.get("review_split_gap_sec", 0.8)))
        self.var_rv_minmiss.set(float(c.get("review_min_missing_sec", 0.35)))
        self.var_rv_lines.set(int(c.get("review_batch_lines", 20)))
        self.var_rv_chars.set(int(c.get("review_batch_chars", 1600)))
        self.var_rv_suffix.set(c.get("review_out_suffix", ".fixed"))
        self.lbl_vad_val.config(text=f"{c['vad_threshold']:.2f}")
        self.lbl_temp_val.config(text=f"{c['temperature']:.2f}")
        # 预设下拉：回显“推荐（默认）”，用户点“套用预设”才会真正生效
        self.var_preset.set(PRESET_DEFAULT)

    def _read_widgets(self):
        c = self.cfg
        c["asr_engine"] = self.var_asr_engine.get().strip()
        c["whisper_model"] = self.var_model.get().strip() or "large-v3"
        c["device"] = self.var_device.get().strip() or "auto"
        c["language"] = self.var_lang.get().strip() or "ja"
        c["use_vad"] = bool(self.var_vad.get())
        c["vad_threshold"] = self._get_num(self.var_vad_thr, 0.25)
        c["vad_min_silence_ms"] = int(self._get_num(self.var_vad_silence, 400))
        c["vad_speech_pad_ms"] = int(self._get_num(self.var_vad_pad, 300))
        c["no_speech_threshold"] = self._get_num(self.var_no_speech, 0.75)
        c["log_prob_threshold"] = self._get_num(self.var_logprob, -1.2)
        c["gain_db"] = self._get_num(self.var_gain, 6.0)
        c["denoise"] = bool(self.var_denoise.get())
        c["japanese_prompt"] = bool(self.var_jp_prompt.get())
        c["normalize"] = self.var_norm.get().strip() or "dynaudnorm"
        c["condition_on_previous_text"] = bool(self.var_cond.get())
        c["beam_size"] = int(self._get_num(self.var_beam, 5))
        c["use_cache"] = bool(self.var_cache.get())
        c["split_at_silence"] = bool(self.var_split.get())
        c["silence_gap_sec"] = self._get_num(self.var_gap, 0.6)
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
        c["review_use_cache_ref"] = bool(self.var_rv_cache.get())
        c["review_fix_text"] = bool(self.var_rv_text.get())
        c["review_fix_timing"] = bool(self.var_rv_timing.get())
        c["review_add_missing"] = bool(self.var_rv_missing.get())
        c["review_split_sentences"] = bool(self.var_rv_split.get())
        c["review_min_overlap"] = self._get_num(self.var_rv_overlap, 0.35)
        c["review_time_pad_sec"] = self._get_num(self.var_rv_pad, 0.25)
        c["review_split_gap_sec"] = self._get_num(self.var_rv_gap, 0.8)
        c["review_min_missing_sec"] = self._get_num(self.var_rv_minmiss, 0.35)
        c["review_batch_lines"] = int(self._get_num(self.var_rv_lines, 20))
        c["review_batch_chars"] = int(self._get_num(self.var_rv_chars, 1600))
        c["review_out_suffix"] = self.var_rv_suffix.get().strip() or ".fixed"
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
        for b in (self.btn_run, self.btn_asr, self.btn_tr, self.btn_review, self.btn_exp):
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

        # 按真实语音边界规整时间轴：词间静音断句 + 消除相邻段重叠
        n_before = len(segments)
        has_words = any(s.get("words") for s in segments)
        segments = asr.normalize_segments(
            segments,
            silence_gap_sec=float(c.get("silence_gap_sec", 0.6)),
            split_long=bool(c.get("split_at_silence", True)))
        if len(segments) != n_before:
            self._post(("log", f"[*] 时间轴规整: {n_before} 段 → {len(segments)} 段（静音断句/去重叠）"))
        if c.get("split_at_silence", True) and not has_words:
            self._post(("log", "[i] 无词级时间戳，静音断句未生效；旧缓存请关闭缓存开关重新识别一次"))

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
                audio.decode_to_wav(video, tmp_wav, normalize=c["normalize"],
                                    gain_db=c.get("gain_db", 0.0),
                                    denoise=c.get("denoise", False))
                self._post(("log", f"[*] 音频已转码: {tmp_wav}"))
                self._post(("stage", "API 转写中…"))
                self._post(("prog", 10))
                eng = asr.ApiTranscriber(c["llm_base_url"], c["llm_api_key"],
                                         c["whisper_model"])
                segs, info = eng.transcribe(tmp_wav, language=c["language"])
                self._post(("log", f"[*] API 转写完成: {len(segs)} 段"))
                return segs

            # 本地 Whisper
            audio_pcm = audio.decode_audio_pcm(video, normalize=c["normalize"],
                                               gain_db=c.get("gain_db", 0.0),
                                               denoise=c.get("denoise", False))
            self._post(("log", f"[*] 音频解码完成: {len(audio_pcm) / 16000:.0f} 秒"
                               f"（增益{c.get('gain_db', 0):+g}dB, "
                               f"降噪{'开' if c.get('denoise') else '关'}, "
                               f"归一化={c['normalize']}）"))
            self._post(("stage", "加载 Whisper 模型并识别…"))
            self._post(("prog", 5))
            eng = asr.LocalWhisper(c["whisper_model"], c["device"])
            def prog(i, end_sec, total_sec):
                pct = 8 + int(47 * (end_sec / total_sec)) if total_sec else 8
                self._post(("prog", min(pct, 55)))
                self._post(("stage", f"识别中… {i} 段 ({int(end_sec)}s / {int(total_sec)}s)"))
            self._post(("log",
                        f"[*] 识别参数: VAD={c['use_vad']}(阈值{c['vad_threshold']:.2f}/"
                        f"最短静音{c['vad_min_silence_ms']}ms/留白{c['vad_speech_pad_ms']}ms) "
                        f"no_speech={c['no_speech_threshold']:.2f} "
                        f"logprob={c['log_prob_threshold']:.2f} beam={c['beam_size']} "
                        f"日语提示={'开' if c['japanese_prompt'] else '关'}"))
            segs, info = eng.transcribe(
                audio_pcm, language=c["language"], vad=c["use_vad"],
                vad_threshold=c["vad_threshold"],
                vad_min_silence_ms=c["vad_min_silence_ms"],
                vad_speech_pad_ms=c["vad_speech_pad_ms"],
                no_speech_threshold=c["no_speech_threshold"],
                log_prob_threshold=c["log_prob_threshold"],
                initial_prompt=(asr.JAPANESE_PROMPT
                                if c.get("japanese_prompt") else None),
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

    # ============================================================ 字幕审查
    def _review_current(self):
        if self.busy:
            return
        self._read_widgets()
        save_settings(self.cfg)
        video = self.current_video or (self.queue_items[0] if self.queue_items else "")
        if not video or not os.path.exists(video):
            messagebox.showinfo("提示", "请先添加影片（审查需要用影片音轨做参照）")
            return
        guess = os.path.splitext(video)[0] + ".srt"
        srt_path = guess if os.path.exists(guess) else ""
        if not srt_path:
            srt_path = filedialog.askopenfilename(
                title="选择要审查的字幕文件",
                initialdir=os.path.dirname(video),
                filetypes=[("SRT 字幕", "*.srt"), ("所有文件", "*.*")])
            if not srt_path:
                messagebox.showinfo("提示", "该影片目录没有同名 .srt，请手动选择要审查的字幕文件")
                return
        self._set_busy(True)
        self.worker = threading.Thread(target=self._worker_review,
                                       args=(video, srt_path), daemon=True)
        self.worker.start()

    def _worker_review(self, video, srt_path):
        c = self.cfg
        try:
            self._post(("log", f"\n========== 字幕审查: {os.path.basename(srt_path)} "
                               f"← {os.path.basename(video)} =========="))
            try:
                with open(srt_path, "r", encoding="utf-8-sig") as f:
                    subs = review.parse_srt(f.read())
            except Exception as e:
                self._post(("log", f"[X] 字幕读取失败: {e}"))
                return
            if not subs:
                self._post(("log", "[X] 字幕文件没解析出条目，请确认是标准 SRT"))
                return
            self._post(("log", f"[*] 现有字幕: {len(subs)} 条"))

            # ---------- 1. 参照：音轨听写 ----------
            ref = None
            cache = os.path.splitext(video)[0] + ".ja.json"
            if c.get("review_use_cache_ref") and os.path.exists(cache):
                try:
                    with open(cache, "r", encoding="utf-8") as f:
                        ref = json.load(f)
                    self._post(("log", f"[*] 用识别缓存做参照: {len(ref)} 段（未重新识别）"))
                except Exception as e:
                    self._post(("log", f"[!] 缓存读取失败，改为重新识别: {e}"))
                    ref = None
            if ref is None:
                self._post(("log", "[*] 提取音轨并重新识别，作为审查参照…"))
                ref = self._do_asr(video)
                if not ref:
                    self._post(("log", "[X] 音轨识别失败，无法审查"))
                    return
            ref = asr.normalize_segments(
                ref, silence_gap_sec=float(c.get("review_split_gap_sec", 0.8)),
                split_long=bool(c.get("review_split_sentences", True)))
            self._post(("log", f"[*] 参照语音段: {len(ref)} 段"))

            # ---------- 2. 大模型校对 ----------
            client = translator.LLMClient(c["llm_base_url"], c["llm_model"],
                                          c["llm_api_key"], c["temperature"],
                                          c["llm_timeout"])
            if not c["llm_model"]:
                online, model, err = client.check_online()
                if not online:
                    self._post(("log", f"[!] 无法连接审查模型: {err}"))
                    return
                if not model:
                    self._post(("log", "[!] 服务在线但没找到可用模型"))
                    return
                self._post(("model", model))
                self._post(("log", f"[*] 审查模型: {model}"))

            opts = {
                "fix_text": bool(c.get("review_fix_text", True)),
                "fix_timing": bool(c.get("review_fix_timing", True)),
                "add_missing": bool(c.get("review_add_missing", True)),
                "split_sentences": bool(c.get("review_split_sentences", True)),
                "min_overlap": float(c.get("review_min_overlap", 0.35)),
                "time_pad_sec": float(c.get("review_time_pad_sec", 0.25)),
                "split_gap_sec": float(c.get("review_split_gap_sec", 0.8)),
                "min_missing_sec": float(c.get("review_min_missing_sec", 0.35)),
                "batch_lines": int(c.get("review_batch_lines", 20)),
                "batch_chars": int(c.get("review_batch_chars", 1600)),
            }

            def prog(done, total):
                self._post(("prog", min(55 + int(40 * done / max(total, 1)), 95)))
                self._post(("stage", f"审查中… {done}/{total} 条"))

            reviewed, report = review.review_segments(
                subs, ref, client, opts, progress=prog,
                log=lambda m: self._post(("log", m)))
            for line in review.format_report(report):
                self._post(("log", line))

            # ---------- 3. 写新文件（原字幕不动） ----------
            suffix = (c.get("review_out_suffix") or ".fixed").strip()
            out = os.path.splitext(srt_path)[0] + suffix + ".srt"
            if c.get("export_dir"):
                out = os.path.join(c["export_dir"], os.path.basename(out))
            try:
                review.write_srt(reviewed, out)
                self._post(("log", f"[OK] 修正字幕已写入: {out}（原字幕未改动）"))
            except Exception as e:
                self._post(("log", f"[X] 写入失败 {out}: {e}"))
                return

            self._post(("segments", [
                {"start": s["start"], "end": s["end"],
                 "text": s.get("ja", ""), "zh": s.get("zh", "")}
                for s in reviewed]))
            self._post(("prog", 100))
            self._post(("done", "审查完成"))
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
