"""Versioned PostgreSQL integrity helpers for the K01 additive schema.

Keep v1 semantics stable: future changes need a new helper/revision. No provider,
filesystem, task scheduler, or operator configuration is consulted here. K05 must
supply an audited redaction function before any immutable payload can be changed.
Snapshot source descriptors use ``source_id``/``kind``; link descriptors use the
column names ``link_id, version, link_type, target_record_id, target_revision_id,
metadata``. Nullable snapshot fields are present with JSON null.
"""

from uuid import uuid4

from sqlalchemy import event, text

RECORD_TABLE_NAMES = (
    "record_installation",
    "record_scopes",
    "records",
    "knowledge_records",
    "knowledge_revisions",
    "knowledge_revision_payloads",
    "knowledge_search",
    "record_link_heads",
    "record_link_versions",
    "task_record_link_state",
    "record_source_artifacts",
    "record_requests",
    "record_outbox",
    "record_consumer_receipts",
    "record_export_state",
    "record_backfill_state",
)

# Each string is one statement (asyncpg rejects multi-command prepared SQL).
VALIDATORS_V1 = (
    r"""
CREATE OR REPLACE FUNCTION record_metadata_valid_v1(doc jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE depth integer;
BEGIN
    IF jsonb_typeof(doc) IS DISTINCT FROM 'object' OR octet_length(doc::text) > 16384
    THEN RETURN false; END IF;
    IF EXISTS (SELECT 1 FROM jsonb_object_keys(doc) AS k
               WHERE k !~ '^[A-Za-z][A-Za-z0-9_-]*\.[A-Za-z0-9_.-]+$')
    THEN RETURN false; END IF;
    WITH RECURSIVE nodes(value, level) AS (
        SELECT doc, 1
        UNION ALL
        SELECT child.value, nodes.level + 1 FROM nodes
        CROSS JOIN LATERAL (
            SELECT value FROM jsonb_each(CASE WHEN jsonb_typeof(nodes.value) = 'object'
                                            THEN nodes.value ELSE '{}'::jsonb END)
            UNION ALL
            SELECT value FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(nodes.value) = 'array'
                     THEN nodes.value ELSE '[]'::jsonb END)
        ) child WHERE nodes.level <= 8
    ) SELECT max(level) INTO depth FROM nodes;
    RETURN depth <= 8;
END $$
""",
    r"""
CREATE OR REPLACE FUNCTION record_utc_valid_v1(value text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF value IS NULL OR value !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$'
    THEN RETURN false; END IF;
    RETURN to_char(value::timestamptz AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') = value;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$
""",
    r"""
CREATE OR REPLACE FUNCTION knowledge_snapshot_valid_v1(doc jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, public AS $$
DECLARE item jsonb; key text; ids text[]; ident text;
BEGIN
    IF jsonb_typeof(doc) IS DISTINCT FROM 'object' OR NOT doc ?& ARRAY[
        'title','body','category','tags','lifecycle','verification','summary',
        'summary_of_revision','valid_from','valid_until','recheck_at','last_verified_at',
        'last_verified_by','retirement_reason','successor_record_id','sources','metadata',
        'outgoing_links'] THEN RETURN false; END IF;
    IF jsonb_typeof(doc->'title') IS DISTINCT FROM 'string'
       OR length(doc->>'title') NOT BETWEEN 1 AND 240
       OR jsonb_typeof(doc->'body') IS DISTINCT FROM 'string'
       OR octet_length(doc->>'body') > 262144
       OR COALESCE(doc->>'category','') NOT IN
          ('fact','decision','policy','procedure','incident','reference','note')
       OR COALESCE(doc->>'lifecycle','') NOT IN ('active','retired')
       OR COALESCE(doc->>'verification','') NOT IN ('unverified','verified','disputed')
       OR jsonb_typeof(doc->'tags') IS DISTINCT FROM 'array'
       OR jsonb_typeof(doc->'sources') IS DISTINCT FROM 'array'
       OR jsonb_typeof(doc->'outgoing_links') IS DISTINCT FROM 'array'
       OR NOT record_metadata_valid_v1(doc->'metadata')
    THEN RETURN false; END IF;
    IF jsonb_array_length(doc->'tags') > 32 OR jsonb_array_length(doc->'sources') > 100
       OR jsonb_array_length(doc->'outgoing_links') > 1000 THEN RETURN false; END IF;
    FOREACH key IN ARRAY ARRAY['summary','last_verified_by','retirement_reason'] LOOP
        IF jsonb_typeof(doc->key) NOT IN ('null','string') THEN RETURN false; END IF;
    END LOOP;
    IF octet_length(doc->>'summary') > 4096 THEN RETURN false; END IF;
    FOREACH key IN ARRAY ARRAY['summary_of_revision','successor_record_id'] LOOP
        IF doc->key <> 'null'::jsonb THEN
            IF jsonb_typeof(doc->key) <> 'string' THEN RETURN false; END IF;
            PERFORM (doc->>key)::uuid;
        END IF;
    END LOOP;
    IF doc->>'summary_of_revision' IS NOT NULL AND doc->>'summary' IS NULL
    THEN RETURN false; END IF;
    FOREACH key IN ARRAY ARRAY[
        'valid_from','valid_until','recheck_at','last_verified_at'] LOOP
        IF doc->key <> 'null'::jsonb AND
           (jsonb_typeof(doc->key) <> 'string' OR NOT record_utc_valid_v1(doc->>key))
        THEN RETURN false; END IF;
    END LOOP;
    IF (doc->>'valid_from')::timestamptz > (doc->>'valid_until')::timestamptz
    THEN RETURN false; END IF;
    IF doc->>'lifecycle' = 'retired' AND
       COALESCE(length(btrim(doc->>'retirement_reason')),0) = 0 THEN RETURN false; END IF;
    IF doc->>'verification' = 'unverified' THEN
        IF doc->>'last_verified_at' IS NOT NULL OR doc->>'last_verified_by' IS NOT NULL
        THEN RETURN false; END IF;
    ELSE
        IF doc->>'last_verified_at' IS NULL OR
           COALESCE(length(btrim(doc->>'last_verified_by')),0) = 0 OR
           jsonb_array_length(doc->'sources') = 0 THEN RETURN false; END IF;
    END IF;
    ids := ARRAY[]::text[];
    FOR item IN SELECT value FROM jsonb_array_elements(doc->'tags') LOOP
        ident := item #>> '{}';
        IF jsonb_typeof(item) <> 'string' OR length(ident) NOT BETWEEN 1 AND 64
           OR ident = ANY(ids) THEN RETURN false; END IF;
        ids := array_append(ids, ident);
    END LOOP;
    ids := ARRAY[]::text[];
    FOR item IN SELECT value FROM jsonb_array_elements(doc->'sources') LOOP
        ident := item->>'source_id';
        IF jsonb_typeof(item) <> 'object' OR
           jsonb_typeof(item->'source_id') IS DISTINCT FROM 'string' OR
           COALESCE(length(ident),0) = 0 OR ident = ANY(ids) OR
           COALESCE(item->>'kind','') NOT IN ('review','task','git','artifact','url','legacy')
        THEN RETURN false; END IF;
        ids := array_append(ids, ident);
        FOREACH key IN ARRAY ARRAY[
            'source_id','kind','review_id','task_id','attempt_id','session_id','repository',
            'commit','path','blob_sha256','artifact_id','sha256','url','observed_at',
            'snapshot_id','store','key'] LOOP
            IF item ? key AND jsonb_typeof(item->key) IS DISTINCT FROM 'string'
            THEN RETURN false; END IF;
        END LOOP;
        IF item ? 'artifact_id' THEN PERFORM (item->>'artifact_id')::uuid; END IF;
        CASE item->>'kind'
        WHEN 'review' THEN
            IF COALESCE(item->>'review_id','') = '' OR
               COALESCE(item->>'revision','') !~ '^[1-9][0-9]*$' OR
               COALESCE(item->>'sha256','') !~ '^[0-9a-f]{64}$' THEN RETURN false; END IF;
        WHEN 'task' THEN
            IF COALESCE(item->>'task_id','') = '' THEN RETURN false; END IF;
        WHEN 'git' THEN
            IF COALESCE(item->>'repository','') = '' OR COALESCE(item->>'path','') = '' OR
               COALESCE(item->>'commit','') !~ '^([0-9a-f]{40}|[0-9a-f]{64})$'
            THEN RETURN false; END IF;
        WHEN 'artifact' THEN
            IF item->>'artifact_id' IS NULL OR
               COALESCE(item->>'sha256','') !~ '^[0-9a-f]{64}$' THEN RETURN false; END IF;
            PERFORM (item->>'artifact_id')::uuid;
        WHEN 'url' THEN
            IF COALESCE(item->>'url','') !~ '^https?://' OR
               NOT record_utc_valid_v1(item->>'observed_at') OR
               jsonb_typeof(item->'retained') IS DISTINCT FROM 'boolean' OR
               (item->>'retained' = 'true' AND item->>'artifact_id' IS NULL)
            THEN RETURN false; END IF;
        WHEN 'legacy' THEN
            IF COALESCE(item->>'snapshot_id','') = '' OR COALESCE(item->>'store','') = '' OR
               COALESCE(item->>'key','') = '' OR
               COALESCE(item->>'sha256','') !~ '^[0-9a-f]{64}$' THEN RETURN false; END IF;
        END CASE;
    END LOOP;
    ids := ARRAY[]::text[];
    FOR item IN SELECT value FROM jsonb_array_elements(doc->'outgoing_links') LOOP
        ident := item->>'link_id';
        IF jsonb_typeof(item) <> 'object' OR NOT item ?& ARRAY[
            'link_id','version','link_type','target_record_id','target_revision_id','metadata']
            OR ident IS NULL OR ident = ANY(ids) OR
            COALESCE(item->>'version','') !~ '^[1-9][0-9]*$' OR
            jsonb_typeof(item->'version') IS DISTINCT FROM 'number' OR
            COALESCE(item->>'link_type','') NOT IN
            ('references','motivated_by','produces','supports','contradicts','supersedes') OR
            item->>'target_record_id' IS NULL OR NOT record_metadata_valid_v1(item->'metadata')
        THEN RETURN false; END IF;
        PERFORM ident::uuid, (item->>'version')::bigint, (item->>'target_record_id')::uuid;
        IF item->>'target_revision_id' IS NOT NULL THEN
            PERFORM (item->>'target_revision_id')::uuid;
        END IF;
        ids := array_append(ids, ident);
    END LOOP;
    RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$
""",
)

