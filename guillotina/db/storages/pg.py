import asyncio
import concurrent
import logging
import time
from asyncio import shield

import asyncpg
import asyncpg.connection
import ujson
from guillotina._settings import app_settings
from guillotina.db import TRASHED_ID
from guillotina.db.interfaces import IPostgresStorage
from guillotina.db.oid import MAX_OID_LENGTH
from guillotina.db.storages.base import BaseStorage
from guillotina.db.storages.utils import SQLStatements
from guillotina.db.storages.utils import get_table_definition
from guillotina.db.storages.utils import register_sql
from guillotina.exceptions import ConflictError
from guillotina.exceptions import ConflictIdOnContainer
from guillotina.exceptions import TIDConflictError
from guillotina.profile import profilable
from zope.interface import implementer


log = logging.getLogger("guillotina.storage")


# we can not use FOR UPDATE or FOR SHARE unfortunately because
# it can cause deadlocks on the database--we need to resolve them ourselves
register_sql('GET_OID', f"""
SELECT zoid, tid, state_size, resource, of, parent_id, id, type, state
FROM {{table_name}}
WHERE zoid = $1::varchar({MAX_OID_LENGTH})
""")

register_sql('GET_CHILDREN_KEYS', f"""
SELECT id
FROM {{table_name}}
WHERE parent_id = $1::varchar({MAX_OID_LENGTH})
""")


register_sql('GET_ANNOTATIONS_KEYS', f"""
SELECT id, parent_id
FROM {{table_name}}
WHERE of = $1::varchar({MAX_OID_LENGTH})
""")

register_sql('GET_CHILD', f"""
SELECT zoid, tid, state_size, resource, type, state, id
FROM {{table_name}}
WHERE parent_id = $1::varchar({MAX_OID_LENGTH}) AND id = $2::text
""")

register_sql('GET_CHILDREN_BATCH', f"""
SELECT zoid, tid, state_size, resource, type, state, id
FROM {{table_name}}
WHERE parent_id = $1::varchar({MAX_OID_LENGTH}) AND id = ANY($2)
""")

register_sql('EXIST_CHILD', f"""
SELECT zoid
FROM {{table_name}}
WHERE parent_id = $1::varchar({MAX_OID_LENGTH}) AND id = $2::text
""")


register_sql('HAS_OBJECT', f"""
SELECT zoid
FROM {{table_name}}
WHERE zoid = $1::varchar({MAX_OID_LENGTH})
""")


register_sql('GET_ANNOTATION', f"""
SELECT zoid, tid, state_size, resource, type, state, id, parent_id
FROM {{table_name}}
WHERE
    of = $1::varchar({MAX_OID_LENGTH}) AND
    id = $2::text
""")

def _wrap_return_count(txt):
    return """WITH rows AS (
{}
    RETURNING 1
)
SELECT count(*) FROM rows""".format(txt)


# upsert without checking matching tids on updated object
NAIVE_UPSERT = f"""
INSERT INTO {{table_name}}
(zoid, tid, state_size, part, resource, of, otid, parent_id, id, type, json, state)
VALUES ($1::varchar({MAX_OID_LENGTH}), $2::int, $3::int, $4::int, $5::boolean,
        $6::varchar({MAX_OID_LENGTH}), $7::int, $8::varchar({MAX_OID_LENGTH}),
        $9::text, $10::text, $11::json, $12::bytea)
ON CONFLICT (zoid)
DO UPDATE SET
    tid = EXCLUDED.tid,
    state_size = EXCLUDED.state_size,
    part = EXCLUDED.part,
    resource = EXCLUDED.resource,
    of = EXCLUDED.of,
    otid = EXCLUDED.otid,
    parent_id = EXCLUDED.parent_id,
    id = EXCLUDED.id,
    type = EXCLUDED.type,
    json = EXCLUDED.json,
    state = EXCLUDED.state"""
register_sql('UPSERT', _wrap_return_count(NAIVE_UPSERT + """
WHERE
    tid = EXCLUDED.otid"""))
register_sql('NAIVE_UPSERT', _wrap_return_count(NAIVE_UPSERT))


