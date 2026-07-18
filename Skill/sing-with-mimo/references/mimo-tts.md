# MiMo V2.5 singing API notes

- Endpoint: `POST https://api.xiaomimimo.com/v1/chat/completions`
- API key environment variable: `MIMO_API_KEY`
- Singing model: `mimo-v2.5-tts`
- Put performance direction in a `user` message.
- Put target text in an `assistant` message and prefix it with `(唱歌)`.
- Request `audio.format: wav` and a supported preset `audio.voice`.
- Decode `choices[0].message.audio.data` from Base64.
- Preset Chinese voices include `冰糖`, `茉莉`, `苏打`, and `白桦`.
- Voice-design and voice-clone models do not support singing mode.

Source: https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5

