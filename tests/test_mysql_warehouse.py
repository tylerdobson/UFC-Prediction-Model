"""Source contracts and atomic, idempotent warehouse publication."""
import copy
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from tests.test_historical_stats import fixture
from ufc_odds_model.historical_stats import build_history,_content_hash
from ufc_odds_model import mysql_warehouse as wh


ENV={'UFC_MYSQL_HOST':'127.0.0.1','UFC_MYSQL_PORT':'3307','UFC_MYSQL_USER':'fixture_user',
     'UFC_MYSQL_PASSWORD':'fixture-password-never-print','UFC_MYSQL_DATABASE':'ufc_fixture'}


class FakeConnection:
    """Transactional DB-API recorder; schema SQL is tested against real MySQL below."""
    def __init__(self):
        self.active='previous';self.loads={};self.rows={};self.backup=None
        self.fail_table=None;self.closed=False;self.rollback_count=0;self.lock_released=False
    def cursor(self):return FakeCursor(self)
    def begin(self):self.backup=copy.deepcopy((self.active,self.loads,self.rows))
    def commit(self):self.backup=None
    def rollback(self):
        self.rollback_count+=1
        if self.backup is not None:self.active,self.loads,self.rows=copy.deepcopy(self.backup)
        self.backup=None
    def close(self):self.closed=True