# update without checking matching tids on updated object
NAIVE_UPDATE = f"""
UPDATE {{table_name}}
SET
    tid = $2::int,
    state_size = $3::int,
    part = $4::int,
    resource = $5::boolean,
    of = $6::varchar({MAX_OID_LENGTH}),
    otid = $7::int,
    parent_id = $8::varchar({MAX_OID_LENGTH}),
    id = $9::text,
    type = $10::text,
    json = $11::json,
    state = $12::bytea
WHERE
    zoid = $1::varchar({MAX_OID_LENGTH})"""
register_sql(
    'UPDATE', _wrap_return_count(NAIVE_UPDATE + """ AND tid = $7::int"""))
register_sql('NAIVE_UPDATE', _wrap_return_count(NAIVE_UPDATE))


NEXT_TID = "SELECT nextval('tid_sequence');"
MAX_TID = "SELECT last_value FROM tid_sequence;"


register_sql(
    'NUM_CHILDREN',
    f"SELECT count(*) FROM {{table_name}} WHERE parent_id = $1::varchar({MAX_OID_LENGTH})")


register_sql('NUM_ROWS', "SELECT count(*) FROM {table_name}")


register_sql('NUM_RESOURCES', "SELECT count(*) FROM {table_name} WHERE resource is TRUE")

register_sql(
    'NUM_RESOURCES_BY_TYPE',
    "SELECT count(*) FROM {table_name} WHERE type=$1::TEXT")

register_sql('RESOURCES_BY_TYPE', """
SELECT zoid, tid, state_size, resource, type, state, id
FROM {table_name}
WHERE type=$1::TEXT
ORDER BY zoid
LIMIT $2::int
OFFSET $3::int
""")


register_sql('GET_CHILDREN', f"""
SELECT zoid, tid, state_size, resource, type, state, id
FROM {{table_name}}
WHERE parent_id = $1::VARCHAR({MAX_OID_LENGTH})
""")


register_sql('TRASH_PARENT_ID', f"""
UPDATE {{table_name}}
SET
    parent_id = '{TRASHED_ID}'
WHERE
    zoid = $1::varchar({MAX_OID_LENGTH})
""")


register_sql('INSERT_BLOB_CHUNK', f"""
INSERT INTO {{table_name}}
(bid, zoid, chunk_index, data)
VALUES ($1::VARCHAR({MAX_OID_LENGTH}), $2::VARCHAR({MAX_OID_LENGTH}),
        $3::INT, $4::BYTEA)
""")


register_sql('READ_BLOB_CHUNK', f"""
SELECT * from {{table_name}}
WHERE bid = $1::VARCHAR({MAX_OID_LENGTH})
AND chunk_index = $2::int
""")


register_sql('DELETE_BLOB', f"""
DELETE FROM {{table_name}} WHERE bid = $1::VARCHAR({MAX_OID_LENGTH});
""")


TXN_CONFLICTS = """
    SELECT zoid, tid, state_size, resource, type, id
    FROM {table_name}
    WHERE tid > $1"""
register_sql('TXN_CONFLICTS', TXN_CONFLICTS)

register_sql('TXN_CONFLICTS_ON_OIDS', TXN_CONFLICTS + ' AND zoid = ANY($2)')


register_sql('BATCHED_GET_CHILDREN_KEYS', f"""
SELECT id
FROM {{table_name}}
WHERE parent_id = $1::varchar({MAX_OID_LENGTH})
ORDER BY zoid
LIMIT $2::int
OFFSET $3::int
""")

register_sql('DELETE_OBJECT', f"""
DELETE FROM {{table_name}}
WHERE zoid = $1::varchar({MAX_OID_LENGTH});
""")

register_sql('GET_TRASHED_OBJECTS', f"""
SELECT zoid from {{table_name}} where parent_id = '{TRASHED_ID}';
""")

register_sql('CREATE_TRASH', f'''
INSERT INTO {{table_name}} (zoid, tid, state_size, part, resource, type)
SELECT '{TRASHED_ID}', 0, 0, 0, FALSE, 'TRASH_REF'
WHERE NOT EXISTS (SELECT * FROM {{table_name}} WHERE zoid = '{TRASHED_ID}')
RETURNING id;
''')


# how long to wait before trying to recover bad connections
BAD_CONNECTION_RESTART_DELAY = 0.25


