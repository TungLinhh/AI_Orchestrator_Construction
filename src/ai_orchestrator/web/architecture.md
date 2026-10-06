# Operator console

The console operates a governed AI organization: work is persisted, delegation is
bounded, humans approve exact proposals, and platform decisions remain auditable.
It is not a chat transcript. `docs/CURRENT_STATE.md` and F247/F266 explain why
duplicated rosters and empty dashboards were removed. They remain reference material.

## Navigation ownership

| Destination | Question | Source and control |
|---|---|---|
| Work | What was assigned, and what happened? | Tasks, reports, executions, scoped events; create/run/retry |
| Issues | What needs intervention? | CEO work projection; exact failed/blocked task |
| Approvals | What exact proposal requires my decision? | Approval records and existing human gates |
| Organization | Who owns the work? | Fleet hierarchy; agent detail, state, budget and capabilities |
| Processes | What instructions and gates apply? | SOP catalogue, definitions and recruitment example |
| Library | What can agents reuse? | Skills, tools and provenance-bearing memory |
| Business records | What project or document is this about? | Tenant corpus projections; explicit empty state |
| Operations | Why did the runtime behave this way? | Effective profiles, usage, events, decisions, policy and health |
| Settings | What limits apply to this organization? | Existing organization configuration and admin gate |

## Implementation choice

Two options were evaluated: browser ES modules served as separate assets, or trusted
source modules assembled into the existing single document. ES modules require a
different Node VM verifier and introduce another asset/authentication lifecycle.
Assembly preserves the native deployment, same-origin credential transport and real
page verifier. `page.py` explicitly orders source files; missing assets fail visibly.
There is no generated HTML checked into the repository and no bundler prerequisite.

Core owns transport, escaping and language. Navigation owns route parsing and the
current-navigation token. Work owns task reports and polling. Stream owns event
delivery. Management modules keep their state inside closures and register renderer
functions receiving a route and a `current()` guard. Boot runs last. Incremental
extraction of existing work internals must preserve their shared report state until
that state can move together; splitting it into competing caches is not a refactor.

Every deep link contains an exact resource identifier. Older requests cannot overwrite
a newer route. Mutation forms show server refusal without changing local state to
pretend success. Server transitions and permission checks remain authoritative; no
browser-only privilege grant is allowed. Configuration screens resolve model profiles
through the same tenant catalogue as task execution. Provider failures remain failures.

Future features get a route only when an API and meaningful action exist. Empty corpus
views explain the missing input. Integrations, authentication products and speculative
project writers are not invented by the frontend.
