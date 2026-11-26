"""P4 (GRAPH-17) social 프로필 — 비율·안전영역 게이트·팩 테스트."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphs.profiles import base
from graphs.profiles import social as social_profile


class _StubSocialCtx(base.ProfileContext):
    def __init__(self, run_dir: Path):
        super().__init__(run_dir=run_dir)
        self.extra["content"] = "민들레 소셜 캠페인"

    def convert(self, svg: Path, out: Path, width: int, max_height: int = 3000) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"PNG")

    def jpeg(self, png: Path, out: Path, quality: int = 90) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"JPG")


def test_social_profile_is_registered_and_plans_all_formats():
    prof = base.profile_for_spec({"profile": "social"})
    assert prof.id == "social"
    assert "social" in base.known_profiles()

    variants = prof.plan(
        {"directions": [{"title": "기본", "motifs": []}]},
        {"profile": "social", "themes": ["light"]},
        [{"name": "social-clean"}],
    )
    assert [v["format"] for v in variants] == ["1:1", "4:5", "9:16", "16:9"]


def test_social_render_and_pack_pass_aspect_ratio_and_safe_zone(tmp_path: Path):
    prof = social_profile.SocialProfile()
    ctx = _StubSocialCtx(tmp_path)
    variants = prof.plan(
        {"directions": [{"title": "봄", "motifs": []}]},
        {"profile": "social", "formats": ["1:1", "9:16"], "themes": ["light"]},
        [],
    )
    renders = [prof.render(v, ctx) for v in variants]
    packed = prof.pack(renders, {"profile": "social"}, {"run_dir": str(tmp_path), "profile_context": ctx})

    assert len(packed["items"]) == 2
    assert Path(packed["meta"]).is_file()
    assert Path(packed["entry"]).is_file()
    meta = json.loads(Path(packed["meta"]).read_text(encoding="utf-8"))
    assert all(g["ok"] for row in meta["items"] for g in row["gates"])


def test_social_safe_zone_gate_blocks_ui_intrusion(tmp_path: Path):
    # 9:16 상단 UI 안전영역(top=250)을 침범하는 텍스트(y=80)
    bad_svg = tmp_path / "bad.svg"
    bad_svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1920" width="1080" height="1920">'
        '<g id="content"><text x="120" y="80" font-size="32">침범</text></g></svg>',
        encoding="utf-8",
    )
    res = social_profile.check_safe_zone(bad_svg, "9:16")
    assert res["ok"] is False
    assert "안전영역 침범" in res["detail"]

    prof = social_profile.SocialProfile()
    with pytest.raises(social_profile.SocialGateError):
        prof.pack(
            [{"id": "bad", "format": "9:16", "svg": str(bad_svg)}],
            {"profile": "social"},
            {"run_dir": str(tmp_path)},
        )
