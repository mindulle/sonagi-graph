# GHCR 이미지 + 호스트 pull 배포 (GRAPH-45)

CI가 master에서 성공하면 GHCR에 이미지를 밀고, 호스트가 5분마다 digest를 확인해
바뀐 것만 당겨서 재배포한다. 배포 방향은 항상 **호스트 → GHCR(pull)** 이다.

## 토폴로지

```
 PR/push (master)
        │
        ▼
 GitHub Actions "CI" (guardrails/tests/brand-tokens/design-tokens)
        │  workflow_run: completed + success + push
        ▼
 GitHub Actions "Image"  (GITHUB_TOKEN, packages:write)
   langgraph build → ghcr.io/mindulle/sonagi-graph:sha-<12>
   docker tag      → ghcr.io/mindulle/sonagi-graph:stable
   push 두 태그
        │
        ▼  (private package, read:packages 필요)
 GHCR
        ▲
        │ 5분마다 digest 확인 (systemd timer, 호스트가 시작)
        │
 /home/ubuntu/sonagi-graph  ── langgraph up --image ghcr.io/...@sha256:...
        │                       + docker-compose.deploy.yml (env/마운트)
        ▼
 sonagi-graph-langgraph-api-1  :8123  (/ok, graph_cli list 11개)
```

- 이미지에는 **앱 코드만** 들어간다. LLM 키, wiki/assets 마운트, 폰트, SSH 키,
  SearXNG 주소 같은 런타임 값은 저장소의 `docker-compose.deploy.yml`과 호스트
  환경이 계속 공급한다.
- 그래서 이미지가 바뀌어도 호스트 compose/마운트는 그대로다.

### 왜 인바운드 러너가 없나

배포 서버에 self-hosted runner를 두면 GitHub이 호스트로 접속하는 경로와, 그
러너가 저장소 코드/시크릿을 실행할 수 있는 권한이 생긴다. 지금 구조는 호스트가
5분마다 바깥으로 나가서 digest만 확인하므로:

- 인바운드 포트/터널/러너 토큰이 없다.
- GHCR push 권한은 CI의 일회성 `GITHUB_TOKEN`에만 있고 호스트에는 없다.
- 호스트에는 read 전용(`read:packages`) 자격 증명만 둔다.

## 이미지 태그와 digest

| 태그 | 용도 |
| --- | --- |
| `sha-<commit 12자>` | 특정 커밋 고정. 롤백/추적용 |
| `stable` | 최신 master CI 성공 이미지. 호스트가 보는 태그 |

`stable`은 매 빌드마다 같은 태그에 새 digest가 붙는다. 호스트는 태그 문자열이
아니라 **digest**로 비교하고, 배포할 때도 `image@sha256:...`로 고정한다.
그래서 확인과 배포 사이에 `stable`이 또 갱신돼도 다른 이미지가 올라가지 않는다.

`langgraph build -t`는 click 옵션이 multiple이 아니라 마지막 값만 남는다. 그래서
워크플로는 sha 태그로 한 번 빌드하고 `docker tag`로 `stable`을 붙인 뒤 둘 다
push한다.

## 호스트 인증 (read:packages)

CI는 `permissions: packages: write`의 `GITHUB_TOKEN`을 쓰므로 별도 설정이 없다.
호스트는 private 패키지를 읽어야 하므로 `read:packages`가 필요하다.

```bash
# 방법 1: gh 토큰에 scope 추가 (현재 토큰은 gist, read:org, repo, workflow만 있음)
gh auth refresh -s read:packages
gh auth token        # deploy 스크립트가 자동으로 사용

# 방법 2: classic PAT (read:packages)를 파일로
sudo install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/.secrets
sudo -u ubuntu sh -c 'umask 077; cat > /home/ubuntu/.secrets/ghcr.env' <<'EOF'
GHCR_USER=mindulle
GHCR_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
EOF
```

- `/home/ubuntu/.secrets/ghcr.env`가 있으면 스크립트가 그것을 우선 사용하고,
  없으면 `gh auth token`을 쓴다.
- fine-grained PAT는 GHCR read에 쓰지 말고 classic PAT(`read:packages`)를 쓴다.
- 확인:

```bash
sudo -u ubuntu docker pull ghcr.io/mindulle/sonagi-graph:stable
```

`denied`가 나오면 scope 문제다. 토큰을 바꾼 뒤 `docker login ghcr.io`를 다시 해야
`~/.docker/config.json`이 갱신된다(스크립트가 매번 수행한다).

## 설치

배포 체크아웃(`/home/ubuntu/sonagi-graph`)에서 root로 실행한다.

```bash
cd /home/ubuntu/sonagi-graph
sudo deploy/install.sh
```

설치기는 순서대로:

1. `ubuntu` 사용자로 `deploy/sonagi-graph-deploy.sh --check` (로그인 + digest 조회)
2. `ubuntu` 사용자로 `docker pull ghcr.io/mindulle/sonagi-graph:stable`
3. 둘 다 성공했을 때만 `/etc/systemd/system`에 유닛 복사, `daemon-reload`,
   `enable --now sonagi-graph-deploy.timer`

