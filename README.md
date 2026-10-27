# 日语字幕生成器（SRT）

一个带图形界面的日语影片字幕工具：**本地 Whisper 大模型识别日语语音 → 大模型翻译成简体中文 → 导出 SRT 字幕**（支持中日双语，中文在上、日文在下）。

本工具是在你提供的 `生成日语字幕.bat` 基础上的 GUI 升级版：批量加载影片、可视化进度与日志、字幕片段预览与逐条编辑、多格式导出。

## 功能

- **影片加载**：批量添加 mp4 / mkv / avi / mov / wmv / flv / webm 及常见音频格式，多文件队列依次处理
- **日语识别（ASR）**：faster-whisper 本地大模型（tiny→large-v3 可选，自动 CUDA/CPU），可选 VAD 静音过滤、音频归一化；也支持 OpenAI 兼容的云端转写 API
- **智能翻译**：调用大模型逐批翻译（LM Studio / Ollama / DeepSeek / 通义 等任意 OpenAI 兼容服务），自动检测在线模型，翻译失败自动重试
- **字幕导出**：SRT 格式，支持 中日双语（中文上/日文下，与视频同名可被 PotPlayer 自动加载）、仅中文、仅日文、日文+中文双文件
- **片段编辑**：识别/翻译结果表格预览，双击任意行可修改日文与中文后重新导出
- **断点续做**：识别结果自动缓存为 `<影片>.ja.json`，再次处理时跳过识别直接翻译

## 目录结构

```
D:\AI_Work\SRT\
├── 启动字幕生成器.bat     ← 双击运行 APP（自动选择 Python 环境）
├── 安装依赖.bat           ← 首次使用先运行：安装/修复依赖
├── requirements.txt
├── README.md
├── 生成日语字幕.bat        ← 你原有的拖拽批处理脚本（未改动）
├── python\               ← 项目内置完整 Python 3.13.16（python.org 官方版，
│                           自带 tkinter/Tcl 8.6.15；本机原先缺少可用的 GUI Python，
│                           故随项目内置一份，可整体删除不影响系统）
└── jp_subtitle_app\       ← 应用源码
    ├── main.py           入口
    ├── gui.py            tkinter 界面
    ├── config.py         设置读写（settings.json）
    ├── audio.py          ffmpeg 音频解码/时长探测
    ├── asr.py            本地 Whisper / 云端 API 转写
    ├── translator.py     OpenAI 兼容大模型翻译
    └── srt_builder.py    SRT 生成与导出
```

## 快速开始

1. **安装依赖**（一次性，约几分钟）：双击 `安装依赖.bat`
   - 自动检测 `python\` 内置 Python（或本机其他带 tkinter 的 Python）并安装 faster-whisper、numpy、requests、imageio-ffmpeg
   - 首次识别时会自动下载 Whisper 模型（large-v3 约 3GB，走 hf-mirror 国内镜像），之后走本地缓存
2. **启动**：双击 `启动字幕生成器.bat`
3. **准备翻译服务**（二选一）：
   - **LM Studio（推荐，本地免费）**：启动 LM Studio → 加载一个中文翻译模型（如 Qwen2.5-7B-Instruct）→ 打开 Developer 页签 → Start Server（默认 `http://127.0.0.1:1234/v1`）
   - **云端 OpenAI 兼容服务**：在「翻译设置」页填 API 地址、API Key、模型名（如 DeepSeek: `https://api.deepseek.com/v1` + `deepseek-chat`）
4. **操作**：添加影片 → （可选）点「检测在线模型」自动识别翻译模型 → 点「开始生成」→ 完成后在影片同目录生成 `.srt`

## 界面说明

| 页签 | 设置项 | 说明 |
|---|---|---|
| 识别设置 | 识别引擎 | `local` 本地 Whisper（默认）；`api` 云端转写接口 |
| | Whisper 模型 | tiny/base/small/medium/large-v3；API 模式填 API 模型名（如 whisper-1） |
| | 计算设备 | auto / cuda / cpu，自动识别 N 卡 |
| | VAD / 归一化 | 识别不出语音时：阈值调低到 0.2–0.3，或归一化换 loudnorm / none |
| 翻译设置 | API 地址 | LM Studio/Ollama/云端 /v1 地址 |
| | 翻译模型 | 留空点「检测在线模型」自动填充 |
| | 批量翻译 | 更快，默认开启；每批最大字符/行数可调 |
| 导出设置 | 导出格式 | bilingual 双语（默认）/ zh 中文 / ja 日文 / separate 双文件 |
| | 输出目录 | 留空 = 与影片同目录 |
| | 文件名后缀 | 留空时双语字幕与影片同名 `.srt`，PotPlayer 自动加载 |

## 常见问题

- **识别出 0 段 / 大量漏识别**：VAD 阈值降到 0.2–0.3；「音频归一化」改 `loudnorm` 或 `none` 试试
- **Whisper 反复输出同一句（幻觉循环）**：勾掉「使用上文提示识别」（默认已关闭）
- **CUDA 报错 / 黑屏卡死**：设备选 `cpu` 跑（速度较慢但稳定）；N 卡驱动建议更新
- **提示无法连接翻译服务**：确认 LM Studio 已启动 Server；或改用云端服务并填 API Key
- **模型下载慢**：脚本默认走 `https://hf-mirror.com` 镜像；已设置 `HF_ENDPOINT` 则尊重你的配置

## 说明与限制

- 音频解码走 ffmpeg（imageio-ffmpeg 内置），无需单独安装 ffmpeg
- 字幕时间轴来自 Whisper 识别结果，编辑片段只改文本不影响时间轴
- 首次使用本地大模型需联网下载模型；识别与翻译都在本机/你配置的服务完成
