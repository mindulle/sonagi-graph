"""taste/motion_features — 영상·프레임 시퀀스에서 결정론적 모션 특징을 뽑는다.

같은 파일을 두 번 넣으면 항상 같은 숫자가 나온다. 머신러닝 라이브러리는 쓰지
않고 **Pillow + 표준 라이브러리**만 쓴다. `ffmpeg`/`ffprobe`가 설치되어 있으면
`graphs.core.exec`를 통해 영상(mp4/webm 등)의 프레임·오디오 추출에 활용하고,
없으면 해당 키를 `None`으로 둔 채 예외를 던지지 않는다.

값은 "심사 점수"가 아니라 A/B 비교용 눈금이다 (GRAPH-52).

공통 출력(키는 항상 같다)::

    {
      "linear_ratio":    float | None,        # 전체 이동 중 등속 구간 비율(0.0~1.0)
      "stagger_offsets": list[float] | None,  # 요소별 최초 등장 시각 차이(초)
      "overshoot_ratio": float | None,        # 스케일 최대값 / 목표값(예: 1.03)
      "loop_seam_diff":  float | None,        # 첫 프레임과 끝 프레임의 평균 채널 차이(0~255)
      "audio_offset":    float | None,        # 오디오 트랜지언트 - 모션 피크(초)
      "source":          "video" | "frames"
    }

사용::

    from graphs.taste import motion_features

    motion_features.extract_motion_features("/path/to/frames_dir")
    motion_features.safe_extract_features("/path/to/render.mp4")  # 실패하면 {}
"""

from __future__ import annotations

import math
import shutil
import struct
import tempfile
import wave
from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from graphs.core import exec as core_exec

#: 분석 시 긴 변을 이 크기로 줄인다.
#: 103% 오버슈트 등 미세한 크기 변화(약 3%)가 양자화에 묻히지 않도록 256px를 사용한다.
ANALYSIS_MAX_DIM = 256
#: 프레임 시퀀스 디렉터리 또는 ffprobe 부재 시 사용하는 기본 FPS.
DEFAULT_FPS = 30.0
#: 인접 프레임 간 픽셀 밝기 차분(0~255)이 이 값 이상일 때 유효 모션으로 본다.
MOTION_DIFF_THRESHOLD = 8
#: 한 프레임 차분에서 이 픽셀 수 미만이면 정지 구간으로 간주한다.
MIN_MOTION_PIXELS = 4
#: 활성 이동 구간으로 인정하는 최소 속도(분석 해상도 기준 px/frame).
MIN_ACTIVE_SPEED = 0.25
#: 속도 분산을 계산할 연속 속도 벡터 슬라이딩 윈도우 크기.
#: 3프레임 윈도우는 부드러운 이징 곡선에서도 국소 변화가 작아 등속으로 오인되기 쉬우므로
#: 가감속 기울기 변화가 드러나는 5프레임 윈도우를 사용한다.
VELOCITY_WINDOW = 5
#: 등속 구간 판정 임계값(정규화 속도의 윈도우 내 분산).
#:
#: 근거: 각 활성 프레임의 속도를 평균 속도로 정규화(u_t = |v_t| / mean_s)하면
#: 이상적인 등속 이동의 분산은 0이다. 256px 다운스케일 및 정수 픽셀 양자화로
#: 생기는 미세 오차(표준편차 약 7% 이내, 0.071^2 ≈ 0.005)까지는 등속으로
#: 허용하고, 가감속 이징 곡선(ease-in/out)의 5프레임 구간 속도 변화는
#: 비등속으로 분리하기 위해 0.005를 임계값으로 둔다.
LINEAR_VARIANCE_THRESHOLD = 0.005
#: 첫 프레임 테두리 배경색 대비 이 값 이상 차이나면 전경 픽셀로 본다.
BG_DIFF_THRESHOLD = 16
#: 잡음으로 제외할 최소 전경 덩어리 크기(픽셀).
MIN_ELEMENT_PIXELS = 12
#: 오디오 트랜지언트 RMS 에너지 계산 윈도우(ms).
AUDIO_WINDOW_MS = 10

VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
FRAME_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class MotionFeatureError(ValueError):
    """모션 특징을 추출할 수 없는 경로·형식일 때 발생한다."""


def _empty_result(source: str) -> dict[str, Any]:
    return {
        "linear_ratio": None,
        "stagger_offsets": None,
        "overshoot_ratio": None,
        "loop_seam_diff": None,
        "audio_offset": None,
        "source": source,
    }


