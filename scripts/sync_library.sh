#!/usr/bin/env bash
# devops의 실자산 라이브러리를 그래프 컨테이너가 마운트하는 자산 디렉터리로 동기화한다.
#
#   devops:/srv/sonagi-assets/library/     (약 7.2GB, 41,684 files)
#     -> /home/ubuntu/sonagi-assets/library/
#
# 동기화한 뒤 카탈로그의 에셋 경로도 같은 접두사로 다시 쓴다.
#
#   /srv/sonagi-assets/library/...  ->  /home/ubuntu/sonagi-assets/library/...
#
# 왜 경로를 다시 쓰나
#   카탈로그는 devops에서 만들어져 devops 절대경로(`/srv/sonagi-assets/...`)를 담는다.
#   이 호스트와 컨테이너는 `/home/ubuntu/sonagi-assets`를 마운트하므로, 그대로 두면
#   렌더 단계가 `FileNotFoundError: /srv/sonagi-assets/library/...`로 죽는다.
#
# 안전 규칙
#   - 원격은 읽기만 한다. `--delete`는 로컬 라이브러리에만 적용된다.
#   - 여유 공간이 부족하면 아무것도 지우지 않고 즉시 실패한다.
#   - 카탈로그는 임시 파일에 쓴 뒤 atomic rename 하므로, 실행 중인 그래프가
#     반쯤 쓰인 카탈로그를 읽지 않는다.
#   - 재실행 안전(idempotent). 카탈로그에 바꿀 경로가 없으면 파일을 건드리지
#     않는다. cron에 그대로 걸어도 된다.
#
# 사용:
#   scripts/sync_library.sh
#   SONAGI_LIBRARY_REMOTE_HOST=devops scripts/sync_library.sh
#
# 환경변수(기본값):
#   SONAGI_LIBRARY_REMOTE_HOST   devops
#   SONAGI_LIBRARY_REMOTE_DIR    /srv/sonagi-assets/library
#   SONAGI_ASSETS_DIR            /home/ubuntu/sonagi-assets
#   SONAGI_LIBRARY_LOCAL_DIR     <assets>/library
#   SONAGI_CATALOG               <assets>/catalog/catalog.json
#   SONAGI_LIBRARY_OLD_PREFIX    /srv/sonagi-assets/
#   SONAGI_LIBRARY_NEW_PREFIX    /home/ubuntu/sonagi-assets/
#   SONAGI_LIBRARY_MIN_FREE_GB   원격 크기 + 이 여유분(GB). 기본 1
#   SONAGI_LIBRARY_EXCLUDES       rsync 제외 패턴(공백 구분). 기본 없음
#   SONAGI_LIBRARY_SSH_OPTS       -o BatchMode=yes -o ConnectTimeout=15
set -euo pipefail

REMOTE_HOST="${SONAGI_LIBRARY_REMOTE_HOST:-devops}"
REMOTE_DIR="${SONAGI_LIBRARY_REMOTE_DIR:-/srv/sonagi-assets/library}"
ASSETS_DIR="${SONAGI_ASSETS_DIR:-/home/ubuntu/sonagi-assets}"
LOCAL_LIBRARY="${SONAGI_LIBRARY_LOCAL_DIR:-$ASSETS_DIR/library}"
CATALOG="${SONAGI_CATALOG:-$ASSETS_DIR/catalog/catalog.json}"
OLD_PREFIX="${SONAGI_LIBRARY_OLD_PREFIX:-/srv/sonagi-assets/}"
NEW_PREFIX="${SONAGI_LIBRARY_NEW_PREFIX:-/home/ubuntu/sonagi-assets/}"
MIN_FREE_GB="${SONAGI_LIBRARY_MIN_FREE_GB:-1}"
EXCLUDES="${SONAGI_LIBRARY_EXCLUDES:-}"
SSH_OPTS="${SONAGI_LIBRARY_SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=15}"

log() { printf '[sync_library] %s\n' "$*" >&2; }
die() { printf '[sync_library] ERROR: %s\n' "$*" >&2; exit 1; }

command -v rsync >/dev/null 2>&1 || die "rsync가 설치되어 있지 않습니다"
command -v python3 >/dev/null 2>&1 || die "python3가 필요합니다(경로 재작성·검증)"
command -v ssh >/dev/null 2>&1 || die "ssh가 설치되어 있지 않습니다"
[ -f "$CATALOG" ] || die "카탈로그가 없습니다: $CATALOG (먼저 scripts/sync_catalog.sh 실행)"

