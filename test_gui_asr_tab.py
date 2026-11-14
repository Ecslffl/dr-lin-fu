# -*- coding: utf-8 -*-
"""GUI 冒烟测试：界面可构建、预设可套用、参数可回写、帮助页存在。"""
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jp_subtitle_app import gui, config

FAIL = []

# 备份真实配置，测试结束后原样还原，避免污染用户设置
SET_PATH = config.SETTINGS_FILE
BACKUP = None
if os.path.exists(SET_PATH):
    with open(SET_PATH, "rb") as f:
        BACKUP = f.read()


def check(name, cond, detail=""):
    print(("[OK] " if cond else "[FAIL] ") + name + (("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def restore():
    if BACKUP is not None:
        with open(SET_PATH, "wb") as f:
            f.write(BACKUP)
        print(f"[i] 已还原 {SET_PATH}")


root = tk.Tk()
root.withdraw()
app = gui.SubtitleApp(root)
root.update()
app._set_initial_split()
root.update()

# ---------- 默认值 ----------
print("\n===== 1. 默认值是否为新推荐档 =====")
print("vad=%.2f 最短静音=%d 留白=%d no_speech=%.2f 置信度=%.2f 增益=%+.1f 降噪=%s 提示词=%s"
      % (app.var_vad_thr.get(), app.var_vad_silence.get(), app.var_vad_pad.get(),
         app.var_no_speech.get(), app.var_logprob.get(), app.var_gain.get(),
         app.var_denoise.get(), app.var_jp_prompt.get()))
check("VAD 阈值默认 0.25", abs(app.var_vad_thr.get() - 0.25) < 1e-6)
check("最短静音默认 400", app.var_vad_silence.get() == 400)
check("语音留白默认 300", app.var_vad_pad.get() == 300)
check("no_speech 默认 0.75", abs(app.var_no_speech.get() - 0.75) < 1e-6)
check("置信度默认 -1.2", abs(app.var_logprob.get() + 1.2) < 1e-6)
check("增益默认 +6", abs(app.var_gain.get() - 6.0) < 1e-6)
check("降噪默认开", bool(app.var_denoise.get()))
check("日语提示词默认开", bool(app.var_jp_prompt.get()))

# ---------- 预设 ----------
print("\n===== 2. 预设套用 =====")
app.var_preset.set("轻声 / 含糊对话")
app._apply_preset()
print("套用后: vad=%.2f 留白=%d 增益=%+.1f no_speech=%.2f 置信度=%.2f beam=%d"
      % (app.var_vad_thr.get(), app.var_vad_pad.get(), app.var_gain.get(),
         app.var_no_speech.get(), app.var_logprob.get(), app.var_beam.get()))
check("轻声预设 VAD 降到 0.18", abs(app.var_vad_thr.get() - 0.18) < 1e-6)
check("轻声预设置信度 -1.6", abs(app.var_logprob.get() + 1.6) < 1e-6)
check("轻声预设写进配置", app.cfg["vad_threshold"] == 0.18)
check("VAD 数值标签同步", app.lbl_vad_val.cget("text") == "0.18")

app._reset_defaults()
print("恢复默认后: vad=%.2f 增益=%+.1f beam=%d"
      % (app.var_vad_thr.get(), app.var_gain.get(), app.var_beam.get()))
check("恢复默认 VAD=0.25", abs(app.var_vad_thr.get() - 0.25) < 1e-6)
check("恢复默认 beam=5", app.var_beam.get() == 5)

# ---------- 参数回写 ----------
print("\n===== 3. 控件读回配置 =====")
app.var_gain.set(12.0)
app.var_denoise.set(False)
app.var_jp_prompt.set(False)
app.var_vad_silence.set(250)
app.var_vad_pad.set(500)
app._read_widgets()
check("增益回写 12", app.cfg["gain_db"] == 12.0)
check("降噪回写 False", app.cfg["denoise"] is False)
check("提示词回写 False", app.cfg["japanese_prompt"] is False)
check("最短静音回写 250", app.cfg["vad_min_silence_ms"] == 250)
check("留白回写 500", app.cfg["vad_speech_pad_ms"] == 500)

# ---------- 帮助页 ----------
print("\n===== 4. 参数说明页 =====")
help_tab_found = False
text_len = 0
nb = None
for child in root.winfo_children():
    if child.winfo_class() == "TNotebook":
        nb = child
if nb:
    tabs = [nb.tab(t, "text") for t in nb.tabs()]
    print("选项页:", tabs)
    help_tab_found = "参数说明" in tabs
    for t in nb.tabs():
        if nb.tab(t, "text") == "参数说明":
            widget = nb.nametowidget(t)
            for sub in widget.winfo_children():
                if sub.winfo_class() in ("Text", "Frame"):
                    widgets = [sub] + list(sub.winfo_children())
                    for w in widgets:
                        try:
                            text_len = max(text_len, len(w.get("1.0", "end")))
                        except Exception:
                            pass
check("存在“参数说明”页", help_tab_found)
check("说明文字足够详细(>2000字)", text_len > 2000, f"{text_len} 字")
for kw in ("VAD 阈值", "no_speech", "置信度阈值", "音频增益", "降噪", "Beam Size", "静音断句"):
    present = kw in gui.SubtitleApp.HELP_TEXT
    check(f"说明覆盖「{kw}」", present)

# ---------- 启动参数链 ----------
print("\n===== 5. 识别调用参数拼装（含音频滤镜）=====")
app.var_gain.set(6.0)
app.var_denoise.set(True)
app.var_norm.set("dynaudnorm")
app._read_widgets()
log_ctx = gui.SubtitleApp
check("_do_asr 传入增益", "gain_db=c.get(\"gain_db\"" in
      open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "jp_subtitle_app", "gui.py"), encoding="utf-8").read())
check("_do_asr 传入 VAD 细项", "vad_speech_pad_ms=c[\"vad_speech_pad_ms\"]" in
      open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "jp_subtitle_app", "gui.py"), encoding="utf-8").read())

# ---------- 设置文件 ----------
print("\n===== 6. 设置文件 =====")
print("settings:", config.SETTINGS_FILE)
check("配置文件在应用目录内", os.path.dirname(config.SETTINGS_FILE) ==
      os.path.dirname(os.path.abspath(config.__file__)))

root.destroy()
restore()
print("\n" + ("ALL GUI TESTS PASSED" if not FAIL else "FAILED: " + ", ".join(FAIL)))
sys.exit(0 if not FAIL else 1)
