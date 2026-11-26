#!/usr/bin/env bash
# sonagi-graph release-asset 배포 — GitHub prerelease의 이미지 tar를 호스트로 당긴다.
#
# GHCR은 GitHub CLI OAuth 토큰을 레지스트리 인증으로 받지 않는다(401). PAT를
# 새로 받지 않기로 했으므로, CI가 만든 이미지를 `image-<sha12>` prerelease의
# `graph-image.tar.zst` 자산으로 올리고 호스트는 기존 repo-scoped gh 토큰으로
# 릴리스 API에서 내려받는다.
#
#   deploy/sonagi-graph-deploy.sh            # 새 릴리스 태그가 있으면 배포
#   deploy/sonagi-graph-deploy.sh --check    # 릴리스 태그 비교만, 다운로드 없음
#   deploy/sonagi-graph-deploy.sh --force    # 같은 태그여도 재배포
#   deploy/sonagi-graph-deploy.sh --rollback # 상태 파일의 previous_tag로 되돌림
#
# 이미지에는 앱 코드만 들어 있다. 런타임 env/마운트는 저장소의
# docker-compose.deploy.yml이 계속 공급한다.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${SONAGI_GRAPH_REPO:-/home/ubuntu/sonagi-graph}"
GITHUB_REPO="${SONAGI_GRAPH_GITHUB_REPO:-mindulle/sonagi-graph}"
IMAGE="${SONAGI_GRAPH_IMAGE:-ghcr.io/mindulle/sonagi-graph}"
ASSET_NAME="${SONAGI_GRAPH_ASSET:-graph-image.tar.zst}"
STATE_FILE="${SONAGI_GRAPH_STATE:-/home/ubuntu/.sonagi-graph-deploy.json}"
LOG_FILE="${SONAGI_GRAPH_DEPLOY_LOG:-/home/ubuntu/sonagi-graph-deploy.log}"
LOCK_FILE="${SONAGI_GRAPH_LOCK:-/home/ubuntu/.sonagi-graph-deploy.lock}"
HEALTH_URL="${SONAGI_GRAPH_HEALTH_URL:-http://127.0.0.1:8123/ok}"
EXPECTED_GRAPHS="${SONAGI_GRAPH_EXPECTED_GRAPHS:-11}"
DOCKER="${DOCKER:-docker}"
DOCKER_ROOT="${SONAGI_GRAPH_DOCKER_ROOT:-/var/lib/docker}"
LANGGRAPH="${LANGGRAPH:-langgraph}"
PYTHON="${PYTHON:-python3}"
GH="${GH:-gh}"
ZSTD="${ZSTD:-zstd}"
STATE_HELPER="$SCRIPT_DIR/deploy_state.py"
LOG_MAX_BYTES="${SONAGI_GRAPH_LOG_MAX_BYTES:-5242880}"
# 자산 크기의 3배 + 2GB 여유를 요구한다(다운로드 + 압축 해제 + docker load).
MIN_FREE_KB="${SONAGI_GRAPH_MIN_FREE_KB:-2097152}"
PRUNE_KEEP="${SONAGI_GRAPH_PRUNE_KEEP:-3}"

usage() {
  cat <<'EOF'
사용법: sonagi-graph-deploy.sh [--check|--force|--rollback]

  (옵션 없음)  최신 image-<sha> 릴리스가 상태와 다르면 배포한다.
  --check      릴리스 태그만 비교하고 끝낸다. 다운로드/배포하지 않는다.
  --force      같은 태그여도 로컬 이미지로 다시 배포한다.
  --rollback   상태 파일의 previous_tag로 되돌린다.
  -h, --help   이 도움말.

환경 변수(테스트/운영):
  SONAGI_GRAPH_REPO, SONAGI_GRAPH_GITHUB_REPO, SONAGI_GRAPH_IMAGE,
  SONAGI_GRAPH_STATE, SONAGI_GRAPH_DEPLOY_LOG, SONAGI_GRAPH_LOCK,
  SONAGI_GRAPH_HEALTH_URL, SONAGI_GRAPH_PRUNE_KEEP,
  SONAGI_GRAPH_DOCKER_ROOT, DOCKER, LANGGRAPH, PYTHON, GH, ZSTD
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
# 명령 치환(`$(ensure_image ...)`)이 로그를 값으로 삼키지 않는다.
log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE" >&2
}

