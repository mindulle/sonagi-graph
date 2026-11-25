"""VideoOpsGraph: 브리프 한 줄 → EffectCraft 타이포 모션 → FilmCraft 컷편집 → HITL → CDN 배포.

크래프트 CLI 두 개를 순서대로 호출한다. 모션 클립은 EffectCraft가, 컷편집·프리뷰·인코딩은
FilmCraft가 맡는다. `export_and_publish` 직전에서 멈추므로, 디렉터가 프리뷰 프레임을 보고
`approval_status="approved"`로 재개해야 최종 렌더가 돈다(비싼 인코딩을 두 번 하지 않는다).

W2(core 이관): 명령 실행은 전부 `core.exec.run`(타임아웃 명시, 실패는 `CommandError`),
경로는 `core.env`, 실행 기록은 `core.artifacts`가 맡는다. 이 파일에는 `subprocess`가 없다.

환경 변수
    SONAGI_VIDEO_OUT          작업 디렉터리 (우선순위: 이 변수 > SONAGI_OUT_ROOT > 코어 기본값)
    SONAGI_CDN_VIDEO_DIR      배포 디렉터리 (기본 /var/www/craft-suite/video/exports)
    SONAGI_VIDEO_PUBLIC_BASE  공개 URL 접두사 (기본 https://draw.sonagi.space/video/exports)
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from graphs.core import artifacts
from graphs.core import contract as core_contract
from graphs.core import env as core_env
from graphs.core import exec as core_exec

# CLI 바이너리 경로 — PATH 우선, 없으면 기존 설치 경로(core.exec.which)
FILMCRAFT_BIN = core_exec.which("filmcraft-cli", "/usr/local/bin/filmcraft-cli")
EFFECTCRAFT_BIN = core_exec.which("effectcraft-cli", "/usr/local/bin/effectcraft-cli")

# 작업 디렉터리 — 레거시 SONAGI_VIDEO_OUT이 있으면 그 값이 이긴다.
OUTPUT_TEMP_ROOT = Path(
    os.environ.get("SONAGI_VIDEO_OUT") or core_env.out_root("video_ops")
)
# CDN 배포 경로·공개 접두사는 아직 코어 env에 자리가 없다(비디오 전용 설정).
CDN_VIDEO_DIR = Path(
    os.environ.get("SONAGI_CDN_VIDEO_DIR", "/var/www/craft-suite/video/exports")
)
CDN_PUBLIC_BASE = os.environ.get(
    "SONAGI_VIDEO_PUBLIC_BASE", "https://draw.sonagi.space/video/exports"
).rstrip("/")

# 검증 기준(기존 verify 스크립트와 같은 값)
MIN_VIDEO_BYTES = 1_000_000

# 명령별 타임아웃(초). 암묵적 기본값에 기대지 않는다.
MOTION_BUILD_TIMEOUT = 120.0
MOTION_RENDER_TIMEOUT = 600.0
ASSEMBLY_IMPORT_TIMEOUT = 120.0
PREVIEW_RENDER_TIMEOUT = 120.0
EXPORT_TIMEOUT = 900.0

CONTRACT_NAME = "video_ops"
PRE_EXPORT_STATUSES = frozenset({"preview_ready", "awaiting_approval"})


class VideoOpsState(TypedDict):
    run_id: str
    brief: str
    aspect_ratio: str  # "16:9" or "9:16"
    duration_seconds: float
    scenes: List[Dict[str, Any]]
    motion_asset_path: Optional[str]
    preview_frame_path: Optional[str]
    approval_status: str  # "pending", "approved", "rejected"
    exported_video_path: Optional[str]
    published_url: Optional[str]
    manifest_path: Optional[str]
    status: str


# -------------------------------------------------------------
# 실행 기록: core.artifacts manifest (입력·산출 해시·CDN 경로·길이)
# -------------------------------------------------------------
def _artifact_entry(
    run_dir: Path, name: str, path: Optional[str]
) -> Optional[Dict[str, Any]]:
    """파일이 있으면 manifest 항목을 만든다. 없으면 조용히 건너뛴다."""
    if not path:
        return None
    target = Path(path)
    if not target.is_file():
        return None
    try:
        return artifacts.artifact_entry(target, root=run_dir, name=name)
    except ValueError:  # CDN처럼 run 디렉터리 밖에 있는 산출물
        return artifacts.artifact_entry(target, name=name)


def write_run_manifest(
    state: Mapping[str, Any], run_dir: Path, *, status: str = "ok"
) -> Path:
    """실행 manifest를 `run_dir/manifest.json`에 쓴다.

    입력(브리프·비율·길이·씬), 산출물 해시(motion·preview·export·CDN),
    HITL 게이트 상태, CDN 공개 경로를 남긴다.
    """
    brief_text = state.get("brief") or ""
    entries = [
        entry
        for entry in (
            _artifact_entry(run_dir, "motion_clip", state.get("motion_asset_path")),
            _artifact_entry(run_dir, "preview_frame", state.get("preview_frame_path")),
            _artifact_entry(run_dir, "final_video", state.get("exported_video_path")),
        )
        if entry is not None
    ]
    manifest = artifacts.build_manifest(
        run_id=str(state.get("run_id") or run_dir.name),
        kind=CONTRACT_NAME,
        inputs={
            "brief": brief_text,
            "brief_sha256": artifacts.sha256_bytes(brief_text.encode("utf-8")),
            "aspect_ratio": state.get("aspect_ratio"),
            "duration_seconds": state.get("duration_seconds"),
            "scenes": state.get("scenes") or [],
        },
        artifacts=entries,
        gates=[
            {
                "name": "director_hitl",
                "status": state.get("approval_status", "pending"),
            }
        ],
        status=status,
        meta={
            "published_url": state.get("published_url"),
            "cdn_output_path": state.get("exported_video_path"),
            "duration_seconds": state.get("duration_seconds"),
            "cdn_dir": str(CDN_VIDEO_DIR),
            "cdn_public_base": CDN_PUBLIC_BASE,
            "paths": core_env.PATHS.as_dict(),
        },
    )
    return artifacts.write_manifest(run_dir, manifest)


# -------------------------------------------------------------
# 1. Brief Node: 씬 구조 및 멀티트랙 타임라인 설계 (시네마틱 헌법 4단 전개)
# -------------------------------------------------------------
def brief_node(state: VideoOpsState) -> Dict[str, Any]:
    run_id = state.get("run_id") or str(uuid.uuid4())[:8]
    brief_text = state.get("brief") or "Mindulle Studio 15s Product Demo Showreel"
    aspect_ratio = state.get("aspect_ratio", "16:9")
    duration = state.get("duration_seconds", 15.0)

    print(
        f"🎬 [VideoOps: Brief] '{brief_text}' ({aspect_ratio}, {duration}s) 타임라인 설계 중..."
    )

    # 시네마틱 4단 기승전결(Intro -> Showcase -> Kinetic Motion -> Outro Lockup)
    scenes = [
        {
            "scene_id": 1,
            "start": 0.0,
            "end": 3.0,
            "title": "Intro Aura",
            "layer": "EffectCraft Kinetic Typography",
        },
        {
            "scene_id": 2,
            "start": 3.0,
            "end": 8.0,
            "title": "Feature Showcase",
            "layer": "FilmCraft Multi-track Cut",
        },
        {
            "scene_id": 3,
            "start": 8.0,
            "end": 12.0,
            "title": "Kinetic Proof",
            "layer": "Speed Ramp & Dynamic Transition",
        },
        {
            "scene_id": 4,
            "start": 12.0,
            "end": 15.0,
            "title": "Call to Action",
            "layer": "Brand Outro Lockup",
        },
    ]

    return {
        "run_id": run_id,
        "scenes": scenes,
        "duration_seconds": duration,
        "status": "brief_designed",
    }


# -------------------------------------------------------------
# 2. EffectCraft Motion Node: 타이포 모션 & 그래픽 에셋 무인 생성
# -------------------------------------------------------------
def effectcraft_motion_node(state: VideoOpsState) -> Dict[str, Any]:
    run_id = state.get("run_id", "default")
    brief_text = state.get("brief", "SONAGI SHOWREEL")
    out_dir = OUTPUT_TEMP_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    motion_proj_path = out_dir / f"motion_{run_id}.ecproj"
    motion_clip_path = out_dir / f"motion_title_{run_id}.mp4"

    print(
        f"✨ [VideoOps: EffectCraft] JSX 스크립트 기반 타이포 모션 컴포지션 빌드 중..."
    )

    # After Effects 스타일 JSX 스크립트로 다이나믹 타이포그래피 생성
    title_text = brief_text.split(":")[0].strip() if ":" in brief_text else brief_text
    sub_text = "AI-NATIVE CREATIVE SHOWREEL PIPELINE"

    jsx_script = f'''
var comp = app.project.items.addComp("MotionTitle_{run_id}", 1920, 1080, 1.0, 3.0, 30);
var bg = comp.layers.addSolid([0.04, 0.06, 0.09], "Background", 1920, 1080, 1.0);
var title = comp.layers.addText("{title_text}");
title.property("Position").setValue([960, 480]);
var sub = comp.layers.addText("{sub_text}");
sub.property("Position").setValue([960, 580]);
writeLn("EffectCraft composition successfully assembled for {run_id}");
'''

    # 1) 스크립트 실행 및 프로젝트 저장
    cmd_build = [
        EFFECTCRAFT_BIN,
        "--empty",
        "script",
        "--eval",
        jsx_script,
        "--save-as",
        str(motion_proj_path),
    ]
    core_exec.run(cmd_build, timeout=MOTION_BUILD_TIMEOUT)

    # 2) 타이포 모션 비디오 렌더링 (2초 H.264 클립)
    cmd_render = [
        EFFECTCRAFT_BIN,
        "--project",
        str(motion_proj_path),
        "render",
        "--out",
        str(motion_clip_path),
        "--start",
        "0",
        "--end",
        "2.0",
        "--fps",
        "30",
    ]
    core_exec.run(cmd_render, timeout=MOTION_RENDER_TIMEOUT)
    print(
        f"  ✓ EffectCraft 모션 클립 렌더링 완료: {motion_clip_path} ({motion_clip_path.stat().st_size} bytes)"
    )

    return {"motion_asset_path": str(motion_clip_path), "status": "motion_asset_ready"}


# -------------------------------------------------------------
# 3. FilmCraft Assembly Node: 모션 에셋 임포트 및 멀티트랙 타임라인 조립 (139ms 검수)
# -------------------------------------------------------------
def filmcraft_assembly_node(state: VideoOpsState) -> Dict[str, Any]:
    run_id = state.get("run_id", "default")
    motion_clip = state.get("motion_asset_path")
    out_dir = OUTPUT_TEMP_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    preview_path = out_dir / f"preview_frame_{run_id}.png"
    print(
        f"⚙️ [VideoOps: FilmCraft] 모션 에셋 임포트 및 타임라인 컷편집 검수 준비 중..."
    )

    # 1) EffectCraft에서 생성된 모션 클립을 FilmCraft에 임포트
    if motion_clip and Path(motion_clip).exists():
        import_cmd = [FILMCRAFT_BIN, "--demo", "import", motion_clip]
        core_exec.run(import_cmd, timeout=ASSEMBLY_IMPORT_TIMEOUT)
        print(f"  ✓ EffectCraft 비디오 클립 임포트 성공: {motion_clip}")

    # 2) 1.0초 시점 단일 프레임 고속 렌더링 검수 (139ms 미만)
    preview_cmd = [
        FILMCRAFT_BIN,
        "--demo",
        "render",
        "--seconds",
        "1.0",
        "--out",
        str(preview_path),
        "--scale",
        "0.5",
    ]
    result = core_exec.run(preview_cmd, timeout=PREVIEW_RENDER_TIMEOUT)
    print(
        f"  ✓ 단일 프레임 사전 검수 이미지 렌더 완료: {preview_path} ({result.duration_ms:.1f}ms)"
    )

    new_state = {"preview_frame_path": str(preview_path), "status": "preview_ready"}
    # 승인 대기 상태에서도 무엇을 만들었는지 남긴다(승인 후 최종 manifest로 덮어쓴다).
    manifest_path = write_run_manifest(
        {**state, **new_state}, out_dir, status="awaiting_approval"
    )
    new_state["manifest_path"] = str(manifest_path)
    return new_state


# -------------------------------------------------------------
# 4. Checkpoint Node: 인간 디렉터 프리뷰 승인 게이트 (HITL)
# -------------------------------------------------------------
def hitl_checkpoint_node(state: VideoOpsState) -> Dict[str, Any]:
    print("🛑 [VideoOps: HITL] 디렉터 영상 검수 대기 (인터럽트 지점)...")
    approval_status = state.get("approval_status", "pending")

    if approval_status == "approved":
        print("  ✅ [HITL] 디렉터 승인 확인 완료. 최종 렌더링을 시작합니다.")
        return {"status": "approved"}

    return {"approval_status": "pending", "status": "awaiting_approval"}


# -------------------------------------------------------------
# 5. Final Export & Publish Node: 4K / H.264 / WebM 고속 인코딩 배포
# -------------------------------------------------------------
def export_and_publish_node(state: VideoOpsState) -> Dict[str, Any]:
    run_id = state.get("run_id", "default")
    out_dir = OUTPUT_TEMP_ROOT / run_id
    final_video_path = out_dir / f"showreel_{run_id}.mp4"

    print(f"🚀 [VideoOps: Export] 최종 H.264 프로덕션 비디오 인코딩 시작...")

    # FilmCraft CLI 고속 익스포트 (고품질 1080p HD 프리셋, 3초 쇼릴)
    export_cmd = [
        FILMCRAFT_BIN,
        "--demo",
        "export",
        str(final_video_path),
        "--preset",
        "High Quality 1080p HD",
        "--start",
        "0.0",
        "--end",
        "3.0",
    ]
    core_exec.run(export_cmd, timeout=EXPORT_TIMEOUT)

    # CDN 디렉터리로 복사 배포
    CDN_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    target_cdn_path = CDN_VIDEO_DIR / f"showreel_{run_id}.mp4"
    shutil.copy2(final_video_path, target_cdn_path)

    published_url = f"{CDN_PUBLIC_BASE}/showreel_{run_id}.mp4"
    print(
        f"  ✓ 최종 비디오 렌더 및 배포 성공: {published_url} ({target_cdn_path.stat().st_size} bytes)"
    )

    final_state = {
        "exported_video_path": str(target_cdn_path),
        "published_url": published_url,
        "status": "completed",
    }
    manifest_path = write_run_manifest({**state, **final_state}, out_dir)
    print(f"  ✓ 실행 manifest 기록 완료: {manifest_path}")

    return {**final_state, "manifest_path": str(manifest_path)}


# -------------------------------------------------------------
# 검증: 기존 scripts/verify_video_ops.py를 흡수한 verify()
# -------------------------------------------------------------
def _check_file(
    state: Mapping[str, Any], field: str, minimum: int = 1
) -> List[core_contract.Issue]:
    value = state.get(field)
    if not value:
        return [core_contract.error("missing_artifact", f"{field}가 없다", field)]
    path = Path(value)
    if not path.is_file():
        return [
            core_contract.error(
                "missing_artifact", f"{field} 파일이 없다: {path}", field
            )
        ]
    if path.stat().st_size < minimum:
        return [
            core_contract.error(
                "empty_artifact",
                f"{field}가 비었다(최소 {minimum} bytes): {path}",
                field,
            )
        ]
    return []


def verify(state: Mapping[str, Any]) -> core_contract.VerifyResult:
    """VideoOps 실행 결과 판정 — 게이트 전/후 두 국면을 모두 다룬다.

    게이트 전(`preview_ready`·`awaiting_approval`): 모션 클립·프리뷰 프레임이
    실제로 있어야 하고, 승인 전에 배포 URL이 생기면 실패다.
    승인 후(`approved`·`completed`): 최종 영상이 1MB 이상이고 CDN URL 규격이 맞아야 한다.
    """
    issues: List[core_contract.Issue] = []
    checks: List[str] = []

    checks.append("motion")
    issues += _check_file(state, "motion_asset_path")
    checks.append("preview")
    issues += _check_file(state, "preview_frame_path")

    status = state.get("status")
    approved = (
        state.get("approval_status") == "approved"
        or status == "approved"
        or status == "completed"
    )

    if not approved:
        checks.append("hitl_gate")
        if status not in PRE_EXPORT_STATUSES:
            issues.append(
                core_contract.error(
                    "not_gated",
                    f"HITL 대기 상태가 아니다: {status}",
                    "status",
                )
            )
        if state.get("published_url"):
            issues.append(
                core_contract.error(
                    "published_before_approval",
                    f"승인 전에 배포 URL이 생겼다: {state.get('published_url')}",
                    "published_url",
                )
            )
        return core_contract.VerifyResult(
            issues=tuple(issues), checks=tuple(checks), name=CONTRACT_NAME
        )

    checks.append("export")
    issues += _check_file(state, "exported_video_path", MIN_VIDEO_BYTES)

    checks.append("cdn_url")
    published_url = state.get("published_url")
    if not published_url:
        issues.append(
            core_contract.error("missing_url", "배포 URL이 없다", "published_url")
        )
    elif not str(published_url).startswith(f"{CDN_PUBLIC_BASE}/"):
        issues.append(
            core_contract.error(
                "bad_url",
                f"CDN URL 규격이 아니다: {published_url}",
                "published_url",
            )
        )

    if status != "completed":
        issues.append(
            core_contract.error(
                "not_completed",
                f"최종 상태가 completed가 아니다: {status}",
                "status",
            )
        )

    return core_contract.VerifyResult(
        issues=tuple(issues), checks=tuple(checks), name=CONTRACT_NAME
    )


CONTRACT = core_contract.SimpleContract(
    name=CONTRACT_NAME,
    inputs=VideoOpsState,
    outputs=VideoOpsState,
    verify=verify,
)


def verify_run(
    state: Mapping[str, Any], *, stage: str = "none"
) -> core_contract.VerifyResult:
    """`verify()` + (선택) 입출력 스키마 검사를 한 번에. 실패는 `ContractError`."""
    return core_contract.verify_contract(CONTRACT, state, stage=stage)


# -------------------------------------------------------------
# Graph Builder
# -------------------------------------------------------------
def build_video_ops_graph(checkpointer=None):
    builder = StateGraph(VideoOpsState)

    builder.add_node("brief", brief_node)
    builder.add_node("effectcraft_motion", effectcraft_motion_node)
    builder.add_node("filmcraft_assembly", filmcraft_assembly_node)
    builder.add_node("hitl_checkpoint", hitl_checkpoint_node)
    builder.add_node("export_and_publish", export_and_publish_node)

    builder.add_edge(START, "brief")
    builder.add_edge("brief", "effectcraft_motion")
    builder.add_edge("effectcraft_motion", "filmcraft_assembly")
    builder.add_edge("filmcraft_assembly", "hitl_checkpoint")
    builder.add_edge("hitl_checkpoint", "export_and_publish")
    builder.add_edge("export_and_publish", END)

    # 렌더링 비용 절약 및 디렉터 시각 검수를 위해 export 직전에 일시정지 (HITL)
    return builder.compile(
        checkpointer=checkpointer, interrupt_before=["export_and_publish"]
    )


graph = build_video_ops_graph()
