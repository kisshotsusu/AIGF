---
name: comfyui-image-video
description: 使用本地 ComfyUI 生成图像、编辑图像和生成带音频的视频（Qwen-Image-2512 / Anima / Qwen-Image-Edit-2511 / MiniMax-H3），并返回可直接展示或打开的输出文件。
---

# ComfyUI 图像与视频生成

通过 `HomeAgent/home_modules/comfyui_client.py` 调用本地 ComfyUI（Comfy Desktop 独立版，
`http://127.0.0.1:8188`）。所有 ComfyUI 通信逻辑都封装在该模块中，HomeAgent 主程序只做薄委托；
本技能提供使用契约和命令行入口，供模型或人工直接调用。

## 何时使用

**先判断“有没有参考图”，再选工具，不要凭“画/改”两个字猜。**

| 场景 | 工具 | 关键参数 |
|---|---|---|
| 完全没有参考图，要求全新场景/全新角色 | `comfy_generate_image` | `prompt` 写主体、风格、构图、光影 |
| 用户粘贴了图片、有角色设定图（`primary`/角色三视图/正面照片）、或要沿用上一轮生成图，要求改姿势/背景/风格/表情/服装/局部 | `comfy_edit_image` | `image` 填参考图绝对路径（用户附件路径见系统提示【本次消息附带的图片】；角色图可用 `primary`/文件名；上一轮结果用输出 `media.path`） |
| 多张参考图 | `comfy_edit_image` 分步串联 | 先用设定图改姿势，第二次的 `image` 用第一次输出的 `media.path` 改背景 |
| 生成视频/动画 | `comfy_generate_video` | `first_frame` 可填首帧图路径 |
| 只理解图片内容 | `analyze_image` | 编辑前可先分析参考图，把角色细节、构图、风格说清楚再改 |

先不确定服务是否可用时调用 `comfy_status`；需要确认已装模型时调用 `comfy_list_models`。

## 参考图与路由规范（防止“编辑/生成”混乱）

- 有参考图时**一律**走 `comfy_edit_image`，禁止 `comfy_generate_image` 凭空重画——后者会丢失角色细节和原图构图。
- “这张图/刚才那张图/我的设定图/上一张”等指代，必须解析成真实路径：用户附件用系统提示中列出的绝对路径；角色图用 `list_character_images` 返回的路径或别名；上一轮编辑用返回结果的 `media.path`。
- 用户给的风格词、构图词、负面词必须**原样传入** `prompt` / `negative_prompt`，不要丢弃；模块会自动合并默认质量正向词和默认负面词。
- 参考图不清楚时，先 `analyze_image`（或 `list_character_images`）再编辑，不要盲目调用。
- 编辑 `denoise` 建议：`0.6～0.7` 保留细节微调；`0.7～0.85` 改姿势/背景；`0.9～1.0` 大改。
- 本地视觉识别（`view_image`、本地 OCR、截图分析等）**只用于桌面点击操作**，禁止用于图像生成验收；验收一律走 `analyze_image`（MiMo 视觉 API）。

## 防鬼图规范（2026-08-11 实战验证）

每次图像编辑/生成按五步走：**先分析参考图 → 按模板写提示词 → 选稳定参数 → 生成 → 用 analyze_image 验收**。
不要省略前两步直接丢一句话给 ComfyUI。

### 1. 分析参考图（必做）

- 有参考图时，先 `analyze_image` 问清：角色外貌、服装款式与配色、姿势、表情、构图、背景、风格，以及原图缺陷
  （模糊、锯齿、多余杂线、肢体畸形、水印文字等）。
- 把分析结果里“要保持什么、要修什么”写进编辑指令，不要只写一句“优化一下”。

### 2. 编辑提示词模板（原样套用，替换方括号内容）

**模板 A：美术优化 / 高清清线稿（本次主形象优化的成功案例）**

```text
以这张立绘为底图做高清美术优化，必须保持角色身份和构图完全一致：
[逐条列出角色锚点，如：银灰色及腰长发、两侧各有细长红色挑染、猩红杏眼、黑白不对称挂脖式露腰短上衣、
黑色系带与金色小坠饰、上白下黑渐变百褶短裙、白色浅口鞋带红色装饰]；
保持[姿势，如：全身 T 字展示姿势]、[表情]、[背景，如：浅灰色纯色背景]、[风格，如：日系赛璐璐平涂风格]。
重绘为高清精致版本：线条干净流畅连续无断裂，消除锯齿、像素化、模糊和多余杂线；
修正手臂与腿部轮廓、比例和线条粗细；[头发分束、服装拼接、百褶、鞋饰]细节清晰锐利；
整体画面干净、精致、画质明显提升。
```

**模板 B：改姿势 / 改背景（分步串联）**

```text
以这张[角色设定图/上一步结果图]为底图，保持角色身份和构图完全一致：
[逐条列出角色锚点，必须从 workspace/CHARACTER.md 复制，至少保留五个：发色、瞳色、上衣、裙装、鞋型等]。
把姿势改成[具体动作，如：侧坐回眸]，表情[保持平静/改成微笑]；
背景改成[具体场景与光线]，人物和服装细节不得改变。
保持线条干净流畅，比例正确，禁止新增发饰、兽耳、翅膀、尾巴、首饰、文字、Logo、水印。
```

