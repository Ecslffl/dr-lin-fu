# -*- coding: utf-8 -*-
"""验证字幕审查：SRT 解析、时间对齐、模型输出解析、切句/补漏/时间轴修正。

用假的 LLMClient 替代真实大模型，只测本地逻辑。
"""
import os
import sys
import tempfile

sys.path.insert(0, r"D:\AI_Work\SRT")
from jp_subtitle_app import review

SAMPLE_BILINGUAL = """1
00:00:01,000 --> 00:00:03,000
你好。
こんにちは。

2
00:00:04,000 --> 00:00:06,500
今天天气不错。
いい天気ですね。
"""

subs = review.parse_srt(SAMPLE_BILINGUAL)
assert len(subs) == 2, len(subs)
assert subs[0]["zh"] == "你好。" and subs[0]["ja"] == "こんにちは。", subs[0]
assert abs(subs[1]["start"] - 4.0) < 0.01 and abs(subs[1]["end"] - 6.5) < 0.01, subs[1]
print("[OK] 双语 SRT 解析: 中文/日文按字形正确分离, 时间戳正确")

ja_only = """1
00:00:01,000 --> 00:00:02,000
明日は晴れです

2
00:00:03,000 --> 00:00:04,000
学校へ行く
"""
subs_ja = review.parse_srt(ja_only)
assert len(subs_ja) == 2 and subs_ja[0]["ja"].startswith("明日"), subs_ja
assert subs_ja[0]["zh"] == "", subs_ja[0]
print("[OK] 纯日文字幕解析: 汉字行没有被误判成中文")


class FakeClient:
    """最小可用的假模型：日文统一加标记，中文统一加标记，翻译原样返回。"""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, max_tokens=2048):
        self.calls += 1
        user = messages[-1]["content"]
        n = sum(1 for ln in user.splitlines() if ln.strip().isdigit())
        lines = []
        for i in range(1, n + 1):
            lines.append(f"{i}: 修正{i} ||| 译文{i}")
        return "\n".join(lines)

    def translate_batch(self, texts):
        return [f"译:{t}" for t in texts]


# 场景1：文本修正
subs = review.parse_srt(SAMPLE_BILINGUAL)
ref = [{"start": 1.0, "end": 3.0, "text": "こんにちは"},
       {"start": 4.0, "end": 6.5, "text": "いい天気ですね"}]
out, rep = review.review_segments(subs, ref, FakeClient(),
                                  {"fix_timing": False, "add_missing": False,
                                   "split_sentences": False})
assert out[0]["ja"].startswith("修正"), out[0]
assert out[0]["zh"].startswith("译文"), out[0]
kinds = {r["kind"] for r in rep}
assert "ja" in kinds and "zh" in kinds, kinds
print(f"[OK] 文本修正: {len(out)} 条, 报告类型={sorted(kinds)}")

# 场景2：时间轴收紧（字幕 0~10s，语音只在 2~4s）
subs = [{"start": 0.0, "end": 10.0, "ja": "こんにちは", "zh": "你好"}]
ref = [{"start": 2.0, "end": 4.0, "text": "こんにちは"}]
out, rep = review.review_segments(subs, ref, FakeClient(),
                                  {"fix_text": False, "add_missing": False})
assert abs(out[0]["start"] - 1.75) < 0.01 and abs(out[0]["end"] - 4.25) < 0.01, out[0]
assert any(r["kind"] == "timing" for r in rep), rep
print(f"[OK] 时间轴收紧: 0~10s -> {out[0]['start']:.2f}~{out[0]['end']:.2f}s")

# 场景3：一条字幕跨两句且句间有静音 -> 切句
subs = [{"start": 0.0, "end": 10.0, "ja": "おはよう。さようなら。", "zh": "早上好。再见。"}]
ref = [{"start": 0.5, "end": 2.0, "text": "おはよう"},
       {"start": 6.0, "end": 8.0, "text": "さようなら"}]
out, rep = review.review_segments(subs, ref, FakeClient(),
                                  {"fix_text": False, "add_missing": False})
