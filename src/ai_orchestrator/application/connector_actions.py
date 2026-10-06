"""One stable write intent, commit before SMTP, reconcile uncertain delivery read-only."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from sqlalchemy import select, text

from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import PreconditionError
from ai_orchestrator.domain.ids import make_id
from ai_orchestrator.persistence.models import ConnectorAction
from ai_orchestrator.persistence.session import Database


class ConnectorUnknown(PreconditionError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class ConnectorActions:
    def __init__(self, db: Database, org: str) -> None:
        self.db, self.org = db, org

    async def send_tests(
        self, root: str, stage: str, mailbox: Any, cvs: list[dict[str, str]]
    ) -> dict[str, Any]:
        key = digest({"org": self.org, "root": root, "stage": stage, "action": "smtp_self_test_v1"})
        snapshot: dict[str, Any] = {
            "protocol": "SMTP_SSL",
            "recipient": mailbox.settings.recruitment_mail_address
            if hasattr(mailbox, "settings")
            else "fixture",
            "synthetic": True,
            "messages": [
                {
                    "message_id": "<onx." + key + "." + str(i) + "@o-nexus.invalid>",
                    "filename": cv["filename"],
                    "candidate_name": cv["name"],
                    "sha256": hashlib.sha256(cv["text"].encode()).hexdigest(),
                }
                for i, cv in enumerate(cvs)
            ],
        }
        async with self.db.engine.begin() as lock:
            acquired = await lock.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:org), hashtext(:key))"),
                {"org": self.org, "key": "connector:" + key},
            )
            if not acquired:
                raise ConnectorUnknown("Another process is observing this external action")
            async with self.db.tenant_session(self.org) as session:
                row = await session.scalar(
                    select(ConnectorAction).where(
                        ConnectorAction.organization_id == self.org,
                        ConnectorAction.action_key == key,
                    )
                )
                if row:
                    if row.input_hash != digest(snapshot):
                        raise PreconditionError(
                            "Prepared connector input changed; create a new revision"
                        )
                    if row.state == "confirmed":
                        return {**row.receipt, "action_id": row.id, "reused_receipt": True}
                    if row.state in {"sending", "unknown"}:
                        raise ConnectorUnknown(
                            "SMTP outcome is uncertain; reconcile before retry",
                            details={"action_id": row.id},
                        )
                else:
                    row = ConnectorAction(
                        id=make_id("act"),
                        organization_id=self.org,
                        root_task_id=root,
                        stage_task_id=stage,
                        action_key=key,
                        input_hash=digest(snapshot),
                        snapshot=snapshot,
                        state="prepared",
                        receipt={},
                    )
                    session.add(row)
                    await session.flush()
                action_id = row.id
            # Prepared is durable before attempting a write. Sending is an intent,
            # not proof of acceptance; process death here must never imply failure.
            async with self.db.tenant_session(self.org) as session:
                row = await session.get(ConnectorAction, action_id)
                assert row is not None
                row.state = "sending"
            try:
                if hasattr(mailbox, "send_prepared"):
                    receipt = await asyncio.to_thread(
                        mailbox.send_prepared,
                        root,
                        cvs,
                        [m["message_id"] for m in snapshot["messages"]],
                    )
                else:
                    receipt = await asyncio.to_thread(mailbox.send_tests, root, cvs)
                async with self.db.tenant_session(self.org) as session:
                    row = await session.get(ConnectorAction, action_id)
                    assert row is not None
                    row.state, row.receipt = "confirmed", receipt
                    await AuditService(session, self.org).record(
                        actor=Actor(id="connector-actions", kind=ActorType.SYSTEM),
                        action="connector.smtp.accepted",
                        resource_type="connector_action",
                        resource_id=action_id,
                        task_id=stage,
                        context={
                            "action_key": key,
                            "input_hash": row.input_hash,
                            "message_ids": [m["message_id"] for m in snapshot["messages"]],
                        },
                    )
                return {**receipt, "action_id": action_id, "reused_receipt": False}
            except BaseException as exc:
                async with self.db.tenant_session(self.org) as session:
                    row = await session.get(ConnectorAction, action_id)
                    assert row is not None
                    row.state, row.last_error = "unknown", type(exc).__name__
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ConnectorUnknown(
                    "SMTP may have accepted a message; read-back required",
                    details={"action_id": action_id},
                ) from exc

    async def reconcile(self, action_id: str, mailbox: Any, actor: Actor) -> dict[str, Any]:
        async with self.db.tenant_session(self.org) as session:
            row = await session.get(ConnectorAction, action_id)
            if not row:
                raise PreconditionError("Connector action is not available")
            key = row.action_key
        async with self.db.engine.begin() as lock:
            acquired = await lock.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:org), hashtext(:key))"),
                {"org": self.org, "key": "connector:" + key},
            )
            if not acquired:
                raise PreconditionError("SMTP write is still active; wait before reconciling")
            return await self._reconcile(action_id, mailbox, actor)

    async def _reconcile(self, action_id: str, mailbox: Any, actor: Actor) -> dict[str, Any]:
        async with self.db.tenant_session(self.org) as session:
            row = await session.get(ConnectorAction, action_id)
            if row is None:
                raise PreconditionError("Connector action is not available in this organization")
            root, stage, snapshot = row.root_task_id, row.stage_task_id, row.snapshot
            if row.state == "confirmed":
                return {
                    "id": row.id,
                    "state": row.state,
                    "receipt": row.receipt,
                    "stage_task_id": stage,
                }
            if row.state == "prepared":
                return {
                    "id": row.id,
                    "state": "prepared",
                    "stage_task_id": stage,
                    "not_attempted": True,
                }
        observed = await asyncio.to_thread(mailbox.reconcile_tests, root, snapshot)
        async with self.db.tenant_session(self.org) as session:
            row = await session.scalar(
                select(ConnectorAction)
                .where(ConnectorAction.organization_id == self.org, ConnectorAction.id == action_id)
                .with_for_update()
            )
            assert row is not None
            if observed.get("confirmed"):
                row.state, row.receipt, row.last_error = "confirmed", observed["receipt"], None
            await AuditService(session, self.org).record(
                actor=actor,
                action="connector.smtp.reconciled",
                resource_type="connector_action",
                resource_id=action_id,
                task_id=stage,
                outcome="success" if observed.get("confirmed") else "unresolved",
                context={
                    "input_hash": row.input_hash,
                    "confirmed": bool(observed.get("confirmed")),
                    "observed": observed.get("observed", []),
                },
            )
            return {
                "id": row.id,
                "state": row.state,
                "receipt": row.receipt,
                "stage_task_id": stage,
                "observed": observed.get("observed", []),
            }
