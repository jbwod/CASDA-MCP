from __future__ import annotations

import json

from casda_mcp.server import create_mcp_server
from casda_mcp.skills_loader import get_skill, list_skills

EXPECTED_PROMPTS = {
    "find-and-inspect-products",
    "stage-and-download",
    "build-reproducible-selection",
    "query-catalogue",
    "query-tables",
    "run-adql",
    "make-cutout",
    "monitor-releases",
    "dap-navigate",
}

EXPECTED_SKILLS = {
    "casda-safe-archive-access",
    "casda-find-and-inspect",
    "casda-stage-and-download",
    "casda-reproducible-manifest",
}


async def test_list_prompts_includes_planned_workflow_names() -> None:
    server = create_mcp_server()
    prompts = await server.list_prompts()
    assert {prompt.name for prompt in prompts} == EXPECTED_PROMPTS
    await server.casda_service.aclose()  # type: ignore[attr-defined]


async def test_make_cutout_prompt_uses_cutout_workflow() -> None:
    server = create_mcp_server()
    result = await server.get_prompt("make-cutout")
    text = result.messages[0].content.text  # type: ignore[union-attr]
    assert "casda_create_cutout" in text
    assert "casda_get_data_job" in text
    await server.casda_service.aclose()  # type: ignore[attr-defined]


async def test_query_tables_and_run_adql_prompts() -> None:
    server = create_mcp_server()
    tables = await server.get_prompt("query-tables")
    tables_text = tables.messages[0].content.text  # type: ignore[union-attr]
    assert "casda_list_schemas" in tables_text
    assert "casda_describe_table" in tables_text
    adql = await server.get_prompt("run-adql")
    adql_text = adql.messages[0].content.text  # type: ignore[union-attr]
    assert "casda_validate_adql" in adql_text
    assert "CASDA_ENABLE_ADVANCED_ADQL" in adql_text
    await server.casda_service.aclose()  # type: ignore[attr-defined]


async def test_skills_index_and_markdown_resources() -> None:
    server = create_mcp_server()
    skills = list_skills()
    assert {skill.name for skill in skills} == EXPECTED_SKILLS

    index_content = await server.read_resource("casda://skills")
    index_text = index_content[0].content  # type: ignore[index]
    if hasattr(index_text, "text"):
        index_text = index_text.text
    payload = json.loads(str(index_text))
    assert {item["name"] for item in payload["skills"]} == EXPECTED_SKILLS

    for name in sorted(EXPECTED_SKILLS):
        skill = get_skill(name)
        assert skill.markdown.startswith("---\n")
        assert f"name: {name}" in skill.markdown
        assert skill.description
        resource = await server.read_resource(f"casda://skills/{name}")
        body = resource[0].content  # type: ignore[index]
        if hasattr(body, "text"):
            body = body.text
        assert str(body).startswith("---\n")
        assert f"name: {name}" in str(body)

    await server.casda_service.aclose()  # type: ignore[attr-defined]
