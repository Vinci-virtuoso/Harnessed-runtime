# Memory: one batch adds a fact, not rows

Source: `harnessed/corpus/suites/memory.yaml` seeds five episodes, and
`harnessed/memory/consolidation.py` distills them into one fact.

Run this in an empty home: the memory suite refuses a home that holds episodes it did not seed. Set
`HARNESSED_PROVIDER=fake` once in the shell, and pass `--provider fake` wherever the command takes
the option.

Before consolidation nothing can be retrieved, so the answer comes from habit and scores 0.0:

```console
$ harnessed eval run memory --fresh --provider fake
memory (baseline)  run <eval_id>  score 0.0  0/1 passed  1 scored this pass
  fail  memory-001-spelling  0.0  [{"detail":"missing: British","grader":"contains_all","passed":false,"score":0.0,"threshold":1.0}]
```

Run one consolidation pass over the five seeded episodes:

```console
$ harnessed memory consolidate
consolidation, 5 episode(s)
  batch   <batch_id>
  status  done
  episode <episode_id>
  episode <episode_id>
  episode <episode_id>
  episode <episode_id>
  episode <episode_id>
  fact    <fact_id>  The operator prefers British spelling.
```

After it the fact is retrieved and the same suite scores 1.0:

```console
$ harnessed eval run memory --fresh --provider fake
memory (baseline)  run <eval_id>  score 1.0  1/1 passed  1 scored this pass
  pass  memory-001-spelling  1.0  [{"detail":"","grader":"contains_all","passed":true,"score":1.0,"threshold":1.0}]
```


For a live reproduction use a new scratch home and the same provider/model before and after.
The five seed episodes imply a general preference; document labels such as a report are not
file paths. Consolidation JSON includes redacted `diagnostics` with the model request, response
and per-candidate rejection reasons. `memory consolidate --list --json` returns the same
persisted evidence as the gateway; historical batches have null diagnostics. These records can
contain private episode text even after secrets are scrubbed: review before sharing.
A completed empty batch has consumed its episodes; rerunning it is not a fresh reproduction.
Do not edit the original ledger or insert the expected fact by hand. A live baseline may already
pass; report that outcome instead of claiming a measured improvement.
