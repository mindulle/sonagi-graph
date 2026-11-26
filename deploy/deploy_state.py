#!/usr/bin/env python3
"""sonagi-graph pull 배포 상태 헬퍼.

`deploy/sonagi-graph-deploy.sh`가 digest 비교와 상태 전이를 이 파일에 맡긴다.
표준 라이브러리만 쓰고, 상태 파일은 같은 디렉터리의 임시 파일에 쓴 뒤
`os.replace`로 교체한다(중간에 죽어도 반쯤 쓴 JSON이 남지 않는다).

상태 파일 예시 (/home/ubuntu/.sonagi-graph-deploy.json):

    {
      "digest": "sha256:...",            # 지금 컨테이너가 쓰는 이미지
      "previous_digest": "sha256:...",   # 롤백 대상
      "image": "ghcr.io/mindulle/sonagi-graph",
      "tag": "stable",
      "deployed_at": "2026-11-26T00:00:00Z",
      "previous_deployed_at": "...",
      "updated_at": "...",
      "failed_digest": null,             # 마지막으로 실패한 digest (자동 재시도 금지)
      "failed_at": null,
      "last_error": null
    }

CLI는 bash에서 쓰기 좋게 한 줄/탭 구분 출력만 낸다.

    deploy_state.py normalize --value 'ghcr.io/x@sha256:...'
    deploy_state.py plan --state FILE --digest sha256:... [--force]
    deploy_state.py success --state FILE --digest sha256:... --image IMAGE --tag stable
    deploy_state.py failure --state FILE --digest sha256:... --error "메시지"
    deploy_state.py get --state FILE --field digest
    deploy_state.py show --state FILE
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_DIGEST_RE = re.compile(r"sha256:([0-9a-fA-F]{64})", re.IGNORECASE)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_digest(value):
    """`repo@sha256:...`, `sha256:...`, 공백 섞인 값을 `sha256:<64hex>`로 만든다."""
    match = _DIGEST_RE.search(value or "")
    if not match:
        raise ValueError(f"sha256 digest가 아니다: {value!r}")
    return "sha256:" + match.group(1).lower()


def load_state(path):
    """없거나 깨진 상태 파일은 빈 dict로 본다. 배포를 막지 않는 쪽을 택한다."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path, state):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".sonagi-graph-deploy.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def plan(state, digest, force=False):
    """(action, reason)을 돌려준다. action은 deploy/skip/blocked."""
    digest = normalize_digest(digest)
    if force:
        return "deploy", "--force"
    if state.get("failed_digest") == digest:
        return "blocked", "이전에 실패한 digest (--force로만 재시도)"
    current = state.get("digest")
    if not current:
        return "deploy", "첫 배포"
    if current == digest:
        return "skip", "이미 같은 digest가 배포됨"
    return "deploy", f"digest 변경 {current} -> {digest}"


def record_success(state, digest, image, tag, at=None):
    digest = normalize_digest(digest)
    at = at or utc_now()
    current = state.get("digest")
    previous = state.get("previous_digest")
    previous_at = state.get("previous_deployed_at")
    if current and current != digest:
        # 같은 digest를 --force로 다시 올린 경우엔 롤백 대상을 덮어쓰지 않는다.
        previous = current
        previous_at = state.get("deployed_at")
    return {
        "digest": digest,
        "previous_digest": previous,
        "image": image,
        "tag": tag,
        "deployed_at": at,
        "previous_deployed_at": previous_at,
        "updated_at": at,
        "failed_digest": None,
        "failed_at": None,
        "last_error": None,
    }


def record_failure(state, digest, error, at=None):
    """현재 배포 상태는 그대로 두고 실패한 digest만 기록한다(롤백 후 상태 복원)."""
    digest = normalize_digest(digest)
    at = at or utc_now()
    updated = dict(state)
    updated.update(
        {
            "failed_digest": digest,
            "failed_at": at,
            "last_error": (error or "")[:500],
            "updated_at": at,
        }
    )
    return updated


def _cmd_plan(args):
    action, reason = plan(load_state(args.state), args.digest, force=args.force)
    print(f"{action}\t{reason}")


def _cmd_success(args):
    state = load_state(args.state)
    save_state(
        args.state,
        record_success(state, args.digest, args.image, args.tag, at=args.at),
    )


def _cmd_failure(args):
    state = load_state(args.state)
    save_state(
        args.state,
        record_failure(state, args.digest, args.error, at=args.at),
    )


def _cmd_get(args):
    value = load_state(args.state).get(args.field)
    print(value if isinstance(value, str) else "")


def _cmd_show(args):
    print(
        json.dumps(load_state(args.state), ensure_ascii=False, indent=2, sort_keys=True)
    )


def _cmd_normalize(args):
    try:
        print(normalize_digest(args.value))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="digest 비교만 한다")
    p_plan.add_argument("--state", required=True)
    p_plan.add_argument("--digest", required=True)
    p_plan.add_argument("--force", action="store_true")
    p_plan.set_defaults(func=_cmd_plan)

    p_success = sub.add_parser("success", help="성공 상태를 기록한다")
    p_success.add_argument("--state", required=True)
    p_success.add_argument("--digest", required=True)
    p_success.add_argument("--image", required=True)
    p_success.add_argument("--tag", default="stable")
    p_success.add_argument("--at", default=None)
    p_success.set_defaults(func=_cmd_success)

    p_failure = sub.add_parser("failure", help="실패한 digest를 기록한다")
    p_failure.add_argument("--state", required=True)
    p_failure.add_argument("--digest", required=True)
    p_failure.add_argument("--error", default="")
    p_failure.add_argument("--at", default=None)
    p_failure.set_defaults(func=_cmd_failure)

    p_get = sub.add_parser("get", help="상태 필드 하나를 출력한다")
    p_get.add_argument("--state", required=True)
    p_get.add_argument("--field", required=True)
    p_get.set_defaults(func=_cmd_get)

    p_show = sub.add_parser("show", help="상태 파일 전체를 출력한다")
    p_show.add_argument("--state", required=True)
    p_show.set_defaults(func=_cmd_show)

    p_norm = sub.add_parser("normalize", help="digest 문자열을 정규화한다")
    p_norm.add_argument("--value", required=True)
    p_norm.set_defaults(func=_cmd_normalize)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
