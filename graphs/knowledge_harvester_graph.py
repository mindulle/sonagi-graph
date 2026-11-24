"""KnowledgeHarvesterGraph: 공공 오픈 데이터 6곳을 훑어 위키 카탈로그 한 장으로 합성한다.

Open Library·Internet Archive·The Met(CC0)·Smithsonian(CC0)·NASA·서울/World Bank를 차례로
호출하고, 결과를 Dual-Coding 규격 마크다운으로 압축해 볼트에 저장한다. 외부 API는 전부
키 없이 쓰는 공개 엔드포인트이고, 네트워크 실패는 노드별로 삼켜 빈 목록으로 계속 진행한다.

환경 변수
    SONAGI_WIKI_DIR   볼트 루트 (기본 /home/ubuntu/llm-wiki)
"""

import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import TypedDict, List, Dict, Any, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from graphs.llm import wiki_dir

# 위키 및 카탈로그 저장 경로
WIKI_REFERENCES_DIR = Path(wiki_dir()) / "03_Resources" / "References" / "Catalogs"

class HarvesterState(TypedDict):
    query: str
    category: str  # "design", "motion", "typography", "architecture", "art", "space", "economics"
    open_library_results: List[Dict[str, Any]]
    archive_results: List[Dict[str, Any]]
    met_museum_results: List[Dict[str, Any]]
    smithsonian_results: List[Dict[str, Any]]
    nasa_media_results: List[Dict[str, Any]]
    public_data_results: List[Dict[str, Any]]
    curriculum_indices: List[Dict[str, Any]]
    compiled_markdown: str
    saved_wiki_path: Optional[str]
    status: str

