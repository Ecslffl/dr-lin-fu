# -*- coding: utf-8 -*-
"""配置读写：设置保存在应用目录下的 settings.json，可移植。"""
import json
import os

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULTS = {
    # ---- 识别 ----
    "asr_engine": "local",          # local=本地 Whisper, api=OpenAI 兼容 API 转写
    "whisper_model": "large-v3",    # tiny/base/small/medium/large-v3/distil-large-v3
    "device": "auto",               # auto/cuda/cpu
    "language": "ja",               # 源语言代码
    "use_vad": True,                # 启用 VAD 静音过滤
    "vad_threshold": 0.25,          # VAD 语音阈值（调低=更容易抓到轻声对话）
    "vad_min_silence_ms": 400,      # VAD 判定一句结束所需的最短静音
    "vad_speech_pad_ms": 300,       # 语音片段前后保留的余量，防止切掉轻声字头
    "no_speech_threshold": 0.75,    # 判定“没人说话”的概率阈值（调高=更难丢弃台词）
    "log_prob_threshold": -1.2,     # 置信度低于该值才允许丢弃（调低=保留含糊台词）
    "normalize": "dynaudnorm",      # dynaudnorm/loudnorm/none 音频归一化
    "gain_db": 6.0,                 # 解码时的音量增益(dB)，提升轻声台词
    "denoise": True,                # 轻度 FFT 降噪(afftdn)，改善吐字不清
    "japanese_prompt": True,        # 追加日语提示词，改善书写与含糊发音
    "condition_on_previous_text": False,  # 上下文累积识别
    "beam_size": 5,
    "use_cache": True,              # 优先使用 <视频>.ja.json 识别缓存
    "split_at_silence": True,       # 词间静音超阈值时断句，静音期字幕留白
    "silence_gap_sec": 0.6,        # 静音断句阈值（秒）
    # ---- 翻译 ----
    "llm_base_url": "http://127.0.0.1:1234/v1",   # LM Studio / Ollama / 任意 OpenAI 兼容
    "llm_model": "",                # 空 = 启动时自动检测
    "llm_api_key": "",
    "llm_timeout": 180,
    "temperature": 0.3,
    "batch_translate": True,        # 批量翻译（更快）
    "max_batch_chars": 1500,        # 每批最大字符数
    "max_batch_lines": 60,          # 每批最大行数
    "skip_translate": False,        # 只识别不翻译
    # ---- 字幕审查（音轨听写做参照，大模型校对已有字幕） ----
    "review_use_cache_ref": False,   # True=用已有 .ja.json 做参照，跳过重新识别
    "review_fix_text": True,         # 修正日文错字与中文译文
    "review_fix_timing": True,       # 按音轨真实语音边界收紧时间轴
    "review_add_missing": True,      # 补齐音轨说了但字幕没有的句子
    "review_split_sentences": True,  # 一条字幕跨多句且句间有静音时按句切开
    "review_min_overlap": 0.35,      # 字幕与语音段重叠比例阈值（调低=更容易算同一条）
    "review_time_pad_sec": 0.25,     # 收紧时间轴后保留的余量(秒)
    "review_split_gap_sec": 0.8,     # 句间静音达到该值才切句(秒)
    "review_min_missing_sec": 0.35,  # 补句的最短语音时长(秒)
    "review_batch_lines": 20,        # 每次送模型的条数
    "review_batch_chars": 1600,      # 每次送模型的最大字符数
    "review_out_suffix": ".fixed",   # 输出文件名后缀，原字幕不动
    # ---- 导出 ----
    "export_mode": "bilingual",     # bilingual=中文上/日文下, zh, ja, separate
    "export_dir": "",               # 空 = 与视频同目录
    "output_suffix": "",            # 追加到文件名，如 .双语
    "auto_export": True,            # 处理完成后自动导出 SRT
    # ---- 界面 ----
    "last_dir": "",
    # ---- 元信息 ----
    "config_version": 2,            # 用于把旧配置迁移到新的推荐默认值
}

# 旧版(v1)的默认值：迁移时据此判断“用户是否动过这一项”。
# 只有仍等于旧默认值的键才会被升级为新的推荐值，用户手动调过的设置一律保留。
OLD_DEFAULTS_V1 = {
    "vad_threshold": 0.35,
    "normalize": "dynaudnorm",
    "condition_on_previous_text": False,
    "beam_size": 5,
    "use_vad": True,
    "silence_gap_sec": 0.6,
}