# 동시 실행 방지. 타이머와 수동 실행이 겹치면 한쪽만 진행한다.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "다른 배포가 실행 중입니다. 이번 회차를 건너뜁니다."
  exit 0
fi

for cmd in "$GH" "$PYTHON" curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log "필수 명령이 없습니다: $cmd"
    exit 1
  fi
done
if [ ! -f "$STATE_HELPER" ]; then
  log "상태 헬퍼가 없습니다: $STATE_HELPER"
  exit 1
fi

if ! "$GH" auth status >/dev/null 2>&1; then
  log "gh 인증이 필요합니다: gh auth login (repo scope)"
  exit 1
fi

# gh 2.4에는 `release list --json`이 없다. 있으면 그대로 쓰고, 없으면
# releases REST API로 같은 정보를 얻는다. 둘 다 JSON 배열을 stdout에 낸다.
list_releases() {
  local out=""
  if out="$("$GH" release list --repo "$GITHUB_REPO" --limit 20 --json tagName,createdAt,isPrerelease 2>/dev/null)"; then
    printf '%s' "$out"
  else
    "$GH" api "repos/$GITHUB_REPO/releases?per_page=20"
  fi
}

# 가장 최근 생성된 image-* 릴리스 태그를 stdout으로만 출력한다.
newest_tag() {
  list_releases | "$PYTHON" -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(data, list):
    sys.exit(1)
rows = []
for release in data:
    tag = (release.get("tagName") or release.get("tag_name") or "").strip().lower()
    created = release.get("createdAt") or release.get("created_at") or ""
    if tag.startswith("image-"):
        rows.append((created, tag))
rows.sort(key=lambda row: row[0], reverse=True)
print(rows[0][1] if rows else "")
'
}

# 릴리스 자산(graph-image.tar.zst) 크기(byte)를 stdout으로 출력한다.
release_asset_size() {
  local tag="$1" payload=""
  if payload="$("$GH" release view "$tag" --repo "$GITHUB_REPO" --json assets 2>/dev/null)"; then
    printf '%s' "$payload" | "$PYTHON" -c '
import json
import sys

name = sys.argv[1]
data = json.load(sys.stdin)
assets = data.get("assets", []) if isinstance(data, dict) else []
for asset in assets:
    if asset.get("name") == name:
        print(int(asset.get("size", 0)))
        break
else:
    sys.exit(1)
' "$ASSET_NAME"
  else
    "$GH" api "repos/$GITHUB_REPO/releases/tags/$tag" | "$PYTHON" -c '
import json
import sys

name = sys.argv[1]
data = json.load(sys.stdin)
assets = data.get("assets", []) if isinstance(data, dict) else []
for asset in assets:
    if asset.get("name") == name:
        print(int(asset.get("size", 0)))
        break
else:
    sys.exit(1)
' "$ASSET_NAME"
  fi
}

image_ref_for_tag() {
  printf '%s:sha-%s' "$IMAGE" "${1#image-}"
}

