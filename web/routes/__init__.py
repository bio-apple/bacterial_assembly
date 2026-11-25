"""HTTP route modules for the 分析工作台.

Job cancel / submit / step display go through web.cancel, web.job_submit, and
web.step_read_model. Flask still registers handlers in web.app.create_app;
import register_blueprints from here as route groups are extracted.
"""

from __future__ import annotations


def register_blueprints(app) -> None:
    """Register extracted blueprints (none yet beyond app.py handlers)."""
    return None
