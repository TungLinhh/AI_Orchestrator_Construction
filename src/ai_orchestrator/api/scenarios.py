"""The pieces of work a person can hand the company, as a list.

Sits beside `/tasks` rather than under it because this is a catalogue, not a task:
nothing here is work that has been asked for yet. The page needs it to offer
something real, and until now the "Start a task" card offered a free-text box --
which asks the person using it to know both what work exists and who does it,
which are the two things the organisation exists to hold.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ai_orchestrator.api.deps import ApiContext, get_context

router = APIRouter(tags=["scenarios"])


@router.get("/scenarios")
async def list_scenarios(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """Every scenario, with the goal written out in full.

    The goal travels whole rather than as a title, because a title is not
    something an agent can do: "Reconcile the tender documents" names a task with
    no document, no supplier and no amount. The full text is what makes finishing
    it possible, and `expected_output` is the contract the run is held to.

    `ctx` is taken and not used. It is there so the endpoint answers under the same
    tenant rules as everything else: a caller with no organisation gets a
    rejection here for the same reason it gets one everywhere else, rather than
    discovering the catalogue is public by accident.
    """
    from ai_orchestrator.application.scenarios import catalogue

    del ctx
    return {"scenarios": catalogue()}
