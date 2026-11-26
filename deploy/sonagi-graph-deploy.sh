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
# 이미지에는 앱 코드만 들어 있다. 런타임 env/마운트와 langgraph.json은 저장소의
# docker-compose.deploy.yml과 배포 클론이 계속 공급한다.
#
# 배포 클론(`/home/ubuntu/sonagi-graph-deploy`)은 타이머만 체크아웃/리셋하는
# 트리다. 배포 전에 `image-<sha>` 태그의 커밋을 fetch해 detached로 맞춘 뒤
# `langgraph up`과 `graph_cli list`를 그 트리에서 실행한다. 사람이 쓰는
# 에이전트 체크아웃(`/home/ubuntu/sonagi-graph`)은 건드리지 않는다.

set -euo pipefail

# 배포 클론을 대상 커밋으로 체크아웃하는 과정에서 이 스크립트 파일 자체가
# 바뀔 수 있다. bash는 스크립트를 실행 중에 조금씩 읽으므로, 임시 사본으로
# 한 번 재실행해 실행 중 파일 교체의 영향을 받지 않게 한다.
# (SONAGI_GRAPH_DEPLOY_LIB_ONLY=1로 source하면 함수만 정의하고 멈춘다.)
if [ "${SONAGI_GRAPH_DEPLOY_LIB_ONLY:-}" != "1" ] &&
  [ "${SONAGI_GRAPH_DEPLOY_REEXEC:-}" != "1" ]; then
  _deploy_src="${BASH_SOURCE[0]}"
  _deploy_tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/sonagi-graph-deploy.XXXXXX")"
  cp "$_deploy_src" "$_deploy_tmpdir/sonagi-graph-deploy.sh"
  if [ -f "$(dirname "$_deploy_src")/deploy_state.py" ]; then
    cp "$(dirname "$_deploy_src")/deploy_state.py" "$_deploy_tmpdir/deploy_state.py"
  fi
  SONAGI_GRAPH_DEPLOY_REEXEC=1 SONAGI_GRAPH_DEPLOY_TMPDIR="$_deploy_tmpdir" \
    exec bash "$_deploy_tmpdir/sonagi-graph-deploy.sh" "$@"
fi
if [ -n "${SONAGI_GRAPH_DEPLOY_TMPDIR:-}" ]; then
  trap 'rm -rf "$SONAGI_GRAPH_DEPLOY_TMPDIR"' EXIT
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${SONAGI_GRAPH_REPO:-/home/ubuntu/sonagi-graph-deploy}"
GIT_BRANCH="${SONAGI_GRAPH_BRANCH:-master}"
COMPOSE_PROJECT="${SONAGI_GRAPH_COMPOSE_PROJECT:-sonagi-graph}"
GITHUB_REPO="${SONAGI_GRAPH_GITHUB_REPO:-mindulle/sonagi-graph}"
IMAGE="${SONAGI_GRAPH_IMAGE:-ghcr.io/mindulle/sonagi-graph}"
ASSET_NAME="${SONAGI_GRAPH_ASSET:-graph-image.tar.zst}"
STATE_FILE="${SONAGI_GRAPH_STATE:-/home/ubuntu/.sonagi-graph-deploy.json}"
LOG_FILE="${SONAGI_GRAPH_DEPLOY_LOG:-/home/ubuntu/sonagi-graph-deploy.log}"
LOCK_FILE="${SONAGI_GRAPH_LOCK:-/home/ubuntu/.sonagi-graph-deploy.lock}"
HEALTH_URL="${SONAGI_GRAPH_HEALTH_URL:-http://127.0.0.1:8123/ok}"
# 비워 두면 배포 클론의 langgraph.json에서 계산한다. 테스트/비상용 override.
EXPECTED_GRAPHS="${SONAGI_GRAPH_EXPECTED_GRAPHS:-}"
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

# systemd가 아닌 수동 실행(`sudo -u ubuntu ...`)은 secure_path를 써서
# ~/.local/bin(langgraph CLI)이 PATH에 없다. 있으면 앞에 붙인다.
if [ -n "${HOME:-}" ] && [ -d "$HOME/.local/bin" ]; then
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) PATH="$HOME/.local/bin:$PATH" ;;
  esac
fi
export PATH