def extract_motion_features(path: str | Path) -> dict[str, Any]:
    """영상 파일 또는 프레임 디렉터리에서 결정론적 모션 특징을 추출한다."""
    if not path:
        raise MotionFeatureError("경로가 비어 있습니다.")

    target = Path(path)
    if not target.exists():
        raise MotionFeatureError(f"경로를 찾을 수 없습니다: {target}")

    if target.is_dir():
        frame_paths = sorted(
            (
                p
                for p in target.iterdir()
                if p.is_file() and p.suffix.lower() in FRAME_SUFFIXES
            ),
            key=lambda p: p.name,
        )
        if not frame_paths:
            raise MotionFeatureError(f"프레임 이미지가 없습니다: {target}")
        frames = [_load_rgb_frame(p) for p in frame_paths]
        return _analyze_frames(frames, fps=DEFAULT_FPS, audio_path=None, source="frames")

    if target.is_file():
        suffix = target.suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            raise MotionFeatureError(
                f"지원하지 않는 형식입니다: {suffix or '(확장자 없음)'}"
            )
        return _extract_from_video(target)

    raise MotionFeatureError(f"파일 또는 디렉터리가 아닙니다: {target}")


def safe_extract_features(path: str | Path) -> dict[str, Any]:
    """모션 특징 추출 실패 시 예외 대신 빈 dict를 반환한다."""
    try:
        return extract_motion_features(path)
    except (MotionFeatureError, OSError, ValueError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# 내부: 프레임·오디오 로드
# ---------------------------------------------------------------------------


def _load_rgb_frame(path: Path) -> Image.Image:
    """이미지를 열어 결정론적 크기의 RGB 이미지로 변환한다."""
    with Image.open(path) as raw:
        if raw.mode == "RGBA":
            canvas = Image.new("RGBA", raw.size, (0, 0, 0, 255))
            rgb = Image.alpha_composite(canvas, raw).convert("RGB")
        else:
            rgb = raw.convert("RGB")
        rgb.thumbnail((ANALYSIS_MAX_DIM, ANALYSIS_MAX_DIM), Image.Resampling.BILINEAR)
        return rgb


def _probe_video_fps(video_path: Path) -> float:
    """ffprobe로 FPS를 읽는다. 없거나 실패하면 DEFAULT_FPS를 반환한다."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return DEFAULT_FPS
    try:
        proc = core_exec.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=r_frame_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            timeout=15,
            check=False,
        )
        raw = proc.stdout.strip()
        if "/" in raw:
            num_s, den_s = raw.split("/", 1)
            num, den = float(num_s), float(den_s)
            if den > 0 and num > 0:
                return num / den
        elif raw:
            val = float(raw)
            if val > 0:
                return val
    except (OSError, ValueError):
        pass
    return DEFAULT_FPS


def _extract_from_video(video_path: Path) -> dict[str, Any]:
    """ffmpeg로 영상 프레임과 오디오를 추출해 분석한다. 없으면 None 채움."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return _empty_result("video")

    fps = _probe_video_fps(video_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        frame_pattern = tmp / "frame_%06d.png"
        try:
            core_exec.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-i",
                    str(video_path),
                    "-vf",
                    f"scale='min({ANALYSIS_MAX_DIM},iw)':-2",
                    str(frame_pattern),
                ],
                timeout=60,
                check=False,
            )
        except OSError:
            return _empty_result("video")

        frame_paths = sorted(tmp.glob("frame_*.png"), key=lambda p: p.name)
        if not frame_paths:
            return _empty_result("video")

        audio_path: Path | None = tmp / "audio.wav"
        try:
            proc_audio = core_exec.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-i",
                    str(video_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-f",
                    "wav",
                    str(audio_path),
                ],
                timeout=30,
                check=False,
            )
            if (
                not proc_audio.ok
                or not audio_path.exists()
                or audio_path.stat().st_size <= 44
            ):
                audio_path = None
        except OSError:
            audio_path = None

        frames = [_load_rgb_frame(p) for p in frame_paths]
        return _analyze_frames(frames, fps=fps, audio_path=audio_path, source="video")


# ---------------------------------------------------------------------------
# 내부: 모션·루프·스태거·오버슈트·오디오 계산
# ---------------------------------------------------------------------------


