"""HTTP route modules and their aggregated APIRouter exports."""

from interfaces.http.routes.admin import router as admin_router
from interfaces.http.routes.health import router as health_router
from interfaces.http.routes.kanban import router as kanban_router
from interfaces.http.routes.operations import router as operations_router
from interfaces.http.routes.runs import router as runs_router

__all__ = [
    "admin_router",
    "health_router",
    "kanban_router",
    "operations_router",
    "runs_router",
]
