"""HTTP route modules for the 分析工作台.

Job cancel / submit / step display use web.cancel, web.job_submit, and
web.step_read_model. Flask registers handlers in web.app.create_app;
register_blueprints hooks extracted blueprints as they are added.
"""

from __future__ import annotations

from flask import Blueprint

from web.routes import jobs as jobs_routes
from web.routes import uploads as uploads_routes


def register_blueprints(app, hooks) -> None:
    http_bp = Blueprint("http_handlers", __name__)
    uploads_routes.register_routes(http_bp, hooks)
    jobs_routes.register_routes(http_bp, hooks)
    app.register_blueprint(http_bp, name="")