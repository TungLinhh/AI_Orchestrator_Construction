"""The eight agents of the dossier's register, in its order, with the reason for each.

## Why this is a module and not a seed script

A seed script's content is invisible to a reader: the reasons an agent exists live in a
string literal in a function that runs once. Putting the register here means the register
can be **asserted** — the invariants below are properties of the dossier's table 10 and a
test is the only thing that notices when one of them stops holding.

Tập 1's table 10 names eight agents with priorities. The order is not
construction-first, and getting that wrong is the mistake this module exists to prevent: it
is tempting to build Procurement first because the supply chain is already migrated. Tập 2
§G.2 calls the HR Agent the *"pilot chuẩn cho mọi agent sau"* — the standard pilot for
every agent after it — and it is first **because it needs no domain tables to be useful.**
A pilot that needs 76 tables finished before it can run is not a pilot.

## The four invariants, and why each is here

* **HR is first.** Not a scheduling accident: the dossier says so, and the reasoning holds
  on its own. HR runs on `BO-HR-SOP-004/005` and touches no construction table.
* **Procurement is P2 because its tables already exist.** `rfqs` through
  `material_reconciliations` are migrated and tested. A P2 agent backed by a finished
  tranche is a different proposition from a P1 agent backed by an unfinished one.
* **Every agent's SOPs are checked to exist at seed time.** All sixteen are seeded in
  `sop_definitions` under an `ONX-` prefix. A typo in a code would otherwise produce a
  procedure with no SOP behind it, and the agent would run with nothing to follow.
* **Every agent starts granted L1, whatever its ceiling.** The ceiling is what the dossier
  permits; the grant is what this build has evidence for. They are separate columns and
  conflating them is how an agent ends up authorised to spend money because somebody set a
  ceiling.

## The roles are a mismatch, and it is recorded rather than papered over

The register carries nine roles: Back Office, Executive, Finance, IT, Marketing, Programme
Manager, Quality, Risk, Sales. **There is no Procurement Director and no Design Director**,
so two of the eight agents attach to the nearest existing authority and `ROLE_GAP` says so.
Inventing two roles to make the mapping tidy would be a nicer table and a worse system: the
role set is the dossier's, and the gap is a finding about the dossier.

## The model tiers do not exist here, and what replaced them

The dossier assigns Sonnet / Opus / Haiku / Vision by tier. This build is free-tier only,
so a tier is not a model choice: `primary` and `fast_general` are the two profiles, and both
resolve to a free OpenRouter model. What the tier *did* encode — which agents need the
stronger model — is kept as `needs_strong_model`, and it is currently informational. A free
tier has one serious model, so every agent runs on it and the distinction is a note rather
than a routing decision. Recording it keeps the requirement visible for when that changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The six action classes Tập 1 §5.3 defines, as seeded in `autonomy_policies`.
#:
#: An earlier draft of this register invented its own vocabulary -- `read`, `draft`,
#: `record`, `reconcile` -- and **the promotion gate refused every one of them** with
#: `unclassified_action`: *"touches read, draft, record, which is not in the autonomy
#: policy table, so nobody has decided how autonomous to be about it."*
#:
#: That refusal is the system working, and it is why this list exists. An action class is
#: not a label an agent picks; it is a row somebody approved, with a level and possibly a
#: hard block attached. Inventing one produces a procedure nobody decided anything about.
DOSSIER_ACTION_CLASSES: tuple[str, ...] = (
    "hr_personnel_decision",  # L2
    "financial_commitment",  # L3
    "safety_conclusion",  # L1, hard block
    "supplier_risk_flagged",  # L1, hard block
    "contract_signature",  # L3
    "routine_classification",  # L4
)

#: The two classes that are a hard block: refused at **every** autonomy level, L4 included.
#: Checked before the level comparison, so an agent claiming L4 on a safety conclusion is
#: still refused.
HARD_BLOCK_CLASSES: frozenset[str] = frozenset({"safety_conclusion", "supplier_risk_flagged"})

#: The `sop_definitions.code` prefix. The sixteen codes below are written without it,
#: because the prefix is a property of the document numbering scheme and not of the agent.
SOP_PREFIX = "ONX-"

#: The role this build has and the dossier's eight agents do not cover. See the module
#: docstring: inventing the two missing roles would tidy the table and falsify the register.
ROLE_GAP: dict[str, str] = {
    "Procurement Agent": "Programme Manager",
    "Design/M&E Agent": "Quality Director",
}


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One line of the dossier's table 10, plus what this build has to decide about it."""

    #: The dossier's name. Not a display name -- the code, the seed and the UI all use it.
    name: str
    #: `P1`..`P4` from the dossier. Kept because it is the dossier's own prioritisation and
    #: re-deriving one would be a different claim.
    priority: str
    #: Tập 1's block: Back Office, Middle Office, PMO, Front Office.
    block: str
    #: SOP codes without the `ONX-` prefix.
    sop_codes: tuple[str, ...]
    #: The dossier's model tier, kept as a requirement rather than a routing decision.
    model_tier: str
    #: The role this agent is attached to. See `ROLE_GAP`.
    role_name: str
    #: **Not declared here.** An agent's ceiling is the strictest level its action classes
    #: permit, read from `autonomy_policies` at seed time. Declaring it in the register was
    #: an earlier mistake: the numbers were invented and they disagreed with the table.
    #: What this build has evidence for, and therefore grants. L1 for all eight.
    granted_level: str
    #: The profile it binds to, by name. Read from `model_profiles` at run time.
    model_profile: str
    #: The reason this agent is in the register. Not decoration: `test_register_invariants`
    #: fails if a justification is shorter than a sentence.
    justification: str
    #: The tier distinction, kept visible for a future with more than one model.
    needs_strong_model: bool = False
    #: Action classes the agent may take at its granted level. Narrow on purpose, and
    #: **never one from `HARD_BLOCK_CLASSES`.** The promotion gate refuses a version that
    #: touches a hard-blocked class, which is right; the design consequence is that an
    #: agent must not *hold* one, because an agent holding a class is an agent that can be
    #: asked for it. The blocked act appears in `must_refuse` instead, which is where a
    #: person can read what the agent will not do.
    action_classes: tuple[str, ...] = field(default=())
    #: Acts this agent must decline, with the reason. Tập 1 §5.3's hard blocks live here
    #: for the two agents whose domain contains them.
    must_refuse: tuple[str, ...] = field(default=())
    #: RAG over the document library, per the dossier for the Knowledge Agent.
    uses_retrieval: bool = False
    #: Vision input, per the dossier for QA/QC-HSE.
    uses_vision: bool = False

    @property
    def sop_codes_prefixed(self) -> tuple[str, ...]:
        return tuple(f"{SOP_PREFIX}{code}" for code in self.sop_codes)


