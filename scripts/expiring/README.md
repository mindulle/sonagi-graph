# scripts/expiring/

일회성·실험 스크립트의 임시 거처. 규칙은 `docs/GRAPH_REGISTRY.md` §7.3.

- 파일 상단 docstring에 `owner`, `expires`, `purpose`를 적는다.
- 만료일이 지나면 삭제하거나 그래프의 `verify()`로 흡수한다.
- 상시 운영 스크립트(`graph_cli.py` 등)는 여기 두지 않는다.

```python
"""owner: mindulle / expires: 2026-12-31 / purpose: video_ops 렌더 실측 1회"""
```
