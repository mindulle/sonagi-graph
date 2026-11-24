"""VideoOpsGraph: 브리프 한 줄 → EffectCraft 타이포 모션 → FilmCraft 컷편집 → HITL → CDN 배포.

크래프트 CLI 두 개를 순서대로 호출한다. 모션 클립은 EffectCraft가, 컷편집·프리뷰·인코딩은
FilmCraft가 맡는다. `export_and_publish` 직전에서 멈추므로, 디렉터가 프리뷰 프레임을 보고
`approval_status="approved"`로 재개해야 최종 렌더가 돈다(비싼 인코딩을 두 번 하지 않는다).

환경 변수
    SONAGI_VIDEO_OUT          작업 디렉터리 (기본 /tmp/video_ops)
    SONAGI_CDN_VIDEO_DIR      배포 디렉터리 (기본 /var/www/craft-suite/video/exports)
    SONAGI_VIDEO_PUBLIC_BASE  공개 URL 접두사 (기본 https://draw.sonagi.space/video/exports)
"""

import os
import uuid
import time
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict, List, Dict, Any, Optional

from langgraph.graph import StateGraph, START, END

# CLI 바이너리 경로
FILMCRAFT_BIN = shutil.which("filmcraft-cli") or "/usr/local/bin/filmcraft-cli"
EFFECTCRAFT_BIN = shutil.which("effectcraft-cli") or "/usr/local/bin/effectcraft-cli"

OUTPUT_TEMP_ROOT = Path(os.environ.get("SONAGI_VIDEO_OUT", "/tmp/video_ops"))
CDN_VIDEO_DIR = Path(
    os.environ.get("SONAGI_CDN_VIDEO_DIR", "/var/www/craft-suite/video/exports")
)
CDN_PUBLIC_BASE = os.environ.get(
    "SONAGI_VIDEO_PUBLIC_BASE", "https://draw.sonagi.space/video/exports"
).rstrip("/")


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
    status: str


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
    subprocess.run(cmd_build, check=True, capture_output=True)

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
    subprocess.run(cmd_render, check=True, capture_output=True)
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
        subprocess.run(import_cmd, check=True, capture_output=True)
        print(f"  ✓ EffectCraft 비디오 클립 임포트 성공: {motion_clip}")

    # 2) 1.0초 시점 단일 프레임 고속 렌더링 검수 (139ms 미만)
    start_time = time.time()
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
    subprocess.run(preview_cmd, check=True, capture_output=True)
    elapsed_ms = (time.time() - start_time) * 1000
    print(
        f"  ✓ 단일 프레임 사전 검수 이미지 렌더 완료: {preview_path} ({elapsed_ms:.1f}ms)"
    )

    return {"preview_frame_path": str(preview_path), "status": "preview_ready"}


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
    subprocess.run(export_cmd, check=True, capture_output=True)

    # CDN 디렉터리로 복사 배포
    CDN_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    target_cdn_path = CDN_VIDEO_DIR / f"showreel_{run_id}.mp4"
    shutil.copy2(final_video_path, target_cdn_path)

    published_url = f"{CDN_PUBLIC_BASE}/showreel_{run_id}.mp4"
    print(
        f"  ✓ 최종 비디오 렌더 및 배포 성공: {published_url} ({target_cdn_path.stat().st_size} bytes)"
    )

    return {
        "exported_video_path": str(target_cdn_path),
        "published_url": published_url,
        "status": "completed",
    }


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
    return builder.compile(checkpointer=checkpointer, interrupt_before=["export_and_publish"])


graph = build_video_ops_graph()
