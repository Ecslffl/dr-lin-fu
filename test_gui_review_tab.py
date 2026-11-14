# -*- coding: utf-8 -*-
"""GUI 冒烟测试：审查修正页可构建、默认值正确、参数可回写、按钮参与忙碌锁定。"""
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jp_subtitle_app import gui, config

FAIL = []
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

# ---------- 页签与按钮 ----------
print("\n===== 1. 页签与按钮 =====")
nb = None
for child in root.winfo_children():
    if child.winfo_class() == "TNotebook":
        nb = child
tabs = [nb.tab(t, "text") for t in nb.tabs()] if nb else []
print("选项页:", tabs)
check("存在「审查修正」页", "审查修正" in tabs)
check("底部有审查按钮", app.btn_review.winfo_class() == "TButton")
check("按钮文案含音轨参照", "音轨" in app.btn_review.cget("text"))

# ---------- 默认值 ----------
print("\n===== 2. 审查默认值 =====")
print("用缓存=%s 改文本=%s 改时间轴=%s 补漏句=%s 切句=%s 阈值=%.2f 余量=%.2f "
      "切句静音=%.2f 补句最短=%.2f 批=%d/%d 后缀=%s"
      % (app.var_rv_cache.get(), app.var_rv_text.get(), app.var_rv_timing.get(),
         app.var_rv_missing.get(), app.var_rv_split.get(), app.var_rv_overlap.get(),
         app.var_rv_pad.get(), app.var_rv_gap.get(), app.var_rv_minmiss.get(),
         app.var_rv_lines.get(), app.var_rv_chars.get(), app.var_rv_suffix.get()))
check("默认不优先用缓存（重新识别音轨）", app.var_rv_cache.get() is False)
check("默认改文本", app.var_rv_text.get() is True)
check("默认改时间轴", app.var_rv_timing.get() is True)
check("默认补漏句", app.var_rv_missing.get() is True)
check("默认按句切分", app.var_rv_split.get() is True)
check("匹配阈值默认 0.35", abs(app.var_rv_overlap.get() - 0.35) < 1e-6)
check("时间轴余量默认 0.25", abs(app.var_rv_pad.get() - 0.25) < 1e-6)
check("切句静音默认 0.8", abs(app.var_rv_gap.get() - 0.8) < 1e-6)
check("补句最短时长默认 0.35", abs(app.var_rv_minmiss.get() - 0.35) < 1e-6)
check("每批条数默认 20", app.var_rv_lines.get() == 20)
check("输出后缀默认 .fixed", app.var_rv_suffix.get() == ".fixed")

# ---------- 回写 ----------
print("\n===== 3. 控件读回配置 =====")
app.var_rv_cache.set(True)
app.var_rv_missing.set(False)
app.var_rv_overlap.set(0.5)
app.var_rv_pad.set(0.4)
app.var_rv_lines.set(30)
app.var_rv_suffix.set(".ai")
app._read_widgets()
check("用缓存回写 True", app.cfg["review_use_cache_ref"] is True)
check("补漏句回写 False", app.cfg["review_add_missing"] is False)
check("匹配阈值回写 0.5", abs(app.cfg["review_min_overlap"] - 0.5) < 1e-6)
check("余量回写 0.4", abs(app.cfg["review_time_pad_sec"] - 0.4) < 1e-6)
check("批条数回写 30", app.cfg["review_batch_lines"] == 30)
check("后缀回写 .ai", app.cfg["review_out_suffix"] == ".ai")

app.var_rv_suffix.set("")
app._read_widgets()
check("后缀留空回落 .fixed", app.cfg["review_out_suffix"] == ".fixed")

# ---------- 忙碌锁定 ----------
print("\n===== 4. 忙碌锁定 =====")
app._set_busy(True)
check("忙碌时审查按钮禁用", str(app.btn_review.cget("state")) == "disabled")
app._set_busy(False)
check("空闲时审查按钮可用", str(app.btn_review.cget("state")) == "normal")

# ---------- 接线 ----------
print("\n===== 5. 调用链 =====")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "jp_subtitle_app", "gui.py"), encoding="utf-8").read()
check("gui 导入 review 模块", "from . import audio, asr, review," in src)
check("_worker_review 解析字幕", "review.parse_srt(" in src)
check("_worker_review 调用审查", "review.review_segments(" in src)
check("_worker_review 写新文件", "review.write_srt(" in src)
check("原字幕文件不被写入", "open(srt_path, \"r\"," in src and
      'open(srt_path, "w"' not in src)

# 配置往返：写盘后重新加载应保留审查项
config.save_settings(app.cfg)
cfg2 = config.load_settings()
check("审查项能往返配置", cfg2["review_batch_lines"] == 30 and
      cfg2["review_min_overlap"] == 0.5)

root.destroy()
restore()
print("\n" + ("ALL REVIEW-GUI TESTS PASSED" if not FAIL else "FAILED: " + ", ".join(FAIL)))
sys.exit(0 if not FAIL else 1)
