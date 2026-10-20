"""Private logical MySQL backups, native integrity, and portable evidence bundles.

Backups preserve one explicitly named logical dataset. Verification never creates
a database. Restoration creates a fresh namespace and never activates an alias.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile
from importlib.resources import files

from .backup import BackupError
from .retained_paths import retained_path


class MySQLBackupError(BackupError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _runtime():
    from . import mysql_runtime
    return mysql_runtime


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value):
        raise MySQLBackupError("Invalid native schema identifier")
    return "`" + value + "`"


def _native_rows(connection, sql, parameters):
    with connection.raw.cursor() as cursor:
        cursor.execute(sql, parameters)
        return list(cursor.fetchall())


def verify_native_schema(connection):
    """Check real MySQL metadata and anti-join FK results, never facade PRAGMAs.

    The returned checksum records the actual metadata observed. Check-expression
    semantics and arbitrary source-trigger bodies are explicitly not certified;
    required runtime immutability guards are checked by name, event, and body.
    """
    tables = connection.tables
    if not tables:
        raise MySQLBackupError("Native dataset has no table descriptors")
    names = [table["physical"] for table in tables.values()]
    for name in names:
        _identifier(name)
    slots = ",".join("%s" for _ in names); parameters = tuple(names)
    engines = _native_rows(connection, "SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (" + slots + ")", parameters)
    columns = _native_rows(connection, "SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (" + slots + ") ORDER BY TABLE_NAME,ORDINAL_POSITION", parameters)
    keys = _native_rows(connection, "SELECT TABLE_NAME,CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME,ORDINAL_POSITION FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (" + slots + ") ORDER BY TABLE_NAME,CONSTRAINT_NAME,ORDINAL_POSITION", parameters)
    indexes = _native_rows(connection, "SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,COLUMN_NAME,SEQ_IN_INDEX FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (" + slots + ") ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX", parameters)
    triggers = _native_rows(connection, "SELECT EVENT_OBJECT_TABLE,TRIGGER_NAME,ACTION_TIMING,EVENT_MANIPULATION,ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE IN (" + slots + ") ORDER BY EVENT_OBJECT_TABLE,TRIGGER_NAME", parameters)
    issues = []
    engine_map = dict(engines); column_map = defaultdict(list); key_map = defaultdict(list); index_map = defaultdict(list)
    for row in columns: column_map[row[0]].append(row)
    for row in keys: key_map[row[0], row[1]].append(row)
    for row in indexes: index_map[row[0], row[1]].append(row)
    trigger_map = {row[1]: row for row in triggers}
    def issue(code, table): issues.append({"code": code, "table": table})
    normalize = lambda value: re.sub(r"\b(tinyint|smallint|mediumint|int|bigint)\(\d+\)", r"\1", str(value).lower()).replace(" ", "")
    for logical, table in tables.items():
        physical = table["physical"]
        if str(engine_map.get(physical, "")).lower() != "innodb": issue("missing_table_or_wrong_engine", logical)
        expected = [(c["name"], normalize(c["mysql_type"]),
                     "NO" if c.get("notnull") or c["name"] in table["primary"] or c["name"] == table["ordinal"] else "YES")
                    for c in table["columns"]]
        if table.get("extra_ordinal"): expected.append((table["ordinal"], "bigintunsigned", "NO"))
        observed = [(r[1], normalize(r[2]), r[3]) for r in column_map[physical]]
        if expected != observed: issue("native_columns_differ", logical)
        ordinal = next((r for r in column_map[physical] if r[1] == table["ordinal"]), None)
        if ordinal is None or "auto_increment" not in ordinal[4].lower(): issue("native_ordinal_generation_differs", logical)
        primary = [r[2] for r in key_map[physical, "PRIMARY"]]
        if primary != table["primary"]: issue("native_primary_key_differs", logical)
        actual_foreign = {(tuple(r[2] for r in group), group[0][3], tuple(r[4] for r in group))
                          for (name, _), group in key_map.items() if name == physical and group and group[0][3] is not None}
        expected_foreign = {(tuple(cols), tables[parent]["physical"], tuple(target or tables[parent]["primary"]))
                            for cols, parent, target in table["foreign"]}
        if actual_foreign != expected_foreign: issue("native_foreign_keys_differ", logical)
        unique = {tuple(r[3] for r in group) for (name, _), group in index_map.items() if name == physical and not group[0][2]}
        expected_unique = {tuple(fields) for fields in table["unique"]}
        if table["primary"]: expected_unique.add(tuple(table["primary"]))
        if table.get("extra_ordinal"): expected_unique.add((table["ordinal"],))
        if not expected_unique <= unique: issue("native_unique_constraint_missing", logical)
        prefix = physical[:-len(logical)] if physical.endswith(logical) else ""
        for event in ("INSERT", "UPDATE", "DELETE"):
            name = "guard_" + hashlib.sha256((prefix + logical + event).encode()).hexdigest()[:32]
            trigger = trigger_map.get(name)
            normalize_sql = lambda sql: re.sub(r"\s+", "", sql.lower().replace("`", ""))
            body = normalize_sql(trigger[4]) if trigger else ""
            expected_body = normalize_sql("BEGIN IF EXISTS(SELECT 1 FROM ufc_runtime_datasets WHERE dataset='" + connection.dataset + "' AND immutable=1 AND status='ready') THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='published imported datasets are immutable'; END IF; END")
            if (not trigger or trigger[0] != physical or trigger[2:4] != ("BEFORE", event)
                    or body != expected_body):
                issue("runtime_guard_missing_or_changed", logical)
    foreign_violations = connection.foreign_key_violations()
    if foreign_violations: issues.append({"code": "foreign_key_violations", "count": len(foreign_violations)})
    native = {key: sorted(rows, key=_json) for key, rows in
              {"engines": engines, "columns": columns, "keys": keys, "indexes": indexes, "triggers": triggers}.items()}
    return {"ok": not issues, "backend": "mysql", "method": "native_information_schema_and_foreign_key_anti_joins",
            "table_count": len(tables), "foreign_key_violations": len(foreign_violations), "issues": issues,
            "native_schema_sha256": hashlib.sha256(_json(native).encode()).hexdigest(),
            "verified_scope": ["InnoDB tables", "column types/order/nullability", "primary keys", "foreign keys", "required unique constraints", "runtime guard triggers"],
            "unverified_scope": ["column default semantics", "CHECK expression equivalence", "arbitrary source-trigger bodies"]}


def validate_snapshot(snapshot):
    """Verify schema SQL, every ordered row, and PK/FK relations offline."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1 or snapshot.get("storage_backend") != "mysql-dataset":
        raise MySQLBackupError("Unsupported MySQL dataset snapshot")
    runtime = _runtime()
    checked = runtime.validate_dataset_snapshot(snapshot)
    schema, _, _ = runtime.describe_schema(snapshot["schema_sql"])
    if hashlib.sha256(snapshot["schema_sql"].encode()).hexdigest() != snapshot["schema_sha256"]:
        raise MySQLBackupError("MySQL snapshot schema checksum changed")
    digest = hashlib.sha256(); counts = {}; row_dicts = {}
    if set(schema) != set(snapshot["tables"]): raise MySQLBackupError("Snapshot table list differs from schema")
    for name, table in sorted(snapshot["tables"].items()):
        columns = table["columns"]; expected_columns = [c["name"] for c in schema[name]["columns"]]
        if columns != expected_columns or table["ordinal_column"] != schema[name]["ordinal"]:
            raise MySQLBackupError("Snapshot columns differ from schema")
        digest.update(_json([name, columns]).encode() + b"\n"); previous = None; mapped = []
        for row in table["rows"]:
            if (not isinstance(row, list) or len(row) != len(columns) + 1 or type(row[0]) is not int
                    or previous is not None and row[0] <= previous):
                raise MySQLBackupError("Snapshot row shape or ordinal ordering changed")
            previous = row[0]; values = dict(zip(columns, row[1:])); mapped.append(values)
            if table["ordinal_column"] in values and values[table["ordinal_column"]] != row[0]:
                raise MySQLBackupError("Snapshot primary ordinal disagrees with its row")
            digest.update(_json(row).encode() + b"\n")
        row_dicts[name] = mapped; counts[name] = len(mapped)
    if digest.hexdigest() != snapshot["content_sha256"]:
        raise MySQLBackupError("MySQL snapshot content checksum changed")
    for name, descriptor in schema.items():
        constraints = [(descriptor["primary"], True)] if descriptor["primary"] else []
        constraints += [(fields, False) for fields in descriptor["unique"]]
        for fields, primary in constraints:
            seen = set()
            for row in row_dicts[name]:
                if any(row[field] is None for field in fields):
                    if primary: raise MySQLBackupError("Snapshot primary key is null")
                    continue
                key = _json([row[field] for field in fields])
                if key in seen: raise MySQLBackupError("Snapshot has duplicate key values")
                seen.add(key)
        for fields, parent, targets in descriptor["foreign"]:
            targets = targets or schema[parent]["primary"]
            available = {_json([r[field] for field in targets]) for r in row_dicts[parent]}
            if any(_json([r[field] for field in fields]) not in available for r in row_dicts[name]
                   if all(r[field] is not None for field in fields)):
                raise MySQLBackupError("Snapshot contains a foreign-key violation")
    return {"ok": True, "table_counts": counts, "content_sha256": digest.hexdigest(),
            "schema_sha256": snapshot["schema_sha256"], "foreign_key_violations": 0, "runtime_validation": checked}


