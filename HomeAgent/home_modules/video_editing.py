from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .video_understanding import _fmt_hms, _first_value, _srt_timestamp, parse_time

class VideoEditor:
    """ffmpeg/ffprobe helpers for cutting, subtitling and voiceover mixing."""

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = dict(config or {})
        self.project_root = Path(cfg.get("project_root") or Path.cwd()).resolve()
        output_dir = Path(cfg.get("output_dir") or "outputs/video_understanding")
        self.output_dir = (
            output_dir.resolve() if output_dir.is_absolute()
            else (self.project_root / output_dir).resolve()
        )
        self.ffmpeg = str(cfg.get("ffmpeg") or "ffmpeg")
        self.ffprobe = str(cfg.get("ffprobe") or "ffprobe")

    def _run(self, command: list[str]) -> str:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            check=False,
        )
        stdout = completed.stdout.decode("utf-8", "replace").strip()
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        if completed.returncode != 0:
            detail = (stderr or stdout)[-1200:]
            raise RuntimeError(f"命令失败（exit={completed.returncode}）：{detail}")
        return stdout

    def probe(self, path: str | Path) -> dict[str, Any]:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"视频文件不存在：{source}")
        command = [
            self.ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
            "-of", "json", str(source),
        ]
        out = self._run(command)
        data = json.loads(out or "{}")
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        duration = float(
            stream.get("duration") or fmt.get("duration") or 0.0
        )
        return {
            "ok": True,
            "path": str(source),
            "duration_seconds": duration,
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "fps": str(stream.get("avg_frame_rate") or ""),
        }

    def _resolve_output_dir(self, value: str | Path | None = None) -> Path:
        if value:
            path = Path(value).expanduser()
            return path.resolve() if path.is_absolute() else (self.project_root / path).resolve()
        return self.output_dir

    @staticmethod
    def _safe_component(value: Any) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(value or "")).strip("_")
        return cleaned or "segment"

    @staticmethod
    def _stamp(seconds: float) -> str:
        return _fmt_hms(seconds).replace(":", "")

    def _normalize_segments(self, segments: Any) -> list[dict[str, Any]]:
        if isinstance(segments, dict):
            raw_items = [segments]
        elif isinstance(segments, (list, tuple)):
            raw_items = list(segments)
        else:
            raise ValueError("segments 必须是对象或对象列表")
        normalized: list[dict[str, Any]] = []
        for raw in raw_items:
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                start = parse_time(raw[0])
                end = parse_time(raw[1])
                label = str(raw[2]) if len(raw) > 2 else ""
            elif isinstance(raw, dict):
                start = parse_time(_first_value(
                    raw, ("start_time", "start", "start_seconds", "start_sec", "from")
                ))
                end = parse_time(_first_value(
                    raw, ("end_time", "end", "end_seconds", "end_sec", "to", "until")
                ))
                label = str(_first_value(raw, ("event", "label", "text"), ""))
            elif isinstance(raw, str):
                match = re.fullmatch(r"\s*([0-9:.]+)\s*[-~,，]\s*([0-9:.]+)\s*(.*)", raw)
                if not match:
                    raise ValueError(f"无法解析时间段：{raw}")
                start = parse_time(match.group(1))
                end = parse_time(match.group(2))
                label = match.group(3).strip()
            else:
                raise ValueError(f"不支持的时间段格式：{raw!r}")
            if start is None or end is None or end <= start:
                raise ValueError(
                    "时间段必须包含有效开始和结束时间，且结束大于开始"
                )
            normalized.append({"start": start, "end": end, "label": label})
        if not normalized:
            raise ValueError("没有可用的时间段")
        return normalized

    def cut_segments(self, input_path: str | Path, segments: Any,
                     output_dir: str | Path | None = None,
                     prefix: str = "clip") -> dict[str, Any]:
        source = Path(input_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"视频文件不存在：{source}")
        items = self._normalize_segments(segments)
        target = self._resolve_output_dir(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        clips: list[dict[str, Any]] = []
        for index, seg in enumerate(items, 1):
            duration = max(0.1, seg["end"] - seg["start"])
            label = self._safe_component(seg.get("label"))[:24]
            name = (
                f"{prefix}_{index:03d}_{self._stamp(seg['start'])}_{self._stamp(seg['end'])}"
            )
            if label:
                name += f"_{label}"
            out = target / f"{name}.mp4"
            command = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{seg['start']:.3f}", "-i", str(source),
                "-t", f"{duration:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-movflags", "+faststart", str(out),
            ]
            self._run(command)
            probe = self.probe(out)
            clips.append({
                "index": index,
                "ok": True,
                "path": str(out),
                "start_seconds": round(seg["start"], 3),
                "end_seconds": round(seg["end"], 3),
                "duration_seconds": round(probe.get("duration_seconds") or duration, 3),
                "label": seg.get("label", ""),
            })
        return {"ok": True, "count": len(clips), "input": str(source), "clips": clips}

    def build_srt(self, items: Any, output_path: str | Path | None = None,
                  duration: Any = None) -> str | Path:
        """Build SRT text (or file) from timed dicts or plain text lines."""
        if isinstance(items, str):
            items = [items]
        entries = list(items or [])
        if not entries:
            raise ValueError("字幕内容不能为空")
        duration_value = parse_time(duration) if duration is not None else None
        has_plain = any(isinstance(item, str) for item in entries)
        if has_plain and duration_value is None:
            raise ValueError("纯文本字幕行需要 duration 参数分配时间")
        span = (duration_value / max(1, len(entries))) if duration_value else None
        blocks: list[str] = []
        for index, item in enumerate(entries, 1):
            if isinstance(item, str):
                start = (index - 1) * span if span else 0.0
                end = min(duration_value, index * span) if span and duration_value else start + 5.0
                text = item.strip()
            elif isinstance(item, dict):
                start = parse_time(_first_value(
                    item,
                    ("start_seconds", "start_time", "start"),
                    (index - 1) * span if span else None,
                ))
                end = parse_time(_first_value(
                    item,
                    ("end_seconds", "end_time", "end"),
                    (index * span if span else None),
                ))
                text = str(_first_value(
                    item, ("event", "text", "content", "subtitle", "description"), ""
                )).strip()
                if start is None:
                    start = (index - 1) * span if span else 0.0
                if end is None or end <= start:
                    end = start + (span or 5.0)
            else:
                raise ValueError(f"不支持的字幕条目：{item!r}")
            if not text:
                text = f"片段 {index}"
            blocks.append(
                f"{index}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}"
            )
        srt_text = "\n\n".join(blocks) + "\n"
        if output_path:
            target = Path(output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(srt_text, encoding="utf-8")
            return target
        return srt_text

    @staticmethod
    def _escape_filter_path(path: str | Path) -> str:
        value = str(Path(path).resolve()).replace("\\", "/")
        return value.replace("'", "\\'").replace(":", "\\:")

    def burn_subtitles(self, input_path: str | Path, srt_path: str | Path,
                       output_path: str | Path,
                       fontsize: int | None = None) -> dict[str, Any]:
        source = Path(input_path).resolve()
        sub = Path(srt_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"视频文件不存在：{source}")
        if not sub.is_file():
            raise FileNotFoundError(f"字幕文件不存在：{sub}")
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        filter_spec = f"subtitles='{self._escape_filter_path(sub)}'"
        if fontsize:
            filter_spec += f":force_style='FontSize={int(fontsize)}'"
        command = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-vf", filter_spec,
            "-c:a", "copy", "-movflags", "+faststart", str(target),
        ]
        self._run(command)
        return {
            "ok": True,
            "path": str(target),
            "input": str(source),
            "subtitles": str(sub),
            "mode": "burn",
        }

    def mix_voiceover(self, input_path: str | Path, audio_path: str | Path,
                      output_path: str | Path, volume: float = 1.0,
                      mode: str = "replace") -> dict[str, Any]:
        source = Path(input_path).resolve()
        voice = Path(audio_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"视频文件不存在：{source}")
        if not voice.is_file():
            raise FileNotFoundError(f"旁白音频不存在：{voice}")
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        volume = max(0.0, min(3.0, float(volume)))
        mode = str(mode or "replace").lower()
        if mode == "replace":
            command = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source), "-i", str(voice),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac",
            ]
            if abs(volume - 1.0) > 1e-6:
                command += ["-filter:a", f"volume={volume:.3f}"]
            command += ["-shortest", "-movflags", "+faststart", str(target)]
        elif mode == "mix":
            command = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source), "-i", str(voice),
                "-filter_complex",
                f"[1:a]volume={volume:.3f}[vo];"
                "[0:a][vo]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                "-map", "0:v:0", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac",
                "-movflags", "+faststart", str(target),
            ]
        else:
            raise ValueError("mode 必须是 replace 或 mix")
        self._run(command)
        return {
            "ok": True,
            "path": str(target),
            "input": str(source),
            "voiceover": str(voice),
            "mode": mode,
            "volume": volume,
        }


    def concat_videos(self, input_paths: list, output_path: str | Path | None = None,
                      prefix: str = "merged") -> dict[str, Any]:
        """把多个视频按给定顺序拼接成一个文件（独立能力，不依赖理解/字幕/配音）。"""
        paths: list[Path] = []
        for value in input_paths or []:
            path = Path(str(value)).expanduser()
            if not path.is_absolute():
                path = (self.project_root / path).resolve()
            else:
                path = path.resolve()
            if not path.is_file():
                raise FileNotFoundError(f"视频不存在：{path}")
            paths.append(path)
        if not paths:
            raise ValueError("至少需要一个视频")
        target = Path(output_path) if output_path else (
            self.output_dir / f"{prefix}_{len(paths):03d}.mp4"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        list_file = target.with_suffix(".concat.txt")
        with list_file.open("w", encoding="utf-8") as handle:
            for path in paths:
                escaped = str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
                handle.write(f"file '{escaped}'\n")
        try:
            command = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-movflags", "+faststart", str(target),
            ]
            self._run(command)
        finally:
            try:
                list_file.unlink(missing_ok=True)
            except OSError:
                pass
        return {
            "ok": True,
            "path": str(target),
            "count": len(paths),
            "inputs": [str(path) for path in paths],
        }

    def finalize(self, input_path: str | Path, srt_path: str | Path,
                 voiceover_path: str | Path, output_path: str | Path,
                 volume: float = 1.0) -> dict[str, Any]:
        """One-pass burn subtitles and replace audio with a voiceover."""
        source = Path(input_path).resolve()
        sub = Path(srt_path).resolve()
        voice = Path(voiceover_path).resolve()
        for label, path in (("视频", source), ("字幕", sub), ("旁白", voice)):
            if not path.is_file():
                raise FileNotFoundError(f"{label}文件不存在：{path}")
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-i", str(voice),
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", f"subtitles='{self._escape_filter_path(sub)}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac",
        ]
        if abs(float(volume) - 1.0) > 1e-6:
            command += ["-filter:a", f"volume={max(0.0, min(3.0, float(volume))):.3f}"]
        command += ["-shortest", "-movflags", "+faststart", str(target)]
        self._run(command)
        return {
            "ok": True,
            "path": str(target),
            "input": str(source),
            "subtitles": str(sub),
            "voiceover": str(voice),
            "mode": "subtitle+voiceover",
        }