#: The register, in the dossier's order. **Do not reorder.** The order is the dossier's
#: prioritisation and it is asserted by `test_register_invariants`.
REGISTER: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="HR Agent",
        priority="P1",
        block="Back Office",
        sop_codes=("BO-HR-SOP-004", "BO-HR-SOP-005"),
        model_tier="Sonnet",
        role_name="Back Office Director",
        granted_level="L1",
        model_profile="primary",
        justification=(
            "Tập 2 §G.2 names this the standard pilot for every agent after it, and the "
            "reason holds independently: HR runs on two Back Office SOPs and touches no "
            "construction table, so it can be exercised before any domain tranche is "
            "finished. First for that reason, not because HR is the most valuable agent."
        ),
        action_classes=("hr_personnel_decision",),
    ),
    AgentSpec(
        name="Procurement Agent",
        priority="P2",
        block="Middle Office",
        sop_codes=("MO-PRC-SOP-005", "MO-PRC-SOP-006"),
        model_tier="Opus",
        role_name="Programme Manager",
        granted_level="L1",
        model_profile="primary",
        justification=(
            "The only P2 agent whose tables are already migrated and tested: the chain from "
            "`rfqs` through `material_reconciliations` is ten tables with a proven "
            "3-way match. Its ceiling is L2 because a requisition is reversible and a "
            "purchase order is not, and the grant stays L1 until shadow runs show it "
            "drafts an order a human would have issued."
        ),
        must_refuse=(
            "Transacting with a supplier carrying a risk flag (forbidden or "
            "legal). Tập 1 §5.3 lists it as an absolute prohibition: there is no level at "
            "which automating it is permitted, so the agent does not hold the class at "
            "all. It escalates to Legal and freezes the requisition instead.",
            "Issuing a purchase order. The agent drafts one; a human issues it.",
        ),
        needs_strong_model=True,
        action_classes=("financial_commitment",),
    ),
    AgentSpec(
        name="Knowledge Agent",
        priority="P2",
        block="PMO",
        sop_codes=("PMO-KNW-SOP-006",),
        model_tier="Haiku/Sonnet, RAG",
        role_name="IT Director",
        granted_level="L1",
        model_profile="fast_general",
        justification=(
            "RAG over a library of 1,540 documents with two readers already proved "
            "against it. It writes nothing, so its blast radius is an answer rather than a "
            "record -- which is why it sits at P2 in parallel with Procurement rather than "
            "after it."
        ),
        uses_retrieval=True,
        action_classes=("routine_classification",),
    ),
    AgentSpec(
        name="Finance Agent",
        priority="P3",
        block="Back Office",
        sop_codes=("BO-FIN-SOP-001", "BO-FIN-SOP-002", "BO-FIN-SOP-003"),
        model_tier="Sonnet",
        role_name="Finance Director",
        granted_level="L1",
        model_profile="primary",
        justification=(
            "Three SOPs covering reconciliation, retention and payment, and the only agent "
            "whose output is money. L1 for the grant is not caution for its own sake: a "
            "posting is the one write in this system that cannot be reversed by a "
            "correction, so it needs a shadow run per action class before it is granted."
        ),
        action_classes=("financial_commitment", "contract_signature"),
    ),
    AgentSpec(
        name="Project Mgmt Agent",
        priority="P3",
        block="Middle Office and PMO",
        sop_codes=("MO-PM-SOP-002", "MO-PM-SOP-003", "PMO-REP-SOP-002"),
        model_tier="Sonnet",
        role_name="Programme Manager",
        granted_level="L1",
        model_profile="primary",
        justification=(
            "Schedule and progress, which is the one domain where this build has real data: "
            "2,778 progress readings across 240 WBS nodes, all reachable. Its ceiling is the "
            "highest of the eight at L3 because a baseline re-plan is a business decision, "
            "and the gap between L3 and the L1 grant is the widest in the register."
        ),
        action_classes=("routine_classification",),
    ),
    AgentSpec(
        name="Design/M&E Agent",
        priority="P4",
        block="Middle Office",
        sop_codes=("MO-DES-SOP-001",),
        model_tier="Opus",
        role_name="Quality Director",
        granted_level="L1",
        model_profile="primary",
        justification=(
            "Reviews a drawing against the MEP packages in the corpus. Reads and comments; "
            "it does not approve a drawing, because approving one is a licensed act. P4 in "
            "the dossier and last of the middle-office agents here."
        ),
        needs_strong_model=True,
        action_classes=("routine_classification",),
    ),
    AgentSpec(
        name="Sales/BD Agent",
        priority="P4",
        block="Front Office",
        sop_codes=("FO-BD-SOP-001", "FO-TE-SOP-002"),
        model_tier="Sonnet",
        role_name="Sales Director",
        granted_level="L1",
        model_profile="primary",
        justification=(
            "Drafts a quotation from the tender data. A *sent* quotation is a commitment, "
            "so the agent's output stops at draft and the ceiling reflects that the send is "
            "a human act."
        ),
        action_classes=("contract_signature", "routine_classification"),
    ),
    AgentSpec(
        name="QA/QC-HSE Agent",
        priority="P4",
        block="Middle Office",
        sop_codes=("MO-QA-SOP-007", "MO-HSE-SOP-008"),
        model_tier="Vision + Sonnet",
        role_name="Risk Director",
        granted_level="L1",
        model_profile="primary",
        justification=(
            "Quality and safety over one corpus, so a stop-work recommendation is a "
            "*proposal* and never the stop itself. The dossier assigns it a vision model for "
            "site photographs; this build has no free vision model, so `uses_vision` is "
            "recorded and unmet rather than silently dropped."
        ),
        must_refuse=(
            "Concluding a workplace-safety matter — a stop-work decision, an incident "
            "finding. Tập 1 §5.3 lists it as an absolute prohibition. The agent raises a "
            "recommendation with its evidence and a human decides; a stop-work is the "
            "human's act, not the agent's.",
        ),
        uses_vision=True,
        action_classes=("routine_classification",),
    ),
)

