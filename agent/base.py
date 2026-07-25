"""Shared result type for SQLAgent and GraphAgent so callers (eval harness,
Streamlit) can treat either backend interchangeably."""
from dataclasses import dataclass, field


@dataclass
class AgentResult:
    answer: str
    queries: list = field(default_factory=list)  # SQL or Cypher, depending on backend
    last_columns: list | None = None
    last_rows: list | None = None
    last_truncated: bool = False
