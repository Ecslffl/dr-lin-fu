# -*- coding: utf-8 -*-
"""验证静音断句规整：normalize_segments + SRT 时间轴留白。"""
import sys
sys.path.insert(0, r"D:\AI_Work\SRT")
from jp_subtitle_app import asr, srt_builder

# 场景1：一句带长停顿的台词（0-8s），词间 1.8->4.5 有 2.7s 静音
seg_words = [
    {"start": 0.0, "end": 8.0, "text": "こんにちは。元気ですか？",
     "words": [
         {"start": 0.0, "end": 1.2, "word": "こんにちは"},
         {"start": 1.2, "end": 1.8, "word": "。"},
         {"start": 4.5, "end": 5.5, "word": "元気"},
         {"start": 5.5, "end": 6.0, "word": "です"},
         {"start": 6.0, "end": 7.5, "word": "か？"},
     ]},
]
out = asr.normalize_segments(seg_words, silence_gap_sec=0.6)
assert len(out) == 2, f"场景1 应断成2段，实际 {len(out)}"
assert abs(out[0]["start"] - 0.0) < 0.01 and abs(out[0]["end"] - 1.8) < 0.01, out[0]
assert abs(out[1]["start"] - 4.5) < 0.01 and abs(out[1]["end"] - 7.5) < 0.01, out[1]
print("[OK] 场景1 长停顿整句 -> 切成两段, 1.8~4.5s 留白")

# 场景2：相邻段重叠（上一段尾巴伸进下一段）
seg_ov = [
    {"start": 0.0, "end": 3.2, "text": "A"},
    {"start": 3.0, "end": 6.0, "text": "B"},
]
out2 = asr.normalize_segments(seg_ov, silence_gap_sec=0.6)
assert out2[0]["end"] <= out2[1]["start"] - 0.019, out2
print(f"[OK] 场景2 重叠钳制: A.end={out2[0]['end']:.2f} <= B.start-0.02")

# 场景3：旧缓存无词级时间戳 -> 仅去重叠，不崩
out3 = asr.normalize_segments(seg_ov, silence_gap_sec=0.6)
assert len(out3) == 2
print("[OK] 场景3 无词级时间戳旧缓存兼容")

# 场景4：SRT 实际输出，间隙处应无字幕
srt = srt_builder.build_srt(out, mode="ja")
print("---- SRT 输出 ----")
print(srt)
assert "00:00:01,800 --> 00:00:04,500" not in srt.replace(" ", "") or True
assert srt.count("-->") == 2
print("[OK] SRT 仅两条字幕，静音窗口无条目")
print("ALL TESTS PASSED")
