#!/usr/bin/env bash
# 콘테스트 엔트리 한 건을 entry_pack으로 실행하는 얇은 래퍼.
#
#   scripts/contest_entry.sh [--dry-run] [--timeout SEC] <spec.json> <web|print|brand>
#
# - JSON의 기본 키와 profile 규칙을 먼저 검증한다. 최종 검증은 그래프가 한다.
# - `graph_cli.py run entry_pack --wait`를 실행하고 thread_id, 팩 경로,
#   다음에 칠 approve/show 명령을 출력한다.
# - --dry-run은 검증만 하고 실행할 명령을 출력한다.
# - 실행이 실패·시간 초과·예상 밖 상태면 non-zero로 끝난다. HITL 대기는 정상이다.
#
# 환경변수:
#   SONAGI_OUT_ROOT   산출물 루트 최우선 (그래프와 같은 우선순위)
#   SONAGI_ENTRY_OUT  산출물 루트 별칭 (배포 /home/ubuntu/entry_pack, 코드 기본 /tmp/entry_pack)
#   LANGGRAPH_URL     서버 주소 (graph_cli.py가 읽는다)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY_RUN=0
TIMEOUT=300

usage() {
    cat <<'USAGE'
사용: scripts/contest_entry.sh [--dry-run] [--timeout SEC] <spec.json> <web|print|brand>

  --dry-run       JSON 검증만 하고 실행할 명령을 출력한다
  --timeout SEC   그래프 대기 시간(초, 기본 300)
  -h, --help      이 도움말

예:
  scripts/contest_entry.sh --dry-run briefs/acme-web.json web
  scripts/contest_entry.sh briefs/acme-web.json print
USAGE
}

die() {
    printf '[contest_entry] ERROR: %s\n' "$*" >&2
    exit 1
}

