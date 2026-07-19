# Sound — SenseVoiceSmall 语音识别

基于阿里达摩院开源 **SenseVoiceSmall** (FunASR) 的本地语音识别模块，运行在 RTX 5070 Ti (CUDA)。

## 能力
- 音频文件转写：wav / mp3 / flac / m4a / ogg 等
- 麦克风实时录音转写
- 多语种：中文(zh) / 英文(en) / 日文(ja) / 韩文(ko) / 粤语(yue) / auto
- 输出带标点、数字规整(ITN)，并附情绪/事件标签（如 `<|HAPPY|>` `<|NEUTRAL|>` `<|Speech|>`）

## 环境
- 共享项目 venv：`..\.venv`（Python 3.12 + torch **cu128**；5070 Ti 是 Blackwell sm_120，cu124 不兼容）
- 依赖：funasr / modelscope / sounddevice / librosa / soundfile / mcp
- 模型：`Sound/models/SenseVoiceSmall`（model.pt ~936MB + config.yaml/am.mvn/tokens.json/bpe 词表，共 20 文件）

> 本目录**不再单独持有 venv**，统一使用父级 `.venv`（详见父目录 README「共享环境」一节）。

## 安装 / 下载（多数已就绪，仅作重装参考）
```bat
:: 1. 依赖(在父 .venv 中)
.venv\Scripts\python.exe -m pip install funasr modelscope sounddevice librosa soundfile mcp

:: 2. 下载模型权重(若未下载)
.venv\Scripts\python.exe Sound\download_model.py
```

## 用法（作为 Python 模块）
```python
import asr
asr.transcribe_file(r"E:\音频.wav", language="auto")     # 文件
asr.record_and_transcribe(duration=5.0, language="zh")   # 麦克风
```

## MCP 工具（可被 WorkBuddy / HomeAgent 调用）
在 `~/.workbuddy/mcp.json` 注册 `sound-asr`（stdio，命令指向父 .venv）：
```json
"sound-asr": {
  "command": ".venv\\Scripts\\python.exe",
  "args": ["Sound\\mcp_server.py"],
  "env": { "SENSEVOICE_MODEL": "Sound\\models\\SenseVoiceSmall" }
}
```
工具：`transcribe_file(path, language)`、`record_and_transcribe(duration, language)`。

## 与 GUI Web Agent 联动（语音控制浏览器）
`Sound` 识别出的文本可作为 `Vision`（vision-gui MCP）的指令，实现"说一句话就操控网页/桌面"。
HomeAgent 已内置此链路：语音识别 → 模型决策 → 命中关键词时调用 vision-gui 操作电脑。

## 已知坑（已修复，记录备查）
1. **非 ASCII 路径**：若工程根目录含非 ASCII 字符，sentencepiece 的 C++ 后端可能读不到 bpe 词表。
   本机 NTFS 关闭了 8.3 短名生成，`GetShortPathNameW` 返回的短名实际不存在。
   **解决**：`asr.load_model()` 检测到中文路径时，自动把整目录复制到系统 TEMP(纯 ASCII) 下的
   `sensevoice_model` 并复用，规避该问题。
2. **torchcodec 缺失**：早期用 `torchaudio.load` 默认走未安装的 torchcodec 后端会报错。
   **解决**：`to_wav16k` 改用 `librosa.load`（原生支持中文路径 + mp3，重采样 16k）+ `soundfile`
   写 ASCII 临时 wav 交给 funasr，彻底绕开非 ASCII 与 torchcodec 两个坑。