#: Every action class any agent claims. Used by the seeder to widen the procedure's
#: classification, and by a test to check the union is not the empty set.
ALL_ACTION_CLASSES: frozenset[str] = frozenset(
    cls for spec in REGISTER for cls in spec.action_classes
)


#: Level ordering, loosest last. Used only to pick the strictest of a set.
_LEVEL_ORDER: dict[str, int] = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}


def ceiling_for(spec: AgentSpec, permitted: dict[str, str]) -> str:
    """The strictest level `spec`'s action classes permit, or `L1` if none is known.

    `permitted` is `{action_class: max_level}` read from `autonomy_policies`. **A class the
    table does not have yields `L1`**, which is the safe direction: an unclassified class
    must not be able to *raise* an agent's ceiling.

    The strictest rather than the loosest, because a procedure's ceiling is the weakest link
    in the set it touches. An agent that both reconciles a budget and commits money is
    bounded by the commitment, not by the reconciliation.

    A hard-block class is not a ceiling question at all -- the promotion gate refuses those
    at every level -- so it is reported here as the level the table carries and the gate
    does the rest.
    """
    levels = [
        _LEVEL_ORDER[permitted[name]]
        for name in spec.action_classes
        if name in permitted and permitted[name] in _LEVEL_ORDER
    ]
    if not levels:
        return "L1"
    strictest = min(levels)
    return next(name for name, order in _LEVEL_ORDER.items() if order == strictest)


def by_name(name: str) -> AgentSpec:
    for spec in REGISTER:
        if spec.name == name:
            return spec
    raise KeyError(f"no agent named {name!r} in the register")
