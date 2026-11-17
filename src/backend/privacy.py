"""개인정보 마스킹 — 외부 AI(Claude)로 보내기 전에 글 속 개인정보를 가린다 (이슈 #20).

대회 규정: 실제 개인정보·민감정보를 외부 AI API로 보내지 않는다.
claude.create_message가 요청을 보내기 직전에 mask_messages를 부른다 → Claude를 부르는 모든 곳
(공고 정리·이미지 읽기·리포트 쓰기·리포트 검토)에 빠짐없이 걸린다.

가리는 것 (공고 정리에 필요 없는 값만):
  - 주민등록번호   900101-1234567        → [주민번호]
  - 전화번호       010-1234-5678, 02-123-4567, +82 10-…  → [전화번호]
  - 이메일         jobs@example.com      → [이메일]
  - 사람 이름      '담당자 홍길동', '성명: 김철수' → 담당자 [이름]
                   (앞에 '담당자·성명·이름'이 올 때만)
  - 상세 주소      시·군·구 뒤 도로명의 건물번호, 지번, 동·호수  → 분당구 판교역로 [번지]
근무지는 시·군·구까지만 있으면 계산된다 (월세 시세·지원 정책은 시군구 단위)
→ 시·군·구·도로명은 남긴다.

일부러 가리지 않는 것:
  - 회사 이름 — 비교표에 필요하고 공개된 정보다.
  - 금액·날짜 — 계산에 필요하다. 전화번호 규칙은 0으로 시작하고 구분자(-·.·공백)가 있어야만 잡아서
    '3,600만원', '2026-11-16' 같은 값은 건드리지 않는다 (휴대폰 번호만 붙여 쓴 것도 잡는다).
  - 이미지 — 그림 속 글자는 Claude가 읽기 전에는 알 수 없다. 공고 캡처는 회사가 공개한 글이라
    그대로 보내고, 옮겨 적은 글이 다음 단계(공고 정리)로 갈 때 이 마스킹을 거친다.
"""

import re
from typing import Any

_RRN = re.compile(r'(?<!\d)\d{6}\s?-\s?[1-8]\d{6}(?!\d)')
_EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_PHONE = re.compile(
    r'(?<![\d,.])(?:'
    r'(?:\+82[-.\s]?|0)1[016789][-.\s]?\d{3,4}[-.\s]?\d{4}'  # 휴대폰 (붙여 써도)
    r'|(?:\+82[-.\s]?|0)(?:2|[3-6][1-5]|70|80)[-.)\s]\s?\d{3,4}[-.\s]\d{4}'  # 지역번호·070·080
    r'|1[5-9]\d{2}-\d{4}'  # 1588-0000 같은 대표번호
    # 뒤에 숫자가 이어지면 금액 등이다 (쉼표 뒤 숫자 '1,234'도). 쉼표·괄호로 끝나면 번호다
    r')(?!\d|,\d)'
)
# 흔한 성씨로 시작하는 2~4글자 (성씨 확인으로 '담당자 미정' 같은 말을 이름으로 잘못 가리지 않게)
_SURNAMES = (
    '김이박최정강조윤장임한오서신권황안송류유전홍고문양손배백허남심노하곽성차주우구민진나지'
    '엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예봉경사부'
)
_NAME = re.compile(
    r'((?:채용\s*|인사\s*)?담당자|성명|이름)(\s*[:：]?\s*)'
    # 이름 자리에 자주 오는 보통 낱말은 이름이 아니다 ('담당자 이메일', '담당자 미정')
    r'(?!이메일|연락|전화|번호|정보|미정|없음|확인|문의|배정|지정|채용|부서|소속)'
    rf'([{_SURNAMES}][가-힣]{{1,3}}?)'  # 짧은 쪽부터: '이영희님'에서 '님'까지 먹지 않게
    r'(?=\s|$|[,.)·/(]|님|씨|과장|대리|차장|부장|팀장|주임|사원|매니저|선임|책임)'
)
# 도로명 주소의 건물번호: 시·군·구(읍·면) 바로 뒤에 오는 '…로/길 숫자'만 (주소 꼴일 때만 가린다).
# '연봉으로 3,600만원'처럼 '…로' 뒤에 숫자가 오는 보통 문장은 건드리지 않는다.
_ROAD_NUMBER = re.compile(
    r'([가-힣]+(?:시|군|구|읍|면)\s+(?:[가-힣\d]+\s)?[가-힣\d]+(?:로|길))'
    r'\s?(?:지하\s?)?\d+(?:-\d+)?(?=\s|\(|,(?!\d)|번|$)'
)
_LOT_NUMBER = re.compile(r'(?<![\d,])\d+(?:-\d+)?\s?번지')
_UNIT = re.compile(r'(?<![\d,])(?:\d+동\s?)?\d+호(?![가-힣])')


def mask_text(text: str) -> str:
    """글 속 개인정보를 [종류]로 바꾼다. 순서: 길게 잡히는 것(주민번호·이메일)부터."""
    text = _RRN.sub('[주민번호]', text)
    text = _EMAIL.sub('[이메일]', text)
    text = _PHONE.sub('[전화번호]', text)
    text = _NAME.sub(lambda m: f'{m[1]}{m[2]}[이름]', text)
    text = _ROAD_NUMBER.sub(lambda m: f'{m[1]} [번지]', text)
    text = _LOT_NUMBER.sub('[번지]', text)
    return _UNIT.sub('[상세주소]', text)


def _mask_block(block: Any) -> Any:
    # dict 모양 글 블록만 가린다. 이미지 블록, 앞서 Claude가 돌려준 답(SDK 객체)은 그대로 둔다.
    if (
        isinstance(block, dict)
        and block.get('type') == 'text'
        and isinstance(block.get('text'), str)
    ):
        return {**block, 'text': mask_text(block['text'])}
    return block


def mask_messages(messages: list[dict]) -> list[dict]:
    """Claude에 보낼 messages의 글을 모두 가린 새 목록 (원본은 바꾸지 않는다)."""
    masked = []
    for message in messages:
        content = message.get('content')
        if isinstance(content, str):
            content = mask_text(content)
        elif isinstance(content, list):
            content = [_mask_block(b) for b in content]
        masked.append({**message, 'content': content})
    return masked
