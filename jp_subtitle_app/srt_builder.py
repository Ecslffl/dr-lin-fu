# -*- coding: utf-8 -*-
"""SRT 字幕生成与导出。"""
import os


def format_timestamp(seconds):
    """格式化 SRT 时间戳: HH:MM:SS,mmm"""
    if seconds is None or seconds < 0:
        seconds = 0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    whole = int(secs)
    millis = int(round((secs - whole) * 1000))
    if millis >= 1000:
        millis = 999
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"


def build_srt(segments, mode="bilingual"):
    """生成 SRT 文本。
    mode: bilingual=中文上/日文下, zh=仅中文, ja=仅日文
    """
    out = []
    for i, seg in enumerate(segments, 1):
        ja = (seg.get("text") or "").strip()
        zh = (seg.get("zh") or "").strip()
        if mode == "ja":
            line = ja
        elif mode == "zh":
            line = zh or ja
        else:  # bilingual：中文在上、日文在下；中文缺失时只显示日文
            line = f"{zh}\n{ja}" if zh else ja
        if not line.strip():
            continue
        out.append(
            f"{i}\n"
            f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n"
            f"{line}\n"
        )
    return "\n".join(out)


def export_srt(segments, output_path, mode="bilingual"):
    """写 SRT 文件（UTF-8 with BOM，Windows 播放器兼容性最好）。"""
    content = build_srt(segments, mode)
    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write(content)
    return output_path


def default_output_paths(video_path, mode, suffix=""):
    """返回默认输出路径列表。
    - bilingual: <视频>.srt（与视频同名，PotPlayer 自动加载）
    - zh/ja:     <视频><后缀>.<中文/日文>.srt
    - separate:  <视频><后缀>.zh.srt 与 <视频><后缀>.ja.srt
    """
    base = os.path.splitext(video_path)[0]
    if mode == "separate":
        return [base + suffix + ".zh.srt", base + suffix + ".ja.srt"]
    if mode == "zh":
        return [base + suffix + ".中文.srt"]
    if mode == "ja":
        return [base + suffix + ".日文.srt"]
    return [base + suffix + ".srt"]
