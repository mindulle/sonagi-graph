---
title: Contest Entry Run (Kimaki)
description: >
  콘테스트 엔트리를 entry_pack으로 만들고 HITL로 고른 뒤 납품 팩을 검수한다.
  그래프를 직접 실행하지 않고 scripts/contest_entry.sh와 scripts/graph_cli.py만 쓴다.
---

# 콘테스트 엔트리 런 루틴

99designs·라우드소싱 같은 디자인 콘테스트에 낼 엔트리를 `entry_pack`으로 만들고,
사람이 후보를 고른 뒤 팩을 검수해 제출한다. 플랫폼 규칙과 제출 전 점검은
`docs/CONTEST_PLAYBOOK.md`를 따른다.

## 1. 입력

`brief`는 한 줄 브리프, `content`는 긴 본문이 있을 때만 쓴다. `spec.profile`이
비어 있으면 `contest_entry.sh`가 인자로 받은 profile을 채운다. 둘이 다르면 실행
전에 멈춘다.

### web — 웹·랜딩·포스터

```json
{
  "brief": "핸드메이드 티 브랜드 랜딩 — 차분하고 고급스러운, 웹",
  "spec": {
    "profile": "web",
    "platform": "99designs",
    "themes": ["light", "dark"],
    "variant_cap": 12
  }
}
```

- `brief` 또는 `content` 중 하나는 필수다.
- `spec.platform`은 필수다(`99designs`, `loudsourcing` 등). 팩 `meta.json`에 남는다.
  그래프 자체는 platform을 선택 항목으로 두지만, 콘테스트 런에서는 래퍼가 필수로
  검사한다.
- web은 기본으로 knowledge·LLM 방향 생성을 거친다. 배포 컨테이너는 렌더를
  devops에 위임한다(`SONAGI_REMOTE_RENDER=1`, `docs/REMOTE_RENDER.md`). 로컬
  실행은 로컬 렌더다. 빨리 돌릴 때만 `skip_knowledge`·`skip_llm`을 쓴다.

### print — 라벨·파우치 전개도

```json
{
  "brief": "차 브랜드 라벨 — calm",
  "spec": {
    "profile": "print",
    "platform": "loudsourcing",
    "package": {
      "type": "label",
      "brand": "Sonagi Tea",
      "product": "Earl Grey 50g",
      "size_mm": [90, 120],
      "mark_path": "/home/ubuntu/briefs/sonagi-mark.svg",
      "colors": {"ink": "#161616", "paper": "#fbf9f5", "accent": "#b4551f"}
    }
  }
}
```

- `spec.package`에 `brand`·`product`·`size_mm`(가로·세로 mm 두 값)가 필수다.
- `type`은 `label` 또는 `pouch`(기본 `label`).
- `mark_path`는 선택이지만 주면 존재하는 파일이어야 한다(SVG 권장).
- `motif`는 `auto`(기본) 또는 `none`이다. `auto`는 `mark_path`가 있으면 같은
  마크 SVG를 재료와 바코드 사이 빈 밴드 가운데에 큰 워터마크(라벨 폭의 50~58%,
  불투명도 0.08)로 놓고, 없으면 완전한 원이 아닌 이중 호(라벨 폭의 40%,
  불투명도 0.12~0.18)를 그린다. `none`은 아무것도 그리지 않아 이전 레이아웃을
  그대로 재현한다. 어느 쪽이든 라벨 manifest의 `middle.mode`에
  `mark-watermark`·`arc`·`notes`·`none` 중 하나와 `opacity`·`bbox`가 남는다.
- `notes`는 선택이다. 문자열 또는 짧은 줄 목록을 주면 모티프 대신 중간 밴드
  가운데에 작은 글씨(최대 3줄)로 놓는다. 3줄을 넘으면 접은 뒤 줄임표로 자르고
  라벨 manifest의 `middle.truncated`에 기록한다.
- print는 네트워크·LLM 없이 결정론으로 돈다. `print_ready`를 켜지 않는 한
  콘테스트 제출용 표현물(도련 미적용)만 만든다.