GUARDS_V1 = (
    """
CREATE OR REPLACE FUNCTION record_immutable_v1() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME USING ERRCODE = '23514';
END $$
""",
    """
CREATE OR REPLACE FUNCTION record_identity_guard_v1() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'record identity is permanent' USING ERRCODE = '23514';
    END IF;
    IF (NEW.record_id, NEW.kind, NEW.scope_key, NEW.task_id, NEW.knowledge_alias,
        NEW.created_at, NEW.created_by) IS DISTINCT FROM
       (OLD.record_id, OLD.kind, OLD.scope_key, OLD.task_id, OLD.knowledge_alias,
        OLD.created_at, OLD.created_by) THEN
        RAISE EXCEPTION 'record identity is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$
""",
    """
CREATE OR REPLACE FUNCTION knowledge_revision_insert_v1() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prior knowledge_revisions;
BEGIN
    PERFORM 1 FROM records WHERE record_id = NEW.record_id FOR UPDATE;
    SELECT * INTO prior FROM knowledge_revisions WHERE record_id = NEW.record_id
        ORDER BY sequence DESC LIMIT 1;
    IF NEW.sequence <> COALESCE(prior.sequence, 0) + 1 OR
       NEW.parent_revision_id IS DISTINCT FROM prior.revision_id THEN
        RAISE EXCEPTION 'revision must append to previous sequence' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$
""",
    """
CREATE OR REPLACE FUNCTION knowledge_head_check_v1() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rid uuid; head knowledge_records; payload jsonb; expected_links jsonb;
BEGIN
    rid := CASE WHEN TG_OP = 'DELETE' THEN OLD.record_id ELSE NEW.record_id END;
    IF NOT EXISTS (SELECT 1 FROM records WHERE record_id = rid AND kind = 'knowledge')
    THEN RETURN NULL; END IF;
    SELECT * INTO head FROM knowledge_records WHERE record_id = rid;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'knowledge domain missing' USING ERRCODE = '23514';
    END IF;
    IF head.current_sequence IS DISTINCT FROM
       (SELECT max(sequence) FROM knowledge_revisions WHERE record_id = rid) THEN
        RAISE EXCEPTION 'knowledge head must be latest sequence' USING ERRCODE = '23514';
    END IF;
    SELECT snapshot INTO payload FROM knowledge_revision_payloads
        WHERE revision_id = head.current_revision_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'knowledge payload missing' USING ERRCODE = '23514';
    END IF;
    IF payload IS NULL THEN
        IF EXISTS (SELECT 1 FROM knowledge_search WHERE record_id = rid) THEN
            RAISE EXCEPTION 'redacted head cannot be indexed' USING ERRCODE = '23514';
        END IF;
        RETURN NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM knowledge_search WHERE record_id = rid) THEN
        RAISE EXCEPTION 'knowledge search projection missing' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM knowledge_search s JOIN records r USING (record_id)
        WHERE s.record_id = rid AND
          (s.revision_id <> head.current_revision_id OR s.scope_key <> r.scope_key OR
           (s.title,s.summary,s.category,s.lifecycle,s.verification,s.valid_until,s.recheck_at)
           IS DISTINCT FROM
           (payload->>'title',payload->>'summary',payload->>'category',payload->>'lifecycle',
            payload->>'verification',(payload->>'valid_until')::timestamptz,
            (payload->>'recheck_at')::timestamptz) OR
           s.search_vector <> to_tsvector('simple', payload->>'title' || ' ' ||
               COALESCE(payload->>'summary','') || ' ' || (payload->>'body')))
    ) THEN RAISE EXCEPTION 'search disagrees with current head' USING ERRCODE = '23514'; END IF;
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'link_id',h.link_id,'version',v.version,'link_type',v.link_type,
        'target_record_id',v.target_record_id,'target_revision_id',v.target_revision_id,
        'metadata',v.metadata) ORDER BY h.link_id), '[]'::jsonb)
    INTO expected_links FROM record_link_heads h JOIN record_link_versions v
      ON (v.link_id,v.version) = (h.link_id,h.current_version)
    WHERE h.source_record_id = rid AND NOT v.removed;
    IF payload->'outgoing_links' IS DISTINCT FROM expected_links THEN
        RAISE EXCEPTION 'head outgoing links disagree with link heads' USING ERRCODE = '23514';
    END IF;
    IF payload->>'successor_record_id' IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM records successor JOIN records source ON source.record_id = rid
        JOIN record_link_heads h ON h.source_record_id = successor.record_id
        JOIN record_link_versions v ON (v.link_id,v.version) = (h.link_id,h.current_version)
        WHERE successor.record_id = (payload->>'successor_record_id')::uuid
        AND successor.kind = 'knowledge' AND successor.scope_key = source.scope_key
        AND v.target_record_id = rid AND v.link_type = 'supersedes' AND NOT v.removed
    ) THEN RAISE EXCEPTION 'successor requires same-scope supersedes link'
        USING ERRCODE = '23514'; END IF;
    RETURN NULL;
END $$
""",
    """
CREATE OR REPLACE FUNCTION knowledge_revision_check_v1() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE payload jsonb; item jsonb; owner uuid; seq bigint; summary_id uuid;
BEGIN
    SELECT record_id, sequence INTO owner, seq FROM knowledge_revisions
        WHERE revision_id = NEW.revision_id;
    SELECT snapshot INTO payload FROM knowledge_revision_payloads
        WHERE revision_id = NEW.revision_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'revision payload missing'
        USING ERRCODE = '23514'; END IF;
    IF payload IS NULL THEN RETURN NULL; END IF;
    summary_id := (payload->>'summary_of_revision')::uuid;
    IF summary_id IS NOT NULL AND (summary_id = NEW.revision_id OR NOT EXISTS (
        SELECT 1 FROM knowledge_revisions WHERE revision_id = summary_id
    )) THEN RAISE EXCEPTION 'summary input must already be retained'
        USING ERRCODE = '23514'; END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(payload->'outgoing_links') LOOP
        IF NOT EXISTS (
            SELECT 1 FROM record_link_heads h JOIN record_link_versions v USING (link_id)
            JOIN knowledge_revisions origin ON origin.revision_id = v.source_revision_id
            WHERE h.source_record_id = owner AND h.link_id = (item->>'link_id')::uuid
            AND v.version = (item->>'version')::bigint AND NOT v.removed
            AND origin.record_id = owner AND origin.sequence <= seq
            AND v.link_type = item->>'link_type'
            AND v.target_record_id = (item->>'target_record_id')::uuid
            AND v.target_revision_id IS NOT DISTINCT FROM (item->>'target_revision_id')::uuid
            AND v.metadata = item->'metadata'
        ) THEN RAISE EXCEPTION 'snapshot link not owned by knowledge revision'
            USING ERRCODE = '23514'; END IF;
    END LOOP;
    RETURN NULL;
END $$
""",
    """
CREATE OR REPLACE FUNCTION record_link_lock_v1() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_id uuid; latest bigint;
BEGIN
    IF TG_TABLE_NAME = 'record_link_heads' THEN
        source_id := NEW.source_record_id;
        IF TG_OP = 'UPDATE' AND
           (NEW.link_id,NEW.source_record_id,NEW.owner_scope_key) IS DISTINCT FROM
           (OLD.link_id,OLD.source_record_id,OLD.owner_scope_key) THEN
            RAISE EXCEPTION 'link ownership is immutable' USING ERRCODE = '23514';
        END IF;
    ELSE
        SELECT source_record_id INTO source_id FROM record_link_heads WHERE link_id = NEW.link_id;
    END IF;
    PERFORM 1 FROM records WHERE record_id = source_id FOR UPDATE;
    IF TG_TABLE_NAME = 'record_link_versions' THEN
        SELECT max(version) INTO latest FROM record_link_versions WHERE link_id = NEW.link_id;
        IF NEW.version <> COALESCE(latest,0) + 1 THEN
            RAISE EXCEPTION 'link version must append' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END $$
""",
    """
CREATE OR REPLACE FUNCTION record_link_check_v1() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE head record_link_heads; source records; target records; ver record_link_versions;
        payload jsonb; active_count bigint; expected_links jsonb;
BEGIN
    SELECT * INTO head FROM record_link_heads WHERE link_id = NEW.link_id;
    SELECT * INTO source FROM records WHERE record_id = head.source_record_id;
    IF head.owner_scope_key <> source.scope_key OR head.current_version IS DISTINCT FROM
       (SELECT max(version) FROM record_link_versions WHERE link_id = head.link_id)
    THEN RAISE EXCEPTION 'link scope or head mismatch' USING ERRCODE = '23514'; END IF;
    FOR ver IN SELECT * FROM record_link_versions WHERE link_id = head.link_id
        AND version = CASE WHEN TG_TABLE_NAME = 'record_link_versions'
                           THEN (to_jsonb(NEW)->>'version')::bigint ELSE head.current_version END
    LOOP
        SELECT * INTO target FROM records WHERE record_id = ver.target_record_id;
        IF source.record_id = target.record_id OR
           (ver.link_type IN ('motivated_by','produces') AND
            (source.kind <> 'task' OR target.kind <> 'knowledge')) OR
           (ver.link_type IN ('supports','contradicts','supersedes') AND
            (source.kind <> 'knowledge' OR target.kind <> 'knowledge')) OR
           (ver.link_type = 'supersedes' AND source.scope_key <> target.scope_key)
        THEN RAISE EXCEPTION 'invalid record link matrix' USING ERRCODE = '23514'; END IF;
        IF source.kind = 'task' THEN
            IF ver.source_revision_id IS NOT NULL THEN
                RAISE EXCEPTION 'task source has no knowledge revision' USING ERRCODE = '23514';
            END IF;
        ELSE
            IF TG_TABLE_NAME = 'record_link_versions' AND ver.source_revision_id IS DISTINCT FROM
               (SELECT current_revision_id FROM knowledge_records WHERE record_id = source.record_id)
            THEN RAISE EXCEPTION 'new link version requires current source revision'
                USING ERRCODE = '23514'; END IF;
            SELECT p.snapshot INTO payload FROM knowledge_revisions r
                JOIN knowledge_revision_payloads p USING (revision_id)
                WHERE r.revision_id = ver.source_revision_id AND r.record_id = source.record_id;
            IF NOT FOUND THEN RAISE EXCEPTION 'wrong knowledge source revision'
                USING ERRCODE = '23514'; END IF;
            IF payload IS NOT NULL AND NOT ver.removed AND NOT payload->'outgoing_links' @>
                jsonb_build_array(jsonb_build_object('link_id',ver.link_id,'version',ver.version))
            THEN RAISE EXCEPTION 'link version missing from owning snapshot'
                USING ERRCODE = '23514'; END IF;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM knowledge_records k JOIN knowledge_revision_payloads p
            ON p.revision_id = k.current_revision_id
        WHERE k.record_id IN (SELECT target_record_id FROM record_link_versions
            WHERE link_id = head.link_id AND version IN (head.current_version,head.current_version-1))
        AND p.snapshot->>'successor_record_id' = source.record_id::text
        AND NOT EXISTS (
            SELECT 1 FROM record_link_heads h JOIN record_link_versions v
              ON (v.link_id,v.version) = (h.link_id,h.current_version)
            WHERE h.source_record_id = source.record_id AND v.target_record_id = k.record_id
              AND v.link_type = 'supersedes' AND NOT v.removed
        )
    ) THEN RAISE EXCEPTION 'link change would orphan a named successor'
        USING ERRCODE = '23514'; END IF;
    IF EXISTS (
        SELECT 1 FROM record_link_heads h JOIN record_link_versions v
          ON (v.link_id,v.version) = (h.link_id,h.current_version)
        WHERE h.source_record_id = source.record_id AND NOT v.removed
        GROUP BY v.link_type,v.target_record_id,v.target_revision_id HAVING count(*) > 1
    ) THEN RAISE EXCEPTION 'duplicate active record link' USING ERRCODE = '23514'; END IF;
    SELECT count(*) INTO active_count FROM record_link_heads h JOIN record_link_versions v
        ON (v.link_id,v.version) = (h.link_id,h.current_version)
        WHERE h.source_record_id = source.record_id AND NOT v.removed;
    IF active_count > 1000 THEN RAISE EXCEPTION 'too many outgoing links'
        USING ERRCODE = '23514'; END IF;
    IF source.kind = 'knowledge' THEN
        SELECT p.snapshot INTO payload FROM knowledge_records k
            JOIN knowledge_revision_payloads p ON p.revision_id = k.current_revision_id
            WHERE k.record_id = source.record_id;
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'link_id',h.link_id,'version',v.version,'link_type',v.link_type,
            'target_record_id',v.target_record_id,'target_revision_id',v.target_revision_id,
            'metadata',v.metadata) ORDER BY h.link_id), '[]'::jsonb)
        INTO expected_links FROM record_link_heads h JOIN record_link_versions v
          ON (v.link_id,v.version) = (h.link_id,h.current_version)
        WHERE h.source_record_id = source.record_id AND NOT v.removed;
        IF payload IS NOT NULL AND payload->'outgoing_links' IS DISTINCT FROM expected_links THEN
            RAISE EXCEPTION 'knowledge link change needs matching snapshot'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NULL;
END $$
""",
    """
CREATE OR REPLACE FUNCTION task_record_link_kind_v1() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM records WHERE record_id = NEW.record_id AND kind = 'task')
    THEN RAISE EXCEPTION 'task link state requires task record' USING ERRCODE = '23514'; END IF;
    IF TG_OP = 'UPDATE' AND (NEW.record_id <> OLD.record_id OR
        NEW.link_sequence < OLD.link_sequence OR
        (NEW.link_sequence > OLD.link_sequence AND NEW.link_token = OLD.link_token))
    THEN RAISE EXCEPTION 'task link state cannot rewind or reuse token' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$
""",
)


