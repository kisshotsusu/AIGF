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
