# -*- coding: utf-8 -*-
"""字幕审查与修正：把影片音轨重新识别成文本作为参照，让大模型校对已有字幕。

流程：
  音轨 → Whisper 听写(ref) → 与现有字幕按时间重叠对齐 → 大模型逐条校对
  → 可选：按 ref 收紧时间轴 / 按句切分 / 补齐漏句 → 写出新字幕文件（不动原文件）

设计约束：大模型不能听音频，所以参照文本必须由 ASR 产生；大模型只改文本，
时间轴与切句由本地规则处理，避免模型输出不可靠的时间戳。
"""
import bisect
import re
import time

from . import asr
from .translator import translate_with_retry

REVIEW_SYSTEM = (
    "你是影视字幕校对员。每条字幕给你三个字段：\n"
    "  ref：由语音识别模型从影片音轨听写出的日语文本（语音事实来源，但可能有识别错误）\n"
    "  ja：现有字幕的日文\n"
    "  zh：现有字幕的中文译文（可能为空）\n"
    "任务：\n"
    "1) 以 ref 为准修正 ja 的同音错字、漏字、错误断句、助词误写；\n"
    "2) ref 明显是识别噪声（乱码、整句重复、无意义的拟声）时，保留 ja 的原有合理写法；\n"
    "3) zh 必须忠实反映修正后的 ja：口语自然、简洁、术语前后一致、人名地名用通行译法；\n"
    "4) 不增删条目，不改动时间轴，不解释；\n"
    "5) 输出严格逐行格式：`序号: 修正后日文 ||| 修正后中文`；\n"
    "   该条没有中文时只输出 `序号: 修正后日文`；无需修改也原样输出；\n"
    "   不要输出代码块、注释、说明文字。"
)

TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
KANA_RE = re.compile(r"[\u3040-\u309f\u30ff-\u30ff\uff66-\uff9f]")
KANJI_RE = re.compile(r"[\u4e00-\u9fff]")
SENT_SPLIT_RE = re.compile(r"[。！？!?]+|\n+")

DEFAULT_OPTS = {
    "fix_text": True,           # 修正日文与中文文本
    "fix_timing": True,         # 按参照收紧字幕起止时间
    "add_missing": True,        # 补齐音轨里说了但字幕没有的句子
    "split_sentences": True,    # 一条字幕跨过多个语音段且中间有静音时按句切开
    "fix_shift": None,          # 是否纠正整体时间偏移（None=跟随 fix_timing）
    "min_overlap": 0.35,        # 字幕与语音段的重叠比例阈值，判定为同一条
    "time_pad_sec": 0.25,       # 收紧时间轴后前后保留的余量
    "split_gap_sec": 0.8,       # 句间静音达到该值才切句
    "min_missing_sec": 0.35,    # 补句的最短语音时长
    "batch_lines": 20,          # 每次送给模型的条数
    "batch_chars": 1600,        # 每次送给模型的最大字符数
}


# ---------------------------------------------------------------- SRT 解析
def parse_timestamp(text):
    m = TIME_RE.search(text)
    if not m:
        return None, None
    g = m.groups()

    def to_sec(h, mi, s, ms):
        return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0

    return to_sec(*g[:4]), to_sec(*g[4:])


def _has_kana(line):
    return bool(KANA_RE.search(line))


def _has_cjk(line):
    return bool(KANJI_RE.search(line))


