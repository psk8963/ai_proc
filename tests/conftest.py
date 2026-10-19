"""테스트 공용 설정: Oracle · Ollama 없이 돌도록 샘플 소스와 메타데이터 스냅샷, 가짜 LLM 을 쓴다.

실행:  (venv에서)  pip install -r requirements-dev.txt  →  python -m pytest -q
"""

import glob
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

import pytest

# 실제 source_repository / analysis_index / .env 를 건드리지 않도록 import 전에 경로를 바꾼다.
_TMP = tempfile.mkdtemp(prefix="proc-agent-test-")
os.environ["PROC_AGENT_NO_DOTENV"] = "1"
os.environ["SOURCE_REPOSITORY_DIR"] = os.path.join(_TMP, "src")
os.environ["ANALYSIS_INDEX_DIR"] = os.path.join(_TMP, "index")
os.environ["SCHEMA_SNAPSHOT_PATH"] = os.path.join(_TMP, "index", "schema_snapshot.json")
for _k in ("ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_DSN", "ORACLE_SCHEMAS"):
    os.environ[_k] = ""
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import source_repository  # noqa: E402

SAMPLES = os.path.join(ROOT, "tests", "samples")


class FakeLLM:
    """미리 정한 응답을 순서대로 돌려주고, 호출 내역을 기록한다. 응답이 예외면 던진다."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        content = self.responses.pop(0) if self.responses else "기본 답변"
        if isinstance(content, Exception):
            raise content
        return SimpleNamespace(content=content)


def load_samples(with_snapshot=True):
    source_repository.clear_assets("source")
    source_repository.clear_assets("batch")
    src = [(os.path.basename(p), open(p, "rb").read()) for p in glob.glob(os.path.join(SAMPLES, "proc", "*"))]
    bat = [(os.path.basename(p), open(p, "rb").read()) for p in glob.glob(os.path.join(SAMPLES, "batch", "*"))]
    source_repository.save_assets(src, "source")
    source_repository.save_assets(bat, "batch")
    snap = os.environ["SCHEMA_SNAPSHOT_PATH"]
    if with_snapshot:
        shutil.copy(os.path.join(SAMPLES, "schema_snapshot.json"), snap)
    elif os.path.exists(snap):
        os.remove(snap)


@pytest.fixture(autouse=True)
def samples():
    load_samples()
    yield


@pytest.fixture(autouse=True)
def fake_models(monkeypatch):
    llm = FakeLLM()
    try:
        import agent_runtime
        monkeypatch.setattr(agent_runtime, "get_llm", lambda: llm)
    except ImportError:  # langgraph 등이 없는 환경에서는 파서/엔진 테스트만 돈다
        pass
    return llm
