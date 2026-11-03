# Architecture

How `hostctl` is built: what to understand before changing its structure. The code covers the
details. Update this file in the same commit as any structural change.

## Overview

`hostctl` turns `config/site.toml` (the desired state) into a running platform on one server. It
never edits the machine directly from logic: every component **observes** the live state with
read-only commands, **diffs** it against config in a pure function, and returns a **plan** of exact
commands. `hostctl apply --yes` runs those commands in order, then plans again to prove nothing is
left.

```
config/site.toml ──load──▶ Site (domain)
                               │
      ┌────────────────────────┼─────────────────────────┐
      ▼                        ▼                         ▼
 StoragePool              BridgeNetwork      Firewall      TenantVm      … one component each
      │  observe (read-only, via ports)  ─▶  diff (pure)  ─▶  Plan
      └───────────────────────────────▶ engine.plan_all ─▶ render / engine.apply
```

## Stack and layout

Python 3.12+, standard library only at runtime. Dev: pytest, ruff, mypy `--strict`, uv.

| Path | Layer | Holds | May import |
|---|---|---|---|
| `src/hostctl/domain/` | Domain | Pure data and logic: `Command`, `Plan`, `Site` specs, size parsing. No I/O. | domain |
| `src/hostctl/ports/` | Ports | Interfaces: `Shell`, `LxdReader`, `SourceReader` (git checkouts), `EdgeApi` (Cloudflare), the `Host` bundle | domain, ports |
| `src/hostctl/components/` | Application | One class per piece of infrastructure, each with a pure `diff_*` or render function | domain, ports, components |
| `src/hostctl/checks/` | Application | Read-only checks: `doctor` (prerequisites), `verify` (isolation) | domain, ports, checks |
| `src/hostctl/adapters/` | Adapters | Talking to the world: local and SSH shells, `lxc query`, `git` on the machine running hostctl, Cloudflare's API via `curl -K -` on the server | domain, ports, adapters |
| `src/hostctl/config/` | Edge | TOML → validated `Site`: `loader` reads and merges files, `assemble.parse` builds the `Site`, one module per spec group | domain |
| `src/hostctl/engine.py`, `wiring.py`, `cli.py` | Composition | Plan/apply loop; the only place adapters and components are chosen and ordered; argument parsing | anything |
| `config/site.toml`, `config/apps/*.toml` | Data | Every site value (names, sizes, addresses, check targets); one file per app, merged at load ([apps.md](apps.md)) | — |
| `tests/` | Tests | See "Testing" below | — |

`tests/test_architecture.py` enforces the "May import" column, that the domain does no I/O, and
that only `wiring.py` picks adapters. A change that breaks it is a design change: discuss it, don't
silence the test.

## Rules for code

1. **Imports point inwards** (table above). Outer layers depend on inner ones, never the reverse.
2. **Grow by adding.** New infrastructure is a new component in `components/`, registered in
   `wiring.components()`. Existing components do not learn about new ones.
