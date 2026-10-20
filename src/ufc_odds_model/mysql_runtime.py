"""MySQL-first dataset storage with explicit, immutable SQLite import boundaries.

Dataset handles are ``mysql:NAME``. Each dataset has its own physical table
namespace in the project database. A small DB-API facade preserves the existing
application's named/indexed row contract and supported parameterized SQL forms;
unsupported SQLite operations fail rather than falling back to another store.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import quote

CATALOG='ufc_runtime_datasets'
TABLE_CATALOG='ufc_runtime_tables'
ALIAS_CATALOG='ufc_runtime_aliases'
ORDINAL='_ufc_row_ordinal'
_NAME=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
_IDENT=re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


class RuntimeDatabaseError(sqlite3.DatabaseError):
    """Portable, secret-safe database failure; compatible with legacy catch sites."""


class RuntimeIntegrityError(RuntimeDatabaseError,sqlite3.IntegrityError):pass


def is_mysql_target(value):
    if isinstance(value,Connection):return True
    if value is None:return False
    return bool(re.fullmatch(r'mysql:[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',str(value).replace('\\','/').rsplit('/',1)[-1]))


def dataset_name(value='operating'):
    if isinstance(value,Connection):return value.dataset
    text=str(value)
    if is_mysql_target(text):text=text.replace('\\','/').rsplit('/',1)[-1][6:]
    if not _NAME.fullmatch(text):raise RuntimeDatabaseError('Invalid MySQL dataset name')
    return text


def _json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False,
                      default=lambda v:{'$bytes':bytes(v).hex()} if isinstance(v,(bytes,bytearray)) else str(v))


def _sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def _identifier(value):
    if not _IDENT.fullmatch(value) or len(value)>64:raise RuntimeDatabaseError('Invalid dataset SQL identifier')
    return '`'+value+'`'


def _configuration():
    env={}
    configured=os.environ.get('UFC_MYSQL_CONFIG')
    path=Path(configured).expanduser() if configured else Path.home()/'Library/Application Support/UFCForecast/pipeline-env.json'
    if path.exists() or configured:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode&0o077:
            raise RuntimeDatabaseError('MySQL configuration must be an owner-only regular file')
        try:
            value=json.loads(path.read_text());env=value.get('environment',{})
            if not isinstance(env,dict):raise ValueError()
        except (OSError,ValueError,TypeError):raise RuntimeDatabaseError('MySQL configuration is invalid') from None
    env={**env,**{k:v for k,v in os.environ.items() if k.startswith('UFC_MYSQL_')}}
    if any(k not in env for k in ('UFC_MYSQL_USER','UFC_MYSQL_PASSWORD','UFC_MYSQL_DATABASE')):
        raise RuntimeDatabaseError('Configure UFC_MYSQL_USER, UFC_MYSQL_PASSWORD and UFC_MYSQL_DATABASE; SQLite fallback is disabled')
    database=env['UFC_MYSQL_DATABASE']
    if not isinstance(database,str) or not re.fullmatch('[A-Za-z0-9_]{1,64}',database) or database.lower() in {'mysql','sys','information_schema','performance_schema'}:
        raise RuntimeDatabaseError('A dedicated project MySQL database is required')
    try:port=int(env.get('UFC_MYSQL_PORT','3306'))
    except (ValueError,TypeError):raise RuntimeDatabaseError('Invalid UFC_MYSQL_PORT') from None
    if not 1<=port<=65535:raise RuntimeDatabaseError('Invalid UFC_MYSQL_PORT')
    return dict(host=env.get('UFC_MYSQL_HOST','127.0.0.1'),port=port,user=env['UFC_MYSQL_USER'],password=env['UFC_MYSQL_PASSWORD'],
                database=database,charset='utf8mb4',autocommit=False,local_infile=False,
                connect_timeout=5,read_timeout=120,write_timeout=120)


def connect_server(*,readonly=False,connector=None):
    options=_configuration()
    if connector is None:
        try:import pymysql
        except ImportError:raise RuntimeDatabaseError('Install the project warehouse extra for MySQL storage') from None
        connector=pymysql.connect
    try:
        raw=connector(**options)
        with raw.cursor() as cursor:
            cursor.execute("SET SESSION sql_mode=CONCAT_WS(',',@@sql_mode,'NO_AUTO_VALUE_ON_ZERO')")
            if readonly:cursor.execute('SET SESSION TRANSACTION READ ONLY')
        return raw
    except Exception:raise RuntimeDatabaseError('Cannot connect to project MySQL; SQLite fallback is disabled') from None


def _private_json(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode&0o077 or path.stat().st_uid!=os.getuid():
        raise RuntimeDatabaseError('Project schema credentials must be owner-only regular files')
    try:return json.loads(path.read_text())
    except (OSError,ValueError):raise RuntimeDatabaseError('Project schema credential configuration is invalid') from None


def _restricted_trigger(sql):
    plain=_plain(sql)
    match=re.match(r'CREATE TRIGGER\s+(?:IF NOT EXISTS\s+)?[`\"]?\w+[`\"]?\s+(?:BEFORE|AFTER)\s+(?:INSERT|UPDATE|DELETE)\s+ON\s+[`\"]?(\w+)[`\"]?\s+FOR EACH ROW\s+([\s\S]+)$',plain,re.I)
    if not match or not match[1].startswith(('rt_','ufc_app_','ufc_runtime_','ufc_wh_')):
        raise RuntimeDatabaseError('Schema privilege is restricted to project integrity triggers')
    body=''.join(' ' if token.startswith("'") else token for token in _tokens(match[2]))
    if re.search(r'\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|GRANT|REVOKE|CREATE|TRUNCATE|CALL|OUTFILE|INFILE|INTO|PREPARE|EXECUTE|HANDLER|LOAD)\b',body,re.I):
        raise RuntimeDatabaseError('Privileged trigger bodies may only read project rows and signal integrity failures')
    for table in re.findall(r'\b(?:FROM|JOIN)\s+[`\"]?(\w+)',body,re.I):
        if not table.startswith(('rt_','ufc_app_','ufc_runtime_','ufc_wh_')):
            raise RuntimeDatabaseError('Privileged trigger reads must remain in project tables')
    # No database-qualified identifiers: this connection selects only the
    # verified project database. NEW/OLD and local query aliases are allowed.
    aliases={'NEW','OLD'}
    for table,alias in re.findall(r'\b(?:FROM|JOIN)\s+[`\"]?(\w+)[`\"]?\s+(?:AS\s+)?(\w+)',body,re.I):
        aliases.update((table.upper(),alias.upper()))
    for left in re.findall(r'[`\"]?([A-Za-z_]\w*)[`\"]?\s*\.',body):
        if left.upper() not in aliases:raise RuntimeDatabaseError('Cross-database privileged trigger references are disabled')
    return plain


class _SchemaCursor:
    def __init__(self,cursor):self.cursor=cursor
    def __enter__(self):self.cursor.__enter__();return self
    def __exit__(self,*args):return self.cursor.__exit__(*args)
    def execute(self,sql,params=None):return self.cursor.execute(_restricted_trigger(sql),params)
    def __getattr__(self,name):return getattr(self.cursor,name)


class _SchemaConnection:
    def __init__(self,raw):self.raw=raw
    def cursor(self):return _SchemaCursor(self.raw.cursor())
    def commit(self):self.raw.commit()
    def rollback(self):self.raw.rollback()
    def close(self):self.raw.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def schema_connection():
    """Narrow project-trigger setup privilege; never used for normal data I/O.

    CI/remote setup may explicitly supply UFC_MYSQL_SCHEMA_USER/PASSWORD.
    Local setup uses only the separately provisioned project socket/admin file
    after validating its owner, port, config, database and physical data dir.
    """
    try:import pymysql
    except ImportError:raise RuntimeDatabaseError('Install the project warehouse extra for schema setup') from None
    options=_configuration();options['autocommit']=True
    schema_user=os.environ.get('UFC_MYSQL_SCHEMA_USER');schema_password=os.environ.get('UFC_MYSQL_SCHEMA_PASSWORD')
    local=False
    if schema_user is not None or schema_password is not None:
        if not schema_user or schema_password is None:raise RuntimeDatabaseError('Explicit MySQL schema credentials require both user and password')
        options.update(user=schema_user,password=schema_password)
    else:
        base=Path.home()/'Library/Application Support/UFCForecast';state=base/'mysql'
        admin=_private_json(state/'admin.json')
        config=Path(os.environ.get('UFC_MYSQL_CONFIG',str(base/'pipeline-env.json'))).expanduser()
        configured=_private_json(config).get('environment',{})
        if (options['host'] not in {'127.0.0.1','localhost'} or options['port']!=admin.get('port')
                or Path(admin.get('config','')).resolve()!=config.resolve()
                or options['database']!=configured.get('UFC_MYSQL_DATABASE')
                or configured.get('UFC_MYSQL_PORT')!=str(options['port'])
                or state.is_symlink() or (state/'mysql.sock').is_symlink()):
            raise RuntimeDatabaseError('Project admin credentials do not match the configured local MySQL instance')
        options.update(user='root',password=admin['password'],unix_socket=str(state/'mysql.sock'));local=True
    try:
        raw=pymysql.connect(**options)
        with raw.cursor() as c:
            c.execute('SELECT @@datadir,@@port,DATABASE()');data_dir,port,database=c.fetchone()
        if database!=options['database'] or int(port)!=options['port'] or local and Path(data_dir).resolve()!=(state/'data').resolve():
            raw.close();raise RuntimeDatabaseError('Schema connection resolved outside the configured project instance')
        return _SchemaConnection(raw)
    except RuntimeDatabaseError:raise
    except Exception:raise RuntimeDatabaseError('Project trigger setup requires explicit DBA/schema credentials') from None


def _execute_trigger(cursor,sql):
    try:cursor.execute(sql)
    except Exception as exc:
        if not getattr(exc,'args',()) or exc.args[0]!=1419:raise
        with schema_connection() as admin:
            with admin.cursor() as elevated:elevated.execute(sql)


def _catalog(raw):
    with raw.cursor() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS ufc_runtime_datasets(
          dataset varchar(96) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
          role varchar(48) NOT NULL,status varchar(16) NOT NULL,immutable tinyint NOT NULL,
          source_sha256 char(64),content_sha256 char(64),schema_sha256 char(64) NOT NULL,
          metadata_json JSON NOT NULL,created_at_utc varchar(40) NOT NULL,
          publication_version bigint NOT NULL DEFAULT 0) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin''')
        c.execute('''CREATE TABLE IF NOT EXISTS ufc_runtime_tables(
          dataset varchar(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          table_name varchar(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          physical_name varchar(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
          descriptor_json JSON NOT NULL,PRIMARY KEY(dataset,table_name),
          FOREIGN KEY(dataset) REFERENCES ufc_runtime_datasets(dataset))
          ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin''')
        c.execute('''CREATE TABLE IF NOT EXISTS ufc_runtime_aliases(
          alias varchar(96) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
          target varchar(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          activated_at_utc varchar(40) NOT NULL,
          FOREIGN KEY(target) REFERENCES ufc_runtime_datasets(dataset))
          ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin''')
    raw.commit()


def _row_dict(cursor,row):return dict(zip([col[0] for col in cursor.description],row))


def _entry(raw,name):
    try:
        with raw.cursor() as c:
            c.execute('SELECT * FROM ufc_runtime_datasets WHERE dataset=%s',(name,));row=c.fetchone()
            if row is None:return None
            result=_row_dict(c,row)
            if isinstance(result['metadata_json'],str):result['metadata_json']=json.loads(result['metadata_json'])
            return result
    except Exception as exc:
        if getattr(exc,'args',()) and exc.args[0]==1146:return None
        raise RuntimeDatabaseError('Could not read MySQL dataset catalog') from None


def _descriptors(raw,name):
    with raw.cursor() as c:
        c.execute('SELECT table_name,physical_name,descriptor_json FROM ufc_runtime_tables WHERE dataset=%s ORDER BY table_name',(name,))
        return {logical:{**(json.loads(meta) if isinstance(meta,str) else meta),'physical':physical} for logical,physical,meta in c.fetchall()}


def _resolved(raw,name):
    direct=_entry(raw,name)
    if direct:return name,direct
    try:
        with raw.cursor() as c:
            c.execute('SELECT target FROM ufc_runtime_aliases WHERE alias=%s',(name,));row=c.fetchone()
        return (row[0],_entry(raw,row[0])) if row else (name,None)
    except Exception as exc:
        if getattr(exc,'args',()) and exc.args[0]==1146:return name,None
        raise RuntimeDatabaseError('Could not resolve MySQL dataset alias') from None


def dataset_exists(dataset='operating'):
    raw=connect_server(readonly=True)
    try:return _resolved(raw,dataset_name(dataset))[1] is not None
    finally:raw.close()


class Row:
    def __init__(self,names,values):self._names=tuple(names);self._values=tuple(values)
    def keys(self):return self._names
    def __len__(self):return len(self._values)
    def __iter__(self):return iter(self._values)
    def __getitem__(self,key):return self._values[self._names.index(key)] if isinstance(key,str) else self._values[key]
    def __repr__(self):return repr(dict(self))


def _sqlite_value(value):
    # Native source columns use BIGINT/DOUBLE; MySQL aggregate expressions over
    # integers additionally return Decimal. Preserve the prior SQLite scalar
    # contract so callers can serialize coverage and ledger totals directly.
    if isinstance(value,Decimal):
        if not value.is_finite():raise RuntimeIntegrityError('MySQL returned a non-finite numeric value')
        return int(value) if value==value.to_integral_value() else float(value)
    return value


class Cursor:
    def __init__(self,rows=(),names=(),*,lastrowid=None,rowcount=None,description=None):
        self._rows=list(rows);self._position=0;self.names=tuple(names)
        self.lastrowid=lastrowid;self.rowcount=len(self._rows) if rowcount is None else rowcount
        self.description=description or tuple((name,None,None,None,None,None,None) for name in names)
    def fetchone(self):
        if self._position>=len(self._rows):return None
        value=self._rows[self._position];self._position+=1
        return Row(self.names,value)
    def fetchall(self):
        values=self._rows[self._position:];self._position=len(self._rows)
        return [Row(self.names,value) for value in values]
    def __iter__(self):
        while (row:=self.fetchone()) is not None:yield row
    def close(self):pass


def _tokens(sql):
    """Tokenize strings/comments separately so binding and identifiers stay safe."""
    pattern=r"('(?:''|\\.|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|[A-Za-z_][A-Za-z0-9_]*|\?|\s+|.)"
    return re.findall(pattern,sql)


def _plain(sql):return ''.join(' ' if token.startswith(('--','/*')) else token for token in _tokens(sql)).strip().rstrip(';').strip()


def _split_definitions(value):
    result=[];start=0;depth=0
    tokens=_tokens(value);current=[]
    for token in tokens:
        if token=='(':depth+=1
        elif token==')':depth-=1
        if token==',' and depth==0:result.append(''.join(current).strip());current=[]
        else:current.append(token)
    if current:result.append(''.join(current).strip())
    return result


def _statements(script):
    current=''
    for token in _tokens(script):
        current+=token
        if token==';' and sqlite3.complete_statement(current):
            if _plain(current):yield _plain(current)
            current=''
    if _plain(current):yield _plain(current)


def _column_name(value):
    match=re.match(r'[`\"]?([A-Za-z_][A-Za-z0-9_]*)[`\"]?',value.strip())
    if not match:raise RuntimeDatabaseError('Unsupported source schema identifier')
    return match[1]


def _schema(script):
    tables={};indexes=[];triggers={}
    for statement in _statements(script):
        if re.match('PRAGMA ',statement,re.I):continue
        match=re.match(r'CREATE TABLE(?: IF NOT EXISTS)?\s+[`\"]?(\w+)[`\"]?\s*\(([\s\S]*)\)$',statement,re.I)
        if match:
            name,body=match.groups();tables[name]={'name':name,'definitions':_split_definitions(body),'source_sql':statement};continue
        match=re.match(r'ALTER TABLE\s+(\w+)\s+ADD(?: COLUMN)?\s+([\s\S]+)',statement,re.I)
        if match:
            name,definition=match.groups()
            if name not in tables:raise RuntimeDatabaseError('Schema alteration lacks its base table')
            tables[name]['definitions'].append(definition);continue
        match=re.match(r'CREATE(?: UNIQUE)? INDEX(?: IF NOT EXISTS)?\s+\w+\s+ON\s+(\w+)\s*\(([\s\S]+)\)',statement,re.I)
        if match:indexes.append((match[1],match[2],bool(re.match('CREATE UNIQUE',statement,re.I))));continue
        match=re.match(r'CREATE TRIGGER(?: IF NOT EXISTS)?\s+(\w+)\s+([\s\S]+)',statement,re.I)
        if match:triggers[match[1]]=statement;continue
        match=re.match(r'DROP TRIGGER(?: IF EXISTS)?\s+(\w+)',statement,re.I)
        if match:triggers.pop(match[1],None);continue
        raise RuntimeDatabaseError('Unsupported schema statement; native migration is required')
    for name,table in tables.items():
        columns=[];primary=[];unique=[];foreign=[];checks=[]
        for definition in table['definitions']:
            upper=definition.upper()
            if re.match(r'^PRIMARY KEY\s*\(',upper):
                primary=[_column_name(v) for v in _split_definitions(definition[definition.index('(')+1:definition.rindex(')')])];continue
            if re.match(r'^UNIQUE\s*\(',upper):
                unique.append([_column_name(v) for v in _split_definitions(definition[definition.index('(')+1:definition.rindex(')')])]);continue
            if re.match(r'^CHECK\s*\(',upper):checks.append(definition);continue
            if upper.startswith('FOREIGN KEY'):
                m=re.match(r'FOREIGN KEY\s*\(([^)]+)\)\s+REFERENCES\s+(\w+)(?:\s*\(([^)]+)\))?',definition,re.I)
                if not m:raise RuntimeDatabaseError('Unsupported source foreign-key declaration')
                foreign.append(([x.strip() for x in m[1].split(',')],m[2],[x.strip() for x in m[3].split(',')] if m[3] else None));continue
            match=re.match(r'[`\"]?(\w+)[`\"]?\s+(INTEGER|INT|REAL|TEXT|BLOB)([\s\S]*)$',definition,re.I)
            if not match:raise RuntimeDatabaseError('Unsupported source column type')
            column,kind,rest=match.groups();kind=kind.upper()
            if re.search(r'PRIMARY KEY',rest,re.I):primary=[column]
            if re.search(r'\bUNIQUE\b',rest,re.I):unique.append([column])
            reference=re.search(r'REFERENCES\s+(\w+)(?:\s*\((\w+)\))?',rest,re.I)
            if reference:foreign.append(([column],reference[1],[reference[2]] if reference[2] else None))
            default=re.search(r"DEFAULT\s+((?:'[^']*(?:''[^']*)*')|[+-]?[0-9.]+|NULL)",rest,re.I)
            columns.append({'name':column,'type':kind,'notnull':int(bool(re.search(r'NOT NULL',rest,re.I))),
                            'default':default[1] if default else None,'rest':rest})
        integer_primary=next((c['name'] for c in columns if primary==[c['name']] and c['type'] in ('INTEGER','INT')),None)
        indexed=set(primary)|{c for group in unique for c in group}|{c for cols,_,_ in foreign for c in cols}
        for target,fields,_ in indexes:
            if target==name:indexed.update(_column_name(v) for v in _split_definitions(fields))
        for number,c in enumerate(columns):
            c['cid']=number;c['pk']=primary.index(c['name'])+1 if c['name'] in primary else 0
            c['mysql_type']=('BIGINT' if c['type'] in ('INTEGER','INT') else 'DOUBLE' if c['type']=='REAL' else
                             'LONGBLOB' if c['type']=='BLOB' else 'VARCHAR(191)' if c['name'] in indexed else 'LONGTEXT')
        table.update(columns=columns,primary=primary,unique=unique,foreign=foreign,checks=checks,
                     ordinal=integer_primary or ORDINAL,extra_ordinal=integer_primary is None)
    for table in tables.values():
        for cols,parent,target in table['foreign']:
            if parent not in tables:raise RuntimeDatabaseError('Source schema references an absent table')
            target=target or tables[parent]['primary']
            if len(cols)!=len(target):raise RuntimeDatabaseError('Source foreign-key width differs')
            for column,parent_column in zip(cols,target):
                own=next(c for c in table['columns'] if c['name']==column)
                referenced=next(c for c in tables[parent]['columns'] if c['name']==parent_column)
                if own['type']=='TEXT':own['mysql_type']=referenced['mysql_type']='VARCHAR(191)'
    return tables,indexes,triggers


def describe_schema(schema_sql):
    """Describe the supported source schema without opening either database.

    Returns independent ``(tables, indexes, triggers)`` descriptors. The native
    physical names are assigned only when a new dataset is created.
    """
    if not isinstance(schema_sql,str):raise RuntimeDatabaseError('Dataset schema must be text')
    return _schema(schema_sql)


def _mapped(sql,tables,*,parameters=True):
    known={c['name'].lower() for table in tables.values() for c in table['columns']};result=[];prior=None
    tokens=_tokens(sql)
    for position,token in enumerate(tokens):
        if token=='?' and parameters:result.append('%s')
        elif token.startswith("'") or token.startswith('--') or token.startswith('/*'):result.append(token.replace('%','%%') if parameters else token)
        elif token=='%' and parameters:result.append('%%')
        else:
            unquoted=token[1:-1] if token.startswith(('"','`')) else token
            lowered=unquoted.lower()
            following=next((t for t in tokens[position+1:] if t.strip()),None)
            if unquoted in tables and (prior in {'FROM','JOIN','INTO','UPDATE','TABLE'} or following=='.'):
                result.append(_identifier(tables[unquoted]['physical']))
            elif lowered in known or token.startswith(('"','`')):result.append(_identifier(unquoted))
            else:result.append(token)
        if token.strip():prior=token.upper()
    return ''.join(result)


def _table_ddl(table,*,temporary=False):
    parts=[];column_checks=[]
    for c in table['columns']:
        rest=re.sub(r'\bPRIMARY KEY\b|\bAUTOINCREMENT\b|\bUNIQUE\b','',c['rest'],flags=re.I)
        rest=re.sub(r'REFERENCES\s+\w+(?:\s*\(\w+\))?','',rest,flags=re.I)
        # MySQL column CHECKs may not reference another column. SQLite permits
        # that spelling, so retain every expression as a table-level CHECK.
        while (check:=re.search(r'\bCHECK\s*\(',rest,re.I)) is not None:
            opening=rest.index('(',check.start());depth=0;length=0
            for token in _tokens(rest[opening:]):
                length+=len(token)
                if token=='(':depth+=1
                elif token==')':depth-=1
                if depth==0:break
            if depth:raise RuntimeDatabaseError('Unbalanced source CHECK expression')
            column_checks.append(rest[check.start():opening+length])
            rest=rest[:check.start()]+rest[opening+length:]
        if c['name']==table['ordinal']:
            rest=re.sub(r'\bNOT NULL\b','',rest,flags=re.I)
            rest=' NOT NULL AUTO_INCREMENT '+rest
        if c['mysql_type'] in ('LONGTEXT','LONGBLOB') and c['default'] is not None:
            rest=re.sub(r'DEFAULT\s+'+re.escape(c['default']), 'DEFAULT ('+c['default']+')',rest,flags=re.I)
        parts.append(_identifier(c['name'])+' '+c['mysql_type']+' '+rest.strip())
    if table['extra_ordinal']:parts.append(_identifier(ORDINAL)+' BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE')
    if table['primary']:parts.append('PRIMARY KEY ('+','.join(_identifier(c) for c in table['primary'])+')')
    for fields in table['unique']:parts.append('UNIQUE ('+','.join(_identifier(c) for c in fields)+')')
    # All generated identifiers above are already quoted. Map only source
    # CHECK expressions: mapping the entire generated DDL would turn the SQL
    # keyword KEY into a column identifier for schemas containing a `key` field.
    local={table['name']:{**table,'physical':table['physical']}}
    parts.extend(_mapped(check,local,parameters=False) for check in table['checks']+column_checks)
    query=('CREATE '+('TEMPORARY ' if temporary else '')+'TABLE '+_identifier(table['physical'])+
           '('+','.join(parts)+') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin')
    return query


def _datetime_functions(sql):
    # The supported timestamp fields are canonical UTC ISO strings. Invalid
    # timestamps fail their gate rather than being silently accepted.
    return re.sub(r'julianday\(([^()]+)\)',lambda m:"UNIX_TIMESTAMP(STR_TO_DATE(REPLACE(REPLACE("+m[1]+",'T',' '),'Z',''),'%Y-%m-%d %H:%i:%s'))",sql,flags=re.I)


def _trigger_ddl(statement,tables,prefix,dataset=None):
    match=re.match(r'CREATE TRIGGER(?: IF NOT EXISTS)?\s+(\w+)\s+(BEFORE|AFTER)\s+(INSERT|UPDATE|DELETE)\s+ON\s+(\w+)\s*([\s\S]*?)\bBEGIN\b([\s\S]*)\bEND$',statement,re.I)
    if not match:raise RuntimeDatabaseError('Unsupported immutable trigger declaration')
    name,timing,event,table,when,body=match.groups()
    when=re.sub(r'^WHEN\s+','',when.strip(),flags=re.I)
    when=re.sub(r'\b(NEW|OLD)\.(\w+)\s+IS NOT\s+(NEW|OLD)\.(\w+)',r'NOT (\1.\2 <=> \3.\4)',when,flags=re.I)
    message=re.fullmatch(r"\s*SELECT\s+RAISE\(ABORT,\s*('(?:''|[^'])*')\s*\)\s*;?\s*",body,re.I)
    if not message:raise RuntimeDatabaseError('Source triggers must contain only a retained integrity failure')
    body="SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT="+message[1]+';'
    trigger='tr_'+hashlib.sha256((prefix+name).encode()).hexdigest()[:36]
    inner=('IF '+when+' THEN '+body+' END IF;' if when else body)
    if dataset is not None:
        inner="IF EXISTS(SELECT 1 FROM ufc_runtime_datasets WHERE dataset='"+dataset+"' AND status='ready') THEN "+inner+' END IF;'
    query='CREATE TRIGGER '+_identifier(trigger)+' '+timing+' '+event+' ON '+_identifier(tables[table]['physical'])+' FOR EACH ROW BEGIN '+inner+' END'
    return _datetime_functions(_mapped(query,tables,parameters=False))


class Connection:
    dialect='mysql'
    def __init__(self,raw,name,tables,entry,*,readonly=False,scratch=False,lock=None):
        self.raw=raw;self.dataset=name;self.requested_dataset=name;self.tables=tables;self.role=entry.get('role');self.source_sha256=entry.get('source_sha256')
        self.immutable=bool(entry.get('immutable'));self.readonly=readonly or self.immutable and entry.get('status')=='ready'
        self.scratch=scratch;self.lock=lock;self.row_factory=None;self._closed=False
    def __enter__(self):return self
    def __exit__(self,kind,value,tb):self.rollback() if kind else self.commit()
    def close(self):
        if not self._closed:self._closed=True;self.raw.close()
    def commit(self):self.raw.commit()
    def rollback(self):self.raw.rollback()
    def begin(self):self.raw.begin()
    @property
    def in_transaction(self):return bool(getattr(self.raw,'server_status',0)&1)
    def cursor(self):return _ExecuteCursor(self)
    def _metadata_query(self,sql,params):
        upper=sql.upper()
        if upper.startswith('PRAGMA'):
            if re.fullmatch(r'PRAGMA\s+(?:QUERY_ONLY|FOREIGN_KEYS)\s*=\s*(?:ON|1)',upper):
                if 'QUERY_ONLY' in upper:self.readonly=True
                return Cursor()
            if re.fullmatch(r'PRAGMA\s+DATABASE_LIST',upper):return Cursor([(0,'main','mysql:'+self.dataset)],('seq','name','file'))
            match=re.fullmatch(r'PRAGMA\s+TABLE_INFO\s*\(\s*[`\"]?(\w+)[`\"]?\s*\)',sql,re.I)
            if match:
                table=self.tables.get(match[1]);values=[] if not table else [(c['cid'],c['name'],c['type'],c['notnull'],c['default'],c['pk']) for c in table['columns']]
                return Cursor(values,('cid','name','type','notnull','dflt_value','pk'))
            if re.fullmatch(r'PRAGMA\s+FOREIGN_KEY_CHECK',upper):return Cursor(self.foreign_key_violations(),('table','rowid','parent','fkid'))
            if re.fullmatch(r'PRAGMA\s+INTEGRITY_CHECK',upper):
                issues=self.foreign_key_violations()
                return Cursor([('ok' if not issues else 'foreign key violations',)],('integrity_check',))
            if re.fullmatch(r'PRAGMA\s+JOURNAL_MODE',upper):return Cursor([('mysql',)],('journal_mode',))
            raise RuntimeDatabaseError('Unsupported SQLite PRAGMA on MySQL')
        if re.search(r'\b(?:sqlite_master|sqlite_schema)\b',sql,re.I):
            names=sorted(self.tables)
            exact=re.search(r"\bname\s*=\s*'([^']+)'",sql,re.I)
            if exact:names=[name for name in names if name==exact[1]]
            if re.search(r'\bname\s*=\s*\?',sql,re.I):names=[name for name in names if params and name==params[0]]
            excluded=re.search(r"\bname\s*!=\s*'([^']+)'",sql,re.I)
            if excluded:names=[name for name in names if name!=excluded[1]]
            if re.search(r"type\s*=\s*'trigger'",sql,re.I):return Cursor([],('sql',))
            projection=re.search(r'^SELECT\s+(.+?)\s+FROM',sql,re.I|re.S)[1].strip()
            columns=[v.strip() for v in projection.split(',')]
            rows=[]
            for name in names:
                values={'name':name,'tbl_name':name,'type':'table','sql':self.tables[name]['source_sql'],'1':1}
                rows.append(tuple(values[c] for c in columns))
            if re.search(r'LIMIT\s+1\b',sql,re.I):rows=rows[:1]
            return Cursor(rows,columns)
        return None
    def _translate(self,sql):
        implicit=re.match(r'^(INSERT|REPLACE)\s+INTO\s+[`"]?(\w+)[`"]?\s+(VALUES\b)',sql,re.I)
        if implicit and implicit[2] in self.tables:
            # Native tables may contain an internal order column. Legacy
            # VALUES forms still address only the original source columns.
            columns=','.join(_identifier(c['name']) for c in self.tables[implicit[2]]['columns'])
            sql=implicit[1]+' INTO '+_identifier(implicit[2])+'('+columns+') '+sql[implicit.start(3):]
        explicit_rowid=bool(re.search(r'^SELECT\s+[\s\S]*?\browid\b[\s\S]*?\bFROM\b',sql,re.I))
        sql=_mapped(_datetime_functions(sql),self.tables)
        # Explicit rowid spelling preserves imported insertion order.
        for logical,table in self.tables.items():
            physical=_identifier(table['physical'])
            sql=re.sub(re.escape(physical)+r'\.rowid\b',physical+'.'+_identifier(table['ordinal']),sql,flags=re.I)
        aliases={}
        for match in re.finditer(r'(?:FROM|JOIN)\s+[`\"]?(\w+)[`\"]?\s+(?:AS\s+)?(\w+)',_plain(sql),re.I):
            for table in self.tables.values():
                if match[1]==table['physical']:aliases[match[2]]=table['ordinal']
        for alias,column in aliases.items():sql=re.sub(r'\b'+re.escape(alias)+r'\.rowid\b',_identifier(alias)+'.'+_identifier(column),sql,flags=re.I)
        if re.search(r'(?<![\w.])rowid\b',sql,re.I):
            target=next((t for t in self.tables.values() if _identifier(t['physical']) in sql),None)
            if target:sql=re.sub(r'(?<![\w.])rowid\b',_identifier(target['ordinal']),sql,flags=re.I)
        if explicit_rowid:
            projection=re.match(r'^(SELECT\s+)([\s\S]*?)(\s+FROM\b[\s\S]*)',sql,re.I)
            if projection:
                fields=[]
                for field in _split_definitions(projection[2]):
                    if any(re.fullmatch(r'(?:`?\w+`?\.)?'+re.escape(_identifier(t['ordinal'])),field) for t in self.tables.values()):
                        field+=' AS `rowid`'
                    fields.append(field)
                sql=projection[1]+','.join(fields)+projection[3]
        sql=re.sub(r'^BEGIN(?: IMMEDIATE)?$','START TRANSACTION',sql,flags=re.I)
        return sql
    def execute(self,sql,parameters=()):
        sql=_plain(sql);parameters=tuple(parameters or ())
        metadata=self._metadata_query(sql,parameters)
        if metadata is not None:return metadata
        verb=sql.split(None,1)[0].upper() if sql else ''
        if self.readonly and verb not in {'SELECT','SHOW','EXPLAIN','BEGIN','COMMIT','ROLLBACK','SAVEPOINT','RELEASE'}:
            raise RuntimeDatabaseError('MySQL dataset is read-only')
        if re.search(r'\bON\s+CONFLICT\s*\(',sql,re.I):return self._upsert(sql,parameters)
        ignore=bool(re.match(r'INSERT OR IGNORE\b',sql,re.I))
        sql=re.sub(r'^INSERT OR IGNORE\b','INSERT',sql,flags=re.I)
        sql=re.sub(r'^INSERT OR REPLACE\b','REPLACE',sql,flags=re.I)
        try:
            with self.raw.cursor() as c:
                c.execute(self._translate(sql),parameters)
                if c.description:
                    kept=[i for i,item in enumerate(c.description) if item[0]!=ORDINAL]
                    names=[c.description[i][0] for i in kept]
                    return Cursor([tuple(_sqlite_value(row[i]) for i in kept) for row in c.fetchall()],names,lastrowid=c.lastrowid,rowcount=c.rowcount)
                return Cursor(lastrowid=c.lastrowid,rowcount=c.rowcount)
        except Exception as exc:
            code=exc.args[0] if getattr(exc,'args',()) else None
            if ignore and code==1062:return Cursor(rowcount=0)
            if code in {1062,1451,1452,3819,1644,1048}:raise RuntimeIntegrityError('MySQL row violates a retained integrity or immutability constraint') from None
            raise RuntimeDatabaseError('MySQL dataset SQL operation failed') from None
    def _upsert(self,sql,params):
        match=re.fullmatch(r'INSERT INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)\s*ON CONFLICT\s*\(([^)]+)\)\s*DO UPDATE SET\s+([\s\S]+)',sql,re.I)
        if not match:raise RuntimeDatabaseError('Unsupported UPSERT form on MySQL')
        table,columns,values,conflict,updates=match.groups()
        columns=[_column_name(x) for x in _split_definitions(columns)];values=_split_definitions(values)
        if any(value!='?' for value in values) or len(columns)!=len(params):raise RuntimeDatabaseError('UPSERT values must use explicit bound parameters')
        data=dict(zip(columns,params));keys=[_column_name(x) for x in _split_definitions(conflict)]
        where=' AND '.join(_identifier(key)+' <=> ?' for key in keys)
        found=self.execute('SELECT 1 FROM '+_identifier(table)+' WHERE '+where+' FOR UPDATE',[data[key] for key in keys]).fetchone()
        if found:
            bound=[]
            def excluded(match):bound.append(data[match[1]]);return '?'
            updates=re.sub(r'\bexcluded\.(\w+)',excluded,updates,flags=re.I)
            return self.execute('UPDATE '+_identifier(table)+' SET '+updates+' WHERE '+where,bound+[data[key] for key in keys])
        return self.execute('INSERT INTO '+_identifier(table)+'('+','.join(_identifier(c) for c in columns)+') VALUES('+','.join('?' for _ in params)+')',params)
    def executemany(self,sql,parameters):
        count=0;last=None
        for values in parameters:
            last=self.execute(sql,values);count+=max(last.rowcount,0)
        return Cursor(lastrowid=last.lastrowid if last else None,rowcount=count)
    def executescript(self,script):
        raise RuntimeDatabaseError('MySQL schema scripts must use create_dataset or ensure_operating_dataset')
    def foreign_key_violations(self):
        result=[]
        for name,table in self.tables.items():
            for number,(columns,parent,targets) in enumerate(table['foreign']):
                targets=targets or self.tables[parent]['primary']
                condition=' AND '.join('p.'+_identifier(b)+'=c.'+_identifier(a) for a,b in zip(columns,targets))
                present=' AND '.join('c.'+_identifier(c)+' IS NOT NULL' for c in columns)
                query='SELECT c.'+_identifier(table['ordinal'])+' FROM '+_identifier(table['physical'])+' c WHERE '+present+' AND NOT EXISTS(SELECT 1 FROM '+_identifier(self.tables[parent]['physical'])+' p WHERE '+condition+')'
                with self.raw.cursor() as c:
                    c.execute(query);result.extend((name,row[0],parent,number) for row in c.fetchall())
        return result


class _ExecuteCursor:
    def __init__(self,connection):self.connection=connection;self.result=Cursor()
    def execute(self,sql,parameters=()):self.result=self.connection.execute(sql,parameters);return self
    def executemany(self,sql,parameters):self.result=self.connection.executemany(sql,parameters);return self
    def __getattr__(self,key):return getattr(self.result,key)
    def __iter__(self):return iter(self.result)
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def close(self):pass


def connect_dataset(dataset='operating',*,readonly=False,connector=None):
    requested=dataset_name(dataset);raw=connect_server(readonly=readonly,connector=connector)
    try:
        name,entry=_resolved(raw,requested)
        if entry is None or entry['status']!='ready':raise RuntimeDatabaseError('MySQL dataset is missing or unpublished: '+requested)
        tables=_descriptors(raw,name)
        if not tables:raise RuntimeDatabaseError('MySQL dataset has no published table contracts')
        connection=Connection(raw,name,tables,entry,readonly=readonly);connection.requested_dataset=requested
        return connection
    except BaseException:raw.close();raise


def _install(raw,name,tables,indexes,triggers,*,scratch=False):
    prefix=('tmp_'+uuid.uuid4().hex[:12]+'_' if scratch else 'rt_'+hashlib.sha256(name.encode()).hexdigest()[:16]+'_')
    for logical,table in tables.items():table['physical']=prefix+logical
    with raw.cursor() as c:
        for table in tables.values():c.execute(_table_ddl(table,temporary=scratch))
        if not scratch:
            for table in tables.values():
                for number,(cols,parent,target) in enumerate(table['foreign']):
                    target=target or tables[parent]['primary'];key='fk_'+hashlib.sha256((prefix+table['name']+str(number)).encode()).hexdigest()[:32]
                    c.execute('ALTER TABLE '+_identifier(table['physical'])+' ADD CONSTRAINT '+_identifier(key)+' FOREIGN KEY('+','.join(_identifier(v) for v in cols)+') REFERENCES '+_identifier(tables[parent]['physical'])+'('+','.join(_identifier(v) for v in target)+')')
        for number,(table,fields,unique) in enumerate(indexes):
            c.execute('CREATE '+('UNIQUE ' if unique else '')+'INDEX '+_identifier('idx_'+str(number))+' ON '+_identifier(tables[table]['physical'])+'('+','.join(_identifier(_column_name(x))+(' DESC' if re.search(r'\bDESC\b',x,re.I) else '') for x in _split_definitions(fields))+')')
        if not scratch:
            for statement in triggers.values():_execute_trigger(c,_trigger_ddl(statement,tables,prefix,name))
            for table in tables.values():
                for event in ('INSERT','UPDATE','DELETE'):
                    trigger='guard_'+hashlib.sha256((prefix+table['name']+event).encode()).hexdigest()[:32]
                    _execute_trigger(c,'CREATE TRIGGER '+_identifier(trigger)+' BEFORE '+event+' ON '+_identifier(table['physical'])+
                        " FOR EACH ROW BEGIN IF EXISTS(SELECT 1 FROM ufc_runtime_datasets WHERE dataset='"+name+"' AND immutable=1 AND status='ready') THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='published imported datasets are immutable'; END IF; END")
    return tables


def create_dataset(schema_sql,dataset,role):
    name=dataset_name(dataset)
    if role not in {'operating','research','statistics','legacy','synthetic'}:raise RuntimeDatabaseError('Unsupported MySQL dataset role')
    tables,indexes,triggers=_schema(schema_sql)
    if not tables:raise RuntimeDatabaseError('Dataset schema must contain tables')
    raw=connect_server();lock='ufc_rt:'+hashlib.sha256((_configuration()['database'].casefold()+':'+name).encode()).hexdigest()[:48]
    try:
        _catalog(raw)
        with raw.cursor() as c:
            c.execute('SELECT GET_LOCK(%s,30)',(lock,))
            if c.fetchone()!=(1,):raise RuntimeDatabaseError('Another process is publishing this MySQL dataset')
        if _resolved(raw,name)[1] is not None:raise RuntimeDatabaseError('MySQL dataset already exists; choose a new version name')
        with raw.cursor() as c:
            c.execute('INSERT INTO ufc_runtime_datasets(dataset,role,status,immutable,schema_sha256,metadata_json,created_at_utc) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                (name,role,'building',int(role!='operating'),hashlib.sha256(schema_sql.encode()).hexdigest(),_json({'schema_sql':schema_sql}),datetime.now(timezone.utc).isoformat()))
        raw.commit()
        tables=_install(raw,name,tables,indexes,triggers)
        with raw.cursor() as c:
            for logical,table in tables.items():
                c.execute('INSERT INTO ufc_runtime_tables(dataset,table_name,physical_name,descriptor_json) VALUES(%s,%s,%s,%s)',(name,logical,table['physical'],_json({k:v for k,v in table.items() if k!='physical'})))
        raw.commit()
        return Connection(raw,name,tables,{'role':role,'status':'building','immutable':role!='operating'},lock=lock)
    except BaseException:raw.close();raise


def _fingerprint(connection):
    h=hashlib.sha256();counts={}
    for name,table in sorted(connection.tables.items()):
        columns=[c['name'] for c in table['columns']]
        h.update(_json([name,columns]).encode()+b'\n')
        with connection.raw.cursor() as c:
            c.execute('SELECT '+','.join(_identifier(x) for x in [table['ordinal']]+columns)+' FROM '+_identifier(table['physical'])+' ORDER BY '+_identifier(table['ordinal']))
            rows=c.fetchall();counts[name]=len(rows)
            for row in rows:h.update(_json(list(row)).encode()+b'\n')
    return h.hexdigest(),counts


def finalize_dataset(dataset_or_connection,metadata=None,*,source_sha256=None):
    owned=not isinstance(dataset_or_connection,Connection)
    if owned:
        raw=connect_server();name=dataset_name(dataset_or_connection);entry=_entry(raw,name)
        if not entry:raw.close();raise RuntimeDatabaseError('MySQL dataset is missing')
        connection=Connection(raw,name,_descriptors(raw,name),entry)
    else:connection=dataset_or_connection;raw=connection.raw;name=connection.dataset
    try:
        if connection.foreign_key_violations():raise RuntimeIntegrityError('MySQL dataset contains foreign-key violations')
        content,counts=_fingerprint(connection)
        if source_sha256 is not None and not re.fullmatch('[0-9a-f]{64}',source_sha256):raise RuntimeDatabaseError('Invalid imported source SHA256')
        existing=_entry(raw,name)
        payload={**existing.get('metadata_json',{}),**(metadata or {}),'row_counts':counts,'content_sha256':content}
        with raw.cursor() as c:
            c.execute("UPDATE ufc_runtime_datasets SET status='ready',source_sha256=%s,content_sha256=%s,metadata_json=%s,publication_version=publication_version+1 WHERE dataset=%s AND status='building'",(source_sha256,content,_json(payload),name))
            if c.rowcount!=1:raise RuntimeDatabaseError('Only a building MySQL dataset can be finalized')
        raw.commit();connection.source_sha256=source_sha256
        if connection.immutable:connection.readonly=True
        return {'dataset':name,'target':'mysql:'+name,'role':connection.role,'status':'ready','source_sha256':source_sha256,
                'content_sha256':content,'counts':counts,'publication_version':1,'backend':'mysql'}
    except BaseException:raw.rollback();raise
    finally:
        if owned:connection.close()


def inspect_dataset(dataset='operating',*,verify=True):
    connection=connect_dataset(dataset,readonly=True)
    try:
        entry=_entry(connection.raw,connection.dataset)
        content,counts=_fingerprint(connection) if verify else (entry['content_sha256'],entry['metadata_json'].get('row_counts',{}))
        if verify and entry['immutable'] and content!=entry['content_sha256']:
            raise RuntimeIntegrityError('Published MySQL dataset content changed from its import receipt')
        return {**entry,'source_sha256':entry['source_sha256'],'content_sha256':content,'counts':counts,
                'name':connection.requested_dataset,'requested_dataset':connection.requested_dataset,
                'resolved_name':connection.dataset,'metadata':entry['metadata_json'],
                'backend':'mysql','target':'mysql:'+connection.dataset,'verified':verify,
                'row_content_verified':verify,'physical_schema_verified':False}
    finally:connection.close()


def _snapshot_cell(value):
    if value is None or type(value) in (str,int):return value
    if type(value) is float and math.isfinite(value):return value
    if isinstance(value,dict) and set(value)=={'$bytes'} and isinstance(value['$bytes'],str) and re.fullmatch(r'(?:[0-9a-f]{2})*',value['$bytes']):
        return bytes.fromhex(value['$bytes'])
    raise RuntimeIntegrityError('Snapshot contains an unsupported or non-finite cell')


def validate_dataset_snapshot(document):
    """Verify a logical snapshot entirely offline, before any native writes.

    The checksum covers sorted table names, original column names, row order,
    ordinals and cells. Source file SHA256 remains separate from this logical
    checksum. This validates retained schema and row contracts, not the physical
    state of a running MySQL server.
    """
    if not isinstance(document,dict) or document.get('schema_version')!=1 or document.get('storage_backend')!='mysql-dataset':
        raise RuntimeIntegrityError('Unsupported MySQL dataset snapshot format')
    dataset_name(document.get('dataset',''))
    dataset_name(document.get('requested_dataset',document['dataset']))
    if document.get('role') not in {'operating','research','statistics','legacy','synthetic'}:
        raise RuntimeIntegrityError('Invalid snapshot dataset role')
    for field in ('content_sha256','schema_sha256'):
        if not isinstance(document.get(field),str) or not re.fullmatch('[0-9a-f]{64}',document[field]):
            raise RuntimeIntegrityError('Invalid snapshot checksum')
    source=document.get('source_sha256')
    if source is not None and (not isinstance(source,str) or not re.fullmatch('[0-9a-f]{64}',source)):
        raise RuntimeIntegrityError('Invalid snapshot source checksum')
    script=document.get('schema_sql')
    if not isinstance(script,str) or hashlib.sha256(script.encode()).hexdigest()!=document['schema_sha256']:
        raise RuntimeIntegrityError('Snapshot schema checksum differs')
    tables,indexes,triggers=describe_schema(script)
    if not tables or not isinstance(document.get('tables'),dict) or set(document['tables'])!=set(tables):
        raise RuntimeIntegrityError('Snapshot table inventory differs from its schema')
    metadata=document.get('metadata')
    if not isinstance(metadata,dict):raise RuntimeIntegrityError('Snapshot metadata must be an object')
    try:json.dumps(metadata,allow_nan=False)
    except (ValueError,TypeError):raise RuntimeIntegrityError('Snapshot metadata is not finite JSON') from None
    if 'schema_sql' in metadata and metadata['schema_sql']!=script:
        raise RuntimeIntegrityError('Snapshot metadata schema differs')
    checksum=hashlib.sha256();counts={};decoded={}
    for name,table in sorted(tables.items()):
        saved=document['tables'][name];columns=[c['name'] for c in table['columns']]
        if not isinstance(saved,dict) or saved.get('columns')!=columns or saved.get('ordinal_column')!=table['ordinal'] or not isinstance(saved.get('rows'),list):
            raise RuntimeIntegrityError('Snapshot column or ordinal contract differs')
        checksum.update(_json([name,columns]).encode()+b'\n');rows=[];previous=None
        for row in saved['rows']:
            if not isinstance(row,list) or len(row)!=len(columns)+1 or type(row[0]) is not int or not -(2**63)<=row[0]<2**63:
                raise RuntimeIntegrityError('Snapshot row width or ordinal differs')
            if previous is not None and row[0]<=previous:raise RuntimeIntegrityError('Snapshot ordinals must be unique and increasing')
            previous=row[0];cells=[_snapshot_cell(cell) for cell in row[1:]]
            if not table['extra_ordinal'] and cells[columns.index(table['ordinal'])]!=row[0]:
                raise RuntimeIntegrityError('Snapshot primary key and ordinal disagree')
            for descriptor,value in zip(table['columns'],cells):
                if value is None and (descriptor['notnull'] or descriptor['pk']):
                    raise RuntimeIntegrityError('Snapshot contains a null required value')
            rows.append(cells);checksum.update(_json(row).encode()+b'\n')
        decoded[name]=rows;counts[name]=len(rows)
        for keys in [table['primary']]+table['unique']:
            if not keys:continue
            seen=set();positions=[columns.index(key) for key in keys]
            for row in rows:
                key=tuple(row[i] for i in positions)
                if any(cell is None for cell in key):continue
                if key in seen:raise RuntimeIntegrityError('Snapshot contains a duplicate primary or unique key')
                seen.add(key)
    if checksum.hexdigest()!=document['content_sha256']:
        raise RuntimeIntegrityError('Snapshot logical row checksum differs')
    for name,table in tables.items():
        columns=[c['name'] for c in table['columns']]
        for own,parent,target in table['foreign']:
            target=target or tables[parent]['primary'];parent_columns=[c['name'] for c in tables[parent]['columns']]
            parent_keys={tuple(row[parent_columns.index(key)] for key in target) for row in decoded[parent]}
            for row in decoded[name]:
                key=tuple(row[columns.index(column)] for column in own)
                if all(cell is not None for cell in key) and key not in parent_keys:
                    raise RuntimeIntegrityError('Snapshot contains a foreign-key violation')
    return {'content_sha256':checksum.hexdigest(),'schema_sha256':document['schema_sha256'],
            'counts':counts,'tables':tables,'indexes':indexes,'triggers':triggers,'verified':True}


def export_dataset_snapshot(target):
    """Return one consistent logical snapshot without connection credentials."""
    connection=connect_dataset(target,readonly=True)
    try:
        entry=_entry(connection.raw,connection.dataset);metadata=dict(entry['metadata_json'])
        script=metadata.get('schema_sql')
        if not isinstance(script,str):raise RuntimeIntegrityError('Dataset lacks its retained source schema; cannot produce a complete backup')
        saved={}
        for name,table in sorted(connection.tables.items()):
            columns=[c['name'] for c in table['columns']]
            with connection.raw.cursor() as c:
                c.execute('SELECT '+','.join(_identifier(x) for x in [table['ordinal']]+columns)+' FROM '+_identifier(table['physical'])+' ORDER BY '+_identifier(table['ordinal']))
                # JSON roundtrip gives binary cells the documented $bytes form.
                rows=json.loads(_json([list(row) for row in c.fetchall()]))
            saved[name]={'columns':columns,'ordinal_column':table['ordinal'],'rows':rows}
        h=hashlib.sha256()
        for name,table in sorted(saved.items()):
            h.update(_json([name,table['columns']]).encode()+b'\n')
            for row in table['rows']:h.update(_json(row).encode()+b'\n')
        content=h.hexdigest()
        if entry['immutable'] and content!=entry['content_sha256']:
            raise RuntimeIntegrityError('Published MySQL dataset changed from its content receipt')
        document={'schema_version':1,'storage_backend':'mysql-dataset','dataset':connection.dataset,
                  'requested_dataset':connection.requested_dataset,'role':entry['role'],
                  'source_sha256':entry['source_sha256'],'content_sha256':content,
                  'schema_sha256':entry['schema_sha256'],'schema_sql':script,'metadata':metadata,'tables':saved}
        validate_dataset_snapshot(document)
        return document
    finally:connection.close()


def import_dataset_snapshot(document,new_target):
    """Restore only into a new namespace; never overwrite or activate an alias."""
    validated=validate_dataset_snapshot(document)
    name=dataset_name(new_target)
    if dataset_exists(name):raise RuntimeDatabaseError('Snapshot restore requires a new, unused MySQL dataset namespace')
    connection=create_dataset(document['schema_sql'],name,document['role'])
    try:
        with connection.raw.cursor() as cursor:cursor.execute('SET SESSION foreign_key_checks=0')
        for logical,table in sorted(connection.tables.items()):
            columns=[c['name'] for c in table['columns']]
            destination=([ORDINAL] if table['extra_ordinal'] else [])+columns
            rows=[]
            for saved in document['tables'][logical]['rows']:
                cells=tuple(_snapshot_cell(cell) for cell in saved[1:])
                rows.append((saved[0],)+cells if table['extra_ordinal'] else cells)
            query='INSERT INTO '+_identifier(table['physical'])+'('+','.join(_identifier(c) for c in destination)+') VALUES('+','.join('%s' for _ in destination)+')'
            with connection.raw.cursor() as cursor:
                for start in range(0,len(rows),500):cursor.executemany(query,rows[start:start+500])
        with connection.raw.cursor() as cursor:cursor.execute('SET SESSION foreign_key_checks=1')
        actual,counts=_fingerprint(connection)
        if actual!=validated['content_sha256'] or counts!=validated['counts']:
            raise RuntimeIntegrityError('Restored MySQL rows differ from the verified snapshot')
        return finalize_dataset(connection,{**document['metadata'],'restored_from_dataset':document['dataset'],
            'restore_source_content_sha256':document['content_sha256'],'source_format':'mysql_logical_snapshot'},
            source_sha256=document['source_sha256'])
    except BaseException:connection.rollback();raise
    finally:connection.close()


def clone_dataset(source,new_target,*,role='research',upgrade_operating_schema=False):
    """Create a private BUILDING native copy for an explicit new publication.

    The caller owns the returned connection and must finalize/close it. Existing
    source rows and their ordinals are preserved; optional pending operating
    migrations apply only to the new schema. No source is made writable and no
    alias is changed. The new publication has its own logical content identity.
    """
    if not is_mysql_target(source):raise RuntimeDatabaseError('Native clones require a MySQL source dataset')
    name=dataset_name(new_target)
    document=export_dataset_snapshot(source)
    script=document['schema_sql'];pending=[]
    if upgrade_operating_schema:
        from importlib.resources import files
        migration_table=document['tables'].get('schema_migrations')
        if migration_table is None:raise RuntimeDatabaseError('Operating upgrade requires migration receipts')
        position=migration_table['columns'].index('version')+1
        applied={row[position] for row in migration_table['rows']}
        migrations=sorted((p for p in files('ufc_odds_model').joinpath('sql/migrations').iterdir() if p.name.endswith('.sql')),key=lambda p:p.name)
        if applied-{p.name for p in migrations}:raise RuntimeDatabaseError('Clone source has unknown operating migrations')
        pending=[p for p in migrations if p.name not in applied]
        for migration in pending:script+='\n'+migration.read_text()+'\n'
    connection=create_dataset(script,name,role)
    try:
        if set(document['tables'])-set(connection.tables):raise RuntimeIntegrityError('Clone migration would discard a source table')
        with connection.raw.cursor() as cursor:cursor.execute('SET SESSION foreign_key_checks=0')
        try:
            for logical,saved in sorted(document['tables'].items()):
                table=connection.tables[logical];columns=saved['columns']
                if table['ordinal']!=saved['ordinal_column'] or set(columns)-{c['name'] for c in table['columns']}:
                    raise RuntimeIntegrityError('Clone migration would change original columns or row ordering')
                destination=([ORDINAL] if table['extra_ordinal'] else [])+columns
                rows=[]
                for row in saved['rows']:
                    cells=tuple(_snapshot_cell(cell) for cell in row[1:])
                    rows.append((row[0],)+cells if table['extra_ordinal'] else cells)
                query='INSERT INTO '+_identifier(table['physical'])+'('+','.join(_identifier(c) for c in destination)+') VALUES('+','.join('%s' for _ in destination)+')'
                with connection.raw.cursor() as cursor:
                    for start in range(0,len(rows),500):cursor.executemany(query,rows[start:start+500])
        finally:
            with connection.raw.cursor() as cursor:cursor.execute('SET SESSION foreign_key_checks=1')
        copied=hashlib.sha256()
        for logical,saved in sorted(document['tables'].items()):
            table=connection.tables[logical];columns=saved['columns']
            copied.update(_json([logical,columns]).encode()+b'\n')
            with connection.raw.cursor() as cursor:
                cursor.execute('SELECT '+','.join(_identifier(c) for c in [table['ordinal']]+columns)+' FROM '+_identifier(table['physical'])+' ORDER BY '+_identifier(table['ordinal']))
                for row in cursor.fetchall():copied.update(_json(list(row)).encode()+b'\n')
        if copied.hexdigest()!=document['content_sha256']:
            raise RuntimeIntegrityError('Native clone differs from its source rows or ordering')
        stamp=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        connection.executemany('INSERT INTO schema_migrations(version,applied_at_utc) VALUES(?,?)',[(p.name,stamp) for p in pending])
        if connection.foreign_key_violations():raise RuntimeIntegrityError('Native clone contains foreign-key violations')
        metadata={'schema_sql':script,'source_format':'native_mysql_clone','cloned_from_dataset':document['dataset'],
                  'parent_content_sha256':document['content_sha256'],'parent_source_sha256':document['source_sha256'],
                  'new_schema_migrations':[p.name for p in pending]}
        with connection.raw.cursor() as cursor:
            cursor.execute('UPDATE ufc_runtime_datasets SET metadata_json=%s WHERE dataset=%s AND status=%s',(_json(metadata),name,'building'))
        return connection
    except BaseException:connection.rollback();connection.close();raise


def activate_alias(alias,target):
    """Activate only a verified ready immutable version after caller quality gates."""
    alias,target=dataset_name(alias),dataset_name(target)
    verified=inspect_dataset(target,verify=True)
    if verified['dataset']!=target or not verified['immutable']:
        raise RuntimeDatabaseError('Aliases must reference a direct immutable ready dataset version')
    raw=connect_server()
    try:
        _catalog(raw)
        if _entry(raw,alias) is not None:raise RuntimeDatabaseError('Dataset alias would hide an existing dataset namespace')
        with raw.cursor() as c:
            c.execute('INSERT INTO ufc_runtime_aliases(alias,target,activated_at_utc) VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE target=VALUES(target),activated_at_utc=VALUES(activated_at_utc)',
                      (alias,target,datetime.now(timezone.utc).isoformat()))
        raw.commit()
        return {'alias':alias,'target':target,'content_sha256':verified['content_sha256'],'status':'active','backend':'mysql'}
    except BaseException:raw.rollback();raise
    finally:raw.close()


def cleanup_created_dataset(dataset,*,expected_schema_sha256):
    """Remove only an empty, unpublished namespace identified by its schema hash.

    This recovery operation cannot remove ready publications, active aliases,
    nonempty tables or another dataset's namespace. Original sources are never
    involved. It exists for failed initial schema creation, not data pruning.
    """
    name=dataset_name(dataset);raw=connect_server();prefix='rt_'+hashlib.sha256(name.encode()).hexdigest()[:16]+'_'
    try:
        entry=_entry(raw,name)
        if not entry or entry['status']!='building' or entry['schema_sha256']!=expected_schema_sha256:
            raise RuntimeDatabaseError('Cleanup requires the exact empty building dataset schema receipt')
        with raw.cursor() as c:
            c.execute('SELECT COUNT(*) FROM ufc_runtime_aliases WHERE target=%s',(name,))
            if c.fetchone()[0]:raise RuntimeDatabaseError('Cannot clean an active dataset alias')
            c.execute('SELECT physical_name,dataset FROM ufc_runtime_tables WHERE physical_name LIKE %s',(prefix.replace('_','\\_')+'%',))
            if any(owner!=name for _,owner in c.fetchall()):raise RuntimeDatabaseError('Physical namespace belongs to another dataset')
            c.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE %s',(prefix.replace('_','\\_')+'%',))
            tables=[row[0] for row in c.fetchall()]
            for table in tables:
                if not table.startswith(prefix):raise RuntimeDatabaseError('Unexpected cleanup namespace')
                c.execute('SELECT COUNT(*) FROM '+_identifier(table))
                if c.fetchone()[0]:raise RuntimeDatabaseError('Refusing cleanup of a building dataset containing rows')
            c.execute('SET SESSION foreign_key_checks=0')
            try:
                for table in tables:c.execute('DROP TABLE '+_identifier(table))
            finally:c.execute('SET SESSION foreign_key_checks=1')
            c.execute('DELETE FROM ufc_runtime_tables WHERE dataset=%s',(name,))
            c.execute('DELETE FROM ufc_runtime_datasets WHERE dataset=%s',(name,))
        raw.commit();return {'dataset':name,'removed_empty_tables':len(tables),'status':'removed_empty_build'}
    except BaseException:raw.rollback();raise
    finally:raw.close()


def _operating_schema():
    from importlib.resources import files
    migrations=sorted(files('ufc_odds_model').joinpath('sql/migrations').iterdir(),key=lambda p:p.name)
    script='CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at_utc TEXT NOT NULL);\n'
    for migration in migrations:
        if migration.name.endswith('.sql'):script+=migration.read_text()+'\n'
    return script,[m.name for m in migrations if m.name.endswith('.sql')]


def ensure_operating_dataset(dataset='operating'):
    if dataset_exists(dataset):
        result=inspect_dataset(dataset)
        if result['role']!='operating' or result['immutable']:raise RuntimeDatabaseError('Operating dataset must be a mutable native MySQL dataset')
        return result
    schema,versions=_operating_schema();connection=create_dataset(schema,dataset,'operating')
    try:
        stamp=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        connection.executemany('INSERT INTO schema_migrations(version,applied_at_utc) VALUES(?,?)',[(v,stamp) for v in versions])
        return finalize_dataset(connection,{'schema_migrations':versions})
    finally:connection.close()


@contextmanager
def scratch_operating():
    schema,versions=_operating_schema();tables,indexes,triggers=_schema(schema);raw=connect_server()
    try:
        tables=_install(raw,'scratch',tables,indexes,triggers,scratch=True)
        connection=Connection(raw,'scratch',tables,{'role':'synthetic','immutable':False},scratch=True)
        stamp=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        connection.executemany('INSERT INTO schema_migrations(version,applied_at_utc) VALUES(?,?)',[(v,stamp) for v in versions])
        yield connection
    finally:raw.close()


def import_sqlite_snapshot(path,*,dataset,role='legacy',expected_sha256=None):
    """One-time explicit legacy import; original bytes and IDs stay unchanged."""
    path=Path(path).expanduser().resolve()
    if not path.is_file():raise RuntimeDatabaseError('Legacy SQLite source does not exist')
    source_sha=_sha(path)
    if expected_sha256 is not None and expected_sha256!=source_sha:raise RuntimeIntegrityError('Legacy SQLite source hash differs')
    if dataset_exists(dataset):
        result=inspect_dataset(dataset)
        if result['source_sha256']!=source_sha:raise RuntimeIntegrityError('Dataset name already identifies a different legacy source')
        if not result.get('immutable') and result['content_sha256']!=result['metadata'].get('source_row_checksum'):
            raise RuntimeIntegrityError('Mutable import destination changed after its migration; choose a new dataset name')
        return {**result,'idempotent_reuse':True}
    source=sqlite3.connect('file:'+quote(str(path),safe='/')+'?mode=ro',uri=True)
    try:
        source.execute('PRAGMA query_only=ON');source.execute('BEGIN')
        if source.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or source.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeIntegrityError('Legacy SQLite integrity or foreign keys do not verify')
        objects=source.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,rowid").fetchall()
        script=';\n'.join(sql.rstrip(';') for kind,name,sql in objects if kind in {'table','index','trigger'})+';'
        connection=create_dataset(script,dataset,role)
        try:
            wanted=hashlib.sha256();counts={}
            # Source FK graphs include a paper/gate cycle; all rows enter one
            # private building version, followed by explicit orphan validation.
            with connection.raw.cursor() as cursor:cursor.execute('SET SESSION foreign_key_checks=0')
            for name,table in sorted(connection.tables.items()):
                columns=[c['name'] for c in table['columns']]
                wanted.update(_json([name,columns]).encode()+b'\n')
                rows=source.execute('SELECT rowid,'+','.join('"'+c+'"' for c in columns)+' FROM "'+name+'" ORDER BY rowid').fetchall();counts[name]=len(rows)
                for row in rows:wanted.update(_json(list(row)).encode()+b'\n')
                destination_columns=([ORDINAL] if table['extra_ordinal'] else [])+columns
                values=[tuple(row) if table['extra_ordinal'] else tuple(row[1:]) for row in rows]
                query='INSERT INTO '+_identifier(table['physical'])+'('+','.join(_identifier(c) for c in destination_columns)+') VALUES('+','.join('%s' for _ in destination_columns)+')'
                with connection.raw.cursor() as cursor:
                    for start in range(0,len(values),500):cursor.executemany(query,values[start:start+500])
            with connection.raw.cursor() as cursor:cursor.execute('SET SESSION foreign_key_checks=1')
            actual,actual_counts=_fingerprint(connection)
            if actual!=wanted.hexdigest() or actual_counts!=counts:raise RuntimeIntegrityError('MySQL imported rows differ from the original SQLite logical rows or ordering')
            if _sha(path)!=source_sha:raise RuntimeIntegrityError('Legacy SQLite source changed during import')
            result=finalize_dataset(connection,{'source_format':'sqlite_legacy_immutable','source_row_checksum':wanted.hexdigest()},source_sha256=source_sha)
            return {**result,'idempotent_reuse':False}
        except BaseException:connection.rollback();raise
        finally:connection.close()
    finally:source.close()
