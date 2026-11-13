"""공고 정규화 (#5): 텍스트 공고 → 비교표 행.

schema.py  비교표 행 모양(API용)과 LLM 출력 모양
llm.py     Claude 호출 (구조화 출력)
rows.py    LLM 답을 원문과 대조해 status를 정하고 비교표 행으로 만든다
checks.py  원문 대조 도구 (인용 찾기, 금액·개월·비율 읽기, 지역 코드 찾기)
"""

from backend.normalize.llm import ClaudeExtractor, Extractor, NormalizeError
from backend.normalize.rows import build_rows
from backend.normalize.schema import ComparisonRow, ExtractionResult

__all__ = [
    'ClaudeExtractor',
    'ComparisonRow',
    'ExtractionResult',
    'Extractor',
    'NormalizeError',
    'build_rows',
]
