# 실카탈로그 동기화 (GRAPH-22 · GRAPH-23)

그래프의 레퍼런스 스카우트(`entry_pack`, `asset_craft`)는 자산 카탈로그를 읽는다.
개발용 골든 카탈로그는 **8건**뿐이라 실제 품질의 레퍼런스를 고르지 못한다.
운영 카탈로그는 devops에 있고, 이 호스트의 마운트 경로로 동기화해서 쓴다.

카탈로그의 `path`는 실제 파일 경로지만, **렌더는 이 호스트에서 하지 않는다**.
`entry_pack` web 렌더는 devops에서 원격 카탈로그(`/srv/sonagi-assets`)를 그대로 쓰고,
이 호스트는 카탈로그 메타데이터(스카우트·import 검증)만 읽는다. 따라서 7.2GB
라이브러리를 로컬로 받지 않는다 — `docs/REMOTE_RENDER.md` 참고.

## 어디에 있나

| 위치 | 역할 |
|---|---|
| `devops:/srv/sonagi-assets/catalog/catalog.json` | 원본 (14,938 items) — 원격 렌더가 쓰는 파일 |
| `/home/ubuntu/sonagi-assets/catalog/catalog.json` | 동기화 사본 — 로컬 메타데이터용 |
| `devops:/srv/sonagi-assets/library/` | 원본 라이브러리 (7.2GB) — 원격 렌더가 연다 |
| `/home/ubuntu/sonagi-assets/test_data/golden/catalog.json` | 골든 폴백 (8 items) |

로컬 라이브러리는 **삭제됐다**. 로컬 카탈로그의 `path`는 삭제된 로컬 경로를
가리킬 수 있지만, 렌더가 원격 카탈로그를 쓰므로 영향이 없다.

컨테이너는 `/home/ubuntu/sonagi-assets`를 그대로 마운트하므로, 위 경로가
컨테이너 안에서도 동일하다. `docker-compose.deploy.yml`이
`SONAGI_CATALOG=/home/ubuntu/sonagi-assets/catalog/catalog.json`을 지정한다.

## 동기화

```bash
scripts/sync_catalog.sh
```

- `rsync -a` over ssh (`devops`), 임시 파일에 받은 뒤 JSON을 검증하고
  atomic rename 한다. 실행 중인 그래프가 반쯤 쓰인 파일을 읽지 않는다.
- ssh/rsync 오류, 빈 파일, `items` 배열 누락/파싱 실패 시 즉시 non-zero로
  실패한다.
- 내용이 같으면 파일을 건드리지 않는다. cron에 그대로 걸어도 안전하다.
- 성공 시 stderr에 경로와 항목 수를, stdout에 항목 수만 출력한다.

경로·호스트는 환경변수로 바꿀 수 있다.

```bash
SONAGI_CATALOG_REMOTE_HOST=devops \
SONAGI_CATALOG_REMOTE_PATH=/srv/sonagi-assets/catalog/catalog.json \
SONAGI_CATALOG_LOCAL_PATH=/home/ubuntu/sonagi-assets/catalog/catalog.json \
scripts/sync_catalog.sh
```

### 주기

카탈로그는 devops에서 수집 파이프라인이 갱신한다. 하루 1~4회면 충분하다.
예시 (매 6시간, 로그는 journal/syslog로):

```cron
17 */6 * * * cd /home/ubuntu/sonagi-graph && scripts/sync_catalog.sh
```

동기화 후 그래프 컨테이너는 파일을 매 요청마다 새로 읽으므로 재시작이
필요 없다(`entry_pack`은 import 시 경로만 계산하고, 스카우트 시점에 읽는다).

## 자산 라이브러리 동기화 — 폐기 (GRAPH-24)

로컬 라이브러리 동기화는 폐기됐다. `web` 렌더는 devops에서 원격 카탈로그·라이브러리를
그대로 쓰고(`docs/REMOTE_RENDER.md`), `print`·`brand`는 라이브러리를 열지 않는다.
이 호스트의 `/home/ubuntu/sonagi-assets/library`는 삭제됐다(7.2GB 회수).

`scripts/sync_library.sh`는 비상용으로만 남겨 두었고, `SONAGI_ALLOW_LOCAL_LIBRARY=1`
없이는 즉시 실패한다. 카탈로그 경로 재작성도 원격 렌더에는 필요 없다.

## 해석 순서와 폴백

`graphs/core/env.py:resolve_catalog()`가 결정한다.

1. `SONAGI_CATALOG`가 설정되어 있으면 그 경로를 그대로 쓴다 (기존 동작 유지,
   존재 검사 없음).
2. 없으면 `<assets>/catalog/catalog.json`이 **존재할 때** 그것을 쓴다 → `real`.
3. 없으면 `<assets>/test_data/golden/catalog.json` → `fallback`.

폴백이면 프로세스당 한 번 stderr에 한 줄 경고를 낸다. 실행은 실패하지 않는다.

```
[sonagi-graph] catalog: real catalog not found — using golden fallback .../test_data/golden/catalog.json. Run scripts/sync_catalog.sh to sync the real one.
```

`env.CATALOG_SOURCE`(`"real"` | `"fallback"`)로 호출부에서 분기할 수 있고,
`env.load_paths().as_dict()`에도 `catalog_source`가 남는다.

## 확인

호스트:

```bash
scripts/sync_catalog.sh          # 마지막 줄에 항목 수
python3 -c "import json;print(len(json.load(open('/home/ubuntu/sonagi-assets/catalog/catalog.json'))['items']))"

test ! -d /home/ubuntu/sonagi-assets/library && echo 'local library removed'
python3 - <<'PY'
from graphs.adapters import remote_assets
p = remote_assets.RemotePaths.from_env()
print(remote_assets.healthcheck(paths=p))   # "14938 items" — devops 카탈로그
PY
```

컨테이너 (볼륨 마운트·환경변수·해석 결과를 한 번에 확인):

```bash
docker exec sonagi-graph-langgraph-api-1 python3 -c "
from graphs.core import env
print(env.CATALOG, env.CATALOG_SOURCE)
import json; print(len(json.load(open(env.CATALOG))['items']))
"
```

`real`과 14,938이 나오면 정상이다. `fallback`이면 동기화가 안 된 것이므로
`scripts/sync_catalog.sh`를 실행한다.

카탈로그 메타데이터는 컨테이너에서 열리지만 `path`는 삭제된 로컬 경로다.
렌더가 원격 카탈로그를 쓰는지 확인한다:

```bash
docker exec sonagi-graph-langgraph-api-1 python3 -c "
from graphs.adapters import remote_assets
p = remote_assets.RemotePaths.from_env()
print(p.host, p.root, p.ssh_config)
print(remote_assets.healthcheck(paths=p))
"
```

## 테스트

`test_core.py`가 해석 순서(env → real → golden)와 폴백 경고 1회를 검증한다.
원격 렌더는 `test_remote_assets.py`가 `run` 훅으로 argv·왕복을 검증한다(ssh 불필요).

```bash
.venv/bin/pytest test_core.py -q
```
