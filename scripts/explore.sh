#!/usr/bin/env bash
# 여러 시드로 entry_pack을 순차 실행하고 콘택트 시트를 한 폴더에 모은다 (GRAPH-32).
#
#   scripts/explore.sh [옵션] <spec.json> [count]
#
# - 실행마다 spec.seed만 바꾸고, 그래프는 HITL에서 멈춘 채 승인하지 않는다.
# - HITL 상태의 `contact_sheet` 경로를 out 디렉터리로 복사하고 index.md를 쓴다.
# - `--seeds a,b,c` 또는 `--count N`(기본 시드에서 파생)으로 실행 수를 정한다.
# - `--dry-run`은 서버에 붙지 않고 실행할 명령만 출력한다.
#
# 환경변수:
#   LANGGRAPH_URL     서버 주소 (graph_cli.py가 읽는다)
#   SONAGI_OUT_ROOT   산출물 루트 (그래프와 같은 우선순위)
#   SONAGI_ENTRY_OUT  산출물 루트 별칭
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY_RUN=0
OUT_DIR="explore-out"
TIMEOUT=600
COUNT=""
SEEDS_RAW=""
VARIANT_CAP=""

usage() {
    cat <<'USAGE'
사용: scripts/explore.sh [옵션] <spec.json> [count]

  --count N          실행 횟수 (기본 1)
  --seeds a,b,c      시드 목록 (--count 대신)
  --out DIR          콘택트 시트·index.md 출력 폴더 (기본 ./explore-out)
  --timeout SEC      실행별 대기 시간(초, 기본 600)
  --variant-cap N    spec.variant_cap 덮어쓰기 (24~120 권장)
  --dry-run          실행 없이 계획·명령만 출력
  -h, --help         이 도움말

예:
  scripts/explore.sh --dry-run --count 3 briefs/web.json
  scripts/explore.sh --seeds rain-a,rain-b,rain-c --out /tmp/explore briefs/web.json
USAGE
}

die() {
    printf '[explore] ERROR: %s\n' "$*" >&2
    exit 1
}

warn() {
    printf '[explore] 경고: %s\n' "$*" >&2
}