def parse_srt(text):
    """解析 SRT 文本，按字形区分日文/中文行，返回 [{start,end,ja,zh}]。

    双语字幕（中文在上、日文在下）按脚本自动拆分；纯假名/汉字组成的日文行
    靠全文件假名占比判断归属，避免把汉字日文行误判成中文。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    blocks = [b for b in blocks if b.strip()]

    # 先判断整个文件是不是双语：某个块里同时出现“带假名的行”和“纯汉字行”，
    # 就是中文在上/日文在下的双语字幕，纯汉字行按中文处理。
    body_lines = []
    for b in blocks:
        lines = [ln.strip() for ln in b.split("\n") if ln.strip()]
        if len(lines) >= 2 and re.fullmatch(r"\d+", lines[0]):
            lines = lines[1:]
        body_lines.append(lines[1:])
    bilingual_file = any(
        any(_has_kana(ln) for ln in body) and
        any(_has_cjk(ln) and not _has_kana(ln) for ln in body)
        for body in body_lines)

    kana_blocks = sum(1 for body in body_lines if any(_has_kana(ln) for ln in body))
    kana_ratio = kana_blocks / len(blocks) if blocks else 0.0
    # 单语文件里，纯汉字行只有在“本文件以日语为主”时才当归日文
    kanji_is_ja = (not bilingual_file) and kana_ratio >= 0.4

    segs = []
    for b in blocks:
        lines = [ln.strip() for ln in b.split("\n") if ln.strip()]
        if len(lines) >= 2 and re.fullmatch(r"\d+", lines[0]):
            lines = lines[1:]
        if not lines:
            continue
        start, end = parse_timestamp(lines[0])
        if start is None:
            continue
        ja, zh = [], []
        for ln in lines[1:]:
            if _has_kana(ln):
                ja.append(ln)
            elif _has_cjk(ln):
                (ja if kanji_is_ja else zh).append(ln)
            elif ln:
                ja.append(ln)
        segs.append({"start": start, "end": end,
                     "ja": " ".join(ja).strip(), "zh": " ".join(zh).strip()})
    return segs


def build_srt(segments):
    """按每条字幕自身的语言组成生成 SRT：中日都有则中文在上、日文在下。"""
    out = []
    for i, seg in enumerate(segments, 1):
        ja = (seg.get("ja") or "").strip()
        zh = (seg.get("zh") or "").strip()
        if ja and zh:
            line = f"{zh}\n{ja}"
        else:
            line = ja or zh
        if not line.strip():
            continue
        out.append(f"{i}\n{srt_ts(seg['start'])} --> {srt_ts(seg['end'])}\n{line}\n")
    return "\n".join(out)


def srt_ts(seconds):
    from .srt_builder import format_timestamp
    return format_timestamp(seconds)


def write_srt(segments, path):
    """写 UTF-8 with BOM，Windows 播放器兼容性最好。"""
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(build_srt(segments))
    return path


# ---------------------------------------------------------------- 对齐
def _dur(s):
    return max(0.0, float(s["end"]) - float(s["start"]))


def _overlap(a, b):
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def _matched(a, b, thr):
    shorter = min(_dur(a), _dur(b))
    if shorter <= 0:
        return abs(a["start"] - b["start"]) <= 0.3
    return _overlap(a, b) / shorter >= thr


def estimate_offset(ref, subs, max_shift=12.0, tol=0.6):
    """估计字幕相对音轨的整体时间偏移（秒），正数=字幕比语音晚。

    做法：每条语音段找最近的字幕起点，取这些差值的**中位数**（对漏句、多句不敏感），
    并要求足够多的语音段支持这个偏移量，否则认为没有偏移。
    存在整体偏移时（换版本、裁掉片头、帧率差异很常见），若不先纠正，
    短字幕会完全对不上，补句逻辑还会把已有台词重复插入。
    """
    if not ref or not subs:
        return 0.0
    starts = sorted(float(s["start"]) for s in subs)
    if len(starts) < 2:
        return 0.0
    deltas = []
    for r in ref:
        rs = float(r["start"])
        i = bisect.bisect_left(starts, rs)
        cands = [starts[j] for j in (i - 1, i) if 0 <= j < len(starts)]
        if not cands:
            continue
        nearest = min(cands, key=lambda s: abs(s - rs))
        d = rs - nearest
        if abs(d) <= max_shift:
            deltas.append(d)
    if len(deltas) < 3:
        return 0.0
    deltas.sort()
    med = deltas[len(deltas) // 2]
    support = sum(1 for d in deltas if abs(d - med) <= tol)
    if support < max(3, len(deltas) * 0.5):
        return 0.0
    return round(med, 2)


def align(ref, subs, thr=0.35):
    """按时间重叠把字幕与语音段配对。

    返回 (sub_refs, unmatched_ref, orphan_sub)：
      sub_refs[i]   → 该字幕命中的语音段下标列表（可能多个，说明一条字幕跨多句）
      unmatched_ref → 没有任何字幕命中的语音段下标（候选漏句）
      orphan_sub    → 没有任何语音段命中的字幕下标（音轨里没语音，保留不动）
    """
    sub_refs = {i: [] for i in range(len(subs))}
    ref_hit = {j: [] for j in range(len(ref))}
    for i, s in enumerate(subs):
        for j, r in enumerate(ref):
            if _matched(s, r, thr):
                sub_refs[i].append(j)
                ref_hit[j].append(i)
    unmatched = [j for j, lst in ref_hit.items() if not lst]
    orphan = [i for i, lst in sub_refs.items() if not lst]
    return sub_refs, unmatched, orphan


def split_text(text, n):
    """把文本按句末标点切成 n 段；切不出 n 段返回 None（宁可不切，也不乱切）。"""
    parts = [p.strip() for p in SENT_SPLIT_RE.split(text) if p.strip()]
    if len(parts) < n:
        return None
    if len(parts) > n:
        # 多出的部分并入最后一段，保证总字符不丢
        parts = parts[:n - 1] + ["。".join(parts[n - 1:])]
    return parts


# ---------------------------------------------------------------- 模型输出解析
def parse_review(content, expected):
    """解析 `序号: 日文 ||| 中文` 格式，容错：代码块、全角竖线、缺中文。"""
    out = [("", "")] * expected
    if not content:
        return out
    content = content.strip()
    content = re.sub(r"^```[a-zA-Z]*\s*", "", content).strip()
    content = re.sub(r"\s*```$", "", content).strip()
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d{1,4})\s*[:：.、)）]\s*(.+)$", line)
        if not m:
            continue
        idx = int(m.group(1))
        if not (1 <= idx <= expected):
            continue
        body = m.group(2)
        parts = re.split(r"\|\|\||｜｜｜|\|\s*\|\s*", body, maxsplit=1)
        ja = parts[0].strip()
        zh = parts[1].strip() if len(parts) > 1 else ""
        out[idx - 1] = (ja, zh)
    return out


def review_batch(client, items):
    """一次批量校对，items 为 [(ref_text, segment)]，返回 [(ja, zh)]。"""
    header = f"请逐条校对以下 {len(items)} 条字幕，每条一行，格式：序号: 日文 ||| 中文\n\n"
    blocks = []
    for n, (ref_text, seg) in enumerate(items, 1):
        blocks.append(f"{n}\nref: {ref_text or '(无听写文本)'}\n"
                      f"ja: {seg.get('ja', '')}\nzh: {seg.get('zh', '')}")
    content = client.chat([{"role": "system", "content": REVIEW_SYSTEM},
                           {"role": "user", "content": header + "\n\n".join(blocks)}],
                          max_tokens=2048)
    return parse_review(content, len(items))


def review_with_retry(client, items, retries=2):
    last = None
    for attempt in range(1, retries + 1):
        try:
            return review_batch(client, items), None
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.5)
    return [("", "")] * len(items), last


def _make_batches(items, max_lines, max_chars):
    batches, cur, n = [], [], 0
    for it in items:
        size = len(it[0]) + len(it[1].get("ja", "")) + len(it[1].get("zh", "")) + 8
        if cur and (n + size > max_chars or len(cur) >= max_lines):
            batches.append(cur)
            cur, n = [], 0
        cur.append(it)
        n += size
    if cur:
        batches.append(cur)
    return batches


# ---------------------------------------------------------------- 主流程
def review_segments(subs, ref, client, opts=None, progress=None, log=None):
    """审查并修正字幕。

    subs: parse_srt 的结果；ref: ASR 段 [{start,end,text,words?}]。
    返回 (new_segments, report)，report 为 [{kind,index,note,before,after}]。
    """
    o = dict(DEFAULT_OPTS)
    if opts:
        o.update({k: v for k, v in opts.items() if v is not None})
    if o.get("fix_shift") is None:
        o["fix_shift"] = bool(o["fix_timing"])
    report = []

    def say(msg):
        if log:
            log(msg)

    ref_norm = asr.normalize_segments(ref, silence_gap_sec=o["split_gap_sec"],
                                     split_long=True)

    # 先估整体偏移：字幕常见“整体提前/滞后”，不先纠正的话短句对不上、
    # 补句还会把已有台词当漏句重复插入。
    offset = 0.0
    if o.get("fix_shift", True) and len(subs) >= 3:
        offset = estimate_offset(ref_norm, subs)
        if abs(offset) >= 0.4:
            report.append({"kind": "offset", "start": 0.0,
                           "note": f"字幕整体与音轨相差 {offset:+.2f}s，已按音轨对齐",
                           "before": f"{offset:+.2f}s", "after": "0.00s"})
            say(f"[*] 检测到整体时间偏移 {offset:+.2f}s，按音轨对齐后再审查")
    match_subs = subs
    if abs(offset) >= 0.4:
        match_subs = [{"start": float(s["start"]) + offset,
                       "end": float(s["end"]) + offset,
                       "ja": s.get("ja", ""), "zh": s.get("zh", "")} for s in subs]

    sub_refs, unmatched, orphan = align(ref_norm, match_subs, o["min_overlap"])

    # ---------- 1. 文本校对 ----------
    matched = [(i, sub_refs[i]) for i in range(len(subs)) if sub_refs[i]]
    if o["fix_text"] and matched:
        items = []
        for i, js in matched:
            ref_text = " ".join(ref_norm[j]["text"] for j in js)
            items.append((ref_text, subs[i]))
        batches = _make_batches(items, o["batch_lines"], o["batch_chars"])
        say(f"[*] 字幕审查: {len(items)} 条 / {len(batches)} 批送模型校对")
        done = 0
        for b in batches:
            out, err = review_with_retry(client, b)
            if err:
                say(f"[!] 一批审查失败: {err}")
            for (ref_text, seg), (ja, zh) in zip(b, out):
                if ja:
                    old = seg.get("ja", "")
                    if ja != old:
                        report.append({"kind": "ja", "start": seg["start"],
                                       "note": "日文按音轨听写修正",
                                       "before": old, "after": ja})
                        seg["ja"] = ja
                if zh and o["fix_text"]:
                    old = seg.get("zh", "")
                    if zh != old:
                        report.append({"kind": "zh", "start": seg["start"],
                                       "note": "中文译文按日文原文重写",
                                       "before": old, "after": zh})
                        seg["zh"] = zh
            done += len(b)
            if progress:
                progress(done, len(items))
    elif not o["fix_text"]:
        say("[i] 未启用文本修正，只处理时间轴与漏句")

    # ---------- 2. 切句 / 时间轴 ----------
    new_subs = []
    for i, s in enumerate(subs):
        js = sub_refs[i]
        if not js:
            if i in orphan:
                report.append({"kind": "orphan", "start": s["start"],
                               "note": "音轨该时间段没检到语音，原样保留",
                               "before": s.get("ja", "") or s.get("zh", ""), "after": ""})
            new_subs.append(s)
            continue
        rs = [ref_norm[j] for j in js]
        if o["split_sentences"] and len(rs) > 1:
            gaps = [rs[k + 1]["start"] - rs[k]["end"] for k in range(len(rs) - 1)]
            ja_parts = split_text(s.get("ja", ""), len(rs)) if s.get("ja") else None
            zh_parts = split_text(s.get("zh", ""), len(rs)) if s.get("zh") else None
            if max(gaps) >= o["split_gap_sec"] and ja_parts and \
                    (not s.get("zh") or zh_parts):
                pad = o["time_pad_sec"]
                for k in range(len(rs)):
                    new_subs.append({"start": max(0.0, rs[k]["start"] - pad),
                                     "end": rs[k]["end"] + pad,
                                     "ja": ja_parts[k],
                                     "zh": zh_parts[k] if zh_parts else s.get("zh", "")})
                report.append({"kind": "split", "start": s["start"],
                               "note": f"一条字幕跨 {len(rs)} 句且句间静音 "
                                       f"{max(gaps):.1f}s → 切成 {len(rs)} 条",
                               "before": s.get("ja", ""), "after": ""})
                continue
            if max(gaps) >= o["split_gap_sec"]:
                report.append({"kind": "split_skip", "start": s["start"],
                               "note": "句间有静音但文本无法按句对应，未切句（可关“改时间轴”后手动切）",
                               "before": s.get("ja", ""), "after": ""})
        if o["fix_timing"]:
            pad = o["time_pad_sec"]
            ns = max(0.0, min(r["start"] for r in rs) - pad)
            ne = max(r["end"] for r in rs) + pad
            if ne - ns < 0.35:
                ne = ns + 0.35
            if abs(ns - s["start"]) > 0.3 or abs(ne - s["end"]) > 0.3:
                report.append({"kind": "timing", "start": s["start"],
                               "note": "时间轴按音轨收紧",
                               "before": f"{srt_ts(s['start'])} --> {srt_ts(s['end'])}",
                               "after": f"{srt_ts(ns)} --> {srt_ts(ne)}"})
                s["start"], s["end"] = ns, ne
        new_subs.append(s)

    # ---------- 3. 补漏句 ----------
    added = []
    if o["add_missing"] and unmatched:
        cands = []
        for j in unmatched:
            r = ref_norm[j]
            text = (r.get("text") or "").strip()
            if len(text) < 2 or _dur(r) < o["min_missing_sec"]:
                continue
            heavy = max((_overlap(r, s) / max(_dur(r), 0.1) for s in new_subs),
                        default=0.0)
            if heavy >= 0.6:
                report.append({"kind": "skip_missing", "start": r["start"],
                               "note": "听写段与现有字幕大部分重叠，未补句（可下调匹配阈值再试）",
                               "before": text, "after": ""})
                continue
            cands.append((j, r, text))
        if cands:
            say(f"[*] 音轨有语音但字幕缺失: {len(cands)} 句候选，翻译后补入")
            zh_list, err = translate_with_retry(client, [t for _, _, t in cands])
            if err:
                say(f"[!] 漏句翻译失败，跳过补句: {err}")
                zh_list = []
            pad = o["time_pad_sec"]
            for (j, r, text), zh in zip(cands, zh_list or []):
                added.append({"start": max(0.0, r["start"] - pad),
                              "end": r["end"] + pad,
                              "ja": text, "zh": (zh or "").strip()})
                report.append({"kind": "missing", "start": r["start"],
                               "note": "补齐音轨说了但字幕没有的句子",
                               "before": "", "after": f"{(zh or '').strip()} / {text}"})
    elif o["add_missing"]:
        say("[i] 未发现漏句")

    merged = new_subs + added
    merged.sort(key=lambda s: (s["start"], s["end"]))
    # 相邻段不重叠：上一段结束不超过下一段开始，避免两条字幕同时压屏
    for i in range(len(merged) - 1):
        limit = merged[i + 1]["start"] - 0.02
        if merged[i]["end"] > limit:
            merged[i]["end"] = max(merged[i]["start"] + 0.1, limit)
    final = [s for s in merged
             if (s.get("ja") or s.get("zh")).strip() and s["end"] > s["start"]]

    n_timing = sum(1 for r in report if r["kind"] == "timing")
    n_text = sum(1 for r in report if r["kind"] in ("ja", "zh"))
    n_missing = sum(1 for r in report if r["kind"] == "missing")
    say(f"[OK] 审查完成: 文本改动 {n_text} 处 · 时间轴调整 {n_timing} 条 · "
        f"补句 {n_missing} 条 · 切句 {sum(1 for r in report if r['kind'] == 'split')} 条")
    return final, report


def format_report(report, limit_per_kind=200):
    """把 report 转成日志文本行。"""
    lines = []
    counts = {}
    for r in report:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        if counts[r["kind"]] > limit_per_kind:
            continue
        t = srt_ts(r["start"])
        if r["kind"] == "timing":
            lines.append(f"  {t}  时间轴 {r['before']} → {r['after']}")
        elif r["kind"] == "offset":
            lines.append(f"  整体偏移: {r['note']}")
        elif r["kind"] == "missing":
            lines.append(f"  {t}  补句: {r['after']}")
        elif r["kind"] == "split":
            lines.append(f"  {t}  切句: {r['note']}")
        elif r["kind"] == "skip_missing":
            lines.append(f"  {t}  未补句: {r['note']} · {r['before'][:40]}")
        elif r["kind"] == "split_skip":
            lines.append(f"  {t}  未切句: {r['note']}")
        elif r["kind"] == "orphan":
            lines.append(f"  {t}  音轨无语音，保留: {r['before'][:40]}")
        elif r["kind"] == "ja":
            lines.append(f"  {t}  日文: {r['before']} → {r['after']}")
        elif r["kind"] == "zh":
            lines.append(f"  {t}  中文: {r['before']} → {r['after']}")
    if counts:
        head = " · ".join(f"{k}×{v}" for k, v in sorted(counts.items()))
        lines.insert(0, f"[i] 审查报告 ({head})")
    return lines
