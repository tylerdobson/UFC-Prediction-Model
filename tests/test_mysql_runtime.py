"""Native schema compilation and SQLite-contract compatibility without fallback."""
from pathlib import Path
import os
import hashlib
import copy
import json
from decimal import Decimal
import sqlite3
import tempfile
import uuid
import unittest
from unittest.mock import patch

from ufc_odds_model import mysql_runtime as rt
from ufc_odds_model.historical_stats import SCHEMA


class RuntimeContractTests(unittest.TestCase):
    def test_target_handles_survive_legacy_path_coercion(self):
        for value in ('mysql:operating',Path('mysql:research-complete').resolve()):
            self.assertTrue(rt.is_mysql_target(value))
        self.assertEqual(rt.dataset_name(Path('mysql:research-complete').resolve()),'research-complete')
        self.assertFalse(rt.is_mysql_target('data/history.sqlite'))
        with self.assertRaises(rt.RuntimeDatabaseError):rt.dataset_name("bad';DROP DATABASE mysql")

    def test_rows_preserve_index_name_dict_and_value_iteration(self):
        row=rt.Row(('one','two'),(1,'second'))
        self.assertEqual(row[0],1);self.assertEqual(row['two'],'second')
        self.assertEqual(tuple(row),(1,'second'));self.assertEqual(dict(row),{'one':1,'two':'second'})
        c=rt.Cursor([(1,'a'),(2,'b')],('id','name'))
        self.assertEqual(c.fetchone()['name'],'a');self.assertEqual(c.fetchall()[0][0],2);self.assertIsNone(c.fetchone())

    def test_native_aggregate_numbers_preserve_serializable_sqlite_scalar_contract(self):
        self.assertIs(type(rt._sqlite_value(Decimal('12'))),int)
        self.assertEqual(rt._sqlite_value(Decimal('12')),12)
        self.assertEqual(rt._sqlite_value(Decimal('1.25')),1.25)
        self.assertEqual(json.dumps({'count':rt._sqlite_value(Decimal('2'))}),' {"count": 2}'.strip())
        with self.assertRaises(rt.RuntimeIntegrityError):rt._sqlite_value(Decimal('NaN'))

    def test_all_fifteen_operating_migrations_compile_to_native_tables_and_triggers(self):
        script,versions=rt._operating_schema();tables,indexes,triggers=rt._schema(script)
        self.assertEqual(len(versions),15)
        self.assertIn('odds_schedule_snapshots',tables)
        self.assertIn('metadata_json',[c['name'] for c in tables['predictions']['columns']])
        self.assertIn('roster_snapshot_id',[c['name'] for c in tables['prefight_gate_checks']['columns']])
        self.assertEqual(next(c['mysql_type'] for c in tables['prefight_gate_checks']['columns'] if c['name']=='checked_at_utc'),'VARCHAR(191)')
        self.assertEqual(tables['predictions']['ordinal'],'prediction_id')
        self.assertTrue(tables['fighters']['extra_ordinal'])
        self.assertIn('paper_bets_freeze_decision',triggers)
        for name,table in tables.items():table['physical']='test_'+name
        for table in tables.values():
            ddl=rt._table_ddl(table)
            self.assertIn('ENGINE=InnoDB',ddl)
            self.assertNotIn('AUTOINCREMENT',ddl)
        count_ddl=rt._table_ddl(tables['fight_stat_observations'])
        self.assertNotRegex(count_ddl,r'`sig_strikes_attempted` BIGINT[^,]*CHECK')
        self.assertIn('`sig_strikes_landed` <= `sig_strikes_attempted`',count_ddl)
        trigger=rt._trigger_ddl(triggers['paper_bets_freeze_decision'],tables,'test_','fixture')
        self.assertIn("SIGNAL SQLSTATE '45000'",trigger)
        self.assertIn('<=>',trigger)
        self.assertNotIn('RAISE(',trigger)
        roster=rt._trigger_ddl(triggers['prefight_gate_checks_require_roster'],tables,'test_','fixture')
        self.assertNotIn('julianday',roster)
        self.assertIn('UNIX_TIMESTAMP',roster)

    def test_canonical_schema_retains_raw_receipts_and_explicit_row_order(self):
        tables,_,_=rt._schema(SCHEMA)
        self.assertEqual(len(tables),13)
        self.assertIn('raw_json',[c['name'] for c in tables['bouts']['columns']])
        self.assertIn('source_round_rows',tables)
        self.assertEqual(tables['source_round_rows']['ordinal'],'row_number')
        self.assertTrue(tables['round_facts']['extra_ordinal'])
        self.assertEqual(tables['fighter_bout_facts']['primary'],['bout_id','fighter_id'])
        tables['metadata']['physical']='fixture_metadata'
        metadata_ddl=rt._table_ddl(tables['metadata'])
        self.assertIn('PRIMARY KEY (`key`)',metadata_ddl)
        self.assertNotIn('PRIMARY `KEY`',metadata_ddl)

    def test_compiled_columns_match_independent_sqlite_schema_oracle(self):
        # The test oracle is explicit legacy SQLite. Native production schema
        # compilation itself never opens or stages a SQLite database.
        for script in (rt._operating_schema()[0],SCHEMA):
            with self.subTest(schema=script[:40]),sqlite3.connect(':memory:') as oracle:
                oracle.executescript(script);tables,_,_=rt._schema(script)
                for name,table in tables.items():
                    expected=[r[1] for r in oracle.execute('PRAGMA table_info("'+name+'")')]
                    self.assertEqual([c['name'] for c in table['columns']],expected,name)

    def test_sql_mapping_preserves_strings_comments_percent_and_bindings(self):
        tables,_,_=rt._schema('CREATE TABLE events(event_id TEXT PRIMARY KEY,name TEXT);')
        tables['events']['physical']='test_events'
        mapped=rt._mapped("SELECT name FROM events WHERE name LIKE '%events%' AND event_id=?",tables)
        self.assertIn('FROM `test_events`',mapped)
        self.assertIn("'%%events%%'",mapped)
        self.assertIn('`event_id`=%s',mapped)
        self.assertEqual(rt._plain("SELECT 'a--b' -- real comment\n"),"SELECT 'a--b'")

    def test_implicit_result_alias_matching_a_logical_table_keeps_its_public_name(self):
        tables,_,_=rt.describe_schema('CREATE TABLE bouts(bout_id TEXT PRIMARY KEY);')
        tables['bouts']['physical']='rt_fixture_bouts'
        mapped=rt._mapped('SELECT COUNT(b.bout_id) bouts FROM bouts b ORDER BY bouts',tables)
        self.assertIn(') bouts FROM `rt_fixture_bouts` b ORDER BY bouts',mapped)
        self.assertNotIn(') `rt_fixture_bouts`',mapped)

    def test_explicit_rowid_projection_is_visible_with_or_without_a_table_alias(self):
        tables,_,_=rt.describe_schema('CREATE TABLE fighters(id TEXT PRIMARY KEY,name TEXT);')
        tables['fighters']['physical']='test_fighters'
        con=rt.Connection(None,'fixture',tables,{'role':'research','immutable':True,'status':'ready'})
        for sql in ('SELECT rowid FROM fighters ORDER BY rowid',
                    'SELECT b.rowid FROM fighters b ORDER BY b.rowid'):
            with self.subTest(sql=sql):
                mapped=con._translate(sql)
                self.assertIn(' AS `rowid` FROM',mapped)
                self.assertNotIn('.rowid',mapped)
        self.assertNotIn(' AS `rowid`',con._translate('SELECT * FROM fighters ORDER BY rowid'))

    def test_implicit_values_insert_addresses_source_columns_instead_of_hidden_ordinal(self):
        tables,_,_=rt.describe_schema('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);')
        tables['metadata']['physical']='rt_fixture_metadata'
        con=rt.Connection(None,'fixture',tables,{'role':'statistics','immutable':True,'status':'building'})
        for sql in ('INSERT INTO metadata VALUES(?,?)','REPLACE INTO "metadata" VALUES(?,?)'):
            mapped=con._translate(sql)
            self.assertIn('`rt_fixture_metadata`(`key`,`value`) VALUES(%s,%s)',mapped)
            self.assertNotIn(rt.ORDINAL,mapped)

    @staticmethod
    def snapshot_fixture():
        script='CREATE TABLE parents(id INTEGER PRIMARY KEY,name TEXT UNIQUE); CREATE TABLE children(id TEXT PRIMARY KEY,parent_id INTEGER REFERENCES parents(id),receipt BLOB); CREATE INDEX child_parent ON children(parent_id);'
        tables,_,_=rt.describe_schema(script)
        saved={
            'parents':{'columns':['id','name'],'ordinal_column':'id','rows':[[7,7,'Alpha'],[12,12,'Beta']]},
            'children':{'columns':['id','parent_id','receipt'],'ordinal_column':rt.ORDINAL,'rows':[[4,'child',7,{'$bytes':'00ff'}]]}}
        h=hashlib.sha256()
        for name,table in sorted(saved.items()):
            h.update(rt._json([name,table['columns']]).encode()+b'\n')
            for row in table['rows']:h.update(rt._json(row).encode()+b'\n')
        return {'schema_version':1,'storage_backend':'mysql-dataset','dataset':'legacy-fixture','requested_dataset':'research',
                'role':'research','source_sha256':'a'*64,'content_sha256':h.hexdigest(),
                'schema_sha256':hashlib.sha256(script.encode()).hexdigest(),'schema_sql':script,
                'metadata':{'schema_sql':script},'tables':saved}

    def test_snapshot_validator_retains_schema_binary_cells_ordinals_and_foreign_keys(self):
        document=self.snapshot_fixture();verified=rt.validate_dataset_snapshot(document)
        self.assertEqual(verified['counts'],{'children':1,'parents':2})
        self.assertEqual(len(verified['indexes']),1)
        self.assertEqual(rt._snapshot_cell({'$bytes':'00ff'}),b'\x00\xff')
        self.assertEqual(document,json.loads(json.dumps(document)))
        changes=(lambda d:d['tables']['children']['rows'][0].__setitem__(2,999),
                 lambda d:d['tables']['parents']['rows'][1].__setitem__(0,7),
                 lambda d:d['tables']['parents']['rows'][0].__setitem__(2,'Changed'),
                 lambda d:d.__setitem__('schema_sql',d['schema_sql']+' '),
                 lambda d:d['tables']['children'].__setitem__('columns',['id','receipt','parent_id']),
                 lambda d:d['tables']['children']['rows'][0].__setitem__(3,{'$bytes':'NOT-HEX'}))
        for change in changes:
            altered=copy.deepcopy(document);change(altered)
            with self.subTest(document=altered),self.assertRaises(rt.RuntimeIntegrityError):rt.validate_dataset_snapshot(altered)

    def test_snapshot_validator_checks_orphans_even_with_a_recomputed_row_hash(self):
        document=self.snapshot_fixture();document['tables']['children']['rows'][0][2]=999
        h=hashlib.sha256()
        for name,table in sorted(document['tables'].items()):
            h.update(rt._json([name,table['columns']]).encode()+b'\n')
            for row in table['rows']:h.update(rt._json(row).encode()+b'\n')
        document['content_sha256']=h.hexdigest()
        with self.assertRaisesRegex(rt.RuntimeIntegrityError,'foreign-key'):rt.validate_dataset_snapshot(document)
        with patch.object(rt,'connect_server') as server:
            with self.assertRaises(rt.RuntimeIntegrityError):rt.import_dataset_snapshot(document,'unused-restore')
            server.assert_not_called()

    def test_unsupported_schema_and_pragma_fail_explicitly(self):
        with self.assertRaises(rt.RuntimeDatabaseError):rt._schema('CREATE VIRTUAL TABLE f USING fts5(name);')
        c=rt.Connection(None,'fixture',{}, {'role':'research','immutable':True,'status':'ready'})
        with self.assertRaisesRegex(rt.RuntimeDatabaseError,'Unsupported SQLite PRAGMA'):c.execute('PRAGMA journal_mode=DELETE')
        with self.assertRaisesRegex(rt.RuntimeDatabaseError,'read-only'):c.execute('DELETE FROM events')

    def test_missing_mysql_configuration_does_not_open_or_create_sqlite(self):
        with patch.dict(os.environ,{},clear=True),patch.object(Path,'exists',return_value=False):
            with self.assertRaisesRegex(rt.RuntimeDatabaseError,'fallback is disabled'):rt._configuration()

    def test_import_replay_does_not_claim_a_mutated_operating_copy_still_matches_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'original.sqlite'
            with sqlite3.connect(source) as con:con.execute('CREATE TABLE original(id INTEGER PRIMARY KEY)')
            receipt={'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'immutable':False,
                     'content_sha256':'b'*64,'metadata':{'source_row_checksum':'a'*64}}
            with patch.object(rt,'dataset_exists',return_value=True),patch.object(rt,'inspect_dataset',return_value=receipt):
                with self.assertRaisesRegex(rt.RuntimeIntegrityError,'changed after'):rt.import_sqlite_snapshot(source,dataset='operating-copy',role='operating')

    def test_privileged_schema_helper_accepts_only_project_integrity_triggers(self):
        script,_=rt._operating_schema();tables,_,triggers=rt._schema(script)
        for name,table in tables.items():table['physical']='rt_fixture_'+name
        for statement in triggers.values():
            native=rt._trigger_ddl(statement,tables,'fixture_','fixture')
            self.assertEqual(rt._restricted_trigger(native),native)
        for sql in ('DROP DATABASE ufc_warehouse',
            "CREATE TRIGGER bad BEFORE UPDATE ON mysql_user FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='bad'",
            "CREATE TRIGGER bad BEFORE UPDATE ON ufc_app_rows FOR EACH ROW BEGIN DELETE FROM ufc_app_rows; END",
            "CREATE TRIGGER bad BEFORE UPDATE ON ufc_app_rows FOR EACH ROW BEGIN IF EXISTS(SELECT 1 FROM mysql.user) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='bad'; END IF; END"):
            with self.subTest(sql=sql),self.assertRaises(rt.RuntimeDatabaseError):rt._restricted_trigger(sql)