TMP_CATALOG=""
# EXIT 트랩은 마지막 명령의 상태를 물려준다. `[ -n ... ] && rm` 형태로 두면
# 성공 경로에서도 `[ -n "" ]`이 1을 반환해 스크립트가 exit 1로 끝난다.
cleanup() {
    if [ -n "$TMP_CATALOG" ]; then
        rm -f "$TMP_CATALOG"
    fi
    return 0
}
trap cleanup EXIT

# shellcheck disable=SC2086  # SSH_OPTS는 의도적으로 단어 분리한다.
ssh_run() { ssh $SSH_OPTS "$REMOTE_HOST" "$@"; }

# 1) 원격 규모 확인. ssh가 막히면 여기서 즉시 실패한다.
REMOTE_BYTES="$(ssh_run "du -sb '$REMOTE_DIR' 2>/dev/null | cut -f1")" \
    || die "원격 크기 조회 실패: $REMOTE_HOST:$REMOTE_DIR"
REMOTE_COUNT="$(ssh_run "find '$REMOTE_DIR' -type f | wc -l")" \
    || die "원격 파일 수 조회 실패: $REMOTE_HOST:$REMOTE_DIR"
case "$REMOTE_BYTES" in ''|*[!0-9]*) die "원격 크기를 읽지 못했습니다: '$REMOTE_BYTES'";; esac
case "$REMOTE_COUNT" in ''|*[!0-9]*) die "원격 파일 수를 읽지 못했습니다: '$REMOTE_COUNT'";; esac
[ "$REMOTE_COUNT" -gt 0 ] || die "원격 라이브러리가 비어 있습니다: $REMOTE_HOST:$REMOTE_DIR"

log "원격: $REMOTE_HOST:$REMOTE_DIR — ${REMOTE_COUNT} files, $(numfmt --to=iec --suffix=B "$REMOTE_BYTES")"

# 2) 여유 공간 선검사. 부족하면 아무것도 지우지 않고 멈춘다(사용자 데이터 보호).
#    재실행 시에는 이미 받은 만큼을 빼고 "추가로 필요한 양"만 본다.
mkdir -p "$ASSETS_DIR"
AVAIL_BYTES="$(df -B1 --output=avail "$ASSETS_DIR" | tail -n1 | tr -d ' ')"
case "$AVAIL_BYTES" in ''|*[!0-9]*) die "여유 공간을 읽지 못했습니다: $ASSETS_DIR";; esac
LOCAL_BYTES_PRE=0
if [ -d "$LOCAL_LIBRARY" ]; then
    LOCAL_BYTES_PRE="$(du -sb "$LOCAL_LIBRARY" | cut -f1)"
    case "$LOCAL_BYTES_PRE" in ''|*[!0-9]*) LOCAL_BYTES_PRE=0;; esac
fi
NEED_ADD=$(( REMOTE_BYTES - LOCAL_BYTES_PRE ))
if [ "$NEED_ADD" -lt 0 ]; then
    NEED_ADD=0
fi
NEED_BYTES=$(( NEED_ADD + MIN_FREE_GB * 1024 * 1024 * 1024 ))
if [ "$AVAIL_BYTES" -lt "$NEED_BYTES" ]; then
    die "여유 공간 부족 — 필요 $(numfmt --to=iec --suffix=B "$NEED_BYTES")(추가 $(numfmt --to=iec --suffix=B "$NEED_ADD") + 여유분 ${MIN_FREE_GB}GB), 사용 가능 $(numfmt --to=iec --suffix=B "$AVAIL_BYTES") on $ASSETS_DIR. 아무것도 삭제하지 않았습니다."
fi
log "여유 공간: $(numfmt --to=iec --suffix=B "$AVAIL_BYTES") (필요 $(numfmt --to=iec --suffix=B "$NEED_BYTES") = 추가 $(numfmt --to=iec --suffix=B "$NEED_ADD") + 여유분 ${MIN_FREE_GB}GB)"

# 3) 라이브러리 동기화. 원격은 읽기 전용이고 --delete는 로컬에만 적용된다.
mkdir -p "$LOCAL_LIBRARY"
RSYNC_ARGS=(-a --delete --timeout=600 --info=stats1)
for pattern in $EXCLUDES; do
    RSYNC_ARGS+=(--exclude "$pattern")
