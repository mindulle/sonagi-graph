# Sonagi Workflows

Sonagi 생태계의 다양한 에이전틱 워크플로우(Agentic Workflows)를 정의하고 관리하는 독립 저장소입니다.

## 아키텍처 개요
- **LangGraph** 기반의 Python 코드로 워크플로우를 설계합니다.
- `llmops-instance` 서버에 배포되어 백그라운드 워커로 작동합니다.
- **Paperclip**과 **Kimaki**를 통해 모니터링, 제어(HITL), 오케스트레이션이 이루어집니다.

## 시작하기
```bash
pip install langgraph-cli
langgraph up --wait --debugger-port 3000
```