1이나 2가 실패하면 유닛을 건드리지 않고 멈춘다. GHCR 인증이 안 되는 상태에서
타이머를 켜서 5분마다 실패 로그를 쌓지 않기 위해서다.

## 수동 운용

```bash
# digest만 확인 (배포 안 함)
sudo -u ubuntu /home/ubuntu/sonagi-graph/deploy/sonagi-graph-deploy.sh --check

# 즉시 1회 (변경 없으면 skip)
sudo -u ubuntu /home/ubuntu/sonagi-graph/deploy/sonagi-graph-deploy.sh

# 같은 digest여도 재배포
sudo -u ubuntu /home/ubuntu/sonagi-graph/deploy/sonagi-graph-deploy.sh --force

# systemd로 1회
sudo systemctl start sonagi-graph-deploy.service
```

`--check`는 `docker login` → digest 조회까지만 한다. 인증이 깨졌으면 여기서
nonzero로 끝나므로 설치기/모니터링에서 그대로 쓸 수 있다.

## 롤백

상태 파일이 직전 성공 digest를 들고 있으므로 한 명령이다.

```bash
sudo -u ubuntu /home/ubuntu/sonagi-graph/deploy/sonagi-graph-deploy.sh --rollback
```

- `--rollback`은 `previous_digest`를 `--image image@digest`로 올리고 health check
  (`/ok` + `graph_cli list` 11개)를 통과하면 성공으로 기록한다. 성공 시 현재
  digest와 previous가 서로 바뀌므로 다시 실행하면 롤백을 되돌린다.
- 새 digest 배포가 실패하면 스크립트가 자동으로 **직전 성공 digest(상태 파일의
  `digest`)** 로 되돌린다. 실패한 digest는 `failed_digest`에 남고, 같은 digest가
  `stable`에 남아 있는 동안은 타이머가 재시도하지 않는다(필요하면 `--force`).

상태 파일: `/home/ubuntu/.sonagi-graph-deploy.json`

```json
{
  "digest": "sha256:...",
  "previous_digest": "sha256:...",
  "image": "ghcr.io/mindulle/sonagi-graph",
  "tag": "stable",
  "deployed_at": "2026-11-26T00:00:00Z",
  "previous_deployed_at": "2026-11-25T23:00:00Z",
  "updated_at": "2026-11-26T00:00:00Z",
  "failed_digest": null,
  "failed_at": null,
  "last_error": null
}
```

## 로그와 타이머

```bash
journalctl -u sonagi-graph-deploy.service -f
tail -f /home/ubuntu/sonagi-graph-deploy.log

systemctl list-timers sonagi-graph-deploy.timer
systemctl status sonagi-graph-deploy.timer

# 끄기 / 켜기
sudo systemctl disable --now sonagi-graph-deploy.timer
sudo systemctl enable --now sonagi-graph-deploy.timer
```

- 타이머: `OnCalendar=*:0/5`, `Persistent=true`, `RandomizedDelaySec=30`.
- 서비스는 `Type=oneshot`, `User=ubuntu`, 15분 타임아웃.
- 로그 파일은 5MB를 넘으면 `sonagi-graph-deploy.log.1`로 한 번 돌린다.
- `flock(/home/ubuntu/.sonagi-graph-deploy.lock)`이라 타이머와 수동 실행이
  겹쳐도 하나만 진행한다. 겹친 쪽은 조용히 skip(exit 0)한다.

## 트러블슈팅

| 증상 | 원인/조치 |
| --- | --- |
| `docker login ghcr.io` denied | 토큰에 `read:packages` 없음. `gh auth refresh -s read:packages` 또는 PAT 파일 |
| `imagetools inspect` 실패 후 pull도 실패 | 위와 같음. `--check`가 nonzero면 타이머를 켜지 않는다 |
| digest는 같은데 코드가 안 바뀜 | CI 실패 시 Image 워크플로가 안 돈다. Actions에서 CI 결론 확인 |
| `stable`이 이전 digest | Image 워크플로 실패 또는 GHCR push 실패. 워크플로 로그 확인 |
| 배포 후 그래프 수 < 11 | `graph_cli list`가 11개가 아니면 실패로 처리하고 자동 롤백 |
| 롤백도 실패 | `journalctl -u sonagi-graph-deploy.service`, `docker compose -p sonagi-graph ps` 확인 |

## 관련 파일

- `.github/workflows/image.yml` — CI 성공 후 이미지 빌드/push
- `deploy/sonagi-graph-deploy.sh` — pull 배포 본체
- `deploy/deploy_state.py` — digest 정규화/상태 전이 (표준 라이브러리)
- `deploy/sonagi-graph-deploy.service`, `.timer` — systemd 유닛
- `deploy/install.sh` — 인증/pull 검증 후 유닛 설치
- `deploy/test_deploy_state.py` — 상태 전이 테스트
- `docker-compose.deploy.yml` — 런타임 env/마운트 (이미지 밖)
