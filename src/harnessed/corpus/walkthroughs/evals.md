# Evals: a score that moves only when the work got better

Source: `harnessed/improve/promoter.py` decides by arithmetic, `harnessed/evals/runner.py` scores,
and the cases live in `harnessed/corpus/suites/smoke.yaml`.

Every command here replays the shipped fixtures offline. Set `HARNESSED_PROVIDER=fake` once in the
shell, and pass `--provider fake` wherever the command takes the option. Ids are written
`<cand_id>` and the like; a later command that names one takes the value an earlier command printed.

Score the smoke suite. One case fails, so the score is 0.8:

```console
$ harnessed eval run smoke --fresh --provider fake
smoke (baseline)  run <eval_id>  score 0.8  4/5 passed  5 scored this pass
  pass  smoke-001-capital  1.0  [{"detail":"","grader":"exact_match","passed":true,"score":1.0,"threshold":1.0}]
  pass  smoke-002-offline-summary  1.0  [{"detail":"","grader":"contains_all","passed":true,"score":1.0,"threshold":1.0}]
  fail  smoke-003-json-only  0.0  [{"detail":"not JSON: Expecting value: line 1 column 1 (char 0)","grader":"json_object","passed":false,"score":0.0,"threshold":1.0}]
  pass  smoke-004-version-shape  1.0  [{"detail":"","grader":"regex_match","passed":true,"score":1.0,"threshold":1.0}]
  pass  smoke-005-replay  1.0  [{"detail":"","grader":"tool_called","passed":true,"score":1.0,"threshold":1.0},{"detail":"","grader":"contains_all","passed":true,"score":1.0,"threshold":1.0}]
```

Ask for one improvement and look at it without deciding anything:

```console
$ harnessed improve run --dry-run --provider fake
improvement run <imp_id> over suite smoke
candidate <cand_id>
score 0.8 -> 1.0
dry run - no promotion was decided and the active set is untouched
```

Promote it. The decision is arithmetic: the score rose and nothing regressed.

```console
$ harnessed improve promote <cand_id> --provider fake
improvement run <imp_id> over suite smoke
resumed - 1 of 5 steps run this pass
candidate <cand_id>
score 0.8 -> 1.0
candidate <cand_id>: promoted
rule: score - the aggregate score rose by more than EPSILON and nothing regressed
score: 0.8 -> 1.0
passes: 4 -> 5
regressions: 0
soft_drops: []
provider=fake - this proves the promotion mechanism, not model quality
```

Score again. The same suite now passes every case, which is 1.0 with 0 regressions:

```console
$ harnessed eval run smoke --fresh --provider fake
smoke (baseline)  run <eval_id>  score 1.0  5/5 passed  5 scored this pass
  pass  smoke-001-capital  1.0  [{"detail":"","grader":"exact_match","passed":true,"score":1.0,"threshold":1.0}]
  pass  smoke-002-offline-summary  1.0  [{"detail":"","grader":"contains_all","passed":true,"score":1.0,"threshold":1.0}]
  pass  smoke-003-json-only  1.0  [{"detail":"","grader":"json_object","passed":true,"score":1.0,"threshold":1.0}]
  pass  smoke-004-version-shape  1.0  [{"detail":"","grader":"regex_match","passed":true,"score":1.0,"threshold":1.0}]
  pass  smoke-005-replay  1.0  [{"detail":"","grader":"tool_called","passed":true,"score":1.0,"threshold":1.0},{"detail":"","grader":"contains_all","passed":true,"score":1.0,"threshold":1.0}]
```
