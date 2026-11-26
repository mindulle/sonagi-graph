# 웹 렌더 원격 위임 (GRAPH-24)

`llmops`(langgraph 호스트)의 디스크에서 자산 라이브러리(7.2GB, 41,684 files)를
걷어내고, `entry_pack` web 렌더만 **devops**에서 돌린다. 카탈로그·라이브러리는
devops의 `/srv/sonagi-assets`에 있고, 이 호스트에는 렌더 코드와 카탈로그(12MB)만
남는다.

```
llmops (langgraph 컨테이너)
  scripts·styles·tokens·rules ──rsync──▶ devops:/home/ubuntu/sonagi-entry-render
  recipe.json                   ──rsync──▶ .../incoming/<id>/recipe.json
                                  ssh    python3 scripts/render_recipe.py \
                                            --catalog /srv/sonagi-assets/catalog/catalog.json \
                                            --tokens  .../tokens/semantic.json \
                                            --style   .../styles/<style>.json \
                                            --out     .../out/<id>
  out/<id>/  ◀──────────────────rsync──── .../out/<id>/
```

- 원격 카탈로그는 devops 절대경로(`/srv/sonagi-assets/library/...`)를 담으므로
  로컬 카탈로그처럼 경로를 다시 쓰지 않는다.
- 원격 작업 디렉터리는 **전용**이다. mcp-gateway가 `rsync --delete`로 쓰는
  `/home/ubuntu/sonagi-render`와 섞이지 않도록 `sonagi-entry-render`를 쓴다.
- `ensure_synced()`는 `--delete`를 이 전용 루트 안에서만 적용한다.

## 1. 전용 키와 ssh 별칭

`llmops`에서 한 번만 만든다. 일반 `~/.ssh/id_ed25519`는 쓰지 않는다.

```bash
ssh-keygen -t ed25519 -N '' -C 'sonagi-render@llmops' \
  -f ~/.ssh/sonagi_render_ed25519

# devops의 authorized_keys에 공개키 추가(중복 방지)
ssh devops "grep -qxF '$(cat ~/.ssh/sonagi_render_ed25519.pub)' ~/.ssh/authorized_keys || echo '$(cat ~/.ssh/sonagi_render_ed25519.pub)' >> ~/.ssh/authorized_keys"

# 전용 config — 컨테이너에는 /root/.ssh/config로 붙는다
cat > ~/.ssh/sonagi_render_config <<'EOF'
Host devops-render
    HostName 100.82.121.40
    User ubuntu
    IdentityFile ~/.ssh/sonagi_render_ed25519
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking yes
    UserKnownHostsFile ~/.ssh/sonagi_render_known_hosts
    ConnectTimeout 10
    ServerAliveInterval 30
EOF
chmod 600 ~/.ssh/sonagi_render_config

# 전용 known_hosts (호스트명은 실제 접속지 IP)
ssh-keyscan -H 100.82.121.40 > ~/.ssh/sonagi_render_known_hosts
chmod 644 ~/.ssh/sonagi_render_known_hosts
```

확인:

```bash
ssh -F ~/.ssh/sonagi_render_config -o BatchMode=yes devops-render 'hostname; whoami'
# devops
# ubuntu
```

## 2. 컨테이너

`langgraph.json`의 `dockerfile_lines`가 `openssh-client`·`rsync`를 설치한다
(작은 패키지다 — ghostscript/chromium과 다르다). `docker-compose.deploy.yml`이
전용 키·config·known_hosts만 `/root/.ssh`에 read-only로 붙이고 원격 렌더를 켠다.

```yaml
volumes:
  - /home/ubuntu/.ssh/sonagi_render_ed25519:/root/.ssh/sonagi_render_ed25519:ro
  - /home/ubuntu/.ssh/sonagi_render_config:/root/.ssh/config:ro
  - /home/ubuntu/.ssh/sonagi_render_known_hosts:/root/.ssh/sonagi_render_known_hosts:ro
environment:
  SONAGI_REMOTE_RENDER: "1"
  SONAGI_RENDER_HOST: devops-render
  SONAGI_RENDER_ROOT: /home/ubuntu/sonagi-entry-render
  SONAGI_RENDER_CATALOG: /srv/sonagi-assets/catalog/catalog.json
  SONAGI_RENDER_SSH_CONFIG: /root/.ssh/config
  SONAGI_RENDER_TIMEOUT: "300"
```

