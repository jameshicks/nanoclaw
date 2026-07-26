# Agent Architecture Trade-offs

Why NanoClaw runs each agent turn in a fresh container, with a supervisor
that stays outside the container boundary — and what that costs.

> **Scope note.** Written against NanoClaw v1.2.x. The *argument* is
> architecture-level and still applies, but specific code references (§2,
> §4.5) describe the v1 layout: mounts are assembled by `buildVolumeMounts`
> in `src/container-runner.ts`. In v2 that logic moves into
> `src/modules/mount-security/` plus a `container_configs` table. Treat the
> file/function names below as v1 landmarks, not current API.

---

## 1. The Question

Two requirements that enterprise agent deployments hit simultaneously:

1. **Per-turn sandboxing.** Every agent turn executes tool calls under the
   influence of attacker-controlled input — end users, scraped web pages,
   ingested documents, emails. Prompt injection is live, not hypothetical, and
   the model cannot reliably refuse. The defensive layer *below* the model is
   what matters on a bad day.
2. **Declarative composition.** Production deployments need reproducibility,
   auditability, version-controlled configuration, and clean lifecycle
   management of dependencies (MCP servers, vector DBs, credential proxies,
   telemetry collectors). Compose or Kubernetes manifests, not a `README.md`
   with bash snippets.

These requirements naturally pull in opposite directions. The interesting
design work is getting both simultaneously without quietly sacrificing one.

---

## 2. Architectural Shapes

### Shape 1 — In-process / long-running worker

Examples: most agent frameworks in the open-source ecosystem; nanobot
(Python); default LangChain/LlamaIndex deployments.

- Agent logic runs inside the supervisor process.
- Sessions persist in memory across turns.
- Optional sandboxing (e.g. bubblewrap around shell exec) fences specific
  tool calls, not the Python process itself.

**Declarative story:** trivial. One service in `docker-compose.yml`, done.

**Isolation story:** weak. A prompt-injection path that reaches code execution
inside the process has the whole process — credentials in memory, other
tenants' state, persistent files, the routing logic, everything the process
can reach on the filesystem.

### Shape 2 — Per-turn containers, host-process supervisor

NanoClaw's current architecture.

- Supervisor (NanoClaw) runs as a host process under systemd / launchd, as an
  unprivileged user (`james`).
- Each agent turn spawns a fresh ephemeral container via `docker run -i --rm`.
- Mounts are scoped per group; host project root is mounted read-only;
  `.env` is shadowed with `/dev/null`; per-group IPC namespace.
- Credentials are injected by an on-path gateway (OneCLI), never exposed to
  the container's environment or filesystem.

**Declarative story:** partial. Sidecars (MCP, OneCLI) can live in a Compose
file; the supervisor does not. Startup is two commands.

**Isolation story:** strong. Kernel-enforced namespace separation between
groups, per-turn fresh filesystem, supervisor separated from LLM-influenced
code execution.

### Shape 3 — Per-turn containers, containerized supervisor (fully in Compose)

Moving NanoClaw itself into Compose to get a single declarative artifact.

- Requires the supervisor container to reach the host Docker daemon to spawn
  agent siblings.
- Mechanism: bind-mount `/var/run/docker.sock` into the supervisor container.
- Path consistency: supervisor must see host paths at the same location it
  references them, because `docker run` arguments are interpreted by the
  *host* daemon. Typically solved by mounting the project root at an identical
  path inside and outside the container.
- UID mapping: `process.getuid()` returns the supervisor container's uid
  (1000), not the host user's. Must pass `HOST_UID`/`HOST_GID` as explicit
  env vars.

**Declarative story:** clean. One `docker compose up`, everything rises.

**Isolation story:** *worse than Shape 2.* The socket mount is a designed
pierce of the container boundary. See §3.

---

## 3. The Declarative Composition Problem

### Why Compose fights per-turn isolation

Compose models long-lived services — things with an address, a health check,
and a restart policy. Per-turn containers are the opposite shape: spawned per
message, torn down after, potentially thousands per hour. Compose has no
native primitive for this, so every bridging approach pays a cost somewhere.

### The docker.sock trap

Bind-mounting `/var/run/docker.sock` into a container hands that container
the full Docker daemon API. Because the daemon runs as root on Linux (by
default), the socket is the control plane of a root-owned process. The
canonical escalation, from inside the container with the socket mounted:

```bash
docker run -v /:/host -it alpine chroot /host
```

This asks the host daemon to create a new container with the host root
filesystem mounted and drop you into a chroot. You now have root on the host:
read or write any file, load kernel modules, join the host PID namespace or
network namespace. The container boundary is gone.

