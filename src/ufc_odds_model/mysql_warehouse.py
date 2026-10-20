"""Publish a verified canonical statistics snapshot into a versioned MySQL warehouse.

MySQL is an optional analytical destination. This module never rewrites the
SQLite research/training stores, changes a model, or loads current aggregates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
PREFIX = 'ufc_wh_'
STAT_FIELDS = (
    'sig_landed','sig_attempted','total_landed','total_attempted','td_landed','td_attempted',
    'head_landed','head_attempted','body_landed','body_attempted','leg_landed','leg_attempted',
    'distance_landed','distance_attempted','clinch_landed','clinch_attempted','ground_landed',
    'ground_attempted','kd','sub_attempts','reversals','control_seconds',
)


class WarehouseError(RuntimeError):
    """A sanitized publication error safe to show in an operations log."""


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple
    primary: tuple
    foreign: tuple = ()
    indexes: tuple = ()

    def ddl(self):
        parts=[f'`{name}` {kind}' + ('' if nullable else ' NOT NULL') for name,kind,nullable in self.columns]
        parts.append('PRIMARY KEY ('+','.join('`'+k+'`' for k in self.primary)+')')
        for number,(cols,parent,target) in enumerate(self.foreign):
            parts.append('CONSTRAINT `'+self.name+'_fk'+str(number)+'` FOREIGN KEY ('+
                         ','.join('`'+k+'`' for k in cols)+') REFERENCES `'+parent+'` ('+
                         ','.join('`'+k+'`' for k in target)+')')
        for number,cols in enumerate(self.indexes):
            parts.append('KEY `idx'+str(number)+'` ('+','.join('`'+k+'`' for k in cols)+')')
        return ('CREATE TABLE IF NOT EXISTS `'+self.name+'` (\n  '+',\n  '.join(parts)+
                '\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin')


def _col(name,kind='varchar(128)',nullable=False):return name,kind,nullable


def _versioned(name,columns,primary,foreign=(),indexes=()):
    return Table(PREFIX+name,(_col('run_id','varchar(64) CHARACTER SET ascii COLLATE ascii_bin'),)+tuple(columns),
                 ('run_id',)+tuple(primary),
                 ((('run_id',),PREFIX+'load',('run_id',)),)+tuple(foreign),tuple(indexes))


def _fk(columns,parent,target):return tuple(columns),PREFIX+parent,tuple(target)


_STAT_COLUMNS=tuple(_col(key,'int unsigned',True) for key in STAT_FIELDS)
TABLES=(
    Table(PREFIX+'schema',(_col('singleton','tinyint unsigned'),_col('schema_version','int unsigned'),_col('ddl_sha256','char(64)')),('singleton',)),
    Table(PREFIX+'load',(_col('run_id','varchar(64) CHARACTER SET ascii COLLATE ascii_bin'),
        _col('source_sha256','char(64)'),_col('source_content_sha256','char(64)'),_col('source_manifest_sha256','char(64)'),
        _col('schema_version','int unsigned'),_col('status','varchar(16)'),_col('published_at_utc','datetime(6)'),
        _col('source_captured_at_utc','datetime(6)'),_col('counts_json','json'),_col('metadata_json','json')),('run_id',)),
    Table(PREFIX+'active',(_col('singleton','tinyint unsigned'),_col('run_id','varchar(64) CHARACTER SET ascii COLLATE ascii_bin',True)),
          ('singleton',),(_fk(('run_id',),'load',('run_id',)),)),
    _versioned('dim_fighter',(_col('fighter_id'),_col('name','varchar(255)'),_col('name_key','varchar(255)'),
        _col('source_url','varchar(1024)'),_col('reach_cm','double',True),_col('reach_display','varchar(64)',True)),('fighter_id',)),
    _versioned('dim_event',(_col('event_id'),_col('name','varchar(512)'),_col('event_date','date'),_col('source_url','varchar(1024)')),('event_id',),indexes=(('run_id','event_date'),)),
    _versioned('fact_bout',tuple([_col('bout_id'),_col('event_id',nullable=True),_col('event_date','date',True),
        _col('fighter_a_id',nullable=True),_col('fighter_b_id',nullable=True),_col('fighter_a_name','varchar(255)',True),
        _col('fighter_b_name','varchar(255)',True),_col('source_url','varchar(1024)'),_col('weight_class','varchar(255)',True),
        _col('scheduled_rounds','smallint unsigned',True),_col('finish_round','smallint unsigned',True),
        _col('finish_time','varchar(32)',True),_col('time_format','varchar(128)',True),_col('elapsed_seconds','int unsigned',True),
        _col('outcome','varchar(32)',True),_col('winner_fighter_id',nullable=True),_col('stats_status','varchar(16)'),
        _col('gap_reason','varchar(255)',True),_col('aliases_json','json')]),('bout_id',),
        (_fk(('run_id','event_id'),'dim_event',('run_id','event_id')),
         _fk(('run_id','fighter_a_id'),'dim_fighter',('run_id','fighter_id')),
         _fk(('run_id','fighter_b_id'),'dim_fighter',('run_id','fighter_id')),
         _fk(('run_id','winner_fighter_id'),'dim_fighter',('run_id','fighter_id'))),
        (('run_id','event_date'),('run_id','event_id'))),
    _versioned('fact_fighter_bout',(_col('bout_id'),_col('fighter_id'),_col('opponent_id'))+_STAT_COLUMNS,('bout_id','fighter_id'),
        (_fk(('run_id','bout_id'),'fact_bout',('run_id','bout_id')),
         _fk(('run_id','fighter_id'),'dim_fighter',('run_id','fighter_id')),
         _fk(('run_id','opponent_id'),'dim_fighter',('run_id','fighter_id'))),
        (('run_id','fighter_id','bout_id'),)),
    _versioned('fact_fighter_round',(_col('bout_id'),_col('fighter_id'),_col('round_number','smallint unsigned'),
        _col('elapsed_seconds','int unsigned',True),_col('source_row_number','int unsigned'),_col('source_errors_json','json'))+_STAT_COLUMNS,
        ('bout_id','fighter_id','round_number'),
        (_fk(('run_id','bout_id','fighter_id'),'fact_fighter_bout',('run_id','bout_id','fighter_id')),)),
    _versioned('research_fighter_link',(_col('research_fighter_id'),_col('fighter_id'),_col('reason','varchar(255)')),('research_fighter_id',),
        (_fk(('run_id','fighter_id'),'dim_fighter',('run_id','fighter_id')),)),
    _versioned('research_bout_link',(_col('research_bout_id','varchar(255)'),_col('bout_id'),_col('research_event_id'),_col('fighter_a_id'),_col('fighter_b_id')),('research_bout_id',),
        (_fk(('run_id','bout_id'),'fact_bout',('run_id','bout_id')),)),
    _versioned('research_event_link',(_col('research_event_id'),_col('event_id')),('research_event_id',),
        (_fk(('run_id','event_id'),'dim_event',('run_id','event_id')),)),
    _versioned('quality_gap',(_col('gap_id','int unsigned'),_col('bout_id',nullable=True),_col('field','varchar(128)'),
        _col('reason','varchar(255)'),_col('evidence_json','json')),('gap_id',),
        (_fk(('run_id','bout_id'),'fact_bout',('run_id','bout_id')),)),
    _versioned('research_hold',(_col('research_bout_id','varchar(255)'),_col('research_event_id'),_col('event_date','date'),_col('reason','varchar(255)')),('research_bout_id',)),
    _versioned('source_file',(_col('file','varchar(255)'),_col('sha256','char(64)'),_col('source_url','varchar(1024)'),_col('bytes','bigint unsigned')),('file',)),
)
VERSION_TABLES=TABLES[3:]
DDL_SHA256=hashlib.sha256('\n'.join(table.ddl() for table in TABLES).encode()).hexdigest()


def _view_sql():
    active=' JOIN ufc_wh_active a ON a.singleton=1 AND a.run_id='
    views={
        'ufc_active_publication': 'SELECT l.* FROM ufc_wh_load l'+active+'l.run_id WHERE l.status=\'published\'',
        'ufc_active_fighters':'SELECT f.* FROM ufc_wh_dim_fighter f'+active+'f.run_id',
        'ufc_active_events':'SELECT e.* FROM ufc_wh_dim_event e'+active+'e.run_id',
        'ufc_active_bouts':'SELECT b.* FROM ufc_wh_fact_bout b'+active+'b.run_id',
        'ufc_card_coverage': '''SELECT e.run_id,e.event_id,e.name,e.event_date,e.source_url,
          COUNT(b.bout_id) AS bouts,
          SUM(CASE WHEN b.stats_status='usable' THEN 1 ELSE 0 END) AS usable_stat_bouts,
          SUM(CASE WHEN b.stats_status='held' THEN 1 ELSE 0 END) AS held_stat_bouts,
          SUM(CASE WHEN b.stats_status='usable' AND b.elapsed_seconds IS NOT NULL THEN 1 ELSE 0 END) AS timed_stat_bouts,
          SUM(CASE WHEN b.stats_status='usable' AND fa.reach_cm IS NOT NULL AND fb.reach_cm IS NOT NULL THEN 1 ELSE 0 END) AS both_reach_stat_bouts
          FROM ufc_wh_dim_event e JOIN ufc_wh_active a ON a.singleton=1 AND a.run_id=e.run_id
          LEFT JOIN ufc_wh_fact_bout b ON b.run_id=e.run_id AND b.event_id=e.event_id
          LEFT JOIN ufc_wh_dim_fighter fa ON fa.run_id=b.run_id AND fa.fighter_id=b.fighter_a_id
          LEFT JOIN ufc_wh_dim_fighter fb ON fb.run_id=b.run_id AND fb.fighter_id=b.fighter_b_id
          GROUP BY e.run_id,e.event_id,e.name,e.event_date,e.source_url''',
        'ufc_fighter_history':'''SELECT f.run_id,f.fighter_id,p.name AS fighter_name,f.opponent_id,o.name AS opponent_name,
          b.bout_id,b.event_id,b.event_date,b.source_url,b.outcome,b.winner_fighter_id,b.elapsed_seconds,
          p.reach_cm AS retrospective_reach_cm,'''+','.join('f.`'+key+'`' for key in STAT_FIELDS)+''',
          opponent.sig_landed AS opponent_sig_landed,opponent.sig_attempted AS opponent_sig_attempted,
          opponent.td_landed AS opponent_td_landed,opponent.td_attempted AS opponent_td_attempted,
          60.0*f.sig_landed/NULLIF(b.elapsed_seconds,0) AS sig_landed_per_minute,
          60.0*opponent.sig_landed/NULLIF(b.elapsed_seconds,0) AS sig_absorbed_per_minute
          FROM ufc_wh_fact_fighter_bout f JOIN ufc_wh_active a ON a.singleton=1 AND a.run_id=f.run_id
          JOIN ufc_wh_fact_bout b ON b.run_id=f.run_id AND b.bout_id=f.bout_id
          JOIN ufc_wh_dim_fighter p ON p.run_id=f.run_id AND p.fighter_id=f.fighter_id
          JOIN ufc_wh_dim_fighter o ON o.run_id=f.run_id AND o.fighter_id=f.opponent_id
          JOIN ufc_wh_fact_fighter_bout opponent ON opponent.run_id=f.run_id AND opponent.bout_id=f.bout_id AND opponent.fighter_id=f.opponent_id''',
        'ufc_data_quality':'''SELECT q.run_id,'archive' AS gap_kind,CAST(q.gap_id AS CHAR) AS gap_id,
          q.bout_id,q.field,q.reason,q.evidence_json FROM ufc_wh_quality_gap q
          JOIN ufc_wh_active a ON a.singleton=1 AND a.run_id=q.run_id
          UNION ALL SELECT h.run_id,'research',h.research_bout_id,h.research_bout_id,'research_crosswalk',h.reason,NULL
          FROM ufc_wh_research_hold h JOIN ufc_wh_active a ON a.singleton=1 AND a.run_id=h.run_id''',
        'ufc_round_quality':'SELECT r.run_id,r.bout_id,r.fighter_id,r.round_number,r.source_row_number,r.source_errors_json FROM ufc_wh_fact_fighter_round r'+active+'r.run_id WHERE JSON_LENGTH(r.source_errors_json)>0',
        'ufc_source_provenance':'SELECT s.*,l.source_sha256 AS statistics_database_sha256,l.source_captured_at_utc,l.metadata_json FROM ufc_wh_source_file s'+active+'s.run_id JOIN ufc_wh_load l ON l.run_id=s.run_id',
    }
    return {name:'CREATE OR REPLACE SQL SECURITY INVOKER VIEW `'+name+'` AS '+query for name,query in views.items()}


def _hash(path):
    from .mysql_runtime import is_mysql_target
    if is_mysql_target(path):
        from .model_lab import file_sha256
        return file_sha256(path)
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def _json(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def _sha(value,label):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise WarehouseError('Invalid '+label+' SHA256')
    return value


def _datetime(value):
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None:raise ValueError()
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    except (AttributeError,TypeError,ValueError):raise WarehouseError('Invalid source UTC capture timestamp') from None


def _typed(value,kind,nullable,table,column):
    if value is None:
        if not nullable:raise WarehouseError('Missing required source field '+table+'.'+column)
        return None
    if kind=='json':
        try:return _json(json.loads(value)) if isinstance(value,str) else _json(value)
        except (ValueError,TypeError):raise WarehouseError('Invalid source JSON in '+table+'.'+column) from None
    if kind=='date':
        try:return date.fromisoformat(value)
        except (ValueError,TypeError):raise WarehouseError('Invalid source date in '+table+'.'+column) from None
    if kind.endswith(' unsigned'):
        limit=65535 if kind.startswith('smallint') else 4294967295 if kind.startswith('int ') else 18446744073709551615
        if not isinstance(value,int) or isinstance(value,bool) or not 0<=value<=limit:raise WarehouseError('Invalid unsigned source count in '+table+'.'+column)
        return value
    if kind=='double':
        if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or not 100<=value<=250:
            raise WarehouseError('Invalid source reach in '+table+'.'+column)
        return float(value)
    limit=re.match(r'(?:var)?char\(([0-9]+)\)',kind)
    if limit and (not isinstance(value,str) or len(value)>int(limit[1])):raise WarehouseError('Source text exceeds contract in '+table+'.'+column)
    return value


@dataclass(frozen=True)
class Snapshot:
    sha256: str
    metadata: dict
    rows: dict
    counts: dict


def read_snapshot(statistics_db,source_sha256=None):
    """Validate and materialize the read-only source before opening MySQL."""
    from .historical_stats import FIELDS,HistoryStore
    from .mysql_runtime import is_mysql_target
    mysql=is_mysql_target(statistics_db)
    path=statistics_db if mysql else Path(statistics_db).resolve()
    if not mysql and not path.is_file():raise WarehouseError('Canonical statistics database does not exist')
    digest=_hash(path)
    if source_sha256 is not None and _sha(source_sha256,'source')!=digest:raise WarehouseError('Statistics database SHA256 does not match the requested source')
    if set(FIELDS)!=set(STAT_FIELDS):raise WarehouseError('Canonical statistics fields differ from warehouse schema')
    try:
        with HistoryStore(path) as store:
            source=store.connection;source.execute('BEGIN')
            metadata=dict(store.metadata)
            for key in ('store_content_sha256','source_manifest_sha256'):_sha(metadata.get(key),key)
            _datetime(metadata.get('captured_at_utc'))
            if source.execute('PRAGMA foreign_key_check').fetchone():raise WarehouseError('Canonical source has invalid foreign keys')
            mapping={'dim_fighter':'fighters','dim_event':'events','fact_bout':'bouts','research_fighter_link':'research_fighter_links',
                     'research_bout_link':'research_bout_links','research_event_link':'research_event_links','quality_gap':'gaps',
                     'research_hold':'research_holds','source_file':'source_files'}
            rows={}
            for table in VERSION_TABLES:
                short=table.name.removeprefix(PREFIX)
                if short not in mapping:continue
                columns=[col[0] for col in table.columns[1:]]
                actual={r[1] for r in source.execute('PRAGMA table_info("'+mapping[short]+'")')}
                if not set(columns)<=actual:raise WarehouseError('Canonical source table contract changed: '+mapping[short])
                rows[table.name]=[dict(row) for row in source.execute('SELECT '+','.join('"'+c+'"' for c in columns)+' FROM "'+mapping[short]+'" ORDER BY '+','.join('"'+c+'"' for c in table.primary[1:]))]
            facts=[]
            for row in source.execute('SELECT * FROM fighter_bout_facts ORDER BY bout_id,fighter_id'):
                totals=json.loads(row['totals_json'])
                if set(totals)!=set(STAT_FIELDS):raise WarehouseError('Fighter-bout statistic fields differ from warehouse contract')
                facts.append({'bout_id':row['bout_id'],'fighter_id':row['fighter_id'],'opponent_id':row['opponent_id'],**totals})
            rows[PREFIX+'fact_fighter_bout']=facts
            rounds=[];normalized_controls=0
            for row in source.execute('''SELECT f.*,s.values_json,s.errors_json FROM round_facts f
                    JOIN source_round_rows s ON s.row_number=f.source_row_number ORDER BY f.bout_id,f.fighter_id,f.round_number'''):
                values=json.loads(row['values_json'])
                if set(values)!=set(STAT_FIELDS):raise WarehouseError('Round statistic fields differ from warehouse contract')
                errors=json.loads(row['errors_json'])
                if not isinstance(errors,list):raise WarehouseError('Invalid round source-error contract')
                if values['control_seconds'] is not None and row['elapsed_seconds'] is not None and values['control_seconds']>row['elapsed_seconds']:
                    values['control_seconds']=None
                    errors.append('warehouse:control_exceeds_validated_round_duration')
                    normalized_controls+=1
                rounds.append({k:row[k] for k in ('bout_id','fighter_id','round_number','elapsed_seconds','source_row_number')}|
                              {'source_errors_json':_json(errors)}|values)
            rows[PREFIX+'fact_fighter_round']=rounds
            _validate_facts(rows)
            typed={table.name:[tuple(_typed(row[key],kind,nullable,table.name,key) for key,kind,nullable in table.columns[1:])
                               for row in rows[table.name]] for table in VERSION_TABLES}
            if _hash(path)!=digest:raise WarehouseError('Canonical statistics database changed while reading')
            counts={table.name.removeprefix(PREFIX):len(typed[table.name]) for table in VERSION_TABLES}
            counts.update({'fighters':counts['dim_fighter'],'events':counts['dim_event'],'bouts':counts['fact_bout'],
                           'fighter_bout_facts':counts['fact_fighter_bout'],'fighter_round_facts':counts['fact_fighter_round'],
                           'normalized_round_controls':normalized_controls})
            return Snapshot(digest,metadata,typed,counts)
    except WarehouseError:raise
    except Exception:raise WarehouseError('Canonical statistics validation failed; source is incomplete or incompatible') from None


def _validate_facts(rows):
    bouts={r['bout_id']:r for r in rows[PREFIX+'fact_bout']}
    events={r['event_id']:r for r in rows[PREFIX+'dim_event']}
    fighters={r['fighter_id'] for r in rows[PREFIX+'dim_fighter']}
    for bout in bouts.values():
        if bout['stats_status'] not in ('usable','held'):raise WarehouseError('Unknown source statistics status')
        if bout['event_id'] is not None and (bout['event_id'] not in events or bout['event_date']!=events[bout['event_id']]['event_date']):
            raise WarehouseError('Canonical bout/event date contract failed')
        for key in ('fighter_a_id','fighter_b_id','winner_fighter_id'):
            if bout[key] is not None and bout[key] not in fighters:raise WarehouseError('Canonical bout references an unknown fighter')
    groups=defaultdict(list);rounds=defaultdict(list)
    for row in rows[PREFIX+'fact_fighter_round']:
        rounds[row['bout_id'],row['fighter_id']].append(row)
    for row in rows[PREFIX+'fact_fighter_bout']:
        bout=bouts.get(row['bout_id'])
        if (not bout or bout['stats_status']!='usable' or row['fighter_id']==row['opponent_id']
                or {row['fighter_id'],row['opponent_id']}!={bout['fighter_a_id'],bout['fighter_b_id']}):
            raise WarehouseError('Canonical paired fighter-bout identity contract failed')
        groups[row['bout_id']].append(row)
        own=rounds.pop((row['bout_id'],row['fighter_id']),[])
        if [r['round_number'] for r in own]!=list(range(1,(bout['finish_round'] or 0)+1)):
            raise WarehouseError('Canonical round coverage is incomplete')
        for field in STAT_FIELDS:
            values=[r[field] for r in own]
            total=sum(values) if all(v is not None for v in values) else None
            if total!=row[field]:raise WarehouseError('Canonical round-to-bout totals do not reconcile')
        for prefix in ('sig','total','td','head','body','leg','distance','clinch','ground'):
            landed,attempted=row[prefix+'_landed'],row[prefix+'_attempted']
            if landed is not None and attempted is not None and landed>attempted:raise WarehouseError('Canonical landed count exceeds attempts')
        if row['control_seconds'] is not None and bout['elapsed_seconds'] is not None and row['control_seconds']>bout['elapsed_seconds']:
            raise WarehouseError('Canonical control exceeds bout duration')
    if rounds or any(len(groups[bid])!=2 for bid,b in bouts.items() if b['stats_status']=='usable'):
        raise WarehouseError('Canonical paired-bout coverage is incomplete')


def _configuration():
    for name in ('UFC_MYSQL_USER','UFC_MYSQL_PASSWORD','UFC_MYSQL_DATABASE'):
        if name not in os.environ:raise WarehouseError('Missing '+name+' environment variable')
    database=os.environ['UFC_MYSQL_DATABASE']
    if not re.fullmatch('[A-Za-z0-9_]{1,64}',database):raise WarehouseError('UFC_MYSQL_DATABASE must be a project database identifier')
    if database.lower() in {'mysql','sys','information_schema','performance_schema'}:raise WarehouseError('A dedicated project database is required')
    try:port=int(os.environ.get('UFC_MYSQL_PORT','3306'))
    except ValueError:raise WarehouseError('Invalid UFC_MYSQL_PORT') from None
    if not 1<=port<=65535:raise WarehouseError('Invalid UFC_MYSQL_PORT')
    if not os.environ['UFC_MYSQL_USER']:raise WarehouseError('UFC_MYSQL_USER cannot be empty')
    return {'host':os.environ.get('UFC_MYSQL_HOST','127.0.0.1'),'port':port,'user':os.environ['UFC_MYSQL_USER'],
            'password':os.environ['UFC_MYSQL_PASSWORD'],'database':database,'charset':'utf8mb4',
            'autocommit':False,'connect_timeout':10,'read_timeout':120,'write_timeout':120,'local_infile':False}


def _connect(connector):
    if connector is None:
        from .mysql_runtime import connect_server
        try:return connect_server()
        except Exception:raise WarehouseError('Cannot connect to the project MySQL database; check its private configuration') from None
    config=_configuration()
    try:return connector(**config)
    except Exception:raise WarehouseError('Cannot connect to the project MySQL database; check UFC_MYSQL_* configuration') from None


def _normalize_type(kind):
    return kind.lower().split(' character set ')[0]


def _ensure_schema(connection):
    """DDL occurs before the data transaction; existing table contracts must match."""
    with connection.cursor() as cursor:
        for table in TABLES:cursor.execute(table.ddl())
        cursor.execute('SELECT schema_version,ddl_sha256 FROM ufc_wh_schema WHERE singleton=1')
        existing=cursor.fetchone()
        if existing and tuple(existing)!=(SCHEMA_VERSION,DDL_SHA256):raise WarehouseError('Warehouse schema version or DDL contract differs; use an explicit migration')
        cursor.execute("SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'ufc\\_wh\\_%'")
        actual=defaultdict(dict)
        for table,column,kind,nullable in cursor.fetchall():actual[table][column]=(_normalize_type(kind),nullable=='YES')
        cursor.execute("SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'ufc\\_wh\\_%'")
        engines=dict(cursor.fetchall())
        cursor.execute("SELECT TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND CONSTRAINT_NAME='PRIMARY' AND TABLE_NAME LIKE 'ufc\\_wh\\_%' ORDER BY TABLE_NAME,ORDINAL_POSITION")
        primary=defaultdict(list)
        for table,column,_ in cursor.fetchall():primary[table].append(column)
        for table in TABLES:
            expected={key:(_normalize_type(kind),nullable) for key,kind,nullable in table.columns}
            if actual[table.name]!=expected or tuple(primary[table.name])!=table.primary or engines.get(table.name)!='InnoDB':
                raise WarehouseError('Warehouse table/type/key/engine contract differs: '+table.name)
        cursor.execute("SELECT TABLE_NAME,CONSTRAINT_NAME,COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME,ORDINAL_POSITION FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'ufc\\_wh\\_%' ORDER BY TABLE_NAME,CONSTRAINT_NAME,ORDINAL_POSITION")
        foreign=defaultdict(lambda:defaultdict(list))
        for table,name,column,parent,target,_ in cursor.fetchall():foreign[table][name].append((column,parent,target))
        for table in TABLES:
            expected=sorted(tuple(zip(cols,[parent]*len(cols),target)) for cols,parent,target in table.foreign)
            actual_fk=sorted(tuple(fields) for fields in foreign[table.name].values())
            if actual_fk!=expected:raise WarehouseError('Warehouse foreign-key contract differs: '+table.name)
        if not existing:cursor.execute('INSERT INTO ufc_wh_schema(singleton,schema_version,ddl_sha256) VALUES(1,%s,%s)',(SCHEMA_VERSION,DDL_SHA256))
        cursor.execute('INSERT IGNORE INTO ufc_wh_active(singleton,run_id) VALUES(1,NULL)')
    connection.commit()
    with connection.cursor() as cursor:
        for query in _view_sql().values():cursor.execute(query)
    connection.commit()


def _destination_counts(cursor,run_id):
    result={}
    for table in VERSION_TABLES:
        cursor.execute('SELECT COUNT(*) FROM `'+table.name+'` WHERE run_id=%s',(run_id,))
        result[table.name.removeprefix(PREFIX)]=cursor.fetchone()[0]
    return result


def _publish(connection,snapshot,run_id):
    connection.begin()
    commit_started=False
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT run_id FROM ufc_wh_active WHERE singleton=1 FOR UPDATE')
            if cursor.fetchone() is None:raise WarehouseError('Warehouse active pointer is missing')
            cursor.execute('SELECT source_sha256,status,counts_json FROM ufc_wh_load WHERE run_id=%s',(run_id,))
            existing=cursor.fetchone()
            if existing:
                if existing[0]!=snapshot.sha256 or existing[1]!='published':raise WarehouseError('Warehouse run_id already identifies a different or incomplete publication')
                expected={table.name.removeprefix(PREFIX):len(snapshot.rows[table.name]) for table in VERSION_TABLES}
                if _destination_counts(cursor,run_id)!=expected:raise WarehouseError('Previously published warehouse row counts no longer match the verified source')
                status='already_published'
            else:
                metadata=snapshot.metadata
                cursor.execute('''INSERT INTO ufc_wh_load(run_id,source_sha256,source_content_sha256,source_manifest_sha256,
                    schema_version,status,published_at_utc,source_captured_at_utc,counts_json,metadata_json)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (run_id,snapshot.sha256,metadata['store_content_sha256'],metadata['source_manifest_sha256'],SCHEMA_VERSION,
                     'loading',datetime.now(timezone.utc).replace(tzinfo=None),_datetime(metadata['captured_at_utc']),_json(snapshot.counts),_json(metadata)))
                for table in VERSION_TABLES:
                    columns=','.join('`'+key+'`' for key,_,_ in table.columns)
                    query='INSERT INTO `'+table.name+'` ('+columns+') VALUES ('+','.join(['%s']*len(table.columns))+')'
                    values=snapshot.rows[table.name]
                    for start in range(0,len(values),500):cursor.executemany(query,[(run_id,)+tuple(row) for row in values[start:start+500]])
                expected={table.name.removeprefix(PREFIX):len(snapshot.rows[table.name]) for table in VERSION_TABLES}
                if _destination_counts(cursor,run_id)!=expected:raise WarehouseError('MySQL loaded row counts differ from the validated source')
                cursor.execute("UPDATE ufc_wh_load SET status='published' WHERE run_id=%s",(run_id,))
                status='published'
            cursor.execute('UPDATE ufc_wh_active SET run_id=%s WHERE singleton=1',(run_id,))
        commit_started=True
        connection.commit()
        return {'status':status,'run_id':run_id,'active_run_id':run_id,'source_sha256':snapshot.sha256,
                'schema_version':SCHEMA_VERSION,'counts':snapshot.counts,'research_only':True}
    except BaseException:
        try:connection.rollback()
        except Exception:pass
        if commit_started:
            raise WarehouseError('MySQL commit acknowledgment is uncertain; replay the same run_id to verify the active version') from None
        raise


def publish_statistics(statistics_db:Path,*,source_sha256:str|None=None,run_id:str|None=None,connector=None)->dict:
    """Publish one source version atomically; same run and source replay safely.

    ``connector`` is a DB-API compatible connection factory used by tests and
    deployments. It receives the same environment-derived PyMySQL kwargs.
    """
    snapshot=read_snapshot(statistics_db,source_sha256)
    run_id=run_id or 'stats_'+snapshot.sha256[:40]
    if not isinstance(run_id,str) or not re.fullmatch('[A-Za-z0-9_.-]{1,64}',run_id):raise WarehouseError('run_id must contain 1-64 letters, digits, dots, hyphens or underscores')
    connection=_connect(connector)
    locked=False
    try:
        database=getattr(connection,'db',None)
        if isinstance(database,bytes):database=database.decode('utf-8')
        if not isinstance(database,str):database=os.environ.get('UFC_MYSQL_DATABASE')
        if not isinstance(database,str) or not re.fullmatch('[A-Za-z0-9_]{1,64}',database):
            raise WarehouseError('Connected MySQL project database identity is unavailable')
        lock_name='ufc_wh:'+hashlib.sha256(database.encode()).hexdigest()[:40]
        with connection.cursor() as cursor:
            cursor.execute('SELECT GET_LOCK(%s,30)',(lock_name,))
            if cursor.fetchone()!=(1,):raise WarehouseError('Another warehouse publisher holds the project database lock')
            locked=True
        _ensure_schema(connection)
        return _publish(connection,snapshot,run_id)
    except WarehouseError:raise
    except Exception:raise WarehouseError('MySQL publication failed; the previous active statistics snapshot is retained') from None
    finally:
        if locked:
            try:
                with connection.cursor() as cursor:cursor.execute('SELECT RELEASE_LOCK(%s)',(lock_name,))
            except Exception:pass  # Closing the session also releases its named lock.
        try:connection.close()
        except Exception:pass


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    publish=commands.add_parser('publish',help='Publish a verified statistics snapshot; existing versions are retained')
    publish.add_argument('--statistics-db',type=Path,required=True)
    publish.add_argument('--source-sha256');publish.add_argument('--run-id')
    args=parser.parse_args(argv)
    try:result=publish_statistics(args.statistics_db,source_sha256=args.source_sha256,run_id=args.run_id)
    except (WarehouseError,OSError):
        # Environment/database exceptions can contain credentials or SQL data.
        # Only WarehouseError messages are generated from fixed safe templates.
        error=sys.exc_info()[1]
        print(str(error) if isinstance(error,WarehouseError) else 'Cannot read the statistics source',file=sys.stderr)
        return 1
    print(_json(result))
    return 0


if __name__=='__main__':raise SystemExit(main())
