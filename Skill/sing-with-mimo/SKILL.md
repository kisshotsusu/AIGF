---
name: sing-with-mimo
description: Read or perform short lyric passages with the character's local TTS/SVC voice, with Xiaomi MiMo V2.5 retained as an optional disabled fallback. Use when the user asks the Home Agent character to sing, hum, test lyrics, or read a song passage.
---

# Sing with MiMo

Generate and play a short WAV by default through the existing local character TTS service. Keep MiMo singing available only as a manually enabled fallback.

## Workflow

1. Read project-relative `workspace/IDENTITY.yaml` and the `singing`/`tts` sections in `config.yaml`.
2. Prepare at most ten short lyric lines:
   - Use lyrics supplied by the user, explicitly licensed lyrics, or public-domain lyrics when available.
   - For a named commercial song without supplied rights-cleared lyrics, do not search for or reproduce its lyrics. Write ten new lines matching the requested topic or mood, and state that they are original rather than the song's original lyrics.
   - Never claim original text is an excerpt from an existing song.
3. In Home Agent, call `sing_song`; it reads `singing.mode` from the main config. The default `local_tts` branch sends the text to `http://127.0.0.1:9879` through the project's existing `TTSClient` and current character speaker/model/reference settings.
4. Do not enable the MiMo branch unless `singing.mimo_fallback_enabled` is explicitly changed to `true`. For manual fallback testing, run the bundled script:

```powershell
python scripts/sing_mimo.py --song "歌曲或主题" --lyrics-file lyrics.txt --style "温柔、轻快"
```

Or pass short text directly:

```powershell
python scripts/sing_mimo.py --song "雨夜" --lyrics "第一句\n第二句"
```

5. Parse the result. Report the saved WAV path, backend, and whether playback started.

## Voice selection

- In `local_tts` mode, use the complete existing `tts` configuration so the character keeps the same local voice.
- In the optional MiMo fallback, prefer `singing.voice` from the main config.
- Otherwise reuse `tts.speaker` only when it is a MiMo preset voice.
- Otherwise map the character's gender to `singing.female_voice` or `singing.male_voice`.
- MiMo singing currently supports preset voices only. Do not claim that GPT-SoVITS/SVC cloned timbre was reproduced exactly.

Read [references/mimo-tts.md](references/mimo-tts.md) only when adapting API parameters or troubleshooting the response.
