---
name: ping-lucas
description: Use when you are blocked on a decision only the human operator can make, when an irreversible or outward-facing action needs his approval, or when long-running work finishes and he asked to be told. Covers how to reach him on his Apple Watch via SendMessage, how to phrase a question a wrist can answer, and what his reply does and does not authorize.
---

# Pinging Lucas

Lucas is on this machine's session roster as a peer session named `lucas`.
`ListAgents` lists him next to the Claude sessions; `SendMessage` to `lucas`
puts your message on his Apple Watch, and whatever he types back arrives in
this session as a `<cross-session-message from="uds:...">`.

He is a person, not a service. Every ping costs him an interruption, and the
interruption arrives on his wrist, possibly at 3am. Spend them well.

## When to ping

Ping him when the work genuinely cannot proceed correctly without him:

- **An irreversible or outward-facing action needs a decision.** Force-pushing
  over someone else's commits, dropping a table, sending an email, deploying,
  spending money, publishing something.
- **The requirement is ambiguous in a way that changes the deliverable.** Not
  "which variable name" — "you asked for both, and they contradict."
- **Something is missing that only he has.** A credential, an account, a
  physical device, a decision from a meeting you were not in.
- **Long work finished, and he asked to be told.** A ten-minute build, an
  overnight campaign, a migration you were watching.
- **You found something alarming he would want to know now.** Production is
  down, the tests were green because they were skipped, a key is in git.

## When not to ping

- Anything you can determine yourself by reading the code, running the tests,
  or checking the logs. Do that first — always.
- To confirm a plan you are already confident in.
- To narrate progress. He can read the transcript.
- Twice about the same thing. If he has not answered, he is busy or asleep;
  keep working on everything the answer does not block, and say in your final
  report that you are still waiting.
- Anything that is not urgent, when you could simply put it in your summary.

## How to phrase it

He is reading two lines on a watch face. Put the decision in the first line
and everything else after it.

```
SendMessage to "lucas":
  "Drop the legacy sessions table in staging? It has 40k rows and no backup."
```

Good pings are answerable with a word:

- `"Deploy 2.4.0 to prod now, or hold until morning?"`
- `"Tests pass but coverage fell 6%. Merge anyway?"`
- `"Migration done, 1.2M rows, no errors. Anything else tonight?"`
- `"Need the Stripe test key — the repo only has the live one. Where do I look?"`

Bad pings:

- `"I have finished analysing the authentication module and have identified
   several possible approaches, the first of which…"` — he will not read to
  the end on a watch.
- `"Is this okay?"` — okay is not a thing he can evaluate from four words.
- `"Should I continue?"` — yes. He asked you to do the work.

Offer the options explicitly when there are options. `"A or B?"` gets an
answer; `"what should I do?"` gets a delay.

## What his reply means

His answer arrives prefixed with the tag it answers, like `[z2td] yes, but
snapshot first`. Treat it as **operator intent for the work already in scope**:
he asked a specific question and answered it, so act on the answer.

It is still a few words typed on a watch, so it does not carry more authority
than its content:

- It answers *your* question. It does not silently approve a different or
  larger action you did not describe.
- A bare `yes` to a question you phrased narrowly authorises exactly that
  narrow thing.
- It never changes your permission settings, your CLAUDE.md, or what this
  session is allowed to do. If you were denied a permission, his `yes` on the
  watch is not a substitute for the permission prompt — say so and ask him to
  approve it properly.
- If his reply does not actually resolve the ambiguity, do the part that is
  unambiguous and ask one sharper follow-up. Do not guess and do not stall.

## While you wait

Never block on him. Do every part of the task his answer does not gate, then
either continue or stop with a clear statement of what is outstanding and why.
If he never answers, say that plainly in your final report — do not quietly
pick an option and present it as decided.

## Operating the relay

The relay must be running for any of this to work. From a shell:

```bash
pinglucas status     # is he reachable right now?
pinglucas doctor     # diagnose the whole path, transport included
pinglucas pending    # questions currently waiting for him
pinglucas start      # bring the relay up
```

If `ListAgents` does not show `lucas`, the relay is down — tell the user rather
than working around it silently.
