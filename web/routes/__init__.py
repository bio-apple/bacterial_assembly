"""HTTP route modules for the 分析工作台.

Job cancel / submit / step display use web.cancel, web.job_submit, and
web.step_read_model. Flask registers handlers in web.app.create_app;
register_blueprints hooks extracted blueprints as they are added.
"""

from __future__ import annotations


def register_blueprints(app) -> None:
    return None
