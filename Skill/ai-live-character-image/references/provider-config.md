# Provider configuration

Configure `image_generation` in project-relative `config.yaml` and place the key in `.env`.

```yaml
image_generation:
  preset: custom               # custom | qwen | grok
  mode: images                 # images | chat_multimodal | dashscope_multimodal | xai_images
  base_url: https://api.example.com/v1
  api_key_env: IMAGE_API_KEY
  model: your-image-model
  size: 1024x1024
  timeout_seconds: 180
```

```dotenv
IMAGE_API_KEY=replace-me
```

## images mode

- Generate: `POST {base_url}/images/generations` with JSON.
- Edit: `POST {base_url}/images/edits` with multipart form data and the reference image.
- Accepts either `data[0].b64_json` or `data[0].url` in the response.

## chat_multimodal mode

- Calls `POST {base_url}/chat/completions`.
- Sends the reference as an `image_url` data URI when editing.
- Extracts an image from `choices[0].message.images`, `choices[0].message.content`, a data URI, or a direct response `data` item.

Provider-specific fields can be added under `extra_body`; the script merges them into the request body.

## 千问图像预设

```yaml
image_generation:
  preset: qwen
  provider: qwen
  mode: dashscope_multimodal
  base_url: https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1
  api_key_env: DASHSCOPE_API_KEY
  model: qwen-image-2.0-pro
  size: 2048*2048
  timeout_seconds: 300
```

将 `{WorkspaceId}` 替换为百炼工作空间 ID。北京和新加坡的 API Key 与地址不能混用；新加坡地址使用
`https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/api/v1`。

## Grok Imagine 预设

```yaml
image_generation:
  preset: grok
  provider: xai
  mode: xai_images
  base_url: https://api.x.ai/v1
  api_key_env: XAI_API_KEY
  model: grok-imagine-image-quality
  size: ''
  timeout_seconds: 300
```

`xai_images` 使用 `/images/generations`，编辑时使用 `/images/edits`；不向 xAI 发送通用 `size` 字段。

## MiMo image understanding

MiMo's multimodal model analyzes an input image and returns text through `chat/completions`. It is separate from `image_generation` and must not be configured as an image generation/editing provider.

```yaml
image_understanding:
  provider: mimo
  base_url: https://api.xiaomimimo.com/v1
  api_key_env: MIMO_API_KEY
  model: mimo-v2.5
  auth_header: api-key
  max_tokens_field: max_completion_tokens
  max_completion_tokens: 1024
  timeout_seconds: 60
  extra_body:
    thinking:
      type: disabled
```

```dotenv
MIMO_API_KEY=replace-me
```

Run `scripts/image_understanding_api.py --image primary --prompt "..."`. Local paths are encoded as an `image_url` data URI and sent to MiMo only when this command or the HomeAgent `analyze_image` tool is invoked.
