"""공유 픽스처 — 그래프를 네트워크·LLM·외부 저장소 없이 돌린다.

GRAPH-10(P2 검증 표준)의 L2(fixture E2E)·L3(결정론)를 위한 하네스다.
그래프 본체(`graphs/*_graph.py`)는 건드리지 않고, 노드가 의존하는 경계만 바꾼다:

| 경계 | 대체 수단 |
| --- | --- |
| `chat_model` / `ChatOpenAI` | `fake_llm` 픽스처(모듈 속성 monkeypatch) |
| HTTP (SearXNG, Figma 브리지) | `fake_searxng`, `patch_bridge` 픽스처 |
| subprocess (`scripts/*.py`) | `asset_repo` 픽스처의 스텁 스크립트 |
| 토큰 정본 | `token_fixture` 픽스처가 만드는 임시 정본 |

네트워크 차단은 `no_network` 픽스처를 쓴 테스트에만 적용한다(전역 autouse 금지 —
기존 테스트의 로컬 서비스 호출을 깨지 않기 위해서다).
"""

from __future__ import annotations

import builtins
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _local_render_default(monkeypatch):
    """테스트는 ssh·devops 없이 돈다. 원격 렌더는 개별 테스트가 켠다."""
    monkeypatch.setenv("SONAGI_REMOTE_RENDER", "0")


# ---------------------------------------------------------------- 네트워크 차단