The important bit: this isn't a bug. The socket mount is *designed* as an
escape hatch for the one thing that needs one (the orchestrator). Any
container with the socket mounted has, by construction, a trust level equal
to the daemon's.

**For an agent platform, this matters because the supervisor is what parses
user input.** Any code-execution bug or injection path in the supervisor
escalates directly to host root. Containerizing the supervisor with a socket
mount *degrades* the boundary relative to running it as an unprivileged host
process.

### Rootless Docker

Runs `dockerd` as an unprivileged user instead of root.

**What it changes:** the `-v /:/host` trick still works, but the resulting
access is bounded by what the daemon's user can do. No root, no cross-user
writes, no kernel module loading, no writes to `/etc/sudoers` or
`/root/.ssh/authorized_keys`.

**What it doesn't change:** the attacker can still read and write anything
the daemon user owns. On a single-user box where one account owns everything
interesting (the app, the data, the SSH keys), that's most of the attack
surface. Rootless cuts *escalation beyond the user*, not blast radius within.

**Limitations worth knowing:**
- Userspace networking (slirp4netns / pasta) with measurable overhead.
- Can't bind privileged ports; no `macvlan` / `ipvlan`.
- Requires cgroups v2 for resource limits.
- `--privileged` doesn't grant host-level privileges.
- UID mapping via `/etc/subuid` and `/etc/subgid` — bind-mount ownership
  becomes confusing; uid 1000 in the container is 101000 on the host.
- Daemon is a systemd *user* unit; needs `loginctl enable-linger` to run
  without an active login session.

### Socket proxy with body inspection

The genuine fix. A small proxy (e.g. `tecnativa/docker-socket-proxy`, or a
custom one) sits in front of the socket and restricts what the supervisor
can ask the daemon to do.

- A path-level allowlist alone is not enough. The `-v /:/host` escalation is
  a perfectly valid `POST /containers/create` call; the proxy must inspect
  the request *body* and reject disallowed bind mounts, `--privileged`,
  `--pid=host`, `--network=host`, and disallowed capabilities.
- With a body-inspecting proxy, the escalation path closes at the daemon
  boundary, not inside the supervisor.

### Defense in depth: stack them

Rootless alone is incomplete. Socket proxy alone is incomplete. Together:
- The proxy rejects dangerous creation requests at the API level.
- If the proxy has a bug or is bypassed, rootless caps the blast radius at
  the service user's account.
- Neither requires the other, but neither is sufficient alone for a hardened
  deployment.

---

## 4. What Per-Turn Containers Actually Buy You

The structural properties that distinguish Shape 2 from Shape 1.

### 4.1 Fresh state per turn, by construction

When the container exits, everything in it dies. Any file the agent wrote
outside its scoped mounts, any in-memory state, any forked subprocess — all
gone. A successful injection on turn N cannot plant artifacts that influence
turns N+1..N+k. Persistence is the half of a prompt-injection incident that
turns it from "embarrassing" to "reportable"; per-turn containers remove the
mechanism.

### 4.2 Per-tenant separation enforced by the kernel

Each group gets its own container with its own scoped mounts. Group A cannot
see Group B's folder, session directory, or IPC namespace — not because
Python code declines to read it, but because the kernel's mount namespace
does not contain it. In an in-process framework, tenant separation is a
*code-correctness* property. Here it's a *kernel-namespace* property. The
latter survives bugs in the former.

### 4.3 Credentials are never in the agent's address space

The OneCLI gateway sits on the network path. Agent containers have no
real API keys in env vars, config, or memory. The gateway intercepts
outbound HTTPS and injects credentials per request.

- `cat /proc/self/environ` yields nothing useful.
- `grep -r` across the filesystem for token-shaped strings yields nothing.
- Exfiltrating credentials by sending them to an attacker-controlled URL
  doesn't work because the agent doesn't have them to send.

This is a *structural* protection, not a "please don't log the key" one.
Most frameworks load keys at startup and let them sit in process memory for
the lifetime; any tool call in any turn can read them.

### 4.4 Supervisor in a different trust zone from the agent

The supervisor (host process, message routing, scheduling, credential
metadata) and the agent (container, LLM-driven code execution) are different
processes with different filesystem views and different privileges. A
prompt injection that reaches code execution is inside the agent, not
inside the supervisor. In-process frameworks collapse these into one
process; here they're separated by a kernel boundary.

### 4.5 Host codebase is read-only to the agent

`buildVolumeMounts` in `src/container-runner.ts` mounts the project root
with `:ro` and shadows `.env` with `/dev/null`. The agent cannot rewrite
NanoClaw's own source to weaken its sandbox on the next restart, cannot
plant a malicious pre-commit hook, cannot modify `container-runner.ts`
itself. In an in-process framework that can write files, the agent can
overwrite its own importable modules — a classic persistence path.