`notes`를 쓰는 최소 예(모티프 대신 중간 밴드에 문구를 놓는다):

```json
{
  "brief": "차 브랜드 라벨 — calm",
  "spec": {
    "profile": "print",
    "platform": "loudsourcing",
    "package": {
      "type": "label",
      "brand": "Sonagi Tea",
      "product": "Earl Grey 50g",
      "size_mm": [90, 120],
      "notes": ["우아한 향", "차분한 하루"]
    }
  }
}
```

### brand — 로고·마크

```json
{
  "brief": "소나기 브랜드 마크",
  "spec": {
    "profile": "brand",
    "platform": "99designs",
    "brand": {
      "name": "sonagi",
      "mark_path": "/home/ubuntu/briefs/mark.svg",
      "palette": {"ink": "#161616", "paper": "#fbf9f5", "accent": "#b4551f"},
      "sizes": [16, 32, 64, 256, 512],
      "backgrounds": ["light", "dark"]
    }
  }
}
```

- `spec.brand.mark_path`가 필수다. SVG만 받는다.
- 배포 컨테이너는 `/home/ubuntu/briefs`와 디자인 시스템 `packages/brand-assets`를
  읽기 전용으로 마운트한다. 브리프·마크는 호스트의 이 경로에 두어야 그래프가
  파일을 찾는다. 그 밖의 경로를 주면 `mark_path 파일이 없다`로 즉시 실패한다
  (의도된 동작: 조용히 대체하지 않는다).
- `palette` 키는 `ink`·`paper`·`accent`·`calm`만 쓴다.
- 아이콘 크기·배경은 기본값이 있으므로 비워도 된다.

## 2. 실행

### 드라이런

```bash
scripts/contest_entry.sh --dry-run briefs/acme-web.json web
```

JSON의 기본 키와 profile 규칙을 검증하고 실제로 실행할 `graph_cli.py run`
명령만 출력한다. print·brand는 그래프의 `package_spec`·`brand_spec` 검증을 그대로
호출하므로 작은 재단 크기나 잘못된 palette도 여기서 걸린다. 그래프의 최종 검증을
대체하지는 않는다.

### 실행

```bash
scripts/contest_entry.sh briefs/acme-web.json web
```

- 내부에서 `python3 scripts/graph_cli.py run entry_pack --input '<json>' --wait`를
  실행하고 thread_id, 산출 팩 경로, 다음 명령을 출력한다.
- 기본 대기 시간은 300초다. web 변주가 많으면 `--timeout 900`을 준다.
- 직접 실행할 때는 JSON 파일에 `spec.profile`이 이미 있어야 한다. 없으면 그래프가
  web으로 해석한다. 셸 따옴표 사고를 피하려고 파일 내용을 `$(cat ...)`로 넣는다.

```bash
python3 scripts/graph_cli.py run entry_pack \
  --input "$(cat briefs/acme-web.json)" \
  --wait --timeout 600
```

### 후보 확인

`run`은 `hitl` 앞에서 멈춘다. `approve` 전에 선택지 ID를 읽는다.

```bash
THREAD_ID='<thread_id>'   # 실행 출력의 thread_id로 바꾼다
python3 scripts/graph_cli.py show "$THREAD_ID" --json > /tmp/entry-state.json
python3 - <<'PY'
import json

state = json.load(open("/tmp/entry-state.json", encoding="utf-8"))
for option in state["values"].get("options") or []:
    print(option["id"], "|", option.get("direction"), "|",
          option.get("style"), option.get("theme"))
PY
```

`status <thread_id> --json`처럼 서브커맨드 뒤에 `--json`을 붙이면 argparse가
거부한다. 전역 플래그로 `python3 scripts/graph_cli.py --json status <thread_id>`를
쓰거나, 선택지·manifest까지 한 번에 받으려면 `show --json`을 쓴다.

`options`가 비었으면 승인하지 말고 `renders`·`critiques`를 확인한다. 렌더가
없으면 팩도 없다.

### 승인·재개

