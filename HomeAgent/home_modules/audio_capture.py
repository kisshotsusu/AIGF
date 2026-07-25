from __future__ import annotations

from typing import Any


def resolve_input_settings(
    sounddevice_module: Any,
    *,
    device: int | str | None,
    requested_rate: int | float = 16000,
    requested_channels: int = 1,
    dtype: str = "int16",
) -> dict[str, Any]:
    """Choose capture settings accepted by the selected input device."""
    devices: list[int | str | None] = [device]
    if device is not None:
        devices.append(None)
    errors: list[str] = []
    for candidate in devices:
        try:
            info = sounddevice_module.query_devices(candidate, "input")
            max_channels = int(info.get("max_input_channels") or 0)
            if max_channels < 1:
                raise ValueError("设备没有输入通道")
            channels = max(1, min(int(requested_channels or 1), max_channels))
            rates: list[int] = []
            for value in (requested_rate, info.get("default_samplerate"), 48000, 44100, 16000):
                try:
                    rate = int(round(float(value)))
                except (TypeError, ValueError):
                    continue
                if rate > 0 and rate not in rates:
                    rates.append(rate)
            for rate in rates:
                try:
                    sounddevice_module.check_input_settings(
                        device=candidate, samplerate=rate,
                        channels=channels, dtype=dtype,
                    )
                    requested = int(requested_rate or 16000)
                    return {
                        "device": candidate,
                        "sample_rate": rate,
                        "channels": channels,
                        "dtype": dtype,
                        "device_name": str(info.get("name") or ""),
                        "requested_sample_rate": requested,
                        "used_native_rate": rate != requested,
                    }
                except Exception as exc:
                    errors.append(f"{candidate!r}@{rate}Hz: {exc}")
        except Exception as exc:
            errors.append(f"{candidate!r}: {exc}")
    detail = "；".join(errors[-6:]) or "没有可用输入设备"
    raise RuntimeError(f"无法为麦克风协商可用采样率：{detail}")