def _analyze_frames(
    frames: list[Image.Image],
    *,
    fps: float,
    audio_path: Path | None,
    source: str,
) -> dict[str, Any]:
    if len(frames) < 2:
        return _empty_result(source)

    loop_diff = _compute_loop_seam_diff(frames[0], frames[-1])
    grays = [f.convert("L") for f in frames]
    linear_ratio, motion_peak_sec = _compute_linear_and_peak(grays, fps)

    bg_color = _estimate_border_color(frames[0])
    fg_masks = [_foreground_mask(f, bg_color) for f in frames]
    width, height = frames[0].size

    stagger_offsets = _compute_stagger_offsets(fg_masks, width, height, fps)
    overshoot_ratio = _compute_overshoot_ratio(fg_masks)
    audio_offset = _compute_audio_offset(audio_path, motion_peak_sec)

    return {
        "linear_ratio": linear_ratio,
        "stagger_offsets": stagger_offsets,
        "overshoot_ratio": overshoot_ratio,
        "loop_seam_diff": loop_diff,
        "audio_offset": audio_offset,
        "source": source,
    }


def _compute_loop_seam_diff(first: Image.Image, last: Image.Image) -> float:
    """첫 프레임과 마지막 프레임의 평균 채널 차이(0~255)."""
    if first.size != last.size:
        last = last.resize(first.size, Image.Resampling.BILINEAR)
    diff = ImageChops.difference(first, last)
    means = ImageStat.Stat(diff).mean
    return round(sum(means) / len(means), 4)


def _compute_linear_and_peak(
    grays: list[Image.Image], fps: float
) -> tuple[float | None, float | None]:
    """프레임 차분으로 속도 벡터를 구해 등속 비율과 모션 피크 시각을 계산한다."""
    width, height = grays[0].size
    total_pixels = max(1, width * height)

    centroids: list[tuple[float, float] | None] = []
    energies: list[float] = []

    for idx in range(len(grays) - 1):
        g_curr = grays[idx]
        g_next = grays[idx + 1]
        if g_curr.size != g_next.size:
            g_next = g_next.resize(g_curr.size, Image.Resampling.BILINEAR)
        diff = ImageChops.difference(g_curr, g_next)
        pixels = list(diff.getdata())

        weight_sum = 0.0
        wx_sum = 0.0
        wy_sum = 0.0
        active_count = 0
        energy_sum = 0

        for pos, val in enumerate(pixels):
            energy_sum += val
            if val >= MOTION_DIFF_THRESHOLD:
                active_count += 1
                x = pos % width
                y = pos // width
                weight_sum += val
                wx_sum += x * val
                wy_sum += y * val

        energies.append(energy_sum / total_pixels)
        if active_count >= MIN_MOTION_PIXELS and weight_sum > 0:
            centroids.append((wx_sum / weight_sum, wy_sum / weight_sum))
        else:
            centroids.append(None)

    motion_peak_sec: float | None = None
    if energies and max(energies) > 0:
        peak_idx = max(range(len(energies)), key=lambda i: energies[i])
        motion_peak_sec = round((peak_idx + 0.5) / fps, 4)

    active_speeds: list[float] = []
    for idx in range(len(centroids) - 1):
        c0 = centroids[idx]
        c1 = centroids[idx + 1]
        if c0 is None or c1 is None:
            continue
        speed = math.hypot(c1[0] - c0[0], c1[1] - c0[1])
        if speed >= MIN_ACTIVE_SPEED:
            active_speeds.append(speed)

    if len(active_speeds) < 2:
        return None, motion_peak_sec

    mean_speed = sum(active_speeds) / len(active_speeds)
    if mean_speed <= 0:
        return None, motion_peak_sec

    norm_speeds = [s / mean_speed for s in active_speeds]
    win_size = min(VELOCITY_WINDOW, len(norm_speeds))
    total_windows = len(norm_speeds) - win_size + 1
    linear_windows = 0

    for start in range(total_windows):
        window = norm_speeds[start : start + win_size]
        mean_w = sum(window) / win_size
        var_w = sum((v - mean_w) ** 2 for v in window) / win_size
        if var_w <= LINEAR_VARIANCE_THRESHOLD:
            linear_windows += 1

    return round(linear_windows / total_windows, 4), motion_peak_sec


def _estimate_border_color(frame: Image.Image) -> tuple[int, int, int]:
    """첫 프레임 테두리 픽셀에서 가장 흔한 색을 배경색으로 추정한다."""
    width, height = frame.size
    pixels = list(frame.getdata())
    border: Counter[tuple[int, int, int]] = Counter()
    for x in range(width):
        border[pixels[x]] += 1
        border[pixels[(height - 1) * width + x]] += 1
    for y in range(height):
        border[pixels[y * width]] += 1
        border[pixels[y * width + (width - 1)]] += 1
    return min(border.items(), key=lambda item: (-item[1], item[0]))[0]