# 릴리스 태그의 로컬 이미지 ref를 stdout으로 출력한다. 없으면 릴리스 자산을
# 내려받아 zstd 해제 + docker load까지 한다. 로그는 전부 stderr로 간다.
ensure_image() {
  local tag="$1" ref="" size="" need_kb="" avail_kb="" tmp=""
  ref="$(image_ref_for_tag "$tag")"
  if "$DOCKER" image inspect "$ref" >/dev/null 2>&1; then
    printf '%s' "$ref"
    return 0
  fi

  size="$(release_asset_size "$tag")" || {
    log "릴리스 $tag 에 $ASSET_NAME 자산이 없습니다."
    return 1
  }
  need_kb=$((size * 3 / 1024 + MIN_FREE_KB))
  if [ -d "$DOCKER_ROOT" ]; then
    avail_kb="$(df -Pk "$DOCKER_ROOT" | awk 'NR==2{print $4}')"
  else
    avail_kb="$(df -Pk / | awk 'NR==2{print $4}')"
  fi
  if [ "${avail_kb:-0}" -lt "$need_kb" ]; then
    log "디스크 여유 부족: ${avail_kb}KB < ${need_kb}KB (자산 $((size / 1048576))MiB)"
    return 1
  fi

  tmp="$(mktemp -d "${TMPDIR:-/tmp}/sonagi-graph-image.XXXXXX")"
  log "릴리스 자산 다운로드: $tag/$ASSET_NAME ($((size / 1048576))MiB)"
  if ! "$GH" release download "$tag" --repo "$GITHUB_REPO" --pattern "$ASSET_NAME" -D "$tmp" >&2; then
    log "릴리스 자산 다운로드 실패: $tag"
    rm -rf "$tmp"
    return 1
  fi
  if [ ! -f "$tmp/$ASSET_NAME" ]; then
    log "다운로드한 자산이 없습니다: $tmp/$ASSET_NAME"
    rm -rf "$tmp"
    return 1
  fi

  log "압축 해제 + docker load: $ref"
  if ! "$ZSTD" -d -c "$tmp/$ASSET_NAME" | "$DOCKER" load >&2; then
    log "zstd/docker load 실패: $tag"
    rm -rf "$tmp"
    return 1
  fi
  rm -rf "$tmp"

  if ! "$DOCKER" image inspect "$ref" >/dev/null 2>&1; then
    log "로드 후 이미지 ref를 찾지 못했습니다: $ref"
    return 1
  fi
  printf '%s' "$ref"
}

# 로컬에 쌓인 sha-* 이미지를 최근 배포 3개만 남긴다. 실행 중 컨테이너가 쓰는
# 이미지는 history에 있으므로 지우지 않는다.
prune_local_images() {
  local keep=() tag="" keep_it="" item=""
  mapfile -t keep < <("$PYTHON" "$STATE_HELPER" history --state "$STATE_FILE" --limit "$PRUNE_KEEP")
  while IFS= read -r tag; do
    [ -n "$tag" ] || continue
    keep_it=0
    for item in "${keep[@]}"; do
      if [ "sha-${item#image-}" = "$tag" ]; then
        keep_it=1
        break
      fi
    done
    if [ "$keep_it" -eq 0 ]; then
      log "로컬 이미지 정리: $IMAGE:$tag"
      "$DOCKER" image rm "$IMAGE:$tag" >/dev/null 2>&1 || true
    fi
  done < <("$DOCKER" images --format '{{.Tag}}' "$IMAGE" 2>/dev/null | grep -E '^sha-[0-9a-f]+$' || true)
}

