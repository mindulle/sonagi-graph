"""모션 특징 추출(GRAPH-52) 테스트 — 합성 PNG 시퀀스 기반, 네트워크 없음.

사용::

    pytest -q test_motion_features.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from graphs.taste import motion_features

EXPECTED_KEYS = {
    "linear_ratio",
    "stagger_offsets",
    "overshoot_ratio",
    "loop_seam_diff",
    "audio_offset",
    "source",
}


def _make_moving_box_frames(
    folder: Path,
    *,
    n_frames: int = 24,
    eased: bool = False,
) -> Path:
    """등속 또는 이징(ease-in-out)으로 이동하는 사각형 PNG 시퀀스를 생성한다."""
    folder.mkdir(parents=True, exist_ok=True)
    for idx in range(n_frames):
        t = idx / max(1, n_frames - 1)
        if eased:
            # 3차 가감속 이징: 시작·끝은 느리고 중간이 빠르다.
            progress = 3.0 * t * t - 2.0 * t * t * t
        else:
            progress = t
        x = int(round(16 + 160 * progress))
        img = Image.new("RGB", (240, 120), (250, 250, 248))
        draw = ImageDraw.Draw(img)
        draw.rectangle((x, 40, x + 31, 71), fill=(20, 20, 20))
        img.save(folder / f"frame_{idx:03d}.png")
    return folder


def test_linear_sequence_has_higher_linear_ratio_than_eased(tmp_path: Path) -> None:
    """테스트 1: 등속 이동 시퀀스의 linear_ratio가 이징 시퀀스보다 유의하게 높다."""
    linear_dir = _make_moving_box_frames(tmp_path / "linear", eased=False)
    eased_dir = _make_moving_box_frames(tmp_path / "eased", eased=True)

    linear_res = motion_features.extract_motion_features(str(linear_dir))
    eased_res = motion_features.extract_motion_features(str(eased_dir))

    assert set(linear_res) == EXPECTED_KEYS
    assert set(eased_res) == EXPECTED_KEYS
    assert linear_res["source"] == "frames"
    assert linear_res["audio_offset"] is None

    # 결정론 검증
    assert linear_res == motion_features.extract_motion_features(str(linear_dir))

    assert linear_res["linear_ratio"] is not None
    assert eased_res["linear_ratio"] is not None
    assert linear_res["linear_ratio"] > eased_res["linear_ratio"] + 0.3


def test_loop_seam_diff_reflects_first_and_last_frame_similarity(
    tmp_path: Path,
) -> None:
    """테스트 2: 첫/끝 프레임이 같으면 loop_seam_diff가 작고, 다른 색이면 크다."""
    same_dir = tmp_path / "loop_same"
    same_dir.mkdir()
    for idx, color in enumerate([(30, 30, 30), (120, 120, 120), (30, 30, 30)]):
        Image.new("RGB", (64, 64), color).save(same_dir / f"f_{idx:02d}.png")

    diff_dir = tmp_path / "loop_diff"
    diff_dir.mkdir()
    for idx, color in enumerate([(0, 0, 0), (120, 120, 120), (255, 255, 255)]):
        Image.new("RGB", (64, 64), color).save(diff_dir / f"f_{idx:02d}.png")

    same_res = motion_features.extract_motion_features(str(same_dir))
    diff_res = motion_features.extract_motion_features(str(diff_dir))

    assert same_res["loop_seam_diff"] is not None
    assert diff_res["loop_seam_diff"] is not None
    assert same_res["loop_seam_diff"] == 0.0
    assert diff_res["loop_seam_diff"] > 200.0
    assert same_res["loop_seam_diff"] < diff_res["loop_seam_diff"]


def test_safe_extract_features_returns_empty_dict_on_missing_path(
    tmp_path: Path,
) -> None:
    """테스트 3: safe_extract_features가 없는 경로에서 빈 dict를 반환한다."""
    missing = tmp_path / "does_not_exist"
    assert motion_features.safe_extract_features(str(missing)) == {}
    assert motion_features.safe_extract_features("") == {}


def test_overshoot_and_stagger_detection(tmp_path: Path) -> None:
    """스태거 등장과 103% 오버슈트를 결정론적으로 검출한다."""
    # 오버슈트 1.03x 테스트 (목표 100px, 피크 103px)
    over_dir = tmp_path / "overshoot"
    over_dir.mkdir()
    for idx, size in enumerate([60, 85, 103, 100, 100, 100]):
        img = Image.new("RGB", (200, 200), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        x0 = 100 - size // 2
        draw.rectangle((x0, x0, x0 + size - 1, x0 + size - 1), fill=(0, 0, 0))
        img.save(over_dir / f"f_{idx:02d}.png")

    over_res = motion_features.extract_motion_features(str(over_dir))
    assert over_res["overshoot_ratio"] == 1.03

    # 스태거 테스트: 두 요소가 0프레임, 3프레임(30fps 기준 0.1초)에 각각 등장
    stag_dir = tmp_path / "stagger"
    stag_dir.mkdir()
    for idx in range(6):
        img = Image.new("RGB", (160, 80), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle((16, 20, 47, 59), fill=(10, 10, 10))
        if idx >= 3:
            draw.rectangle((96, 20, 127, 59), fill=(10, 10, 10))
        img.save(stag_dir / f"f_{idx:02d}.png")

    stag_res = motion_features.extract_motion_features(str(stag_dir))
    assert stag_res["stagger_offsets"] == [0.0, 0.1]
