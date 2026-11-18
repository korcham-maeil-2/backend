"""개인정보 마스킹 — 외부 AI(Claude)로 보내기 전에 글 속 개인정보를 가린다 (이슈 #20).

대회 규정: 실제 개인정보·민감정보를 외부 AI API로 보내지 않는다.
claude.create_message가 요청을 보내기 직전에 mask_messages를 부른다 → Claude를 부르는 모든 곳
(공고 정리·이미지 읽기·리포트 쓰기·리포트 검토)에 빠짐없이 걸린다.

가리는 것 (공고 정리에 필요 없는 값만):
  - 주민등록번호   900101-1234567, 9001011234567        → [주민번호]
  - 생년월일       '생년월일 1990.01.01' (앞에 낱말이 올 때만) → 생년월일 [생년월일]
  - 전화번호       010-1234-5678, (02) 555-1234, 0507-…, 1588-1234, +82 10-…  → [전화번호]
  - 이메일         jobs@example.com, hong @ corp.com, hong(at)corp.com  → [이메일]
  - 사람 이름      '담당자 홍길동', '대표이사 최영수', '인사팀 이서연' → 담당자 [이름]
                   연락처가 있는 줄의 '홍길동 대리' → [이름] 대리
  - 계좌·여권·운전면허 번호, 메신저 아이디 (앞에 낱말이 올 때만) → [식별번호], [아이디]
  - 상세 주소      시·군·구 뒤 도로명의 건물번호, 동·리 뒤 지번, 동·호수 → 분당구 판교역로 [번지]
근무지는 시·군·구까지만 있으면 계산된다 (월세 시세·지원 정책은 시군구 단위)
→ 시·군·구·도로명·동 이름은 남긴다.

일부러 가리지 않는 것:
  - 회사 이름, 사업자등록번호 — 회사의 공개 정보다.
  - 금액·날짜 — 계산에 필요하다. '연봉 1800-2000만원', '3,600만원', '2026-11-16'은 건드리지 않는다.
  - 주소(URL) 속 글자 — 지원 정책 링크가 깨지지 않게 http(s) 주소는 통째로 그대로 둔다.
  - 이미지 — 그림 속 글자는 Claude가 읽기 전에는 알 수 없다. 공고 캡처는 회사가 공개한 글이라
    그대로 보내고, 옮겨 적은 글이 다음 단계(공고 정리)로 갈 때 이 마스킹을 거친다.

[한계] 규칙으로 찾는다. 앞뒤에 단서가 없는 이름('홍길동'만 한 줄), 영문 이름, 낱말 없이 적힌
계좌번호는 가리지 못한다. 이름 규칙은 직무 낱말(백엔드·마케팅 등)을 이름으로 잘못 가리지 않는
쪽으로 맞췄다.
"""

import re
from typing import Any

_URL = re.compile(r'(https?://\S+)')

_YYMMDD = r'\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])'
_RRN = re.compile(rf'(?<!\d){_YYMMDD}\s?-\s?[1-8]\d{{6}}(?!\d)|(?<!\d){_YYMMDD}[1-8]\d{{6}}(?!\d)')
_BIRTH = re.compile(
    r'(생년월일|생일|출생일?)(\s*[:：]?\s*)'
    r'(?:\d{4}\s?[.\-/년]\s?\d{1,2}\s?[.\-/월]\s?\d{1,2}일?|\d{6}|\d{8})(?!\d)'
)
# 낱말 처음에서만 시작하고 길이를 묶는다: 띄어쓰기 없는 긴 글('xxxx…' 15만 자)에서 자리마다
# 끝까지 훑어 시간이 제곱으로 늘던 것을 막는다 (이슈 #52: 2만 자 2.2초 → 15만 자는 사실상 멈춤)
_EMAIL = re.compile(
    r'(?<![\w.+-])[\w.+-]{1,64}\s?(?:@|\(at\)|\[at\])\s?[\w-]{1,63}(?:\.[\w-]{1,63}){1,6}',
    re.IGNORECASE,
)
_AREA = r'(?:\+?82[-.\s]?0?|0)(?:2|[3-6][1-5]|70|80|50\d)'  # 지역번호·070·080·050x
_PHONE = re.compile(
    r'(?<![\d,.])(?:'
    # 휴대폰 (붙여 써도, +82·82로 시작해도)
    r'(?:\+?82[-.\s]?0?|0)1[016789][-.\s]?\d{3,4}[-.\s]?\d{4}'
    # 지역번호·070·080·050x. '(02) 555-1234', '02)555-1234'도.
    # 가운데·끝자리 사이엔 구분자가 있어야 한다
    rf'|(?:\({_AREA}\)\s?|{_AREA}(?:\)\s?|[-.\s]))\d{{3,4}}[-.\s]\d{{4}}'
    # 1588-0000 같은 대표번호. 뒤에 단위가 오면 금액·시간 범위다 ('연봉 1800-2000만원')
    r'|1[5-9]\d{2}-\d{4}(?!\s?(?:만|천|억|원|시|분|명|년|개|건|세|%|㎡))'
    # 뒤에 숫자가 이어지면 금액 등이다 (쉼표 뒤 숫자 '1,234'도). 쉼표·괄호로 끝나면 번호다
    r')(?!\d|,\d)'
)
_ID_NUMBER = re.compile(
    r'(계좌\s?번호|계좌|여권\s?번호|여권|운전면허\s?번호|운전면허|면허\s?번호)(\s*[:：]?\s*(?:[가-힣]{2,5}\s)?)'
    r'[A-Z]?\d[\dA-Z-]{6,}'
)
_MESSENGER = re.compile(
    r'((?:카카오톡|카톡|텔레그램|인스타그램|라인)\s*(?:ID|아이디)\s*[:：]?\s*)@?[\w.-]{3,}',
    re.IGNORECASE,
)