```bash
python3 scripts/graph_cli.py approve "$THREAD_ID" --graph entry_pack \
  --values '{"approval_status":"approved","selection":["<id-1>","<id-2>"]}' \
  --wait --timeout 600
```

- `selection`은 `show --json`에서 본 ID를 쓴다. web·print는 고른 수만큼 팩 항목이
  늘고, brand는 첫 항목으로 팩을 만든다.
- `selection`을 비우고 `approval_status`만 승인하면 서로 다른 방향에서 최대 3개를
  자동으로 고른다. 자동 선택은 의도가 반영되지 않으므로 기본은 명시 선택이다.
- `approval_status`를 `approved`로 주지 않고 재개하면 `hitl`은 `pending`만 남기고
  그래프는 pack으로 진행한다. web은 빈 팩이 되고, print·brand는 `pack`에서 실패한다.
  CLI가 `✅ 완료`를 찍어도 `run_status`를 확인해야 하는 이유다. 승인과 `selection`을
  함께 넣어야 한다.
- `approve`는 다시 `hitl`에서 멈추지 않는다. `interrupt_before`는 `run`이 `hitl`에
  들어갈 때만 걸린다. 재개 후에는 `show`로 결과를 확인한다.

### 팩·manifest 확인

```bash
python3 scripts/graph_cli.py show "$THREAD_ID"
```

`show`는 상태 요약과 함께 `manifest.json` 경로·sha256·digest·artifact 수를
출력한다. `--json`을 붙이면 `values`와 `manifests`를 한 번에 받는다.

## 3. 산출물

배포 컨테이너는 `SONAGI_ENTRY_OUT=/home/ubuntu/entry_pack`을 쓴다. 로컬 그래프
기본값은 `/tmp/entry_pack`이다. `contest_entry.sh`는 환경변수 → 배포 경로
(`/home/ubuntu/entry_pack`) 존재 여부 → `/tmp/entry_pack` 순으로 예상 경로를
출력한다. pack이 만들어진 뒤에는 `show --json`의 manifest 경로가 가장 정확하다.

```
<out_root>/<run_id>/
  manifest.json
  pack/
    mockups/        # 선택한 항목의 목업(profile별 카드·전개도)
```

| profile | `pack/`에서 반드시 볼 것 |
|---|---|
| web | `<id>.png`, `<id>.jpg`, `meta.json`, `entry.md` |
| print | `<id>-front.png`, `<id>.jpg`, `meta.json`, `entry.md` (`print_ready`면 `<id>-print/` PDF) |
| brand | `normalized.svg`, `outlined.svg`, `mark-<width>.png`, `mark.jpg`, `mark.pdf`, `icons/`, `meta.json`, `entry.md` |

검수 순서:

1. print·brand는 `meta.json`의 `gates_ok`가 `true`인지, 각 gate의 `ok`가
   `true`인지 본다. brand에서 `pdf: false`로 끈 `brand.pdf`는 `ok: true,
   skipped: true`가 정상이다. 그 밖의 `skipped: true`는 우회 흔적이므로 통과로
   보지 않는다. web은 gate가 없으므로 2번으로 바로 간다.
2. web은 `items` 수가 승인한 `selection` 수와 같은지, `png_size`·`jpg_size`가
   0보다 큰지, PNG 폭이 `image_width`(기본 2000) 이하이고 높이가 `max_height`
   (기본 3000) 이하인지 확인한다.
3. print는 `print.assets`·`print.layers`·`print.barcode`·`print.render_size`
   gate와 `entry.md`의 재단 크기를 대조한다.
4. brand는 `outlined.svg`에 `<text>`·`<style>`이 없는지, `mark.pdf`가 있는지,
   `icons/`에 요청한 크기·배경이 모두 있는지 확인한다. PDF는 기본으로 만들고
   `brand.pdf: false`일 때만 생략한다.
5. `entry.md`를 읽어 방향·스타일·테마가 브리프와 맞는지 확인한다. 파일 이름만
   보고 제출하지 않는다.

## 4. 제출 규칙

