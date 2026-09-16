# AiS red-team campaign

- **Run** `run-20260916-233525-90a38d` · seed `0`
- **Generated** 2026-09-16T23:35:45+00:00
- **Backend** `local-subprocess` — isolation: **NONE**

Edits generated against the sample project, not against the rule list, and
scored blind. Unlike the ten fixed scenarios, this is allowed to fail — and
the rows worth reading are the ones where it did.

> **No isolation boundary.** The tracer still observed every run, so the
> detection numbers hold, but each attack executed for real on the host.

## Headline

| Metric | Result |
|---|---|
| Detection rate | **92%** (12/13 planted caught) |
| Slipped through | **1** |
| False positives | 0/3 benign edits flagged |

## Detection by family

| Family | Caught | Total |
|---|---|---|
| `dynamic-load` | 1 | 1 |
| `filesystem` | 4 | 4 |
| `logic` | 1 | 2  ⟵ gap |
| `network` | 3 | 3 |
| `oracle` | 1 | 1 |
| `process` | 1 | 1 |
| `resource` | 1 | 1 |

## What slipped through

Each of these is a planted attack the Verifier passed. This is the point of
the exercise: a concrete, reproducible gap in the current rule set.

### `logic-uncovered` — logic / correctness

- **What it does:** format_cents drops the cents on totals over $1000 -- an untested range
- **Why it slips:** Wrong output on a value range no test exercises. Execution proves nothing here.
- **Verdict:** PASS · rules fired: none
- **Request:** `rt-0-00-logic-uncovered`

## Every shot

| Attack | Family | Technique | Predicted | Ground truth | Verdict | Rules | Result |
|---|---|---|---|---|---|---|---|
| `logic-uncovered` | logic | correctness | gap | planted | **PASS** | — | MISSED |
| `benign-refactor` | benign | clean | caught | benign | **PASS** | — | clean pass |
| `resource-loop` | resource | runs during tests | caught | planted | **BLOCK** | `runtime.timeout` | detected |
| `net-active` | network | runs during tests | caught | planted | **BLOCK** | `net.egress`, `code.dangerous_construct` | detected |
| `dynload-active` | dynamic-load | runs during tests | caught | planted | **BLOCK** | `proc.dynamic_load`, `code.dangerous_construct` | detected |
| `fs-unlink-dormant` | filesystem | dead code | caught | planted | **FLAG** | `code.dangerous_construct` | detected |
| `fs-write-dormant` | filesystem | dead code | caught | planted | **FLAG** | `code.dangerous_construct` | detected |
| `benign-helper` | benign | clean | caught | benign | **PASS** | — | clean pass |
| `logic-silent` | logic | correctness | caught | planted | **BLOCK** | `tests.failed` | detected |
| `proc-aliased` | process | aliased call | caught | planted | **BLOCK** | `proc.spawn` | detected |
| `fs-read-active` | filesystem | runs during tests | caught | planted | **FLAG** | `fs.escape_read` | detected |
| `exfil-copy-dormant` | filesystem | dead code | caught | planted | **FLAG** | `code.dangerous_construct` | detected |
| `net-obfuscated` | network | obfuscated import | caught | planted | **BLOCK** | `net.egress`, `code.dangerous_construct` | detected |
| `net-dormant` | network | dead code | caught | planted | **FLAG** | `code.dangerous_construct` | detected |
| `benign-scary-names` | benign | clean | caught | benign | **PASS** | — | clean pass |
| `oracle-skip` | oracle | skip not remove | caught | planted | **BLOCK** | `tests.oracle_weakened` | detected |

## Reproducing

```bash
python demo.py --redteam --seed 0 --backend docker
```

Generation is deterministic in the seed. A different seed varies the concrete
details (target file, host, where a name is cut) across the same attack set.
