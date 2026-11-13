"""그래프 상태.

비교표 한 행(공고 1건)의 확정 스키마는 normalize 이슈(#5)에서 정한다. 지금은 뼈대라 dict로 둔다.
"""

import operator
from typing import Annotated, Any, Literal, TypedDict

InputKind = Literal['text', 'image', 'pdf', 'sheet']


class GraphState(TypedDict, total=False):
    # 사용자가 보낸 메시지. 새 메시지가 오면 뒤에 붙는다.
    messages: Annotated[list[str], operator.add]
    input_kind: InputKind
    # 비교표: 공고 1건이 한 행
    table: list[dict[str, Any]]
    table_confirmed: bool
    result: dict[str, Any]
    report: str