run_langgraph() {
  local ref="$1"
  (
    cd "$REPO_DIR"
    # 로컬에 로드한 이미지를 쓴다. --pull(기본값)이면 GHCR에서 당기려다 실패한다.
    "$LANGGRAPH" up -p 8123 -d docker-compose.deploy.yml \
      --image "$ref" --no-pull --wait --recreate
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
  local ref="$1"
  log "langgraph up --image $ref --no-pull"
  if ! run_langgraph "$ref"; then
    log "langgraph up 실패"
    return 1
  fi
  health_check
}

if [ "$MODE" = "rollback" ]; then
  DEPLOY_TAG="$("$PYTHON" "$STATE_HELPER" get --state "$STATE_FILE" --field previous_tag)"
  if [ -z "$DEPLOY_TAG" ]; then
    log "롤백 대상(previous_tag)이 없습니다."
    exit 2
  fi
  ACTION="rollback"
  REASON="수동 롤백"
else
  if ! DEPLOY_TAG="$(newest_tag)"; then
    log "릴리스 목록을 가져오지 못했습니다."
    exit 1
  fi
  if [ -z "$DEPLOY_TAG" ]; then
    log "$GITHUB_REPO 에 image-* 릴리스가 없습니다."
    exit 1
  fi
  if ! DEPLOY_TAG="$("$PYTHON" "$STATE_HELPER" normalize --value "$DEPLOY_TAG")"; then
    log "릴리스 태그 형식이 잘못됐습니다."
    exit 1
  fi
  PLAN_ARGS=(plan --state "$STATE_FILE" --tag "$DEPLOY_TAG")
  if [ -n "$FORCE" ]; then
    PLAN_ARGS+=(--force)
  fi
  PLAN_OUT="$("$PYTHON" "$STATE_HELPER" "${PLAN_ARGS[@]}")"
  IFS=$'\t' read -r ACTION REASON <<<"$PLAN_OUT"
fi

if [ "$MODE" = "check" ]; then
  log "[check] $GITHUB_REPO -> $DEPLOY_TAG"
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

for cmd in "$DOCKER" "$ZSTD" "$LANGGRAPH"; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log "필수 명령이 없습니다: $cmd"
    exit 1
  fi
done

log "배포 시도: $DEPLOY_TAG ($ACTION: $REASON)"
if ! IMAGE_REF="$(ensure_image "$DEPLOY_TAG")"; then
  # 다운로드/압축 해제 실패는 일시적일 수 있으므로 failed_tag로 막지 않는다.
  log "이미지를 준비하지 못했습니다: $DEPLOY_TAG"
  exit 1
fi
DIGEST="$("$DOCKER" image inspect --format '{{.Id}}' "$IMAGE_REF")"

if deploy_and_verify "$IMAGE_REF"; then
  "$PYTHON" "$STATE_HELPER" success \
    --state "$STATE_FILE" --tag "$DEPLOY_TAG" --image-ref "$IMAGE_REF" --digest "$DIGEST"
  prune_local_images
  log "배포 성공: $DEPLOY_TAG ($IMAGE_REF)"
  exit 0
fi

log "배포 실패: $DEPLOY_TAG ($IMAGE_REF)"
"$PYTHON" "$STATE_HELPER" failure \
  --state "$STATE_FILE" --tag "$DEPLOY_TAG" --error "langgraph up 또는 health check 실패"

# 이번 시도 직전에 정상이던 릴리스 태그로 되돌린다. 로컬에 있으면 바로 쓰고,
# 없으면(정리 대상이었으면) 릴리스 자산에서 다시 로드한다.
ROLLBACK_TAG="$("$PYTHON" "$STATE_HELPER" get --state "$STATE_FILE" --field previous_tag)"
ROLLBACK_REF="$("$PYTHON" "$STATE_HELPER" get --state "$STATE_FILE" --field previous_image_ref)"
if [ -n "$ROLLBACK_TAG" ] && [ "$ROLLBACK_TAG" != "$DEPLOY_TAG" ]; then
  if [ -z "$ROLLBACK_REF" ]; then
    if ! ROLLBACK_REF="$(ensure_image "$ROLLBACK_TAG")"; then
      log "롤백 이미지를 준비하지 못했습니다: $ROLLBACK_TAG"
      exit 1
    fi
  fi
  log "이전 릴리스로 롤백: $ROLLBACK_TAG ($ROLLBACK_REF)"
  if deploy_and_verify "$ROLLBACK_REF"; then
    log "롤백 성공: $ROLLBACK_TAG (상태 파일은 그대로)"
  else
    log "롤백도 실패했습니다. 수동 개입이 필요합니다."
  fi
else
  log "롤백할 이전 릴리스가 없습니다."
fi
exit 1
