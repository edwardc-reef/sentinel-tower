# User Stories

> **Note:** The stories below were reverse-engineered from the codebase — Django models, admin 
> configuration, management commands, Celery tasks, notification handlers, and Grafana dashboards.
> Where intent was ambiguous, the story is phrased around what the code actually enables rather 
> than speculation about unbuilt features. 

Sentinel Tower watches the Bittensor blockchain (subtensor chain), records what happens on it, and
tells specific people when specific things happen. It has no public API and no end-user frontend —
its "users" are internal: chain/infra operators, subnet owners, and anyone reading the Grafana
dashboards or Django admin. Stories are grouped by the persona whose need drove that part of the
system.

---

## 1. Chain Operator / On-Call Engineer

Responsible for keeping the ingestion pipeline healthy and trusting the data it produces.

- **As a chain operator**, I want every extrinsic on the chain recorded as it happens, so that I have
  a complete, queryable audit trail of on-chain activity (`sync_extrinsics` daemon →
  `apps.extrinsics.Extrinsic`).
- **As a chain operator**, I want an unreadable block to remain a detectable ingestion gap instead
  of being recorded as an empty block, so the daemon leaves it for `backfill_extrinsics` to recover
  later without unnecessarily reopening a healthy provider connection.
- **As a chain operator**, I want a liveness endpoint (`GET /alive/`) so that my orchestrator
  (Docker/Kubernetes) can detect and restart a hung ingestion process.
- **As a chain operator**, I want Prometheus metrics (`/metrics`, `/business-metrics`) exposed so I
  can alert on ingestion lag, queue depth, and pipeline health from existing monitoring
  infrastructure rather than watching logs.
- **As a chain operator**, if the ingestion process misses blocks (crash, network partition, deploy
  gap), I want a backfill command (`backfill_extrinsics --lookback N`) that detects the exact missing
  block range and fills it in, using the live node for recent blocks and falling back to an archive
  node for older ones, so gaps don't require manual investigation.
- **As a chain operator**, I want the ingestion daemons (`sync_extrinsics`, `sync_metagraph`) to ride
  out chain-endpoint trouble: when a chain call fails they reopen the connection, and if that reopen
  fails too (a websocket handshake that times out) they retry it with exponential backoff instead of
  exiting, so a brief network blip doesn't stop ingestion. The backoff continues across successful
  handshakes until a chain operation succeeds. If the endpoint stays unreachable one retry is logged
  at error level (and so reaches Sentry), while later retries remain warnings until recovery resets the
  outage state.
- **As a chain operator**, I want failed extrinsics decoded into a human-readable error name
  (`SubtensorErrorCode`) instead of a raw hex module/index pair, so I can diagnose failures without
  cross-referencing the subtensor source.
- **As a chain operator**, I want old, low-value data (non-validator neuron snapshots, weight/bond/
  collateral rows past a retention cutoff) pruned automatically in bounded batches, so the database
  doesn't grow unbounded while validator history — which the APY dashboards depend on — is preserved.
- **As a chain operator**, I want stuck or misrouted Celery tasks to be movable or flushable
  (`move_tasks`, `flush_queue`) without redeploying, so I can recover from a bad queue state during an
  incident.
- **As a chain operator**, I want a debug tool (`get_block_extrinsics`) that dumps raw extrinsics for
  one block directly from the node, so I can compare "what the chain says" against "what Sentinel
  Tower stored" when investigating a discrepancy.

## 2. Metagraph / APY Data Consumer (Grafana dashboard viewer, analyst)

Wants trustworthy point-in-time snapshots of subnet state without touching the chain directly.

- **As an analyst**, I want periodic full snapshots of every subnet's metagraph (neurons, weights,
  bonds, collateral) stored in Postgres, so I can query historical subnet state through Grafana
  instead of replaying the chain.
- **As an analyst**, I want snapshots taken at meaningful points in the epoch (epoch start, two
  intermediate points, epoch end) rather than every block, so storage stays bounded while I still get
  enough resolution to see how a subnet evolves within an epoch.
- **As a validator operator**, I want my validator's APY computed from actual per-epoch dividend/stake
  data and exposed on a Grafana dashboard (`subnet-apy.json`), so I can verify my returns without
  building the computation myself.
- **As a subnet owner or miner**, I want per-subnet Grafana dashboards (`metagraph-miner.json`,
  `metagraph-validator.json`, `metagraph-multi-miner.json`) so I can track my own neurons' stake,
  incentive, and emissions over time.
- **As an analyst**, I want the materialized APY views refreshed on a predictable schedule (every 1h)
  without ever overlapping, so dashboard numbers are consistent and refreshes don't compete for database
  resources.
- **As an operator historically missing data**, I want a long-running backfill daemon
  (`historical_metagraph_backfill`) that walks a historical block range one epoch-start snapshot at a
  time, so I can populate months of missing APY history without babysitting the process.
