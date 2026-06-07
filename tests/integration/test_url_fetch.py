"""Live smoke tests for URL fetching."""

import asyncio

import pytest

from jri.core.tools.explore import fetch_url

TRIPOS_URL = "https://noticias.ulp.edu.ar/ciencia/tripos-5084"


def test_live_fetch_handles_legacy_tls_site(live: bool) -> None:
    """URL fetching handles public sites with legacy DH TLS config."""
    if not live:
        pytest.skip("use --live to run real URL fetch smoke tests")

    result = asyncio.run(fetch_url(TRIPOS_URL))

    assert "Status: 200" in result
    assert f"Final URL: {TRIPOS_URL}" in result
    assert "TRIPOS" in result
