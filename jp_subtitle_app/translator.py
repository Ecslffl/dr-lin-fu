# -*- coding: utf-8 -*-
"""大模型翻译：OpenAI 兼容 Chat Completions（LM Studio / Ollama / DeepSeek / 通义 等）。"""
import re
import time

import requests

SYSTEM_PROMPT = (
    "你是一位专业的影视字幕翻译。把用户提供的日语字幕逐条翻译成简体中文。\n"
    "规则：\n"
    "1) 只输出译文，不要任何解释、注释、序号前缀或额外文字；\n"
    "2) 口语化、贴合语境、简洁自然，保留语气词；\n"
    "3) 人名、地名、专有名词采用通行译法；\n"
    "4) 同一片段内术语保持一致；\n"
    "5) 输入若不是日语（如拟声词、纯符号、乱码），原样返回。"
)


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, base_url, model="", api_key="", temperature=0.3, timeout=180):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    # ---------- 连通性与模型探测 ----------
    def list_models(self):
        try:
            r = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=10)
            r.raise_for_status()
            return [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        except Exception:
            return []

    def pick_chat_model(self, models):
        """从模型列表挑一个适合翻译的 chat 模型。"""
        skip = ("embed", "whisper", "vl", "vision", "caption", "tts", "asr", "rerank")
        prefer = ("qwen", "gpt", "llama", "chat", "instruct", "deepseek", "glm", "gemma")
        cands = [m for m in models if not any(k in m.lower() for k in skip)]
        if not cands:
            cands = list(models)
        for c in cands:
            if any(k in c.lower() for k in prefer):
                return c
        return cands[0] if cands else None

    def check_online(self):
        """返回 (在线 bool, 推荐模型 or None, 错误信息 or None)。"""
        try:
            models = self.list_models()
            if not models:
                return False, None, "服务在线但没有可用模型，请先在 LM Studio 中加载模型"
            return True, self.pick_chat_model(models), None
        except Exception as e:
            return False, None, f"无法连接 {self.base_url}：{e}"

    # ---------- 翻译 ----------
    def chat(self, messages, max_tokens=4096):
        if not self.model:
            raise LLMError("未设置翻译模型 ID，请先检测/填写模型")
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature, "stream": False}
        if max_tokens and max_tokens > 0:
            payload["max_tokens"] = max_tokens
        try:
            r = requests.post(f"{self.base_url}/chat/completions",
                              headers=self.headers, json=payload,
                              timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise LLMError(f"请求失败: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"响应格式异常: {data}") from e

    def translate_batch(self, texts):
        """批量翻译，返回与输入等长、顺序一致的译文列表（单条失败为空串）。"""
        numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(texts, 1))
        user_msg = (
            "请将以下日语字幕逐条翻译成简体中文，保持行序，一行对应一条：\n\n"
            + numbered
        )
        content = self.chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ])
        return parse_translations(content, len(texts))


def parse_translations(content, expected):
    """解析模型输出为译文列表，尽力恢复序号对应关系，行数不符时对齐/补齐。"""
    if not content:
        return [""] * expected
    content = content.strip()
    content = re.sub(r"^```[a-zA-Z]*\s*", "", content).strip()
    content = re.sub(r"\s*```$", "", content).strip()

    numbered = {}
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d{1,4})\s*[:：.、)）]\s*(.+)$", line)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= expected and idx not in numbered:
                numbered[idx] = m.group(2).strip()

    if len(numbered) == expected:
        return [numbered[i] for i in range(1, expected + 1)]

    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if lines and len(lines) == expected:
        return lines
    if numbered:
        out = [""] * expected
        for i, t in numbered.items():
            out[i - 1] = t
        return out
    if len(lines) >= expected:
        return lines[:expected]
    return lines + [""] * (expected - len(lines))


def translate_with_retry(client, texts, retries=2):
    """带重试的批量翻译：失败后间隔 1.5s 重试，最终失败返回空译文并记录失败行。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return client.translate_batch(texts), None
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5)
    return [""] * len(texts), last_err