assert len(out) == 2, out
assert out[0]["ja"].startswith("おはよう") and out[1]["ja"].startswith("さようなら"), out
print(f"[OK] 跨句切分: 1 条 -> {len(out)} 条 ({out[0]['start']:.2f}s / {out[1]['start']:.2f}s)")

# 场景4：补漏句（语音段没有任何字幕命中）
subs = [{"start": 0.0, "end": 2.0, "ja": "おはよう", "zh": "早上好"}]
ref = [{"start": 0.0, "end": 2.0, "text": "おはよう"},
       {"start": 20.0, "end": 23.0, "text": "ありがとう"}]
out, rep = review.review_segments(subs, ref, FakeClient(),
                                  {"fix_text": False, "fix_timing": False})
assert len(out) == 2, [o["ja"] for o in out]
added = [o for o in out if o["ja"] == "ありがとう"]
assert added and added[0]["zh"] == "译:ありがとう", added
assert any(r["kind"] == "missing" for r in rep), rep
print(f"[OK] 补漏句: 新增 1 条 -> {added[0]['zh']} / {added[0]['ja']}")

# 场景5：模型输出容错（代码块、全角冒号、只有日文）
parsed = review.parse_review("```\n1：修正后日文 ||| 修正后中文\n2: 只有日文\n```", 2)
assert parsed[0] == ("修正后日文", "修正后中文"), parsed
assert parsed[1] == ("只有日文", ""), parsed
print("[OK] 模型输出容错: 代码块/全角冒号/缺中文")

# 场景6：整体时间偏移估计与纠正（字幕整体提前 1.5s）
# 真实素材上踩到过：偏移不先纠正，短句会完全对不上，补句还会重复插入。
subs_shift = [{"start": 0.0, "end": 2.0, "ja": "あ", "zh": "啊"},
              {"start": 10.0, "end": 12.0, "ja": "い", "zh": "咿"},
              {"start": 20.0, "end": 22.0, "ja": "う", "zh": "呜"}]
ref_shift = [{"start": 1.5, "end": 3.5, "text": "あ"},
             {"start": 11.5, "end": 13.5, "text": "い"},
             {"start": 21.5, "end": 23.5, "text": "う"}]
off = review.estimate_offset(ref_shift, subs_shift)
assert abs(off - 1.5) < 0.11, off
print(f"[OK] 偏移估计: 字幕提前 1.5s -> 估计 {off:+.2f}s")

aligned_ref = [{"start": 1.0, "end": 2.0, "text": "あ"},
               {"start": 3.0, "end": 4.0, "text": "い"},
               {"start": 5.0, "end": 6.0, "text": "う"}]
aligned_subs = [{"start": 1.0, "end": 2.0, "ja": "あ", "zh": "啊"},
                {"start": 3.0, "end": 4.0, "ja": "い", "zh": "咿"},
                {"start": 5.0, "end": 6.0, "ja": "う", "zh": "呜"}]
assert review.estimate_offset(aligned_ref, aligned_subs) == 0.0
print("[OK] 偏移估计: 本来对齐的字幕不误报偏移")

out, rep = review.review_segments(subs_shift, ref_shift, FakeClient(),
                                  {"fix_text": False, "add_missing": False,
                                   "split_sentences": False})
assert any(r["kind"] == "offset" for r in rep), rep
assert abs(out[0]["start"] - 1.25) < 0.05, out[0]   # ref.start - 0.25 余量
print(f"[OK] 偏移纠正: 首条起点 {out[0]['start']:.2f}s, 报告含 offset")

# 场景7：写文件带 BOM，双语中文在上
tmp = os.path.join(tempfile.gettempdir(), "review_test.srt")
review.write_srt([{"start": 1.0, "end": 2.0, "ja": "こんにちは", "zh": "你好"}], tmp)
raw = open(tmp, "rb").read()
assert raw.startswith(b"\xef\xbb\xbf"), raw[:3]
body = raw.decode("utf-8-sig").replace("\r\n", "\n")
assert "你好\nこんにちは" in body, body
os.remove(tmp)
print("[OK] 输出文件: UTF-8 BOM, 中文在上/日文在下")

print("ALL TESTS PASSED")
