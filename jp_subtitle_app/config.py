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
    "vad_threshold": 0.35,          # VAD 语音阈值
    "normalize": "dynaudnorm",      # dynaudnorm/loudnorm/none 音频归一化
    "condition_on_previous_text": False,  # 上下文累积识别
    "beam_size": 5,
    "use_cache": True,              # 优先使用 <视频>.ja.json 识别缓存
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
    # ---- 导出 ----
    "export_mode": "bilingual",     # bilingual=中文上/日文下, zh, ja, separate
    "export_dir": "",               # 空 = 与视频同目录
    "output_suffix": "",            # 追加到文件名，如 .双语
    "auto_export": True,            # 处理完成后自动导出 SRT
    # ---- 界面 ----
    "last_dir": "",
}


def load_settings(path=SETTINGS_FILE):
    cfg = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in cfg:
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def save_settings(cfg, path=SETTINGS_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[!] 保存设置失败: {e}")
        return False
