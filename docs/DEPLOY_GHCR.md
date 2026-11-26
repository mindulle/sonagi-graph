# GitHub 릴리스 자산 배포 (GRAPH-45)

CI가 master에서 성공하면 이미지를 **GitHub prerelease 자산**(`graph-image.tar.zst`)으로
올리고, 호스트가 5분마다 최신 `image-<sha12>` 릴리스를 확인해 바뀐 것만 내려받아
재배포한다. 배포 방향은 항상 **호스트 → GitHub(pull)** 이다.

> 파일명은 이전 GHCR 설계를 이어받아 `DEPLOY_GHCR.md`지만, 현재 호스트 전송은
> GHCR이 아니라 릴리스 자산이다. GHCR은 CI의 선택적 미러일 뿐이다.

## 왜 GHCR이 아니라 릴리스 자산인가

- GHCR은 GitHub CLI의 OAuth 토큰을 레지스트리 인증으로 받지 않는다.
  `read:packages`를 추가해도 `docker login ghcr.io`가 `/v2/`에서 401/denied로
  끝났다. GHCR read에는 **classic PAT**가 필요하다.
- 새 PAT를 사용자에게 요구하지 않기로 했다. 이미 호스트에 있는 `gh` 토큰은
  `repo` scope를 가지고 있고, 릴리스 API/자산 다운로드는 그 권한으로 충분하다.
- 대신 이미지 tar를 릴리스 자산으로 올린다. GitHub Release 자산은 파일당 2GB
  제한이 있고, 이 이미지는 비압축 약 1.08GB라 안전하다.

## 토폴로지

```
 PR/push (master)
        │
        ▼
 GitHub Actions "CI" (guardrails/tests/brand-tokens/design-tokens)
        │  workflow_run: completed + success + push
        ▼
 GitHub Actions "Image"  (contents: write, packages: write)
   langgraph build → ghcr.io/mindulle/sonagi-graph:sha-<12>
   docker save | zstd -3 → graph-image.tar.zst
   gh release create image-<12> --prerelease (자산 업로드)
   GHCR push (선택적 미러, 실패해도 무시)
        │
        ▼
 GitHub Releases (private repo, 기존 repo-scoped gh 토큰으로 접근)
        ▲
        │ 5분마다 최신 image-* 태그 확인 (systemd timer)
        │ 새 태그일 때만 자산 다운로드 → zstd -d → docker load
        │
 /home/ubuntu/sonagi-graph ── langgraph up --image <로컬 ref> --no-pull
        │                       + docker-compose.deploy.yml (env/마운트)
        ▼
 sonagi-graph-langgraph-api-1  :8123  (/ok, graph_cli list 11개)
```

- 이미지에는 **앱 코드만** 들어간다. LLM 키, wiki/assets 마운트, 폰트, SSH 키,
  SearXNG 주소 같은 런타임 값은 저장소의 `docker-compose.deploy.yml`과 호스트
  환경이 계속 공급한다.
- `langgraph up`은 기본이 `--pull`이라 GHCR에서 당기려다 실패한다. 로컬에 로드한
  이미지를 쓰므로 배포 스크립트는 **`--no-pull`**을 붙인다.

## 크기와 지연

| 항목 | 값 |
| --- | --- |
| 비압축 이미지 | 약 1.08GB (`docker save` 기준) |
| 릴리스 자산 | `zstd -3` 압축, 보통 400~700MB (빌드마다 다름) |
| 새 릴리스 배포 | 다운로드 + 압축 해제 + `docker load` + 컨테이너 재생성, 보통 1~4분 |
| 변경 없음 | 릴리스 목록 조회만, 수 초 |

5분 타이머보다 다운로드가 길어지면 다음 회차는 systemd가 중복 실행하지 않고
넘어간다. 수동 실행과 겹치면 `flock`으로 한쪽만 진행한다.

## 릴리스 태그와 자산

| 이름 | 용도 |
| --- | --- |
| `image-<commit 12자>` | prerelease 태그. 배포 단위 |
| `graph-image.tar.zst` | `docker save` + zstd 압축 자산 |
| 로컬 ref | `ghcr.io/mindulle/sonagi-graph:sha-<12>` (tar에 들어 있는 태그) |