@unittest.skipUnless(os.environ.get('UFC_MYSQL_INTEGRATION')=='1','Requires an isolated project MySQL test database')
class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.base='test-runtime-'+uuid.uuid4().hex[:16]
        self.names=[];self.aliases=[]
        self.addCleanup(self.cleanup_owned_fixtures)

    def name(self,suffix):
        value=self.base+'-'+suffix;self.names.append(value);return value

    def research_source(self,suffix,*,version_limit=None,fighter_id='wikidata:Q42'):
        from importlib.resources import files
        from ufc_odds_model import db
        migrations=sorted((p for p in files('ufc_odds_model').joinpath('sql/migrations').iterdir() if p.name.endswith('.sql')),key=lambda p:p.name)
        if version_limit is not None:migrations=migrations[:version_limit]
        script='CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at_utc TEXT NOT NULL);\n'+'\n'.join(p.read_text() for p in migrations)
        name=self.name(suffix);con=rt.create_dataset(script,name,'research')
        try:
            con.executemany('INSERT INTO schema_migrations VALUES(?,?)',[(p.name,'2026-01-01T00:00:00Z') for p in migrations])
            db.upsert_fighter(con,fighter_id,'Example')
            rt.finalize_dataset(con)
        finally:con.close()
        return name

    def cleanup_owned_fixtures(self):
        # Only UUID namespaces created by this test are removed. No existing
        # application namespace, publication, source file or alias is touched.
        raw=rt.connect_server()
        try:
            with raw.cursor() as c:
                for alias in self.aliases:c.execute('DELETE FROM ufc_runtime_aliases WHERE alias=%s',(alias,))
                c.execute('SET SESSION foreign_key_checks=0')
                for name in self.names:
                    self.assertTrue(name.startswith(self.base+'-'))
                    c.execute('SELECT physical_name FROM ufc_runtime_tables WHERE dataset=%s',(name,))
                    for (table,) in c.fetchall():c.execute('DROP TABLE IF EXISTS '+rt._identifier(table))
                    c.execute('DELETE FROM ufc_runtime_tables WHERE dataset=%s',(name,))
                    c.execute('DELETE FROM ufc_runtime_datasets WHERE dataset=%s',(name,))
                c.execute('SET SESSION foreign_key_checks=1')
            raw.commit()
        finally:raw.close()

    def test_native_all_migrations_upsert_and_immutable_receipt(self):
        from ufc_odds_model import db
        name=self.name('operating');result=rt.ensure_operating_dataset(name)
        self.assertEqual(result['counts']['schema_migrations'],15)
        con=rt.connect_dataset(name)
        try:
            db.upsert_fighter(con,'fa','Alpha');db.upsert_fighter(con,'fa','Alpha Updated')
            self.assertEqual(con.execute('SELECT canonical_name FROM fighters WHERE fighter_id=?',('fa',)).fetchone()[0],'Alpha Updated')
            created=con.execute('INSERT INTO ingestion_runs(source,fetched_at_utc,payload_path,sha256) VALUES(?,?,?,?)',('fixture','2026-10-19T00:00:00Z','retained/fixture.json','a'*64)).lastrowid
            self.assertGreater(created,0);con.commit()
            with self.assertRaises(rt.RuntimeIntegrityError):con.execute('UPDATE ingestion_runs SET sha256=? WHERE run_id=?',('b'*64,created))
            con.rollback()
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:con.close()

    def test_original_row_values_and_rowids_import_exactly_with_alias_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'legacy.sqlite'
            with sqlite3.connect(source) as sqlite:
                sqlite.executescript('CREATE TABLE fighters(fighter_id TEXT PRIMARY KEY,name TEXT NOT NULL); CREATE TABLE bouts(bout_id INTEGER PRIMARY KEY,fighter_id TEXT REFERENCES fighters(fighter_id),value REAL);')
                sqlite.execute("INSERT INTO fighters(rowid,fighter_id,name) VALUES(7,'fa','Alpha')")
                sqlite.execute("INSERT INTO fighters(rowid,fighter_id,name) VALUES(12,'fb','Beta')")
                sqlite.execute("INSERT INTO bouts VALUES(3,'fa',1.25)")
            original=hashlib.sha256(source.read_bytes()).hexdigest();name=self.name('legacy')
            result=rt.import_sqlite_snapshot(source,dataset=name,role='research',expected_sha256=original)
            replay=rt.import_sqlite_snapshot(source,dataset=name,role='research',expected_sha256=original)
            self.assertTrue(replay['idempotent_reuse']);self.assertEqual(result['content_sha256'],replay['content_sha256'])
            alias=self.base+'-alias';self.aliases.append(alias);rt.activate_alias(alias,name)
            con=rt.connect_dataset('mysql:'+alias,readonly=True)
            try:
                self.assertEqual(con.dataset,name);self.assertEqual(con.requested_dataset,alias)
                rows=con.execute('SELECT * FROM fighters ORDER BY rowid').fetchall()
                self.assertEqual([tuple(r) for r in rows],[('fa','Alpha'),('fb','Beta')])
                self.assertEqual([r[0] for r in con.execute('SELECT rowid FROM fighters ORDER BY rowid')],[7,12])
                self.assertEqual([r['rowid'] for r in con.execute('SELECT b.rowid FROM fighters b ORDER BY b.rowid')],[7,12])
                self.assertNotIn(rt.ORDINAL,rows[0].keys())
                with self.assertRaises(rt.RuntimeDatabaseError):con.execute('INSERT INTO fighters VALUES(?,?)',('fc','Gamma'))
            finally:con.close()
            inspected=rt.inspect_dataset(alias)
            self.assertEqual(inspected['source_sha256'],original);self.assertEqual(inspected['resolved_name'],name)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),original)

    def test_snapshot_restores_new_native_namespace_with_binary_and_unchanged_fingerprint(self):
        original=self.name('snapshot');restored=self.name('restore')
        document=RuntimeContractTests.snapshot_fixture()
        first=rt.import_dataset_snapshot(document,original)
        exported=rt.export_dataset_snapshot(original)
        self.assertEqual(exported['content_sha256'],first['content_sha256'])
        second=rt.import_dataset_snapshot(exported,restored)
        self.assertEqual(first['content_sha256'],second['content_sha256'])
        self.assertEqual(second['source_sha256'],document['source_sha256'])
        con=rt.connect_dataset(restored,readonly=True)
        try:
            row=con.execute('SELECT rowid,receipt FROM children').fetchone()
            self.assertEqual(row['rowid'],4);self.assertEqual(row['receipt'],b'\x00\xff')
            self.assertEqual(con.foreign_key_violations(),[])
        finally:con.close()
        with self.assertRaisesRegex(rt.RuntimeDatabaseError,'unused'):rt.import_dataset_snapshot(exported,restored)

    def test_complete_statistics_schema_preserves_key_column_without_quoting_sql_keyword(self):
        name=self.name('statistics');con=rt.create_dataset(SCHEMA,name,'statistics')
        try:
            con.execute('INSERT INTO metadata VALUES(?,?)',('fixture','retained'))
            result=rt.finalize_dataset(con)
            self.assertEqual(result['counts']['metadata'],1)
            self.assertEqual(con.execute('SELECT value FROM metadata WHERE key=?',('fixture',)).fetchone()[0],'retained')
            count=con.execute("SELECT SUM(key='fixture') FROM metadata").fetchone()[0]
            self.assertIs(type(count),int);self.assertEqual(json.dumps({'count':count}),'{"count": 1}')
        finally:con.close()

    def test_foreign_key_check_detects_real_orphans_before_publication(self):
        name=self.name('orphan')
        con=rt.create_dataset('CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id));',name,'statistics')
        try:
            with con.raw.cursor() as c:
                c.execute('SET SESSION foreign_key_checks=0')
                c.execute('INSERT INTO '+rt._identifier(con.tables['child']['physical'])+'(id,parent_id) VALUES(1,999)')
                c.execute('SET SESSION foreign_key_checks=1')
            violations=con.execute('PRAGMA foreign_key_check').fetchall()
            self.assertEqual(tuple(violations[0]),('child',1,'parent',0))
            with self.assertRaises(rt.RuntimeIntegrityError):rt.finalize_dataset(con)
        finally:con.rollback();con.close()

    def test_upsert_cannot_update_row_from_unrelated_unique_key_collision(self):
        name=self.name('upsert');con=rt.create_dataset('CREATE TABLE items(id TEXT PRIMARY KEY,alias TEXT UNIQUE,value REAL);',name,'operating')
        try:
            con.execute('INSERT INTO items(id,alias,value) VALUES(?,?,?)',('one','unique',1.));rt.finalize_dataset(con)
            with self.assertRaises(rt.RuntimeIntegrityError):
                con.execute('INSERT INTO items(id,alias,value) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',('two','unique',99.))
            self.assertEqual(con.execute('SELECT id,value FROM items').fetchone()['value'],1.)
        finally:con.rollback();con.close()

    def test_scratch_operating_uses_no_persistent_namespace(self):
        from ufc_odds_model import db
        with rt.scratch_operating() as con:
            db.upsert_fighter(con,'temporary','Only this connection')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM fighters').fetchone()[0],1)
            self.assertTrue(con.scratch)
        raw=rt.connect_server(readonly=True)
        try:
            with raw.cursor() as c:
                c.execute('SELECT COUNT(*) FROM ufc_runtime_datasets WHERE dataset=%s',('scratch',));self.assertEqual(c.fetchone()[0],0)
        finally:raw.close()

    def test_native_clone_upgrades_only_the_new_version_and_preserves_original_rows(self):
        from ufc_odds_model import db
        source=self.research_source('old-schema',version_limit=14)
        before=rt.inspect_dataset(source);target=self.name('upgraded-clone')
        con=rt.clone_dataset('mysql:'+source,target,role='research',upgrade_operating_schema=True)
        try:
            db.require_current_schema(con)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0],15)
            self.assertEqual(con.execute('SELECT canonical_name FROM fighters WHERE fighter_id=?',('wikidata:Q42',)).fetchone()[0],'Example')
            db.upsert_fighter(con,'new-person','New version only')
            result=rt.finalize_dataset(con)
            self.assertIsNone(result['source_sha256'])
        finally:con.close()
        self.assertEqual(rt.inspect_dataset(source)['content_sha256'],before['content_sha256'])
        original=rt.connect_dataset(source,readonly=True)
        try:
            self.assertEqual(original.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0],14)
            self.assertEqual(original.execute('SELECT COUNT(*) FROM fighters').fetchone()[0],1)
        finally:original.close()
        self.assertEqual(rt.inspect_dataset(target)['metadata']['parent_content_sha256'],before['content_sha256'])

    def test_native_profile_copy_publishes_new_receipts_without_sqlite_backup(self):
        from ufc_odds_model.wikidata_profiles import import_profiles_copy
        source=self.research_source('profile-source');before=rt.inspect_dataset(source)['content_sha256']
        target=self.name('profile-output')
        raw=json.dumps({'results':{'bindings':[{'item':{'value':'http://www.wikidata.org/entity/Q42'},
            'dob':{'value':'1990-01-02T00:00:00Z'},'prec':{'value':'11'}}]}}).encode()
        with tempfile.TemporaryDirectory() as directory,patch('sqlite3.connect',side_effect=AssertionError('Native profile workflow must not stage SQLite')):
            result=import_profiles_copy('mysql:'+source,'mysql:'+target,raw_dir=Path(directory)/'raw',
                report_path=Path(directory)/'profiles.json',fetch=lambda request:raw)
        self.assertEqual(result['fighters_with_birth_date'],1)
        self.assertEqual(rt.inspect_dataset(source)['content_sha256'],before)
        con=rt.connect_dataset(target,readonly=True)
        try:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM fighter_static_attributes').fetchone()[0],2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM ingestion_runs WHERE source='wikidata_profiles'").fetchone()[0],1)
        finally:con.close()

    def test_native_completion_preserves_identity_receipts_and_never_stages_sqlite(self):
        from tests.test_research_complete import CompleteHistoryTests
        from ufc_odds_model.research_complete import complete_history
        fixture=CompleteHistoryTests('test_nfkc_names_and_redirect_chains');fixture.setUp();self.addCleanup(fixture.tearDown)
        source=self.name('complete-source');target=self.name('complete-output')
        rt.import_sqlite_snapshot(fixture.source,dataset=source,role='research')
        before=rt.inspect_dataset(source)['content_sha256']
        with patch('sqlite3.connect',side_effect=AssertionError('Native completion must not stage SQLite')):
            result=complete_history('mysql:'+source,'mysql:'+target,review_path=fixture.review,
                report_path=fixture.report,raw_dir=fixture.root/'native-raw')
        self.assertEqual(result['restored_bouts'],1);self.assertEqual(result['storage_backend'],'mysql')
        self.assertEqual(rt.inspect_dataset(source)['content_sha256'],before)
        con=rt.connect_dataset(target,readonly=True)
        try:
            row=con.execute('SELECT winner_fighter_id,method FROM results').fetchone()
            self.assertEqual(tuple(row),('wikipedia:42','TKO (punches)'))
            self.assertEqual(con.foreign_key_violations(),[])
        finally:con.close()


if __name__=='__main__':unittest.main()
