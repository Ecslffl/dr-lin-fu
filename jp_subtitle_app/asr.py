# -*- coding: utf-8 -*-
"""语音识别：本地 faster-whisper 与 OpenAI 兼容 API 转写。"""
import os

MODEL_CHOICES = ["large-v3", "medium", "small", "base", "tiny", "distil-large-v3"]


class ASRError(Exception):
    pass


def cuda_available():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


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

    def transcribe(self, audio, language="ja", vad=True, vad_threshold=0.35,
                   condition_on_previous_text=False, beam_size=5, progress=None):
        self.load()
        kw = dict(language=language, task="transcribe", beam_size=beam_size,
                  condition_on_previous_text=condition_on_previous_text)
        if vad:
            kw["vad_filter"] = True
            kw["vad_parameters"] = dict(threshold=vad_threshold,
                                        min_silence_duration_ms=500,
                                        speech_pad_ms=200)
        segments_gen, info = self.model.transcribe(audio, **kw)
        total = float(len(audio)) / 16000.0
        segments = []
        for i, seg in enumerate(segments_gen, 1):
            text = seg.text.strip()
            if text:
                segments.append({"start": seg.start, "end": seg.end, "text": text})
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