usage() {
  cat <<'EOF'
사용법: sonagi-graph-deploy.sh [--check|--force|--rollback]

  (옵션 없음)  최신 image-<sha> 릴리스가 상태와 다르면 배포한다.
  --check      릴리스 태그만 비교하고 끝낸다. 다운로드/배포하지 않는다.
  --force      같은 태그여도 로컬 이미지로 다시 배포한다.
  --rollback   상태 파일의 previous_tag로 되돌린다.
  -h, --help   이 도움말.

환경 변수(테스트/운영):
  SONAGI_GRAPH_REPO, SONAGI_GRAPH_BRANCH, SONAGI_GRAPH_COMPOSE_PROJECT,
  SONAGI_GRAPH_GITHUB_REPO, SONAGI_GRAPH_IMAGE, SONAGI_GRAPH_EXPECTED_GRAPHS,
  SONAGI_GRAPH_STATE, SONAGI_GRAPH_DEPLOY_LOG, SONAGI_GRAPH_LOCK,
  SONAGI_GRAPH_HEALTH_URL, SONAGI_GRAPH_PRUNE_KEEP,
  SONAGI_GRAPH_DOCKER_ROOT, DOCKER, LANGGRAPH, PYTHON, GH, ZSTD
EOF
}

# 로그는 stderr로도 보낸다. systemd는 StandardError=journal로 받고,
# 명령 치환(`$(ensure_image ...)`)이 로그를 값으로 삼키지 않는다.
log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE" >&2
}

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

# image-<sha>에서 sha만 뽑는다. 태그는 이미 정규화됐지만 여기서도 검증한다.
tag_sha() {
  local sha="${1#image-}"
  case "$sha" in
    '' | *[!0-9a-fA-F]*)
      log "릴리스 태그에서 sha를 뽑을 수 없습니다: $1"
      return 1
      ;;
  esac
  if [ "${#sha}" -lt 7 ] || [ "${#sha}" -gt 40 ]; then
    log "릴리스 태그의 sha 길이가 7~40자가 아닙니다: $1"
    return 1
  fi
  printf '%s' "$sha" | tr 'A-F' 'a-f'
}

# 로컬 이미지 ref에서 릴리스 태그를 되돌린다(롤백/기준 이미지용).
tag_from_image_ref() {
  local ref="$1"
  case "$ref" in
    "$IMAGE":sha-*)
      printf 'image-%s' "${ref#"$IMAGE":sha-}"
      ;;
    *) return 1 ;;
  esac
}

# 기대 그래프 수. 배포 클론의 langgraph.json이 정본이고,
# SONAGI_GRAPH_EXPECTED_GRAPHS가 있으면 그 값을 쓴다.
derive_expected_graphs() {
  local config="$REPO_DIR/langgraph.json" count=""
  if [ -n "${SONAGI_GRAPH_EXPECTED_GRAPHS:-}" ]; then
    case "$SONAGI_GRAPH_EXPECTED_GRAPHS" in
      *[!0-9]*)
        log "SONAGI_GRAPH_EXPECTED_GRAPHS가 숫자가 아닙니다: $SONAGI_GRAPH_EXPECTED_GRAPHS"
        return 1
        ;;
    esac
    printf '%s' "$SONAGI_GRAPH_EXPECTED_GRAPHS"
    return 0
  fi
  if [ ! -f "$config" ]; then
    log "langgraph.json이 없습니다: $config (배포 클론 체크아웃을 확인하세요)"
    return 1
  fi
  if ! count="$(
    "$PYTHON" - "$config" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
    graphs = data["graphs"]
    if not isinstance(graphs, dict):
        raise TypeError("graphs가 객체가 아닙니다")
except Exception as exc:  # noqa: BLE001 - 원인을 그대로 보여준다
    print(f"langgraph.json을 읽지 못했습니다: {exc}", file=sys.stderr)
    raise SystemExit(1)
print(len(graphs))
PY
  )"; then
    log "기대 그래프 수를 계산하지 못했습니다: $config"
    return 1
  fi
  case "$count" in
    '' | *[!0-9]*)
      log "기대 그래프 수가 숫자가 아닙니다: '$count' ($config)"
      return 1
      ;;
  esac
  printf '%s' "$count"
}