@pytest.fixture
def no_network(monkeypatch):
    """실소켓 연결을 막는다. 패치하지 않은 HTTP 호출이 있으면 즉시 실패한다."""

    class _Blocked(OSError):
        pass

    def _connect(self, address):  # noqa: ANN001 - socket 시그니처
        raise _Blocked(f"네트워크 차단됨(offline fixture): {address!r}")

    def _create_connection(address, *args, **kwargs):  # noqa: ANN001
        raise _Blocked(f"네트워크 차단됨(offline fixture): {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _connect, raising=True)
    monkeypatch.setattr(socket, "create_connection", _create_connection, raising=True)
    return _Blocked


# ---------------------------------------------------------------- 토큰 정본


def _primitives() -> dict:
    return {
        "$schema": "https://design-tokens.github.io/community-group/format/",
        "meta": {"name": "fixture", "version": "9.9.9"},
        "primitive": {
            "brand": {
                "blue": {"$value": "#0055ff", "$type": "color"},
                "sun": {"$value": "#ffcc00", "$type": "color"},
            },
            "base": {"white": {"$value": "#ffffff", "$type": "color"}},
        },
        "spacing": {
            "0": {"$value": "0px", "$type": "dimension"},
            "1": {"$value": "4px", "$type": "dimension"},
            "2": {"$value": "8px", "$type": "dimension"},
            "4": {"$value": "16px", "$type": "dimension"},
            "6": {"$value": "24px", "$type": "dimension"},
            "8": {"$value": "32px", "$type": "dimension"},
        },
        "typography": {
            "fontFamily": {
                "sans": {"$value": "'Inter', sans-serif", "$type": "fontFamily"}
            },
            "fontSize": {
                "sm": {"$value": "14px", "$type": "dimension"},
                "base": {"$value": "16px", "$type": "dimension"},
            },
        },
        "borderRadius": {
            "none": {"$value": "0px", "$type": "dimension"},
            "sm": {"$value": "4px", "$type": "dimension"},
            "base": {"$value": "6px", "$type": "dimension"},
            "md": {"$value": "8px", "$type": "dimension"},
            "full": {"$value": "9999px", "$type": "dimension"},
        },
    }


def _typography_scale(steps: int = 6) -> dict:
    sizes = [((steps - index) * 8 + 8) for index in range(steps)]
    return {
        f"h{index + 1}": {
            "fontSize": {"$value": f"{size}px", "$type": "dimension"},
            "lineHeight": {"$value": f"{size + 4}px", "$type": "dimension"},
            "fontWeight": {"$value": "700", "$type": "fontWeight"},
        }
        for index, size in enumerate(sizes)
    }


def _semantics(*, raw_hex: bool = False, typography: dict | None = None) -> dict:
    light = {
        "brand": {
            "primary": {"$value": "{primitive.brand.blue}", "$type": "color"},
            "accent": {"$value": "{primitive.brand.sun}", "$type": "color"},
        },
        "background": {"base": {"$value": "{primitive.base.white}", "$type": "color"}},
        "text": {
            "primary": {"$value": "{primitive.brand.blue}", "$type": "color"},
            "inverse": {"$value": "{primitive.base.white}", "$type": "color"},
        },
        "border": {"default": {"$value": "{primitive.brand.blue}", "$type": "color"}},
        "state": {"success": {"$value": "{primitive.brand.blue}", "$type": "color"}},
    }
    dark = {
        "brand": {
            "primary": {"$value": "{primitive.brand.sun}", "$type": "color"},
            "accent": {"$value": "{primitive.brand.blue}", "$type": "color"},
        },
        "background": {"base": {"$value": "{primitive.brand.blue}", "$type": "color"}},
        "text": {
            "primary": {"$value": "{primitive.base.white}", "$type": "color"},
            "inverse": {"$value": "{primitive.brand.blue}", "$type": "color"},
        },
        "border": {"default": {"$value": "{primitive.brand.sun}", "$type": "color"}},
        "state": {"success": {"$value": "{primitive.brand.sun}", "$type": "color"}},
    }
    if raw_hex:  # 치명 결함: semantic 계층의 원시 hex
        light["text"]["primary"]["$value"] = "#abcdef"
    return {
        "$schema": "https://design-tokens.github.io/community-group/format/",
        "meta": {"name": "fixture-semantics", "version": "9.9.9"},
        "semantic": {"light": {"color": light}, "dark": {"color": dark}},
        "typography": typography if typography is not None else _typography_scale(),
    }


def write_token_fixture(
    directory: Path, *, raw_hex: bool = False, typography: dict | None = None
) -> Path:
    """검사를 통과하는 임시 정본(primitives+semantics)을 만든다."""
    tokens = Path(directory) / "tokens"
    tokens.mkdir(parents=True, exist_ok=True)
    docs = {
        "primitives.json": _primitives(),
        "semantics.json": _semantics(raw_hex=raw_hex, typography=typography),
    }
    for name, doc in docs.items():
        (tokens / name).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return Path(directory)


@pytest.fixture
def token_fixture():
    """`token_fixture(tmp_path)` → 임시 정본 루트. `raw_hex=True`면 치명 결함."""

    def make(directory, *, raw_hex: bool = False, typography: dict | None = None):
        return write_token_fixture(
            Path(directory), raw_hex=raw_hex, typography=typography
        )

    return make


# ---------------------------------------------------------------- Figma 브리지


@pytest.fixture
def patch_bridge(monkeypatch):
    """`token_sync.probe_bridge`를 결정론적 응답으로 바꾼다(실소켓 없음)."""
    import token_sync as ts

    def apply(*, verified: bool = False, clients: int = 0):
        probed: dict = {"ports": None, "timeout": None}

        def fake_probe(ports=None, timeout=0.4):
            probed["ports"] = list(ports or [])
            probed["timeout"] = timeout
            return {
                "verified": verified,
                "probes": [
                    {
                        "port": port,
                        "url": f"http://127.0.0.1:{port}/health",
                        "ok": verified,
                        "status": 200,
                        "connectedClients": clients,
                    }
                    for port in probed["ports"]
                ],
                "connectedClients": clients,
                "checkedAt": None,
                "evidence": (
                    "GET /health 200 + connectedClients>0"
                    if verified
                    else "GET /health connectedClients=0(플러그인 미연결)"
                ),
            }

        monkeypatch.setattr(ts, "probe_bridge", fake_probe)
        return probed

    return apply


# ---------------------------------------------------------------- assets 어댑터 저장소

_IMPORT_CONTENT = """\
import argparse, json, pathlib
parser = argparse.ArgumentParser()
parser.add_argument("content")
parser.add_argument("--catalog")
parser.add_argument("--out")
args = parser.parse_args()
out = pathlib.Path(args.out)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "content": pathlib.Path(args.content).name,
    "catalog": pathlib.Path(args.catalog).name,
}, sort_keys=True), encoding="utf-8")
"""

_RENDER_RECIPE = """\
import argparse, json, pathlib
parser = argparse.ArgumentParser()
parser.add_argument("--recipe")
parser.add_argument("--style")
parser.add_argument("--catalog")
parser.add_argument("--tokens")
parser.add_argument("--out")
parser.add_argument("--breakpoint")
args = parser.parse_args()
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
style = pathlib.Path(args.style).stem
(out / "render.svg").write_text(f'<svg data-style="{style}"></svg>', encoding="utf-8")
(out / "render.manifest.json").write_text(json.dumps({
    "style": style,
    "breakpoint": args.breakpoint,
    "recipe": pathlib.Path(args.recipe).name,
}, sort_keys=True), encoding="utf-8")
"""

_LINT_RENDER = """\
import argparse, json, pathlib
parser = argparse.ArgumentParser()
parser.add_argument("--svg")
parser.add_argument("--manifest")
parser.add_argument("--tokens")
parser.add_argument("--json", action="store_true")
args = parser.parse_args()
manifest = json.loads(pathlib.Path(args.manifest).read_text(encoding="utf-8"))
style = manifest["style"]
if style.startswith("risk"):
    payload = {"verdict": "fail", "findings": [{"rule": "contrast", "severity": "fail"}]}
elif style.startswith("warn"):
    payload = {"verdict": "warn", "findings": [{"rule": "spacing", "severity": "warn"}]}
else:
    payload = {"verdict": "pass", "findings": []}
print(json.dumps(payload, sort_keys=True))
"""

_LEDGER = """\
import argparse, json, pathlib
parser = argparse.ArgumentParser()
parser.add_argument("--svg")
parser.add_argument("--manifest")
parser.add_argument("--decision", action="append", default=[])
parser.add_argument("--by")
parser.add_argument("--out")
args = parser.parse_args()
out = pathlib.Path(args.out)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "svg": pathlib.Path(args.svg).name,
    "by": args.by,
    "decisions": args.decision,
}, indent=2, sort_keys=True), encoding="utf-8")
"""

# 브리프 "민들레 홀씨 인쇄물" 기준: 톤 매칭은 styles 축으로, 참조 매칭은 주제어로.
_CATALOG_ITEMS = [
    {
        "id": "ill-1",
        "name": "Dandelion Study",
        "category": "painting",
        "ext": "png",
        "topics": ["dandelion"],
        "tags": ["kind:illustration"],
        "license": "cc0",
        "license_scope": "bundle",
    },
    {
        "id": "ill-2",
        "name": "Seed Drift",
        "category": "drawing",
        "ext": "jpg",
        "topics": ["seed"],
        "tags": ["kind:illustration"],
        "license": "cc0",
        "license_scope": "output",
    },
    {
        "id": "ico-1",
        "name": "Seed Glyph",
        "ext": "svg",
        "topics": ["seed"],
        "tags": ["kind:icon"],
        "license": "cc0",
        "license_scope": "bundle",
    },
    {
        "id": "typo-1",
        "name": "Seed Specimen",
        "category": "typography",
        "ext": "woff2",
        "topics": ["seed"],
        "license": "ofl",
        "license_scope": "output",
    },
    {
        "id": "blocked-1",
        "name": "Seed Blocked",
        "category": "painting",
        "ext": "png",
        "topics": ["seed"],
        "license_scope": "blocked",
    },
]

_STYLES = [
    {
        "name": "minimal-clean",
        "id": "style-minimal",
        "axes": {
            "palette": "minimal",
            "type": "inter",
            "layout": "grid",
            "ornament": "none",
        },
    },
    {
        "name": "risk-loud",
        "id": "style-risk",
        "axes": {
            "palette": "minimal-poster",
            "type": "archivo-caps",
            "layout": "columns",
            "ornament": "none",
        },
    },
    {
        "name": "warn-soft",
        "id": "style-warn",
        "axes": {
            "palette": "ocean-minimal",
            "type": "inter",
            "layout": "grid",
            "ornament": "rule",
        },
    },
]


def _write_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def asset_repo(tmp_path):
    """`sonagi-assets` 대역: 스텁 스크립트 + 명부 + 카탈로그 + 토큰.

    경로를 `AssetPaths`로 돌려준다 — 어댑터 호출부가 전역 상수를 monkeypatch하지
    않고 명시적으로 경로를 넘긴다(그래프가 사라진 뒤의 계약).
    """
    from graphs.adapters.assets import AssetPaths

    root = tmp_path / "sonagi-assets"
    _write_script(root / "scripts/import_content.py", _IMPORT_CONTENT)
    _write_script(root / "scripts/render_recipe.py", _RENDER_RECIPE)
    _write_script(root / "scripts/lint_render.py", _LINT_RENDER)
    _write_script(root / "scripts/ledger.py", _LEDGER)
    (root / "registry").mkdir(parents=True, exist_ok=True)
    (root / "registry/styles.json").write_text(
        json.dumps({"styles": _STYLES}, indent=2), encoding="utf-8"
    )
    (root / "tokens").mkdir(parents=True, exist_ok=True)
    (root / "tokens/semantic.json").write_text(
        json.dumps({"color": {"brand": "#0055ff"}}), encoding="utf-8"
    )
    catalog = root / "test_data/golden/catalog.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        json.dumps({"items": _CATALOG_ITEMS}, indent=2), encoding="utf-8"
    )

    out_root = tmp_path / "craft-out"
    paths = AssetPaths(
        assets_dir=root,
        catalog=catalog,
        tokens=root / "tokens/semantic.json",
        out_root=out_root,
    )
    return SimpleNamespace(root=root, catalog=catalog, out=out_root, paths=paths)