- **As an analyst**, I want known coldkeys/hotkeys labeled with human-readable names
  (`load_key_labels`), so dashboards show "Foundation Validator" instead of a raw SS58 address.

## 3. Metagraph Explorer User (Django admin)

Wants to inspect raw metagraph state interactively without writing SQL.

- **As an admin user**, I want to pick a subnet and a block from dropdowns and see that subnet's full
  metagraph state at that block, so I can inspect specific historical moments without knowing the
  underlying schema.
- **As an admin user**, I want the block list for a given subnet to only show blocks that were
  actually dumped (not every chain block), so I'm not offered blocks with no data.
- **As an admin user**, I want to browse and search extrinsics by call module, success/failure, netuid,
  address, or extrinsic hash directly in the Django admin, so I can investigate a specific transaction
  without a custom tool.

## 4. Subnet Owner / Governance Watcher (Discord notification recipient)

Wants to be alerted the moment something governance-relevant happens, without polling the chain.

- **As a subnet owner**, I want a Discord alert the instant my subnet is registered or dissolved, so I
  don't have to poll the chain to know the registration succeeded (or that dissolution — often
  irreversible and high-stakes — actually happened).
- **As a subnet owner**, I want a Discord alert when a coldkey swap is announced, disputed, reset, or
  cleared for keys relevant to my subnet, routed to a webhook I control, so I can react to a
  potentially fraudulent or unexpected swap in progress before it completes.
- **As a subnet owner**, I want to configure my own Discord webhook URL for my subnet through the
  Django admin (`SubnetWebhook`), without needing a code deploy or asking an engineer to add an
  environment variable.
- **As a subnet owner**, I want to be able to disable my webhook temporarily (rather than delete it),
  so I can silence notifications during planned maintenance without losing the configuration.
- **As a chain governance watcher**, I want a catch-all alert for any `Sudo`-wrapped call that isn't
  handled by a more specific notification (coldkey swap, subnet registration/dissolution), so
  unexpected root-level chain interventions never go unnoticed just because no one wrote a specific
  handler for them yet.
- **As a notification recipient**, I only want to be alerted about extrinsics that actually succeeded
  on-chain, so I'm not paged for attempted-but-rejected transactions.
- **As an engineer extending the notification system**, I want to unwrap `Sudo`-wrapped calls to their
  inner call before matching against handlers, so a governance action performed via sudo (the common
  path for `AdminUtils` hyperparameter changes) is still routed to the right notification instead of
  falling through to the generic Sudo alert.

## 5. Subnet Hyperparameter Watcher

Wants to know not just *that* a hyperparameter changed, but what it changed *from* and *to*.

- **As a subnet owner or researcher**, I want the current value of every tunable subnet hyperparameter
  (tempo, weight rate limit, immunity period, min/max difficulty, etc.) tracked in one table, so I can
  see a subnet's current configuration at a glance instead of querying the chain live.
- **As a subnet owner or researcher**, I want every hyperparameter *change* recorded with its old and
  new value, so I can build a timeline of governance decisions for a subnet (`SubnetHyperparamHistory`,
  `hyperparam-events.json` dashboard).
- **As a notification recipient**, when a hyperparameter-changing extrinsic fires, I want the alert to
  include the previous value alongside the new one, so I can judge the significance of the change
  (e.g., "immunity period 4032 → 100" reads very differently from "4032 → 4000") without a separate
  lookup.
- **As a chain operator**, I want a one-shot sync command (`sync_hyperparams`) to seed or repair the
  current-value table for one or more subnets directly from the chain, independent of the
  extrinsic-driven update path, so I can recover from drift without replaying history.

## 6. Platform/Infra Engineer (developer extending the system)

Wants the system to be operable and debuggable, since there's no support team behind it.

- **As a developer**, I want Celery tasks isolated onto separate queues by cost (`metagraph` vs.
  `celery`), so a burst of expensive metagraph dumps can't starve lightweight notification tasks.
- **As a developer**, I want tasks to re-enqueue automatically if a worker crashes mid-task
  (`acks_late`, `reject_on_worker_lost`), so a worker restart during a deploy doesn't silently drop
  in-flight work.
- **As a developer**, I want workers to recycle after a fixed number of tasks, so a memory leak in the
  underlying Bittensor SDK client doesn't slowly degrade a long-lived worker process.
- **As a developer**, I want a pluggable storage abstraction (local filesystem or S3, chosen by
  config) behind one interface, so artifact storage can move from local disk to S3 in production
  without touching call sites.
- **As a developer adding a new alert type**, I want to add a notification handler by subclassing a
  base class, declaring which extrinsic patterns it matches, and registering it with a decorator, so
  wiring a new Discord alert doesn't require touching the dispatch/matching code at all.
