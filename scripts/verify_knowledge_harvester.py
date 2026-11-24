import os
import sys
import uuid
from pathlib import Path

# 저장소 루트를 import 경로에 넣는다 (scripts/에서 실행해도 graphs를 찾도록)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graphs.knowledge_harvester_graph import build_knowledge_harvester_graph
from langgraph.checkpoint.memory import MemorySaver

graph = build_knowledge_harvester_graph(checkpointer=MemorySaver())

def verify_knowledge_harvester_pipeline():
    print("=== 🚀 [GRAPH-2, 3, 4] KnowledgeHarvesterGraph 확장 파이프라인 검증 ===")
    
    thread_id = f"harvester_test_{uuid.uuid4().hex[:6]}"
    config = {"configurable": {"thread_id": thread_id}}
    
    initial_input = {
        "query": "Bauhaus Architecture & Space Exploration",
        "category": "design"
    }
    
    print("\n[단계 1] 그래프 실행: Open Library ➔ Internet Archive ➔ The Met ➔ Smithsonian ➔ NASA ➔ Public Data ➔ Wiki")
    result = graph.invoke(initial_input, config=config)
    
    print("\n[수집 및 합성 결과 검증]")
    print(f"- 실행 상태: {result.get('status')}")
    assert result.get("status") == "completed", "그래프가 정상적으로 completed 상태로 완료되어야 합니다."
    
    ol_results = result.get("open_library_results", [])
    print(f"- Open Library 도서: {len(ol_results)}권")
    assert len(ol_results) > 0, "Open Library 서지가 수집되어야 합니다."
    
    ia_results = result.get("archive_results", [])
    print(f"- Internet Archive 레코드: {len(ia_results)}건")
    assert len(ia_results) > 0, "Internet Archive 레코드가 수집되어야 합니다."
    
    met_results = result.get("met_museum_results", [])
    print(f"- The Met CC0 예술 도판: {len(met_results)}건")
    for m in met_results:
        print(f"  • {m['title']} ({m['artist']}) - {m['primary_image_small']}")
        assert m.get("primary_image"), "The Met 도판은 고해상도 이미지 URL을 포함해야 합니다."
        
    smith_results = result.get("smithsonian_results", [])
    print(f"- Smithsonian CC0 오픈 액세스 도판: {len(smith_results)}건")
    assert len(smith_results) > 0, "Smithsonian 도판이 수집되어야 합니다."
    for s in smith_results:
        print(f"  • [{s['unit_code']}] {s['title']} - {s['image_url']}")
        assert s.get("image_url"), "스미소니언 에셋은 이미지 URL을 포함해야 합니다."
        
    nasa_results = result.get("nasa_media_results", [])
    print(f"- NASA 미디어 & 비디오 푸티지: {len(nasa_results)}건")
    assert len(nasa_results) > 0, "NASA 미디어가 수집되어야 합니다."
    for n in nasa_results:
        print(f"  • [{n['media_type']}] {n['title']} (NASA ID: {n['nasa_id']})")
        if n.get("video_mp4_url"):
            print(f"    - MP4 푸티지 URL: {n['video_mp4_url']}")
        
    public_results = result.get("public_data_results", [])
    print(f"- 공공데이터 & 거시지표: {len(public_results)}건")
    assert len(public_results) > 0, "공공데이터 지표가 수집되어야 합니다."
        
    wiki_path = result.get("saved_wiki_path")
    print(f"\n[저장된 위키 카탈로그]: {wiki_path}")
    assert wiki_path and Path(wiki_path).exists(), "위키 마크다운 파일이 디스크에 실재해야 합니다."
    assert Path(wiki_path).stat().st_size > 1000, "확장 카탈로그는 1,000바이트 이상의 유의미한 콘텐츠를 담아야 합니다."
    
    print("\n✅ KnowledgeHarvesterGraph 확장 (GRAPH-4: Smithsonian & NASA) 파이프라인 검증 성공!")

if __name__ == "__main__":
    verify_knowledge_harvester_pipeline()