# ---------------------------------------------------------------- SearXNG / LLM


class _FakeResponse:
    def __init__(self, payload=None, *, status_error: Exception | None = None):
        self._payload = payload or {"results": []}
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error

    def json(self):
        return self._payload


@pytest.fixture
def fake_searxng(monkeypatch):
    """`fetch_searxng`가 쓰는 `requests.get`을 대체한다. 호출 URL을 기록한다."""
    from graphs import source_verification_graph as sv

    calls: list[dict] = []

    def install(results_per_query=None, *, error: Exception | None = None):
        results_per_query = results_per_query or {}

        def fake_get(url, params=None, timeout=None):
            calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
            if error:
                raise error
            query = (params or {}).get("q")
            return _FakeResponse({"results": results_per_query.get(query, [])})

        monkeypatch.setattr(sv, "requests", SimpleNamespace(get=fake_get))
        return calls

    return install


class FakeStructuredLLM:
    """`with_structured_output(...).invoke()`만 흉내 내는 결정론 평가기."""

    def __init__(self, schema, *, score_map=None, default=10, raises_on=()):
        self.schema = schema
        self.score_map = score_map or {}
        self.default = default
        self.raises_on = tuple(raises_on)
        self.calls = []

    def _text(self, payload):
        if hasattr(payload, "to_string"):
            return payload.to_string()
        return str(payload)

    def invoke(self, payload, *args, **kwargs):
        text = self._text(payload)
        self.calls.append(text)
        for needle in self.raises_on:
            if needle in text:
                raise RuntimeError(f"LLM 실패: {needle}")
        score = self.default
        reason = "보조 블로그/리스트 글"
        for needle, mapped in self.score_map.items():
            if needle in text:
                score, reason = mapped
                break
        return self.schema(trust_score=score, reason=reason)


