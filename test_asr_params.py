# -*- coding: utf-8 -*-
"""验证：音频滤镜链可执行 / 识别参数确实传进 Whisper / 旧配置迁移只升级未改动项。"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jp_subtitle_app import audio, asr, config

FAIL = []


def check(name, cond, detail=""):
    print(("[OK] " if cond else "[FAIL] ") + name + (("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


# ---------- 1. 滤镜链字符串 ----------
print("\n===== 1. 音频滤镜链 =====")
f_quiet = audio._afilter("dynaudnorm", gain_db=10, denoise=True)
f_none = audio._afilter("none", gain_db=8, denoise=False)
f_plain = audio._afilter("dynaudnorm", gain_db=0, denoise=False)
print("轻声档 :", f_quiet)
print("原音+增益:", f_none)
print("默认档 :", f_plain)
check("轻声档含降噪", "afftdn" in f_quiet)
check("轻声档含增益", "volume=10dB" in f_quiet)
check("增益顺序在归一化之前",
      f_quiet.index("volume=") < f_quiet.index("dynaudnorm="))
check("none+增益自动加限幅", "alimiter" in f_none)
check("无增益不加限幅", "alimiter" not in f_plain)

# ---------- 2. 滤镜链真实执行（用 ffmpeg 造一段“很轻”的音频） ----------
print("\n===== 2. 滤镜链实测（30dB 以下的很轻音频） =====")
tmp_in = os.path.join(os.environ["TEMP"], "asr_test_quiet.wav")
exe = audio.find_ffmpeg()
import subprocess
subprocess.run([exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=300:duration=4",
                "-af", "volume=-34dB", "-ar", "16000", "-ac", "1",
                "-c:a", "pcm_s16le", tmp_in], check=True)
import numpy as np
raw = audio.decode_audio_pcm(tmp_in, normalize="none", gain_db=0, denoise=False)
proc = audio.decode_audio_pcm(tmp_in, normalize="dynaudnorm", gain_db=10, denoise=True)
peak_raw = float(np.max(np.abs(raw)))
peak_proc = float(np.max(np.abs(proc)))
print(f"原始峰值={peak_raw:.4f}  处理后峰值={peak_proc:.4f}  提升={20*np.log10(max(peak_proc,1e-9)/max(peak_raw,1e-9)):.1f}dB")
check("滤镜链可执行且输出非空", proc.size > 0, f"{proc.size} 样本")
check("轻声被显著放大", peak_proc > peak_raw * 1.5, f"{peak_raw:.4f}→{peak_proc:.4f}")
check("放大后未削顶", peak_proc <= 1.0, f"peak={peak_proc:.4f}")

# ---------- 3. 识别参数是否真的传给模型 ----------
print("\n===== 3. 识别参数传递（用假模型捕获 kwargs） =====")
captured = {}


class FakeModel:
    def transcribe(self, audio_in, **kw):
        captured.update(kw)
        seg = types.SimpleNamespace(
            start=0.2, end=2.5, text=" 小声で話しています。 ",
            words=[types.SimpleNamespace(start=0.2, end=0.9, word="小声で"),
                   types.SimpleNamespace(start=0.9, end=2.5, word="話しています。")],
            no_speech_prob=0.2, avg_logprob=-0.4)
        return iter([seg]), types.SimpleNamespace(language="ja")


w = asr.LocalWhisper("large-v3", "cpu")
w.model = FakeModel()
segs, info = w.transcribe(np.zeros(16000 * 5, dtype=np.float32),
                          language="ja", vad=True, vad_threshold=0.18,
                          vad_min_silence_ms=300, vad_speech_pad_ms=450,
                          no_speech_threshold=0.9, log_prob_threshold=-1.6,
                          initial_prompt=asr.JAPANESE_PROMPT,
                          beam_size=8, condition_on_previous_text=False)
print("vad_parameters:", captured.get("vad_parameters"))
print("no_speech_threshold:", captured.get("no_speech_threshold"),
      " log_prob_threshold:", captured.get("log_prob_threshold"))
check("vad_parameters 含细项", captured.get("vad_parameters", {}).get("speech_pad_ms") == 450
      and captured.get("vad_parameters", {}).get("min_silence_duration_ms") == 300)
check("VAD 阈值已下探到 0.18", captured.get("vad_parameters", {}).get("threshold") == 0.18)
check("no_speech_threshold 已传递", captured.get("no_speech_threshold") == 0.9)
check("log_prob_threshold 已传递", captured.get("log_prob_threshold") == -1.6)
check("日语提示词已传递", captured.get("initial_prompt") == asr.JAPANESE_PROMPT)
check("word_timestamps 仍开启", captured.get("word_timestamps") is True)
check("word 时间戳被保留", len(segs) == 1 and len(segs[0]["words"]) == 2)

# 关闭提示词/阈值时不应传入
captured.clear()
w.transcribe(np.zeros(16000 * 3, dtype=np.float32), vad=False, initial_prompt=None,
             no_speech_threshold=None, log_prob_threshold=None)
check("关掉提示词后不传 initial_prompt", "initial_prompt" not in captured)
check("关掉 VAD 后不传 vad_parameters", "vad_filter" not in captured)

# ---------- 4. 旧配置迁移 ----------
print("\n===== 4. 旧 settings.json 迁移 =====")
tmp_cfg = os.path.join(os.environ["TEMP"], "cfg_migrate_test.json")
old = dict(status="v1")
old.update({
    "asr_engine": "local", "whisper_model": "large-v3", "device": "auto",
    "language": "ja", "use_vad": True,
    "vad_threshold": 0.35,        # 旧默认 → 应升级成 0.25
    "normalize": "dynaudnorm",    # 旧默认
    "condition_on_previous_text": False,
    "beam_size": 8,               # 用户改过 → 必须保留
    "use_cache": False,           # 用户改过 → 必须保留
    "silence_gap_sec": 0.6,
    "llm_base_url": "http://192.168.1.9:1234/v1",   # 用户改过 → 保留
})
with open(tmp_cfg, "w", encoding="utf-8") as f:
    json.dump(old, f, ensure_ascii=False)
cfg = config.load_settings(tmp_cfg)
check("VAD 阈值升级为 0.25", cfg["vad_threshold"] == 0.25, str(cfg["vad_threshold"]))
check("新增 gain_db=6", cfg["gain_db"] == 6.0, str(cfg["gain_db"]))
check("新增 denoise=True", cfg["denoise"] is True)
check("新增 no_speech=0.75", cfg["no_speech_threshold"] == 0.75)
check("新增 logprob=-1.2", cfg["log_prob_threshold"] == -1.2)
check("尊重用户 beam_size=8", cfg["beam_size"] == 8, str(cfg["beam_size"]))
check("尊重用户 use_cache=False", cfg["use_cache"] is False)
check("尊重用户 API 地址", cfg["llm_base_url"] == "http://192.168.1.9:1234/v1")
check("写入 config_version=2", cfg.get("config_version") == 2)
with open(tmp_cfg, "r", encoding="utf-8") as f:
    saved = json.load(f)
check("迁移结果已落盘", saved.get("vad_threshold") == 0.25 and saved.get("gain_db") == 6.0)
# 二次加载不应再变
cfg2 = config.load_settings(tmp_cfg)
check("二次加载保持稳定", cfg2["vad_threshold"] == 0.25 and cfg2["beam_size"] == 8)

# 用户改过 vad_threshold 的情况
old2 = dict(old)
old2["vad_threshold"] = 0.5
with open(tmp_cfg, "w", encoding="utf-8") as f:
    json.dump(old2, f, ensure_ascii=False)
cfg3 = config.load_settings(tmp_cfg)
check("用户自定 VAD 阈值不被覆盖", cfg3["vad_threshold"] == 0.5, str(cfg3["vad_threshold"]))
os.remove(tmp_cfg)

print("\n" + ("ALL TESTS PASSED" if not FAIL else "FAILED: " + ", ".join(FAIL)))
sys.exit(0 if not FAIL else 1)
