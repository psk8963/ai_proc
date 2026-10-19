from typing import Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel, Field


# [M02] API 요청/응답 규약 (ai_nexacro 와 같은 형식)
class ConversationTurn(TypedDict):
    question: str
    answer: str


class Evidence(BaseModel):
    source: str
    score: Optional[float] = None
    snippet: str = ""


class ChatResponse(BaseModel):
    report: str
    decided_by: str = Field(..., description="rule | llm | fallback")
    status: str = Field(..., description="SUCCESS | UNCERTAIN | INSUFFICIENT_EVIDENCE")
    intent: str = Field(..., description="impact | lookup | none")
    search_query: str
    evidence: List[Evidence]
    notes: List[str] = []
    trace_id: str
    scenario: Optional[Dict[str, Any]] = None
    impact: Optional[Dict[str, Any]] = None


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    trace_id: str
    details: Optional[Dict[str, Any]] = None


class ImpactRequest(BaseModel):
    table: str
    column: Optional[str] = None
    change: str = Field(..., description="length | type | nullable | drop_column | rename_column | add_column | drop_table | rename_table | data")
    new_length: Optional[int] = None
    new_type: Optional[str] = None
    new_precision: Optional[int] = None
    new_scale: Optional[int] = None
    nullable: Optional[bool] = None
    default: Optional[bool] = None
    new_name: Optional[str] = None
    operation: Optional[str] = Field(None, description="update | delete | insert | set_null (change=data)")
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    condition: Optional[str] = None
    run_db_checks: bool = False


class SqlRequest(BaseModel):
    sql: str
    max_rows: Optional[int] = None


class FolderImportRequest(BaseModel):
    path: str
    kind: str = Field("source", description="source | batch")


# [M05] Agent State 스키마
class AgentState(TypedDict, total=False):
    trace_id: str
    query: str
    history: List[ConversationTurn]       # 최근 대화(이미 길이 제한된 상태)
    search_query: str                     # 대화 맥락을 반영해 다시 쓴 질의
    intent: str                           # impact | lookup | none
    entities: Dict[str, Any]              # 질문에서 찾은 테이블·컬럼·프로그램·함수
    scenario: Optional[Dict[str, Any]]    # 영향도 분석 시나리오
    scenario_error: str
    impact: Optional[Dict[str, Any]]      # impact_engine 결과
    facts: str                            # LLM 에 줄 분석 사실(문자열)
    evidence: List[Dict[str, Any]]
    result_text: str
    decided_by: str                       # rule | llm | fallback
    status: str                           # SUCCESS | UNCERTAIN | INSUFFICIENT_EVIDENCE
    notes: List[str]