### 4.6 Kernel-enforced, not language-enforced, boundaries

Container isolation uses Linux namespaces, cgroups, and capability drops —
the same primitives that isolate tenants in multi-tenant SaaS infrastructure.
Language-level sandboxes (restricted Python, AST filters, seccomp-bpf inside
an interpreter) are notoriously fragile against LLM-generated code, because
the LLM will reliably emit whatever bypass exists. Bubblewrap / firejail are
the right family but only fence shell-exec tool calls; the Python process
hosting the agent is not sandboxed from what the LLM emits via other paths.

### 4.7 Bounded blast radius for a worst-case turn

If a turn is fully compromised, the damage ceiling is: that group's folder,
its session directory, its IPC namespace, and whatever else is in its mount
list. Not the database, not other groups, not the host, not credentials,
not the supervisor, not persistent infrastructure.

Most alternatives' worst case is "the process and everything it can touch,"
which for a long-running framework is effectively the whole deployment.

---

## 5. What You Pay For It

Honest accounting — surface these before anyone else does.

- **Cold-start latency per turn.** Seconds, not milliseconds. In-process
  frameworks are 10–100× faster at turn handoff.
- **No shared in-memory state across turns.** Anything expensive must be
  memoized to disk or recomputed.
- **Operational overhead.** Requires a working container runtime, image
  builds, mount discipline. `pip install` is a lot less to set up.
- **Constrained tool surface.** Tools that assume "I can read anywhere on
  the filesystem" or "I share a network namespace with the orchestrator"
  need adaptation.
- **No shared model-context caches across tenants.** Prompt-cache warm-up
  is per-container; density at scale is lower.
- **Declarative composition is partial.** Supervisor is not in the Compose
  file; startup is two commands unless you paper over it with a systemd
  unit or a Makefile target.

---

## 6. The Four Enterprise-Grade Answers

If you want per-turn isolation *and* declarative composition
simultaneously, these are the patterns that actually survive review.

### 6.1 Kubernetes Jobs per turn

Orchestrator runs in-cluster with an RBAC-scoped ServiceAccount that can
only create Jobs in one namespace with a restricted PodSecurityAdmission
profile.

- **Declarative:** everything is YAML; admission controllers enforce what
  the orchestrator is allowed to create.
- **Isolated:** each Job is a fresh pod.
- **Auditable:** Kubernetes audit log, RBAC, and admission policy.
- **Cost:** cold-start latency, operational burden of running Kubernetes.

### 6.2 Firecracker microVMs per turn

The Fly Machines / AWS Lambda / Modal model. Each turn runs in a lightweight
VM, not a container.

- **Isolated:** stronger than containers; hypervisor boundary.
- **Fast cold start:** ~100ms in the best implementations.
- **Declarative:** control plane is API-driven.
- **Cost:** you're building on a platform, not your own boxes. Vendor lock
  or serious infrastructure effort.

### 6.3 Docker socket proxy + Compose

The compromise answer. Keep the familiar Compose deployment, give the
supervisor access to a *restricted* daemon API.

- **Declarative:** one Compose file; proxy, supervisor, sidecars all
  declared together.
- **Isolated:** proxy rejects dangerous create shapes before they reach the
  daemon.
- **Cost:** the proxy is now trust-critical and must be correct. You're
  still on one host. The proxy must inspect request bodies, not just paths.

### 6.4 gVisor / Kata Containers runtime

Run per-turn containers under a syscall-filtering or lightweight-VM runtime
instead of `runc`.

- **Declarative:** looks like normal containers to Compose / Kubernetes.
- **Isolated:** gVisor intercepts syscalls in userspace; Kata wraps each
  container in a lightweight VM.
- **Cost:** performance overhead, less mature ecosystem, some syscall
  compatibility gaps (gVisor).

---

## 7. TL;DR

- Per-turn container isolation is a real structural property, not an
  aesthetic preference. It bounds blast radius, separates tenants,
  separates supervisor from workload, and removes persistence from the
  prompt-injection playbook.
- In-process frameworks trade those properties for simpler declarative
  deployment and lower latency. That trade is defensible for
  single-tenant, low-trust, or internal-only deployments. It is not
  defensible for multi-tenant deployments that touch real credentials or
  real user data.
- Getting *both* isolation and declarative composition requires either
  (a) keeping the supervisor out of the container boundary, or
  (b) restricting the daemon API the supervisor can reach, or
  (c) moving up to Kubernetes / microVM platforms that have first-class
  per-turn primitives.
- The cheap answer — containerize the supervisor with a plain socket
  mount — is net-negative for the security boundary, regardless of how
  much more "cloud-native" it feels.
