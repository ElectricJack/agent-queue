"""Resume durable candidate publication before asking for exact CI evidence."""

from sqlalchemy import select

from src.database.tables import integration_candidate_publications


class CandidateCIService:
    def __init__(self, db, *, candidate_service_factory, attestation):
        self.db = db
        self.candidate_service_factory = candidate_service_factory
        self.attestation = attestation

    async def handle(self, row, now):
        async with self.db._engine.connect() as conn:
            publication = (
                (
                    await conn.execute(
                        select(integration_candidate_publications).where(
                            integration_candidate_publications.c.batch_id == row["batch_id"],
                            integration_candidate_publications.c.revision == row["revision"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if (
            publication is None
            or publication["state"] != "pr_published"
            or publication["head_sha"] != row["candidate_sha"]
        ):
            # This poll is only a hint. Build rechecks the current batch, lease,
            # writer and publication reservation before any external mutation.
            service = await self.candidate_service_factory(row)
            if service is None:
                return {"outcome": "wait"}
            result = await service.build(row["batch_id"])
            if (
                result.outcome not in {"built", "already_built"}
                or result.revision != row["revision"]
                or result.head_sha != row["candidate_sha"]
            ):
                return {"outcome": "wait"}
        # Attestation still requires its own exact pr_published record. A build
        # result alone never substitutes for durable publication or green CI.
        return await self.attestation.handle_candidate_ci(row, now)
