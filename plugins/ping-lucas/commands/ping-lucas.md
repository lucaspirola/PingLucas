---
description: Ask Lucas something on his Apple Watch and wait for the answer
argument-hint: [question]
---

Send this question to the human operator's watch and continue with whatever
does not depend on the answer.

Question: $ARGUMENTS

Steps:

1. Run `pinglucas status` to confirm the relay is up. If it is not, tell the
   user and stop — do not silently skip the ping.
2. `SendMessage` to `lucas` with the question rewritten as a single
   self-contained line that a person can answer in one word from a wrist. If
   there are options, name them explicitly.
3. Continue with every part of the current task that his answer does not gate.
4. When his reply arrives it will be a `<cross-session-message>` tagged with
   the question it answers. Act on it within this session's existing
   permissions, and say in your report what he decided.