3. **One component, one responsibility.** It owns its resources completely (the firewall owns whole
   chains, not lines in someone else's).
4. **Decisions are pure.** The logic lives in a function that takes specs and observed data and
   returns a `Plan` or text (`diff_vm`, `chain_rules`, `render_script`). Components only gather
   input for it.
5. **`plan` is read-only and `apply` is idempotent.** After `apply`, `plan` must be empty; if a
   component can't reach that, it returns a warning instead of a change.
6. **No hidden state.** Desired state lives in config; live state is read from the machine every
   time. No state files, no caches.
7. **Fail closed.** Components are ordered so a partial apply is safe (the firewall comes before
   every VM, and LXD itself will not start without it). Checks that guard isolation fail loudly.
8. **Data, not strings, until the edge.** Commands are `Command` objects (argv tuples), rendered
   only for display. Config never reaches a shell string without `shlex`.
9. **Typed and small.** mypy strict, frozen dataclasses for data, functions short enough to test one
   behaviour each.
10. **No secrets in code, config or tests.** They arrive through environment variables read in
    `wiring.py`.
11. **Destructive changes are a person's call.** hostctl never deletes data, DNS records, models or
    disks on its own; it warns and leaves them.

## Interfaces and invariants

- `Component`: `name` and `plan(host: Host) -> Plan` (`components/base.py`). Only read-only commands
  run in `plan`; returned commands are complete and ordered; applying them empties the next plan.
- `Shell.run(Command) -> Result` never raises on a non-zero exit; callers decide (`run_checked`
  raises `CommandFailed`). A sudo command refused for lack of a password raises `SudoUnavailable`,
  so `plan` stops instead of reading "unreadable" as "absent".
- `LxdReader.get(path)` returns `None` for "not found" and raises on any other error.
- `Plan` is immutable; plans compose with `+` in component order.
- `SourceReader` reads committed trees only (`git archive <ref>`); uncommitted changes produce a
  warning, never a shipment.
- `EdgeApi.get` returns a Cloudflare `result` or raises `EdgeError`; `EdgeApi.write` only builds a
  command, so every Cloudflare change goes through the plan like a shell change. Without
  `HOSTCTL_CLOUDFLARE_TOKEN`, tunnel components warn instead of planning.
- Secrets never enter a rendered plan: generated ones are made inside the VM (`openssl rand`), and
  tokens travel only in a command's stdin, which `render` shows as a byte count.
- `apply` repeats plan-and-run for up to 3 rounds (`engine.MAX_ROUNDS`) for changes that depend on
  ids created earlier (a new tunnel). It stops when a round plans the same actions again.
- Component order (`wiring.components`): host groups, sshd, disabled services, CPU energy
  preference, host volumes, the data disk (before the pool whose file it holds), swap and the user
  memory line, pool, project, retired VMs, bridges, firewall, VMs and containers, packages, services
  kept off inside each instance, VM cache drop, Ollama, API gates, zone settings, tunnels, redirects,
  retired host connectors, stacks. Tunnels come before stacks so a stack's connector token is in
  place when it starts, and before redirects so a redirected name already has its proxied CNAME.
- Every apply that ran appends a line to the apply record on the server (`applies.py`). Before an
  apply, a guard refuses to run a checkout that lacks commits a previous apply shipped, so one
  branch cannot silently undo another.
- One VM per app environment: `[vms.<name>]` with its own bridge; every `lxc` command is built by
  `domain/lxc.py` (sudo, `--project` for app VMs).
- Instances: `VmSpec.kind` is `"vm"` (`TenantVm`) or `"container"` (`ServiceContainer`, for the
  operator's own GPU services in the default project). Every in-instance component works the same
  in both, through `lxc exec`.
- Stack updates: `start()` in `components/stack.py` uses a plain `up` for a stack's first start or a
  stack without `rolling` services; otherwise it builds, runs the jobs, then replaces each rolled
  service new-before-old, keeping the old one if the new one fails its healthcheck.
- A VM with `running = false` is stopped; every in-VM component returns a warning instead of a plan,
  and `verify` skips the VM and its URLs.
- Tunnel connectors (`components/connectors.py`): `connector = "host"`, `"vm:<name>"` or
  `"stack:<name>"`. Host and VM connectors are one `ContainerConnector` (cloudflared as uid 61000,
  no capabilities, read-only, metrics on loopback) that differs only in where its commands run.
- Firewall: per-bridge chains block every VM from the LAN, from private ranges and from each other;
  with `host_default_deny`, the host itself refuses everything but SSH from listed ranges.
- DNS (`components/dns.py`): `DnsRecords` keeps `[[cloudflare.records]]`, matching TXT records by
  their first word so other TXT records on a name are left alone, and never deletes.

## Adding a component

1. Add the spec to `domain/site.py` and its parsing and validation to the matching module in
   `config/`, with example values in `config/site.toml`.
2. Write the pure function first, with unit tests in `tests/hostctl/components/test_<name>.py`
   covering create, no-op, update and drift.
3. Write the component class: observe through `host.shell` / `host.lxd`, call the pure function. A
   new kind of outside access gets a port in `ports/` and an adapter in `adapters/`.
4. Register it in `wiring.components()` where its dependencies need it, and extend the ordering
   tests in `tests/hostctl/test_wiring.py` if order matters.
5. Run `make check`, then `hostctl plan` against the server, review, `hostctl apply --yes`, and
   confirm `plan` is empty and `verify` passes.

Apps with their own Compose stack are added by config alone: see [apps.md](apps.md).

## Testing

Tests mirror the source: `src/hostctl/components/firewall.py` is tested in
`tests/hostctl/components/test_firewall.py`.

- **Unit** (`tests/hostctl/`): pure functions and components with the doubles in `tests/support/`
  (`FakeShell`, `FakeLxd`, `FakeSources`, `fake_host`, `FakeEdge`). `FakeShell` is strict: an
  unexpected command fails the test, so tests document exactly what each component runs.
- **Adapters** (`tests/hostctl/adapters/`): command wrapping, quoting, sudo handling, `lxc query`
  parsing, and a real local subprocess.
- **Architecture** (`tests/test_architecture.py`): layer imports, adapter choice only in
  `wiring.py`, unique and registered components.
- **Integration** (`tests/integration/`): apply, then re-plan, against an in-memory host
  (`tests/support/simulated.py`) for files, systemd units and the firewall.
- **On the server:** `hostctl apply --yes` followed by an empty `hostctl plan` and a passing
  `hostctl verify` is the end-to-end test of a live change.

`make check` runs ruff (lint and format), mypy strict and every test. CI runs the same command.
