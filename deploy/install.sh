#!/usr/bin/env bash
# sonagi-graph pull 배포 유닛 설치기.
#
#   1) ubuntu 사용자로 GHCR 로그인 + stable digest 확인 (--check)
#   2) ubuntu 사용자로 실제 pull 성공 확인
#   3) 위 둘이 모두 성공했을 때만 systemd 유닛을 복사하고 타이머를 켠다.
#
# 하나라도 실패하면 아무 유닛도 건드리지 않는다. GHCR이 private이므로
# read:packages 권한이 없으면 여기서 멈추는 것이 정상이다.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
EXPECTED_REPO="/home/ubuntu/sonagi-graph"
IMAGE="${SONAGI_GRAPH_IMAGE:-ghcr.io/mindulle/sonagi-graph}"
TAG="${SONAGI_GRAPH_TAG:-stable}"
UNIT_DIR="/etc/systemd/system"
TIMER="sonagi-graph-deploy.timer"
SERVICE="sonagi-graph-deploy.service"

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

for cmd in docker runuser systemctl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log "필수 명령이 없습니다: $cmd"
    exit 1
  fi
done

if ! id ubuntu >/dev/null 2>&1; then
  log "ubuntu 사용자가 없습니다."
  exit 1
fi

# 1) ubuntu 사용자로 인증 + digest 조회. --check는 배포하지 않는다.
log "GHCR 인증과 digest 확인 (ubuntu 사용자)"
if ! runuser -u ubuntu -- "$SCRIPT_DIR/sonagi-graph-deploy.sh" --check; then
  log "GHCR 인증/조회 실패. 타이머를 켜지 않았습니다."
  log "read:packages 권한을 먼저 해결하세요:"
  log "  sudo -u ubuntu gh auth refresh -s read:packages"
  log "  또는 /home/ubuntu/.secrets/ghcr.env 에 GHCR_USER/GHCR_TOKEN(read:packages PAT) 작성"
  exit 1
fi

# 2) 실제 pull까지 성공해야 한다. digest 조회만 되고 blob pull이 막히는 경우를 거른다.
log "이미지 pull 확인: $IMAGE:$TAG"
if ! runuser -u ubuntu -- docker pull "$IMAGE:$TAG"; then
  log "이미지를 pull할 수 없습니다. 타이머를 켜지 않았습니다."
  log "read:packages 권한과 패키지 가시성(private)을 확인하세요."
  exit 1
fi

# 3) 유닛 설치. 여기까지 왔으면 인증과 pull이 검증됐다.
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
