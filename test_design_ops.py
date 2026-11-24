import os
import sys
import uuid
import json
import sqlite3
from pathlib import Path
from pprint import pprint

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from graphs.design_ops_graph import build_graph


def test_full_design_ops_pipeline():
    print(
        "=== 🚀 [CEO-1183] sonagi-graph ArtCraft 디자인 HITL 파이프라인 실전 검증 ==="
    )

    graph = build_graph()
    thread_id = f"design_ops_test_{uuid.uuid4().hex[:6]}"
    config = {"configurable": {"thread_id": thread_id}}

    initial_input = {
        "brief": "Mindulle Studio CI [유리 문진 속 민들레 홀씨] 공식 인장 정립 (Cormorant Garamond + 8각 파셋 크리스탈 + 2px 정밀 선화)",
        "run_id": f"run_{uuid.uuid4().hex[:6]}",
        "selected_draft_index": 0,
        "approval_status": "pending",
        "drafts": [],
        "messages": [],
    }

    print(
        "\n[단계 1] 그래프 1차 실행: Brief 바인딩 ➔ VectorCraft 다중 시안 생성 ➔ HITL 게이트 진입"
    )
    for event in graph.stream(initial_input, config=config):
        for node_name, node_state in event.items():
            print(f"👉 노드 완료: [{node_name}]")

    # 인터럽트 상태 검증
    state = graph.get_state(config)
    print("\n[현재 체크포인트 상태]")
    print(f"- 다음 실행 대기 노드 (next): {state.next}")
    assert "publish" in state.next, (
        "그래프가 publish 노드 직전에 정상적으로 중단되어야 합니다."
    )

    drafts = state.values.get("drafts", [])
    print(f"\n[생성된 시안 검증] 총 {len(drafts)}종 생성됨")
    assert len(drafts) == 3, "Draft A, B, C 3종의 시안이 모두 생성되어야 합니다."

    for d in drafts:
        print(f"  • {d['name']}")
        print(f"    - SVG: {d['svg_path']} ({d['file_sizes']['svg_bytes']} bytes)")
        print(
            f"    - AST (.vectorcraft): {d['vectorcraft_path']} ({d['file_sizes']['vectorcraft_bytes']} bytes)"
        )
        print(f"    - PDF: {d['pdf_path']} ({d['file_sizes']['pdf_bytes']} bytes)")
        print(f"    - PNG: {d['png_path']} ({d['file_sizes']['png_bytes']} bytes)")
        print(f"    - Nodes: {d['nodes_count']} objects")

        # 파일 실존 여부 및 크기 검증
        assert Path(d["svg_path"]).exists() and d["file_sizes"]["svg_bytes"] > 0
        assert (
            Path(d["vectorcraft_path"]).exists()
            and d["file_sizes"]["vectorcraft_bytes"] > 0
        )
        assert Path(d["pdf_path"]).exists() and d["file_sizes"]["pdf_bytes"] > 0
        assert Path(d["png_path"]).exists() and d["file_sizes"]["png_bytes"] > 0

    approval_id = state.values.get("approval_id")
    print(f"\n[HITL Approval ID]: {approval_id}")
    # 외부 결재 시스템 연동은 폐기되었다. 결재 티켓 없이 수동으로 approval_status를 approved로 바꿔 재개한다.

    print("\n[단계 2] 인간 디렉터 승인 후 그래프 재개 (resume)")
    # 상태 갱신: 승인 완료 및 시안 0번 (Draft A: 8각 파셋형) 확정
    graph.update_state(
        config, {"approval_status": "approved", "selected_draft_index": 0}
    )

    for event in graph.stream(None, config=config):
        for node_name, node_state in event.items():
            print(f"👉 노드 완료: [{node_name}]")

    final_state = graph.get_state(config)
    print("\n[단계 3] 최종 배포 자산 검증")
    published = final_state.values.get("published_assets", {})
    pprint(published)

    # 배포된 포털 파일 검증
    portal_svg = Path(published["portal_path"])
    portal_vc = Path(published["vectorcraft_path"])
    portal_pdf = Path(published["pdf_path"])
    portal_png = Path(published["png_path"])

    assert portal_svg.exists(), "Portal SVG가 존재해야 합니다."
    assert portal_vc.exists(), "Portal VectorCraft 파일이 존재해야 합니다."
    assert portal_pdf.exists(), "Portal PDF가 존재해야 합니다."
    assert portal_png.exists(), "Portal PNG가 존재해야 합니다."

    # sonagi-assets DB 검증
    db_path = "/home/ubuntu/sonagi-assets/asset_hub.db"
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute(
        "SELECT id, name, ext, url FROM items WHERE id = ?", (published["asset_id"],)
    )
    row = c.fetchone()
    conn.close()

    assert row is not None, "sonagi-assets DB에 아이템이 등록되어야 합니다."
    print(f"\n📦 sonagi-assets DB 등록 확인: ID={row[0]}, Name={row[1]}, URL={row[3]}")

    print("\n🎉 모든 파이프라인 테스트 및 검증이 완벽하게 통과했습니다!")


if __name__ == "__main__":
    test_full_design_ops_pipeline()
