# 복구 노트 — 그래프 정리 중 잃은 작업물

2026-11-24 그래프 정리 작업(#16~#19) 중 `git reset --hard`로 작업 브랜치
`feat/TOOL-12-13-video-and-harvester`의 커밋되지 않은 변경이 지워졌다.
브랜치 포인터는 reflog로 cd3237e에 복구했지만, working tree의 변경은 git에
없어 복구할 수 없었다.

## 복구된 파일 (이 PR에 포함)

- `graphs/bitwarden_resolver_graph.py` — Bitwarden CLI 연동 데모 그래프 (미등록).
  `graph`가 아닌 `bitwarden_resolver_graph`를 export하고, 가짜 비밀번호가
  하드코딩되어 있어 그대로 등록하지는 않는다.
- `graphs/mass_research_graph.py` — DuckDuckGo + Jina Reader 대량 리서치.
  `claude-sonnet-4-6` 모델명이 프록시에 없어 400이 난다. 살리려면 모델명 교체 필요.
- `update_graph_docs.py` — 그래프 다이어그램을 위키에 자동 생성하는 스크립트.
  `.git/hooks`가 매 커밋마다 이 파일을 호출하고 있었는데 저장소에 없어서 에러가 났다.

## 복구하지 못한 것

- `graphs/market_intelligence_graph.py`의 149줄짜리 로컬 재작성 (가장 큰 손실).
  아래는 `git diff` 앞부분만 남아 있다. 나머지는 다시 작성해야 한다.

```diff
 import operator
-from typing import Annotated, TypedDict, List, Dict, Any
+import os
+import sqlite3
+import requests
+import xml.etree.ElementTree as ET
+from typing import Annotated, TypedDict, List, Dict, Any, Optional
+from datetime import datetime
+from pydantic import BaseModel, Field
+
 from langgraph.graph import StateGraph, END
-from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
+from langchain_core.messages import SystemMessage, HumanMessage
+from langchain_openai import ChatOpenAI
 from langgraph.types import Send
 from langgraph.checkpoint.memory import MemorySaver
-import json
```

- `main.py` (Market Intelligence 워크플로우 실행 스크립트) — trivial, 재작성 가능.
- `intelligence_test.db` — 런타임 산출물, 복구 불필요.

## 남은 정리 과제

- `graphs/__pycache__/*.pyc` tracked 상태 → `git rm --cached`.
- `.git/hooks`의 `update_graph_docs.py` 호출 → hook 수정 또는 스크립트 커밋.