def _read_json(path):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file(): raise MySQLBackupError("Backup is missing or is a symlink")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise MySQLBackupError("Duplicate JSON field in backup")
            result[key] = value
        return result
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise MySQLBackupError("MySQL backups must be owner-private regular files")
        raw = stream.read()
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(MySQLBackupError("Nonfinite JSON value"))), raw


def _load_json(path):
    return _read_json(path)[0]


def _publish_json(document, destination, *, validator=None):
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink(): raise FileExistsError("Backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".ufc-mysql-", dir=destination.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_json(document) + "\n"); stream.flush(); os.fsync(stream.fileno())
        if validator is not None: validator(name)
        os.link(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
    return destination


def _verified_backup(backup_path):
    envelope, raw = _read_json(backup_path)
    if not isinstance(envelope, dict) or envelope.get("format_version") != 1 or envelope.get("storage_backend") != "mysql-logical-backup":
        raise MySQLBackupError("Unsupported logical MySQL backup")
    stated = envelope.get("envelope_sha256")
    if stated != hashlib.sha256(_json({k: v for k, v in envelope.items() if k != "envelope_sha256"}).encode()).hexdigest():
        raise MySQLBackupError("MySQL backup envelope checksum changed")
    if not all(isinstance(envelope.get(k), dict) for k in ("snapshot", "delivery_snapshot", "native_schema_check")) or envelope["native_schema_check"].get("ok") is not True:
        raise MySQLBackupError("Backup lacks its verified dataset, delivery, or native-schema evidence")
    checked = validate_snapshot(envelope["snapshot"])
    delivery = validate_delivery_snapshot(envelope["delivery_snapshot"])
    return envelope, {"ok": True, "backend": "mysql", "operation": "verify_backup", "backup_path": str(Path(backup_path)),
            "backup_sha256": hashlib.sha256(raw).hexdigest(), "backup_size_bytes": len(raw),
            "verified_at_utc": _utc(), "logical_snapshot_verified": True, "native_restore_drill_performed": False,
            **{k: v for k, v in checked.items() if k != "runtime_validation"},
            "native_schema_check": envelope["native_schema_check"], "delivery": delivery,
            "scope": "one_logical_dataset_and_global_delivery_archive"}


def verify_backup(backup_path, *, timeout_seconds=60):
    from .backup import _validate_timeout
    _validate_timeout(timeout_seconds)
    return _verified_backup(backup_path)[1]


def create_backup(source_database, backup_path, *, timeout_seconds=60):
    from .backup import _validate_timeout
    _validate_timeout(timeout_seconds)
    destination = Path(backup_path).expanduser().absolute()
    if destination.exists() or destination.is_symlink(): raise FileExistsError("Backup destination already exists")
    runtime = _runtime()
    with closing(runtime.connect_dataset(source_database, readonly=True)) as connection:
        native = verify_native_schema(connection)
    if not native["ok"]: raise MySQLBackupError("Native MySQL schema or foreign keys did not verify")
    snapshot = runtime.export_dataset_snapshot(source_database); validate_snapshot(snapshot)
    with closing(runtime.connect_dataset(source_database, readonly=True)) as connection:
        after = verify_native_schema(connection)
    if not after["ok"] or native["native_schema_sha256"] != after["native_schema_sha256"]:
        raise MySQLBackupError("MySQL schema changed during backup")
    delivery = export_delivery_snapshot()
    envelope = {"format_version": 1, "storage_backend": "mysql-logical-backup", "created_at_utc": _utc(),
                "scope": "one_logical_dataset_and_global_delivery_archive", "snapshot": snapshot,
                "delivery_snapshot": delivery, "native_schema_check": native,
                "cross_component_consistency": "dataset_and_delivery_each_transactionally_consistent_separate_transactions"}
    envelope["envelope_sha256"] = hashlib.sha256(_json(envelope).encode()).hexdigest()
    destination = _publish_json(envelope, destination, validator=verify_backup)
    return {**verify_backup(destination), "operation": "create_backup", "source_database": str(source_database)}


def restore_backup(backup_path, restore_target, *, timeout_seconds=60):
    from .backup import _validate_timeout
    _validate_timeout(timeout_seconds)
    runtime = _runtime()
    if not runtime.is_mysql_target(restore_target):
        raise MySQLBackupError("A MySQL backup restores only to mysql:NEW_NAMESPACE")
    envelope, verified = _verified_backup(backup_path)
    delivery_target = _delivery_target(restore_target)
    if runtime.dataset_exists(runtime.dataset_name(restore_target)) or runtime.dataset_exists(runtime.dataset_name(delivery_target)):
        raise MySQLBackupError("Restore requires unused dataset and delivery recovery namespaces")
    receipt = runtime.import_dataset_snapshot(envelope["snapshot"], restore_target)
    with closing(runtime.connect_dataset(restore_target, readonly=True)) as connection:
        native = verify_native_schema(connection)
    if not native["ok"]: raise MySQLBackupError("Restored native MySQL schema did not verify; no alias was activated")
    replay = runtime.export_dataset_snapshot(restore_target)
    if (replay["content_sha256"] != envelope["snapshot"]["content_sha256"]
            or replay["schema_sha256"] != envelope["snapshot"]["schema_sha256"]
            or validate_snapshot(replay)["table_counts"] != verified["table_counts"]):
        raise MySQLBackupError("Restored MySQL dataset differs from the backup; no alias was activated")
    delivery_receipt = runtime.import_dataset_snapshot(envelope["delivery_snapshot"], delivery_target)
    with closing(runtime.connect_dataset(delivery_target, readonly=True)) as connection:
        delivery_native = verify_native_schema(connection)
    restored_delivery = runtime.export_dataset_snapshot(delivery_target)
    delivery_checked = validate_delivery_snapshot(restored_delivery)
    if not delivery_native["ok"] or restored_delivery["content_sha256"] != envelope["delivery_snapshot"]["content_sha256"]:
        raise MySQLBackupError("Restored delivery recovery archive did not verify; live publications were not changed")
    return {**verified, "operation": "restore_backup", "restored_database": str(restore_target),
            "restore_receipt": receipt, "native_schema_check": native,
            "backup_counts_match": True, "native_restore_drill_performed": True, "alias_activated": False,
            "delivery_recovery_target": delivery_target, "delivery_restore_receipt": delivery_receipt,
            "delivery_native_schema_check": delivery_native, "delivery": delivery_checked,
            "live_delivery_activated": False}


_DELIVERY_SCHEMA = """
CREATE TABLE ufc_app_document(document_sha256 TEXT PRIMARY KEY,kind TEXT NOT NULL,document_key TEXT NOT NULL,payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,source_sha256 TEXT,captured_at_utc TEXT NOT NULL,published_at_utc TEXT NOT NULL);
CREATE TABLE ufc_app_document_active(kind TEXT NOT NULL,document_key TEXT NOT NULL,document_sha256 TEXT NOT NULL,PRIMARY KEY(kind,document_key),FOREIGN KEY(document_sha256) REFERENCES ufc_app_document(document_sha256));
CREATE TABLE ufc_app_forecast_chain(ledger_name TEXT PRIMARY KEY,tail_sha256 TEXT NOT NULL,entries INTEGER NOT NULL);
CREATE TABLE ufc_app_forecast_entry(ledger_name TEXT NOT NULL,entry_number INTEGER NOT NULL,entry_sha256 TEXT NOT NULL,previous_sha256 TEXT NOT NULL,kind TEXT NOT NULL,forecast_id TEXT NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(ledger_name,entry_number),UNIQUE(ledger_name,kind,forecast_id),UNIQUE(ledger_name,entry_sha256),FOREIGN KEY(ledger_name) REFERENCES ufc_app_forecast_chain(ledger_name));
"""


def _snapshot_document(script, rows, *, dataset, metadata):
    descriptors, _, _ = _runtime().describe_schema(script); saved = {}; digest = hashlib.sha256()
    for name, descriptor in sorted(descriptors.items()):
        columns = [c["name"] for c in descriptor["columns"]]
        saved[name] = {"columns": columns, "ordinal_column": descriptor["ordinal"],
                       "rows": [[i, *row] for i, row in enumerate(rows[name], 1)]}
        digest.update(_json([name, columns]).encode() + b"\n")
        for row in saved[name]["rows"]: digest.update(_json(row).encode() + b"\n")
    return {"schema_version": 1, "storage_backend": "mysql-dataset", "dataset": dataset,
            "requested_dataset": dataset, "role": "legacy", "source_sha256": None,
            "content_sha256": digest.hexdigest(), "schema_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "schema_sql": script, "metadata": metadata, "tables": saved}


def export_delivery_snapshot():
    """Read all four global delivery tables in one repeatable-read transaction.

    The recovery SQL is portable logical SQL. Original native CREATE statements
    are retained as evidence and hashed separately; recovery does not claim to
    activate global documents, pointers, or ledger tables.
    """
    runtime = _runtime(); descriptors, _, _ = runtime.describe_schema(_DELIVERY_SCHEMA)
    rows = {}; native_schema = {}
    with closing(runtime.connect_server(readonly=True)) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                for name, descriptor in sorted(descriptors.items()):
                    identifier = _identifier(name)
                    cursor.execute("SHOW CREATE TABLE " + identifier)
                    native_schema[name] = cursor.fetchone()[1]
                    columns = [c["name"] for c in descriptor["columns"]]
                    cursor.execute("SELECT " + ",".join(_identifier(c) for c in columns) + " FROM " + identifier
                                   + " ORDER BY " + ",".join(_identifier(c) for c in descriptor["primary"]))
                    rows[name] = [list(row) for row in cursor.fetchall()]
            connection.rollback()
        except BaseException:
            connection.rollback(); raise
    snapshot = _snapshot_document(_DELIVERY_SCHEMA, rows, dataset="delivery-recovery-archive",
        metadata={"scope": "all_global_ufc_app_delivery_tables", "captured_at_utc": _utc(),
                  "recovery_only": True, "live_delivery_activated": False, "native_schema_sql": native_schema,
                  "native_schema_sha256": hashlib.sha256(_json(native_schema).encode()).hexdigest()})
    validate_delivery_snapshot(snapshot)
    return snapshot


def _rows(snapshot, table):
    saved = snapshot["tables"][table]
    return [dict(zip(saved["columns"], row[1:])) for row in saved["rows"]]


def validate_delivery_snapshot(snapshot):
    checked = validate_snapshot(snapshot)
    if snapshot["schema_sql"] != _DELIVERY_SCHEMA:
        raise MySQLBackupError("Unsupported delivery recovery schema")
    native = snapshot["metadata"].get("native_schema_sql")
    if not isinstance(native, dict) or set(native) != set(snapshot["tables"]) or hashlib.sha256(_json(native).encode()).hexdigest() != snapshot["metadata"].get("native_schema_sha256"):
        raise MySQLBackupError("Retained native delivery schema checksum differs")
    for row in _rows(snapshot, "ufc_app_document"):
        if hashlib.sha256(row["payload_json"].encode()).hexdigest() != row["payload_sha256"]:
            raise MySQLBackupError("Delivery document payload checksum differs")
        json.loads(row["payload_json"])
        identity = [row["kind"], row["document_key"], row["captured_at_utc"], row["source_sha256"], row["payload_sha256"]]
        if hashlib.sha256(_json(identity).encode()).hexdigest() != row["document_sha256"]:
            raise MySQLBackupError("Delivery document identity checksum differs")
    documents = {r["document_sha256"]: r for r in _rows(snapshot, "ufc_app_document")}
    for row in _rows(snapshot, "ufc_app_document_active"):
        document = documents[row["document_sha256"]]
        if (row["kind"], row["document_key"]) != (document["kind"], document["document_key"]):
            raise MySQLBackupError("Delivery active pointer identifies another document")
    from .forecast_ledger import _decode
    entries = defaultdict(list)
    for row in _rows(snapshot, "ufc_app_forecast_entry"): entries[row["ledger_name"]].append(row)
    chains = _rows(snapshot, "ufc_app_forecast_chain")
    for chain in chains:
        records = sorted(entries[chain["ledger_name"]], key=lambda row: row["entry_number"])
        decoded = _decode("".join(row["payload_json"] + "\n" for row in records))
        for number, (row, payload) in enumerate(zip(records, decoded), 1):
            if (row["entry_number"] != number or any(row[key] != payload[key] for key in ("entry_sha256", "previous_sha256", "kind", "forecast_id"))):
                raise MySQLBackupError("Forecast ledger index and payload disagree")
        if chain["entries"] != len(decoded) or chain["tail_sha256"] != (decoded[-1]["entry_sha256"] if decoded else "0" * 64):
            raise MySQLBackupError("Forecast ledger tail and entries disagree")
    return {"verified": True, "table_counts": checked["table_counts"], "content_sha256": snapshot["content_sha256"],
            "document_count": len(documents), "ledger_count": len(chains), "entry_count": sum(len(x) for x in entries.values()),
            "document_hashes_verified": True, "forecast_chains_verified": True, "live_delivery_activated": False}


def _delivery_target(target):
    name = _runtime().dataset_name(target)
    return "mysql:" + name[:70] + ".delivery." + hashlib.sha256(name.encode()).hexdigest()[:8]


def snapshot_receipts(snapshot):
    """Return immutable receipt identities independently of their local location."""
    receipts = []
    if "ingestion_runs" in snapshot["tables"]:
        for row in _rows(snapshot, "ingestion_runs"):
            receipts.append({"receipt_id": "run:" + str(row["run_id"]), "source": str(row["source"]),
                             "recorded_path": str(row["payload_path"] or ""), "sha256": str(row["sha256"] or "")})
    if "source_files" in snapshot["tables"]:
        metadata = {}
        for row in _rows(snapshot, "metadata") if "metadata" in snapshot["tables"] else []:
            try: value = json.loads(row["value"])
            except (TypeError, ValueError): value = row["value"]
            metadata[row["key"]] = value
        manifest_hash = metadata.get("source_manifest_sha256") or metadata.get("archive_manifest_sha256")
        if manifest_hash:
            receipts.append({"receipt_id": "source:manifest.json", "source": "ufcstats-archive-manifest",
                             "recorded_path": "manifest.json", "sha256": manifest_hash,
                             "manifest_sha256": manifest_hash, "is_manifest": True})
        for row in _rows(snapshot, "source_files"):
            receipts.append({"receipt_id": "source:" + row["file"], "source": "ufcstats-archive",
                             "recorded_path": row["file"], "sha256": row["sha256"],
                             "manifest_sha256": manifest_hash})
    if len({r["receipt_id"] for r in receipts}) != len(receipts):
        raise MySQLBackupError("Duplicate retained receipt identity")
    return receipts


def _receipt_index(root, snapshot):
    path = root / "receipt_paths.json"
    if not path.exists() and not path.is_symlink(): return {}
    index = _load_json(path)
    stated = index.get("index_sha256")
    if (index.get("format_version") != 1 or index.get("source_content_sha256") != snapshot["content_sha256"]
            or index.get("source_schema_sha256") != snapshot["schema_sha256"]
            or index.get("restored_dataset") != snapshot["dataset"]
            or stated != hashlib.sha256(_json({k:v for k,v in index.items() if k != "index_sha256"}).encode()).hexdigest()):
        raise MySQLBackupError("Recovery receipt index does not match this dataset")
    result = {}
    for entry in index["receipts"]:
        if entry["receipt_id"] in result: raise MySQLBackupError("Duplicate recovery receipt identity")
        result[entry["receipt_id"]] = entry
    return result


def _file_hash(path):
    if path.is_symlink(): raise MySQLBackupError("symlink_payload")
    if not path.is_file(): raise MySQLBackupError("missing_payload")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise MySQLBackupError("unreadable_payload")
        for block in iter(lambda: stream.read(1024 * 1024), b""): digest.update(block)
    return digest.hexdigest()


def delivery_receipts(snapshot):
    """Include exact saved forecast artifacts referenced by the frozen ledger."""
    receipts = []
    for row in _rows(snapshot, "ufc_app_forecast_entry"):
        payload = json.loads(row["payload_json"])
        if payload.get("source_path") is not None or payload.get("source_sha256") is not None:
            receipts.append({"receipt_id": "delivery:" + row["ledger_name"] + ":" + str(row["entry_number"]),
                             "source": "frozen-forecast-artifact", "recorded_path": payload.get("source_path") or "",
                             "sha256": payload.get("source_sha256") or ""})
    return receipts


def resolve_receipts(snapshot, root, *, extra_receipts=()):
    """Locate and hash actual files; an unavailable source is an explicit issue."""
    root = Path(root).expanduser().resolve(); index = _receipt_index(root, snapshot)
    manifests = {}; result = []
    for receipt in [*snapshot_receipts(snapshot), *extra_receipts]:
        row = dict(receipt); stated = row["sha256"]
        try:
            if not re.fullmatch(r"[0-9a-f]{64}", stated): raise MySQLBackupError("invalid_receipt_hash")
            if row["receipt_id"] in index:
                saved = index[row["receipt_id"]]
                if saved.get("sha256") != stated or not _safe_name(saved.get("archive_path")):
                    raise MySQLBackupError("invalid_recovery_receipt")
                payload = root / saved["archive_path"]
                if not payload.resolve().is_relative_to(root): raise MySQLBackupError("invalid_recovery_receipt")
            elif row.get("manifest_sha256"):
                manifest_hash = row["manifest_sha256"]
                if manifest_hash not in manifests:
                    matches = []
                    raw_root = root / "data/raw"
                    if raw_root.is_dir():
                        for candidate in raw_root.rglob("manifest.json"):
                            if not candidate.is_symlink() and _file_hash(candidate) == manifest_hash: matches.append(candidate)
                    manifests[manifest_hash] = matches
                matches = manifests[manifest_hash]
                if not matches: raise MySQLBackupError("missing_source_manifest")
                if not _safe_name(row["recorded_path"]): raise MySQLBackupError("invalid_payload_path")
                payload = matches[0] if row.get("is_manifest") else matches[0].parent / row["recorded_path"]
            else:
                if not row["recorded_path"]: raise MySQLBackupError("missing_payload_path")
                payload = Path(row["recorded_path"]).expanduser()
                if not payload.is_absolute(): payload = root / payload
                payload = retained_path(payload, reference=root / "data/raw")
            row["resolved_path"] = str(payload)
            if _file_hash(payload) != stated: raise MySQLBackupError("payload_hash_mismatch")
            row["verified"] = True
        except (OSError, ValueError, MySQLBackupError) as exc:
            code = str(exc) if isinstance(exc, MySQLBackupError) else "unreadable_payload"
            row.update(verified=False, issue=code)
        result.append(row)
    return result


def dataset_state(target):
    inspected = _runtime().inspect_dataset(target, verify=True)
    return {"backend": "mysql", "dataset": inspected["dataset"], "content_sha256": inspected["content_sha256"],
            "schema_sha256": inspected["schema_sha256"], "publication_version": inspected["publication_version"]}


def verify_evidence(target, *, project_root=None, max_issues=100):
    if type(max_issues) is not int or max_issues < 1: raise ValueError("max_issues must be a positive integer")
    root = Path(project_root).expanduser().resolve() if project_root else Path.cwd().resolve()
    report = {"checked_at_utc": _utc(), "database_path": str(target), "project_root": str(root), "backend": "mysql",
              "database_mtime_ns": None, "database_file_state": None, "status": "unavailable", "ok": False,
              "database_integrity": None, "foreign_key_violations": None, "missing_migrations": [],
              "receipt_count": 0, "verified_receipts": 0, "by_source": {}, "issue_count": 0, "issues": []}
    def issue(code, detail="", receipt_id=None):
        report["issue_count"] += 1
        if len(report["issues"]) < max_issues:
            report["issues"].append({"code": code, "detail": detail, **({"receipt_id": receipt_id} if receipt_id else {})})
    try:
        before = dataset_state(target)
        snapshot = _runtime().export_dataset_snapshot(target); validate_snapshot(snapshot)
        with closing(_runtime().connect_dataset(target, readonly=True)) as connection:
            native = verify_native_schema(connection)
        report["database_integrity"] = native; report["foreign_key_violations"] = native["foreign_key_violations"]
        if not native["ok"]: issue("mysql_native_integrity_failed", "Native metadata or foreign-key checks failed")
        if "schema_migrations" in snapshot["tables"]:
            applied = {str(r["version"]) for r in _rows(snapshot, "schema_migrations")}
            expected = {p.name for p in files("ufc_odds_model").joinpath("sql/migrations").iterdir() if p.name.endswith(".sql")}
            report["missing_migrations"] = sorted(expected - applied)
            if report["missing_migrations"]: issue("missing_migrations", ", ".join(report["missing_migrations"]))
        receipts = resolve_receipts(snapshot, root); counts = Counter()
        for receipt in receipts:
            counts[receipt["source"]] += 1
            if receipt["verified"]: report["verified_receipts"] += 1
            else: issue(receipt["issue"], "Retained receipt bytes did not verify", receipt["receipt_id"])
        report["receipt_count"] = len(receipts); report["by_source"] = dict(sorted(counts.items()))
        if not receipts: issue("no_source_receipts", "No retained source payload is recorded in this dataset")
        after = dataset_state(target); report["database_file_state"] = after
        if before != after or snapshot["content_sha256"] != after["content_sha256"]:
            issue("database_changed_during_check", "Repeat verification after database writes stop")
    except Exception:
        # Database drivers may include credential-bearing endpoints; never copy
        # arbitrary connection exceptions into an integrity artifact.
        issue("database_read_failed", "MySQL verification could not complete")
        return report
    report["ok"] = report["issue_count"] == 0; report["status"] = "verified" if report["ok"] else "issues"
    return report


def _safe_name(value):
    return isinstance(value, str) and bool(value) and not value.startswith("/") and "\\" not in value and all(p not in ("", ".", "..") for p in value.split("/"))


def _bundle_manifest(archive):
    members = archive.namelist()
    if len(members) != len(set(members)) or "manifest.json" not in members:
        raise MySQLBackupError("Duplicate archive entries or missing manifest")
    manifest = json.loads(archive.read("manifest.json"))
    if not isinstance(manifest, dict) or manifest.get("format_version") != 2 or manifest.get("storage_backend") != "mysql":
        raise MySQLBackupError("Unsupported MySQL evidence bundle")
    names = []; receipt_ids = []
    if not isinstance(manifest.get("files"), list): raise MySQLBackupError("Missing bundle inventory")
    for entry in manifest["files"]:
        if not isinstance(entry, dict): raise MySQLBackupError("Invalid archive inventory entry")
        name = entry.get("archive_path"); role = entry.get("role")
        if not _safe_name(name) or role not in ("database", "payload", "models", "reports"):
            raise MySQLBackupError("Unsafe archive entry")
        if role == "database" and name != "database.mysql.json": raise MySQLBackupError("Invalid database archive entry")
        if role == "payload":
            if not name.startswith("payloads/") or not isinstance(entry.get("receipt_id"), str):
                raise MySQLBackupError("Invalid payload archive entry")
            receipt_ids.append(entry["receipt_id"])
        if role in ("models", "reports") and not name.startswith(role + "/"):
            raise MySQLBackupError("Invalid artifact archive entry")
        if not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise MySQLBackupError("Invalid archive checksum")
        if type(entry.get("size_bytes")) is not int or entry["size_bytes"] < 0:
            raise MySQLBackupError("Invalid archive size")
        names.append(name)
    if (len(names) != len(set(names)) or sorted(members) != sorted([*names, "manifest.json"])
            or sum(e["role"] == "database" for e in manifest["files"]) != 1
            or len(receipt_ids) != len(set(receipt_ids))):
        raise MySQLBackupError("Bundle inventory does not match its contents")
    return manifest


def _copy_zip_entry(archive, name, destination):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with archive.open(name) as source, os.fdopen(descriptor, "wb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def _copy_private(source, destination):
    source = Path(source).expanduser()
    if source.is_symlink() or not source.is_file(): raise MySQLBackupError("Archive is missing or a symlink")
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "rb") as origin, os.fdopen(output, "wb") as target:
        if not stat.S_ISREG(os.fstat(origin.fileno()).st_mode): raise MySQLBackupError("Archive is not a regular file")
        shutil.copyfileobj(origin, target, length=1024 * 1024)


def _verify_stable_bundle(bundle, workspace):
    with zipfile.ZipFile(bundle) as archive:
        manifest = _bundle_manifest(archive)
        for entry in manifest["files"]:
            digest = hashlib.sha256(); size = 0
            with archive.open(entry["archive_path"]) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block); size += len(block)
            if digest.hexdigest() != entry["sha256"] or size != entry["size_bytes"]:
                raise MySQLBackupError("Archived evidence bytes differ from inventory")
        database = workspace / "database.mysql.json"
        _copy_zip_entry(archive, "database.mysql.json", database)
    envelope, verified = _verified_backup(database)
    receipts = [*snapshot_receipts(envelope["snapshot"]), *delivery_receipts(envelope["delivery_snapshot"])]
    expected = {r["receipt_id"]: r["sha256"] for r in receipts}
    actual = {e["receipt_id"]: e["sha256"] for e in manifest["files"] if e["role"] == "payload"}
    if len(expected) != len(receipts) or actual != expected:
        raise MySQLBackupError("Archived payloads do not match dataset and frozen-forecast receipts")
    return manifest, envelope, {"ok": True, "operation": "verify_bundle", "backend": "mysql",
        "bundle_sha256": _sha(bundle), "verified_at_utc": _utc(), "receipt_count": len(receipts),
        "artifact_count": sum(e["role"] in ("models", "reports") for e in manifest["files"]),
        "table_counts": verified["table_counts"], "content_sha256": verified["content_sha256"],
        "schema_sha256": verified["schema_sha256"], "delivery": verified["delivery"],
        "logical_snapshot_verified": True, "native_restore_drill_performed": False}


def verify_bundle(bundle_path):
    with tempfile.TemporaryDirectory(prefix="ufc-mysql-bundle-verify-") as directory:
        root = Path(directory); stable = root / "evidence.zip"; _copy_private(bundle_path, stable)
        _, _, report = _verify_stable_bundle(stable, root)
    return {**report, "bundle_path": str(Path(bundle_path))}


def create_bundle(database_path, output_path, *, project_root=None, models_dir="models", reports_dir="reports"):
    from .evidence_bundle import _optional_files, _write_file
    root = Path(project_root).expanduser().resolve() if project_root else Path.cwd().resolve()
    destination = Path(output_path).expanduser().absolute()
    if destination.exists() or destination.is_symlink(): raise FileExistsError("Bundle destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ufc-mysql-bundle-", dir=destination.parent) as directory:
        temporary = Path(directory); database = temporary / "database.mysql.json"
        create_backup(database_path, database); envelope, _ = _verified_backup(database)
        receipts = resolve_receipts(envelope["snapshot"], root, extra_receipts=delivery_receipts(envelope["delivery_snapshot"]))
        if any(not r["verified"] for r in receipts):
            raise MySQLBackupError("Cannot archive missing, linked, or changed retained receipt payloads")
        archive_path = temporary / "evidence.zip"; entries = []
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            entries.append(_write_file(archive, database, "database.mysql.json", "database"))
            for number, receipt in enumerate(receipts, 1):
                entry = _write_file(archive, Path(receipt["resolved_path"]), f"payloads/receipt-{number:08d}", "payload", expected_sha256=receipt["sha256"])
                entries.append({**entry, "receipt_id": receipt["receipt_id"]})
            for label, configured in (("models", models_dir), ("reports", reports_dir)):
                directory = Path(configured).expanduser() if configured is not None else None
                if directory is not None and not directory.is_absolute(): directory = root / directory
                for path, name in _optional_files(directory, label): entries.append(_write_file(archive, path, name, label))
            archive.writestr("manifest.json", _json({"format_version": 2, "storage_backend": "mysql", "created_at_utc": _utc(), "files": entries}) + "\n")
        checked = verify_bundle(archive_path); archive_path.chmod(0o600); os.link(archive_path, destination)
    return {**checked, "operation": "create_bundle", "bundle_path": str(destination)}


def restore_bundle(bundle_path, output_directory, *, mysql_target=None):
    if mysql_target is None or not _runtime().is_mysql_target(mysql_target):
        raise MySQLBackupError("MySQL bundle recovery requires --mysql-target mysql:NEW_NAMESPACE")
    destination = Path(output_directory).expanduser().absolute()
    with tempfile.TemporaryDirectory(prefix="ufc-mysql-bundle-restore-") as directory:
        temporary = Path(directory); stable = temporary / "evidence.zip"; _copy_private(bundle_path, stable)
        manifest, envelope, verified = _verify_stable_bundle(stable, temporary)
        destination.parent.mkdir(parents=True, exist_ok=True); destination.mkdir(mode=0o700, exist_ok=False)
        try:
            with zipfile.ZipFile(stable) as archive:
                for entry in manifest["files"]: _copy_zip_entry(archive, entry["archive_path"], destination / entry["archive_path"])
            structural = restore_backup(destination / "database.mysql.json", mysql_target)
            index = {"format_version": 1, "restored_dataset": _runtime().dataset_name(mysql_target),
                     "source_content_sha256": envelope["snapshot"]["content_sha256"],
                     "source_schema_sha256": envelope["snapshot"]["schema_sha256"],
                     "receipts": [{k: e[k] for k in ("receipt_id", "archive_path", "sha256")} for e in manifest["files"] if e["role"] == "payload"]}
            index["index_sha256"] = hashlib.sha256(_json(index).encode()).hexdigest()
            _publish_json(index, destination / "receipt_paths.json")
            replay = _runtime().export_dataset_snapshot(mysql_target)
            receipts = resolve_receipts(replay, destination, extra_receipts=delivery_receipts(envelope["delivery_snapshot"]))
            if any(not r["verified"] for r in receipts): raise MySQLBackupError("Recovered retained payloads did not verify")
            report = {**verified, "operation": "restore_bundle", "bundle_path": str(Path(bundle_path)),
                      "restored_database": str(mysql_target), "restored_at_utc": _utc(), "output_directory": str(destination),
                      "native_restore_drill_performed": True, "receipt_paths_rebound_in_database": False,
                      "receipt_index": "receipt_paths.json", "delivery_recovery_target": structural["delivery_recovery_target"],
                      "live_delivery_activated": False, "alias_activated": False}
            _publish_json(report, destination / "restore_report.json")
            return report
        except BaseException:
            # A failed database restore may leave a new recovery-only namespace;
            # never drop READY data as a cleanup side effect. Keep extracted
            # evidence for diagnosing/retrying that recovery.
            raise