def _foreground_mask(frame: Image.Image, bg: tuple[int, int, int]) -> bytearray:
    """배경색과 BG_DIFF_THRESHOLD 이상 차이나는 전경 픽셀 마스크."""
    bg_r, bg_g, bg_b = bg
    pixels = list(frame.getdata())
    mask = bytearray(len(pixels))
    for idx, (r, g, b) in enumerate(pixels):
        if (
            abs(r - bg_r) > BG_DIFF_THRESHOLD
            or abs(g - bg_g) > BG_DIFF_THRESHOLD
            or abs(b - bg_b) > BG_DIFF_THRESHOLD
        ):
            mask[idx] = 1
    return mask


def _connected_components(
    mask: bytearray, width: int, height: int
) -> list[list[int]]:
    """8방향 연결 전경 덩어리(픽셀 인덱스 목록)를 반환한다."""
    seen = bytearray(len(mask))
    components: list[list[int]] = []
    for start in range(len(mask)):
        if not mask[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        comp: list[int] = []
        while stack:
            idx = stack.pop()
            comp.append(idx)
            x = idx % width
            y = idx // width
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < width and 0 <= ny < height:
                        nb = ny * width + nx
                        if mask[nb] and not seen[nb]:
                            seen[nb] = 1
                            stack.append(nb)
        if len(comp) >= MIN_ELEMENT_PIXELS:
            components.append(comp)
    return components


def _compute_stagger_offsets(
    fg_masks: list[bytearray], width: int, height: int, fps: float
) -> list[float] | None:
    """공간적으로 분리된 요소별 최초 등장 시각 차이(초)를 계산한다."""
    if not fg_masks:
        return None
    union_mask = bytearray(len(fg_masks[0]))
    for mask in fg_masks:
        for idx, val in enumerate(mask):
            if val:
                union_mask[idx] = 1

    components = _connected_components(union_mask, width, height)
    if len(components) < 2:
        return None

    appear_frames: list[int] = []
    for comp in components:
        threshold = max(1, min(MIN_ELEMENT_PIXELS, len(comp) // 2))
        first_frame: int | None = None
        for t, mask in enumerate(fg_masks):
            hits = sum(1 for idx in comp if mask[idx])
            if hits >= threshold:
                first_frame = t
                break
        if first_frame is not None:
            appear_frames.append(first_frame)

    if len(appear_frames) < 2:
        return None

    appear_frames.sort()
    base = appear_frames[0]
    return [round((t - base) / fps, 4) for t in appear_frames]


def _compute_overshoot_ratio(fg_masks: list[bytearray]) -> float | None:
    """전경 면적의 제곱근을 스케일로 보고 최대값/목표값 비율을 계산한다."""
    scales = [math.sqrt(sum(mask)) for mask in fg_masks]
    tail = scales[-min(3, len(scales)) :]
    sorted_tail = sorted(tail)
    target_scale = sorted_tail[len(sorted_tail) // 2]
    if target_scale <= 0:
        return None
    max_scale = max(scales)
    return round(max_scale / target_scale, 4)


def _compute_audio_offset(
    audio_path: Path | None, motion_peak_sec: float | None
) -> float | None:
    """WAV 오디오의 최대 에너지 급등(트랜지언트)과 모션 피크 시간차를 구한다."""
    if audio_path is None or motion_peak_sec is None or not audio_path.exists():
        return None

    try:
        with wave.open(str(audio_path), "rb") as wf:
            sample_rate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            n_channels = wf.getnchannels()
            n_frames = wf.getnframes()
            if sample_rate <= 0 or sampwidth != 2 or n_channels != 1 or n_frames == 0:
                return None
            raw = wf.readframes(n_frames)
    except (OSError, wave.Error):
        return None

    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    win_samples = max(1, int(sample_rate * AUDIO_WINDOW_MS / 1000))
    rms_values: list[float] = []
    for start in range(0, len(samples), win_samples):
        chunk = samples[start : start + win_samples]
        if not chunk:
            continue
        rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
        rms_values.append(rms)

    if len(rms_values) < 2:
        return None

    onset_idx = max(
        range(1, len(rms_values)),
        key=lambda i: rms_values[i] - rms_values[i - 1],
    )
    if rms_values[onset_idx] - rms_values[onset_idx - 1] <= 0:
        return None

    audio_transient_sec = (onset_idx * win_samples) / sample_rate
    return round(audio_transient_sec - motion_peak_sec, 4)
