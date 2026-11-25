#!/usr/bin/env bash
# devops의 실카탈로그를 그래프 컨테이너가 마운트하는 자산 디렉터리로 동기화한다.
#
#   devops:/srv/sonagi-assets/catalog/catalog.json
#     -> /home/ubuntu/sonagi-assets/catalog/catalog.json
#
# - ssh/rsync 실패, 빈 파일, JSON 파싱 실패 시 즉시 실패한다(exit != 0).
# - 임시 파일에 받아 검증한 뒤 atomic rename 하므로, 실행 중인 그래프가
#   반쯤 쓰인 카탈로그를 읽지 않는다.
# - 내용이 같으면 파일을 건드리지 않는다(idempotent, cron 안전).
#
# 사용:
#   scripts/sync_catalog.sh
#   SONAGI_CATALOG_REMOTE_HOST=devops scripts/sync_catalog.sh
#
# 환경변수(기본값):
#   SONAGI_CATALOG_REMOTE_HOST  devops
#   SONAGI_CATALOG_REMOTE_PATH  /srv/sonagi-assets/catalog/catalog.json
#   SONAGI_CATALOG_LOCAL_PATH   /home/ubuntu/sonagi-assets/catalog/catalog.json
#   SONAGI_CATALOG_SSH_OPTS     -o BatchMode=yes -o ConnectTimeout=15
set -euo pipefail

REMOTE_HOST="${SONAGI_CATALOG_REMOTE_HOST:-devops}"
REMOTE_PATH="${SONAGI_CATALOG_REMOTE_PATH:-/srv/sonagi-assets/catalog/catalog.json}"
LOCAL_PATH="${SONAGI_CATALOG_LOCAL_PATH:-/home/ubuntu/sonagi-assets/catalog/catalog.json}"
SSH_OPTS="${SONAGI_CATALOG_SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=15}"

log() { printf '[sync_catalog] %s\n' "$*" >&2; }
die() { printf '[sync_catalog] ERROR: %s\n' "$*" >&2; exit 1; }

command -v rsync >/dev/null 2>&1 || die "rsync가 설치되어 있지 않습니다"
command -v python3 >/dev/null 2>&1 || die "python3가 필요합니다(항목 수 검증)"

LOCAL_DIR="$(dirname "$LOCAL_PATH")"
mkdir -p "$LOCAL_DIR"

TMP_PATH="$LOCAL_DIR/.catalog.json.tmp.$$"
cleanup() { rm -f "$TMP_PATH"; }
trap cleanup EXIT

# shellcheck disable=SC2086  # SSH_OPTS는 의도적으로 단어 분리한다.
if ! rsync -a --timeout=120 -e "ssh $SSH_OPTS" \
    "$REMOTE_HOST:$REMOTE_PATH" "$TMP_PATH"; then
    die "$REMOTE_HOST:$REMOTE_PATH 동기화 실패 (ssh/rsync 오류)"
fi

[ -s "$TMP_PATH" ] || die "동기화된 파일이 비어 있습니다: $REMOTE_HOST:$REMOTE_PATH"

# JSON 유효성 + 항목 수 확인. 실패하면 die.
COUNT="$(python3 - "$TMP_PATH" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError) as exc:
    sys.exit(f"JSON 파싱 실패: {exc}")
items = data.get("items")
if not isinstance(items, list):
    sys.exit("카탈로그에 items 배열이 없습니다")
if not items:
    sys.exit("카탈로그 items가 비어 있습니다")
print(len(items))
PY
)" || die "동기화 파일 검증 실패"

if [ -f "$LOCAL_PATH" ] && cmp -s "$TMP_PATH" "$LOCAL_PATH"; then
    log "변경 없음 — $LOCAL_PATH (${COUNT} items)"
    printf '%s\n' "$COUNT"
    exit 0
fi

mv -f "$TMP_PATH" "$LOCAL_PATH"
log "동기화 완료 — $LOCAL_PATH (${COUNT} items)"
printf '%s\n' "$COUNT"