class FakeCursor:
    def __init__(self,connection):self.connection=connection;self.result=None
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def fetchone(self):return self.result
    def execute(self,sql,args=()):
        c=self.connection
        if sql.startswith('SELECT GET_LOCK'):self.result=(1,)
        elif sql.startswith('SELECT RELEASE_LOCK'):c.lock_released=True;self.result=(1,)
        elif sql.startswith('SELECT run_id FROM ufc_wh_active'):self.result=(c.active,)
        elif sql.startswith('SELECT source_sha256,status,counts_json'):self.result=c.loads.get(args[0])
        elif sql.startswith('INSERT INTO ufc_wh_load'):c.loads[args[0]]=(args[1],args[5],args[8])
        elif sql.startswith('SELECT COUNT(*)'):
            table=re.search(r'FROM `([^`]+)`',sql)[1];self.result=(len(c.rows.get((table,args[0]),[])),)
        elif sql.startswith('UPDATE ufc_wh_load'):
            source,_,counts=c.loads[args[0]];c.loads[args[0]]=(source,'published',counts)
        elif sql.startswith('UPDATE ufc_wh_active'):c.active=args[0]
        else:raise AssertionError('Unexpected warehouse SQL')
    def executemany(self,sql,values):
        table=re.search(r'INSERT INTO `([^`]+)`',sql)[1]
        if table==self.connection.fail_table:raise RuntimeError('database failure containing '+ENV['UFC_MYSQL_PASSWORD'])
        for row in values:self.connection.rows.setdefault((table,row[0]),[]).append(row)


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.manifest,self.research=fixture(self.root);self.source=self.root/'statistics.sqlite'
        build_history(self.manifest,self.source,self.research)
        self.env=patch.dict(os.environ,ENV,clear=True);self.env.start();self.addCleanup(self.env.stop)

    def publish(self,connection,run_id='fixture-run',**kwargs):
        with patch.object(wh,'_ensure_schema'):
            return wh.publish_statistics(self.source,run_id=run_id,connector=lambda **options:connection,**kwargs)

    def test_private_config_connection_does_not_require_exported_database_variable(self):
        connection=FakeConnection();connection.db=b'ufc_private_configuration'
        os.environ.pop('UFC_MYSQL_DATABASE',None)
        with patch.object(wh,'_connect',return_value=connection),patch.object(wh,'_ensure_schema'):
            first=wh.publish_statistics(self.source,run_id='private-config')
            second=wh.publish_statistics(self.source,run_id='private-config')
        self.assertEqual(first['status'],'published')
        self.assertEqual(second['status'],'already_published')
        self.assertTrue(connection.closed)

    def test_read_only_snapshot_preserves_nulls_grains_and_provenance(self):
        before=self.source.read_bytes();snapshot=wh.read_snapshot(self.source)
        self.assertEqual(snapshot.counts['fighters'],3)
        self.assertEqual(snapshot.counts['bouts'],3)
        self.assertEqual(snapshot.counts['fighter_bout_facts'],6)
        self.assertEqual(snapshot.counts['fighter_round_facts'],8)
        table=next(t for t in wh.TABLES if t.name=='ufc_wh_fact_fighter_bout')
        control=[c[0] for c in table.columns[1:]].index('control_seconds')
        self.assertTrue(any(row[control] is None for row in snapshot.rows[table.name]))
        self.assertEqual(snapshot.metadata['current_career_averages_used'],False)
        self.assertEqual(self.source.read_bytes(),before)

    def test_wrong_hash_and_changed_content_fail_before_mysql_connection(self):
        connector=unittest.mock.Mock()
        with self.assertRaisesRegex(wh.WarehouseError,'SHA256'):
            wh.publish_statistics(self.source,source_sha256='0'*64,connector=connector)
        connector.assert_not_called()
        with sqlite3.connect(self.source) as connection:connection.execute("UPDATE fighters SET name='tampered'")
        with self.assertRaisesRegex(wh.WarehouseError,'validation failed'):
            wh.publish_statistics(self.source,connector=connector)
        connector.assert_not_called()

    def test_round_to_bout_reconciliation_detects_content_valid_but_inconsistent_source(self):
        with sqlite3.connect(self.source) as connection:
            row=connection.execute('SELECT bout_id,fighter_id,totals_json FROM fighter_bout_facts LIMIT 1').fetchone()
            value=json.loads(row[2]);value['sig_landed']+=1
            connection.execute('UPDATE fighter_bout_facts SET totals_json=? WHERE bout_id=? AND fighter_id=?',(json.dumps(value),row[0],row[1]))
            connection.execute("UPDATE metadata SET value=? WHERE key='store_content_sha256'",(json.dumps(_content_hash(connection)),))
        with self.assertRaisesRegex(wh.WarehouseError,'reconcile'):wh.read_snapshot(self.source)

    def test_impossible_raw_control_is_null_with_explicit_reason(self):
        with sqlite3.connect(self.source) as connection:
            row=connection.execute("SELECT row_number,values_json FROM source_round_rows WHERE fighter_name='Alpha One' AND round_number=1 ORDER BY row_number LIMIT 1").fetchone()
            values=json.loads(row[1]);values['control_seconds']=999
            connection.execute('UPDATE source_round_rows SET values_json=? WHERE row_number=?',(json.dumps(values),row[0]))
            connection.execute("UPDATE metadata SET value=? WHERE key='store_content_sha256'",(json.dumps(_content_hash(connection)),))
        snapshot=wh.read_snapshot(self.source)
        self.assertEqual(snapshot.counts['normalized_round_controls'],1)
        table=next(t for t in wh.TABLES if t.name=='ufc_wh_fact_fighter_round');columns=[x[0] for x in table.columns[1:]]
        rows=[dict(zip(columns,row)) for row in snapshot.rows[table.name]]
        flagged=[r for r in rows if 'warehouse:control_exceeds_validated_round_duration' in json.loads(r['source_errors_json'])]
        self.assertEqual(len(flagged),1);self.assertIsNone(flagged[0]['control_seconds'])

    def test_publish_and_replay_do_not_duplicate_rows(self):
        connection=FakeConnection();first=self.publish(connection)
        self.assertEqual(first['status'],'published');self.assertEqual(connection.active,'fixture-run')
        counts=copy.deepcopy(connection.rows)
        second=self.publish(connection)
        self.assertEqual(second['status'],'already_published');self.assertEqual(connection.rows,counts)
        self.assertTrue(connection.lock_released)

    def test_failed_insert_rolls_back_new_version_and_keeps_active(self):
        connection=FakeConnection();self.publish(connection,'baseline');before=copy.deepcopy(connection.rows)
        connection.fail_table='ufc_wh_fact_fighter_round'
        with self.assertRaisesRegex(wh.WarehouseError,'previous active') as error:self.publish(connection,'failing')
        self.assertNotIn(ENV['UFC_MYSQL_PASSWORD'],str(error.exception))
        self.assertEqual(connection.active,'baseline');self.assertEqual(connection.rows,before)
        self.assertNotIn('failing',connection.loads);self.assertEqual(connection.rollback_count,1)
        self.assertTrue(connection.closed);self.assertTrue(connection.lock_released)

    def test_reused_run_id_rejects_other_source_and_row_loss(self):
        connection=FakeConnection();self.publish(connection)
        source,_,counts=connection.loads['fixture-run'];connection.loads['fixture-run']=('0'*64,'published',counts)
        with self.assertRaisesRegex(wh.WarehouseError,'different'):self.publish(connection)
        connection.loads['fixture-run']=(source,'published',counts)
        connection.rows['ufc_wh_dim_fighter','fixture-run'].pop()
        with self.assertRaisesRegex(wh.WarehouseError,'row counts'):self.publish(connection)

    def test_lost_commit_ack_is_reported_as_uncertain_and_replay_resolves(self):
        connection=FakeConnection();original=connection.commit
        def uncertain():
            original()  # Server commits the complete version before the socket fails.
            raise RuntimeError('lost acknowledgment with '+ENV['UFC_MYSQL_PASSWORD'])
        connection.commit=uncertain
        with self.assertRaisesRegex(wh.WarehouseError,'acknowledgment is uncertain') as error:self.publish(connection)
        self.assertNotIn(ENV['UFC_MYSQL_PASSWORD'],str(error.exception))
        self.assertEqual(connection.active,'fixture-run')
        connection.commit=original
        result=self.publish(connection)
        self.assertEqual(result['status'],'already_published')
        self.assertEqual(len(connection.rows['ufc_wh_dim_fighter','fixture-run']),3)

    def test_configuration_and_connect_errors_never_expose_password(self):
        def failing(**options):
            self.assertFalse(options['autocommit']);self.assertFalse(options['local_infile'])
            raise RuntimeError('connection rejected '+options['password'])
        with self.assertRaises(wh.WarehouseError) as error:wh.publish_statistics(self.source,connector=failing)
        self.assertNotIn(ENV['UFC_MYSQL_PASSWORD'],str(error.exception))
        os.environ['UFC_MYSQL_DATABASE']='mysql'
        with self.assertRaisesRegex(wh.WarehouseError,'dedicated project'):wh._configuration()
        os.environ['UFC_MYSQL_DATABASE']='ufc_fixture';os.environ['UFC_MYSQL_PORT']='bad'
        with self.assertRaisesRegex(wh.WarehouseError,'PORT'):wh._configuration()

    def test_cli_redacts_untrusted_connection_error(self):
        with patch.object(wh,'_connect',side_effect=wh.WarehouseError('Cannot connect to the project MySQL database')),redirect_stderr(io.StringIO()) as output:
            self.assertEqual(wh.main(['publish','--statistics-db',str(self.source)]),1)
        self.assertNotIn(ENV['UFC_MYSQL_PASSWORD'],output.getvalue())

    def test_all_fact_keys_are_version_scoped_and_ddl_is_nondestructive(self):
        for table in wh.VERSION_TABLES:
            self.assertEqual(table.primary[0],'run_id')
            self.assertTrue(all(columns[0]=='run_id' and target[0]=='run_id' for columns,_,target in table.foreign))
            self.assertIn('ENGINE=InnoDB',table.ddl())
            self.assertNotIn('DROP ',table.ddl());self.assertNotIn('DELETE ',table.ddl())
        self.assertIn('ufc_fighter_history',wh._view_sql());self.assertIn('ufc_card_coverage',wh._view_sql())