def install_record_validators_v1(bind) -> None:
    for statement in VALIDATORS_V1:
        bind.exec_driver_sql(statement)


def _trigger(bind, table, name, events, function, *, deferred=False):
    bind.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}" ON "{table}"')
    mode = "CONSTRAINT " if deferred else ""
    timing = "AFTER" if deferred else "BEFORE"
    defer = " DEFERRABLE INITIALLY DEFERRED" if deferred else ""
    bind.exec_driver_sql(
        f'CREATE {mode}TRIGGER "{name}" {timing} {events} ON "{table}"{defer} '
        f"FOR EACH ROW EXECUTE FUNCTION {function}()"
    )


def install_record_guards_v1(bind) -> None:
    for statement in GUARDS_V1:
        bind.exec_driver_sql(statement)
    for table in (
        "record_installation",
        "record_scopes",
        "knowledge_revisions",
        "knowledge_revision_payloads",
        "record_link_versions",
    ):
        _trigger(bind, table, f"tr_{table}_immutable_v1", "UPDATE OR DELETE", "record_immutable_v1")
    _trigger(
        bind, "records", "tr_records_identity_v1", "UPDATE OR DELETE", "record_identity_guard_v1"
    )
    _trigger(
        bind,
        "knowledge_revisions",
        "tr_knowledge_revisions_append_v1",
        "INSERT",
        "knowledge_revision_insert_v1",
    )
    for table in ("records", "knowledge_records", "knowledge_revisions", "knowledge_search"):
        _trigger(
            bind,
            table,
            f"tr_{table}_head_v1",
            "INSERT OR UPDATE OR DELETE",
            "knowledge_head_check_v1",
            deferred=True,
        )
    for table in ("knowledge_revisions", "knowledge_revision_payloads"):
        _trigger(
            bind,
            table,
            f"tr_{table}_payload_v1",
            "INSERT",
            "knowledge_revision_check_v1",
            deferred=True,
        )
    for table in ("record_link_heads", "record_link_versions"):
        _trigger(
            bind,
            table,
            f"tr_{table}_lock_v1",
            "INSERT OR UPDATE" if table == "record_link_heads" else "INSERT",
            "record_link_lock_v1",
        )
        _trigger(
            bind,
            table,
            f"tr_{table}_check_v1",
            "INSERT OR UPDATE",
            "record_link_check_v1",
            deferred=True,
        )
    _trigger(
        bind,
        "record_link_heads",
        "tr_record_link_heads_permanent_v1",
        "DELETE",
        "record_immutable_v1",
    )
    _trigger(
        bind,
        "task_record_link_state",
        "tr_task_record_link_state_kind_v1",
        "INSERT OR UPDATE",
        "task_record_link_kind_v1",
    )
    bind.execute(
        text(
            "INSERT INTO record_installation(singleton,installation_id) VALUES (1,:id) "
            "ON CONFLICT (singleton) DO NOTHING"
        ),
        {"id": uuid4()},
    )


def _before_create(metadata, bind, **kwargs):
    install_record_validators_v1(bind)


def _after_create(metadata, bind, **kwargs):
    from sqlalchemy import inspect

    # A caller may create an unrelated subset of metadata tables.
    if all(inspect(bind).has_table(name) for name in RECORD_TABLE_NAMES):
        install_record_guards_v1(bind)
        from src.knowledge.protection_schema import after_create

        after_create(metadata, bind)


def register_record_schema_events(metadata) -> None:
    event.listen(metadata, "before_create", _before_create)
    event.listen(metadata, "after_create", _after_create)
