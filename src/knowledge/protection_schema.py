"""Versioned K05 database guards and audited, one-way payload erasure."""

from sqlalchemy import inspect

PROTECTION_TABLE_NAMES = (
    "knowledge_proposals",
    "knowledge_authority_grants",
    "knowledge_global_shares",
    "knowledge_redactions",
    "knowledge_redaction_targets",
)

GUARDS = (
    r"""
CREATE OR REPLACE FUNCTION knowledge_erasure_guard_v1() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rid uuid;
BEGIN
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'knowledge_revision_payloads' THEN
        IF OLD.snapshot IS NOT NULL AND NEW.snapshot IS NULL AND
           NEW.revision_id = OLD.revision_id AND NEW.redacted_at IS NOT NULL AND
           current_setting('aq.knowledge_redaction', true) = NEW.redaction_id::text AND EXISTS (
               SELECT 1 FROM knowledge_redaction_targets t
               WHERE t.revision_id = OLD.revision_id AND t.redaction_id = NEW.redaction_id
           ) THEN RETURN NEW; END IF;
    ELSIF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'record_link_versions' THEN
        rid := nullif(current_setting('aq.knowledge_redaction', true), '')::uuid;
        IF rid IS NOT NULL AND NEW.metadata = '{}'::jsonb AND
           (to_jsonb(NEW) - 'metadata') = (to_jsonb(OLD) - 'metadata') AND EXISTS (
               SELECT 1 FROM knowledge_redactions WHERE redaction_id = rid
           ) AND (EXISTS (
               SELECT 1 FROM knowledge_redaction_targets t
               WHERE t.revision_id = OLD.source_revision_id AND t.redaction_id = rid
           ) OR EXISTS (
               SELECT 1 FROM knowledge_redaction_targets t JOIN knowledge_revisions r
                 ON r.revision_id = t.revision_id
               WHERE t.redaction_id = rid AND r.record_id = OLD.target_record_id
           )) THEN RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION 'immutable knowledge evidence' USING ERRCODE = '23514';
END $$
""",
    r"""
CREATE OR REPLACE FUNCTION knowledge_erase_v1(rid uuid) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM knowledge_redactions WHERE redaction_id = rid) THEN
        RAISE EXCEPTION 'redaction ledger required' USING ERRCODE = '23514';
    END IF;
    PERFORM set_config('aq.knowledge_redaction', rid::text, true);
    UPDATE knowledge_revision_payloads p SET snapshot = NULL, redacted_at = now(), redaction_id = rid
    FROM knowledge_redaction_targets t
    WHERE t.redaction_id = rid AND t.revision_id = p.revision_id AND p.snapshot IS NOT NULL;
    UPDATE record_link_versions v SET metadata = '{}'::jsonb
    WHERE EXISTS (SELECT 1 FROM knowledge_redaction_targets t WHERE t.redaction_id = rid
                  AND t.revision_id = v.source_revision_id)
       OR EXISTS (SELECT 1 FROM knowledge_redaction_targets t JOIN knowledge_revisions r
                    ON r.revision_id = t.revision_id
                  WHERE t.redaction_id = rid AND r.record_id = v.target_record_id);
    PERFORM set_config('aq.knowledge_redaction', '', true);
END $$
""",
    r"""
CREATE OR REPLACE FUNCTION knowledge_protection_scope_v1() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE scope text;
BEGIN
    IF NEW.record_id IS NOT NULL THEN
        SELECT scope_key INTO scope FROM records WHERE record_id = NEW.record_id;
        IF TG_TABLE_NAME = 'knowledge_global_shares' THEN
            IF scope IS DISTINCT FROM 'global' THEN
                RAISE EXCEPTION 'only global knowledge may be shared' USING ERRCODE = '23514';
            END IF;
        ELSIF scope IS DISTINCT FROM NEW.scope_key THEN
            RAISE EXCEPTION 'protection scope mismatch' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END $$
""",
)


def install_protection_guards_v1(bind):
    from src.records.schema import _trigger

    for statement in GUARDS:
        bind.exec_driver_sql(statement)
    # An audited scrub updates historical versions without allocating a new
    # version; their owning revision may no longer be the current head.
    from src.records.schema import GUARDS_V1

    link_guard = next(s for s in GUARDS_V1 if "FUNCTION record_link_check_v1" in s)
    link_guard = link_guard.replace("record_link_check_v1", "record_link_check_v2")
    link_guard = link_guard.replace(
        "BEGIN\n",
        """BEGIN
    IF TG_OP = 'UPDATE' AND NEW.metadata = '{}'::jsonb AND
       (to_jsonb(NEW) - 'metadata') = (to_jsonb(OLD) - 'metadata') AND EXISTS (
           SELECT 1 FROM knowledge_redaction_targets t JOIN knowledge_revisions r
             ON r.revision_id = t.revision_id
           WHERE r.record_id = NEW.target_record_id OR t.revision_id = NEW.source_revision_id
       ) THEN RETURN NULL; END IF;
""",
        1,
    )
    bind.exec_driver_sql(link_guard)
    _trigger(
        bind,
        "record_link_versions",
        "tr_record_link_versions_check_v1",
        "INSERT OR UPDATE",
        "record_link_check_v2",
        deferred=True,
    )
    bind.exec_driver_sql("REVOKE ALL ON FUNCTION knowledge_erase_v1(uuid) FROM PUBLIC")
    for table in ("knowledge_revision_payloads", "record_link_versions"):
        _trigger(
            bind,
            table,
            f"tr_{table}_immutable_v1",
            "UPDATE OR DELETE",
            "knowledge_erasure_guard_v1",
        )
    for table in ("knowledge_redaction_targets",):
        _trigger(bind, table, f"tr_{table}_immutable_v1", "UPDATE OR DELETE", "record_immutable_v1")
    for table in ("knowledge_proposals", "knowledge_authority_grants", "knowledge_global_shares"):
        _trigger(
            bind, table, f"tr_{table}_scope_v1", "INSERT OR UPDATE", "knowledge_protection_scope_v1"
        )


def after_create(metadata, bind, **kwargs):
    if all(inspect(bind).has_table(name) for name in PROTECTION_TABLE_NAMES):
        install_protection_guards_v1(bind)
