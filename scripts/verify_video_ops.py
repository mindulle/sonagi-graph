import os
import sys
import uuid
from pathlib import Path

# 저장소 루트를 import 경로에 넣는다 (scripts/에서 실행해도 graphs를 찾도록)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graphs.video_ops_graph import build_video_ops_graph
from langgraph.checkpoint.memory import MemorySaver

graph = build_video_ops_graph(checkpointer=MemorySaver())


def test_video_ops_pipeline():
    print(
        "=== 🚀 [GRAPH-1] VideoOpsGraph FilmCraft x EffectCraft 무인 쇼릴 조립 파이프라인 검증 ==="
    )

    run_id = f"test_{uuid.uuid4().hex[:6]}"
    config = {"configurable": {"thread_id": run_id}}

    initial_input = {
        "run_id": run_id,
        "brief": "Sonagi Creative Studio 2026: AI-Native Video Showcase",
        "aspect_ratio": "16:9",
        "duration_seconds": 15.0,
    }

    print(
        "\n[단계 1] 1차 실행: Brief 설계 ➔ EffectCraft 타이포 모션 생성 ➔ FilmCraft 컷편집 타임라인 ➔ HITL 진입"
    )
    for event in graph.stream(initial_input, config=config):
        for node_name, node_state in event.items():
            print(f"👉 노드 완료: [{node_name}]")

    # 인터럽트 및 프리뷰 검증
    state = graph.get_state(config)
    print("\n[현재 체크포인트 상태]")
    print(f"- 다음 실행 대기 노드: {state.next}")
    assert "export_and_publish" in state.next, (
        "그래프가 export_and_publish 노드 직전에 정상 중단(Interrupt)되어야 합니다."
    )

    motion_clip = state.values.get("motion_asset_path")
    preview_path = state.values.get("preview_frame_path")
    print(f"- EffectCraft 모션 클립: {motion_clip}")
    print(f"- FilmCraft 사전 검수 프레임: {preview_path}")

    assert (
        motion_clip
        and Path(motion_clip).exists()
        and Path(motion_clip).stat().st_size > 0
    ), "EffectCraft가 모션 비디오 에셋을 생성해야 합니다."
    assert (
        preview_path
        and Path(preview_path).exists()
        and Path(preview_path).stat().st_size > 0
    ), "FilmCraft가 139ms 단일 검수 프레임을 렌더링해야 합니다."

    print("\n[단계 2] 인간 디렉터 승인 ➔ 최종 렌더링 및 CDN 배포 재개 (Resume)")
    graph.update_state(config, {"approval_status": "approved"})

    for event in graph.stream(None, config=config):
        for node_name, node_state in event.items():
            print(f"👉 노드 완료: [{node_name}]")

    final_state = graph.get_state(config)
    exported_video = final_state.values.get("exported_video_path")
    published_url = final_state.values.get("published_url")
    status = final_state.values.get("status")

    print("\n[최종 배포 결과 검증]")
    print(f"- 최종 상태: {status}")
    print(
        f"- 로컬/CDN 비디오 경로: {exported_video} ({Path(exported_video).stat().st_size} bytes)"
    )
    print(f"- 공개 배포 CDN URL: {published_url}")

    assert status == "completed", "최종 상태는 completed여야 합니다."
    assert (
        exported_video
        and Path(exported_video).exists()
        and Path(exported_video).stat().st_size > 1000000
    ), "최종 영상은 1MB 이상의 완성본이어야 합니다."
    assert published_url.startswith("https://draw.sonagi.space/video/exports/"), (
        "CDN 배포 URL이 올바른 규격이어야 합니다."
    )

    print("\n✅ VideoOpsGraph (GRAPH-1) 파이프라인 검증 성공!")


if __name__ == "__main__":
    test_video_ops_pipeline()
