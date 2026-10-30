from __future__ import annotations

import pytest

from domain import AgentSpec
from framework.registry import AgentRegistry, RegistryError


def _agent(name: str, tags: list[str]) -> AgentSpec:
    return AgentSpec(name=name, version="1.0.0", route_tags=tags, runtime={"type": "echo"})


@pytest.mark.parametrize("replace_existing", [False, True])
def test_rejected_registration_does_not_change_agents_or_routes(replace_existing):
    owner = _agent("owner", ["occupied"])
    previous = _agent("candidate", ["previous"])
    registry = AgentRegistry([owner, previous] if replace_existing else [owner])
    agents_before = registry.list()
    routes_before = registry.routes()
    rejected = _agent("candidate", ["new-route", "occupied"])

    with pytest.raises(RegistryError, match="already routed"):
        registry.register(rejected)

    assert registry.list() == agents_before
    assert registry.routes() == routes_before
    assert registry.get_optional("candidate", "1.0.0") == (previous if replace_existing else None)


def test_rejected_default_selection_does_not_add_partial_routes():
    registry = AgentRegistry([_agent("owner", ["occupied"])])
    candidate = _agent("candidate", ["new-route", "occupied"])
    registry.register(candidate, make_default=False)
    before = registry.routes()

    with pytest.raises(RegistryError, match="already routed"):
        registry.set_default(candidate.name, candidate.version)

    assert registry.routes() == before
