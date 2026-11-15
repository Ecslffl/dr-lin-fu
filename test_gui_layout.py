# -*- coding: utf-8 -*-
"""GUI 布局冒烟测试：设置区限高且每页都有滚动条，日志区与片段预览等高。"""
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


def find_notebook(root):
    for child in root.winfo_children():
        if child.winfo_class() == "TNotebook":
            return child
    return None


def walk(w):
    yield w
    for k in w.winfo_children():
        yield from walk(k)


# 需要真实几何（withdraw 的窗口高度恒为 1），所以不隐藏窗口
root = tk.Tk()
app = gui.SubtitleApp(root)
root.update_idletasks()
root.update()
app._set_initial_split()
root.update()

nb = find_notebook(root)
check("找到 Notebook", nb is not None)

# ---------- 设置区压缩 ----------
print("\n===== 1. 设置区高度 =====")
req = nb.winfo_reqheight() if nb else 0
print(f"Notebook 请求高度: {req}px（页内视口 {gui.TAB_VIEW_H}px）")
check("设置区不超过视口+页签栏", req <= gui.TAB_VIEW_H + 45, f"{req}px")
for t in nb.tabs():
    w = nb.nametowidget(t)
    check(f"「{nb.tab(t, 'text')}」页高度受限", w.winfo_reqheight() <= gui.TAB_VIEW_H + 12,
          f"{w.winfo_reqheight()}px")

# ---------- 每页都有滚动条 ----------
print("\n===== 2. 滚动条 =====")
for t in nb.tabs():
    name = nb.tab(t, "text")
    bars = [x for x in walk(nb.nametowidget(t))
            if x.winfo_class() in ("Scrollbar", "TScrollbar")]
    check(f"「{name}」有垂直滚动条", len(bars) >= 1, f"{len(bars)} 条")
log_bars = [x for x in walk(app.pw_log) if x.winfo_class() == "Scrollbar"]
check("日志区有滚动条", len(log_bars) >= 1)
seg_bars = [x for x in walk(app.pw_seg) if x.winfo_class() == "TScrollbar"]
check("片段预览有滚动条", len(seg_bars) >= 1)

# ---------- 日志区与预览等高 ----------
print("\n===== 3. 日志/预览等高 =====")
seg = app.pw_seg.winfo_height()
log = app.pw_log.winfo_height()
print(f"预览 {seg}px · 日志 {log}px · 窗口 {root.winfo_width()}x{root.winfo_height()}")
if seg > 1 and log > 1:
    check("日志区与片段预览等高(≤2px)", abs(seg - log) <= 2, f"差 {abs(seg - log)}px")
else:
    print("[i] 该环境未分配真实几何，跳过等高断言")

# 拉高窗口后两边等比例增长，仍保持等高
root.geometry("1180x1000")
root.update_idletasks()
root.update()
app._set_initial_split()
root.update()
seg2, log2 = app.pw_seg.winfo_height(), app.pw_log.winfo_height()
print(f"拉高后: 预览 {seg2}px · 日志 {log2}px")
if seg2 > 1 and log2 > 1:
    check("窗口变高后仍等高(≤2px)", abs(seg2 - log2) <= 2, f"差 {abs(seg2 - log2)}px")

# ---------- 源码接线 ----------
print("\n===== 4. 源码接线 =====")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "jp_subtitle_app", "gui.py"), encoding="utf-8").read()
check("设置区不随窗口长高", 'self.root.rowconfigure(1, weight=0)' in src)
check("两个 pane 等比例增长", src.count("self.pw.add(f_seg, weight=1)") == 1 and
      src.count("self.pw.add(f_log, weight=1)") == 1)
check("翻译/导出页用滚动容器", src.count("self._scroll_host(tab)") >= 4)
check("参数说明页限高", "height=HELP_VIEW_LINES" in src)
check("分隔条按实测高度微调", "self.pw.sashpos(0, pos - (seg - log) // 2)" in src)

root.destroy()
restore()
print("\n" + ("ALL LAYOUT TESTS PASSED" if not FAIL else "FAILED: " + ", ".join(FAIL)))
sys.exit(0 if not FAIL else 1)
