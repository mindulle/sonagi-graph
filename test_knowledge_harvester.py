"""KnowledgeHarvesterGraph 단위 테스트 — 외부 API는 고정 응답으로 대체한다.

라이브 API 검증은 `scripts/verify_knowledge_harvester.py`(호스트에서만)로 분리했다.
여기서는 응답 파싱·이미지 없는 항목 제외·카탈로그 저장 경로만 결정론적으로 확인한다.
"""

import json

import pytest

from graphs import knowledge_harvester_graph as mod
from langgraph.checkpoint.memory import MemorySaver
mod.graph = mod.build_knowledge_harvester_graph(checkpointer=MemorySaver())


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


FIXTURES = {
    "openlibrary.org": {
        "docs": [
            {
                "title": "The Grid Book",
                "author_name": ["Hannah B Higgins"],
                "first_publish_year": 2009,
                "key": "/works/OL1W",
                "isbn": ["9780262512404"],
                "subject": ["Design", "Grids"],
                "ia": "gridbook00higg",
            }
        ]
    },
    "archive.org": {
        "response": {
            "docs": [
                {
                    "identifier": "bauhaus1919",
                    "title": "Staatliches Bauhaus",
                    "creator": "Gropius",
                    "year": "1919",
                    "description": "school",
                }
            ]
        }
    },
    "v1.1/search": {"objectIDs": [100, 200]},
    "v1/objects/100": {
        "objectID": 100,
        "title": "Glass Vase",
        "artistDisplayName": "Anonymous",
        "objectDate": "1900",
        "medium": "glass",
        "department": "Modern Art",
        "primaryImage": "https://img.met/100.jpg",
        "primaryImageSmall": "https://img.met/100s.jpg",
        "objectURL": "https://met/100",
    },
    "v1/objects/200": {"objectID": 200, "title": "No Image"},
    "api.si.edu": {
        "response": {
            "rows": [
                {
                    "id": "s1",
                    "title": "Desk Lamp",
                    "unitCode": "NMAH",
                    "content": {
                        "descriptiveNonRepeating": {
                            "online_media": {
                                "media": [
                                    {
                                        "content": "https://si/1.jpg",
                                        "thumbnail": "https://si/1t.jpg",
                                    }
                                ]
                            },
                            "record_link": "https://si/rec/1",
                        }
                    },
                }
            ]
        }
    },
    "images-api.nasa.gov/search": {
        "collection": {
            "items": [
                {
                    "href": "https://images-api.nasa.gov/asset/PIA1",
                    "data": [
                        {
                            "nasa_id": "PIA1",
                            "title": "Blue Marble",
                            "media_type": "image",
                            "date_created": "2020-01-01T00:00:00Z",
                            "description": "earth",
                        }
                    ],
                    "links": [{"href": "https://nasa/thumb.jpg"}],
                }
            ]
        }
    },
    "openapi.seoul.go.kr": {
        "RealtimeCityAir": {
            "row": [
                {
                    "MSRSTN_NM": "중구",
                    "MSRMT_DT": "2026-11-24",
                    "PM": "30",
                    "FPM": "15",
                    "OZON": "0.03",
                    "CAI_GRD": "보통",
                }
            ]
        }
    },
    "api.worldbank.org": [
        {"page": 1},
        [{"country": {"value": "대한민국"}, "date": "2023", "value": 51712620}],
    ],
}


def _route(url):
    for needle, payload in FIXTURES.items():
        if needle in url:
            return payload
    raise AssertionError(f"예상하지 못한 외부 호출: {url}")


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None, **kw: _Resp(_route(req.full_url)),
    )
    monkeypatch.setattr(mod, "WIKI_REFERENCES_DIR", tmp_path / "Catalogs")
    return tmp_path


def test_open_library_parses_bibliography(offline):
    result = mod.open_library_node({"query": "Bauhaus"})

    book = result["open_library_results"][0]
    assert book["title"] == "The Grid Book"
    assert book["author"] == "Hannah B Higgins"
    assert book["isbn"] == "9780262512404"
    assert book["ia_id"] == "gridbook00higg"


def test_met_museum_drops_items_without_images(offline):
    result = mod.met_museum_node({"query": "Bauhaus", "category": "design"})

    items = result["met_museum_results"]
    assert len(items) == 1
    assert items[0]["object_id"] == 100
    assert items[0]["primary_image"] == "https://img.met/100.jpg"


def test_nasa_keeps_media_type_and_license(offline):
    result = mod.nasa_media_node({"query": "earth"})

    item = result["nasa_media_results"][0]
    assert item["nasa_id"] == "PIA1"
    assert item["media_type"] == "image"
    assert item["thumbnail_url"] == "https://nasa/thumb.jpg"


def test_compactor_writes_catalog_under_wiki_dir(offline):
    state = {
        "query": "Bauhaus",
        "category": "design",
        "open_library_results": [
            {
                "title": "The Grid Book",
                "author": "H",
                "first_publish_year": 2009,
                "key": "/works/OL1W",
                "isbn": "978",
                "ia_id": None,
                "subjects": ["Design"],
            }
        ],
        "archive_results": [],
        "met_museum_results": [],
        "smithsonian_results": [],
        "nasa_media_results": [],
        "public_data_results": [],
        "curriculum_indices": [],
    }
    result = mod.wiki_compactor_node(state)
    path = mod.Path(result["saved_wiki_path"])

    assert path.exists()
    assert path.parent == mod.WIKI_REFERENCES_DIR
    text = path.read_text(encoding="utf-8")
    assert "Master Knowledge Catalog - Bauhaus" in text
    assert "The Grid Book" in text


def test_graph_end_to_end_offline(offline):
    result = mod.graph.invoke(
        {"query": "Bauhaus", "category": "design"},
        config={"configurable": {"thread_id": "harvester-unit"}},
    )

    assert result["status"] == "completed"
    assert len(result["open_library_results"]) == 1
    assert len(result["public_data_results"]) == 2
    assert mod.Path(result["saved_wiki_path"]).exists()
