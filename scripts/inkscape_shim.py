#!/usr/bin/env python3
"""press가 부르는 `inkscape`를 VectorCraft로 대체하는 shim.

press_audit.render는 `inkscape IN --export-type=png --export-filename=OUT -w WIDTH`
형태로만 호출한다. 이 shim은 같은 인자를 받아 `vectorcraft-cli convert`로 넘긴다.
이미 크래프트 CLI가 마운트되어 있으므로 inkscape를 이미지에 넣을 필요가 없다.

`--query-all`은 VectorCraft에 대응 명령이 없어 빈 출력으로 끝낸다. press_audit은
그 경우 advisory(A070/A071)만 남기고 실패하지 않는다.
"""

import os
import re
import subprocess
import sys

VECTORCRAFT = os.environ.get("VECTORCRAFT_BIN", "vectorcraft-cli")


def _native_width(svg_path):
    """SVG의 width/viewBox에서 원본 폭을 읽는다. 실패하면 120(프레스 기본 그리드)."""
    try:
        text = open(svg_path, encoding="utf-8").read(4096)
    except OSError:
        return 120.0
    m = re.search(r'\bwidth="([0-9.]+)', text)
    if m:
        return float(m.group(1))
    m = re.search(r'viewBox="[-\d.]+[ ,]+[-\d.]+[ ,]+([0-9.]+)', text)
    if m:
        return float(m.group(1))
    return 120.0


def main(argv):
    if "--query-all" in argv:
        return 0

    svg = None
    out = None
    width = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-w" and i + 1 < len(argv):
            width = float(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--export-filename="):
            out = arg.split("=", 1)[1]
        elif arg.startswith("--export-type="):
            pass
        elif not arg.startswith("-"):
            svg = arg
        i += 1

    if not svg or not out:
        print("inkscape shim: IN/OUT을 찾지 못했습니다", file=sys.stderr)
        return 2

    scale = 1.0
    if width:
        scale = width / _native_width(svg)

    cmd = [VECTORCRAFT, "convert", svg, out, "--scale", str(round(scale, 4))]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        sys.stderr.write(done.stderr[-500:])
    return done.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
