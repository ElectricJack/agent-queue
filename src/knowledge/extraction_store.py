"""Inert extraction persistence; every ``_on`` call uses its caller's transaction.

Authorization, retained-artifact creation, provider policy and lifecycle belong to
the orchestration service. No method invokes a provider, starts a worker or writes
knowledge. Lock order is job, daily budget, reservation; claims first lock the
scope to serialize the one-in-flight limit. Never hold these locks across I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import metadata, record_scopes
from src.records.models import RecordError

LEASE_SECONDS = 30
MAX_INPUTS = 8
MAX_CALL_TOKENS = 8000
MAX_CLAIMS = 2


def utc_day(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("a timezone-aware timestamp is required")
    return value.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _nonnegative(*values):
    if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in values):
        raise RecordError("extraction.invalid_budget")


def _text(*values):
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise RecordError("extraction.invalid_identity")


class ExtractionStore:
    """Connection-owned primitives, with an injectable server clock for replay tests."""

    def __init__(self, *, clock=None):
        self.clock = clock or (lambda: datetime.now(UTC))

    @property
    def jobs(self):
        return metadata.tables["knowledge_extraction_jobs"]

    @property
    def inputs(self):
        return metadata.tables["knowledge_extraction_inputs"]

    @property
    def budgets(self):
        return metadata.tables["knowledge_feature_budgets"]

    @property
    def reservations(self):
        return metadata.tables["knowledge_budget_reservations"]

    @property
    def checkpoints(self):
        return metadata.tables["knowledge_capture_checkpoints"]

    async def get_job_on(self, job_id, *, conn, lock=False):
        query = select(self.jobs).where(self.jobs.c.job_id == job_id)
        if lock:
            query = query.with_for_update()
        row = (await conn.execute(query)).mappings().first()
        return dict(row) if row else None

    async def inputs_on(self, job_id, *, conn):
        rows = await conn.execute(
            select(self.inputs)
            .where(self.inputs.c.job_id == job_id)
            .order_by(self.inputs.c.input_ordinal)
        )
        return [dict(row) for row in rows.mappings()]

    async def enqueue_on(
        self,
        *,
        scope_key,
        source_identity,
        source_sha256,
        extractor_version,
        policy_version,
        inputs,
        conn,
    ):
        """Deduplicate one exact source/version and reject changed input receipts.

        Inputs must already identify retained scoped artifacts. Event and attempt
        IDs are soft references so ordinary event retention cannot erase evidence.
        Call ``advance_checkpoint_on`` in this transaction only after all inputs
        through that cursor have been retained; these methods never commit.
        """
        _text(scope_key, source_identity, extractor_version, policy_version)
        if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            raise RecordError("extraction.invalid_hash")
        receipts = list(inputs)
        if not 1 <= len(receipts) <= MAX_INPUTS:
            raise RecordError("extraction.input_limit")
        normalized = []
        for ordinal, receipt in enumerate(receipts):
            if set(receipt) != {
                "event_id",
                "attempt_id",
                "artifact_id",
                "source_scope",
                "actor_id",
            }:
                raise RecordError("extraction.invalid_input")
            _nonnegative(receipt["event_id"])
            _text(receipt["artifact_id"], receipt["source_scope"], receipt["actor_id"])
            if receipt["source_scope"] != scope_key:
                raise RecordError("extraction.input_scope")
            normalized.append({**receipt, "input_ordinal": ordinal})
        identity = dict(
            scope_key=scope_key,
            source_identity=source_identity,
            source_sha256=source_sha256,
            extractor_version=extractor_version,
            policy_version=policy_version,
        )
        job_id = await conn.scalar(
            pg_insert(self.jobs)
            .values(job_id=uuid4(), **identity, available_at=self.clock())
            .on_conflict_do_nothing(constraint="uq_knowledge_extraction_jobs_source")
            .returning(self.jobs.c.job_id)
        )
        if job_id is not None:
            await conn.execute(
                pg_insert(self.inputs), [{"job_id": job_id, **row} for row in normalized]
            )
        else:
            job_id = await conn.scalar(
                select(self.jobs.c.job_id)
                .where(*(self.jobs.c[key] == value for key, value in identity.items()))
                .with_for_update()
            )
            existing = await self.inputs_on(job_id, conn=conn)
            if [
                {key: value for key, value in row.items() if key != "job_id"} for row in existing
            ] != normalized:
                raise RecordError("extraction.input_conflict")
        return await self.get_job_on(job_id, conn=conn)

    async def advance_checkpoint_on(self, *, consumer_id, scope_key, last_event_id, conn):
        """Monotonic capture cursor; caller must compose retained receipts atomically."""
        _text(consumer_id, scope_key)
        _nonnegative(last_event_id)
        table = self.checkpoints
        values = dict(
            consumer_id=consumer_id,
            scope_key=scope_key,
            last_event_id=last_event_id,
            last_reconcile_at=self.clock(),
        )
        statement = pg_insert(table).values(**values)
        await conn.execute(
            statement.on_conflict_do_update(
                index_elements=[table.c.consumer_id, table.c.scope_key],
                set_=dict(
                    last_event_id=statement.excluded.last_event_id,
                    last_reconcile_at=statement.excluded.last_reconcile_at,
                ),
                where=table.c.last_event_id <= last_event_id,
            )
        )

    async def claim_due_on(self, *, scope_keys, conn, limit=MAX_CLAIMS, versions_by_scope=None):
        """Claim at most two jobs, with one live lease per explicitly selected scope.

        Expired paid operations without saved output are quarantined, retaining
        the budget. Saved output may be leased for replay without another call.
        """
        now = self.clock()
        claimed = []
        for scope in dict.fromkeys(scope_keys):
            if len(claimed) >= max(0, min(limit, MAX_CLAIMS)):
                break
            locked = await conn.scalar(
                select(record_scopes.c.scope_key)
                .where(record_scopes.c.scope_key == scope)
                .with_for_update(skip_locked=True)
            )
            if locked is None:
                continue
            active = (
                (
                    await conn.execute(
                        select(self.jobs)
                        .where(self.jobs.c.scope_key == scope, self.jobs.c.state == "leased")
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            if any(row["lease_until"] > now for row in active):
                continue
            for row in active:
                reservation = await self.reservation_on(row["job_id"], conn=conn)
                if (
                    reservation
                    and reservation["provider_operation_id"] is not None
                    and row["result_artifact_id"] is None
                ):
                    await self._unknown_on(row, reservation, conn=conn)
                    await self.provider_result_on(
                        scope_key=scope, feature=reservation["feature"], failed=True, conn=conn
                    )
            row = (
                (
                    await conn.execute(
                        select(self.jobs)
                        .where(
                            self.jobs.c.scope_key == scope,
                            self.jobs.c.state.in_(("pending", "retry", "leased")),
                            self.jobs.c.available_at <= now,
                            self.jobs.c.extractor_version.in_(versions_by_scope[scope])
                            if versions_by_scope is not None else True,
                        )
                        .order_by(self.jobs.c.available_at, self.jobs.c.job_id)
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                continue
            values = dict(
                state="leased",
                lease_token=uuid4(),
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
                attempts=row["attempts"] + 1,
            )
            await conn.execute(
                update(self.jobs).where(self.jobs.c.job_id == row["job_id"]).values(**values)
            )
            claimed.append({**row, **values})
        return claimed

    async def _leased_on(self, job_id, lease_token, *, conn):
        row = await self.get_job_on(job_id, conn=conn, lock=True)
        if (
            not row
            or row["state"] != "leased"
            or row["lease_token"] != lease_token
            or row["lease_until"] <= self.clock()
        ):
            raise RecordError("extraction.stale_lease")
        return row

    async def renew_on(self, job_id, lease_token, *, conn):
        await self._leased_on(job_id, lease_token, conn=conn)
        await conn.execute(
            update(self.jobs)
            .where(self.jobs.c.job_id == job_id)
            .values(lease_until=self.clock() + timedelta(seconds=LEASE_SECONDS))
        )

    async def reservation_on(self, job_id, *, conn):
        row = (
            (
                await conn.execute(
                    select(self.reservations).where(self.reservations.c.job_id == job_id)
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def _budget_on(self, reservation, *, conn):
        row = (
            (
                await conn.execute(
                    select(self.budgets)
                    .where(
                        *(
                            self.budgets.c[key] == reservation[key]
                            for key in ("scope_key", "feature", "period_start")
                        )
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        return dict(row)

    async def _check_allowance_on(self, budget, money, tokens, *, conn):
        # An overage circuit survives daily counter rollover. Only an explicit
        # future operator recovery path may clear it; changing limits cannot.
        opened = await conn.scalar(
            select(self.budgets.c.period_start)
            .where(
                self.budgets.c.scope_key == budget["scope_key"],
                self.budgets.c.feature == budget["feature"],
                self.budgets.c.circuit_open.is_(True),
            )
            .limit(1)
        )
        if opened is not None:
            raise RecordError("extraction.budget_circuit_open")
        if budget["limit_microusd"] == 0:
            raise RecordError("extraction.budget_disabled")
        if (
            budget["spent_microusd"] + budget["reserved_microusd"] + money
            > budget["limit_microusd"]
            or budget["spent_tokens"] + budget["reserved_tokens"] + tokens > budget["token_limit"]
        ):
            raise RecordError("extraction.budget_exceeded")

    async def _roll_reservation_on(self, reservation, *, conn):
        """Move only an unstarted reservation to today's explicitly configured allowance."""
        old_budget = await self._budget_on(reservation, conn=conn)
        key = {key: reservation[key] for key in ("scope_key", "feature")}
        key["period_start"] = utc_day(self.clock())
        await conn.execute(pg_insert(self.budgets).values(**key).on_conflict_do_nothing())
        new_budget = await self._budget_on(key, conn=conn)
        money, tokens = reservation["estimated_microusd"], reservation["estimated_tokens"]
        await self._check_allowance_on(new_budget, money, tokens, conn=conn)
        await conn.execute(
            update(self.budgets)
            .where(
                *(
                    self.budgets.c[key] == reservation[key]
                    for key in ("scope_key", "feature", "period_start")
                )
            )
            .values(
                reserved_microusd=old_budget["reserved_microusd"] - money,
                reserved_tokens=old_budget["reserved_tokens"] - tokens,
            )
        )
        await conn.execute(
            update(self.budgets)
            .where(*(self.budgets.c[name] == value for name, value in key.items()))
            .values(
                reserved_microusd=new_budget["reserved_microusd"] + money,
                reserved_tokens=new_budget["reserved_tokens"] + tokens,
            )
        )
        await conn.execute(
            update(self.reservations)
            .where(self.reservations.c.reservation_id == reservation["reservation_id"])
            .values(period_start=key["period_start"], updated_at=self.clock())
        )
        return await self.reservation_on(reservation["job_id"], conn=conn)

    async def configure_budget_on(
        self,
        *,
        scope_key,
        feature,
        conn,
        limit_microusd=0,
        token_limit=0,
    ):
        """Set today's explicit limits without resetting counters or an open circuit."""
        _text(scope_key, feature)
        _nonnegative(limit_microusd, token_limit)
        values = dict(
            scope_key=scope_key,
            feature=feature,
            period_start=utc_day(self.clock()),
            limit_microusd=limit_microusd,
            token_limit=token_limit,
        )
        await conn.execute(
            pg_insert(self.budgets)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[
                    self.budgets.c.scope_key,
                    self.budgets.c.feature,
                    self.budgets.c.period_start,
                ],
                set_=dict(limit_microusd=limit_microusd, token_limit=token_limit),
            )
        )

    async def provider_result_on(self, *, scope_key, feature, failed, conn):
        """Persist consecutive failures across restarts and UTC day rollover."""
        # One call in flight per scope serializes this across both features.
        key = dict(scope_key=scope_key, feature=feature, period_start=utc_day(self.clock()))
        await conn.execute(pg_insert(self.budgets).values(**key).on_conflict_do_nothing())
        history = (await conn.execute(select(self.budgets).where(
            self.budgets.c.scope_key == scope_key, self.budgets.c.feature == feature,
        ).order_by(self.budgets.c.period_start.desc()).with_for_update())).mappings().all()
        previous = max((row["consecutive_failures"] for row in history), default=0)
        count = previous + 1 if failed else 0
        await conn.execute(update(self.budgets).where(
            self.budgets.c.scope_key == scope_key, self.budgets.c.feature == feature,
        ).values(consecutive_failures=count))
        if count >= 5:
            await conn.execute(update(self.budgets).where(
                *(self.budgets.c[name] == value for name, value in key.items())
            ).values(circuit_open=True))

    async def reserve_on(
        self,
        job_id,
        lease_token,
        *,
        feature,
        estimated_microusd,
        estimated_tokens,
        conn,
    ):
        """Lock both counters before admission. A zero money limit disables calls."""
        _text(feature)
        _nonnegative(estimated_microusd, estimated_tokens)
        if estimated_tokens > MAX_CALL_TOKENS:
            raise RecordError("extraction.call_token_limit")
        job = await self._leased_on(job_id, lease_token, conn=conn)
        existing = await self.reservation_on(job_id, conn=conn)
        if existing:
            if (
                existing["feature"],
                existing["estimated_microusd"],
                existing["estimated_tokens"],
            ) != (feature, estimated_microusd, estimated_tokens):
                raise RecordError("extraction.reservation_conflict")
            if existing["state"] in ("unknown", "released"):
                raise RecordError("extraction.reservation_unavailable")
            if (
                existing["state"] == "reserved"
                and existing["provider_operation_id"] is None
                and existing["period_start"] != utc_day(self.clock())
            ):
                return await self._roll_reservation_on(existing, conn=conn)
            return existing
        key = dict(scope_key=job["scope_key"], feature=feature, period_start=utc_day(self.clock()))
        await conn.execute(pg_insert(self.budgets).values(**key).on_conflict_do_nothing())
        budget = await self._budget_on(key, conn=conn)
        await self._check_allowance_on(budget, estimated_microusd, estimated_tokens, conn=conn)
        await conn.execute(
            update(self.budgets)
            .where(*(self.budgets.c[name] == value for name, value in key.items()))
            .values(
                reserved_microusd=budget["reserved_microusd"] + estimated_microusd,
                reserved_tokens=budget["reserved_tokens"] + estimated_tokens,
            )
        )
        reservation_id = uuid4()
        await conn.execute(
            pg_insert(self.reservations).values(
                reservation_id=reservation_id,
                job_id=job_id,
                **key,
                estimated_microusd=estimated_microusd,
                estimated_tokens=estimated_tokens,
                created_at=self.clock(),
                updated_at=self.clock(),
            )
        )
        await conn.execute(
            update(self.jobs)
            .where(self.jobs.c.job_id == job_id)
            .values(budget_reservation_id=reservation_id)
        )
        return await self.reservation_on(job_id, conn=conn)

    async def begin_operation_on(self, job_id, lease_token, *, provider_operation_id, conn):
        """Return True only once, before outbound I/O. False requires recovery/replay."""
        _text(provider_operation_id)
        await self._leased_on(job_id, lease_token, conn=conn)
        reservation = await self.reservation_on(job_id, conn=conn)
        if not reservation or reservation["state"] != "reserved":
            raise RecordError("extraction.reservation_unavailable")
        budget = await self._budget_on(reservation, conn=conn)
        if reservation["provider_operation_id"] is not None:
            if reservation["provider_operation_id"] != provider_operation_id:
                raise RecordError("extraction.operation_conflict")
            return False
        if reservation["period_start"] != utc_day(self.clock()):
            raise RecordError("extraction.reservation_expired")
        await self._check_allowance_on(budget, 0, 0, conn=conn)
        await conn.execute(
            update(self.reservations)
            .where(self.reservations.c.job_id == job_id)
            .values(provider_operation_id=provider_operation_id, updated_at=self.clock())
        )
        return True

    async def save_output_on(self, job_id, lease_token, *, artifact_id, conn):
        """Pin retained output before settlement/proposals; never overwrite a saved result."""
        _text(artifact_id)
        job = await self._leased_on(job_id, lease_token, conn=conn)
        reservation = await self.reservation_on(job_id, conn=conn)
        if not reservation or reservation["provider_operation_id"] is None:
            raise RecordError("extraction.operation_unavailable")
        if job["result_artifact_id"] not in (None, artifact_id):
            raise RecordError("extraction.output_conflict")
        await conn.execute(
            update(self.jobs)
            .where(self.jobs.c.job_id == job_id)
            .values(result_artifact_id=artifact_id)
        )

    async def settle_on(self, job_id, lease_token, *, actual_microusd, actual_tokens, conn):
        """Record actual usage once, including overage; never cap reported consumption."""
        _nonnegative(actual_microusd, actual_tokens)
        # Completed receipts can be replayed after the lease has been cleared;
        # this read never authorizes another effect from an expired lease.
        await self.get_job_on(job_id, conn=conn, lock=True)
        reservation = await self.reservation_on(job_id, conn=conn)
        if not reservation or reservation["state"] not in ("reserved", "settled"):
            raise RecordError("extraction.reservation_unavailable")
        if reservation["provider_operation_id"] is None:
            raise RecordError("extraction.operation_unavailable")
        if reservation["state"] == "settled":
            if (reservation["actual_microusd"], reservation["actual_tokens"]) != (
                actual_microusd,
                actual_tokens,
            ):
                raise RecordError("extraction.settlement_conflict")
            return reservation
        await self._leased_on(job_id, lease_token, conn=conn)
        budget = await self._budget_on(reservation, conn=conn)
        spent_money = budget["spent_microusd"] + actual_microusd
        spent_tokens = budget["spent_tokens"] + actual_tokens
        reserved_money = budget["reserved_microusd"] - reservation["estimated_microusd"]
        reserved_tokens = budget["reserved_tokens"] - reservation["estimated_tokens"]
        overage = (
            actual_microusd > reservation["estimated_microusd"]
            or actual_tokens > reservation["estimated_tokens"]
            or spent_money + reserved_money > budget["limit_microusd"]
            or spent_tokens + reserved_tokens > budget["token_limit"]
        )
        await conn.execute(
            update(self.budgets)
            .where(
                *(
                    self.budgets.c[key] == reservation[key]
                    for key in ("scope_key", "feature", "period_start")
                )
            )
            .values(
                reserved_microusd=reserved_money,
                reserved_tokens=reserved_tokens,
                spent_microusd=spent_money,
                spent_tokens=spent_tokens,
                circuit_open=budget["circuit_open"] or overage,
            )
        )
        await conn.execute(
            update(self.reservations)
            .where(self.reservations.c.job_id == job_id)
            .values(
                state="settled",
                actual_microusd=actual_microusd,
                actual_tokens=actual_tokens,
                updated_at=self.clock(),
            )
        )
        return await self.reservation_on(job_id, conn=conn)

    async def _unknown_on(self, job, reservation, *, conn):
        if reservation["state"] == "reserved":
            await self._budget_on(reservation, conn=conn)
            await conn.execute(
                update(self.reservations)
                .where(self.reservations.c.job_id == job["job_id"])
                .values(state="unknown", updated_at=self.clock())
            )
        await conn.execute(
            update(self.jobs)
            .where(self.jobs.c.job_id == job["job_id"])
            .values(
                state="quarantined",
                error_code="provider_outcome_unknown",
                lease_token=None,
                lease_until=None,
            )
        )

    async def unknown_on(self, job_id, lease_token, *, conn):
        job = await self._leased_on(job_id, lease_token, conn=conn)
        reservation = await self.reservation_on(job_id, conn=conn)
        if (
            not reservation
            or reservation["state"] != "reserved"
            or reservation["provider_operation_id"] is None
        ):
            raise RecordError("extraction.operation_unavailable")
        await self._unknown_on(job, reservation, conn=conn)

    async def finish_on(self, job_id, lease_token, *, state, conn, error_code=None):
        """Compose success with proposals; retry is allowed only before a paid call."""
        if state not in ("succeeded", "retry", "quarantined", "cancelled"):
            raise RecordError("extraction.invalid_state")
        job = await self._leased_on(job_id, lease_token, conn=conn)
        reservation = await self.reservation_on(job_id, conn=conn)
        if state == "succeeded" and (
            job["result_artifact_id"] is None
            or not reservation
            or reservation["state"] != "settled"
        ):
            raise RecordError("extraction.result_unavailable")
        if reservation and reservation["state"] == "reserved":
            if reservation["provider_operation_id"] is not None:
                await self._unknown_on(job, reservation, conn=conn)
                return "quarantined"
            if state in ("quarantined", "cancelled"):
                budget = await self._budget_on(reservation, conn=conn)
                await conn.execute(
                    update(self.budgets)
                    .where(
                        *(
                            self.budgets.c[key] == reservation[key]
                            for key in ("scope_key", "feature", "period_start")
                        )
                    )
                    .values(
                        reserved_microusd=budget["reserved_microusd"]
                        - reservation["estimated_microusd"],
                        reserved_tokens=budget["reserved_tokens"] - reservation["estimated_tokens"],
                    )
                )
                await conn.execute(
                    update(self.reservations)
                    .where(self.reservations.c.job_id == job_id)
                    .values(state="released", updated_at=self.clock())
                )
        if (
            state == "retry"
            and reservation
            and reservation["state"] == "settled"
            and (job["result_artifact_id"] is None)
        ):
            raise RecordError("extraction.result_unavailable")
        delay = min(300, 2 ** min(job["attempts"] - 1, 9)) if state == "retry" else 0
        await conn.execute(
            update(self.jobs)
            .where(self.jobs.c.job_id == job_id)
            .values(
                state=state,
                error_code=error_code,
                lease_token=None,
                lease_until=None,
                available_at=self.clock() + timedelta(seconds=delay),
            )
        )
        return state
