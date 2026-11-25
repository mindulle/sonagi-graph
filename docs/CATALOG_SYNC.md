# 실카탈로그 동기화 (GRAPH-22 · GRAPH-23)

그래프의 레퍼런스 스카우트(`entry_pack`, `asset_craft`)는 자산 카탈로그를 읽는다.
개발용 골든 카탈로그는 **8건**뿐이라 실제 품질의 레퍼런스를 고르지 못한다.
운영 카탈로그는 devops에 있고, 이 호스트의 마운트 경로로 동기화해서 쓴다.

카탈로그의 `path`는 **실제 파일 경로**다. 렌더 단계는 그 경로를 그대로 열기
때문에 카탈로그만 받아서는 부족하고, 라이브러리도 함께 로컬화해야 한다
(`scripts/sync_library.sh`, 아래 [자산 라이브러리 동기화](#자산-라이브러리-동기화-graph-23)).

## 어디에 있나

| 위치 | 역할 |
|---|---|
| `devops:/srv/sonagi-assets/catalog/catalog.json` | 원본 (14,938 items) |
| `/home/ubuntu/sonagi-assets/catalog/catalog.json` | 동기화 사본 — 그래프가 읽는 파일 |
| `devops:/srv/sonagi-assets/library/` | 원본 라이브러리 (7.2GB, 41,684 files) |
| `/home/ubuntu/sonagi-assets/library/` | 동기화 사본 — 카탈로그 `path`가 가리키는 실제 파일 |
| `/home/ubuntu/sonagi-assets/test_data/golden/catalog.json` | 골든 폴백 (8 items) |

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
17 */6 * * * cd /home/ubuntu/sonagi-graph && scripts/sync_catalog.sh && scripts/sync_library.sh
```

동기화 후 그래프 컨테이너는 파일을 매 요청마다 새로 읽으므로 재시작이
필요 없다(`entry_pack`은 import 시 경로만 계산하고, 스카우트 시점에 읽는다).

## 자산 라이브러리 동기화 (GRAPH-23)

카탈로그의 `path`는 실제 파일 경로다. `web`·`print`·`brand` 프로필의 렌더
단계가 그 경로를 그대로 열기 때문에, 카탈로그만 동기화하면 파일이 없어
`FileNotFoundError`로 죽는다.

### 왜 경로를 다시 쓰나

카탈로그는 devops에서 만들어져 devops 절대경로를 담는다.

| | 경로 |
|---|---|
| 카탈로그 원본 | `/srv/sonagi-assets/library/...` |
| 이 호스트·컨테이너 | `/home/ubuntu/sonagi-assets/library/...` |

컨테이너는 `/home/ubuntu/sonagi-assets`만 마운트하므로 `/srv/...`는 컨테이너
안에 존재하지 않는다. `scripts/sync_library.sh`가 동기화 직후 카탈로그의
`/srv/sonagi-assets/`를 `/home/ubuntu/sonagi-assets/`로 바꾼다(`path` +
`library_root`). 임시 파일에 쓴 뒤 atomic rename 하므로 실행 중인 그래프가
반쯤 쓰인 카탈로그를 읽지 않는다.

### 실행

```bash
scripts/sync_library.sh
```

1. `devops:/srv/sonagi-assets/library/` → `/home/ubuntu/sonagi-assets/library/`
   (`rsync -a --delete`. **원격은 읽기만** 하고 `--delete`는 로컬에만 적용된다)
2. 파일 수·크기를 원격과 비교. 불일치면 non-zero로 실패
3. 카탈로그 경로 재작성. 바꾼 경로 수를 stderr에 남긴다
4. 표본 경로(상위 200건 중)가 실제로 열리는지 확인. 하나도 없으면 실패
5. stdout에 로컬 파일 수만 출력

실패는 전부 즉시 non-zero다. 원격 데이터는 어떤 경우에도 변경되지 않는다.

### 디스크

라이브러리는 **약 7.2GB / 41,684 files**다. `--delete`는 로컬에만 적용되므로
원격은 줄지 않는다. 스크립트는 동기화 전에 여유 공간(아직 받지 않은 양 + 1GB)을
검사하고, 부족하면 **아무것도 지우지 않고** 멈춘다.

블록 디바이스 확장은 프로바이더 콘솔 작업이라 스크립트가 하지 않는다. 공간이
부족하면 정리한 뒤 다시 실행한다. 호스트 여유가 8GB 아래로 떨어지면 다른
서비스가 영향을 받으므로, 7.2GB를 받기 전에 `df -h /`로 확인한다.

### 재실행

- rsync는 증분이라 이미 받은 파일은 건너뛴다.
- 카탈로그에 바꿀 경로가 없으면 파일을 건드리지 않는다(idempotent).
- 카탈로그가 먼저 갱신돼야 하므로 `sync_catalog.sh` 다음에 실행한다.

### 한계

카탈로그 14,938건 중 77건은 devops의 k3s 볼륨 경로
(`/var/lib/rancher/k3s/storage/...`)를 가리킨다. 라이브러리 밖의 경로라 이
동기화로는 해결되지 않는다(카탈로그가 이미 `missing_file=77`로 표시한다).
`/srv/...`를 가리키던 14,861건은 재작성 후 모두 로컬 경로가 된다.

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

find /home/ubuntu/sonagi-assets/library -type f | wc -l   # 41684
du -sh /home/ubuntu/sonagi-assets/library                 # 7.2G
python3 -c "
import json
items = json.load(open('/home/ubuntu/sonagi-assets/catalog/catalog.json'))['items']
print(sum(1 for i in items if i['path'].startswith('/srv/')))   # 0 — 재작성 완료
"
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

카탈로그 경로가 컨테이너 안에서 실제 파일로 열리는지:

```bash
docker exec sonagi-graph-langgraph-api-1 python3 -c "
import json, os
items = json.load(open('/home/ubuntu/sonagi-assets/catalog/catalog.json'))['items']
sample = items[:500]
print(sum(os.path.isfile(i['path']) for i in sample), '/', len(sample))
"
```

## 테스트

`test_core.py`가 해석 순서(env → real → golden)와 폴백 경고 1회를 검증한다.
`scripts/sync_library.sh`는 rsync·ssh가 필요해 단위 테스트 대상이 아니다. 대신
경로 재작성 로직은 표본 검증 단계에서 매 실행마다 확인된다.

```bash
.venv/bin/pytest test_core.py -q
```
