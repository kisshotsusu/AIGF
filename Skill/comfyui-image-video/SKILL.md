---
name: comfyui-image-video
description: 使用本地 ComfyUI 生成图像、编辑图像和生成带音频的视频（Qwen-Image-2512 / Anima / Qwen-Image-Edit-2511 / MiniMax-H3），并返回可直接展示或打开的输出文件。
---

# ComfyUI 图像与视频生成

通过 `HomeAgent/home_modules/comfyui_client.py` 调用本地 ComfyUI（Comfy Desktop 独立版，
`http://127.0.0.1:8188`）。所有 ComfyUI 通信逻辑都封装在该模块中，HomeAgent 主程序只做薄委托；
本技能提供使用契约和命令行入口，供模型或人工直接调用。

## 何时使用

- 用户要求“画/生成/做一张图”，优先 `comfy_generate_image`（写实用 `qwen-image-2512`，动漫用 `anima`）。
- 用户要求“把这张图改成/加上/换成…”，使用 `comfy_edit_image`。
- 用户要求“生成/制作一个视频、短片”，使用 `comfy_generate_video`（文字生视频；提供首帧图片时自动图生视频）。
- 先不确定服务是否可用时调用 `comfy_status`；需要确认已装模型时调用 `comfy_list_models`。
- 只做“识图/理解图片”时不要用本技能，应使用 `analyze_image`。

## 模型预设

| 预设名 | 类型 | 模型 | 默认尺寸 | 说明 |
|---|---|---|---|---|
| `qwen-image-2512` | 图像 | qwen_image_2512_fp8 + Lightning LoRA | 1024×1024 | 写实/通用，默认 40 步，Lightning 4 步 |
| `anima` | 图像 | anima-base-v1.0 + anima-turbo-lora | 1024×1024 | 动漫风格，默认 30 步，turbo 8 步 |
| `qwen-image-edit-2511` | 编辑 | qwen_image_edit_2511_int8 + Lightning LoRA | 跟随输入图 | 图像编辑，默认 20 步，Lightning 4 步 |
| `minimax-h3` | 视频 | MiniMax_H3_FL2VA（nvfp4/int8）+ minimax H3 VAE | 1344×768 | 文生视频/图生视频，默认 20 步、24fps、49 帧，带音频 |

## 输出位置

- ComfyUI 原始输出：`D:\Comfy-Desktop\ComfyUI-Shared\output`。
- HomeAgent 交付副本（工具返回的 media.path）：`E:\Doc\AIAgent\outputs\comfyui`。
- 未运行的 ComfyUI 会在第一次调用时按桌面版相同参数自动启动（日志：
  `D:\Comfy-Desktop\ComfyUI-Installs\ComfyUI\logs\comfyui-homeagent-*.log`）。

## 使用要点

- 生成提示词建议给出主体、风格、构图、光影；视频提示词可加时间线分段和镜头运动。
- 图像生成会自动追加质量/风格正向提示词，并默认应用负面提示词（写实 Qwen 用中文画质/畸形/水印词，动漫 Anima 用英文质量标签）；模型或用户传入的负面提示词会与默认合并，无需每次手写。
- `width/height` 会自动吸附到模型支持的分辨率；视频帧数上限 1024，`steps` 越小越快但质量下降。
- 生成是长任务（图像约 0.5～3 分钟，视频数分钟），期间不要重复提交相同任务。
- 输出文件会通过 HomeAgent 聊天界面直接显示（图片缩略图可点击打开原图，视频卡片可点击“打开”）。
- 不要读取或输出 `.env` 密钥；ComfyUI 模型文件路径不包含任何密钥。

## 命令行入口

```powershell
Set-Location E:\Doc\AIAgent
& .\.venv\Scripts\python.exe Skill\comfyui-image-video\scripts\comfy_cli.py status
& .\.venv\Scripts\python.exe Skill\comfyui-image-video\scripts\comfy_cli.py models
& .\.venv\Scripts\python.exe Skill\comfyui-image-video\scripts\comfy_cli.py generate-image --prompt "一只橘猫在窗台上，午后阳光" --model anima --steps 8
& .\.venv\Scripts\python.exe Skill\comfyui-image-video\scripts\comfy_cli.py edit-image --image D:\图片\a.png --prompt "改成水彩画风格"
& .\.venv\Scripts\python.exe Skill\comfyui-image-video\scripts\comfy_cli.py generate-video --prompt "一只橘猫在花园里散步" --frames 24 --steps 8
```
