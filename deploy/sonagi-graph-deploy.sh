#!/usr/bin/env bash
# sonagi-graph pull 배포 — GHCR의 stable digest를 호스트 compose로 올린다.
#
# 기본 동작(타이머): ghcr.io/mindulle/sonagi-graph:stable의 digest를 확인하고,
# 상태 파일의 digest와 다를 때만 `langgraph up --image <image@digest>`로 재배포한다.
#
#   deploy/sonagi-graph-deploy.sh            # 변경이 있으면 배포
#   deploy/sonagi-graph-deploy.sh --check    # digest 비교만, 배포 없음
#   deploy/sonagi-graph-deploy.sh --force    # 같은 digest여도 재배포
#   deploy/sonagi-graph-deploy.sh --rollback # 상태 파일의 previous_digest로 되돌림
#
# 인증은 /home/ubuntu/.secrets/ghcr.env(GHCR_USER/GHCR_TOKEN)가 있으면 그것을,
# 없으면 `gh auth token`을 쓴다. 토큰에는 read:packages가 필요하다.
#
# 이미지에는 앱 코드만 들어 있다. 런타임 env/마운트는 저장소의
# docker-compose.deploy.yml이 계속 공급하므로, 배포는 "이미지 교체"만 한다.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${SONAGI_GRAPH_REPO:-/home/ubuntu/sonagi-graph}"
IMAGE="${SONAGI_GRAPH_IMAGE:-ghcr.io/mindulle/sonagi-graph}"
TAG="${SONAGI_GRAPH_TAG:-stable}"
STATE_FILE="${SONAGI_GRAPH_STATE:-/home/ubuntu/.sonagi-graph-deploy.json}"
LOG_FILE="${SONAGI_GRAPH_DEPLOY_LOG:-/home/ubuntu/sonagi-graph-deploy.log}"
SECRETS_FILE="${GHCR_ENV_FILE:-/home/ubuntu/.secrets/ghcr.env}"
LOCK_FILE="${SONAGI_GRAPH_LOCK:-/home/ubuntu/.sonagi-graph-deploy.lock}"
HEALTH_URL="${SONAGI_GRAPH_HEALTH_URL:-http://127.0.0.1:8123/ok}"
EXPECTED_GRAPHS="${SONAGI_GRAPH_EXPECTED_GRAPHS:-11}"
DOCKER="${DOCKER:-docker}"
LANGGRAPH="${LANGGRAPH:-langgraph}"
PYTHON="${PYTHON:-python3}"
STATE_HELPER="$SCRIPT_DIR/deploy_state.py"
LOG_MAX_BYTES="${SONAGI_GRAPH_LOG_MAX_BYTES:-5242880}"

usage() {
  cat <<'EOF'
사용법: sonagi-graph-deploy.sh [--check|--force|--rollback]

  (옵션 없음)  stable digest가 상태 파일과 다르면 배포한다.
  --check      GHCR digest와 상태만 비교하고 끝낸다. 배포하지 않는다.
  --force      같은 digest여도 langgraph up을 다시 실행한다.
  --rollback   상태 파일의 previous_digest로 되돌린다.
  -h, --help   이 도움말.

환경 변수로 경로를 바꿀 수 있다(테스트용):
  SONAGI_GRAPH_REPO, SONAGI_GRAPH_IMAGE, SONAGI_GRAPH_TAG,
  SONAGI_GRAPH_STATE, SONAGI_GRAPH_DEPLOY_LOG, GHCR_ENV_FILE,
  SONAGI_GRAPH_LOCK, SONAGI_GRAPH_HEALTH_URL, DOCKER, LANGGRAPH, PYTHON
EOF
}

MODE="deploy"
FORCE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) MODE="check" ;;
    --force) FORCE="1" ;;
    --rollback) MODE="rollback" ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "알 수 없는 옵션: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

mkdir -p "$(dirname "$LOG_FILE")"
if [ -f "$LOG_FILE" ] && [ "$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
  mv -f "$LOG_FILE" "$LOG_FILE.1"
fi

# 로그는 stderr로도 보낸다. systemd는 StandardError=journal로 받고,
# 명령 치환(`$(remote_digest)`)이 로그를 값으로 삼키지 않는다.
log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE" >&2
}