- prerelease는 GitHub의 "Latest"로 표시되지 않는다.
- 워크플로는 최근 `image-*` 릴리스 10개만 남기고 오래된 것은 태그까지 지운다.
- 호스트는 로컬 이미지를 최근 배포 3개만 남긴다(상태 파일 `history` 기준).
- 수동 정리:

```bash
gh release list --repo mindulle/sonagi-graph --limit 30
gh release delete image-<12> --repo mindulle/sonagi-graph --yes --cleanup-tag

docker images 'ghcr.io/mindulle/sonagi-graph'
docker image rm ghcr.io/mindulle/sonagi-graph:sha-<12>
```

## 인증

CI는 `permissions: contents: write`의 `GITHUB_TOKEN`으로 릴리스를 만든다.
호스트는 이미 로그인된 `gh` 토큰(`repo` scope)을 그대로 쓴다. **GHCR 로그인도,
`read:packages`도, PAT도 필요 없다.**

```bash
gh auth status          # ubuntu 사용자
gh release list --repo mindulle/sonagi-graph --limit 5
```

> 호스트의 gh 2.4에는 `gh release list --json`이 없다. 배포 스크립트는 요청한
> JSON 플래그를 먼저 시도하고, 없으면 `gh api repos/.../releases`로 같은 정보를
> 읽는다. 릴리스 자산 다운로드(`gh release download`)와 조회(`gh release view`)는
> 2.4에서도 동작한다.

## 설치

배포 체크아웃(`/home/ubuntu/sonagi-graph`)에서 root로 실행한다.

```bash
cd /home/ubuntu/sonagi-graph
sudo deploy/install.sh
```

설치기는 순서대로:

1. `ubuntu` 사용자로 `gh auth status`, `gh release list`
2. `deploy/sonagi-graph-deploy.sh --check` (최신 `image-*` 태그 비교, 다운로드 없음)
3. `zstd` 존재, 릴리스 자산 크기 확인, 디스크 여유(자산×3 + 2GB) 확인
4. 모두 성공했을 때만 `/etc/systemd/system`에 유닛 복사, `daemon-reload`,
   `enable --now sonagi-graph-deploy.timer`

하나라도 실패하면 유닛을 건드리지 않고 멈춘다.

## 수동 운용

```bash
# 릴리스 태그만 확인 (다운로드/배포 없음)
sudo -u ubuntu /home/ubuntu/sonagi-graph/deploy/sonagi-graph-deploy.sh --check

# 즉시 1회 (새 태그 없으면 skip)
sudo -u ubuntu /home/ubuntu/sonagi-graph/deploy/sonagi-graph-deploy.sh

# 같은 태그여도 로컬 이미지로 재배포
sudo -u ubuntu /home/ubuntu/sonagi-graph/deploy/sonagi-graph-deploy.sh --force

# systemd로 1회
sudo systemctl start sonagi-graph-deploy.service
```

`--check`는 `gh release list`/`gh api` + 상태 비교만 한다. 토큰이나 릴리스가
깨졌으면 여기서 nonzero로 끝나므로 설치기/모니터링에서 그대로 쓸 수 있다.

## 롤백

상태 파일이 직전 성공 태그와 로컬 ref를 들고 있으므로 한 명령이다.

```bash
sudo -u ubuntu /home/ubuntu/sonagi-graph/deploy/sonagi-graph-deploy.sh --rollback
```

- `--rollback`은 `previous_tag`의 이미지를 로컬에서 찾고, 없으면 해당 릴리스
  자산을 다시 받아 `docker load`한 뒤 `--no-pull`로 올린다. health check
  (`/ok` + `graph_cli list` 11개)를 통과하면 성공으로 기록한다.
- 새 태그 배포가 실패하면 스크립트가 **직전 성공 태그**로 자동 롤백한다.
  실패한 태그는 `failed_tag`에 남고, 같은 태그가 최신인 동안은 타이머가
  재시도하지 않는다(필요하면 `--force`).
