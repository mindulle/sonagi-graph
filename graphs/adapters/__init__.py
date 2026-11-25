"""adapters — 그래프가 아닌 재사용 파이프라인 (GRAPH-21).

`graphs/*_graph.py`는 LangGraph 배선이고, 그 아래에서 반복되는 실행 단위는
여기에 둔다. 첫 번째는 `assets`(sonagi-assets 렌더·린트·원장)다.
"""

from __future__ import annotations

from graphs.adapters import assets

__all__ = ["assets"]