shell_quote() {
    local value="$1"
    value=${value//\'/\'\\\'\'}
    printf "'%s'" "$value"
}

POSITIONAL=()
while (($#)); do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --out)
            [[ $# -ge 2 ]] || die "--out 다음에 폴더가 필요하다"
            OUT_DIR="$2"
            shift 2
            ;;
        --count)
            [[ $# -ge 2 ]] || die "--count 다음에 숫자가 필요하다"
            COUNT="$2"
            shift 2
            ;;
        --seeds)
            [[ $# -ge 2 ]] || die "--seeds 다음에 시드 목록이 필요하다"
            SEEDS_RAW="$2"
            shift 2
            ;;
        --timeout)
            [[ $# -ge 2 ]] || die "--timeout 다음에 초가 필요하다"
            TIMEOUT="$2"
            shift 2
            ;;
        --variant-cap)
            [[ $# -ge 2 ]] || die "--variant-cap 다음에 숫자가 필요하다"
            VARIANT_CAP="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            POSITIONAL+=("$@")
            break
            ;;
        -*)
            usage >&2
            die "알 수 없는 옵션: $1"
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

((${#POSITIONAL[@]} >= 1 && ${#POSITIONAL[@]} <= 2)) || {
    usage >&2
    exit 2
}

SPEC="${POSITIONAL[0]}"
if ((${#POSITIONAL[@]} == 2)); then
    [[ -z "$COUNT" ]] || die "count를 위치 인자와 --count에 동시에 줄 수 없다"
    COUNT="${POSITIONAL[1]}"
fi

[[ -f "$SPEC" ]] || die "spec 파일이 없다: $SPEC"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die "--timeout은 1 이상의 정수여야 한다: $TIMEOUT"
if [[ -n "$COUNT" ]]; then
    [[ "$COUNT" =~ ^[1-9][0-9]*$ ]] || die "count는 1 이상의 정수여야 한다: $COUNT"
fi
if [[ -n "$VARIANT_CAP" ]]; then
    [[ "$VARIANT_CAP" =~ ^[1-9][0-9]*$ ]] || die "--variant-cap은 1 이상의 정수여야 한다: $VARIANT_CAP"
fi

command -v python3 >/dev/null 2>&1 || die "python3가 필요하다"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/explore.XXXXXX")"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# 시드 결정 — spec.seed 우선, 없으면 브리프 sha1 앞 8자(그래프와 같은 규칙).
python3 -W ignore - "$SPEC" "$COUNT" "$SEEDS_RAW" <<'PY' > "$TMP_DIR/seeds.txt" || exit $?
import hashlib
import json
import sys
from pathlib import Path

spec_path = Path(sys.argv[1])
count_arg = sys.argv[2]
seeds_arg = sys.argv[3]

try:
    data = json.loads(spec_path.read_text(encoding="utf-8"))
except OSError as exc:
    sys.exit(f"spec 파일을 읽지 못했다: {exc}")
except ValueError as exc:
    sys.exit(f"JSON 파싱 실패: {exc}")

if not isinstance(data, dict):
    sys.exit("spec JSON은 객체여야 한다")
spec = data.get("spec")
if spec is None:
    spec = {}
    data["spec"] = spec
if not isinstance(spec, dict):
    sys.exit("spec은 객체여야 한다")

profile = spec.get("profile") or spec.get("contest_type") or "web"
if str(profile).lower() != "web":
    sys.exit(f"explore는 web profile 전용이다: {profile!r}")

brief = data.get("brief") or data.get("content")
if not isinstance(brief, str) or not brief.strip():
    sys.exit("brief 또는 content가 필요하다")

base_seed = str(spec.get("seed") or "").strip()
if not base_seed:
    base_seed = hashlib.sha1(brief.encode("utf-8")).hexdigest()[:8]

seeds = [part.strip() for part in seeds_arg.split(",") if part.strip()]
if seeds:
    if count_arg and int(count_arg) != len(seeds):
        sys.exit(f"count({count_arg})와 시드 개수({len(seeds)})가 다르다")
else:
    count = int(count_arg) if count_arg else 1
    if count < 1:
        sys.exit("count는 1 이상이어야 한다")
    seeds = [f"{base_seed}-{index + 1}" for index in range(count)]

for seed in seeds:
    print(seed)
PY

mapfile -t SEEDS < "$TMP_DIR/seeds.txt"
((${#SEEDS[@]} > 0)) || die "실행할 시드가 없다"

build_payload() {
    local seed="$1"
    python3 -W ignore - "$SPEC" "$seed" "$VARIANT_CAP" <<'PY'
import json
import sys
from pathlib import Path

spec_path = Path(sys.argv[1])
seed = sys.argv[2]
variant_cap = sys.argv[3]

data = json.loads(spec_path.read_text(encoding="utf-8"))
spec = data.setdefault("spec", {})
spec["seed"] = seed
if variant_cap:
    spec["variant_cap"] = int(variant_cap)
print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
PY
}

if ((DRY_RUN)); then
    printf '# explore dry-run\n'
    printf '# spec: %s\n' "$SPEC"
    printf '# runs: %s\n' "${#SEEDS[@]}"
    printf '# out: %s\n' "$OUT_DIR"
    printf '# timeout: %s\n' "$TIMEOUT"
    if [[ -n "$VARIANT_CAP" ]]; then
        printf '# variant_cap: %s\n' "$VARIANT_CAP"
    fi
    index=0
    for seed in "${SEEDS[@]}"; do
        index=$((index + 1))
        printf '\n[%s/%s] seed=%s\n' "$index" "${#SEEDS[@]}" "$seed"
        payload="$(build_payload "$seed")" || exit $?
        printf 'python3 %q run entry_pack --input ' "$ROOT/scripts/graph_cli.py"
        shell_quote "$payload"
        printf ' --wait --timeout %s\n' "$TIMEOUT"
        printf '# sheet → %s/%02d-%s.png\n' "$OUT_DIR" "$index" "$seed"
    done
    exit 0
fi

mkdir -p "$OUT_DIR"
ROWS="$TMP_DIR/rows.tsv"
: > "$ROWS"

ENTRY_OUT=""
if [[ -n "${SONAGI_OUT_ROOT:-}" ]]; then
    ENTRY_OUT="$SONAGI_OUT_ROOT"
elif [[ -n "${SONAGI_ENTRY_OUT:-}" ]]; then
    ENTRY_OUT="$SONAGI_ENTRY_OUT"
elif [[ -d /home/ubuntu/entry_pack ]]; then
    ENTRY_OUT=/home/ubuntu/entry_pack
else
    ENTRY_OUT=/tmp/entry_pack
fi

write_index() {
    python3 -W ignore - "$ROWS" "$OUT_DIR/index.md" "$SPEC" "${#SEEDS[@]}" <<'PY'
import sys
from pathlib import Path

rows_path = Path(sys.argv[1])
index_path = Path(sys.argv[2])
spec_path = sys.argv[3]
total = sys.argv[4]

lines = [
    "# Web exploration — contact sheets",
    "",
    f"- spec: `{spec_path}`",
    f"- runs: {total}",
    "- 승인하지 않는다. 각 스레드는 HITL에서 멈춘 상태다.",
    "",
    "| # | seed | thread_id | run_id | renders | sheet |",
    "|---|---|---|---|---|---|",
]
for raw in rows_path.read_text(encoding="utf-8").splitlines():
    if not raw.strip():
        continue
    fields = raw.split("\t")
    if len(fields) < 7:
        continue
    index, seed, thread_id, run_id, renders, _options, sheet = fields[:7]
    if sheet == "-":
        sheet_cell = "—"
    else:
        sheet_cell = f"[{Path(sheet).name}]({sheet})"
    lines.append(
        f"| {index} | `{seed}` | `{thread_id}` | `{run_id}` | {renders} | {sheet_cell} |"
    )
lines.append("")
index_path.write_text("\n".join(lines), encoding="utf-8")
PY
}

write_index

TOTAL="${#SEEDS[@]}"
INDEX=0
for seed in "${SEEDS[@]}"; do
    INDEX=$((INDEX + 1))
    printf '\n[explore] %s/%s seed=%s\n' "$INDEX" "$TOTAL" "$seed" >&2

    payload="$(build_payload "$seed")" || exit $?
    set +e
    output="$(python3 "$ROOT/scripts/graph_cli.py" run entry_pack --input "$payload" --wait --timeout "$TIMEOUT")"
    status=$?
    set -e
    if ((status != 0)); then
        printf '%s\n' "$output" >&2
        die "entry_pack 실행 실패 (seed=$seed, exit $status)"
    fi

    mapfile -t run_fields < <(printf '%s\n' "$output" | python3 -W ignore -c '
import json
import sys

thread_id = ""
run_status = ""
needs_hitl = "0"
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        data = json.loads(line)
    except ValueError:
        continue
    if isinstance(data, dict) and "thread_id" in data:
        thread_id = data.get("thread_id") or ""
        run_status = data.get("run_status") or ""
        needs_hitl = "1" if "hitl" in (data.get("next") or []) else "0"
        break
print(thread_id)
print(run_status)
print(needs_hitl)
')
    thread_id="${run_fields[0]:-}"
    run_status="${run_fields[1]:-}"
    needs_hitl="${run_fields[2]:-0}"
    [[ -n "$thread_id" ]] || die "출력에서 thread_id를 찾지 못했다 (seed=$seed)"
    if [[ "$needs_hitl" != 1 ]]; then
        warn "seed=$seed 실행이 HITL이 아닌 상태(${run_status:-unknown})에서 끝났다 — 시트를 계속 수집한다"
    fi

    show_json="$(python3 "$ROOT/scripts/graph_cli.py" show "$thread_id" --json 2>/dev/null)" || {
        warn "show --json 실패 (seed=$seed) — 시트 경로를 상태에서 읽지 못했다"
        show_json=""
    }
    mapfile -t info < <(printf '%s' "$show_json" | python3 -W ignore -c '
import json
import sys

try:
    state = json.load(sys.stdin)
except ValueError:
    state = {}
values = state.get("values") or {}
print(values.get("contact_sheet") or "")
print(values.get("run_id") or "")
print(len(values.get("renders") or []))
print(len(values.get("options") or []))
')

    sheet="${info[0]:-}"
    run_id="${info[1]:-}"
    renders="${info[2]:-0}"
    options="${info[3]:-0}"

    if [[ -z "$sheet" && -n "$run_id" ]]; then
        candidate="$ENTRY_OUT/$run_id/contact-sheet.png"
        if [[ -f "$candidate" ]]; then
            sheet="$candidate"
        fi
    fi

    safe_seed="${seed//[^A-Za-z0-9._-]/_}"
    sheet_md="-"
    if [[ -n "$sheet" && -f "$sheet" ]]; then
        dest_name="$(printf '%02d' "$INDEX")-$safe_seed.png"
        cp "$sheet" "$OUT_DIR/$dest_name"
        sheet_md="./$dest_name"
        printf '[explore] 시트: %s\n' "$OUT_DIR/$dest_name" >&2
    else
        warn "contact sheet를 찾지 못했다 (seed=$seed, run_id=${run_id:-?})"
    fi

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$INDEX" "$seed" "$thread_id" "$run_id" "$renders" "$options" "$sheet_md" >> "$ROWS"
    write_index
done

printf '\n[explore] 완료 — %s개 실행, index: %s/index.md\n' "$TOTAL" "$OUT_DIR" >&2