class LightweightConnection(asyncpg.connection.Connection):
    '''
    See asyncpg.connection.Connection._get_reset_query to see
    details of the point of this.
    '''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # we purposefully do not support these options for performance
        self._server_caps = asyncpg.connection.ServerCapabilities(
            advisory_locks=False,
            notifications=False,
            sql_reset=False,
            sql_close_all=False,
            plpgsql=self._server_caps.plpgsql
        )

    async def add_listener(self, channel, callback):
        raise NotImplementedError('Does not support listeners')

    async def remove_listener(self, channel, callback):
        raise NotImplementedError('Does not support listeners')


class PGVacuum:

    def __init__(self, storage, loop):
        self._storage = storage
        self._loop = loop
        self._queue = asyncio.Queue(loop=loop)
        self._closed = False
        self._active = False

    async def initialize(self):
        while not self._closed:
            try:
                await self._initialize()
            except (concurrent.futures.CancelledError, RuntimeError):
                # we're okay with the task getting cancelled
                return

    async def _initialize(self):
        # get existing trashed objects, push them on the queue...
        # there might be contention, but that is okay
        conn = None
        try:
            conn = await self._storage.open()
            sql = self._storage._sql.get(
                'GET_TRASHED_OBJECTS', self._storage._objects_table_name)
            for record in await conn.fetch(sql):
                self._queue.put_nowait(record['zoid'])
        except concurrent.futures.TimeoutError:
            log.info('Timed out connecting to storage')
        except Exception:
            log.warning('Error deleting trashed object', exc_info=True)
        finally:
            if conn is not None:
                await self._storage.close(conn)

        while not self._closed:
            oid = None
            try:
                oid = await self._queue.get()
                self._active = True
                await self.vacuum(oid)
            except (concurrent.futures.CancelledError, RuntimeError):
                raise
            except Exception:
                log.warning(f'Error vacuuming oid {oid}', exc_info=True)
            finally:
                self._active = False
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

    async def add_to_queue(self, oid):
        if self._closed:
            raise Exception('Closing down')
        await self._queue.put(oid)

    async def vacuum(self, oid):
        '''
        DELETED objects has parent id changed to the trashed ob for the oid...
        '''
        conn = await self._storage.open()
        sql = self._storage._sql.get(
            'DELETE_OBJECT', self._storage._objects_table_name)
        try:
            await conn.execute(sql, oid)
        except Exception:
            log.warning('Error deleting trashed object', exc_info=True)
        finally:
            try:
                await self._storage.close(conn)
            except asyncpg.exceptions.ConnectionDoesNotExistError:
                pass

    async def finalize(self):
        self._closed = True
        await self._queue.join()


