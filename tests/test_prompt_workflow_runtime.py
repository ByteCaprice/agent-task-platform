from types import SimpleNamespace

from framework.runtime.prompt_workflow import extract_model_data, model_runtime


def test_model_runtime_uses_agent_configuration_and_nested_overrides() -> None:
    context = SimpleNamespace(
        agent=SimpleNamespace(
            runtime={
                "model": "top-level-model",
                "temperature": 0,
                "model_config": {"model": "nested-model", "max_tokens": 512},
                "unrelated": "ignored",
            }
        )
    )

    runtime = model_runtime(context, timeout_seconds=15)

    assert runtime == {
        "model": "nested-model",
        "temperature": 0,
        "max_tokens": 512,
        "timeout_seconds": 15,
    }


def test_extract_model_data_parses_json_code_fences() -> None:
    output = extract_model_data(
        {
            "data": '```json\n{"status": "ok"}\n```',
            "agent": {"name": "example"},
            "model_call_id": "call-1",
        }
    )

    assert output == {"status": "ok"}