# 배포 클론을 릴리스 태그의 정확한 커밋으로 맞춘다. 배포 클론은 타이머만
# 리셋하는 트리이므로 --force/detached 체크아웃을 허용한다.
prepare_checkout() {
  local tag="$1" sha="" head=""
  sha="$(tag_sha "$tag")" || return 1
  if ! git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    log "배포 클론이 git 저장소가 아닙니다: $REPO_DIR"
    return 1
  fi
  log "배포 클론 fetch: $tag (origin/$GIT_BRANCH)"
  if ! git -C "$REPO_DIR" fetch --force --prune origin \
    "+refs/heads/$GIT_BRANCH:refs/remotes/origin/$GIT_BRANCH" >&2; then
    log "fetch 실패: origin/$GIT_BRANCH ($REPO_DIR)"
    return 1
  fi
  if ! git -C "$REPO_DIR" cat-file -e "${sha}^{commit}" 2>/dev/null; then
    log "커밋 $sha 가 origin/$GIT_BRANCH 에 없습니다. sha로 직접 fetch합니다."
    if ! git -C "$REPO_DIR" fetch --force origin "$sha" >&2; then
      log "커밋을 fetch하지 못했습니다: $sha ($tag)"
      return 1
    fi
  fi
  if ! git -C "$REPO_DIR" checkout --force --detach "$sha" >&2; then
    log "체크아웃 실패: $sha ($tag)"
    return 1
  fi
  if ! head="$(git -C "$REPO_DIR" rev-parse HEAD)"; then
    log "체크아웃 후 HEAD를 읽지 못했습니다: $REPO_DIR"
    return 1
  fi
  case "$head" in
    "$sha"*) ;;
    *)
      log "체크아웃 검증 실패: HEAD=$head, 기대=$sha ($tag)"
      return 1
      ;;
  esac
  log "배포 클론 체크아웃: $tag -> $head"
}

# 로드한 이미지가 이 호스트에서 실행 가능한 아키텍처인지 확인한다.
# (CI가 amd64를 만들고 호스트가 arm64라서 한 번 서비스가 내려간 적이 있다.)
verify_arch() {
  local ref="$1" host_arch="" image_arch=""
  host_arch="$("$DOCKER" version --format '{{.Server.Arch}}' 2>/dev/null || true)"
  image_arch="$("$DOCKER" image inspect --format '{{.Architecture}}' "$ref" 2>/dev/null || true)"
  if [ -n "$host_arch" ] && [ -n "$image_arch" ] && [ "$host_arch" != "$image_arch" ]; then
    log "아키텍처 불일치: 호스트 $host_arch, 이미지 $image_arch ($ref)"
    return 1
  fi
  return 0
}

# 릴리스 태그의 로컬 이미지 ref를 stdout으로 출력한다. 없으면 릴리스 자산을
# 내려받아 zstd 해제 + docker load까지 한다. 로그는 전부 stderr로 간다.
ensure_image() {
  local tag="$1" ref="" size="" need_kb="" avail_kb="" tmp=""
  ref="$(image_ref_for_tag "$tag")"
  if "$DOCKER" image inspect "$ref" >/dev/null 2>&1; then
    if verify_arch "$ref"; then
      printf '%s' "$ref"
      return 0
    fi
    log "로컬 이미지 아키텍처가 맞지 않아 릴리스 자산으로 다시 받습니다."
    "$DOCKER" image rm "$ref" >/dev/null 2>&1 || true
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
  if ! verify_arch "$ref"; then
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
    # compose 프로젝트 이름은 디렉터리 이름에서 나온다. 배포 클론 이름이
    # 바뀌어도 기존 스택(sonagi-graph)을 그대로 교체하도록 고정한다.
    # 로컬에 로드한 이미지를 쓴다. --pull(기본값)이면 GHCR에서 당기려다 실패한다.
    COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT" \
      "$LANGGRAPH" up -p 8123 -d docker-compose.deploy.yml \
      --image "$ref" --no-pull --wait --recreate
  )
}

