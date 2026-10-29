"""Agent Runtime rejects env vars with empty values (400 "Required field is not set"):
`deploy_agent` must only pass optional variables such as T08_AGENT_URL when they are set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_SAVED = "projects/p1/locations/europe-west1/reasoningEngines/123"


class _Remote:
    def __init__(self, name: str) -> None:
        self.resource_name = name


class _CapturingAgentEngines:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def update(self, *, resource_name: str, **kwargs) -> _Remote:
        self.kwargs = kwargs
        return _Remote(resource_name)

    def create(self, **kwargs) -> _Remote:  # pragma: no cover - update path is exercised
        self.kwargs = kwargs
        return _Remote(_SAVED)


@pytest.fixture
def deploy_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import deploy.deploy as dep

    monkeypatch.setenv("SEED_OFFLINE_MODE", "1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
    monkeypatch.setenv("AGENT_RUNTIME_LOCATION", "europe-west1")
    monkeypatch.setenv("ENABLE_MCP_STDIO", "0")
    monkeypatch.setenv("AGENT_RUNTIME_DEPLOY", "1")
    monkeypatch.setattr(dep.vertexai, "init", lambda **_: None)
    monkeypatch.setattr(dep, "AdkApp", lambda **_: object())
    fake = _CapturingAgentEngines()
    monkeypatch.setattr(dep, "agent_engines", fake)
    state = tmp_path / ".deployed_agent.json"
    state.write_text(json.dumps({"resource_name": _SAVED, "project_id": "p1"}), encoding="utf-8")
    return dep, fake, state


def test_env_vars_never_contain_empty_values_and_omit_unset_t08_url(monkeypatch: pytest.MonkeyPatch, deploy_env):
    dep, fake, state = deploy_env
    monkeypatch.setenv("T08_AGENT_URL", "")
    dep.deploy_agent(pack_id="t07", state_file=state, check_bucket=False)
    env = fake.kwargs["env_vars"]
    assert "T08_AGENT_URL" not in env
    assert all(value != "" for value in env.values()), env


def test_env_vars_include_t08_url_when_set(monkeypatch: pytest.MonkeyPatch, deploy_env):
    dep, fake, state = deploy_env
    monkeypatch.setenv("T08_AGENT_URL", "http://t08.test")
    dep.deploy_agent(pack_id="t07", state_file=state, check_bucket=False)
    assert fake.kwargs["env_vars"]["T08_AGENT_URL"] == "http://t08.test"
    assert "a2a" in " ".join(fake.kwargs["requirements"])  # a2a-sdk must ship in the container


def test_a2a_sdk_is_pinned_everywhere():
    import deploy.deploy as dep

    assert "a2a-sdk==1.3.0" in dep.PINNED_REQUIREMENTS
    assert "a2a-sdk==1.3.0" in Path("pyproject.toml").read_text(encoding="utf-8")
    assert "a2a-sdk==1.3.0" in Path("requirements.txt").read_text(encoding="utf-8")
