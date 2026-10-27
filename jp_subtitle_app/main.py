# -*- coding: utf-8 -*-
"""入口：日语字幕生成器。

运行方式（在项目根目录）：
    .venv\\Scripts\\python.exe -m jp_subtitle_app.main
或双击「启动字幕生成器.bat」
"""
import os
import sys


def _setup_env():
    """本地 LLM 不走代理；HuggingFace 模型下载默认走国内镜像。"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

    # ctranslate2（CUDA）依赖 pip 版 nvidia 运行时 dll（cublas/cublasLt/cudart/cudnn/nvrtc），
    # 把这些 bin 目录全部加入 PATH；缺任何一个都会报 "Library cublas64_12.dll is not found"。
    nvidia_dir = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    cuda_bins = []
    if os.path.isdir(nvidia_dir):
        for pkg in ("cublas", "cuda_runtime", "cudnn", "cuda_nvrtc", "cufft"):
            d = os.path.join(nvidia_dir, pkg, "bin")
            if os.path.isdir(d):
                cuda_bins.append(d)
    if cuda_bins:
        os.environ["PATH"] = os.pathsep.join(cuda_bins) + os.pathsep + os.environ.get("PATH", "")


def main():
    _setup_env()
    if __package__ in (None, ""):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from jp_subtitle_app.gui import main as gui_main
    else:
        from .gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