- **플랫폼이 요구한 이미지는 그대로 올린다.** 콘테스트가 요구하는 장수·비율·형식의
  이미지를 빠뜨리고 미리보기 링크로 대체하지 않는다.
- **미리보기 링크는 선택 사항이다.** 내부 리뷰나 공유용일 뿐이며, 플랫폼이 요구한
  필수 이미지·원본 파일을 대신하지 않는다.
- **미리보기·이미지에 연락처를 넣지 않는다.** 이메일·전화·SNS·메신저 ID·QR·
  포트폴리오 URL을 넣지 않는다. 워터마크는 텍스트 브랜드명만 쓸 수 있다.
- **원본(SVG·AI·PDF)은 수상·결제 전에 넘기지 않는다.** 제출 단계에서는 플랫폼이
  요구한 형식만 올린다(보통 PNG/JPG, 로고는 벡터를 요구할 수 있다). 요구하지 않은
  편집 원본은 수상 후 플랫폼이 지정한 방법으로 전달한다.
- 생성형 AI를 썼다면 플랫폼의 신고 절차를 따른다. AI 원본 리소스의 저작권은
  양도할 수 없고 2차 창작물만 양도·활용할 수 있는 경우가 많다. 제출 전에
  `docs/CONTEST_PLAYBOOK.md`의 플랫폼별 규칙을 확인한다.

## 5. 실패 처리

| 증상 | 원인 | 조치 |
|---|---|---|
| `원격 렌더 산출물이 없습니다`, ssh/rsync timeout | devops 렌더 장애 | devops 복구를 기다린 뒤 같은 입력으로 재실행한다. 로컬 렌더로 대체하지 않는다(자산 라이브러리가 로컬에 없다). 마감이 임박하면 이미 완료된 팩을 쓰거나 print/brand로 전환한다. |
| `renders`·`options`가 비었거나 selection보다 적다 | 렌더 일부 실패, `variant_cap` 과소 | `show --json`에서 `renders` 수와 `critiques`를 확인하고 `variant_cap`을 올려 재실행한다. 빈 selection으로 승인하지 않는다. |
| `meta.json` gate가 `ok: false` | 자산 누락·변환 실패 등 | `detail`을 보고 원인을 고쳐 재실행한다. 운영에서 `allow_print_checks`·`allow_brand_checks`로 우회하지 않는다. |
| `인쇄 게이트 실패 N건` | entry: 재단·레이어·바코드·렌더 크기 / handoff: 도련·PDF·폰트·잉크 | 메시지의 gate id와 `detail`을 보고 `package`·SVG를 고친다. 우회 플래그를 쓰지 않는다. |
| run은 success인데 brand `gates_ok=false` | brand 게이트 실패는 warn으로 기록됨 | CLI의 성공 표시만 믿지 말고 `meta.json` gates를 본다. 실패면 제출하지 않는다. |
| `✅ 완료`인데 산출물 없음 | CLI 요약은 run_status와 무관하게 완료를 찍을 수 있다 | `run_status`가 `success`인지, `show`의 manifest가 있는지 확인한다. |
| 대기 시간 초과 | 변주·원격 렌더가 느림 | 서버 실행은 계속된다. `show "$THREAD_ID"`로 이어서 확인한다. `contest_entry.sh`는 `running`/`pending`이면 non-zero로 끝난다. |
| thread를 잃음 | 세션 종료 | `runs --limit 10`으로 최근 실행을 찾고 `show`로 상태를 확인한다. 없으면 `spec.seed`를 고정해 재실행한다. |

## 6. 마무리

- 제출한 팩의 `manifest.json`과 `entry.md`를 보관한다. 팩 디렉터리는 자동으로
  지워지지 않으므로, 수상 결과가 나오기 전에 손으로 지우지 않는다.
- 제출 후 thread_id, run_id, manifest digest, 플랫폼 URL을 기록한다.
- 결과가 나오면 수상 팩은 영구 보관하고, 탈락 팩은 플랫폼 약관과 내부 보존
  기간을 확인해 정리한다.
