"""그래프 상태(State) — 그래프가 처리하는 동안 들고 다니는 '공용 메모장'.

[LangGraph 기본 개념]
LangGraph는 "노드(함수)들을 화살표로 이은 그래프"를 실행하는 라이브러리다.
  - 노드: 일 하나를 하는 파이썬 함수 (예: 공고 읽기, 계산하기)
  - 엣지: 노드 다음에 어느 노드로 갈지 정한 화살표
  - 상태: 모든 노드가 같이 보는 데이터. 노드는 상태를 읽고, 바꿀 부분만 돌려준다.

이 파일은 그 '상태'에 어떤 칸이 있는지 정의한다.
노드가 {'table': [...]} 를 돌려주면 LangGraph가 상태의 table 칸을 그 값으로 바꿔 준다.

비교표 한 행(공고 1건)의 칸은 backend/normalize/schema.py의 ComparisonRow다.
상태에는 체크포인터(SQLite)에 그대로 저장되도록 ComparisonRow를 dict로 바꿔(model_dump) 담는다.
"""

import operator
from typing import Annotated, Any, TypedDict


# TypedDict: "이런 키를 가진 dict"라는 설계도. 실행 중에는 그냥 평범한 dict다.
# total=False: 모든 칸이 처음부터 있을 필요는 없다 (노드가 실행되며 하나씩 채워진다).
class GraphState(TypedDict, total=False):
    # 사용자가 보낸 메시지 목록.
    # Annotated[..., operator.add]는 LangGraph에게 "이 칸은 덮어쓰지 말고 뒤에 붙여라"라고
    # 알려 주는 표시(리듀서)다. 그래서 메시지를 또 보내면 ['첫 메시지', '두 번째'] 처럼 쌓인다.
    # 이 표시가 없는 아래 칸들은 노드가 새 값을 돌려주면 그 값으로 덮어쓴다.
    messages: Annotated[list[str], operator.add]

    # messages 가운데 이미지에서 옮겨 적은 글 (이미지·이미지 PDF·스캔 PDF의 OCR 글자).
    # normalize가 인용이 이 글에만 있는 칸을 '이미지에서 읽음'으로 표시하는 데 쓴다.
    image_texts: Annotated[list[str], operator.add]

    # 올린 파일 요약 (이름·종류·읽은 방법). 원본 파일은 저장하지 않는다 (backend/inputs/read.py).
    inputs: Annotated[list[dict[str, Any]], operator.add]

    # 비교표: 공고 1건이 한 행. normalize 노드가 만들고, 사용자가 확인·수정한다.
    table: list[dict[str, Any]]

    # 사용자가 비교표를 확인했는지 여부.
    table_confirmed: bool

    # 계산 결과 (월 잔여액 등). calculate 노드가 채운다.
    result: dict[str, Any]

    # 최종 리포트 문장. write_report 노드가 채운다.
    report: str
