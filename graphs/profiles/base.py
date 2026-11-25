"""profiles — 오케스트레이터 하나, 납품 형식 여럿 (GRAPH-17).

`entry_pack` 그래프는 공통 파이프라인만 소유한다 — 브리프→IR→방향→변주→렌더→
린트→게이트→선택. "그 다음에 무엇을 만드나"는 profile이 정한다.

- `web`   : 콘테스트 제출용 SVG/PNG/JPG (기존 entry_pack 동작 그대로)
- `print` : 라벨·파우치 전개도 → 인쇄 PDF 게이트 → 납품 팩

새 형식은 이 디렉터리에 파일 하나를 추가하고 `register()`하면 끝난다.
`graphs/core`는 건드리지 않는다.

profile 해석 순서: `spec["profile"]` → `spec["contest_type"]` → `"web"`.

사용::

    from graphs.profiles import profile_for_spec

    profile = profile_for_spec(spec)
    variants = profile.plan({**ir, "directions": directions}, spec, styles)
    render = profile.render(variant, ctx)
    entry = profile.pack(renders, spec, {**state, "profile_ctx": ctx})

`ProfileContext`는 실행 훅(`run`·`convert_png`·`to_jpg`)과 경로를 담는다.
그래프가 자기 모듈의 얇은 래퍼를 주입하면 테스트가 그 래퍼만 monkeypatch해도
profile까지 그대로 전파된다. `state["profile_ctx"]`가 없으면 core 기본값을 쓴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from pydantic import BaseModel, Field

from graphs.core import artifacts as core_artifacts
from graphs.core import env as core_env
from graphs.core import exec as core_exec

DEFAULT_PROFILE = "web"

#: profile이 없을 때 안내에 쓰는 문구.
SPEC_HINT = "spec['profile'] 또는 spec['contest_type']로 지정한다"


class ConceptDirection(BaseModel):
    """시각 방향 하나 — 마크·레이아웃을 그릴 수 있을 만큼 구체적으로."""

    title: str = Field(
        description="방향 이름 (예: '미니멀 모노그램', '네거티브 스페이스 동물')"
    )
    description: str = Field(description="1~2문장 설명")
    motifs: List[str] = Field(
        default_factory=list,
        description="이 방향에서 쓸 형태 모티프 2~4개 (예: '겹친 이니셜', '원 안의 여백', '활자 압인')",
    )
    visual_language: str = Field(
        default="",
        description="선·면·질감·비례에 대한 구체적 지시 (편집 도구에서 그릴 수 있을 만큼)",
    )


class ConceptOutput(BaseModel):
    """LLM 구조화 출력 — 서로 다른 시각 방향 3~5개."""

    directions: List[ConceptDirection] = Field(
        description="3~5개의 서로 다른 시각 방향"
    )


class ProfileError(ValueError):
    """profile을 찾지 못했거나, profile 입력이 계약을 어겼을 때."""


@dataclass(frozen=True)
class Gate:
    """profile이 요구하는 검사 한 건. `id`는 meta·manifest에 그대로 남는다."""

    id: str
    description: str
    required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "required": self.required,
        }


@runtime_checkable
class Profile(Protocol):
    """납품 형식 하나. `id`는 소문자 슬러그(예: `web`, `print`)."""

    id: str

    def defaults(self) -> Dict[str, Any]:
        """이 profile의 spec 기본값. 그래프 기본값 위에 덮어쓴다."""

    def plan(
        self,
        ir: Mapping[str, Any],
        spec: Mapping[str, Any],
        styles: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        """IR·spec·스타일 레지스트리 → 후보 목록(각 후보는 `id`를 가진다)."""

    def render(
        self, variant: Mapping[str, Any], ctx: "ProfileContext"
    ) -> Optional[Dict[str, Any]]:
        """후보 하나를 렌더한다. 실패·건너뜀이면 `None`."""

    def gates(self) -> List[Gate]:
        """납품 전에 반드시 통과해야 하는 검사 목록.

        비어 있으면 그래프의 critic/gate(web 린트)가 판정한다. 비어 있지 않으면
        그래프는 web 린트를 건너뛰고 이 profile의 게이트를 신뢰한다(pack에서 실행).
        """

    def pack(
        self,
        items: Sequence[Mapping[str, Any]],
        spec: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """선택된 후보를 납품 팩으로 묶는다. `dir`·`items`·`meta`를 담아 돌려준다."""

    def mockups(
        self, items: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        """납품 팩의 평면 목업. `spec['mockup_dir']`에 쓴다."""


# ---------------------------------------------------------------------------
# 실행 훅 — 테스트가 monkeypatch할 수 있는 유일한 지점
# ---------------------------------------------------------------------------


def default_run(
    argv: Sequence[Any], timeout: Optional[float] = 300, **kwargs: Any
) -> str:
    """core/exec로 실행하고 stdout만 돌려준다(그래프들의 `_run` 관례)."""
    kwargs.setdefault("cwd", core_env.ASSETS_DIR)
    result = core_exec.run([str(arg) for arg in argv], timeout=timeout, **kwargs)
    return result.stdout


def svg_size(path: Path) -> Tuple[float, float]:
    """SVG 머리말의 width/height(px). 없으면 1200×900으로 본다."""
    head = Path(path).read_text(encoding="utf-8")[:400]
    width = re.search(r'<svg[^>]*\bwidth="([0-9.]+)', head)
    height = re.search(r'<svg[^>]*\bheight="([0-9.]+)', head)
    return (
        float(width.group(1)) if width else 1200.0,
        float(height.group(1)) if height else 900.0,
    )


def svg_width(path: Path) -> float:
    return svg_size(path)[0]


def convert_png(
    svg: Path,
    out: Path,
    width: int,
    max_height: int = 3000,
    *,
    run: Optional[Callable[..., str]] = None,
    vectorcraft: Optional[str] = None,
) -> None:
    """목표 폭·최대 높이 안에 들어가게 스케일을 정해 PNG로 바꾼다."""
    runner = run or default_run
    native_w, native_h = svg_size(svg)
    native_w = native_w or 1200.0
    native_h = native_h or 900.0
    scale = min(float(width) / native_w, float(max_height) / native_h)
    scale = max(0.1, min(8.0, scale))
    runner(
        [
            vectorcraft or core_env.VECTORCRAFT,
            "convert",
            str(svg),
            str(out),
            "--scale",
            round(scale, 4),
        ],
        timeout=120,
    )


def to_jpg(png: Path, out: Path, quality: int = 90) -> None:
    """JPG는 알파를 흰 배경으로 합성한다(투명 영역이 검게 나오지 않게)."""
    from PIL import Image

    with Image.open(png) as image:
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            base = Image.new("RGB", rgba.size, (255, 255, 255))
            base.paste(rgba, mask=rgba.split()[-1])
            rgb = base
        else:
            rgb = image.convert("RGB")
        rgb.save(out, "JPEG", quality=quality, optimize=True)


@dataclass
class ProfileContext:
    """한 실행의 훅과 경로. `run`은 stdout 문자열을 돌려준다."""

    run: Callable[..., str] = default_run
    convert_png: Optional[Callable[..., None]] = None
    to_jpg: Optional[Callable[..., None]] = None
    out_root: Path = field(default_factory=lambda: Path(core_env.OUT_ROOT))
    assets_dir: Path = field(default_factory=lambda: Path(core_env.ASSETS_DIR))
    catalog: Path = field(default_factory=lambda: Path(core_env.CATALOG))
    tokens: Path = field(default_factory=lambda: Path(core_env.TOKENS))
    run_dir: Optional[Path] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def convert(self, svg: Path, out: Path, width: int, max_height: int = 3000) -> None:
        """주입된 훅이 있으면 그대로, 없으면 core 기본 변환기를 쓴다."""
        if self.convert_png is not None:
            return self.convert_png(svg, out, width, max_height)
        return convert_png(svg, out, width, max_height, run=self.run)

    def jpeg(self, png: Path, out: Path, quality: int = 90) -> None:
        hook = self.to_jpg or to_jpg
        return hook(png, out, quality)


def context_from_state(state: Mapping[str, Any]) -> ProfileContext:
    """`state['profile_ctx']`가 있으면 그것을, 없으면 core 기본값을 쓴다."""
    ctx = state.get("profile_ctx")
    if isinstance(ctx, ProfileContext):
        return ctx
    return ProfileContext()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, Profile] = {}


def register(profile: Profile) -> Profile:
    """profile 하나를 등록한다. 같은 id는 마지막 것이 이긴다(확장 지점)."""
    key = str(getattr(profile, "id", "") or "").strip().lower()
    if not key:
        raise ProfileError("profile.id가 비어 있다")
    _REGISTRY[key] = profile
    return profile


def known_profiles() -> List[str]:
    _ensure_builtin()
    return sorted(_REGISTRY)


def _ensure_builtin() -> None:
    """내장 profile을 지연 등록한다(순환 import 회피)."""
    if _REGISTRY:
        return
    from graphs.profiles import brand as brand_profile
    from graphs.profiles import print as print_profile
    from graphs.profiles import web as web_profile

    register(web_profile.WebProfile())
    register(print_profile.PrintProfile())
    register(brand_profile.BrandProfile())


def resolve_profile_name(spec: Optional[Mapping[str, Any]]) -> str:
    """`spec['profile']` → `spec['contest_type']` → `web` 순으로 이름을 고른다."""
    spec = spec or {}
    for key in ("profile", "contest_type"):
        value = spec.get(key)
        if value:
            return str(value).strip().lower()
    return DEFAULT_PROFILE


def get_profile(name: str) -> Profile:
    """이름으로 profile을 찾는다. 모르는 이름은 사용 가능 목록과 함께 실패한다."""
    _ensure_builtin()
    key = str(name or "").strip().lower()
    try:
        return _REGISTRY[key]
    except KeyError:
        raise ProfileError(
            f"알 수 없는 profile: {name!r}. 사용 가능: {', '.join(known_profiles())}. {SPEC_HINT}"
        ) from None


def profile_for_spec(spec: Optional[Mapping[str, Any]]) -> Profile:
    return get_profile(resolve_profile_name(spec))


# ---------------------------------------------------------------------------
# manifest — profile 공통 기록
# ---------------------------------------------------------------------------


def artifact_entry(path: Any, root: Path) -> Optional[Dict[str, Any]]:
    """manifest 항목 하나. 실행 루트 밖 파일은 절대 경로로 남긴다."""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        return core_artifacts.artifact_entry(target, root=root)
    except ValueError:  # root 밖(예: 외부 브리프 파일)
        return core_artifacts.artifact_entry(target)


def collect_artifacts(paths: Sequence[Any], root: Path) -> List[Dict[str, Any]]:
    """경로 목록 → manifest 산출물 항목(중복 제거, 순서 유지)."""
    rows: List[Dict[str, Any]] = []
    seen = set()
    for path in paths:
        entry = artifact_entry(path, root)
        if entry is None or entry["path"] in seen:
            continue
        seen.add(entry["path"])
        rows.append(entry)
    return rows


def run_manifest(
    state: Mapping[str, Any],
    run_dir: Path,
    artifacts: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    status: str = "ok",
    gates: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """입력 지문(브리프 해시·시드·spec 해시)과 산출물 해시를 담은 manifest."""
    spec = state.get("spec") or {}
    brief = str(state.get("content") or state.get("brief") or "")
    return core_artifacts.build_manifest(
        run_id=Path(run_dir).name,
        kind=kind,
        status=status,
        inputs={
            "brief_sha256": core_artifacts.sha256_bytes(brief.encode("utf-8")),
            "seed": str(spec.get("seed") or ""),
            "spec_sha256": core_artifacts.sha256_json(spec),
        },
        artifacts=artifacts,
        gates=gates,
        meta={
            "platform": spec.get("platform"),
            "media": spec.get("media"),
            "variants": len(state.get("variants") or []),
            "selection": list(state.get("selection") or []),
        },
    )
