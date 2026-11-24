"""CLIProxyAPI(OpenAI 호환) 경유 ChatOpenAI 생성기.

검증 그래프 세 개가 같은 프록시를 쓰므로, 모델·엔드포인트 해석을 한 곳에 모은다.
그래프마다 `get_llm()`을 따로 두고 여기로 위임하면 테스트에서 그래프 단위로 monkeypatch할 수 있다.

환경 변수
    OPENAI_API_BASE   프록시 주소 (기본 http://127.0.0.1:38107/v1)
    OPENAI_API_KEY    프록시 키 (기본 dummy)
    <MODEL_ENV>       그래프별 모델명 (없으면 gemini-3-flash)
"""

import os
from typing import Optional

from langchain_openai import ChatOpenAI

DEFAULT_BASE_URL = "http://127.0.0.1:38107/v1"
DEFAULT_MODEL = "gemini-3-flash"


def chat_model(
    model_env: str,
    default_model: Optional[str] = None,
    temperature: float = 0.1,
    timeout: int = 45,
    max_retries: int = 1,
) -> ChatOpenAI:
    """`model_env`에 지정된 환경 변수에서 모델명을 읽어 프록시용 클라이언트를 만든다."""
    return ChatOpenAI(
        model=os.environ.get(model_env) or default_model or DEFAULT_MODEL,
        temperature=temperature,
        base_url=os.environ.get("OPENAI_API_BASE", DEFAULT_BASE_URL),
        api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
        timeout=timeout,
        max_retries=max_retries,
    )


def wiki_dir() -> str:
    """검증 리포트를 저장할 위키 루트. 기본값은 이 호스트의 llm-wiki."""
    return os.environ.get("SONAGI_WIKI_DIR", "/home/ubuntu/llm-wiki")