模板要点：

- 第一句必须写明“以这张图为底图，保持角色身份和构图完全一致”，让编辑模型知道这是修改不是重画。
- 角色锚点从 `workspace/CHARACTER.md` 复制，而不是凭印象写；锚点之间用顿号或逗号分隔。
- 明确写“要保持什么”和“要修什么”两部分；只写目标不写约束是鬼图常见原因。
- 用户给的风格词、构图词、负面词必须原样并入，不要丢弃。

### 3. 负面词库（与模块默认负面词自动合并）

```text
低分辨率，低画质，线条断裂，锯齿，像素化，模糊，噪点，多余线条，杂线，细红线，乱线，
肢体畸形，手臂扭曲，腿部畸形，五官崩坏，脸部变形，眼睛错位，嘴歪，多余手指，手指数量错误，
身体结构错误，透视错误，多手多脚，重复主体，多余人物，双人，无关物体，文字模糊，构图混乱，画面过饱和，过度光滑，AI感，
水印，签名，文字，Logo，边缘破损，低清
```

负面词必须由 AI **智能填充**：用户没给时，根据任务类型（写实/动漫/改图）从词库自动挑选并补全，
常见场景至少覆盖：画质、脸部/手部/肢体结构、重复主体/多余人物、杂线与清晰度、水印文字。
禁止留空或只依赖模块默认；用户给的负面词原样保留并排在前面。

### 4. 稳定参数（本机 16GB 显存实测）

- `qwen-image-edit-2511` 优先 `use_lora=true` + 4 步（Lightning LoRA 原生步数，约 13～30 秒，稳定不崩）。
- `denoise`：改姿势/背景 `0.7～0.85`；美术优化/清线稿 `0.65～0.75`；只想保留原图 `0.6～0.7`。
- **不要在 16GB 显存上对编辑模型跑无 LoRA 20 步**：实测会在采样阶段被中断/OOM（20 步无 LoRA 仅留给 `qwen-image-2512` 文生图）。
- 不要关闭模块自动合并的 `positive_suffix` / `negative_default`；那是最后一道防鬼图保险。
- 视频仍保持 `frames ≤ 24`、`steps ≤ 10`。

### 5. 生成后验收（必做）

- 验收**必须**调用 `analyze_image`（MiMo 视觉 API），禁止用本地视觉/截图工具看生成结果；本地视觉只用于桌面点击。
- 验收提示词模板（原样套用）：

```text
请以挑剔的眼光验收这张图：1) 角色身份锚点是否齐全且未漂移；2) 姿势/构图/背景是否符合要求；
3) 画质是否达标（线条干净、无锯齿/像素化/模糊/杂线）；4) 有无崩坏——脸部变形、五官错位、
手部错误、多手多脚、肢体畸形、重复主体、多余人物、无关物体；5) 有无文字、水印、Logo。
结论：通过/不通过，并列出最需要修复的 3 个问题。中文回答。
```

- 结论为“不通过”或明显丑/崩坏时：**只允许修改提示词或参数重试一次**（补充对应负面词），
  同一套参数连续重试是鬼图生产循环，禁止。
- 验收通过后再交付给用户，不要用“应该可以”代替实际检查，禁止把未验收图片直接交付。

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

## 视频生成规范（防止“视频失败→反复画图”死循环）

- 视频/动画请求**只能**调用 `comfy_generate_video`；`comfy_generate_image` / `comfy_edit_image` 只生成静态图，不能冒充视频。
- 模型版本自动探测：`MiniMax_H3_FL2VA_pruned_nvfp4.safetensors` 缺失时模块自动回退 `minimax_h3_fl2va_pruned_int8_convrot.safetensors`，无需模型指定。
- 本机显存 16GB，视频模型约 19.5GB，必须保守设参：`frames ≤ 24`、`steps ≤ 10`，否则极慢甚至 OOM。
- 视频生成失败时：如实报告错误原因并停止；不得反复用相同参数重试，更不得改用画图工具生成静态图后谎称完成。

## 命令行入口

```powershell
Set-Location E:\Doc\AIAgent
& .\.venv\Scripts\python.exe Skill\comfyui-image-video\scripts\comfy_cli.py status
& .\.venv\Scripts\python.exe Skill\comfyui-image-video\scripts\comfy_cli.py models
& .\.venv\Scripts\python.exe Skill\comfyui-image-video\scripts\comfy_cli.py generate-image --prompt "一只橘猫在窗台上，午后阳光" --model anima --steps 8
& .\.venv\Scripts\python.exe Skill\comfyui-image-video\scripts\comfy_cli.py edit-image --image D:\图片\a.png --prompt "改成水彩画风格"
& .\.venv\Scripts\python.exe Skill\comfyui-image-video\scripts\comfy_cli.py generate-video --prompt "一只橘猫在花园里散步" --frames 24 --steps 8
```
