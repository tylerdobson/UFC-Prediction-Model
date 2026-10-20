"""MySQL-backed published documents and append-only forecast tracking.

Raw captures and exported reports remain files. Their data and provenance are
published here explicitly; dashboard requests only read committed publications.
"""
from __future__ import annotations

from contextlib import closing, nullcontext, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re


def _connection(*, readonly=False):
    from .mysql_runtime import connect_server
    return connect_server(readonly=readonly)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.:-]{0,95}', value):
        raise ValueError('Invalid delivery document or ledger identifier')
    return value


def ensure_schema():
    """Explicit setup only. No schema changes occur while viewing the dashboard."""
    statements = [
        """CREATE TABLE IF NOT EXISTS ufc_app_document (
            document_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            kind VARCHAR(96) NOT NULL, document_key VARCHAR(96) NOT NULL,
            payload_json LONGTEXT NOT NULL, payload_sha256 CHAR(64) NOT NULL,
            source_sha256 CHAR(64), captured_at_utc VARCHAR(40) NOT NULL,
            published_at_utc VARCHAR(40) NOT NULL,
            CHECK(JSON_VALID(payload_json))) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
        """CREATE TABLE IF NOT EXISTS ufc_app_document_active (
            kind VARCHAR(96) NOT NULL, document_key VARCHAR(96) NOT NULL,
            document_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            PRIMARY KEY(kind,document_key),
            FOREIGN KEY(document_sha256) REFERENCES ufc_app_document(document_sha256)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
        """CREATE TABLE IF NOT EXISTS ufc_app_forecast_chain (
            ledger_name VARCHAR(96) PRIMARY KEY, tail_sha256 CHAR(64) NOT NULL,
            entries BIGINT UNSIGNED NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
        """CREATE TABLE IF NOT EXISTS ufc_app_forecast_entry (
            ledger_name VARCHAR(96) NOT NULL, entry_number BIGINT UNSIGNED NOT NULL,
            entry_sha256 CHAR(64) NOT NULL, previous_sha256 CHAR(64) NOT NULL,
            kind VARCHAR(16) NOT NULL, forecast_id VARCHAR(128) NOT NULL,
            payload_json LONGTEXT NOT NULL,
            PRIMARY KEY(ledger_name,entry_number),
            UNIQUE KEY unique_forecast_action(ledger_name,kind,forecast_id),
            UNIQUE KEY unique_chain_hash(ledger_name,entry_sha256),
            FOREIGN KEY(ledger_name) REFERENCES ufc_app_forecast_chain(ledger_name),
            CHECK(kind IN ('freeze','settle')), CHECK(JSON_VALID(payload_json))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
    ]
    with closing(_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK('ufc_app_delivery_schema',30)")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError('Another delivery schema setup is running')
            try:
                for statement in statements:
                    cursor.execute(statement)
                for table in ('ufc_app_document', 'ufc_app_forecast_entry'):
                    for operation in ('UPDATE', 'DELETE'):
                        name = table + '_no_' + operation.lower()
                        cursor.execute('SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s', (name,))
                        if not cursor.fetchone()[0]:
                            statement = f"CREATE TRIGGER `{name}` BEFORE {operation} ON `{table}` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Published evidence is immutable'"
                            try:
                                cursor.execute(statement)
                            except Exception as exc:
                                if not exc.args or exc.args[0] != 1419:
                                    raise
                                from .mysql_runtime import schema_connection
                                with closing(schema_connection()) as installer:
                                    with installer.cursor() as privileged:
                                        privileged.execute(statement)
                connection.commit()
            finally:
                cursor.execute("SELECT RELEASE_LOCK('ufc_app_delivery_schema')")


def publish_document(kind, key, payload, *, captured_at_utc, source_sha256=None, connection=None):
    """Publish a version plus its active pointer in one transaction."""
    from .pipeline import parse_utc, utc_string
    kind, key = _identifier(kind), _identifier(key)
    captured = parse_utc(captured_at_utc)
    if captured > datetime.now(timezone.utc):
        raise ValueError('Cannot publish a future document capture')
    if source_sha256 is not None and not re.fullmatch('[0-9a-f]{64}', source_sha256):
        raise ValueError('Invalid document source digest')
    raw = _json(payload)
    payload_hash = hashlib.sha256(raw.encode()).hexdigest()
    identity = hashlib.sha256(_json([kind, key, captured_at_utc, source_sha256, payload_hash]).encode()).hexdigest()
    owns_connection = connection is None
    with (closing(_connection()) if owns_connection else nullcontext(connection)) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT d.captured_at_utc FROM ufc_app_document_active a JOIN ufc_app_document d USING(document_sha256) WHERE a.kind=%s AND a.document_key=%s FOR UPDATE', (kind, key))
                previous = cursor.fetchone()
                if previous and parse_utc(previous[0]) > captured:
                    raise ValueError('An older capture cannot replace a newer publication')
                cursor.execute('INSERT IGNORE INTO ufc_app_document VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                               (identity, kind, key, raw, payload_hash, source_sha256, captured_at_utc, utc_string(datetime.now(timezone.utc))))
                cursor.execute('INSERT INTO ufc_app_document_active VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE document_sha256=VALUES(document_sha256)', (kind, key, identity))
            if owns_connection:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {'document_sha256': identity, 'payload_sha256': payload_hash, 'kind': kind, 'key': key}


def activate_statistics_publication(publication):
    """Commit the verified dataset alias and its publication receipt together."""
    from .mysql_runtime import inspect_dataset, dataset_name
    target = dataset_name(publication['dataset'])
    verified = inspect_dataset(target, verify=True)
    if (verified['dataset'] != target or not verified['immutable'] or verified['role'] != 'statistics'
            or verified['content_sha256'] != publication['mysql_content_sha256']):
        raise ValueError('Statistics publication does not match a verified immutable dataset')
    with closing(_connection()) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT dataset FROM ufc_runtime_datasets WHERE dataset='statistics' FOR UPDATE")
                if cursor.fetchone():
                    raise ValueError('Statistics alias would hide an existing direct dataset namespace')
                cursor.execute("SELECT target FROM ufc_runtime_aliases WHERE alias='statistics' FOR UPDATE")
                cursor.fetchone()
                cursor.execute('INSERT INTO ufc_runtime_aliases(alias,target,activated_at_utc) VALUES(%s,%s,%s) '
                               'ON DUPLICATE KEY UPDATE target=VALUES(target),activated_at_utc=VALUES(activated_at_utc)',
                               ('statistics', target, publication['published_at_utc']))
            receipt = publish_document('pipeline', 'statistics', publication,
                                       captured_at_utc=publication['published_at_utc'],
                                       source_sha256=verified['content_sha256'], connection=connection)
            connection.commit()
            return receipt
        except BaseException:
            connection.rollback()
            raise


def load_document(kind, key, *, as_of=None):
    from .pipeline import parse_utc
    kind, key = _identifier(kind), _identifier(key)
    with closing(_connection(readonly=True)) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT d.payload_json,d.payload_sha256,d.captured_at_utc,d.source_sha256,d.document_sha256 FROM ufc_app_document_active a JOIN ufc_app_document d USING(document_sha256) WHERE a.kind=%s AND a.document_key=%s', (kind, key))
            row = cursor.fetchone()
    if row is None:
        return None
    raw, digest, captured, source, identity = row
    if hashlib.sha256(raw.encode()).hexdigest() != digest:
        raise ValueError('Published document payload changed')
    if parse_utc(captured) > (as_of or datetime.now(timezone.utc)):
        raise ValueError('Published document is later than the requested observation time')
    expected = hashlib.sha256(_json([kind, key, captured, source, digest]).encode()).hexdigest()
    if identity != expected:
        raise ValueError('Published document identity changed')
    return json.loads(raw)


def _chain(cursor, name):
    cursor.execute('INSERT IGNORE INTO ufc_app_forecast_chain VALUES(%s,%s,0)', (name, '0' * 64))
    cursor.execute('SELECT tail_sha256,entries FROM ufc_app_forecast_chain WHERE ledger_name=%s FOR UPDATE', (name,))
    return cursor.fetchone()


@contextmanager
def _ledger_connection(name):
    # Serialize initial chain creation too: concurrent INSERT IGNORE followed by
    # FOR UPDATE can otherwise deadlock while upgrading shared duplicate locks.
    with closing(_connection()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(CONCAT('ufc_fc:',LEFT(SHA2(CONCAT(DATABASE(),':',%s),256),48)),30)", (name,))
            if cursor.fetchone()[0] != 1:
                raise RuntimeError('Another writer holds this forecast ledger')
        try:
            yield connection
        finally:
            connection.rollback()
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(CONCAT('ufc_fc:',LEFT(SHA2(CONCAT(DATABASE(),':',%s),256),48)))", (name,))


def read_forecast_entries(name='forecast'):
    from .forecast_ledger import _decode
    name = _identifier(name)
    with closing(_connection(readonly=True)) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT tail_sha256,entries FROM ufc_app_forecast_chain WHERE ledger_name=%s', (name,))
            chain = cursor.fetchone()
            cursor.execute('SELECT entry_number,payload_json,entry_sha256,previous_sha256,kind,forecast_id FROM ufc_app_forecast_entry WHERE ledger_name=%s ORDER BY entry_number', (name,))
            rows = cursor.fetchall()
    entries = _decode(''.join(row[1] + '\n' for row in rows))
    for number, (row, entry) in enumerate(zip(rows, entries), 1):
        if row[0] != number or tuple(row[2:]) != (entry['entry_sha256'], entry['previous_sha256'], entry['kind'], entry['forecast_id']):
            raise ValueError('Forecast ledger index and retained payload disagree')
    if chain is None and entries or chain and (chain[1] != len(entries) or chain[0] != (entries[-1]['entry_sha256'] if entries else '0' * 64)):
        raise ValueError('Forecast ledger tail and records disagree')
    return entries


def append_forecast_entry(entry, name='forecast'):
    from .pipeline import utc_string
    from .research_evaluation import _digest_json
    name = _identifier(name)
    if entry.get('kind') not in {'freeze', 'settle'} or not isinstance(entry.get('forecast_id'), str):
        raise ValueError('Forecast entry must identify its action and forecast')
    if set(entry) & {'previous_sha256', 'entry_sha256', 'recorded_at_utc'}:
        raise ValueError('Forecast hash-chain fields are assigned by the database writer')
    with _ledger_connection(name) as connection:
        try:
            with connection.cursor() as cursor:
                tail, count = _chain(cursor, name)
                cursor.execute('SELECT COUNT(*) FROM ufc_app_forecast_entry WHERE ledger_name=%s AND kind=%s AND forecast_id=%s', (name, entry['kind'], entry['forecast_id']))
                if cursor.fetchone()[0]:
                    raise ValueError('Forecast already has this action recorded')
                payload = {'schema_version': 1, 'recorded_at_utc': utc_string(datetime.now(timezone.utc)), **entry, 'previous_sha256': tail}
                payload['entry_sha256'] = _digest_json(payload)
                cursor.execute('INSERT INTO ufc_app_forecast_entry VALUES(%s,%s,%s,%s,%s,%s,%s)',
                               (name, count + 1, payload['entry_sha256'], tail, entry['kind'], entry['forecast_id'], _json(payload)))
                cursor.execute('UPDATE ufc_app_forecast_chain SET tail_sha256=%s,entries=%s WHERE ledger_name=%s', (payload['entry_sha256'], count + 1, name))
            connection.commit()
            return payload
        except BaseException:
            connection.rollback()
            raise


def import_forecast_entries(entries, name='forecast'):
    """Preserve an existing validated chain byte semantics, including its dates."""
    from .forecast_ledger import _decode
    name = _identifier(name)
    entries = _decode(''.join(_json(entry) + '\n' for entry in entries))
    existing = read_forecast_entries(name)
    if existing != entries[:len(existing)]:
        raise ValueError('Imported chain conflicts with existing forecast records')
    with _ledger_connection(name) as connection:
        try:
            with connection.cursor() as cursor:
                tail, count = _chain(cursor, name)
                if count != len(existing) or tail != (existing[-1]['entry_sha256'] if existing else '0' * 64):
                    raise ValueError('Forecast chain changed during import; retry')
                for entry in entries[count:]:
                    count += 1
                    cursor.execute('INSERT INTO ufc_app_forecast_entry VALUES(%s,%s,%s,%s,%s,%s,%s)',
                                   (name, count, entry['entry_sha256'], entry['previous_sha256'], entry['kind'], entry['forecast_id'], _json(entry)))
                    tail = entry['entry_sha256']
                cursor.execute('UPDATE ufc_app_forecast_chain SET tail_sha256=%s,entries=%s WHERE ledger_name=%s', (tail, count, name))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {'entries': len(entries), 'imported': len(entries) - len(existing), 'tail_sha256': tail}


def register_report_forecast(report_path, card_path, *, root):
    """Register a verified research edition now, without backdating its freeze."""
    from pathlib import Path
    from .dashboard_forecast import load_saved_forecast
    from .pipeline import parse_utc
    from .research_evaluation import _digest_json
    root = Path(root).resolve()
    saved = load_saved_forecast(report_path, project_root=root)
    if saved['status'] != 'available' or saved['event_status'] != 'upcoming':
        raise ValueError('Forward registration requires a verified, unstarted report edition')
    report = saved['report']
    card = json.loads(Path(card_path).read_bytes())
    event = card['event']
    if (event['name'] != report['event']['name'] or event['event_date'] != report['event']['event_date']
            or parse_utc(event['event_start_utc']) != parse_utc(report['event']['start_time_utc'])):
        raise ValueError('The report and identity card describe different events')
    identities = {row['bout_id']: row for row in card['bouts']}
    rows = []
    for forecast in report['bouts']:
        identity = identities.get(forecast['bout_id'])
        if identity is None or any(not identity[side].get('fighter_id') or identity[side]['name'] != forecast[side]
                                   for side in ('fighter_a', 'fighter_b')):
            raise ValueError('Report registration needs exact established participant identities')
        rows.append({'bout_id': forecast['bout_id'], 'fighter_a_id': identity['fighter_a']['fighter_id'],
                     'fighter_b_id': identity['fighter_b']['fighter_id'], 'fighter_a': forecast['fighter_a'],
                     'fighter_b': forecast['fighter_b'], 'model_probability': forecast['p_fighter_a'],
                     'market_no_vig_probability': None, 'market_rule': 'no_verified_prices_in_report'})
    identifier = report['forecast'].get('forecast_id') or _digest_json([event['event_id'], report['forecast'], rows])
    existing = [entry for entry in read_forecast_entries() if entry['kind'] == 'freeze' and entry['forecast_id'] == identifier]
    if existing:
        if existing[0]['rows'] != rows:
            raise ValueError('Registered report rows differ from the verified edition')
        return existing[0]
    source = Path(report_path).resolve()
    if not source.is_relative_to(root):
        raise ValueError('Report evidence must be retained inside the project')
    return append_forecast_entry({'kind': 'freeze', 'forecast_id': identifier,
        'event_id': event['event_id'], 'event_name': event['name'], 'event_date': event['event_date'],
        'event_start_at_utc': event['event_start_utc'], 'model_version': report['forecast']['model_version'],
        'cutoff_at_utc': report['forecast']['cutoff_at_utc'], 'rows': rows, 'rows_sha256': _digest_json(rows),
        'unscored_bouts': report['unscored_bouts'], 'coverage': {'scored': len(rows), 'held': len(report['unscored_bouts'])},
        'research_only': True, 'promotion_eligible': False, 'wager_authorization': False,
        'recovered_saved_forecast': False, 'recovered_after_event': False,
        'registration_basis': 'verified_saved_report_registered_before_event',
        'source_path': str(source.relative_to(root)), 'source_sha256': saved['artifacts']['json']['sha256'],
        'registration_note': 'Registered at the database recorded_at_utc, not at the earlier report cutoff. Held matchups have no probability or stake.'})
