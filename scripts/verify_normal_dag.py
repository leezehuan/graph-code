import asyncio
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from lib.db import create_async_pool
from lib.dag_scheduler import DAGScheduler
from lib.message_hub import RocketMQMessageBus, OutboxDispatcher


def child(runtime, events, stop, release_bytearray, release_osc8):
    import lib.sub_agent as worker

    async def no_scan(self, **kwargs):
        return []  # MQ notifications must be the only source of claims.

    async def execute(**kwargs):
        scheduler, claim = kwargs['scheduler'], kwargs['claim']
        name = claim.task_id.split('-')[0]
        identity = dict(task_id=claim.task_id, execution_id=claim.execution_id,
                        attempt=claim.attempt, runtime_id=runtime)
        assert await scheduler.mark_execution_started(**identity)
        events.put(('started', name, runtime))
        if name in ('bytearray', 'osc8'):
            barrier = release_bytearray if name == 'bytearray' else release_osc8
            assert await asyncio.to_thread(barrier.wait, 60)
        assert await scheduler.complete_execution(**identity, summary='deterministic smoke')
        events.put(('completed', name, runtime))

    worker.DAGScheduler.get_ready_shards = no_scan
    worker._run_claimed_execution = execute

    class Bus(RocketMQMessageBus):
        async def subscribe(self, *args, **kwargs):
            await super().subscribe(*args, **kwargs)
            if len(self._subscriptions) == 4:
                events.put(('ready', runtime, os.getpid()))

    async def run():
        pool = create_async_pool()
        await pool.open()
        bus = Bus(pool)
        task = None
        try:
            await bus.setup()
            task = asyncio.create_task(worker.run_sub_agent(
                runtime_id=runtime, llm=None, checkpointer=None,
                message_bus=bus, work_dir=ROOT))
            while not stop.is_set():
                if task.done():
                    await task
                await asyncio.sleep(.1)
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await bus.close()
            await pool.close()

    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run())
    except Exception as exc:
        events.put(('error', runtime, repr(exc)))
        raise


