"""FastAPI web application for the search-enhancement service.

This package exposes three surfaces from a single app:

* the **ingest API** (``/api/requests``) that the SharePoint SPFx command calls
  to enqueue enhancement work,
* the user-facing **status page** (``/s/{token}``) reachable through an opaque
  link, and
* the **admin portal** (``/admin``) gated by an opaque admin key.

The app is assembled by :func:`crewmeal.search_enhancement.web.app.create_app`.
"""

from crewmeal.search_enhancement.web.app import create_app, create_app_from_env

__all__ = ["create_app", "create_app_from_env"]
