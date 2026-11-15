"""화면에 그대로 나가는 금액 글자 — 프론트(format.ts의 formatWon)와 같은 모양으로 쓴다.

    36_000_000 → '3,600만원'    150_000 → '15만원'    62_000 → '6만 2,000원'
    150_000_000 → '1억 5,000만원'    0 → '0원'

계산 결과의 안내(notes)·출처(source)는 결과 화면에 그대로 보이므로, '150,000원' 대신
사회초년생이 바로 읽는 한글 단위로 쓴다 (DESIGN.md "쉬운 말").
"""


def won(value: int) -> str:
    """원 단위 금액을 한글 단위 글자로. 음수는 앞에 '−'를 붙인다."""
    if value == 0:
        return '0원'
    sign = '−' if value < 0 else ''
    value = abs(value)
    eok, rest = divmod(value, 100_000_000)
    man, won_ = divmod(rest, 10_000)
    parts = []
    if eok:
        parts.append(f'{eok:,}억')
    if man:
        parts.append(f'{man:,}만')
    if won_:
        parts.append(f'{won_:,}')
    return f'{sign}{" ".join(parts)}원'
