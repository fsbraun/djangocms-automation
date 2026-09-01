import re

import pytest


def normalize_html(html_string):
    """Normalize HTML by removing extra whitespace"""
    # Remove all 'aria-describedby' attributes
    html_string = re.sub(r'\s*aria-describedby="[^"]*"', "", html_string)
    # Remove whitespace between tags
    html_string = re.sub(r">\s+<", "><", html_string.strip())
    # Normalize internal whitespace
    html_string = re.sub(r"\s+", " ", html_string)
    return html_string


@pytest.fixture
def assert_html_in_response():
    """
    Assert that an HTML fragment exists in the response, ignoring whitespace.
    Similar to Django's assertContains with html=True
    """

    def assert_html(fragment, response, status_code=200):
        assert response.status_code == status_code

        # Normalize both the response content and the fragment
        response_content = normalize_html(response.content.decode("utf-8"))
        normalize_fragment = normalize_html(fragment)
        assert normalize_fragment in response_content, (
            f"Expected HTML fragment not found in response.\n"
            f"Fragment: {normalize_fragment}\n"
            f"Response: {response_content}"
        )

    return assert_html


@pytest.fixture(autouse=True)
def _forget_which_providers_refused_a_schema():
    """What one test learned about a provider is not the next test's business.

    ``llm`` remembers a model that refused an output schema so the next call
    asks the other way round first. Kept across tests it would silently skip
    the first attempt somewhere that was checking for it.
    """
    from djangocms_automation.ai.llm import _SHAPE_NEEDS_A_TOOL

    _SHAPE_NEEDS_A_TOOL.clear()
    yield
    _SHAPE_NEEDS_A_TOOL.clear()
