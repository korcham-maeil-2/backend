from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.graph import nodes
from backend.graph.state import GraphState


def build_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    builder = StateGraph(GraphState)
    builder.add_node('route_input', nodes.route_input)
    builder.add_node('normalize', nodes.normalize)
    builder.add_node('confirm_table', nodes.confirm_table)
    builder.add_node('calculate', nodes.calculate)
    builder.add_node('write_report', nodes.write_report)

    builder.add_edge(START, 'route_input')
    builder.add_edge('route_input', 'normalize')
    builder.add_edge('normalize', 'confirm_table')
    builder.add_edge('confirm_table', 'calculate')
    builder.add_edge('calculate', 'write_report')
    builder.add_edge('write_report', END)

    # interrupt는 체크포인터가 있어야 동작한다. 세션(thread_id)마다 상태가 저장된다.
    return builder.compile(checkpointer=checkpointer)