# v1 → v2 新增/升级的识别参数（用户未改动时套用）
MIGRATE_V1_TO_V2 = {
    "vad_threshold": 0.25,
    "vad_min_silence_ms": 400,
    "vad_speech_pad_ms": 300,
    "no_speech_threshold": 0.75,
    "log_prob_threshold": -1.2,
    "gain_db": 6.0,
    "denoise": True,
    "japanese_prompt": True,
}


def _is_untouched(cur, old):
    """判断某项是否仍停留在旧默认值。

    GUI 滑块会把 0.35 存成 0.34905660377358494 这类浮点残留，
    所以数值型必须按容差比较，不能用 ==。
    """
    if isinstance(old, bool) or isinstance(cur, bool):
        return cur == old
    if isinstance(old, (int, float)) and isinstance(cur, (int, float)):
        return abs(float(cur) - float(old)) <= 0.01
    return cur == old


def load_settings(path=SETTINGS_FILE):
    cfg = dict(DEFAULTS)
    dirty = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in cfg:
                    cfg[k] = v
            # ---- 旧配置迁移：只升级用户从未改动过的识别参数 ----
            # 不按版本号硬门禁：部分旧配置已被写成 version=2 但值仍是旧默认，
            # 只有“值确实变了”才需要落盘，因此每次加载做一次幂等检查。
            for key, new_val in MIGRATE_V1_TO_V2.items():
                cur = data.get(key, None)
                old = OLD_DEFAULTS_V1.get(key, None)
                if cur is None:
                    if cfg.get(key) != new_val:
                        cfg[key] = new_val
                        dirty = True
                elif old is not None and _is_untouched(cur, old) and cfg.get(key) != new_val:
                    cfg[key] = new_val
                    dirty = True
            if cfg.get("config_version") != 2:
                cfg["config_version"] = 2
                dirty = True
    except Exception:
        pass
    if dirty:
        save_settings(cfg, path)
    return cfg


def save_settings(cfg, path=SETTINGS_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[!] 保存设置失败: {e}")
        return False


# ---- 识别参数预设：一键切换常用场景 ----
PRESET_DEFAULT = "推荐（默认）"

PRESETS = {
    PRESET_DEFAULT: {
        "use_vad": True,
        "vad_threshold": 0.25,
        "vad_min_silence_ms": 400,
        "vad_speech_pad_ms": 300,
        "no_speech_threshold": 0.75,
        "log_prob_threshold": -1.2,
        "gain_db": 6.0,
        "denoise": True,
        "normalize": "dynaudnorm",
        "japanese_prompt": True,
        "condition_on_previous_text": False,
        "beam_size": 5,
        "silence_gap_sec": 0.6,
    },
    "轻声 / 含糊对话": {
        "use_vad": True,
        "vad_threshold": 0.18,
        "vad_min_silence_ms": 300,
        "vad_speech_pad_ms": 450,
        "no_speech_threshold": 0.9,
        "log_prob_threshold": -1.6,
        "gain_db": 10.0,
        "denoise": True,
        "normalize": "loudnorm",
        "japanese_prompt": True,
        "condition_on_previous_text": False,
        "beam_size": 8,
        "silence_gap_sec": 0.5,
    },
    "嘈杂 / 音乐音效多": {
        "use_vad": True,
        "vad_threshold": 0.33,
        "vad_min_silence_ms": 600,
        "vad_speech_pad_ms": 300,
        "no_speech_threshold": 0.65,
        "log_prob_threshold": -1.0,
        "gain_db": 4.0,
        "denoise": False,
        "normalize": "dynaudnorm",
        "japanese_prompt": True,
        "condition_on_previous_text": False,
        "beam_size": 5,
        "silence_gap_sec": 0.7,
    },
    "原音保真 / 追求速度": {
        "use_vad": True,
        "vad_threshold": 0.4,
        "vad_min_silence_ms": 500,
        "vad_speech_pad_ms": 250,
        "no_speech_threshold": 0.6,
        "log_prob_threshold": -1.0,
        "gain_db": 0.0,
        "denoise": False,
        "normalize": "none",
        "japanese_prompt": False,
        "condition_on_previous_text": False,
        "beam_size": 5,
        "silence_gap_sec": 0.6,
    },
}
