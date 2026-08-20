"""FastAPI routes for run submission/query/cancel/retry, plus run logs,
results, callbacks and traces under /v1/runs and /v1/traces (scope `runs`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from domain import AgentRun, AgentStage, CallbackDelivery, LogEvent, ModelCallRecord
from interfaces.http.dependencies import require_scope
from interfaces.schemas import RunSubmitRequest, RunSubmitResponse

router = APIRouter(dependencies=[Depends(require_scope("runs"))])


@router.post("/v1/runs", response_model=RunSubmitResponse, status_code=202)
async def submit_run(request: Request, payload: RunSubmitRequest) -> RunSubmitResponse:
    run = await request.app.state.manager.submit(payload)
    return RunSubmitResponse(
        run_id=run.run_id,
        status=run.status,
        trace_id=run.trace_id,
        conversation_id=run.conversation_id,
    )


@router.get("/v1/runs", response_model=list[AgentRun])
async def list_runs(request: Request, limit: int = 100) -> list[AgentRun]:
    return request.app.state.manager.list(limit)


@router.get("/v1/runs/{run_id}", response_model=AgentRun)
async def get_run(request: Request, run_id: str) -> AgentRun:
    run = request.app.state.manager.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/v1/runs/{run_id}/result")
async def get_result(request: Request, run_id: str) -> dict[str, Any]:
    run = request.app.state.manager.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run.run_id, "status": run.status, "output": run.output}


@router.get("/v1/runs/{run_id}/errors")
async def get_errors(request: Request, run_id: str) -> dict[str, Any]:
    run = request.app.state.manager.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run.run_id, "error_type": run.error_type, "error_message": run.error_message}


@router.get("/v1/runs/{run_id}/logs", response_model=list[LogEvent])
async def get_run_logs(request: Request, run_id: str) -> list[LogEvent]:
    return request.app.state.store.logs.for_run(run_id)


@router.get("/v1/runs/{run_id}/stages", response_model=list[AgentStage])
async def get_run_stages(request: Request, run_id: str) -> list[AgentStage]:
    if request.app.state.manager.get(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return request.app.state.store.stages.list_for_run(run_id)


@router.post("/v1/runs/{run_id}/cancel", response_model=AgentRun)
async def cancel_run(request: Request, run_id: str) -> AgentRun:
    return request.app.state.manager.cancel(run_id)


@router.post("/v1/runs/{run_id}/retry", response_model=AgentRun)
async def retry_run(request: Request, run_id: str) -> AgentRun:
    return await request.app.state.manager.retry(run_id)


@router.get("/v1/runs/{run_id}/callbacks", response_model=list[CallbackDelivery])
async def get_callbacks(request: Request, run_id: str) -> list[CallbackDelivery]:
    return request.app.state.store.callbacks.for_run(run_id)


@router.post("/v1/runs/{run_id}/callbacks/resend", response_model=CallbackDelivery | None)
async def resend_callback(request: Request, run_id: str) -> CallbackDelivery | None:
    return await request.app.state.manager.resend_callback(run_id)


@router.get("/v1/runs/{run_id}/model-calls", response_model=list[ModelCallRecord])
async def get_run_model_calls(request: Request, run_id: str) -> list[ModelCallRecord]:
    return request.app.state.store.model_calls.for_run(run_id)


@router.get("/v1/traces/{trace_id}", response_model=list[LogEvent])
async def get_trace(request: Request, trace_id: str) -> list[LogEvent]:
    return request.app.state.store.logs.for_trace(trace_id)


@router.get("/v1/traces/{trace_id}/model-calls", response_model=list[ModelCallRecord])
async def get_trace_model_calls(request: Request, trace_id: str) -> list[ModelCallRecord]:
    return request.app.state.store.model_calls.for_trace(trace_id)
