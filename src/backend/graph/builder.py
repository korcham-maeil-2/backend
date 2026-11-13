"""그래프 조립 — 노드(함수)들을 화살표(엣지)로 이어 실행 가능한 그래프를 만든다.

완성된 모양:
    START → route_input → normalize → confirm_table → calculate → write_report → END
    (START, END는 LangGraph가 정해 둔 '시작점'과 '끝점' 표시)
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.graph import nodes
from backend.graph.state import GraphState


def build_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """그래프를 만들어 돌려준다.

    checkpointer(체크포인터): 그래프 상태를 저장해 두는 저장소.
      - 노드가 하나 끝날 때마다 그때의 상태를 저장한다.
      - 세션마다 따로 저장한다 (구분 이름 = thread_id, 우리 API에서는 session_id).
      - 그래서 confirm_table에서 멈춘 뒤 나중에 '이어서' 실행할 수 있다.
      우리는 main.py에서 SQLite 파일에 저장하는 체크포인터를 만들어 넘겨준다.
    """
    # 1) 상태 설계도(GraphState)를 가진 빈 그래프를 만든다.
    builder = StateGraph(GraphState)

    # 2) 노드 등록: add_node('이름', 함수)
    builder.add_node('route_input', nodes.route_input)
    builder.add_node('normalize', nodes.normalize)
    builder.add_node('confirm_table', nodes.confirm_table)
    builder.add_node('calculate', nodes.calculate)
    builder.add_node('write_report', nodes.write_report)

    # 3) 엣지(화살표) 연결: add_edge(앞 노드, 다음 노드)
    #    지금은 한 줄로 쭉 이어진다. 나중에 '되묻기'처럼 조건에 따라 갈라지는 길은
    #    add_conditional_edges로 추가한다.
    builder.add_edge(START, 'route_input')
    builder.add_edge('route_input', 'normalize')
    builder.add_edge('normalize', 'confirm_table')
    builder.add_edge('confirm_table', 'calculate')
    builder.add_edge('calculate', 'write_report')
    builder.add_edge('write_report', END)

    # 4) compile: 설계도를 실제로 실행할 수 있는 그래프로 만든다.
    #    interrupt(멈춤)는 체크포인터가 있어야만 동작한다 (멈춘 상태를 어딘가 저장해야 하므로).
    return builder.compile(checkpointer=checkpointer)
