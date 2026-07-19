# Provider configuration

Configure `image_generation` in project-relative `config.yaml` and place the key in `.env`.

```yaml
image_generation:
  mode: images                 # images | chat_multimodal
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