class FailingCursor:
    def __init__(self,cursor):self.cursor=cursor
    def __enter__(self):self.cursor.__enter__();return self
    def __exit__(self,*args):return self.cursor.__exit__(*args)
    def __getattr__(self,name):return getattr(self.cursor,name)
    def executemany(self,sql,args):
        if 'INSERT INTO `ufc_wh_fact_fighter_round`' in sql:raise RuntimeError('injected mid-load failure')
        return self.cursor.executemany(sql,args)


class FailingConnection:
    def __init__(self,connection):self.connection=connection
    def __getattr__(self,name):return getattr(self.connection,name)
    def cursor(self):return FailingCursor(self.connection.cursor())


@unittest.skipUnless(os.environ.get('UFC_MYSQL_INTEGRATION')=='1','Set UFC_MYSQL_INTEGRATION=1 with an isolated project MySQL database')
class RealMySQLWarehouseTests(unittest.TestCase):
    def test_publication_replay_sql_constraints_and_midload_rollback(self):
        import pymysql
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        manifest,research=fixture(root);source=root/'statistics.sqlite';build_history(manifest,source,research)
        run='test_'+uuid.uuid4().hex;failed=run+'_fail'
        connection=wh._connect(None)
        try:
            wh._ensure_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute('SELECT run_id FROM ufc_wh_active WHERE singleton=1');previous=cursor.fetchone()[0]
            connection.rollback()  # Release metadata locks before another connection's schema check.
            try:
                first=wh.publish_statistics(source,run_id=run)
                second=wh.publish_statistics(source,run_id=run)
                self.assertEqual(first['status'],'published');self.assertEqual(second['status'],'already_published')
                with connection.cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) FROM ufc_active_bouts');self.assertEqual(cursor.fetchone()[0],3)
                    cursor.execute('SELECT COUNT(*) FROM ufc_fighter_history');self.assertEqual(cursor.fetchone()[0],6)
                    cursor.execute('SELECT SUM(bouts),SUM(usable_stat_bouts),SUM(timed_stat_bouts) FROM ufc_card_coverage')
                    self.assertEqual(tuple(map(int,cursor.fetchone())),(3,3,2))
                    cursor.execute('SELECT COUNT(*) FROM ufc_wh_fact_fighter_bout WHERE run_id=%s AND control_seconds IS NULL',(run,))
                    self.assertGreater(cursor.fetchone()[0],0)
                    cursor.execute('SELECT COUNT(*) FROM ufc_source_provenance');self.assertEqual(cursor.fetchone()[0],4)
                connection.rollback()  # End repeatable-read snapshot before another publisher.
                with self.assertRaisesRegex(wh.WarehouseError,'previous active'):
                    wh.publish_statistics(source,run_id=failed,connector=lambda **kwargs:FailingConnection(pymysql.connect(**kwargs)))
                with connection.cursor() as cursor:
                    cursor.execute('SELECT run_id FROM ufc_wh_active WHERE singleton=1');self.assertEqual(cursor.fetchone()[0],run)
                    cursor.execute('SELECT COUNT(*) FROM ufc_wh_load WHERE run_id=%s',(failed,));self.assertEqual(cursor.fetchone()[0],0)
                    cursor.execute('SELECT COUNT(*) FROM ufc_wh_dim_fighter WHERE run_id=%s',(failed,));self.assertEqual(cursor.fetchone()[0],0)
                    cursor.execute('SELECT COUNT(*) FROM ufc_active_bouts');self.assertEqual(cursor.fetchone()[0],3)
                    with self.assertRaises(pymysql.err.IntegrityError):
                        cursor.execute('INSERT INTO ufc_wh_fact_fighter_bout(run_id,bout_id,fighter_id,opponent_id) VALUES(%s,%s,%s,%s)',(run,'missing','missing','missing'))
                connection.rollback()
            finally:
                with connection.cursor() as cursor:cursor.execute('UPDATE ufc_wh_active SET run_id=%s WHERE singleton=1',(previous,))
                connection.commit()
                # Fixture versions are test data; remove only these exact run IDs.
                with connection.cursor() as cursor:
                    for table in reversed(wh.VERSION_TABLES):cursor.execute('DELETE FROM `'+table.name+'` WHERE run_id IN(%s,%s)',(run,failed))
                    cursor.execute('DELETE FROM ufc_wh_load WHERE run_id IN(%s,%s)',(run,failed))
                connection.commit()
        finally:connection.close()


if __name__=='__main__':unittest.main()
