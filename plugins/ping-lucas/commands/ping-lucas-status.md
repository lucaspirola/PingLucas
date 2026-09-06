---
description: Show whether Lucas is reachable and what is waiting on him
---

Report the state of the PingLucas relay concisely:

1. `pinglucas status` — is the relay running, and under what name?
2. `pinglucas pending` — which questions are still unanswered, and how old?
3. If the relay is down, run `pinglucas doctor` and report the first thing
   that is actually broken. Do not paste the whole diagnostic.

Keep it to a few lines. If everything is healthy and nothing is pending, say
so in one sentence.
