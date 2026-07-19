---
name: ai-live-character-image
description: Generate, edit, inspect, and manage images for the local AI livestream character through a configurable multimodal or Images API. Use when asked to create the character's portrait, avatar, outfit, expression, pose, scene, visual variant, or edited image, or to register/set a generated result in the current project's AI livestream character image library.
---

# AI Live Character Image

Create and operate the livestream character's visual assets while preserving identity.

## Workflow

1. Read project-relative `workspace/CHARACTER.md` and `workspace/character_images/manifest.json`.
2. When a primary image exists, inspect it with `view_image` before composing a prompt. Use it as the default reference for edits and identity-preserving variants.
3. Check `image_generation` in project-relative `config.yaml`. Read [provider-config.md](references/provider-config.md) only when configuration or provider adaptation is needed.
   In a new agent environment or a network without VPN, read [agent-handoff.md](references/agent-handoff.md) before the first call.
4. Run `scripts/character_image_api.py`:

```powershell
python scripts/character_image_api.py --prompt "完整画面要求" --label "图片说明" --tags "立绘,默认服装"
```

For an identity-preserving edit or variant:

```powershell
python scripts/character_image_api.py --operation edit --reference primary --prompt "保持角色身份，改为冬季服装" --label "冬装立绘"
```

5. Inspect the returned absolute image path with `view_image`. Check identity, anatomy, prompt compliance, text artifacts, and overall usability.
6. If unusable, revise the prompt or run one edit. Do not silently accept a broken result.
7. Keep every result as a new file. Set it as primary only when the user explicitly asks or the library has no primary image:

```powershell
python scripts/character_image_api.py --prompt "..." --set-primary
```

## Prompt construction

- Include stable appearance requirements from `CHARACTER.md`.
- State which traits must remain unchanged when a reference exists.
- Specify composition, pose, expression, outfit, background, lighting, style, and intended use.
- Avoid adding logos, signatures, captions, or watermarks unless requested.
- Do not include API keys, cookies, memories, or unrelated private data.

## Image operations

- **Generate**: create a new image from text, optionally allowing the provider to use no reference.
- **Edit/variant**: send the primary or explicitly named reference image to the configured multimodal endpoint.
- **Inspect**: always use `view_image` on the actual saved output before reporting success.
- **Manage**: register all outputs in `manifest.json`; preserve original images; use the management UI for later relabeling or deletion.

The script prints a JSON result with the saved path, library id, and primary status. Never expose the API key in output or logs.
