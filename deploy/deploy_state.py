#!/usr/bin/env python3
"""sonagi-graph release-asset 배포 상태 헬퍼.

호스트는 GHCR 대신 GitHub prerelease의 `image-<sha12>` 태그와
`graph-image.tar.zst` 자산을 배포 단위로 쓴다. 이 파일은 태그 비교와
상태 전이만 맡는다. 표준 라이브러리만 쓰고, 상태 파일은 임시 파일에 쓴 뒤
`os.replace`로 교체한다.

상태 파일 예시 (/home/ubuntu/.sonagi-graph-deploy.json):

    {
      "tag": "image-a9fb1dbaeddd",
      "previous_tag": "image-0123456789ab",
      "image_ref": "ghcr.io/mindulle/sonagi-graph:sha-a9fb1dbaeddd",
      "previous_image_ref": "ghcr.io/mindulle/sonagi-graph:sha-0123456789ab",
      "digest": "sha256:...",              # docker image ID (로컬 내용 digest)
      "previous_digest": "sha256:...",
      "deployed_at": "2026-11-26T00:00:00Z",
      "previous_deployed_at": "...",
      "updated_at": "...",
      "failed_tag": null,                  # 마지막으로 실패한 릴리스 태그
      "failed_at": null,
      "last_error": null,
      "history": ["image-a9fb1dbaeddd", "image-0123456789ab"]
    }

CLI는 bash에서 쓰기 좋게 한 줄/탭 구분 출력만 낸다.

    deploy_state.py normalize --value image-a9fb1dbaeddd
    deploy_state.py plan --state FILE --tag image-... [--force]
    deploy_state.py success --state FILE --tag image-... --image-ref REF --digest SHA
    deploy_state.py failure --state FILE --tag image-... --error "메시지"
    deploy_state.py get --state FILE --field tag
    deploy_state.py history --state FILE --limit 3
    deploy_state.py show --state FILE
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

_TAG_RE = re.compile(r"^image-([0-9a-f]{7,40})$", re.IGNORECASE)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_tag(value):
    """`image-<7~40 hex>`만 허용하고 소문자로 정규화한다."""
    tag = (value or "").strip().lower()
    if not _TAG_RE.match(tag):
        raise ValueError(f"image-<sha> 태그가 아니다: {value!r}")
    return tag


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


def plan(state, tag, force=False):
    """(action, reason)을 돌려준다. action은 deploy/skip/blocked."""
    tag = normalize_tag(tag)
    if force:
        return "deploy", "--force"
    if state.get("failed_tag") == tag:
        return "blocked", "이전에 실패한 릴리스 태그 (--force로만 재시도)"
    current = state.get("tag")
    if not current:
        return "deploy", "첫 배포"
    if current == tag:
        return "skip", "이미 같은 릴리스 태그가 배포됨"
    return "deploy", f"릴리스 태그 변경 {current} -> {tag}"


def _history(state, tag=None):
    values = state.get("history")
    if not isinstance(values, list):
        values = []
    tags = [t for t in values if isinstance(t, str) and t.startswith("image-")]
    if tag:
        tags = [t for t in tags if t != tag]
        tags.insert(0, tag)
    # 중복 제거 + 최근 10개만 유지
    seen = set()
    unique = []
    for item in tags:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique[:10]


def record_success(state, tag, image_ref, digest, at=None):
    tag = normalize_tag(tag)
    at = at or utc_now()
    current = state.get("tag")
    previous_tag = state.get("previous_tag")
    previous_ref = state.get("previous_image_ref")
    previous_digest = state.get("previous_digest")
    previous_at = state.get("previous_deployed_at")
    if current and current != tag:
        # 같은 태그를 --force로 다시 올린 경우엔 롤백 대상을 덮어쓰지 않는다.
        previous_tag = current
        previous_ref = state.get("image_ref")
        previous_digest = state.get("digest")
        previous_at = state.get("deployed_at")
    return {
        "tag": tag,
        "previous_tag": previous_tag,
        "image_ref": image_ref,
        "previous_image_ref": previous_ref,
        "digest": digest,
        "previous_digest": previous_digest,
        "deployed_at": at,
        "previous_deployed_at": previous_at,
        "updated_at": at,
        "failed_tag": None,
        "failed_at": None,
        "last_error": None,
        "history": _history(state, tag),
    }


def record_failure(state, tag, error, at=None):
    """현재 배포 상태는 그대로 두고 실패한 태그만 기록한다(롤백 후 상태 복원)."""
    tag = normalize_tag(tag)
    at = at or utc_now()
    updated = dict(state)
    updated.update(
        {
            "failed_tag": tag,
            "failed_at": at,
            "last_error": (error or "")[:500],
            "updated_at": at,
        }
    )
    updated["history"] = _history(state)
    return updated


def _cmd_plan(args):
    action, reason = plan(load_state(args.state), args.tag, force=args.force)
    print(f"{action}\t{reason}")


def _cmd_success(args):
    state = load_state(args.state)
    save_state(
        args.state,
        record_success(state, args.tag, args.image_ref, args.digest, at=args.at),
    )


def _cmd_failure(args):
    state = load_state(args.state)
    save_state(
        args.state,
        record_failure(state, args.tag, args.error, at=args.at),
    )


def _cmd_get(args):
    value = load_state(args.state).get(args.field)
    print(value if isinstance(value, str) else "")


def _cmd_history(args):
    state = load_state(args.state)
    for tag in _history(state)[: args.limit]:
        print(tag)


def _cmd_show(args):
    print(
        json.dumps(load_state(args.state), ensure_ascii=False, indent=2, sort_keys=True)
    )


def _cmd_normalize(args):
    try:
        print(normalize_tag(args.value))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="릴리스 태그를 비교한다")
    p_plan.add_argument("--state", required=True)
    p_plan.add_argument("--tag", required=True)
    p_plan.add_argument("--force", action="store_true")
    p_plan.set_defaults(func=_cmd_plan)

    p_success = sub.add_parser("success", help="성공 상태를 기록한다")
    p_success.add_argument("--state", required=True)
    p_success.add_argument("--tag", required=True)
    p_success.add_argument("--image-ref", required=True)
    p_success.add_argument("--digest", required=True)
    p_success.add_argument("--at", default=None)
    p_success.set_defaults(func=_cmd_success)

    p_failure = sub.add_parser("failure", help="실패한 태그를 기록한다")
    p_failure.add_argument("--state", required=True)
    p_failure.add_argument("--tag", required=True)
    p_failure.add_argument("--error", default="")
    p_failure.add_argument("--at", default=None)
    p_failure.set_defaults(func=_cmd_failure)

    p_get = sub.add_parser("get", help="상태 필드 하나를 출력한다")
    p_get.add_argument("--state", required=True)
    p_get.add_argument("--field", required=True)
    p_get.set_defaults(func=_cmd_get)

    p_history = sub.add_parser("history", help="최근 배포 태그를 출력한다")
    p_history.add_argument("--state", required=True)
    p_history.add_argument("--limit", type=int, default=3)
    p_history.set_defaults(func=_cmd_history)

    p_show = sub.add_parser("show", help="상태 파일 전체를 출력한다")
    p_show.add_argument("--state", required=True)
    p_show.set_defaults(func=_cmd_show)

    p_norm = sub.add_parser("normalize", help="릴리스 태그를 정규화한다")
    p_norm.add_argument("--value", required=True)
    p_norm.set_defaults(func=_cmd_normalize)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