# 흔한 성씨로 시작하는 2~4글자 (성씨 확인으로 '담당자 미정' 같은 말을 이름으로 잘못 가리지 않게)
_SURNAMES = (
    '김이박최정강조윤장임한오서신권황안송류유전홍고문양손배백허남심노하곽성차주우구민진나지'
    '엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예봉경사부'
)
_TITLES = '과장|대리|차장|부장|팀장|주임|매니저|선임|책임|실장|이사|상무|전무|파트장'
# 이름 자리에 자주 오는 보통 낱말 (성씨 글자로 시작해서 이름처럼 보이는 것)
_NOT_NAMES = frozenset(
    '이메일 연락처 연락 전화 번호 정보 미정 없음 확인 문의 배정 지정 채용 부서 소속 '
    '백엔드 마케팅 서비스 신입 경력 정규 인사 기획 연구 전략 고객 구매 제조 유통 공정 설계 '
    '안전 지원 운영 현장 경영 전산 임원 인턴 사원 직원 전체 각자 추후 변경 예정 기업 기관 '
    '주식 유한 공고 모집 안내 제출 서류 방문 우편 접수 성명 이름 대표 여부 공개 비공개'.split()
)
# 이름이 이렇게 끝나는 일은 드물다: 부서·직무 낱말(…팀, …드), 조사가 붙은 보통 낱말('문자를')
_NOT_NAME_ENDINGS = tuple('팀부실과소처단드팅스님씨자을를는에의로와가도만')
_NAME_AFTER = r'(?=\s|$|[,.)·/(\]]|님|씨|' + _TITLES + r')'
_NAME = rf'(?:[{_SURNAMES}]\s[가-힣]\s[가-힣]|[{_SURNAMES}][가-힣]{{1,3}}?)'
# '담당자 홍길동', '성명: 김철수', '대표이사 최영수', '인사팀 이서연', '문의: 홍길동'
_NAME_KEYWORD = re.compile(
    r'(?<![가-힣])('
    r'(?:채용\s*|인사\s*)?담당자명?|채용\s?담당|작성자|대표이사|대표자명?'
    r'|받는\s?사람|보낸\s?사람|수신인|수신자|발신인|발신자|지원자명?|성함'
    r'|(?:인사|채용|총무|경영지원|HR)\s?(?:팀|부|과|실)'
    r'|(?:성\s*명|이름|담당|대표|문의|수신|발신)(?=\s*[:：])'
    r')(\s*[:：]?\s*)'
    rf'({_NAME}){_NAME_AFTER}'
)
# '홍길동 대리' — 직함만으로는 '백엔드 팀장'과 구분이 안 돼서, 연락처가 있는 줄에서만 가린다
_NAME3 = rf'[{_SURNAMES}][가-힣]{{2}}'
_NAME_TITLE = re.compile(rf'(?<![가-힣])({_NAME3})(\s?(?:{_TITLES})(?:님)?)(?![가-힣])')
# '팀장 김개발' (직함 뒤 이름) — 같은 조건
_TITLE_NAME = re.compile(rf'((?:{_TITLES})\s)({_NAME3})(?=\s|$|[,.(/])')
# '서지후 님, 안녕하세요', '홍길동님께' — 합격 메일의 받는 사람 이름. 어느 줄이든 가린다
_NAME_HONORIFIC = re.compile(
    rf'(?<![가-힣])({_NAME3})(\s?(?:님|씨)(?![가-힣])|\s?님(?=께|의|은|이|을|,))'
)
_CONTACT_LINE = re.compile(r'\[이메일\]|\[전화번호\]|문의|담당|연락')