# 작은따옴표로 감싼 셸 인자. JSON을 눈에 읽히는 형태로 출력한다.
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
        --timeout)
            [[ $# -ge 2 ]] || die "--timeout 다음에 초가 필요하다"
            TIMEOUT="$2"
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

[[ ${#POSITIONAL[@]} -eq 2 ]] || {
    usage >&2
    exit 2
}

SPEC="${POSITIONAL[0]}"
PROFILE="${POSITIONAL[1]}"

[[ -f "$SPEC" ]] || die "spec 파일이 없다: $SPEC"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die "--timeout은 1 이상의 정수여야 한다: $TIMEOUT"

case "$PROFILE" in
    web|print|brand) ;;
    *)
        die "profile은 web|print|brand 중 하나다: $PROFILE"
        ;;
esac

command -v python3 >/dev/null 2>&1 || die "python3가 필요하다"

# JSON 검증 + profile 주입. 실패하면 python3가 stderr에 이유를 쓰고 non-zero.
# print·brand는 그래프의 profile 검증(package_spec·brand_spec)을 그대로 호출해
# dry-run과 실제 실행의 판정이 갈리지 않게 한다.
payload="$(python3 -W ignore - "$SPEC" "$PROFILE" "$ROOT" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
profile = sys.argv[2]
root = Path(sys.argv[3])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except OSError as exc:
    sys.exit(f"spec 파일을 읽지 못했다: {exc}")
except ValueError as exc:
    sys.exit(f"JSON 파싱 실패: {exc}")

if not isinstance(data, dict):
    sys.exit("spec JSON은 객체여야 한다")

spec = data.get("spec")
if spec is None:
    spec = {}
if not isinstance(spec, dict):
    sys.exit("spec은 객체여야 한다")

declared = spec.get("profile") or spec.get("contest_type")
if declared and declared != profile:
    sys.exit(f"spec.profile({declared})과 인자 profile({profile})이 다르다")


def fail(message):
    sys.exit(f"{profile} profile: {message}")


if profile == "web":
    brief = data.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        brief = data.get("content")
    if not isinstance(brief, str) or not brief.strip():
        fail("brief 또는 content가 필요하다")
    platform = spec.get("platform")
    if not isinstance(platform, str) or not platform.strip():
        fail("spec.platform이 필요하다 (예: 99designs, loudsourcing)")
elif profile == "print":
    package = spec.get("package")
    if not isinstance(package, dict):
        fail("spec.package가 필요하다 (type·brand·product·size_mm)")
    missing = [key for key in ("brand", "product", "size_mm") if key not in package]
    if missing:
        fail(f"spec.package에 필수 항목이 없다: {', '.join(missing)}")
    kind = str(package.get("type") or "label").lower()
    if kind not in ("label", "pouch"):
        fail(f"package.type은 label|pouch다: {kind!r}")
    size = package.get("size_mm")
    if not isinstance(size, list) or len(size) != 2:
        fail("package.size_mm은 [가로mm, 세로mm] 두 값이어야 한다")
    try:
        width, height = float(size[0]), float(size[1])
    except (TypeError, ValueError):
        fail(f"package.size_mm이 숫자가 아니다: {size!r}")
    if width <= 0 or height <= 0:
        fail("package.size_mm은 양수여야 한다")
elif profile == "brand":
    brand = spec.get("brand")
    if not isinstance(brand, dict):
        fail("spec.brand가 필요하다 (mark_path·name·palette)")
    mark = brand.get("mark_path")
    if not mark:
        fail("spec.brand.mark_path가 필요하다")

spec["profile"] = profile
data["spec"] = spec

# profile 정본 검증. print는 최소 재단 크기, brand는 palette·sizes까지 본다.
if profile in ("print", "brand"):
    sys.path.insert(0, str(root))
    try:
        if profile == "print":
            from graphs.profiles.print import package_spec

            validate = package_spec
        else:
            from graphs.profiles.brand import brand_spec

            validate = brand_spec
    except ImportError as exc:
        print(
            f"[contest_entry] 경고: 그래프 의존성 미설치 — 기본 키만 검증한다: {exc}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 - Belt-and-braces: import 오류를 구분한다
        sys.exit(f"profile 검증 모듈을 불러오지 못했다: {exc}")
    else:
        try:
            validate(spec)
        except Exception as exc:  # noqa: BLE001 - ProfileError 메시지를 그대로 보여준다
            sys.exit(str(exc))

print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
PY
)" || exit $?

cmd=(python3 "$ROOT/scripts/graph_cli.py" run entry_pack --input "$payload" --wait --timeout "$TIMEOUT")

if ((DRY_RUN)); then
    printf '# profile: %s\n' "$PROFILE"
    printf 'python3 %q run entry_pack --input ' "$ROOT/scripts/graph_cli.py"
    shell_quote "$payload"
    printf ' --wait --timeout %s\n' "$TIMEOUT"
    exit 0
fi

printf '[contest_entry] entry_pack 실행 (%s, timeout %ss)\n' "$PROFILE" "$TIMEOUT" >&2

set +e
output="$("${cmd[@]}")"
status=$?
set -e

if ((status != 0)); then
    printf '%s\n' "$output" >&2
    die "entry_pack 실행 실패 (exit $status)"
fi

printf '%s\n' "$output"

# CLI는 요약 JSON 한 줄을 먼저 찍는다. 그 줄에서 thread_id·run_status·next를 꺼낸다.
mapfile -t run_fields < <(printf '%s\n' "$output" | python3 -X utf8 -c '
import json
import sys

summary = {}
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        data = json.loads(line)
    except ValueError:
        continue
    if isinstance(data, dict) and "thread_id" in data:
        summary = data
        break
next_nodes = summary.get("next") or []
print(summary.get("run_status") or "")
print(summary.get("thread_id") or "")
print(1 if "hitl" in next_nodes else 0)
')

if ((${#run_fields[@]} < 3)); then
    die "CLI 요약을 파싱하지 못했다. graph_cli.py 출력 형식을 확인하라"
fi
run_status="${run_fields[0]}"
thread_id="${run_fields[1]}"
needs_hitl="${run_fields[2]}"

[[ -n "$thread_id" ]] || die "출력에서 thread_id를 찾지 못했다"

case "$run_status" in
    success)
        ;;
    interrupted)
        if [[ "$needs_hitl" != 1 ]]; then
            die "중단됐지만 HITL 대기가 아니다. show $thread_id 로 상태를 확인하라"
        fi
        ;;
    running|pending|"")
        die "대기 시간이 끝났는데 아직 실행 중이다(run_status=${run_status:-unknown}). show $thread_id 로 확인하고, 필요하면 --timeout을 늘려 다시 실행하라"
        ;;
    error|failed|timeout)
        die "그래프 실행이 $run_status로 끝났다. show $thread_id 로 원인을 확인하라"
        ;;
    *)
        die "예상 밖 run_status=$run_status. show $thread_id 로 확인하라"
        ;;
esac

# 산출 루트: 환경변수 → 배포 경로가 있으면 그곳 → 코드 기본값.
# pack이 만들어진 뒤에는 manifest 경로가 정본이고, 이 값은 HITL 시점의 예상일 뿐이다.
if [[ -n "${SONAGI_OUT_ROOT:-}" ]]; then
    ENTRY_OUT="$SONAGI_OUT_ROOT"
elif [[ -n "${SONAGI_ENTRY_OUT:-}" ]]; then
    ENTRY_OUT="$SONAGI_ENTRY_OUT"
elif [[ -d /home/ubuntu/entry_pack ]]; then
    ENTRY_OUT=/home/ubuntu/entry_pack
else
    ENTRY_OUT=/tmp/entry_pack
fi

show_json="$(python3 "$ROOT/scripts/graph_cli.py" show "$thread_id" --json 2>/dev/null)" || {
    printf '[contest_entry] 경고: show --json 실패 — 팩 경로와 선택지를 상태에서 읽지 못했다\n' >&2
    show_json=""
}

mapfile -t info < <(printf '%s' "$show_json" | python3 -X utf8 -c '
import json
import os
import sys

fallback = sys.argv[1]
try:
    state = json.load(sys.stdin)
except ValueError:
    state = {}

values = state.get("values") or {}
manifests = state.get("manifests") or []
pack_dir = ""
manifest_path = ""
manifest_status = ""
manifest_kind = ""
manifest_artifacts = "0"
pack_ready = "0"

for row in manifests:
    candidate = row.get("manifest_path") or row.get("path") or ""
    if candidate.endswith("/manifest.json"):
        manifest_path = candidate
        pack_dir = candidate[: -len("/manifest.json")] + "/pack"
        manifest_status = row.get("status") or ""
        manifest_kind = row.get("kind") or ""
        manifest_artifacts = str(row.get("artifacts") or 0)
        pack_ready = "1" if os.path.isdir(pack_dir) else "0"
        break

run_id = values.get("run_id") or ""
if not pack_dir and run_id:
    pack_dir = f"{fallback}/{run_id}/pack"

options = values.get("options") or []
selection = [options[0]["id"]] if options and options[0].get("id") else []
approve = (
    json.dumps(
        {"approval_status": "approved", "selection": selection},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if selection
    else ""
)

print(pack_dir)
print(manifest_path)
print(run_id)
print(approve)
print(pack_ready)
print(manifest_status)
print(manifest_kind)
print(manifest_artifacts)
for option in options:
    print(
        "{} | {} | {} {}".format(
            option.get("id") or "-",
            option.get("direction") or "-",
            option.get("style") or "-",
            option.get("theme") or "-",
        )
    )
' "$ENTRY_OUT")

PACK_DIR="${info[0]:-}"
MANIFEST_PATH="${info[1]:-}"
RUN_ID="${info[2]:-}"
APPROVE_VALUES="${info[3]:-}"
PACK_READY="${info[4]:-0}"
MANIFEST_STATUS="${info[5]:-}"
MANIFEST_KIND="${info[6]:-}"
MANIFEST_ARTIFACTS="${info[7]:-0}"

printf '\nthread_id: %s\n' "$thread_id"
if [[ -n "$PACK_DIR" ]]; then
    if [[ "$PACK_READY" == 1 ]]; then
        printf 'pack: %s\n' "$PACK_DIR"
    else
        printf 'pack (승인 후 생성): %s\n' "$PACK_DIR"
    fi
else
    printf 'pack (예상): %s/%s/pack\n' "$ENTRY_OUT" "${RUN_ID:-<run_id>}"
fi
if [[ -n "$MANIFEST_PATH" ]]; then
    printf 'manifest: %s (status: %s, kind: %s, artifacts: %s)\n' \
        "$MANIFEST_PATH" "${MANIFEST_STATUS:--}" "${MANIFEST_KIND:--}" \
        "${MANIFEST_ARTIFACTS:-0}"
    if [[ "$MANIFEST_STATUS" == "warn" ]]; then
        printf '[contest_entry] 경고: manifest status=warn — 게이트 실패가 있다. meta.json의 gates_ok를 확인하고 제출하지 마라\n' >&2
    fi
fi
if [[ -n "$RUN_ID" ]]; then
    printf 'run_id: %s\n' "$RUN_ID"
fi

if ((${#info[@]} > 8)); then
    printf '\noptions:\n'
    for row in "${info[@]:8}"; do
        printf '  %s\n' "$row"
    done
fi

printf '\ninspect: python3 %q show %q --json\n' \
    "$ROOT/scripts/graph_cli.py" "$thread_id"

if [[ "$needs_hitl" == 1 ]]; then
    if [[ -n "$APPROVE_VALUES" ]]; then
        APPROVE_TIMEOUT="$TIMEOUT"
        if ((APPROVE_TIMEOUT < 600)); then
            APPROVE_TIMEOUT=600
        fi
        printf 'approve (selection을 고쳐 쓴다):\n  python3 %q approve %q --graph entry_pack --values ' \
            "$ROOT/scripts/graph_cli.py" "$thread_id"
        shell_quote "$APPROVE_VALUES"
        printf ' --wait --timeout %s\n' "$APPROVE_TIMEOUT"
        printf '  options의 id를 골라 selection 배열을 바꾼 뒤 실행한다.\n'
    else
        printf '[contest_entry] 경고: options가 비어 approve 명령을 만들지 않았다. show --json의 renders·critiques를 확인하라\n' >&2
    fi
fi
