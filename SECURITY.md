# Security policy

## Reporting a vulnerability

Open a private security advisory on
<https://github.com/lucaspirola/PingLucas/security/advisories/new>, or email the
maintainer. Please do not open a public issue for anything that would let one
local process impersonate another, read another user's peer token, or inject a
message into a session it does not own.

Include: the version, the platform, what an attacker gains, and the smallest
reproduction you have. A working proof of concept is welcome but not required.

## Threat model

PingLucas is designed for **one human's cooperating processes on one machine**.
Its trust anchor is the Unix user account.

### In scope

| Threat | Control |
| --- | --- |
| A process owned by another local user connecting to the relay socket | `SO_PEERCRED` uid check; socket mode `0600` in a `0700` directory |
| A local process reading a peer token from the registry | Key files are `0600` and rejected if any group/other bit is set; the containing directory must be `0700` and owned by the user |
| A symlink or a writable parent directory redirecting a write | Every registry path is walked to `/`; symlinked, foreign-owned or non-sticky world-writable ancestors are refused |
| A stale record pointing at a socket now owned by something else | Records carry pid, process start tick and pid namespace; a mismatch invalidates the record before any token is read or sent |
| Pid recycling between discovery and delivery | The pid, start tick and `SO_PEERCRED` pid are re-verified on the open connection, after connect and before the token is written |
| A peer forging its identity in the envelope | The claimed `uds:` address must resolve to the socket registered to the pid that actually connected |
| A peer forging a permission attestation | Envelopes are parsed canonically: attribute order, spelling and value shapes must match byte for byte, and any non-canonical form is rejected |
| Payload breaking out of the envelope | A literal closing tag in the body is entity-escaped; a body containing a second closing tag is rejected outright |
| Flooding, replay, and accidental loops | Per-sender token bucket, message-id replay window, immediate-duplicate rejection, bounded hop chains with a hard hop limit |
| Unbounded input exhausting the relay | Caps on message size, line length, frames per connection, concurrent connections, delivery slots, tracked senders and ledger entries |
| A crashed relay leaving a usable socket behind | The successor refuses to evict a socket that still accepts connections, and cleanup only unlinks artifacts matching its own instance, token and process generation |

### Explicitly out of scope

- **Malware already running as your user.** It can read your peer token, your
  config and your ledger directly. PingLucas is not a sandbox.
- **The push provider.** Telegram and ntfy see every ping and every reply in
  plaintext at the application layer. This is inherent to using a hosted push
  service and is why the README says not to send secrets over it. Self-hosting
  ntfy moves this boundary but does not remove it.
- **ntfy topic secrecy.** A topic name is a bearer credential. Anyone who
  learns it can read your pings and publish replies that the relay will treat
  as yours. `pinglucas init` generates a random topic; protecting it is the
  operator's job. Use Telegram, or an authenticated ntfy server, if that
  exposure is unacceptable.
- **Model behaviour after delivery.** A message that reaches a session is model
  input. The provenance wrapper and the bundled skill keep it distinct from
  authorization, but they cannot guarantee how a model weighs it.
- **Claude's own permission system.** PingLucas never changes it. A reply is
  not a permission grant, and the skill instructs agents to refuse permission
  laundering — but that is an instruction, not an enforcement boundary.
- **Non-Linux platforms.** macOS and Windows lack the `SO_PEERCRED` guarantees
  this design relies on. PingLucas refuses to run rather than pretend.

## Handling of your messages

- Pings and replies are held in memory and in a `0600` ledger under
  `$XDG_STATE_HOME/ping-lucas/`, capped and expired after 24 hours by default.
- The ledger stores a 200-character preview of each question so `pinglucas
  pending` is useful. Set `ledger_ttl_s` lower if that retention is unwanted.
- Bot tokens live in the config file (`0600`) and are redacted from log and
  error output.
- Nothing is sent anywhere except the push transport you configured.

## Supported versions

PingLucas is alpha. Only the latest release receives fixes.
