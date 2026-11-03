# Apps

How to host an app that ships its own Docker Compose stack, give it a domain, run more than one
environment, and release to prod. Every app environment gets its own VM.

## Where an app is described

One file per app: `config/apps/<app>.toml`. It may hold only these tables (anything else is
site-wide and belongs in `config/site.toml`):

| Table | What it is |
|---|---|
| `[vms.<name>]` | Its VM: `cpus`, `memory`, `disk`, `network = { name, ipv4 }` (its own bridge, a /24 inside 10.200.0.0/16 that no other VM uses) and a fixed `address` on it. `[vm_defaults]` in `site.toml` supplies the image, packages and the services kept off inside. `exposed_ports` lists TCP ports it may listen on beyond loopback; `verify` fails on any other. |
| `[stacks.<name>]` | One running copy of the app, with `vm = "<vm name>"`: its git checkouts, compose directory, generated secrets, kept files and `.env` settings. See `StackSpec` in `src/hostctl/domain/site.py`. |
| `[tunnels.<name>]` | Its Cloudflare Tunnel: `connector = "stack:<name>"` (the stack's own cloudflared) or `"vm:<name>"` (hostctl runs cloudflared in that VM, for an app without a stack), and `routes = { "<hostname>" = "http://<service>:<port>" }`. |
| `[ollama.<instance>]`, `[gates.<instance>]` | Ollama with a pinned release and models, and an API-key gate in front of it (`config/apps/llm.toml`). |
| `[redirects]` | `"<hostname>" = "https://<other hostname>"`: a 301 at Cloudflare that keeps path and query. The hostname must also be routed by a tunnel. |
| `[verify] public` | URLs that must answer 2xx from the internet; `hostctl verify` checks them. |
| `[verify] not_found` | URLs that must answer 404 from the internet (an API description the edge hides). |

A VM whose data can be lost and rebuilt (CI checkouts and caches) may set `throwaway = true`: its
disk is left out of the check that the VMs' disks fit in `lxd.pool.size`.

An instance may be `kind = "container"` instead of a VM, with `gpu`, `nesting` and `cpu_pin`, in
the default project. That is for the operator's own GPU services, never for other people's apps:
the restricted project refuses GPUs and nesting on purpose.

Examples in `config/apps/`: `demo.toml` is the smallest, `website.toml` serves a zone apex with a
www redirect, `shop.toml` has staging and prod, `relay.toml` is an app without a stack, and
`llm.toml` is a GPU container.

## Add an app

1. If it uses a new domain, add it in Cloudflare (same account) and to `zones` in
   `config/site.toml`. Every zone gets the shared `[cloudflare.settings]`.
2. Write `config/apps/<app>.toml` with a VM, a stack in it, a tunnel and a public check. Config
   refuses VMs whose disks no longer fit in `lxd.pool.size`. Hostnames must sit in a listed zone, at
   most one level below it (Cloudflare's Universal SSL covers one level).
3. `make check`, then `hostctl plan`. Read the plan: a new stack ships its checkouts, generates its
   secrets, then builds; a new tunnel is created in round 1 and gets its routes, DNS and token in
   round 2.
4. `hostctl apply --yes`, then confirm `hostctl plan` is empty and `hostctl verify` passes.

## Add an environment (staging, prod, preview)

Add a VM for it and a second stack that extends the first and lists only what differs:

```toml
[vms.myapp-prod]
cpus = 2
memory = "3GiB"
disk = "16GiB"
network = { name = "myapp-prod", ipv4 = "10.200.12.1/24" }
address = "10.200.12.10"

[stacks.myapp-prod]
extends = "myapp"
vm = "myapp-prod"
ref = "prod"
[stacks.myapp-prod.env]
DOMAIN = "myapp.example.org"
```

Tables merge key by key, so `env` keeps every inherited setting except the ones listed. Each stack
is its own compose project (hostctl writes `COMPOSE_PROJECT_NAME=<stack name>`), so it has its own
containers, volumes, database, secrets and backups. Give it its own tunnel.

## Turn an environment off and on

A VM with `running = false` (like `[vms.shop-staging]`) stays stopped. It keeps its disk and data,
and while it is off its site shows Cloudflare's "tunnel unavailable" page.

- To test staging, set `running = true` and run `hostctl apply --yes`. That starts the VM and ships
  HEAD.
- When you're done, set it back to `false` and apply again.

A VM's `memory` is only a ceiling: the host gives it what it uses. Changing `memory` needs a
restart, which hostctl only does when asked: `hostctl apply --yes --restart <vm>`.

## Releases without downtime

A stack with `rolling = [...]` (services with a Docker healthcheck that can run twice for a moment)
and `jobs = [...]` (one-shot migrations) is updated without taking the site down: new images are
built while the old containers serve, the jobs run, then each rolled service gets a healthy new
container before its old one stops. If the new one never gets healthy, the old one keeps serving
and `apply` fails.

The rule for app developers: **a migration must work with the release before it**, because the old
code still serves while it runs. Add a column in one release and start using it; drop or rename the
old one in a later release.

## Promote a release to prod

A stack with `ref = "prod"` ships the `prod` branch of each checkout; a stack without `ref` ships
HEAD (and warns about uncommitted changes, which never ship).

```sh
# Check staging first, then move prod to the commit you tested, in every source repo:
git -C ~/src/shop-api branch -f prod <commit>
git -C ~/src/shop-web branch -f prod <commit>
uv run hostctl plan     # shows the release it would ship to 'shop-prod'
uv run hostctl apply --yes
```

Roll back the same way: point `prod` at the previous commit and apply.
