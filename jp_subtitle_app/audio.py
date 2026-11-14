# -*- coding: utf-8 -*-
"""音频处理：查找 ffmpeg、探测时长、解码为 16kHz 单声道 PCM。"""
import os
import re
import shutil
import subprocess

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None


def find_ffmpeg():
    """返回可用的 ffmpeg 可执行文件路径，找不到返回 None。"""
    for env_key in ("FFMPEG_BINARY", "IMAGEIO_FFMPEG_EXE"):
        p = os.environ.get(env_key)
        if p and os.path.exists(p):
            return p
    p = shutil.which("ffmpeg")
    if p:
        return p
    if imageio_ffmpeg is not None:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


def probe_duration(path):
    """用 ffmpeg 探测媒体时长（秒，float）；失败返回 None。"""
    exe = find_ffmpeg()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-i", path],
            capture_output=True, timeout=60,
        )
        err = (proc.stderr or b"").decode("utf-8", "ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return None


def _afilter(normalize, gain_db=0.0, denoise=False):
    """组装 ffmpeg 音频滤镜链：增益 → 归一化 → 降噪(→限幅)。

    实测要点（用 -50dBFS 级轻声素材逐条对照得出）：
    - dynaudnorm 的 gausssize(framelen) 取默认 31/500 时对轻声完全不起作用，
      必须用 f=200:g=15 才有效，且 maxgain(m) 默认 10 只够 +20dB，
      轻声对白需要放宽到 30 才抬得起来；
    - afftdn 若放在归一化之前，会把低于噪声门限的轻声整体当噪声压掉
      （实测 -52dBFS 素材被削到 -64dBFS），所以降噪必须放在归一化之后；
    - 数字静音不会被放大（峰值仍为 0），微弱底噪只抬到约 -41dBFS，
      不会凭空制造出“像人声”的大电平噪声。
    """
    parts = []
    try:
        gain = float(gain_db or 0.0)
    except (TypeError, ValueError):
        gain = 0.0
    if gain:
        parts.append(f"volume={gain:g}dB")
    if normalize == "dynaudnorm":
        parts.append("dynaudnorm=f=200:g=15:p=0.95:m=30")
    elif normalize == "loudnorm":
        parts.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    elif gain:
        # 只加增益不做归一化时，补一级限幅，防止大声部分削顶失真
        parts.append("alimiter=limit=0.95")
    if denoise:
        parts.append("afftdn=nf=-25:tn=1")
    return ",".join(parts) if parts else "anull"


def decode_audio_pcm(path, sample_rate=16000, normalize="dynaudnorm",
                     gain_db=0.0, denoise=False):
    """把任意音视频解码为 16kHz 单声道 float32 PCM，返回 numpy 数组。
    绕开 PyAV 在部分 Python 版本上的兼容问题，直接走 ffmpeg stdout。
    """
    exe = find_ffmpeg()
    if not exe:
        raise RuntimeError("未找到 ffmpeg，请安装 ffmpeg 或 imageio-ffmpeg 后重试")
    cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-i", path, "-vn", "-af", _afilter(normalize, gain_db, denoise),
           "-ar", str(sample_rate), "-ac", "1",
           "-f", "f32le", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode("utf-8", "ignore")[:500]
        raise RuntimeError(f"ffmpeg 解码失败: {detail}") from e
    import numpy as np
    audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    if audio.size == 0:
        raise RuntimeError("解码后音频为空，请确认文件为有效的音视频文件")
    return audio


def decode_to_wav(path, wav_path, sample_rate=16000, normalize="dynaudnorm",
                  gain_db=0.0, denoise=False):
    """解码为临时 WAV（API 转写用），返回 wav 路径。"""
    exe = find_ffmpeg()
    if not exe:
        raise RuntimeError("未找到 ffmpeg，请安装 ffmpeg 或 imageio-ffmpeg 后重试")
    cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-i", path, "-vn", "-af", _afilter(normalize, gain_db, denoise),
           "-ar", str(sample_rate), "-ac", "1",
           "-c:a", "pcm_s16le", "-y", wav_path]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "ignore")[:500]
        raise RuntimeError(f"ffmpeg 转码失败: {detail}")
    return wav_path