health_check() {
  local attempt=0 list="" count=""
  if [ -z "$EXPECTED_GRAPHS" ]; then
    log "기대 그래프 수가 비어 있습니다. langgraph.json 계산 단계를 확인하세요."
    return 1
  fi
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
    log "그래프 수가 ${EXPECTED_GRAPHS}개가 아니라 ${count}개입니다."
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

main() {
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

for cmd in "$DOCKER" "$ZSTD" "$LANGGRAPH" git; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log "필수 명령이 없습니다: $cmd"
    exit 1
  fi
done

log "배포 시도: $DEPLOY_TAG ($ACTION: $REASON)"

if [ ! -f "$REPO_DIR/.env" ]; then
  log "배포 클론에 .env가 없습니다: $REPO_DIR/.env"
  log "install.sh가 기존 체크아웃의 .env를 복사합니다. 없으면 수동으로 채우세요."
  exit 1
fi

# 이미지를 받기 전에 배포 클론을 정확한 커밋으로 맞춘다. 그래야 langgraph.json과
# graph_cli가 이미지와 같은 코드를 본다.
if ! prepare_checkout "$DEPLOY_TAG"; then
  log "배포 클론을 $DEPLOY_TAG 커밋으로 맞추지 못했습니다."
  exit 1
fi
if ! EXPECTED_GRAPHS="$(derive_expected_graphs)"; then
  if [ "$MODE" = "deploy" ]; then
    # 릴리스 자체가 잘못된 경우(예: langgraph.json 없음/깨짐)는 같은 태그를
    # 5분마다 재시도하지 않도록 실패로 기록한다.
    "$PYTHON" "$STATE_HELPER" failure --state "$STATE_FILE" --tag "$DEPLOY_TAG" \
      --error "langgraph.json에서 기대 그래프 수를 계산하지 못했습니다"
  fi
  log "릴리스 메타데이터가 잘못되어 배포하지 않습니다: $DEPLOY_TAG"
  exit 1
fi
log "기대 그래프 수: ${EXPECTED_GRAPHS}개 ($REPO_DIR/langgraph.json)"

if ! IMAGE_REF="$(ensure_image "$DEPLOY_TAG")"; then
  # 다운로드/압축 해제 실패는 일시적일 수 있으므로 failed_tag로 막지 않는다.
  log "이미지를 준비하지 못했습니다: $DEPLOY_TAG"
  exit 1
fi
DIGEST="$("$DOCKER" image inspect --format '{{.Id}}' "$IMAGE_REF")"

# 첫 릴리스 배포라 상태에 이전 태그가 없을 때를 대비해, 지금 실행 중인
# 컨테이너의 이미지를 복구 후보로 기억한다. (아키텍처 사고의 재발 방지)
BASELINE_REF=""
if [ -z "$("$PYTHON" "$STATE_HELPER" get --state "$STATE_FILE" --field tag)" ]; then
  BASELINE_REF="$("$DOCKER" inspect sonagi-graph-langgraph-api-1 --format '{{.Config.Image}}' 2>/dev/null || true)"
  if [ "$BASELINE_REF" = "$IMAGE_REF" ]; then
    BASELINE_REF=""
  fi
fi

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
  if ! prepare_checkout "$ROLLBACK_TAG"; then
    log "롤백 커밋을 배포 클론에 맞추지 못했습니다: $ROLLBACK_TAG"
    exit 1
  fi
  if ! EXPECTED_GRAPHS="$(derive_expected_graphs)"; then
    log "롤백 릴리스의 기대 그래프 수를 계산하지 못했습니다: $ROLLBACK_TAG"
    exit 1
  fi
  log "이전 릴리스로 롤백: $ROLLBACK_TAG ($ROLLBACK_REF, 그래프 ${EXPECTED_GRAPHS}개)"
  if deploy_and_verify "$ROLLBACK_REF"; then
    log "롤백 성공: $ROLLBACK_TAG (상태 파일은 그대로)"
  else
    log "롤백도 실패했습니다. 수동 개입이 필요합니다."
  fi
elif [ -n "$BASELINE_REF" ]; then
  log "상태에 이전 릴리스가 없어 배포 전 이미지로 복구: $BASELINE_REF"
  if ! BASELINE_TAG="$(tag_from_image_ref "$BASELINE_REF")"; then
    log "복구 이미지에서 릴리스 태그를 찾지 못했습니다: $BASELINE_REF"
    exit 1
  fi
  if ! prepare_checkout "$BASELINE_TAG"; then
    log "복구 커밋을 배포 클론에 맞추지 못했습니다: $BASELINE_TAG"
    exit 1
  fi
  if ! EXPECTED_GRAPHS="$(derive_expected_graphs)"; then
    log "복구 릴리스의 기대 그래프 수를 계산하지 못했습니다: $BASELINE_TAG"
    exit 1
  fi
  if deploy_and_verify "$BASELINE_REF"; then
    log "복구 성공: $BASELINE_REF (상태 파일은 비어 있음)"
  else
    log "복구도 실패했습니다. 수동 개입이 필요합니다."
  fi
else
  log "롤백할 이전 릴리스가 없습니다."
fi
exit 1
}

# 테스트/디버깅: `SONAGI_GRAPH_DEPLOY_LIB_ONLY=1 source ...`로 함수만 읽는다.
if [ "${SONAGI_GRAPH_DEPLOY_LIB_ONLY:-}" = "1" ]; then
  # shellcheck disable=SC2317  # source 전용 경로. 직접 실행하면 exit로 끝난다.
  return 0 2>/dev/null || exit 0
fi

main "$@"
