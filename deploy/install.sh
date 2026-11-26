#!/usr/bin/env bash
# sonagi-graph release-asset 배포 유닛 설치기.
#
#   1) ubuntu 사용자로 gh 인증 / 릴리스 목록 / git fetch / zstd / 디스크 여유 확인
#   2) ubuntu 사용자로 deploy 스크립트 --check (최신 image-* 태그 비교)
#   3) 배포 스크립트/상태 헬퍼를 /usr/local/lib/sonagi-graph-deploy 에 안정 사본으로 설치
#   4) 모두 성공했을 때만 systemd 유닛을 복사하고 타이머를 켠다.
#
# GHCR 로그인은 하지 않는다. 기존 repo-scoped gh 토큰으로 릴리스 자산을 받는다.
# 하나라도 실패하면 유닛을 건드리지 않는다.
#
# 이 스크립트는 배포 클론(`/home/ubuntu/sonagi-graph-deploy`)에서 root로 실행한다.
# 에이전트 체크아웃(`/home/ubuntu/sonagi-graph`)은 읽기 전용으로만 참조한다(.env 복사).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
EXPECTED_REPO="/home/ubuntu/sonagi-graph-deploy"
LEGACY_REPO="/home/ubuntu/sonagi-graph"
GITHUB_REPO="${SONAGI_GRAPH_GITHUB_REPO:-mindulle/sonagi-graph}"
GIT_BRANCH="${SONAGI_GRAPH_BRANCH:-master}"
ASSET_NAME="${SONAGI_GRAPH_ASSET:-graph-image.tar.zst}"
UNIT_DIR="/etc/systemd/system"
TOOL_DIR="/usr/local/lib/sonagi-graph-deploy"
TIMER="sonagi-graph-deploy.timer"
SERVICE="sonagi-graph-deploy.service"
PYTHON="${PYTHON:-python3}"
DOCKER_ROOT="${SONAGI_GRAPH_DOCKER_ROOT:-/var/lib/docker}"
MIN_FREE_KB="${SONAGI_GRAPH_MIN_FREE_KB:-2097152}"

log() {
  printf '[install] %s\n' "$*" >&2
}

if [ "$(id -u)" -ne 0 ]; then
  log "root로 실행하세요: sudo $0"
  exit 1
fi

if [ "$REPO_DIR" != "$EXPECTED_REPO" ]; then
  log "설치 체크아웃이 $EXPECTED_REPO 가 아닙니다: $REPO_DIR"
  log "유닛의 ExecStart가 배포 체크아웃을 가리키므로, 배포 체크아웃에서 실행하세요."
  exit 1
fi

for file in \
  "$SCRIPT_DIR/sonagi-graph-deploy.sh" \
  "$SCRIPT_DIR/deploy_state.py" \
  "$SCRIPT_DIR/$SERVICE" \
  "$SCRIPT_DIR/$TIMER"; do
  if [ ! -f "$file" ]; then
    log "필수 파일이 없습니다: $file"
    exit 1
  fi
done

for cmd in gh docker zstd git runuser systemctl "$PYTHON" df; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log "필수 명령이 없습니다: $cmd"
    exit 1
  fi
done

if ! id ubuntu >/dev/null 2>&1; then
  log "ubuntu 사용자가 없습니다."
  exit 1
fi

as_ubuntu() {
  runuser -u ubuntu -- env HOME=/home/ubuntu "$@"
}

# 배포 클론은 타이머가 체크아웃/리셋하는 유일한 트리다.
if ! as_ubuntu git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  log "배포 클론이 git 저장소가 아닙니다: $REPO_DIR"
  exit 1
fi
log "배포 클론 origin: $(as_ubuntu git -C "$REPO_DIR" config --get remote.origin.url)"

# .env는 추적되지 않는다(.gitignore). 배포 클론에는 한 번 복사해 두면
# 체크아웃/리셋에도 남는다. 없으면 기존 체크아웃에서 읽어 온다.
if [ ! -f "$REPO_DIR/.env" ]; then
  if [ -f "$LEGACY_REPO/.env" ]; then
    install -o ubuntu -g ubuntu -m 0600 "$LEGACY_REPO/.env" "$REPO_DIR/.env"
    log ".env를 $LEGACY_REPO 에서 배포 클론으로 복사했습니다."
  else
    log "배포 클론에 .env가 없습니다. $LEGACY_REPO/.env 도 없으니 수동으로 채우세요."
    exit 1
  fi
fi

# fetch 권한(gh credential helper)을 유닛 설치 전에 확인한다.
if ! as_ubuntu git -C "$REPO_DIR" ls-remote --exit-code origin "refs/heads/$GIT_BRANCH" >/dev/null; then
  log "origin/$GIT_BRANCH fetch 권한이 없습니다. gh auth 상태와 git credential 설정을 확인하세요."
  exit 1
fi