이미지를 다시 빌드해야 한다(도구 설치 줄이 바뀌었으므로).

```bash
langgraph up -p 8123 --recreate -d docker-compose.deploy.yml
```

## 3. 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SONAGI_REMOTE_RENDER` | `0` | `1`이면 web 렌더를 devops로 보낸다. compose가 켠다 |
| `SONAGI_RENDER_HOST` | `devops-render` | ssh 별칭 |
| `SONAGI_RENDER_ROOT` | `/home/ubuntu/sonagi-entry-render` | 원격 작업 루트(전용) |
| `SONAGI_RENDER_CATALOG` | `/srv/sonagi-assets/catalog/catalog.json` | 원격 카탈로그 |
| `SONAGI_RENDER_SSH_CONFIG` | `~/.ssh/sonagi_render_config` | 컨테이너에서는 `/root/.ssh/config` |
| `SONAGI_RENDER_TIMEOUT` | `300` | ssh/rsync 한 번의 제한(초) |

기본값이 `0`인 이유는 테스트·로컬 실행이 ssh 없이 돌아야 하기 때문이다
(`conftest.py`도 autouse로 끈다). 운영은 compose에서만 켠다.

## 4. 동작

`graphs/profiles/web.py`의 `WebProfile.render()`가 `remote_assets.enabled()`를 보고
분기한다. 원격 경로는 `graphs/adapters/remote_assets.py` 하나에 모여 있다.

- `ensure_synced()` — `scripts·styles·tokens·rules`만 rsync(프로세스당 1회).
  `--delete`는 전용 원격 루트 안에서만 적용된다. 약 1.3MB, 1초 안쪽.
- `render(recipe, style, breakpoint, dark, out_dir)` — 레시피 업로드 → ssh 렌더 →
  산출물(SVG·manifest·CREDITS·raster) 다운로드 → 원격 임시 디렉터리 정리.
- 렌더 전 **자산 preflight**(GRAPH-24) — 레시피 참조를 모아 ssh **한 번**으로
  존재를 확인하고, 없는 참조만 걷어낸 레시피로 렌더한다. 결과에는
  `skipped_assets`와 `preflight {mode, checked, skipped, renderable}`가 붙는다.
  ssh 확인이 실패하면 "전부 누락"으로 넘기지 않고 `PreflightError`로 멈춘다.
- `healthcheck()` — 원격 `render_recipe.py` 존재와 카탈로그 항목 수를 확인한다.
- `run` 훅을 받으므로 테스트는 ssh 없이 argv만 검증한다(`test_remote_assets.py`).

정리(`rm -rf`)는 best-effort다. 실패해도 이미 받은 산출물은 버리지 않는다.
원격 디버깅이 필요하면 `render(..., keep_remote=True)`로 남길 수 있다.

## 5. 확인

호스트(저장소 루트):

```bash
python3 - <<'PY'
from graphs.adapters import remote_assets
p = remote_assets.RemotePaths.from_env()
remote_assets.ensure_synced(paths=p)
print(remote_assets.healthcheck(paths=p))   # "14938 items"
r = remote_assets.render("recipe.json", "editorial", "desktop", dark=False, out_dir="/tmp/remote-check")
print(r["svg"], r["manifest"])
PY
```

컨테이너:

```bash
docker exec sonagi-graph-langgraph-api-1 sh -lc 'command -v ssh rsync'
docker exec sonagi-graph-langgraph-api-1 python3 -c "
from graphs.adapters import remote_assets
p = remote_assets.RemotePaths.from_env()
print(p.host, p.root, p.ssh_config)
print(remote_assets.healthcheck(paths=p))
"
```

`14938 items`가 나오면 원격 카탈로그를 읽은 것이다.

## 6. 라이브러리 동기화 폐기

`scripts/sync_library.sh`는 이제 쓰지 않는다. 실수로 7.2GB를 다시 받지 않도록
`SONAGI_ALLOW_LOCAL_LIBRARY=1` 없이는 즉시 실패한다. 카탈로그만 필요하면
`scripts/sync_catalog.sh`를 쓴다(12MB, 메타데이터 스카우트·import 검증용).

이 호스트에서 `/home/ubuntu/sonagi-assets/library`는 삭제됐다. 로컬 카탈로그의
`path`는 삭제된 로컬 경로를 가리킬 수 있지만, 렌더는 원격 카탈로그를 쓰므로
영향이 없다.