async def main():
    from lib.lead_agent_tools import create_lead_agent_tools
    suffix = uuid4().hex
    thread = 'normal-smoke-' + suffix
    card = 'card-' + suffix
    names = ['analyze', 'bytearray', 'osc8', 'verify']
    ids = {name: name + '-' + suffix for name in names}
    pool = create_async_pool()
    await pool.open()
    bus = RocketMQMessageBus(pool)
    context = mp.get_context('spawn')
    events, stop = context.Queue(), context.Event()
    release_a, release_b = context.Event(), context.Event()
    processes = [context.Process(target=child, args=(f'runtime-{i:03}', events, stop, release_a, release_b)) for i in range(1,4)]
    dispatcher = None
    try:
        await DAGScheduler(pool).setup()
        async with pool.connection() as conn:
            await conn.execute("""INSERT INTO agent_cards
                (agent_card_id,version,status,task_types,system_prompt,tool_allowlist,
                 skill_allowlist,runtime_config,bundle_ref,bundle_digest,config_hash)
                VALUES (%s,'1','active','["general"]','test','[]','[]','{}','builtin:test','test','test')""", [card])
        await bus.setup()
        for p in processes:
            p.start()
        seen = []

        async def until(predicate):
            async with asyncio.timeout(70):
                while not predicate():
                    try:
                        item = events.get_nowait()
                        print(item, flush=True)
                        assert item[0] != 'error', item
                        seen.append(item)
                    except queue.Empty:
                        await asyncio.sleep(.05)

        await until(lambda: len([e for e in seen if e[0] == 'ready']) == 3)
        tasks = [dict(id=ids[name], subject=name, task_type='general', work_shard=i,
                      blockedBy=[ids[d] for d in ([],['analyze'],['analyze'],['bytearray','osc8'])[i]],
                      card_selector={'agent_card_id':card,'version':'1'}) for i,name in enumerate(names)]
        publish = next(t for t in create_lead_agent_tools(bus, {'_thread_id':thread}) if t.name == 'publish_dag')
        assert await publish.ainvoke({'dag_json':json.dumps({'tasks':tasks})}) == 'Published 4 tasks to board'
        dispatcher = asyncio.create_task(OutboxDispatcher(pool,bus).run())
        await until(lambda: {'bytearray','osc8'} <= {e[1] for e in seen if e[0] == 'started'})
        owners = {e[1]: e[2] for e in seen if e[0] == 'started'}
        assert owners['bytearray'] != owners['osc8']

        async def verify_blocked():
            async with pool.connection() as conn:
                row = await (await conn.execute('SELECT status, blocked_by_count FROM tasks WHERE id=%s', [ids['verify']])).fetchone()
                assert row['status'] == 'pending' and row['blocked_by_count'] > 0, row
        await verify_blocked()
        release_a.set()
        await until(lambda: any(e[:2] == ('completed','bytearray') for e in seen))
        await verify_blocked()
        release_b.set()
        await until(lambda: any(e[:2] == ('completed','verify') for e in seen))
        assert sorted(e[1] for e in seen if e[0] == 'started') == sorted(names)
        async with asyncio.timeout(20):
            while True:
                async with pool.connection() as conn:
                    rows = await (await conn.execute("SELECT status,count(*) AS n FROM message_outbox WHERE envelope->>'thread_id'=%s GROUP BY status", [thread])).fetchall()
                if rows and all(r['status'] == 'published' for r in rows):
                    break
                await asyncio.sleep(.2)
        async with pool.connection() as conn:
            acked = await (await conn.execute("""SELECT count(*) AS n FROM message_consumer_inbox i JOIN message_outbox o USING(event_id)
                WHERE o.envelope->>'thread_id'=%s AND o.tag='task_available' AND i.result='consumed'""", [thread])).fetchone()
            assert acked['n'] == 4, acked
        print('PASS: 3 processes, one publish_dag, four unique notification claims, parallel branches, verify barrier, all Outbox published, 4 work ACKs', flush=True)
    finally:
        stop.set(); release_a.set(); release_b.set()
        for p in processes:
            if p.pid:
                await asyncio.to_thread(p.join, 15)
                if p.is_alive():
                    p.terminate(); p.join()
        if dispatcher:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        await bus.close()
        async with pool.connection() as conn:
            await conn.execute('UPDATE tasks SET current_execution_id=NULL,current_attempt=NULL WHERE thread_id=%s',[thread])
            await conn.execute('DELETE FROM task_execution_events WHERE task_id=ANY(%s)',[list(ids.values())])
            await conn.execute('UPDATE agent_runtimes SET current_execution_id=NULL,current_attempt=NULL WHERE current_execution_id IN (SELECT execution_id FROM task_executions WHERE thread_id=%s)',[thread])
            await conn.execute('DELETE FROM task_executions WHERE thread_id=%s',[thread])
            await conn.execute('DELETE FROM tasks WHERE thread_id=%s',[thread])
            await conn.execute("DELETE FROM message_consumer_inbox WHERE event_id IN (SELECT event_id FROM message_outbox WHERE envelope->>'thread_id'=%s)",[thread])
            await conn.execute("DELETE FROM message_delivery_audit WHERE event_id IN (SELECT event_id FROM message_outbox WHERE envelope->>'thread_id'=%s)",[thread])
            await conn.execute("DELETE FROM message_outbox WHERE envelope->>'thread_id'=%s",[thread])
            await conn.execute('DELETE FROM agent_cards WHERE agent_card_id=%s',[card])
        await pool.close()


if __name__ == '__main__':
    load_dotenv(ROOT / '.env')
    os.environ['TASK_WORK_SHARD_COUNT'] = '4'
    os.environ['POSTGRES_POOL_MIN_SIZE'] = '1'
    os.environ['POSTGRES_POOL_MAX_SIZE'] = '8'
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
