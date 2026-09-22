"""Shared Google Search Console API client helper.

Used by both fetch_search_console.py (keyword-demand signal) and
analyze_performance.py (weekly title/description improvement report)
so the credential-loading logic lives in exactly one place.
"""

from __future__ import annotations

import json
import os


class GSCUnavailable(Exception):
    """Raised when Search Console credentials are missing or invalid."""


def build_service():
    """Return (service, site_url) for the Search Console API.

    Raises GSCUnavailable if GSC_SERVICE_ACCOUNT_KEY / GSC_SITE_URL are
    not set or the key isn't valid JSON. Callers should treat this as a
    "feature not configured yet" signal, not a hard failure.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    service_account_json = os.environ.get("GSC_SERVICE_ACCOUNT_KEY")
    site_url = os.environ.get("GSC_SITE_URL")

    if not service_account_json or not site_url:
        raise GSCUnavailable("GSC_SERVICE_ACCOUNT_KEY or GSC_SITE_URL not set")

    try:
        service_account_info = json.loads(service_account_json)
    except json.JSONDecodeError as exc:
        raise GSCUnavailable(f"GSC_SERVICE_ACCOUNT_KEY is not valid JSON: {exc}") from exc

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    service = build("searchconsole", "v1", credentials=credentials)
    return service, site_url
