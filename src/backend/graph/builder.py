"""그래프 조립 — 노드(함수)들을 화살표(엣지)로 이어 실행 가능한 그래프를 만든다.

완성된 모양:
    START → route_input → normalize → confirm_table(멈춤) → check_missing(빈 값이면 멈춤)
        ─┬→ get_rent → calculate ─┬→ write_report ⇄ verify_report (문제 있으면 다시 쓰기)
         └→ get_local_support ───┘        ↓ 통과·횟수 끝
                                         END
    (월세 조회와 청년 지원 조회는 서로 기다릴 필요가 없어 동시에 돈다)
    (START, END는 LangGraph가 정해 둔 '시작점'과 '끝점' 표시)
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.graph import nodes
from backend.graph.state import GraphState
from backend.normalize import ClaudeExtractor, Extractor
from backend.public_data.rent import RentEstimator
from backend.report import ReportAgent
from backend.support import PolicySource


def build_graph(
    checkpointer: BaseCheckpointSaver,
    extractor: Extractor | None = None,
    rent_estimator: RentEstimator | None = None,
    support_source: PolicySource | None = None,
    report_agent: ReportAgent | None = None,
) -> CompiledStateGraph:
    """그래프를 만들어 돌려준다.

    extractor: normalize 노드가 쓸 'LLM으로 공고 뽑기' 함수.
      안 주면 실제 Claude를 부르는 ClaudeExtractor를 쓴다. 테스트에서는 가짜를 넣는다.
    rent_estimator: get_rent 노드가 쓸 월세 추정기. 없으면 월세를 조회하지 않는다.
    support_source: get_local_support 노드가 쓸 청년정책 공급자. 없으면 조회하지 않는다.
    report_agent: 리포트를 쓰고 검증하는 에이전트 (report/agent.py). 없으면 리포트를 쓰지 않는다.

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
    builder.add_node('normalize', nodes.make_normalize(extractor or ClaudeExtractor()))
    builder.add_node('confirm_table', nodes.confirm_table)
    builder.add_node('check_missing', nodes.check_missing)
    builder.add_node('get_rent', nodes.make_get_rent(rent_estimator))
    builder.add_node('get_local_support', nodes.make_get_local_support(support_source))
    builder.add_node('calculate', nodes.calculate)
    write_report, verify_report, after_verify = nodes.make_report_nodes(report_agent)
    builder.add_node('write_report', write_report)
    builder.add_node('verify_report', verify_report)

    # 3) 엣지(화살표) 연결: add_edge(앞 노드, 다음 노드)
    #    지금은 한 줄로 쭉 이어진다. 나중에 '되묻기'처럼 조건에 따라 갈라지는 길은
    #    add_conditional_edges로 추가한다.
    builder.add_edge(START, 'route_input')
    builder.add_edge('route_input', 'normalize')
    builder.add_edge('normalize', 'confirm_table')
    # 한 노드에서 두 갈래로 나가면 둘이 동시에 돈다.
    # 확인 뒤 계산을 막는 빈 값이 있으면 되묻고 기다린다 (없으면 바로 지나간다)
    builder.add_edge('confirm_table', 'check_missing')
    builder.add_edge('check_missing', 'get_rent')
    builder.add_edge('check_missing', 'get_local_support')
    builder.add_edge('get_rent', 'calculate')
    # 목록으로 이으면 '둘 다 끝나야' write_report로 간다.
    builder.add_edge(['calculate', 'get_local_support'], 'write_report')
    builder.add_edge('write_report', 'verify_report')
    # 갈림길: 검증에서 문제가 나오면 다시 쓰고(횟수 제한), 아니면 끝
    builder.add_conditional_edges('verify_report', after_verify, ['write_report', END])

    # 4) compile: 설계도를 실제로 실행할 수 있는 그래프로 만든다.
    #    interrupt(멈춤)는 체크포인터가 있어야만 동작한다 (멈춘 상태를 어딘가 저장해야 하므로).
    return builder.compile(checkpointer=checkpointer)
