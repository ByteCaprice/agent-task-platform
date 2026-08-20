# HTTP API Reference

The FastAPI application exposes an OpenAPI document at `/openapi.json` and interactive documentation at `/docs` when enabled by the server.

## Run API

All Run routes require the `runs` scope.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/runs` | Submit an asynchronous Run |
| GET | `/v1/runs` | List Runs |
| GET | `/v1/runs/{run_id}` | Read a Run |
| GET | `/v1/runs/{run_id}/result` | Read output only |
| GET | `/v1/runs/{run_id}/errors` | Read structured error fields |
| GET | `/v1/runs/{run_id}/logs` | Read Run logs |
| GET | `/v1/runs/{run_id}/stages` | Read durable stages |
| POST | `/v1/runs/{run_id}/cancel` | Request cancellation |
| POST | `/v1/runs/{run_id}/retry` | Requeue an eligible Run |
| GET | `/v1/runs/{run_id}/callbacks` | Read callback delivery attempts |
| POST | `/v1/runs/{run_id}/callbacks/resend` | Retry callback delivery |

## Registry API

`GET /v1/agents`, `GET /v1/tools`, and `GET /v1/skills` list registered capabilities. Registry mutation endpoints are under `/v1/admin/*` and require the `admin` scope.

## Operations API

`GET /v1/queue`, `GET /v1/queue/dead-letter`, `GET /v1/callbacks/dead-letter`, and `GET /v1/metrics` require the `operations` scope.

## Health API

- `/livez` reports whether the process can answer requests.
- `/readyz` verifies platform readiness without returning database exception details.
- `/healthz` is a general health response.

## Errors

Errors use a stable envelope:

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "run not found"
  }
}
```

Do not parse human-readable messages for control flow. Use HTTP status and `error.code`.
