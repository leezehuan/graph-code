# LangCode - LangChain-based Agent Infrastructure for Task Decomposition and Parallel Execution

## Local dependencies

Start PostgreSQL, Redis, and a single-node RocketMQ broker with:

```bash
docker compose up -d
docker compose run --rm rocketmq-init
```

The services are available only on `127.0.0.1` and store their data in named
Docker volumes. The default local connection settings are:

```bash
export POSTGRES_HOST=127.0.0.1
export POSTGRES_PORT=5432
export POSTGRES_DB=langcode
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=postgres
```

Redis is exposed on `127.0.0.1:6380`; RocketMQ NameServer, Broker, and the V5
gRPC proxy are exposed on `127.0.0.1:9876`, `127.0.0.1:10911`, and
`127.0.0.1:8081`, respectively. Override any published port or PostgreSQL
setting with the corresponding environment variable when running `docker
compose`.

The second command must succeed before starting Lead/workers. It is safe to
repeat, including after a restart or on empty Docker volumes. It creates the
four NORMAL topics (`agent-command`, `task-event`, `permission-event`,
`agent-message`) and the consumer groups. No Lite topics or Lite subscriptions
are used. Groups have a `-normal` suffix to avoid old Lite group settings;
existing topics, messages and volumes are not deleted. Legacy Outbox
`lite_topic` values are ignored when publishing.

The default runtime IDs are `runtime-001`, `runtime-002`, `runtime-003`.
To initialize other IDs (the same command works in Bash and PowerShell):

```sh
docker compose run --rm -e LANGCODE_RUNTIME_IDS=runtime-001,runtime-002,runtime-003,runtime-004 rocketmq-init
```

After configuring `.env` with the local PostgreSQL settings and model
credentials, start the CLI from the project directory:

```sh
python cli.py
```

The CLI automatically starts `runtime-001`, `runtime-002`, and `runtime-003`
using the same Python environment and working directory. Worker output shares
the terminal; workers do not read CLI input. Do not start these same runtime
IDs manually while the CLI is running.

On normal exit, Ctrl+C, or an exception, the CLI terminates its workers and
waits up to five seconds per process, killing any that have not exited. On
Windows it also closes the Python launcher child process created by a virtual
environment. It does not manage externally started workers or restart failed
workers.

Run the SDK publish/receive/ack and 100 empty-inbox polls test (requires the
project Python environment; loads `.env`; a 120-second process timeout applies):

```sh
python -c "import os,sys,subprocess; from dotenv import load_dotenv; load_dotenv(); os.environ['LANGCODE_RUN_MQ_TESTS']='1'; subprocess.run([sys.executable,'-m','pytest','tests/test_rocketmq_integration.py','-q'],timeout=120,check=True)"
```

The application receive timeout is independent of the broker POP timeout.
The defaults are 5 seconds of broker polling plus 3 seconds of RPC margin.

With the three default runtime IDs stopped, run the deterministic four-node
DAG acceptance test (starts three worker processes itself; no LLM calls):

```sh
python -c "import subprocess,sys; subprocess.run([sys.executable,'scripts/verify_normal_dag.py'],timeout=150,check=True)"
```

It invokes Lead's `publish_dag` once, disables compensation scans in the test
processes, checks parallel execution of `bytearray`/`osc8`, blocks `verify`
until both finish, and checks published Outbox records and four work ACKs.

## Runtime workers

Lead and worker processes use PostgreSQL as the task and audit source of truth,
with RocketMQ as the asynchronous transport. The V5 Python client connects to
the proxy gRPC endpoint:

```bash
export ROCKETMQ_ENDPOINTS=127.0.0.1:8081
# The shard count must match the scheduler setting.
export TASK_WORK_SHARD_COUNT=64
python -m worker --runtime-id runtime-001
```

Workers are generic. They claim a ready task, load that task's frozen active
Agent Card, execute one attempt, then unload the Card before claiming more
work. The lead CLI runs the Outbox dispatcher; standalone workers also run one
so committed state changes are eventually published while the lead is offline.

## completed

- agent loop
- tool use
- permission check: (permission middleware (customized), deny/allow/ask)
- hooks: (middleware-based)
- todo write: (todo middleware)
- context compact + in-session memory (short-term memory): (async postgres checkpointer + context compression middleware (customized))
- memory: sematic (user preferences) + procedural (behavioral guidelines) + episodic (past experience), LLM-based retrieval, use files as indices for retrieval (long-term memory)
- system prompt: real-time assembly by the middleware sequence
- skill-loading：hot-pluggable, requiring no restart
- error recovery
- subagent
- task system
- background tasks
- agent teams
- autonomous agent

## in_progress
- cron scheduler
- worktree isolation
- mcp plugin

## Code graph and skill learning

The Lead has `code_graph`, `skills_list`, `skill_view`, `skill_manage`, and
`load_skill` tools. Workers receive only tools named in their frozen Agent Card.
The same Card's `skill_allowlist` restricts skill discovery, reads, and writes;
an empty list grants no skills. Existing Cards are not modified automatically.

### Local code graph

`code_graph` parses Python, C, and C++ using Tree-sitter. Its first call builds
the index; later calls hash files and refresh only changed or deleted files.
SQLite WAL and a cross-process file lock coordinate local Lead/Worker access.
The default cache is `~/.langcode/projects/<normalized-project-path-hash>/code-graph.sqlite`.
`LANGCODE_CACHE_DIR` overrides the `~/.langcode` cache root. Use a local disk,
not a shared network filesystem. Each checkout has its own index.