done

log "rsync 시작: $REMOTE_HOST:$REMOTE_DIR/ -> $LOCAL_LIBRARY/"
# shellcheck disable=SC2086
if ! rsync "${RSYNC_ARGS[@]}" -e "ssh $SSH_OPTS" \
    "$REMOTE_HOST:$REMOTE_DIR/" "$LOCAL_LIBRARY/"; then
    die "rsync 실패 — 원격 데이터는 변경되지 않았습니다. 다시 실행하면 이어받는다"
fi

LOCAL_COUNT="$(find "$LOCAL_LIBRARY" -type f | wc -l)"
LOCAL_BYTES="$(du -sb "$LOCAL_LIBRARY" | cut -f1)"
log "로컬: $LOCAL_LIBRARY — ${LOCAL_COUNT} files, $(numfmt --to=iec --suffix=B "$LOCAL_BYTES")"

[ "$LOCAL_COUNT" -eq "$REMOTE_COUNT" ] \
    || die "파일 수 불일치 — 원격 ${REMOTE_COUNT}, 로컬 ${LOCAL_COUNT}. rsync 로그를 확인하라"
[ "$LOCAL_BYTES" -ge "$REMOTE_BYTES" ] \
    || die "크기 불일치 — 원격 $(numfmt --to=iec --suffix=B "$REMOTE_BYTES"), 로컬 $(numfmt --to=iec --suffix=B "$LOCAL_BYTES")"

# 4) 카탈로그 경로 재작성(atomic). 바꿀 경로가 없으면 파일을 건드리지 않는다.
TMP_CATALOG="$(dirname "$CATALOG")/.catalog.json.rw.$$"
CHANGED="$(python3 - "$CATALOG" "$TMP_CATALOG" "$OLD_PREFIX" "$NEW_PREFIX" <<'PY'
import json
import os
import sys

path, tmp_path, old_prefix, new_prefix = sys.argv[1:5]

try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError) as exc:
    sys.exit(f"카탈로그를 읽지 못했습니다: {exc}")

items = data.get("items")
if not isinstance(items, list) or not items:
    sys.exit("카탈로그에 items 배열이 없습니다")

changed = 0
for item in items:
    value = item.get("path")
    if isinstance(value, str) and value.startswith(old_prefix):
        item["path"] = new_prefix + value[len(old_prefix):]
        changed += 1

root = data.get("library_root")
root_changed = isinstance(root, str) and root.startswith(old_prefix)
if root_changed:
    data["library_root"] = new_prefix + root[len(old_prefix):]

if not changed and not root_changed:
    print(0)
    sys.exit(0)

tmp = tmp_path
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    fh.flush()
    os.fsync(fh.fileno())
os.chmod(tmp, os.stat(path).st_mode & 0o7777)
os.replace(tmp, path)          # atomic rename — 독자는 온전한 파일만 본다
print(changed)
PY
)" || die "카탈로그 경로 재작성 실패 (원본 유지)"

if [ "$CHANGED" -gt 0 ]; then
    TMP_CATALOG=""
    log "카탈로그 경로 재작성 — ${CHANGED} paths: ${OLD_PREFIX} -> ${NEW_PREFIX}"
else
    TMP_CATALOG=""
    log "카탈로그 경로 변경 없음 — 이미 ${NEW_PREFIX}를 쓴다"
fi

# 5) 스모크: 카탈로그 경로가 이 호스트에서 실제로 열리는지 표본 확인.
python3 - "$CATALOG" <<'PY' || die "카탈로그 경로 표본 검증 실패"
import json
import sys
from pathlib import Path

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    items = json.load(fh)["items"]

candidates = [item["path"] for item in items if isinstance(item.get("path"), str)][:200]
found = [p for p in candidates if Path(p).is_file()]
if not found:
    sys.exit(f"상위 {len(candidates)}개 경로 중 실제로 존재하는 파일이 없습니다 — 경로 접두사를 확인하라")
print(
    f"[sync_library] 경로 표본 검증 — {len(found)}/{len(candidates)} resolved, "
    f"예: {found[0]}",
    file=sys.stderr,
)
PY

printf '%s\n' "$LOCAL_COUNT"