@pytest.fixture
def fake_llm(monkeypatch):
    """`source_verification`의 `ChatOpenAI`를 결정론 평가기로 바꾼다."""
    from graphs import source_verification_graph as sv

    created: list = []

    def install(*, score_map=None, default=10, raises_on=()):
        def factory(**kwargs):
            engine = FakeStructuredLLM(
                sv.VerificationResult,
                score_map=score_map,
                default=default,
                raises_on=raises_on,
            )
            created.append({"kwargs": kwargs, "engine": engine})

            class _Wrapper:
                def with_structured_output(self, schema):
                    from langchain_core.runnables import RunnableLambda

                    engine.schema = schema
                    # `prompt | structured_llm` 파이프가 요구하는 Runnable 계약.
                    return RunnableLambda(engine.invoke)

            return _Wrapper()

        monkeypatch.setattr(sv, "ChatOpenAI", factory)
        return created

    return install


@pytest.fixture
def capture_vault_write(monkeypatch, tmp_path):
    """`loader_node`의 Vault 쓰기를 메모리로 돌린다(실제 파일 금지).

    안전망: 임시 디렉터리 밖으로 나가는 쓰기/디렉터리 생성은 **먼저 예외**를
    던진다. 그래프의 Vault 경로가 바뀌어도 실파일을 쓰고 나서 실패하지 않는다.
    """
    import tempfile

    captured: dict = {"dirs": [], "files": []}
    real_open = builtins.open
    real_makedirs = os.makedirs
    allowed = [Path(tempfile.gettempdir()).resolve(), Path(tmp_path).resolve()]

    def _in_temp(name: str) -> bool:
        target = Path(name).resolve()
        return any(target == root or root in target.parents for root in allowed)

    class _Sink:
        def __init__(self):
            self.text = ""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, chunk):
            self.text += chunk

    def fake_open(file, mode="r", *args, **kwargs):
        name = str(file)
        if "Verified_Platforms_" in name and name.endswith(".md"):
            sink = _Sink()
            captured["files"].append({"path": name, "sink": sink})
            return sink
        writing = any(flag in str(mode) for flag in ("w", "a", "x", "+"))
        if writing and not _in_temp(name):
            raise AssertionError(f"임시 경로 밖 쓰기 차단: {name} (mode={mode})")
        return real_open(file, mode, *args, **kwargs)

    def fake_makedirs(path, *args, **kwargs):
        name = str(path)
        if "llm-wiki" in name:
            captured["dirs"].append(name)
            return None
        if not _in_temp(name):
            raise AssertionError(f"임시 경로 밖 디렉터리 생성 차단: {name}")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(os, "makedirs", fake_makedirs)
    return captured


@pytest.fixture(autouse=True)
def isolate_taste_dir(monkeypatch, tmp_path):
    """테스트 중 taste 프로필·원장 경로를 tmp_path로 격리한다."""
    taste_dir = tmp_path / "sonagi-taste"
    taste_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SONAGI_TASTE_DIR", str(taste_dir))
    return taste_dir
