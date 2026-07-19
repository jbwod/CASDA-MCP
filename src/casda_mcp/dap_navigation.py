"""Safe DAP deep-link templates (URL construction only; never scrapes or mutates DAP)."""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote, urlencode

from casda_mcp.models import DapLink, UnsupportedDapAction

# Documented public entry points (CASDA user guide / data.csiro.au / research.csiro.au/casda).
CASDA_HOME = "https://research.csiro.au/casda/"
OBSERVATION_SEARCH = "https://data.csiro.au/domain/casdaObservation"
SKYMAP = "https://data.csiro.au/domain/casdaSkymap"
DAP_SEARCH = "https://data.csiro.au/search"
DAP_HOME = "https://data.csiro.au/"
DOI_RESOLVER = "https://doi.org"
USER_GUIDE = "https://research.csiro.au/casda/casda-user-guide/"
LEVEL7_GUIDE = "https://research.csiro.au/casda/casda-user-guide/"

PrivilegedAction = Literal[
    "accept_licence",
    "assign_project_role",
    "level7_deposit",
    "release_observation",
    "mint_doi",
    "launch_carta",
]

PRIVILEGED_ACTIONS: frozenset[str] = frozenset(
    {
        "accept_licence",
        "assign_project_role",
        "level7_deposit",
        "release_observation",
        "mint_doi",
        "launch_carta",
    }
)

NAVIGATION_TEMPLATES: dict[str, str] = {
    "casda_home": CASDA_HOME,
    "observation_search": OBSERVATION_SEARCH,
    "skymap": SKYMAP,
    "dap_search": f"{DAP_SEARCH}?q={{query}}",
    "doi_landing": f"{DOI_RESOLVER}/{{doi}}",
    "user_guide": USER_GUIDE,
}


def build_dap_navigation(
    *,
    product_id: str | None = None,
    scheduling_block_id: int | None = None,
    project_code: str | None = None,
    collection: str | None = None,
    request_id: str | None = None,
    action: str | None = None,
) -> tuple[list[DapLink], list[UnsupportedDapAction]]:
    """Construct deep links and structured unsupported privileged actions."""

    links: list[DapLink] = [
        DapLink(
            title="CASDA home",
            url=CASDA_HOME,
            purpose="CASDA documentation and service overview.",
            requires_human=False,
        ),
        DapLink(
            title="Observation Search",
            url=OBSERVATION_SEARCH,
            purpose="Interactive ObsCore/product search in the Data Access Portal.",
            requires_human=True,
        ),
        DapLink(
            title="CASDA Skymap",
            url=SKYMAP,
            purpose="Interactive sky map selection and preview in the DAP.",
            requires_human=True,
        ),
        DapLink(
            title="CASDA user guide",
            url=USER_GUIDE,
            purpose="Authoritative human documentation for archive and DAP workflows.",
            requires_human=False,
        ),
    ]

    if product_id:
        query = urlencode({"q": product_id})
        links.append(
            DapLink(
                title="DAP search for product",
                url=f"{DAP_SEARCH}?{query}",
                purpose="Search the DAP for the given product identifier.",
                requires_human=True,
            )
        )
    if scheduling_block_id is not None:
        query = urlencode({"q": f"SBID {scheduling_block_id}"})
        links.append(
            DapLink(
                title="Observation Search for scheduling block",
                url=f"{OBSERVATION_SEARCH}?{query}",
                purpose="Open Observation Search with the scheduling block as a search hint.",
                requires_human=True,
            )
        )
    if project_code:
        query = urlencode({"q": project_code})
        links.append(
            DapLink(
                title="DAP search for project",
                url=f"{DAP_SEARCH}?{query}",
                purpose="Search DAP collections/projects for the OPAL project code.",
                requires_human=True,
            )
        )
    if collection:
        query = urlencode({"q": collection})
        links.append(
            DapLink(
                title="DAP search for collection",
                url=f"{DAP_SEARCH}?{query}",
                purpose="Search DAP for the collection / obs_collection name.",
                requires_human=True,
            )
        )
        links.append(
            DapLink(
                title="Collection DOI resolver hint",
                url=f"{DOI_RESOLVER}/",
                purpose=(
                    "Public DOI landing pages use https://doi.org/{doi}. Resolve a known "
                    "DOI with casda_resolve_collection_doi; this server does not invent DOIs."
                ),
                requires_human=False,
            )
        )
    if request_id:
        links.append(
            DapLink(
                title="Data request / job follow-up",
                url=OBSERVATION_SEARCH,
                purpose=(
                    f"Archive job request_id={request_id!r} was created by this MCP server. "
                    "Poll status with casda_get_data_job; for DAP account or licence UI, "
                    "continue as a human in Observation Search / DAP."
                ),
                requires_human=True,
            )
        )

    unsupported: list[UnsupportedDapAction] = []
    if action is not None and action.strip():
        code = action.strip()
        if code in PRIVILEGED_ACTIONS:
            unsupported.append(_unsupported_for(code))
        else:
            unsupported.append(
                UnsupportedDapAction(
                    code="UNKNOWN_ACTION",
                    message=(
                        f"Action {code!r} is not a recognised DAP navigation action. "
                        f"Known privileged codes: {', '.join(sorted(PRIVILEGED_ACTIONS))}."
                    ),
                    navigation_url=USER_GUIDE,
                )
            )
    return links, unsupported


def _unsupported_for(code: str) -> UnsupportedDapAction:
    messages = {
        "accept_licence": (
            "Licence acknowledgement must be completed by a human in the DAP. "
            "This MCP server never auto-accepts legal terms."
        ),
        "assign_project_role": (
            "Project role assignment is privileged DAP administration and is out of scope."
        ),
        "level7_deposit": (
            "Level 7 deposit creation and publishing is a privileged DAP/Pawsey workflow "
            "and is out of scope for automation."
        ),
        "release_observation": (
            "Observation release/reject and quality administration remain DAP-boundary."
        ),
        "mint_doi": (
            "DOI minting and DataCite administration are out of scope. Use "
            "casda_resolve_collection_doi for public read-only citation metadata."
        ),
        "launch_carta": (
            "CARTA viewer/session launch is an interactive DAP workflow and is not automated."
        ),
    }
    urls = {
        "accept_licence": DAP_HOME,
        "assign_project_role": USER_GUIDE,
        "level7_deposit": LEVEL7_GUIDE,
        "release_observation": USER_GUIDE,
        "mint_doi": "https://research.csiro.au/dap/discover/permanent-identifiers/",
        "launch_carta": OBSERVATION_SEARCH,
    }
    return UnsupportedDapAction(
        code=code,
        message=messages[code],
        navigation_url=urls[code],
    )


def navigation_resource_payload() -> dict[str, object]:
    """Static summary for casda://dap/navigation."""

    return {
        "description": (
            "Safe DAP navigation templates. Tools construct HTTPS URLs only; "
            "they never scrape DAP HTML or perform privileged mutations."
        ),
        "templates": NAVIGATION_TEMPLATES,
        "privileged_actions_unsupported": sorted(PRIVILEGED_ACTIONS),
        "tool": "casda_get_dap_navigation",
        "notes": [
            "Observation Search and Skymap require a human browser session.",
            "Licence acceptance, roles, Level 7 deposit, release/reject, DOI minting, "
            "and CARTA launch are never automated.",
            f"Example DOI landing: {DOI_RESOLVER}/{quote('10.25919/example')}",
        ],
    }