# 1) gh 인증과 릴리스 목록. GHCR 로그인은 하지 않는다.
log "gh 인증 확인 (ubuntu 사용자)"
if ! as_ubuntu gh auth status >&2; then
  log "gh 인증이 필요합니다: sudo -u ubuntu gh auth login"
  exit 1
fi
if ! as_ubuntu gh release list --repo "$GITHUB_REPO" --limit 5 >&2; then
  log "릴리스 목록을 읽지 못했습니다. 토큰 repo scope를 확인하세요."
  exit 1
fi

# 2) deploy 스크립트의 릴리스 태그 비교 (다운로드 없음)
log "릴리스 태그 확인 (--check)"
if ! as_ubuntu "$SCRIPT_DIR/sonagi-graph-deploy.sh" --check; then
  log "릴리스 조회/상태 비교 실패. 타이머를 켜지 않았습니다."
  exit 1
fi

# 3) 디스크 여유. 자산 크기의 3배 + 2GB를 요구한다.
TAG="$(
  as_ubuntu gh api "repos/$GITHUB_REPO/releases?per_page=20" |
    "$PYTHON" -c '
import json
import sys

data = json.load(sys.stdin)
rows = []
for release in data if isinstance(data, list) else []:
    tag = (release.get("tag_name") or "").strip().lower()
    if tag.startswith("image-"):
        rows.append((release.get("created_at") or "", tag))
rows.sort(reverse=True)
print(rows[0][1] if rows else "")
'
)"
if [ -z "$TAG" ]; then
  log "$GITHUB_REPO 에 image-* 릴리스가 없습니다. Image 워크플로를 먼저 확인하세요."
  exit 1
fi

ASSET_SIZE="$(
  as_ubuntu gh release view "$TAG" --repo "$GITHUB_REPO" --json assets |
    "$PYTHON" -c '
import json
import sys

name = sys.argv[1]
data = json.load(sys.stdin)
for asset in data.get("assets", []) if isinstance(data, dict) else []:
    if asset.get("name") == name:
        print(int(asset.get("size", 0)))
        break
else:
    sys.exit(1)
' "$ASSET_NAME"
)" || {
  log "릴리스 $TAG 에 $ASSET_NAME 자산이 없습니다."
  exit 1
}

if [ -d "$DOCKER_ROOT" ]; then
  AVAIL_DOCKER="$(df -Pk "$DOCKER_ROOT" | awk 'NR==2{print $4}')"
else
  AVAIL_DOCKER="$(df -Pk / | awk 'NR==2{print $4}')"
fi
AVAIL_TMP="$(df -Pk /tmp | awk 'NR==2{print $4}')"
if [ "$AVAIL_DOCKER" -lt "$AVAIL_TMP" ]; then
  AVAIL_KB="$AVAIL_DOCKER"
else
  AVAIL_KB="$AVAIL_TMP"
fi
NEED_KB=$((ASSET_SIZE * 3 / 1024 + MIN_FREE_KB))
log "디스크: 여유 ${AVAIL_KB}KB, 필요 ${NEED_KB}KB (자산 $((ASSET_SIZE / 1048576))MiB)"
if [ "$AVAIL_KB" -lt "$NEED_KB" ]; then
  log "디스크 여유가 부족합니다. 타이머를 켜지 않았습니다."
  exit 1
fi

# 4) 안정 실행 사본. 배포 클론은 배포 중 태그 커밋으로 체크아웃되므로, 유닛이
# 클론 안 스크립트를 직접 실행하면 다음 회차가 이전 커밋의 낡은 스크립트로
# 시작할 수 있다(실제로 2026-11-26 07:45에 그렇게 실패했다).
log "배포 도구 설치: $TOOL_DIR"
install -d -m 0755 "$TOOL_DIR"
install -m 0755 "$SCRIPT_DIR/sonagi-graph-deploy.sh" "$TOOL_DIR/sonagi-graph-deploy.sh"
install -m 0644 "$SCRIPT_DIR/deploy_state.py" "$TOOL_DIR/deploy_state.py"

# 5) 유닛 설치. 여기까지 왔으면 gh/릴리스/git/zstd/디스크/도구가 검증됐다.
log "systemd 유닛 복사: $UNIT_DIR"
install -m 0644 "$SCRIPT_DIR/$SERVICE" "$UNIT_DIR/$SERVICE"
install -m 0644 "$SCRIPT_DIR/$TIMER" "$UNIT_DIR/$TIMER"
systemctl daemon-reload
systemctl enable --now "$TIMER"

log "타이머 활성화 완료"
systemctl list-timers "$TIMER" --no-pager || true

cat >&2 <<EOF

다음 단계:
  즉시 1회 배포:   sudo systemctl start $SERVICE
  배포 로그:       journalctl -u $SERVICE -f
                   tail -f /home/ubuntu/sonagi-graph-deploy.log
  타이머 확인:     systemctl list-timers $TIMER
  타이머 끄기:     sudo systemctl disable --now $TIMER
  수동 롤백:       sudo -u ubuntu $SCRIPT_DIR/sonagi-graph-deploy.sh --rollback
EOF