# 도로명 주소의 건물번호: 시·군·구(읍·면) 바로 뒤에 오는 '…로/길 숫자'만 (주소 꼴일 때만 가린다).
# '연봉으로 3,600만원'처럼 '…로' 뒤에 숫자가 오는 보통 문장은 건드리지 않는다.
_NUMBER_END = r'(?=\s|\(|,(?!\d)|번|$)'
_ROAD_NUMBER = re.compile(
    r'([가-힣]+(?:시|군|구|읍|면)\s+(?:[가-힣\d]+\s)?[가-힣\d]+(?:로|길))'
    rf'\s?(?:지하\s?)?\d+(?:-\d+)?{_NUMBER_END}'
)
# 지번: 시·군·구 뒤 '○○동/리/가 123-4' (뒤에 '만원' 같은 단위가 붙으면 주소가 아니다)
_DONG_NUMBER = re.compile(
    rf'([가-힣]+(?:시|군|구|읍|면)\s+[가-힣\d]+(?:동|리|가))\s\d+(?:-\d+)?(?:\s?번지|{_NUMBER_END})'
)
# 도로명 주소 뒤 괄호 속 지번: '(용봉동 831-8)'
_PAREN_DONG_NUMBER = re.compile(r'(\([가-힣\d]+(?:동|리|가))\s\d+(?:-\d+)?(?=\))')
_LOT_NUMBER = re.compile(r'(?<![\d,])\d+(?:-\d+)?\s?번지')
_UNIT = re.compile(r'(?<![\d,제])(?:\d+동\s?\d+호|\d{3,}호)(?![가-힣])')


_PARTICLES = '은는이가을를와과로의에도만'


def _is_name(word: str) -> bool:
    # '담당자 문의는 …'의 '문의는'처럼 보통 낱말에 조사가 붙은 것도 이름이 아니다
    if word in _NOT_NAMES or (word[:-1] in _NOT_NAMES and word[-1] in _PARTICLES):
        return False
    return not word.endswith(_NOT_NAME_ENDINGS)


def _mask_keyword_name(m: re.Match) -> str:
    return f'{m[1]}{m[2]}[이름]' if _is_name(m[3]) else m[0]


def _mask_titled_name(m: re.Match) -> str:
    # 이름은 보통 세 글자다. 두 글자('신입 대리')·네 글자는 보통 낱말일 때가 많아 건드리지 않는다
    return f'[이름]{m[2]}' if _is_name(m[1]) else m[0]


def _mask_name_after_title(m: re.Match) -> str:
    return f'{m[1]}[이름]' if _is_name(m[2]) else m[0]


def _mask_names_near_contact(text: str) -> str:
    """연락처가 있는 줄과 그 바로 앞뒤 줄에서 '홍길동 대리', '팀장 김개발'의 이름을 가린다."""
    lines = text.split('\n')
    near = [bool(_CONTACT_LINE.search(line)) for line in lines]
    for i, line in enumerate(lines):
        if near[i] or (i > 0 and near[i - 1]) or (i + 1 < len(lines) and near[i + 1]):
            line = _NAME_TITLE.sub(_mask_titled_name, line)
            lines[i] = _TITLE_NAME.sub(_mask_name_after_title, line)
    return '\n'.join(lines)


def _mask_plain(text: str) -> str:
    """주소(URL)가 아닌 글 한 토막을 가린다. 순서: 길게 잡히는 것(주민번호·이메일)부터."""
    text = _RRN.sub('[주민번호]', text)
    text = _BIRTH.sub(lambda m: f'{m[1]}{m[2]}[생년월일]', text)
    text = _EMAIL.sub('[이메일]', text)
    text = _ID_NUMBER.sub(lambda m: f'{m[1]}{m[2]}[식별번호]', text)
    text = _PHONE.sub('[전화번호]', text)
    text = _MESSENGER.sub(lambda m: f'{m[1]}[아이디]', text)
    text = _NAME_KEYWORD.sub(_mask_keyword_name, text)
    text = _NAME_HONORIFIC.sub(_mask_titled_name, text)
    text = _mask_names_near_contact(text)
    text = _ROAD_NUMBER.sub(lambda m: f'{m[1]} [번지]', text)
    text = _DONG_NUMBER.sub(lambda m: f'{m[1]} [번지]', text)
    text = _PAREN_DONG_NUMBER.sub(lambda m: f'{m[1]} [번지]', text)
    text = _LOT_NUMBER.sub('[번지]', text)
    return _UNIT.sub('[상세주소]', text)


def mask_text(text: str) -> str:
    """글 속 개인정보를 [종류]로 바꾼다. http(s) 주소는 그대로 둔다 (링크가 깨지지 않게)."""
    parts = _URL.split(text)  # 짝수 번째 = 보통 글, 홀수 번째 = 주소
    return ''.join(part if i % 2 else _mask_plain(part) for i, part in enumerate(parts))


def mask_quotes(answer: Any) -> Any:
    """미리 써 둔 LLM 답(JSON)의 인용(quote)을 같은 규칙으로 가린다.

    실제 LLM은 가린 글만 보므로 인용도 가린 글에서 나온다. 시연용 가짜 응답(fixture)은 가리기 전
    글로 써 두었으니, 불러올 때 이 함수로 맞춘다 (안 맞추면 인용이 원문에 없다고 나온다).
    """
    if isinstance(answer, dict):
        return {
            k: mask_text(v) if k == 'quote' and isinstance(v, str) else mask_quotes(v)
            for k, v in answer.items()
        }
    if isinstance(answer, list):
        return [mask_quotes(v) for v in answer]
    return answer


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
