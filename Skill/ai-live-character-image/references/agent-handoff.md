# 无 VPN 环境与其他 Agent 调用

## 目录约定

- 技能：`E:\Doc\AI直播\Skill\ai-live-character-image`
- 主项目：`E:\Doc\AI直播`
- 角色设定：`E:\Doc\AI直播\workspace\CHARACTER.md`
- 形象库：`E:\Doc\AI直播\workspace\character_images`
- API 配置：`E:\Doc\AI直播\config.yaml` 的 `image_generation`
- 密钥：`E:\Doc\AI直播\.env` 的 `IMAGE_API_KEY`

如主项目位于其他目录，设置环境变量 `AI_LIVE_ROOT` 后再运行脚本。

## 首次准备

在 PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File "E:\Doc\AI直播\Skill\ai-live-character-image\scripts\setup-cn.ps1"
```

该脚本使用清华 PyPI 镜像，不依赖访问境外 PyPI。模型 API 本身必须填写当前网络可以访问的国内或内网地址。

将 `assets/config.example.yaml` 中的 `image_generation` 合并到主项目 `config.yaml`，将 `assets/.env.example` 中的变量写入主项目 `.env`。

## Agent 调用

另一 Agent 应先完整读取 `SKILL.md`，然后执行：

```powershell
python "E:\Doc\AI直播\Skill\ai-live-character-image\scripts\character_image_api.py" --prompt "角色形象要求" --label "图片说明" --tags "立绘,默认"
```

参考主形象编辑：

```powershell
python "E:\Doc\AI直播\Skill\ai-live-character-image\scripts\character_image_api.py" --operation edit --reference primary --prompt "保持身份特征，修改服装" --label "服装变体"
```

脚本输出单行 JSON，`path` 是生成图片的绝对路径。Agent 必须打开该图片进行视觉检查，再报告成功或继续编辑。

## 网络注意事项

- 不要默认使用需要 VPN 的 API 地址。
- `base_url` 必须是当前网络可访问的供应商或内网网关。
- 若供应商兼容 OpenAI Images API，使用 `images`。
- 若供应商通过 Chat Completions 接收和返回图片，使用 `chat_multimodal`。
- 不要在对话、日志、提示词或形象索引中输出 API Key。
