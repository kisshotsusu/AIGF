#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SenseVoiceSmall 语音识别核心 (FunASR / 阿里达摩院)。
- 支持音频文件(wav/mp3/flac/ogg/m4a) 与 麦克风实时录音 转写
- 多语种: zh / en / ja / ko / yue / auto
- 输出带标点、ITN(数字规整) 与情绪/事件标签(<|HAPPY|> 等), 同时给出清洗后纯文本
依赖: funasr, modelscope, sounddevice, torch(torchaudio)
"""
import os
import io
import re
import shutil
import tempfile
import wave

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get(
    "SENSEVOICE_MODEL", os.path.join(HERE, "models", "SenseVoiceSmall")
)
# ModelScope 模型 id; 想走 HuggingFace 镜像可设为 "FunAudioLLM/SenseVoiceSmall"
MODEL_ID = os.environ.get("SENSEVOICE_MODEL_ID", "iic/SenseVoiceSmall")

DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"
SR = 16000

_model = None


def _ascii_model_dir():
    """返回纯 ASCII 的模型目录。

    sentencepiece 的 C++ 后端无法打开含中文的路径(本机 NTFS 关闭了 8.3 短名生成,
    GetShortPathNameW 返回的短名实际不存在, 仍会 NOT_FOUND)。因此若模型目录含非
    ASCII 字符, 直接整目录复制到系统 TEMP(纯 ASCII) 下的固定位置并复用, 规避该坑。
    """
    src = MODEL_DIR
    if all(ord(c) < 128 for c in src):
        return src
    dst = os.path.join(tempfile.gettempdir(), "sensevoice_model")
    marker = os.path.join(dst, "model.pt")
    if not os.path.exists(marker):
        print(f"[asr] 模型路径含中文, 复制到 ASCII 临时目录: {dst}", flush=True)
        os.makedirs(dst, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
    return dst


def load_model():
    """懒加载 SenseVoiceSmall (含 fsmn-vad)。"""
    global _model
    if _model is not None:
        return _model
    from funasr import AutoModel

    # 优先用本地已下载目录(转成 ASCII 路径), 否则让 AutoModel 从 ModelScope 自动下载
    if os.path.isdir(MODEL_DIR) and os.listdir(MODEL_DIR):
        local = _ascii_model_dir()
    else:
        local = MODEL_ID
    print(f"[asr] loading SenseVoiceSmall from '{local}' -> {DEVICE}", flush=True)
    _model = AutoModel(
        model=local,
        trust_remote_code=True,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_pad_len": 30},
        device=DEVICE,
        disable_update=True,
    )
    print("[asr] model loaded.", flush=True)
    return _model


def to_wav16k(path_or_bytes):
    """任意格式音频 -> 16k 单声道 wav 临时文件(返回 ASCII 临时路径)。

    用 librosa 加载: 原生支持中文路径与 mp3/flac/m4a 等, 并直接重采样到 16k。
    临时文件落在系统 TEMP(ASCII), 避免 funasr/sentencepiece 读非 ASCII 路径报错。
    """
    import librosa
    import soundfile as sf

    if isinstance(path_or_bytes, (bytes, bytearray)):
        src = io.BytesIO(path_or_bytes)
    else:
        src = str(path_or_bytes)
    # librosa 自动处理重采样 + 单声道, 返回 float32 波形
    wav, _ = librosa.load(src, sr=SR, mono=True)
    wav = wav.astype(np.float32)

    tmp = tempfile.gettempdir()
    fd, out_path = tempfile.mkstemp(suffix=".wav", dir=tmp)
    os.close(fd)
    sf.write(out_path, wav, SR)
    return out_path


def _parse(res):
    """解析 FunASR 返回, 给出原始文本与清洗文本(去情绪/事件/语种标签)。"""
    if not res:
        return {"raw": "", "text": ""}
    raw = res[0].get("text", "") if isinstance(res, list) else res.get("text", "")
    # 清洗: 去掉 <|...|> 形式标签
    import re
    clean = re.sub(r"<\|[^|]+\|>", "", raw).strip()
    return {"raw": raw, "text": clean}


def transcribe_file(path_or_bytes, language: str = "auto"):
    """转写一个音频文件/字节。返回 {raw, text, language}。"""
    m = load_model()
    wav_path = to_wav16k(path_or_bytes)
    try:
        res = m.generate(
            input=wav_path,
            cache={},
            language=language,
            use_itn=True,
            batch_size_s=60,
            merge_vad=True,
            merge_length_s=15,
        )
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass
    out = _parse(res)
    out["language"] = language
    return out


def record_and_transcribe(duration: float = 5.0, language: str = "auto", sr: int = SR):
    """录音 duration 秒并转写。返回 {raw, text, language}。"""
    import sounddevice as sd

    print(f"[asr] recording {duration}s ...", flush=True)
    sd.default.samplerate = sr
    sd.default.channels = 1
    audio = sd.rec(int(duration * sr), samplerate=sr, channels=1, dtype="float32")
    sd.wait()
    # float32 -> int16 wav 字节
    pcm = (audio.flatten() * 32767).clip(-32768, 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return transcribe_file(buf.getvalue(), language=language)


if __name__ == "__main__":
    # 自测: 生成 3 秒 16k 正弦 wav 验证管线(非真实语音, 仅验证可运行)
    import soundfile as sf

    t = np.linspace(0, 3.0, 3 * SR, dtype=np.float32)
    tone = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    p = os.path.join(HERE, "test_audio", "self_test.wav")
    sf.write(p, tone, SR)
    print("self-test wav ->", p)
    print(transcribe_file(p))