# 동시 실행 방지. 타이머와 수동 실행이 겹치면 한쪽만 진행한다.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "다른 배포가 실행 중입니다. 이번 회차를 건너뜁니다."
  exit 0
fi

for cmd in "$DOCKER" "$PYTHON" curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log "필수 명령이 없습니다: $cmd"
    exit 1
  fi
done
if [ ! -f "$STATE_HELPER" ]; then
  log "상태 헬퍼가 없습니다: $STATE_HELPER"
  exit 1
fi

# GHCR 로그인. 성공하면 0, 실패하면 이유를 남기고 1.
ghcr_login() {
  local user="" token="" output=""
  if [ -r "$SECRETS_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$SECRETS_FILE"
    set +a
    user="${GHCR_USER:-}"
    token="${GHCR_TOKEN:-}"
  fi
  if [ -z "$user" ] || [ -z "$token" ]; then
    if ! command -v gh >/dev/null 2>&1; then
      log "gh CLI가 없고 $SECRETS_FILE도 없습니다."
      return 1
    fi
    if [ -z "$user" ]; then
      user="$(gh api user --jq .login 2>/dev/null || true)"
    fi
    if [ -z "$token" ]; then
      token="$(gh auth token 2>/dev/null || true)"
    fi
  fi
  if [ -z "$user" ] || [ -z "$token" ]; then
    log "GHCR 자격 증명을 찾지 못했습니다. $SECRETS_FILE(GHCR_USER/GHCR_TOKEN)를 만들거나 gh에 로그인하세요."
    return 1
  fi
  if ! output="$(printf '%s' "$token" | "$DOCKER" login ghcr.io -u "$user" --password-stdin 2>&1)"; then
    log "docker login ghcr.io 실패: $(printf '%s' "$output" | tail -n 1)"
    log "read:packages 권한이 필요합니다: gh auth refresh -s read:packages  (또는 read:packages classic PAT를 $SECRETS_FILE에)"
    return 1
  fi
  log "ghcr.io 로그인 성공 ($user)"
}

# 원격 stable 태그의 manifest digest를 stdout으로만 출력한다.
remote_digest() {
  local output="" digest=""
  if output="$("$DOCKER" buildx imagetools inspect "$IMAGE:$TAG" 2>&1)"; then
    digest="$(printf '%s\n' "$output" | awk '/^Digest:[[:space:]]/{print $2; exit}')"
  else
    log "imagetools inspect 실패: $(printf '%s' "$output" | tail -n 1)"
  fi
  if [ -z "$digest" ]; then
    log "pull로 digest를 확인하는 폴백을 씁니다."
    if ! "$DOCKER" pull "$IMAGE:$TAG" >/dev/null 2>>"$LOG_FILE"; then
      log "이미지를 pull할 수 없습니다. GHCR 인증(read:packages)을 확인하세요."
      return 1
    fi
    digest="$("$DOCKER" inspect --format '{{index .RepoDigests 0}}' "$IMAGE:$TAG" | sed 's/.*@//')"
  fi
  "$PYTHON" "$STATE_HELPER" normalize --value "$digest"
}

# langgraph up으로 이미지@digest를 올린다. compose 프로젝트 이름은 디렉터리명
# (sonagi-graph)이라 기존 컨테이너를 그대로 교체한다.
run_langgraph() {
  local digest="$1"
  (
    cd "$REPO_DIR"
    "$LANGGRAPH" up -p 8123 -d docker-compose.deploy.yml \
      --image "$IMAGE@$digest" --wait --recreate
  )
}

health_check() {
  local attempt=0 list="" count=""
  while [ "$attempt" -lt 30 ]; do
    if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
  if [ "$attempt" -ge 30 ]; then
    log "health 실패: $HEALTH_URL 응답이 없습니다."
    return 1
  fi
  if ! list="$(cd "$REPO_DIR" && "$PYTHON" scripts/graph_cli.py list 2>&1)"; then
    log "graph_cli list 실패: $(printf '%s' "$list" | tail -n 1)"
    return 1
  fi
  count="$(printf '%s\n' "$list" | grep -c '[^[:space:]]' || true)"
  if [ "$count" -ne "$EXPECTED_GRAPHS" ]; then
    log "그래프 수가 $EXPECTED_GRAPHS가 아니라 $count입니다."
    return 1
  fi
  log "health OK: /ok 응답, 그래프 ${count}개"
}

deploy_and_verify() {
  local digest="$1"
  log "langgraph up --image $IMAGE@$digest"
  if ! run_langgraph "$digest"; then
    log "langgraph up 실패"
    return 1
  fi
  health_check
}

if ! ghcr_login; then
  exit 1
fi

if [ "$MODE" = "rollback" ]; then
  DEPLOY_DIGEST="$("$PYTHON" "$STATE_HELPER" get --state "$STATE_FILE" --field previous_digest)"
  if [ -z "$DEPLOY_DIGEST" ]; then
    log "롤백 대상(previous_digest)이 없습니다."
    exit 2
  fi
  ACTION="rollback"
  REASON="수동 롤백"
else
  if ! DEPLOY_DIGEST="$(remote_digest)"; then
    log "원격 digest를 확인하지 못했습니다."
    exit 1
  fi
  PLAN_ARGS=(plan --state "$STATE_FILE" --digest "$DEPLOY_DIGEST")
  if [ -n "$FORCE" ]; then
    PLAN_ARGS+=(--force)
  fi
  PLAN_OUT="$("$PYTHON" "$STATE_HELPER" "${PLAN_ARGS[@]}")"
  IFS=$'\t' read -r ACTION REASON <<<"$PLAN_OUT"
fi

if [ "$MODE" = "check" ]; then
  log "[check] $IMAGE:$TAG -> $DEPLOY_DIGEST"
  log "[check] 계획: $ACTION ($REASON)"
  "$PYTHON" "$STATE_HELPER" show --state "$STATE_FILE" 2>&1 | sed 's/^/[state] /' >&2 || true
  exit 0
fi

if [ "$ACTION" = "skip" ]; then
  log "변경 없음: $REASON"
  exit 0
fi
if [ "$ACTION" = "blocked" ]; then
  log "배포 차단: $REASON"
  exit 1
fi

if ! command -v "$LANGGRAPH" >/dev/null 2>&1; then
  log "langgraph CLI가 없습니다: $LANGGRAPH"
  exit 1
fi

log "배포 시도: $DEPLOY_DIGEST ($ACTION: $REASON)"
if deploy_and_verify "$DEPLOY_DIGEST"; then
  "$PYTHON" "$STATE_HELPER" success \
    --state "$STATE_FILE" --digest "$DEPLOY_DIGEST" --image "$IMAGE" --tag "$TAG"
  log "배포 성공: $IMAGE@$DEPLOY_DIGEST"
  exit 0
fi

log "배포 실패: $IMAGE@$DEPLOY_DIGEST"
"$PYTHON" "$STATE_HELPER" failure \
  --state "$STATE_FILE" --digest "$DEPLOY_DIGEST" --error "langgraph up 또는 health check 실패"

# 이번 시도 직전에 정상이던 digest(상태 파일의 digest)로 되돌린다.
ROLLBACK_DIGEST="$("$PYTHON" "$STATE_HELPER" get --state "$STATE_FILE" --field digest)"
if [ -n "$ROLLBACK_DIGEST" ] && [ "$ROLLBACK_DIGEST" != "$DEPLOY_DIGEST" ]; then
  log "이전 digest로 롤백: $ROLLBACK_DIGEST"
  if deploy_and_verify "$ROLLBACK_DIGEST"; then
    log "롤백 성공: $ROLLBACK_DIGEST (상태 파일은 그대로)"
  else
    log "롤백도 실패했습니다. 수동 개입이 필요합니다."
  fi
else
  log "롤백할 이전 digest가 없습니다."
fi
exit 1
