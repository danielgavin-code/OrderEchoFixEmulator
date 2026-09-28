# Overview

OrderEchoFixEmulator is a **local FIX counterparty**. It stands in for a broker
or an exchange so that a client can be built and exercised without anybody's
certification environment, credentials or trading calendar. It speaks FIX 4.2
and FIX 4.4, runs one session or several at once, and answers orders the way a
venue would: acks, partial fills, fills, cancels, replaces and rejects, with
correct order state behind them.

It runs as an **acceptor** — the venue side of the connection. You point a
client at it.

## Why it exists

The wider goal is FIX certification driven by an LLM operator: an agent that
connects to a venue's test system, works through a certification script, and
says what happened and whether it was right. That agent needs somewhere to
practise, somewhere its mistakes are cheap, and somewhere a scenario can be
reproduced exactly. This emulator is that target.

Which is why two things here are unusual for a mock:

**It is honest about FIX.** Sequence numbers, ResendRequests answered by
replaying the real messages, session-level Rejects, version differences between
4.2 and 4.4 — the details a client only gets wrong against a real venue are the
details it can get wrong here.

**It is honest about what it did.** Every message in and out is logged, every
decision is recorded in an evidence file, and the log viewer will reconstruct
an order's life and check it for consistency. When the agent claims a test
passed, there is a file that says so.

## Architecture

The shape of the code is one idea: **the parts that decide are pure, and the
parts that touch the world are thin.**

<svg viewBox="0 0 700 300" role="img" aria-label="Architecture: a client
connects to the transport, which drives pure session, order book and version
profile components, and writes logs and evidence."
     xmlns="http://www.w3.org/2000/svg"
     style="max-width:100%;height:auto;color:inherit">
  <g fill="none" stroke="currentColor" stroke-width="1.5"
     font-family="system-ui, sans-serif" font-size="13">
    <rect x="8" y="120" width="110" height="48" rx="6"/>
    <text x="63" y="149" text-anchor="middle" stroke="none"
          fill="currentColor">FIX client</text>

    <rect x="168" y="106" width="120" height="76" rx="6"/>
    <text x="228" y="138" text-anchor="middle" stroke="none"
          fill="currentColor">Transport</text>
    <text x="228" y="158" text-anchor="middle" stroke="none"
          fill="currentColor" font-size="11" opacity="0.7">sockets, clock</text>

    <rect x="338" y="18" width="150" height="52" rx="6"/>
    <text x="413" y="49" text-anchor="middle" stroke="none"
          fill="currentColor">Session</text>

    <rect x="338" y="92" width="150" height="52" rx="6"/>
    <text x="413" y="123" text-anchor="middle" stroke="none"
          fill="currentColor">Order book</text>

    <rect x="338" y="166" width="150" height="52" rx="6"/>
    <text x="413" y="190" text-anchor="middle" stroke="none"
          fill="currentColor">Rules</text>
    <text x="413" y="206" text-anchor="middle" stroke="none"
          fill="currentColor" font-size="11" opacity="0.7">+ version profile</text>

    <rect x="338" y="240" width="150" height="46" rx="6"/>
    <text x="413" y="268" text-anchor="middle" stroke="none"
          fill="currentColor">Pricing</text>

    <rect x="538" y="92" width="150" height="52" rx="6"/>
    <text x="613" y="117" text-anchor="middle" stroke="none"
          fill="currentColor">Logs</text>
    <text x="613" y="134" text-anchor="middle" stroke="none"
          fill="currentColor" font-size="11" opacity="0.7">FIX + engine</text>

    <rect x="538" y="166" width="150" height="52" rx="6"/>
    <text x="613" y="191" text-anchor="middle" stroke="none"
          fill="currentColor">Evidence</text>
    <text x="613" y="208" text-anchor="middle" stroke="none"
          fill="currentColor" font-size="11" opacity="0.7">JSONL per run</text>

    <rect x="168" y="18" width="120" height="52" rx="6"/>
    <text x="228" y="43" text-anchor="middle" stroke="none"
          fill="currentColor">Control API</text>
    <text x="228" y="60" text-anchor="middle" stroke="none"
          fill="currentColor" font-size="11" opacity="0.7">+ /guide, /viewer</text>

    <path d="M118 144 L168 144" marker-end="url(#arrow)"/>
    <path d="M288 130 L338 60" marker-end="url(#arrow)"/>
    <path d="M288 144 L338 122" marker-end="url(#arrow)"/>
    <path d="M288 158 L338 190" marker-end="url(#arrow)"/>
    <path d="M288 170 L338 258" marker-end="url(#arrow)"/>
    <path d="M488 118 L538 118" marker-end="url(#arrow)"/>
    <path d="M488 130 L538 186" marker-end="url(#arrow)"/>
    <path d="M228 106 L228 70" marker-end="url(#arrow)"/>
  </g>
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
            markerWidth="6" markerHeight="6" orient="auto">
      <path d="M0 0 L10 5 L0 10 z" fill="currentColor"/>
    </marker>
  </defs>
</svg>

**Transport** owns the sockets and the clock. It reads bytes, hands messages to
a session, turns the actions it gets back into bytes, and runs the timer. It is
the only part that can block.

**Session** is the FIX session layer: Logon, Heartbeat, TestRequest, sequence
numbers, ResendRequest, SequenceReset, Logout, Reject. It is a function from
(state, message) to a list of actions — `Send`, `Disconnect`, `Evidence` — and
it has no idea what a socket is.

**Order book** owns orders: quantities, states, ExecIDs, and the schedule of
what happens next. It is pure too, driven by an injected clock.

**Rules** decide what an order gets, from configuration. **Version profiles**
decide how that is written on the wire — the order book says *a partial fill
happened*, and the 4.2 profile writes `150=1` where the 4.4 profile writes
`150=F`.

**Pricing** fetches reference prices, with a static table behind it.

**Control API** is an HTTP surface for making the emulator do things a venue
would do on its own — fill by hand, drop the connection, break a checksum on
purpose. It also serves this guide and the log viewer.

## Design principles

**The core is pure.** No sockets, no wall clock, no randomness in the parts
that decide. The same messages with the same config and the same prices produce
the same output, every time. This is why the tests can drive real scenarios
without a network, and why a captured scenario stays reproducible.

**The wire format is pinned.** Golden fixtures hold byte-for-byte renderings of
both versions, captured before the refactors that could have changed them.
Rendering drift is a test failure, not a discovery.

**Nothing is bypassed.** A manual fill from the control API goes through the
same order book as a scheduled one, and out through the same session, so
sequence numbers, logs and evidence stay correct. There is no back door that
produces messages the engine does not know about.

**Deliberate misbehaviour is labelled.** The emulator can send a broken
checksum or skip sequence numbers, because a client has to cope with that. Every
such message is marked `injected` in the evidence and in the FIX log, so a
deliberate fault can never be mistaken for a bug later.

**It is local and unauthenticated, and it says so.** Loopback only, no
credentials, no TLS. It is a development tool, and the logs contain every order
that went through it.

## Where to go next

- [Quick start](quick-start.html) — running, and a first order.
- [Configuration](configuration.html) — every key in the YAML.
- [Order behavior](order-behavior.html) — what an order gets, and why.
- [Log viewer](log-viewer.html) — reading back what happened.