Example tool arguments:

```json
{"action": "search", "query": "claim_next_available_task", "mode": "fts"}
{"action": "query", "target": "DAGScheduler", "relation": "children_of"}
{"action": "impact", "changed_files": ["lib/dag_scheduler.py"]}
{"action": "overview"}
```

Search modes are `fts` (default), `semantic`, and `hybrid`. Supported relations
are `callers_of`, `callees_of`, `importers_of`, `tests_for`, `children_of`,
`inheritors_of`, and `references_to`. Search also accepts `kind` and
`context_files`; all actions accept `max_results` (1–100, default 20).
Impact includes dependencies up to two hops and preserves deleted-symbol
relationships within the current Git HEAD. With no `changed_files`, Git changes
are inspected. Responses retain `ok`, `action`, `summary`, `index`, and `data`,
or an `error` object. Ambiguous symbols require a qualified name.

FTS, relationship queries, impact, and overview make no embedding requests.
To enable semantic/hybrid search, configure these variables in the application's
environment or its startup `.env`:

```dotenv
LANGCODE_ACCEPT_CLOUD_EMBEDDINGS=1
LANGCODE_EMBEDDING_BASE_URL=https://api.example.com/v1
LANGCODE_EMBEDDING_MODEL=text-embedding-model
LANGCODE_EMBEDDING_API_KEY=your-key
```

The provider receives the search query and symbol metadata (name, qualified name,
kind, parent scope, relative file path, language), never function bodies or full
source files. The API key is optional for unauthenticated compatible endpoints.
Missing consent blocks both semantic and hybrid search. With consent, semantic
errors are returned explicitly; hybrid can fall back to FTS with a warning.
Vectors are cached by provider, model, symbol metadata, and file content hash.
Completed embedding batches survive a later batch failure. A busy graph returns
`graph_busy` after 30 seconds instead of blocking the event loop.

Programmatic callers use `CodeGraphService(work_dir, cache_dir=..., embeddings=...,
embedding_identity=...)` and `await service.execute(arguments)`. Injected providers
implement LangChain `Embeddings`, need a stable identity that changes with the
provider/model, and use the same explicit consent switch. Configuration is not
loaded from the repository being analyzed by the graph service itself.

### Skills and automatic review

Project skills live in `skills/<name>/SKILL.md` (optionally under a category);
user skills live in `~/.langcode/skills/`. Project names override user names.
Duplicate names within one source are reported as ambiguous. YAML frontmatter
requires `name` and `description`; `enabled: false` hides a skill. The middleware
injects only the catalog, and `skill_view` / `load_skill` load full instructions.
Changes are visible on the next model call without restarting.

`skill_manage` supports `create`, `edit`, `patch`, `delete`, `write_file`, and
`remove_file`. Attachments belong under `references/`, `templates/`, `scripts/`,
or `assets/`. Writes are atomic and protected by cross-process locks. Writes to
user-level skills require the existing permission approval flow. New skills are
always project-local.

After the Lead finishes normally, a background LangGraph workflow reviews the
conversation once 10 tool-calling **model rounds** have accumulated. Multiple
tools in one model response count as one round. Configure the threshold with
`LANGCODE_SKILL_REVIEW_INTERVAL`; set it to `0` to disable review. The workflow
uses `LIGHT_MODEL_NAME`, only the three skill-management tools, and at most
16 model calls. It favors improving reusable umbrella skills over saving task
transcripts. Workers do not trigger automatic reviews.

Automatic review can only change project skills marked `created_by: agent` and
must read an existing file before changing it. New review-created skills receive
that marker automatically. If another writer changes a file after it was read,
the review must read it again. User-authored and user-level skills are protected
from automatic updates. Review errors do not fail the foreground task; completion
and errors appear in structured logs. Review model output is excluded from the
foreground reply stream.

The trigger counter and last submitted position persist in the main conversation
checkpoint. Review jobs themselves are best-effort, in-process work: shutdown
waits up to 5 seconds before cancellation, and crashes do not replay pending jobs.
Completed skill writes persist. Programmatic users of `create_coding_agent`
should call `await agent.skill_review_manager.close()` during cleanup.

Example fields for a **new** Agent Card version (use the existing publication
process; active frozen Cards cannot be edited in place):

```json
{
  "tool_allowlist": ["read_file", "code_graph", "skills_list", "skill_view", "load_skill"],
  "skill_allowlist": ["code-review"]
}
```

Grant `skill_manage` separately when a Worker should update named skills. The
skill allowlist also applies to creation: the new name must be allowed.

### Verification

Install `requirements.txt` in a Python 3.11+ virtual environment, then run:

```bash
python -m pytest tests/test_code_graph.py tests/test_skill_store.py tests/test_skill_review.py tests/test_knowledge_integration.py -q
python -m pytest tests -q
```

These migration tests use temporary repositories, fake chat/embedding models,
and local HTTP endpoints; no cloud credentials, PostgreSQL, or RocketMQ are
needed. Existing database/broker tests remain opt-in through their environment
switches. Source indexes, private skills, sessions, and credentials are not
imported. See `THIRD_PARTY_NOTICES.md` for the migrated modules' MIT notices.
