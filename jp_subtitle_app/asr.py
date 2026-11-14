# -*- coding: utf-8 -*-
"""语音识别：本地 faster-whisper 与 OpenAI 兼容 API 转写。"""
import os

MODEL_CHOICES = ["large-v3", "medium", "small", "base", "tiny", "distil-large-v3"]

# 日语语境提示词：让模型按日语书写习惯输出，改善含糊发音的补全与专有名词一致性
JAPANESE_PROMPT = "これは日本語の会話です。自然な日本語で書き起こしてください。"


class ASRError(Exception):
    pass


def cuda_available():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def normalize_segments(segments, silence_gap_sec=0.6, split_long=True,
                       min_gap_sec=0.02):
    """按真实语音边界规整字幕时间轴，让字幕只在有人声时出现：

    1) 长段内部若存在 >= silence_gap_sec 的词间静音，按词边界切成两条，
       静音期间屏幕自动留白（关键修复：Whisper 常把带停顿的整句压成一条字幕，
       导致长沉默中仍挂着上一句）；
    2) 相邻段不重叠：上一段结束不超过下一段开始 - min_gap_sec，
       避免下一条字幕提前出现。

    无词级时间戳的旧缓存只会执行第 2 步（重叠钳制）。
    """
    out = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start") or 0)
        end = float(seg.get("end") or 0)
        words = seg.get("words") or []
        if not split_long or not words:
            out.append({"start": start, "end": end, "text": text})
            continue
        chunks, cur = [], []
        prev_end = None
        for w in words:
            ws = float(w.get("start") or 0)
            we = float(w.get("end") or 0)
            wt = (w.get("word") or "").strip()
            if prev_end is not None and cur and (ws - prev_end) >= silence_gap_sec:
                chunks.append(cur)
                cur = []
            if wt:
                cur.append((ws, we, wt))
            prev_end = we
        if cur:
            chunks.append(cur)
        if len(chunks) <= 1:
            out.append({"start": start, "end": end, "text": text})
            continue
        for ch in chunks:
            ct = "".join(t for _, _, t in ch).strip()
            if ct:
                out.append({"start": ch[0][0], "end": ch[-1][1], "text": ct})

    out.sort(key=lambda s: s["start"])
    for i in range(len(out) - 1):
        nxt = out[i + 1]
        limit = nxt["start"] - min_gap_sec
        if out[i]["end"] > limit:
            out[i]["end"] = max(out[i]["start"] + 0.1, limit)
    return out


class LocalWhisper:
    """faster-whisper 本地识别（Whisper 大模型，首次运行自动下载）。"""

    def __init__(self, model_size="large-v3", device="auto", compute_type=None):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.model = None

    def load(self):
        if self.model is not None:
            return
        from faster_whisper import WhisperModel
        device = self.device
        if device == "auto":
            device = "cuda" if cuda_available() else "cpu"
        compute = self.compute_type or ("float16" if device == "cuda" else "int8")
        try:
            self.model = WhisperModel(self.model_size, device=device, compute_type=compute)
        except Exception:
            # Blackwell 等新卡 float16 可能报错，逐级回退
            if device == "cuda":
                try:
                    self.model = WhisperModel(self.model_size, device="cuda",
                                              compute_type="int8_float16")
                    return
                except Exception:
                    pass
            self.model = WhisperModel(self.model_size, device="cpu", compute_type="int8")

    def transcribe(self, audio, language="ja", vad=True, vad_threshold=0.25,
                   condition_on_previous_text=False, beam_size=5, progress=None,
                   vad_min_silence_ms=400, vad_speech_pad_ms=300,
                   no_speech_threshold=None, log_prob_threshold=None,
                   initial_prompt=None):
        """识别音频。

        关键参数说明（针对轻声/含糊对话）：
        - vad_threshold 调低：更容易把轻声、气声判为人声，减少整句漏识别；
        - vad_speech_pad_ms 调大：片段前后多留余量，避免切掉轻声字头；
        - vad_min_silence_ms 调小：一句话中间的短停顿不算结束，减少碎句；
        - no_speech_threshold 调大：模型更难把一段判成“没人说话”而丢弃；
        - log_prob_threshold 调低：保留低置信度但确实说了的台词；
        - initial_prompt 给一句日语语境，改善汉字/假名书写与含糊发音。
        """
        self.load()
        kw = dict(language=language, task="transcribe", beam_size=beam_size,
                  condition_on_previous_text=condition_on_previous_text,
                  word_timestamps=True)
        if no_speech_threshold is not None:
            kw["no_speech_threshold"] = float(no_speech_threshold)
        if log_prob_threshold is not None:
            kw["log_prob_threshold"] = float(log_prob_threshold)
        if initial_prompt:
            kw["initial_prompt"] = initial_prompt
        if vad:
            kw["vad_filter"] = True
            kw["vad_parameters"] = dict(
                threshold=float(vad_threshold),
                min_silence_duration_ms=int(vad_min_silence_ms),
                speech_pad_ms=int(vad_speech_pad_ms),
            )
        segments_gen, info = self.model.transcribe(audio, **kw)
        total = float(len(audio)) / 16000.0
        segments = []
        for i, seg in enumerate(segments_gen, 1):
            text = seg.text.strip()
            if text:
                words = []
                if getattr(seg, "words", None):
                    for w in seg.words:
                        words.append({"start": w.start, "end": w.end,
                                      "word": w.word})
                segments.append({"start": seg.start, "end": seg.end,
                                 "text": text, "words": words})
            if progress:
                progress(i, seg.end, total)
        return segments, info


class ApiTranscriber:
    """OpenAI 兼容 /audio/transcriptions 云端转写（如 OpenAI whisper-1、通义等）。"""

    def __init__(self, base_url, api_key="", model="whisper-1", timeout=900):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def transcribe(self, wav_path, language="ja", progress=None):
        import requests
        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        with open(wav_path, "rb") as f:
            files = {"file": (os.path.basename(wav_path), f, "audio/wav")}
            data = {"model": self.model, "language": language,
                    "response_format": "verbose_json"}
            resp = requests.post(url, headers=headers, files=files,
                                 data=data, timeout=self.timeout)
        if resp.status_code != 200:
            raise ASRError(f"API 转写失败 (HTTP {resp.status_code}): {resp.text[:300]}")
        js = resp.json()
        segments = []
        for seg in js.get("segments", []):
            text = (seg.get("text") or "").strip()
            if text:
                segments.append({"start": seg.get("start", 0.0),
                                 "end": seg.get("end", 0.0),
                                 "text": text})
        return segments, {"language": js.get("language", language)}