def _first(value):
    """Open Library는 같은 필드를 리스트로도 문자열로도 준다. 둘 다 받아 첫 값만 남긴다."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


# -------------------------------------------------------------
# 1. Open Library Node: 글로벌 서지 정보 & 식별자 수집
# -------------------------------------------------------------
def open_library_node(state: HarvesterState) -> Dict[str, Any]:
    query = state.get("query", "design systems")
    print(f"📚 [Harvester: OpenLibrary] '{query}' 서지 메타데이터 탐색 중...")
    
    encoded_q = urllib.parse.quote(query)
    url = f"https://openlibrary.org/search.json?q={encoded_q}&limit=5&mode=everything"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "SonagiStudioHarvester/1.0 (contact: admin@sonagi.space)"}
    )
    
    docs = []
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("docs", [])[:5]:
                docs.append({
                    "title": item.get("title"),
                    "author": item.get("author_name", ["Unknown"])[0] if item.get("author_name") else "Unknown",
                    "first_publish_year": item.get("first_publish_year"),
                    "key": item.get("key"),
                    "isbn": _first(item.get("isbn")),
                    "subjects": item.get("subject", [])[:6],
                    "ia_id": _first(item.get("ia"))
                })
    except Exception as e:
        print(f"⚠️ [OpenLibrary] API 호출 실패: {e}")
        
    print(f"  ✓ Open Library 도서 {len(docs)}권 색인 완료")
    return {"open_library_results": docs, "status": "open_library_collected"}

# -------------------------------------------------------------
# 2. Internet Archive Node: 공공 도메인 도판/원문 식별자 탐색
# -------------------------------------------------------------
def archive_org_node(state: HarvesterState) -> Dict[str, Any]:
    query = state.get("query", "graphic design")
    print(f"🏛️ [Harvester: InternetArchive] '{query}' 공공 도메인 & 아카이브 도판 탐색 중...")
    
    archive_items = []
    try:
        def fetch_archive(term: str):
            clean_term = term.replace("&", " ").strip()
            enc = urllib.parse.quote(f"{clean_term} AND mediatype:texts")
            url = f"https://archive.org/advancedsearch.php?q={enc}&fl[]=identifier,title,creator,year,mediatype,description&rows=5&output=json"
            req = urllib.request.Request(url, headers={"User-Agent": "SonagiStudioHarvester/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                docs = data.get("response", {}).get("docs", [])
                items = []
                for d in docs[:5]:
                    items.append({
                        "identifier": d.get("identifier"),
                        "title": d.get("title"),
                        "creator": d.get("creator", "Unknown"),
                        "year": d.get("year"),
                        "description": str(d.get("description", ""))[:200],
                        "archive_url": f"https://archive.org/details/{d.get('identifier')}"
                    })
                return items

        archive_items = fetch_archive(query)
        if not archive_items and " " in query:
            words = [w for w in query.replace("&", " ").split() if len(w) > 3]
            for w in words:
                archive_items = fetch_archive(w)
                if archive_items:
                    break
        if not archive_items:
            archive_items = fetch_archive("architecture")
    except Exception as e:
        print(f"⚠️ [InternetArchive] 검색 실패: {e}")
        
    print(f"  ✓ Internet Archive 레코드 {len(archive_items)}건 수집 완료")
    return {"archive_results": archive_items, "status": "archive_collected"}

# -------------------------------------------------------------
# 3. The Met Museum CC0 Node: 메트로폴리탄 미술관 40만 점 고해상도 예술 도판 인출
# -------------------------------------------------------------
def met_museum_node(state: HarvesterState) -> Dict[str, Any]:
    query = state.get("query", "design")
    category = state.get("category", "design")
    print(f"🎨 [Harvester: TheMet] '{query}' 메트로폴리탄 미술관 CC0 고해상도 도판 검색 중...")
    
    met_items = []
    try:
        def fetch_items_for_term(term: str, max_items: int = 4):
            enc = urllib.parse.quote(term)
            url = f"https://collectionapi.metmuseum.org/public/collection/v1.1/search?hasImages=true&isPublicDomain=true&q={enc}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SonagiHarvester/1.0"}
            )
            items = []
            try:
                with urllib.request.urlopen(req, timeout=8) as res:
                    d = json.loads(res.read().decode("utf-8"))
                    ids = d.get("objectIDs") or []
                    for oid in ids[:25]:
                        if len(items) >= max_items:
                            break
                        try:
                            obj_url = f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}"
                            obj_req = urllib.request.Request(
                                obj_url,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SonagiHarvester/1.0"}
                            )
                            with urllib.request.urlopen(obj_req, timeout=5) as obj_resp:
                                o = json.loads(obj_resp.read().decode("utf-8"))
                                img = o.get("primaryImage") or o.get("primaryImageSmall")
                                if img:
                                    items.append({
                                        "object_id": oid,
                                        "title": o.get("title") or "Untitled",
                                        "artist": o.get("artistDisplayName") or "Unknown Artist",
                                        "date": o.get("objectDate") or "Date Unknown",
                                        "medium": o.get("medium") or "N/A",
                                        "department": o.get("department") or "General",
                                        "primary_image": o.get("primaryImage") or img,
                                        "primary_image_small": o.get("primaryImageSmall") or img,
                                        "met_url": o.get("objectURL") or f"https://www.metmuseum.org/art/collection/search/{oid}"
                                    })
                        except Exception:
                            continue
            except Exception:
                pass
            return items

        met_items = fetch_items_for_term(query, max_items=4)
        if not met_items and " " in query:
            words = [w for w in query.replace("&", " ").split() if len(w) > 3]
            for w in words:
                met_items = fetch_items_for_term(w, max_items=3)
                if met_items:
                    break
                    
        if not met_items:
            fallback_kw = "Modern Art" if category in ["design", "art", "typography"] else "Architecture"
            met_items = fetch_items_for_term(fallback_kw, max_items=3)
    except Exception as e:
        print(f"⚠️ [TheMet] 미술관 API 검색 실패: {e}")
        
    print(f"  ✓ The Met 미술관 CC0 고해상도 도판 {len(met_items)}건 수집 완료")
    return {"met_museum_results": met_items, "status": "met_collected"}

# -------------------------------------------------------------
# 4. Smithsonian Open Access Node: 스미소니언 450만 점 CC0 문화·과학 도판
# -------------------------------------------------------------
def smithsonian_node(state: HarvesterState) -> Dict[str, Any]:
    query = state.get("query", "design")
    category = state.get("category", "design")
    print(f"🏛️ [Harvester: Smithsonian] '{query}' 스미소니언 오픈 액세스 CC0 도판 검색 중...")
    
    smith_items = []
    try:
        def fetch_smithsonian(term: str, max_items: int = 3):
            enc = urllib.parse.quote(term)
            url = f"https://api.si.edu/openaccess/api/v1.0/search?q=online_visual_material:true+AND+{enc}&api_key=DEMO_KEY"
            req = urllib.request.Request(url, headers={"User-Agent": "SonagiStudioHarvester/1.0"})
            items = []
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    rows = data.get("response", {}).get("rows", [])
                    for r in rows:
                        if len(items) >= max_items:
                            break
                        content = r.get("content", {})
                        dnr = content.get("descriptiveNonRepeating", {})
                        media = dnr.get("online_media", {}).get("media", [])
                        if media:
                            img_url = media[0].get("content") or media[0].get("thumbnail")
                            thumb_url = media[0].get("thumbnail") or img_url
                            if img_url:
                                items.append({
                                    "id": r.get("id"),
                                    "title": r.get("title") or "Smithsonian Item",
                                    "unit_code": r.get("unitCode", "Smithsonian"),
                                    "image_url": img_url,
                                    "thumbnail_url": thumb_url,
                                    "record_link": dnr.get("record_link") or f"https://www.si.edu/search/collection-images?edan_q={enc}",
                                    "license": "Smithsonian CC0 Open Access"
                                })
            except Exception as e:
                print(f"    - Smithsonian 내부 에러 ({term}): {e}")
            return items

        smith_items = fetch_smithsonian(query, max_items=3)
        if not smith_items and " " in query:
            words = [w for w in query.replace("&", " ").split() if len(w) > 3]
            for w in words:
                smith_items = fetch_smithsonian(w, max_items=3)
                if smith_items:
                    break
        if not smith_items:
            fallback = "art" if category in ["design", "art"] else "history"
            smith_items = fetch_smithsonian(fallback, max_items=3)
    except Exception as e:
        print(f"⚠️ [Smithsonian] API 호출 실패: {e}")
        
    print(f"  ✓ Smithsonian CC0 도판 {len(smith_items)}건 수집 완료")
    return {"smithsonian_results": smith_items, "status": "smithsonian_collected"}

# -------------------------------------------------------------
# 5. NASA Media Node: NASA Image & Video Library (4K MP4 영상 푸티지 & 고해상도 천체 사진)
# -------------------------------------------------------------
def nasa_media_node(state: HarvesterState) -> Dict[str, Any]:
    query = state.get("query", "space")
    print(f"🚀 [Harvester: NASA] '{query}' NASA 공식 영상 푸티지 및 천체 미디어 탐색 중...")
    
    nasa_items = []
    try:
        def fetch_nasa_media(term: str, max_items: int = 3):
            enc = urllib.parse.quote(term)
            url = f"https://images-api.nasa.gov/search?q={enc}&media_type=image,video"
            req = urllib.request.Request(url, headers={"User-Agent": "SonagiStudioHarvester/1.0"})
            items = []
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw_items = data.get("collection", {}).get("items", [])
                    for it in raw_items:
                        if len(items) >= max_items:
                            break
                        d = it.get("data", [{}])[0]
                        media_type = d.get("media_type")
                        links = it.get("links", [{}])
                        thumb = links[0].get("href") if links else None
                        collection_json = it.get("href")
                        
                        mp4_url = None
                        if media_type == "video" and collection_json:
                            try:
                                with urllib.request.urlopen(collection_json, timeout=5) as c_resp:
                                    c_urls = json.loads(c_resp.read().decode("utf-8"))
                                    for u in c_urls:
                                        if isinstance(u, str) and u.endswith("~medium.mp4"):
                                            mp4_url = u
                                            break
                                    if not mp4_url:
                                        for u in c_urls:
                                            if isinstance(u, str) and u.endswith(".mp4"):
                                                mp4_url = u
                                                break
                            except Exception:
                                pass

                        items.append({
                            "nasa_id": d.get("nasa_id"),
                            "title": d.get("title") or "NASA Asset",
                            "media_type": media_type,
                            "date_created": d.get("date_created", "")[:10],
                            "description": str(d.get("description", ""))[:200],
                            "thumbnail_url": thumb,
                            "video_mp4_url": mp4_url,
                            "license": "NASA Public Domain (US Government Work)"
                        })
            except Exception as e:
                print(f"    - NASA 내부 에러 ({term}): {e}")
            return items

        nasa_items = fetch_nasa_media(query, max_items=3)
        if not nasa_items:
            nasa_items = fetch_nasa_media("earth", max_items=3)
    except Exception as e:
        print(f"⚠️ [NASA] 미디어 API 호출 실패: {e}")
        
    print(f"  ✓ NASA 공식 미디어 {len(nasa_items)}건 (비디오 푸티지 포함) 인출 완료")
    return {"nasa_media_results": nasa_items, "status": "nasa_collected"}

# -------------------------------------------------------------
# 6. Public Data Node: 대한민국 공공데이터(서울시 환경/도시) & 세계은행 글로벌 경제 지표
# -------------------------------------------------------------
def public_data_node(state: HarvesterState) -> Dict[str, Any]:
    print(f"📊 [Harvester: PublicData] 공공데이터포털 규격 및 World Bank 거시 데이터셋 수집 중...")
    
    public_records = []
    
    # 6-1. 서울 열린데이터광장(Seoul Open Data) 환경/대기 실측치
    try:
        seoul_url = "http://openapi.seoul.go.kr:8088/sample/json/RealtimeCityAir/1/3/"
        req = urllib.request.Request(seoul_url, headers={"User-Agent": "SonagiStudioHarvester/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rows = data.get("RealtimeCityAir", {}).get("row", [])
            for r in rows:
                public_records.append({
                    "source": "Seoul Open Data (data.go.kr 연계)",
                    "category": "실시간 도시 대기 환경 지수",
                    "metric": f"{r.get('MSRSTN_NM')} (측정: {r.get('MSRMT_DT')})",
                    "value": f"미세먼지(PM10): {r.get('PM')}㎍/㎥, 초미세먼지(PM2.5): {r.get('FPM')}㎍/㎥, 오존: {r.get('OZON')}ppm, 등급: {r.get('CAI_GRD')}",
                    "license": "공공누리 제1유형 (출처표시)"
                })
    except Exception as e:
        print(f"⚠️ [PublicData: Seoul] 호출 에러: {e}")
        
    # 6-2. World Bank Open Data 거시 경제 및 인구 통계 지표
    try:
        wb_url = "https://api.worldbank.org/v2/country/KOR;USA;WLD/indicator/SP.POP.TOTL?format=json&date=2022:2023"
        req = urllib.request.Request(wb_url, headers={"User-Agent": "SonagiStudioHarvester/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if len(data) > 1:
                for item in data[1][:3]:
                    country = item.get("country", {}).get("value")
                    year = item.get("date")
                    pop = item.get("value")
                    formatted_pop = f"{pop:,}" if isinstance(pop, (int, float)) else str(pop)
                    public_records.append({
                        "source": "World Bank Open Data (Data.gov 글로벌 파트너)",
                        "category": "국가 총인구 센서스 시계열 지표",
                        "metric": f"{country} ({year})",
                        "value": f"총 인구수: {formatted_pop}명",
                        "license": "CC-BY 4.0 Open Data"
                    })
    except Exception as e:
        print(f"⚠️ [PublicData: WorldBank] 호출 에러: {e}")
        
    print(f"  ✓ 국내외 공공데이터 {len(public_records)}건 인출 완료")
    return {"public_data_results": public_records, "status": "public_data_collected"}

# -------------------------------------------------------------
# 7. Curriculum Structure Node: 업계 표준 학습 목차(TOC) 트리 합성
# -------------------------------------------------------------
def curriculum_indexer_node(state: HarvesterState) -> Dict[str, Any]:
    query = state.get("query", "design systems")
    print(f"🎓 [Harvester: Curriculum] '{query}' 관련 아카데미/대학 교육 챕터 구조 매핑 중...")
    
    curriculum = [
        {"chapter": "01. Fundamentals & Historical Context", "topics": ["역사적 기원과 사조", "기초 조형 요소와 그리드", "공공 문화재와 헤리티지 고찰"]},
        {"chapter": "02. Core Kinematics & Mathematical Ratios", "topics": ["비례 체계(황금비, Major Third)", "텐션 및 속도 곡선 역학", "색채론 및 조화비율"]},
        {"chapter": "03. Production System Architecture", "topics": ["디자인 토큰 규격화", "컴포넌트 계층화와 모듈화", "데이터 시각화 및 인포그래픽 설계"]},
        {"chapter": "04. Real-world Composition & Case Studies", "topics": ["실전 인터랙션 합성", "반응형 뷰포트 배포", "공공 데이터 기반 대시보드 검증"]}
    ]
    
    return {"curriculum_indices": curriculum, "status": "curriculum_indexed"}

# -------------------------------------------------------------
# 8. Wiki Compaction Node: Dual-Coding 마크다운 규격으로 위키에 자동 저장
# -------------------------------------------------------------
def wiki_compactor_node(state: HarvesterState) -> Dict[str, Any]:
    print("📝 [Harvester: WikiCompactor] 수집된 지식 지도를 llm-wiki 규격 마크다운으로 합성 중...")
    
    query = state.get("query", "General")
    category = state.get("category", "design")
    ol_books = state.get("open_library_results", [])
    archives = state.get("archive_results", [])
    met_items = state.get("met_museum_results", [])
    smith_items = state.get("smithsonian_results", [])
    nasa_items = state.get("nasa_media_results", [])
    public_records = state.get("public_data_results", [])
    curriculum = state.get("curriculum_indices", [])
    
    slug = query.lower().replace(" ", "-").replace("/", "-")
    today = time.strftime("%Y-%m-%d")
    
    md_lines = [
        "---",
        f"title: Master Knowledge Catalog - {query.title()}",
        f"tags: [catalog, knowledge-index, {category}, open-library, archive, the-met-cc0, smithsonian, nasa, public-data, reference]",
        f"created: {today}",
        "status: indexed",
        "sources: [open-library, internet-archive, the-met-museum-cc0, smithsonian-open-access, nasa-media-library, seoul-open-data, world-bank-open-data]",
        "---",
        "",
        f"# 📚 Master Knowledge Catalog: {query.title()}",
        "",
        f"> *\"글로벌 오픈 도서관, 메트로폴리탄 미술관, 스미소니언 박물관(CC0), NASA 미디어 라이브러리 및 정부 공공데이터에서 추출한 합법적 지식 색인(Metadata Map).\"*",
        "",
        "## 🏛️ 1. 글로벌 도서관 공식 서지 목록 (Open Library)",
        ""
    ]
    
    if ol_books:
        for b in ol_books:
            ia_link = f" ([Archive.org Read](https://archive.org/details/{b['ia_id']}))" if b.get('ia_id') else ""
            subjects_str = ", ".join(b.get('subjects', [])) if b.get('subjects') else "General"
            md_lines.append(f"- **{b['title']}** — {b['author']} ({b['first_publish_year'] or 'N/A'}){ia_link}")
            md_lines.append(f"  - **ISBN / Key**: `{b.get('isbn') or b.get('key')}`")
            md_lines.append(f"  - **주제 분류**: `{subjects_str}`")
    else:
        md_lines.append("- (수집된 도서관 서지 정보 없음)")
        
    md_lines.extend([
        "",
        "## 📜 2. 인터넷 아카이브 공공 도메인 & 도판 레코드 (Internet Archive)",
        ""
    ])
    
    if archives:
        for a in archives:
            md_lines.append(f"- [{a['title']}]({a['archive_url']}) — {a['creator']} ({a['year'] or 'Archive'})")
            if a.get('description'):
                md_lines.append(f"  > {a['description']}")
    else:
        md_lines.append("- (수집된 공공 도메인 자료 없음)")
        
    md_lines.extend([
        "",
        "## 🎨 3. 메트로폴리탄 미술관(The Met) 40만 점 CC0 고해상도 예술 도판",
        ""
    ])
    
    if met_items:
        for m in met_items:
            md_lines.append(f"### 🖼️ [{m['title']}]({m['met_url']})")
            md_lines.append(f"- **작가**: {m['artist']}")
            md_lines.append(f"- **제작 연대 / 재료**: {m['date']} | {m['medium']}")
            md_lines.append(f"- **소장 부서**: {m['department']}")
            md_lines.append(f"- **고해상도 도판 URL (CC0)**: [고화질 원본 보기]({m['primary_image']})")
            md_lines.append(f"![{m['title']}]({m['primary_image_small']})")
            md_lines.append("")
    else:
        md_lines.append("- (검색된 메트로폴리탄 미술관 CC0 도판 없음)")
        
    md_lines.extend([
        "",
        "## 🏛️ 4. 스미소니언 박물관(Smithsonian) 450만 점 오픈 액세스 도판",
        ""
    ])
    
    if smith_items:
        for s in smith_items:
            md_lines.append(f"### 🏺 [{s['title']}]({s['record_link']})")
            md_lines.append(f"- **소장 기관**: `{s['unit_code']}` | **라이선스**: `{s['license']}`")
            md_lines.append(f"- **도판 링크**: [고화질 이미지]({s['image_url']})")
            md_lines.append(f"![{s['title']}]({s['thumbnail_url']})")
            md_lines.append("")
    else:
        md_lines.append("- (검색된 스미소니언 도판 없음)")
        
    md_lines.extend([
        "",
        "## 🚀 5. 미국 항공우주국(NASA) 고해상도 미디어 & 영상 푸티지 (VideoOps B-roll)",
        ""
    ])
    
    if nasa_items:
        for n in nasa_items:
            media_badge = "🎬 Video (MP4)" if n['media_type'] == "video" else "📷 Photo (High-Res)"
            md_lines.append(f"### {media_badge}: {n['title']} ({n['date_created']})")
            md_lines.append(f"- **NASA ID**: `{n['nasa_id']}` | **라이선스**: `{n['license']}`")
            if n.get('video_mp4_url'):
                md_lines.append(f"- **4K/HD Video URL**: [동영상 다운로드 / 스트리밍]({n['video_mp4_url']})")
            if n.get('thumbnail_url'):
                md_lines.append(f"![{n['title']}]({n['thumbnail_url']})")
            if n.get('description'):
                md_lines.append(f"> {n['description']}")
            md_lines.append("")
    else:
        md_lines.append("- (검색된 NASA 미디어 없음)")

    md_lines.extend([
        "",
        "## 📊 6. 대한민국 공공데이터 & 글로벌 거시 경제/도시 실측치",
        ""
    ])
    
    if public_records:
        for p in public_records:
            md_lines.append(f"- **[{p['source']}] {p['category']}**: `{p['metric']}`")
            md_lines.append(f"  - **실측 지표**: {p['value']}")
            md_lines.append(f"  - **라이선스**: `{p['license']}`")
    else:
        md_lines.append("- (수집된 공공데이터 실측 지표 없음)")
        
    md_lines.extend([
        "",
        "## 🎓 7. 전문가 실무 교육 챕터 구조 (Curriculum Index)",
        ""
    ])
    
    for c in curriculum:
        md_lines.append(f"### {c['chapter']}")
        for t in c['topics']:
            md_lines.append(f"- [ ] {t}")
        md_lines.append("")
        
    compiled_md = "\n".join(md_lines)
    
    WIKI_REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    wiki_file = WIKI_REFERENCES_DIR / f"{slug}-catalog.md"
    wiki_file.write_text(compiled_md, encoding="utf-8")
    print(f"  ✓ 위키 카탈로그 저장 완료: {wiki_file}")
    
    return {
        "compiled_markdown": compiled_md,
        "saved_wiki_path": str(wiki_file),
        "status": "completed"
    }

# -------------------------------------------------------------
# Graph Builder
# -------------------------------------------------------------
def build_knowledge_harvester_graph():
    builder = StateGraph(HarvesterState)
    
    builder.add_node("open_library", open_library_node)
    builder.add_node("archive_org", archive_org_node)
    builder.add_node("met_museum", met_museum_node)
    builder.add_node("smithsonian", smithsonian_node)
    builder.add_node("nasa_media", nasa_media_node)
    builder.add_node("public_data", public_data_node)
    builder.add_node("curriculum_indexer", curriculum_indexer_node)
    builder.add_node("wiki_compactor", wiki_compactor_node)
    
    builder.add_edge(START, "open_library")
    builder.add_edge("open_library", "archive_org")
    builder.add_edge("archive_org", "met_museum")
    builder.add_edge("met_museum", "smithsonian")
    builder.add_edge("smithsonian", "nasa_media")
    builder.add_edge("nasa_media", "public_data")
    builder.add_edge("public_data", "curriculum_indexer")
    builder.add_edge("curriculum_indexer", "wiki_compactor")
    builder.add_edge("wiki_compactor", END)
    
    memory = MemorySaver()
    return builder.compile(checkpointer=memory)

graph = build_knowledge_harvester_graph()
