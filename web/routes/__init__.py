"""HTTP route modules for the 分析工作台.

Job cancel / submit / step display use web.cancel, web.job_submit, and
web.step_read_model. Flask registers handlers in web.app.create_app;
register_blueprints hooks extracted blueprints as they are added.
"""

from __future__ import annotations

from flask import Blueprint

from web.routes import catalog as catalog_routes
from web.routes import jobs as jobs_routes
from web.routes import uploads as uploads_routes


def register_blueprints(app, hooks) -> None:
    uploads_bp = Blueprint("uploads", __name__)
    jobs_bp = Blueprint("jobs", __name__)
    catalog_bp = Blueprint("catalog", __name__)
    uploads_routes.register_routes(uploads_bp, hooks)
    jobs_routes.register_routes(jobs_bp, hooks)
    catalog_routes.register_routes(catalog_bp, hooks)

    # Flask allows only one nameless blueprint; merge route records for stable endpoints.
    http_bp = Blueprint("http_handlers", __name__)
    for bp in (uploads_bp, jobs_bp, catalog_bp):
        for deferred in bp.deferred_functions:
            http_bp.record(deferred)
    app.register_blueprint(http_bp, name="")