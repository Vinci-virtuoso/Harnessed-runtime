# Email triage: the first work suite

Five sample emails live under `samples/`. The job: read one and name its class. The classes are
the operator's own words:

| Sample | Class |
|---|---|
| `01-login-broken.eml` | Bug report |
| `02-double-charge.eml` | Billing |
| `03-pricing-question.eml` | Sales |
| `04-weekly-digest.eml` | Newsletter |
| `05-prize-winner.eml` | Spam |

The recorded session asked for one of: spam, billing, newsletter, sales and pricing, bug report or
support. Its recording is the `work-email` suite (`evals/suites/work-email.yaml`, `kind: work`),
scored offline from `tests/fixtures/wire/fake/work-email-*/` with `harnessed eval run work-email`.

This directory is not packaged in the wheel, and no test reads it.