- 다운로드/압축 해제 실패는 일시적일 수 있으므로 `failed_tag`로 막지 않는다.
  다음 회차에 다시 시도한다.

상태 파일: `/home/ubuntu/.sonagi-graph-deploy.json`

```json
{
  "tag": "image-a9fb1dbaeddd",
  "previous_tag": "image-0123456789ab",
  "image_ref": "ghcr.io/mindulle/sonagi-graph:sha-a9fb1dbaeddd",
  "previous_image_ref": "ghcr.io/mindulle/sonagi-graph:sha-0123456789ab",
  "digest": "sha256:...",
  "previous_digest": "sha256:...",
  "deployed_at": "2026-11-26T00:00:00Z",
  "previous_deployed_at": "2026-11-25T23:00:00Z",
  "updated_at": "2026-11-26T00:00:00Z",
  "failed_tag": null,
  "failed_at": null,
  "last_error": null,
  "history": ["image-a9fb1dbaeddd", "image-0123456789ab"]
}
```

`digest`는 레지스트리 digest가 아니라 로컬 `docker image inspect`의 이미지 ID다.
릴리스 자산에는 레지스트리 manifest가 없기 때문이다. 동일성 비교는 `tag`로 한다.

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

## 나중에 GHCR로 되돌리려면

릴리스 자산은 PAT 없이 동작하지만, GHCR이 더 빠르고 자산 저장소를 쓰지 않는다.
GHCR read를 쓰려면 **classic PAT**(`read:packages`)가 필요하다.

1. classic PAT를 만들고 `/home/ubuntu/.secrets/ghcr.env`에 둔다.

   ```bash
   sudo install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/.secrets
   sudo -u ubuntu sh -c 'umask 077; cat > /home/ubuntu/.secrets/ghcr.env' <<'EOF'
   GHCR_USER=mindulle
   GHCR_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
   EOF
   ```

2. `docker login ghcr.io -u mindulle --password-stdin < /home/ubuntu/.secrets/ghcr.env`로
   로그인이 되는지 확인한다(`/v2/` 401이 사라져야 한다).
3. `deploy/sonagi-graph-deploy.sh`의 `ensure_image`를 GHCR pull 경로로 바꾸거나,
   이전 커밋(`a9fb1db`)의 GHCR 구현을 참고해 되돌린다. `langgraph up`에서
   `--no-pull`을 빼야 GHCR digest를 당긴다.
4. `deploy/install.sh`에 `docker pull` 검증을 다시 넣는다.

## 트러블슈팅

| 증상 | 원인/조치 |
| --- | --- |
| `gh auth status` 실패 | `sudo -u ubuntu gh auth login` (repo scope) |
| 릴리스 목록 404 | 토큰 repo scope/저장소 접근 확인. private 저장소다 |
| `image-*` 릴리스 없음 | CI/Image 워크플로 결론 확인. master push + CI success여야 한다 |
| 자산 다운로드 실패 | 네트워크, GitHub API rate limit, 릴리스 자산 삭제 여부 확인 |
| 디스크 부족 | `docker image prune`, 오래된 릴리스/로컬 이미지 정리 |
| `langgraph up`이 pull 시도 | `--no-pull` 누락. 스크립트를 최신 master로 갱신 |
| 그래프 수 < 11 | health 실패로 처리하고 직전 태그로 자동 롤백 |
| 롤백도 실패 | `journalctl -u sonagi-graph-deploy.service`, `docker compose -p sonagi-graph ps` |

## 관련 파일

- `.github/workflows/image.yml` — CI 성공 후 이미지 빌드/압축/릴리스 업로드(+GHCR 미러)
- `deploy/sonagi-graph-deploy.sh` — 릴리스 자산 pull 배포 본체
- `deploy/deploy_state.py` — 태그 정규화/상태 전이 (표준 라이브러리)
- `deploy/sonagi-graph-deploy.service`, `.timer` — systemd 유닛
- `deploy/install.sh` — gh/릴리스/zstd/디스크 검증 후 유닛 설치
- `deploy/test_deploy_state.py` — 상태 전이 테스트
- `docker-compose.deploy.yml` — 런타임 env/마운트 (이미지 밖)