@implementer(IPostgresStorage)
class PostgresqlStorage(BaseStorage):
    """Storage to a relational database, based on invalidation polling"""

    _dsn = None
    _partition_class = None
    _pool_size = None
    _pool = None
    _large_record_size = 1 << 24
    _vacuum_class = PGVacuum
    _objects_table_name = 'objects'
    _blobs_table_name = 'blobs'

    _object_schema = {
        'zoid': f'VARCHAR({MAX_OID_LENGTH}) NOT NULL PRIMARY KEY',
        'tid': 'BIGINT NOT NULL',
        'state_size': 'BIGINT NOT NULL',
        'part': 'BIGINT NOT NULL',
        'resource': 'BOOLEAN NOT NULL',
        'of': f'VARCHAR({MAX_OID_LENGTH}) REFERENCES {{objects_table_name}} ON DELETE CASCADE',
        'otid': 'BIGINT',
        'parent_id': f'VARCHAR({MAX_OID_LENGTH}) REFERENCES {{objects_table_name}} ON DELETE CASCADE',  # noqa
        'id': 'TEXT',
        'type': 'TEXT NOT NULL',
        'json': 'JSONB',
        'state': 'BYTEA'
    }

    _blob_schema = {
        'bid': f'VARCHAR({MAX_OID_LENGTH}) NOT NULL',
        'zoid': f'VARCHAR({MAX_OID_LENGTH}) NOT NULL REFERENCES {{objects_table_name}} ON DELETE CASCADE',
        'chunk_index': 'INT NOT NULL',
        'data': 'BYTEA'
    }

    _initialize_statements = [
        'CREATE INDEX IF NOT EXISTS {object_table_name}_tid ON {objects_table_name} (tid);',
        'CREATE INDEX IF NOT EXISTS {object_table_name}_of ON {objects_table_name} (of);',
        'CREATE INDEX IF NOT EXISTS {object_table_name}_part ON {objects_table_name} (part);',
        'CREATE INDEX IF NOT EXISTS {object_table_name}_parent ON {objects_table_name} (parent_id);',
        'CREATE INDEX IF NOT EXISTS {object_table_name}_id ON {objects_table_name} (id);',
        'CREATE INDEX IF NOT EXISTS {object_table_name}_type ON {objects_table_name} (type);',
        'CREATE INDEX IF NOT EXISTS {blob_table_name}_bid ON {blobs_table_name} (bid);',
        'CREATE INDEX IF NOT EXISTS {blob_table_name}_zoid ON {blobs_table_name} (zoid);',
        'CREATE INDEX IF NOT EXISTS {blob_table_name}_chunk ON {blobs_table_name} (chunk_index);',
        'CREATE SEQUENCE IF NOT EXISTS tid_sequence;'
    ]

    _unique_constraint = '''ALTER TABLE {objects_table_name}
                            ADD CONSTRAINT {objects_table_name}_parent_id_id_key
                            UNIQUE (parent_id, id)'''

    def __init__(self, dsn=None, partition=None, read_only=False, name=None,
                 pool_size=13, transaction_strategy='resolve_readcommitted',
                 conn_acquire_timeout=20, cache_strategy='dummy',
                 objects_table_name='objects', blobs_table_name='blobs', **options):
        super(PostgresqlStorage, self).__init__(
            read_only, transaction_strategy=transaction_strategy,
            cache_strategy=cache_strategy)
        self._dsn = dsn
        self._pool_size = pool_size
        self._partition_class = partition
        self._read_only = read_only
        self.__name__ = name
        self._read_conn = None
        self._lock = asyncio.Lock()
        self._conn_acquire_timeout = conn_acquire_timeout
        self._options = options
        self._connection_options = {}
        self._connection_initialized_on = time.time()
        self._objects_table_name = objects_table_name
        self._blobs_table_name = blobs_table_name
        self._sql = SQLStatements()

    async def finalize(self):
        await self._vacuum.finalize()
        self._vacuum_task.cancel()
        pool = await self.get_pool()
        try:
            await shield(pool.release(self._read_conn))
        except asyncpg.exceptions.InterfaceError:
            pass
        # terminate force closes all these
        # this step is happening at the end of application shutdown and
        # connections should not be staying open at this step
        pool.terminate()

    async def create(self):
        # Check DB
        log.info('Creating initial database objects')
        statements = [
            get_table_definition(self._objects_table_name, self._object_schema),
            get_table_definition(self._blobs_table_name, self._blob_schema,
                                 primary_keys=('bid', 'zoid', 'chunk_index'))
        ]
        statements.extend(self._initialize_statements)

        for statement in statements:
            otable_name = self._objects_table_name
            if otable_name == 'objects':
                otable_name = 'object'
            btable_name = self._blobs_table_name
            if btable_name == 'blobs':
                btable_name = 'blob'
            statement = statement.format(
                objects_table_name=self._objects_table_name,
                blobs_table_name=self._blobs_table_name,
                # singular, index names
                object_table_name=otable_name,
                blob_table_name=btable_name,
            )
            try:
                await self._read_conn.execute(statement)
            except asyncpg.exceptions.UniqueViolationError:
                # this is okay on creation, means 2 getting created at same time
                pass

        await self.initialize_tid_statements()

    async def restart_connection(self, timeout=0.1):
        log.error('Connection potentially lost to pg, restarting')
        pool = await self.get_pool()
        try:
            await asyncio.wait_for(pool.close(), timeout)
        except asyncio.TimeoutError:
            pass
        pool.terminate()
        # re-bind, throw conflict error so the request is restarted...
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            max_size=self._pool_size,
            min_size=2,
            loop=self._pool._loop,
            connection_class=app_settings['pg_connection_class'],
            **self._connection_options)

        # shared read connection on all transactions
        self._read_conn = await self.open()
        await self.initialize_tid_statements()
        self._connection_initialized_on = time.time()
        raise ConflictError('Restarting connection to postgresql')

    async def has_unique_constraint(self):
        result = await self._read_conn.fetch('''
SELECT *
FROM
    information_schema.table_constraints AS tc
    JOIN information_schema.key_column_usage AS kcu
    ON tc.constraint_name = kcu.constraint_name
    JOIN information_schema.constraint_column_usage AS ccu
    ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_name = '{}_parent_id_id_key' AND tc.constraint_type = 'UNIQUE'
'''.format(self._objects_table_name))
        return len(result) > 0

    async def initialize(self, loop=None, **kw):
        self._connection_options = kw
        if loop is None:
            loop = asyncio.get_event_loop()
        await self.get_pool(loop)  # initialize

        # shared read connection on all transactions
        self._read_conn = await self.open()
        if await self.has_unique_constraint():
            self._supports_unique_constraints = True

        trash_sql = self._sql.get('CREATE_TRASH', self._objects_table_name)
        try:
            await self.initialize_tid_statements()
            await self._read_conn.execute(trash_sql)
        except asyncpg.exceptions.ReadOnlySQLTransactionError:
            # Not necessary for read-only pg
            pass
        except asyncpg.exceptions.UndefinedTableError:
            await self.create()
            # only available on new databases
            await self._read_conn.execute(self._unique_constraint.format(
                objects_table_name=self._objects_table_name
            ))
            self._supports_unique_constraints = True
            await self.initialize_tid_statements()
            await self._read_conn.execute(trash_sql)

        # migrate to larger VARCHAR size...
        result = await self._read_conn.fetch("""
select * from information_schema.columns
where table_name='{}'""".format(self._objects_table_name))
        if len(result) > 0 and result[0]['character_maximum_length'] != MAX_OID_LENGTH:
            log.warn('Migrating VARCHAR key length')
            await self._read_conn.execute(f'''
ALTER TABLE {self._objects_table_name} ALTER COLUMN zoid TYPE varchar({MAX_OID_LENGTH})''')
            await self._read_conn.execute(f'''
ALTER TABLE {self._objects_table_name} ALTER COLUMN of TYPE varchar({MAX_OID_LENGTH})''')
            await self._read_conn.execute(f'''
ALTER TABLE {self._objects_table_name} ALTER COLUMN parent_id TYPE varchar({MAX_OID_LENGTH})''')
            await self._read_conn.execute(f'''
ALTER TABLE {self._blobs_table_name} ALTER COLUMN bid TYPE varchar({MAX_OID_LENGTH})''')
            await self._read_conn.execute(f'''
ALTER TABLE {self._blobs_table_name} ALTER COLUMN zoid TYPE varchar({MAX_OID_LENGTH})''')

        self._vacuum = self._vacuum_class(self, loop)
        self._vacuum_task = asyncio.Task(self._vacuum.initialize(), loop=loop)

        def vacuum_done(task):
            if self._vacuum._closed:
                # if it's closed, we know this is expected
                return
            log.warning('Vacuum pg task ended. This should not happen. '
                        'No database vacuuming will be done here anymore.')

        self._vacuum_task.add_done_callback(vacuum_done)
        self._connection_initialized_on = time.time()

    async def get_pool(self, loop=None, **kw):
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                max_size=self._pool_size,
                min_size=2,
                connection_class=app_settings['pg_connection_class'],
                loop=loop,
                **kw)
        return self._pool

    async def initialize_tid_statements(self):
        self._stmt_next_tid = await self._read_conn.prepare(NEXT_TID)
        self._stmt_max_tid = await self._read_conn.prepare(MAX_TID)

    async def remove(self):
        """Reset the tables"""
        async with (await self.get_pool()).acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS {};".format(self._blobs_table_name))
            await conn.execute("DROP TABLE IF EXISTS {};".format(self._objects_table_name))

    async def open(self):
        pool = await self.get_pool()
        try:
            conn = await pool.acquire(timeout=self._conn_acquire_timeout)
            return conn
        except asyncpg.exceptions.InterfaceError as ex:
            async with self._lock:
                await self._check_bad_connection(ex)

    async def close(self, con):
        try:
            await shield(
                asyncio.wait_for((await self.get_pool()).release(con, timeout=1), 1))
        except (asyncio.CancelledError, RuntimeError, asyncio.TimeoutError,
                asyncpg.exceptions.ConnectionDoesNotExistError):
            pass

    async def terminate(self, conn):
        conn.terminate()

    async def load(self, txn, oid):
        sql = self._sql.get('GET_OID', self._objects_table_name)
        async with txn._lock:
            objects = await self.get_one_row(txn, sql, oid)
        if objects is None:
            raise KeyError(oid)
        return objects

    @profilable
    async def store(self, oid, old_serial, writer, obj, txn):
        assert oid is not None

        pickled = writer.serialize()  # This calls __getstate__ of obj
        if len(pickled) >= self._large_record_size:
            log.info(f"Large object {obj.__class__}: {len(pickled)}")
        json_dict = await writer.get_json()
        json = ujson.dumps(json_dict)
        part = writer.part
        if part is None:
            part = 0

        update = False
        statement_sql = self._sql.get('NAIVE_UPSERT', self._objects_table_name)
        if not obj.__new_marker__ and obj._p_serial is not None:
            # we should be confident this is an object update
            statement_sql = self._sql.get('UPDATE', self._objects_table_name)
            update = True

        conn = await txn.get_connection()
        async with txn._lock:
            try:
                result = await conn.fetch(
                    statement_sql,
                    oid,                 # The OID of the object
                    txn._tid,            # Our TID
                    len(pickled),        # Len of the object
                    part,                # Partition indicator
                    writer.resource,     # Is a resource ?
                    writer.of,           # It belogs to a main
                    old_serial,          # Old serial
                    writer.parent_id,    # Parent OID
                    writer.id,           # Traversal ID
                    writer.type,         # Guillotina type
                    json,                # JSON catalog
                    pickled              # Pickle state)
                )
            except asyncpg.exceptions.UniqueViolationError as ex:
                if 'Key (parent_id, id)' in ex.detail:
                    raise ConflictIdOnContainer(ex)
                raise
            except asyncpg.exceptions.ForeignKeyViolationError:
                txn.deleted[obj._p_oid] = obj
                raise TIDConflictError(
                    f'Bad value inserting into database that could be caused '
                    f'by a bad cache value. This should resolve on request retry.',
                    oid, txn, old_serial, writer)
            except asyncpg.exceptions._base.InterfaceError as ex:
                if 'another operation is in progress' in ex.args[0]:
                    raise ConflictError(
                        f'asyncpg error, another operation in progress.',
                        oid, txn, old_serial, writer)
                raise
            except asyncpg.exceptions.DeadlockDetectedError:
                raise ConflictError(f'Deadlock detected.',
                                    oid, txn, old_serial, writer)
            if len(result) != 1 or result[0]['count'] != 1:
                if update:
                    # raise tid conflict error
                    raise TIDConflictError(
                        f'Mismatch of tid of object being updated. This is likely '
                        f'caused by a cache invalidation race condition and should '
                        f'be an edge case. This should resolve on request retry.',
                        oid, txn, old_serial, writer)
                else:
                    log.error('Incorrect response count from database update. '
                              'This should not happen. tid: {}'.format(txn._tid))
        await txn._cache.store_object(obj, pickled)

    async def _txn_oid_commit_hook(self, status, oid):
        await self._vacuum.add_to_queue(oid)

    async def delete(self, txn, oid):
        conn = await txn.get_connection()
        sql = self._sql.get('TRASH_PARENT_ID', self._objects_table_name)
        async with txn._lock:
            # for delete, we reassign the parent id and delete in the vacuum task
            await conn.execute(sql, oid)
        txn.add_after_commit_hook(self._txn_oid_commit_hook, oid)

    async def _check_bad_connection(self, ex):
        if str(ex) in ('cannot perform operation: connection is closed',
                       'connection is closed', 'pool is closed'):
            if (time.time() - self._connection_initialized_on) > BAD_CONNECTION_RESTART_DELAY:
                # we need to make sure we aren't calling this over and over again
                return await self.restart_connection()

    async def get_next_tid(self, txn):
        async with self._lock:
            # we do not use transaction lock here but a storage lock because
            # a storage object has a shard conn for reads
            try:
                return await self._stmt_next_tid.fetchval()
            except asyncpg.exceptions.InterfaceError as ex:
                await self._check_bad_connection(ex)
                raise

    async def get_current_tid(self, txn):
        async with self._lock:
            # again, use storage lock here instead of trns lock
            return await self._stmt_max_tid.fetchval()

    async def get_one_row(self, txn, sql, *args, prepare=False):
        conn = await txn.get_connection()
        # Helper function to provide easy adaptation to cockroach
        if prepare:
            # latest version of asyncpg has prepare bypassing statement cache
            smt = await conn.prepare(sql)
            return await smt.fetchrow(*args)
        else:
            return await conn.fetchrow(sql, *args)

    def _db_transaction_factory(self, txn):
        # make sure asycpg knows this is a new transaction
        if txn._db_conn._con is not None:
            txn._db_conn._con._top_xact = None
        return txn._db_conn.transaction(readonly=txn._manager._storage._read_only)

    async def start_transaction(self, txn, retries=0):
        error = None
        conn = await txn.get_connection()
        async with txn._lock:
            try:
                txn._db_txn = self._db_transaction_factory(txn)
            except asyncpg.exceptions.InterfaceError as ex:
                async with self._lock:
                    await self._check_bad_connection(ex)
                raise
            try:
                await txn._db_txn.start()
                return
            except (asyncpg.exceptions.InterfaceError,
                    asyncpg.exceptions.InternalServerError) as ex:
                error = ex

        if error is not None:
            if retries > 2:
                raise error  # pylint: disable=E0702

            restart = rollback = False
            if isinstance(error, asyncpg.exceptions.InternalServerError):
                restart = True
                if error.sqlstate == 'XX000':
                    rollback = True
            elif ('manually started transaction' in error.args[0] or
                    'connection is closed' in error.args[0]):
                restart = True
                if 'manually started transaction' in error.args[0]:
                    rollback = True

            if rollback:
                try:
                    # thinks we're manually in txn, manually rollback and try again...
                    await conn.execute('ROLLBACK;')
                except asyncpg.exceptions._base.InterfaceError:
                    # we're okay with this error here...
                    pass
            if restart:
                await self.close(conn)
                txn._db_conn = await self.open()
                return await self.start_transaction(txn, retries + 1)

    async def get_conflicts(self, txn):
        async with self._lock:
            if len(txn.modified) == 0:
                return []
            # use storage lock instead of transaction lock
            if len(txn.modified) < 1000:
                # if it's too large, we're not going to check on object ids
                modified_oids = [k for k in txn.modified.keys()]
                sql = self._sql.get('TXN_CONFLICTS_ON_OIDS', self._objects_table_name)
                return await self._read_conn.fetch(
                    sql, txn._tid, modified_oids)
            else:
                sql = self._sql.get('TXN_CONFLICTS', self._objects_table_name)
                return await self._read_conn.fetch(sql, txn._tid)

    async def commit(self, transaction):
        if transaction._db_txn is not None:
            async with transaction._lock:
                await transaction._db_txn.commit()
        elif (self._transaction_strategy not in ('none', 'tidonly') and
                not transaction._skip_commit):
            log.warning('Do not have db transaction to commit')
        return transaction._tid

    async def abort(self, transaction):
        if transaction._db_txn is not None:
            async with transaction._lock:
                try:
                    await transaction._db_txn.rollback()
                except asyncpg.exceptions._base.InterfaceError:
                    # we're okay with this error here...
                    pass
        # reads don't need transaction necessarily so don't log
        # else:
        #     log.warning('Do not have db transaction to rollback')

    # Introspection
    async def get_page_of_keys(self, txn, oid, page=1, page_size=1000):
        conn = await txn.get_connection()
        keys = []
        sql = self._sql.get('BATCHED_GET_CHILDREN_KEYS', self._objects_table_name)
        for record in await conn.fetch(
                sql, oid, page_size, (page - 1) * page_size):
            keys.append(record['id'])
        return keys

    async def keys(self, txn, oid):
        conn = await txn.get_connection()
        sql = self._sql.get('GET_CHILDREN_KEYS', self._objects_table_name)
        async with txn._lock:
            result = await conn.fetch(sql, oid)
        return result

    async def get_child(self, txn, parent_oid, id):
        sql = self._sql.get('GET_CHILD', self._objects_table_name)
        async with txn._lock:
            result = await self.get_one_row(txn, sql, parent_oid, id)
        return result

    async def get_children(self, txn, parent_oid, ids):
        conn = await txn.get_connection()
        sql = self._sql.get('GET_CHILDREN_BATCH', self._objects_table_name)
        async with txn._lock:
            return await conn.fetch(sql, parent_oid, ids)

    async def has_key(self, txn, parent_oid, id):
        sql = self._sql.get('EXIST_CHILD', self._objects_table_name)
        async with txn._lock:
            result = await self.get_one_row(txn, sql, parent_oid, id)
        if result is None:
            return False
        else:
            return True

    async def len(self, txn, oid):
        conn = await txn.get_connection()
        sql = self._sql.get('NUM_CHILDREN', self._objects_table_name)
        async with txn._lock:
            result = await conn.fetchval(sql, oid)
        return result

    async def items(self, txn, oid):
        conn = await txn.get_connection()
        sql = self._sql.get('GET_CHILDREN', self._objects_table_name)
        async for record in conn.cursor(sql, oid):
            # locks are dangerous in cursors since comsuming code might do
            # sub-queries and they you end up with a deadlock
            yield record

    async def get_annotation(self, txn, oid, id):
        sql = self._sql.get('GET_ANNOTATION', self._objects_table_name)
        async with txn._lock:
            result = await self.get_one_row(txn, sql, oid, id, prepare=True)
            if result is not None and result['parent_id'] == TRASHED_ID:
                result = None
        return result

    async def get_annotation_keys(self, txn, oid):
        conn = await txn.get_connection()
        sql = self._sql.get('GET_ANNOTATIONS_KEYS', self._objects_table_name)
        async with txn._lock:
            result = await conn.fetch(sql, oid)
        items = []
        for item in result:
            if item['parent_id'] != TRASHED_ID:
                items.append(item)
        return items

    async def write_blob_chunk(self, txn, bid, oid, chunk_index, data):
        sql = self._sql.get('HAS_OBJECT', self._objects_table_name)
        async with txn._lock:
            result = await self.get_one_row(txn, sql, oid)
        if result is None:
            # check if we have a referenced ob, could be new and not in db yet.
            # if so, create a stub for it here...
            conn = await txn.get_connection()
            async with txn._lock:
                await conn.execute(f'''INSERT INTO {self._objects_table_name}
(zoid, tid, state_size, part, resource, type)
VALUES ($1::varchar({MAX_OID_LENGTH}), -1, 0, 0, TRUE, 'stub')''', oid)
        conn = await txn.get_connection()
        sql = self._sql.get('INSERT_BLOB_CHUNK', self._blobs_table_name)
        async with txn._lock:
            return await conn.execute(
                sql, bid, oid, chunk_index, data)

    async def read_blob_chunk(self, txn, bid, chunk=0):
        sql = self._sql.get('READ_BLOB_CHUNK', self._blobs_table_name)
        async with txn._lock:
            return await self.get_one_row(txn, sql, bid, chunk)

    async def read_blob_chunks(self, txn, bid):
        conn = await txn.get_connection()
        async for record in conn.cursor(bid):
            # locks are dangerous in cursors since comsuming code might do
            # sub-queries and they you end up with a deadlock
            yield record

    async def del_blob(self, txn, bid):
        conn = await txn.get_connection()
        sql = self._sql.get('DELETE_BLOB', self._blobs_table_name)
        async with txn._lock:
            await conn.execute(sql, bid)

    async def get_total_number_of_objects(self, txn):
        conn = await txn.get_connection()
        sql = self._sql.get('NUM_ROWS', self._objects_table_name)
        async with txn._lock:
            result = await conn.fetchval(sql)
        return result

    async def get_total_number_of_resources(self, txn):
        conn = await txn.get_connection()
        sql = self._sql.get('NUM_RESOURCES', self._objects_table_name)
        async with txn._lock:
            result = await conn.fetchval(sql)
        return result

    async def get_total_resources_of_type(self, txn, type_):
        conn = await txn.get_connection()
        sql = self._sql.get('NUM_RESOURCES_BY_TYPE', self._objects_table_name)
        async with txn._lock:
            result = await conn.fetchval(sql, type_)
        return result

    # Massive treatment without security
    async def _get_page_resources_of_type(self, txn, type_, page, page_size):
        conn = await txn.get_connection()
        async with txn._lock:
            keys = []
            sql = self._sql.get('RESOURCES_BY_TYPE', self._objects_table_name)
            for record in await conn.fetch(
                    sql, type_, page_size, (page - 1) * page_size):
                keys.append(record)
            return keys
