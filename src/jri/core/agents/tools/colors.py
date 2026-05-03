import json
import re
from typing import Any


def run_contrast_check(payload: dict[str, Any]) -> str:
    foreground = _normalize_hex_color(
        "foreground", payload.get("foreground"), allow_alpha=True
    )
    background = _normalize_hex_color(
        "background", payload.get("background"), allow_alpha=False
    )
    standard, threshold = _assert_contrast_standard(payload.get("standard"))

    fg_red, fg_green, fg_blue, fg_alpha = _hex_to_rgba(foreground)
    bg_red, bg_green, bg_blue, _bg_alpha = _hex_to_rgba(background)
    alpha = 1.0 if fg_alpha is None else fg_alpha / 255.0

    composite_red = fg_red * alpha + bg_red * (1.0 - alpha)
    composite_green = fg_green * alpha + bg_green * (1.0 - alpha)
    composite_blue = fg_blue * alpha + bg_blue * (1.0 - alpha)

    fg_luminance = _relative_luminance(composite_red, composite_green, composite_blue)
    bg_luminance = _relative_luminance(bg_red, bg_green, bg_blue)
    lighter = max(fg_luminance, bg_luminance)
    darker = min(fg_luminance, bg_luminance)
    ratio = (lighter + 0.05) / (darker + 0.05)

    result = {
        "standard": standard,
        "ratio": round(ratio, 2),
        "threshold": threshold,
        "result": "pass" if ratio >= threshold else "fail",
    }
    return json.dumps(result, indent=2) + "\n"


def _normalize_hex_color(name: str, value: Any, *, allow_alpha: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{name}` must be a non-empty hex color string")
    normalized = value.strip().removeprefix("#")
    allowed_lengths = {3, 6}
    if allow_alpha:
        allowed_lengths |= {4, 8}
    if len(normalized) not in allowed_lengths or not re.fullmatch(
        r"[0-9a-fA-F]+", normalized
    ):
        raise ValueError(
            f"`{name}` must be a valid {sorted(allowed_lengths)}-digit hex color"
        )
    if len(normalized) in {3, 4}:
        normalized = "".join(ch * 2 for ch in normalized)
    return normalized.upper()


def _srgb_channel_to_linear(channel: float) -> float:
    normalized = channel / 255.0
    if normalized <= 0.03928:
        return normalized / 12.92
    return ((normalized + 0.055) / 1.055) ** 2.4


def _relative_luminance(red: float, green: float, blue: float) -> float:
    return (
        0.2126 * _srgb_channel_to_linear(red)
        + 0.7152 * _srgb_channel_to_linear(green)
        + 0.0722 * _srgb_channel_to_linear(blue)
    )


def _hex_to_rgba(color: str) -> tuple[int, int, int, int | None]:
    if len(color) == 6:
        return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), None
    return (
        int(color[0:2], 16),
        int(color[2:4], 16),
        int(color[4:6], 16),
        int(color[6:8], 16),
    )


def _assert_contrast_standard(value: Any) -> tuple[str, float]:
    thresholds = {
        "AA": 4.5,
        "AALarge": 3.0,
        "AAA": 7.0,
        "AAALarge": 4.5,
        "GraphicsAA": 3.0,
    }
    if not isinstance(value, str) or value not in thresholds:
        raise ValueError(
            "`standard` must be one of: AA, AALarge, AAA, AAALarge, GraphicsAA"
        )
    return value, thresholds[value]
