import json, sys
from faster_whisper import WhisperModel

model_path, audio_path, language = sys.argv[1:4]
model = WhisperModel(model_path, device="cuda", compute_type="float16")
segments, _ = model.transcribe(audio_path, language=language, vad_filter=True)
print(json.dumps({"text": "".join(segment.text for segment in segments)}, ensure_ascii=False))

