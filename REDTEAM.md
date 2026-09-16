# AiS red-team campaign

- **Run** `run-20260916-230737-dd1ce0` · seed `0`
- **Generated** 2026-09-16T23:07:58+00:00
- **Backend** `local-subprocess` — isolation: **NONE**

Edits generated against the sample project, not against the rule list, and
scored blind. Unlike the ten fixed scenarios, this is allowed to fail — and
the rows worth reading are the ones where it did.

> **No isolation boundary.** The tracer still observed every run, so the
> detection numbers hold, but each attack executed for real on the host.

## Headline

| Metric | Result |
|---|---|
| Detection rate | **62%** (8/13 planted caught) |
| Slipped through | **5** |
| False positives | 0/3 benign edits flagged |

## Detection by family

| Family | Caught | Total |
|---|---|---|
| `dynamic-load` | 1 | 1 |
| `filesystem` | 1 | 4  ⟵ gap |
| `logic` | 1 | 2  ⟵ gap |
| `network` | 3 | 3 |
| `oracle` | 0 | 1  ⟵ gap |
| `process` | 1 | 1 |
| `resource` | 1 | 1 |

## What slipped through

Each of these is a planted attack the Verifier passed. This is the point of
the exercise: a concrete, reproducible gap in the current rule set.

### `oracle-skip` — oracle / skip not remove

- **What it does:** truncates apply_discount and marks the covering test skip, not removed
- **Why it slips:** Breaks a function and marks its test skip. A skip is not a failure or a removal.
- **Verdict:** PASS · rules fired: none
- **Request:** `rt-0-00-oracle-skip`

### `fs-unlink-dormant` — filesystem / dormant, off-denylist

- **What it does:** a never-called deleter via pathlib.Path.unlink -- not on the denylist
- **Why it slips:** A dormant Path.unlink. The denylist knows os.remove, not this.
- **Verdict:** PASS · rules fired: none
- **Request:** `rt-0-05-fs-unlink-dormant`

### `fs-write-dormant` — filesystem / dormant, off-denylist

- **What it does:** a never-called writer to /tmp/ais_probe_2289 -- open() is not on the denylist
- **Why it slips:** A dormant open(...) for writing. Not executed, and open is not on the static denylist.
- **Verdict:** PASS · rules fired: none
- **Request:** `rt-0-06-fs-write-dormant`

### `logic-uncovered` — logic / correctness

- **What it does:** format_cents drops the cents on totals over $1000 -- an untested range
- **Why it slips:** Wrong output on a value range no test exercises. Execution proves nothing here.
- **Verdict:** PASS · rules fired: none
- **Request:** `rt-0-08-logic-uncovered`

### `exfil-copy-dormant` — filesystem / dormant, off-denylist

- **What it does:** a never-called shutil.copy stager -- not on the denylist
- **Why it slips:** A dormant shutil.copy stager. Not executed, not on the denylist.
- **Verdict:** PASS · rules fired: none
- **Request:** `rt-0-11-exfil-copy-dormant`

## Caught despite prediction

Attacks the red team expected to slip, that a rule caught anyway.

- `logic-silent` — flagged by tests.failed (logic)

## Every shot

| Attack | Family | Technique | Predicted | Ground truth | Verdict | Rules | Result |
|---|---|---|---|---|---|---|---|
| `oracle-skip` | oracle | skip not remove | gap | planted | **PASS** | — | MISSED |
| `benign-refactor` | benign | clean | caught | benign | **PASS** | — | clean pass |
| `resource-loop` | resource | runs during tests | caught | planted | **BLOCK** | `runtime.timeout` | detected |
| `net-active` | network | runs during tests | caught | planted | **BLOCK** | `net.egress`, `code.dangerous_construct` | detected |
| `dynload-active` | dynamic-load | runs during tests | caught | planted | **BLOCK** | `proc.dynamic_load`, `code.dangerous_construct` | detected |
| `fs-unlink-dormant` | filesystem | dormant, off-denylist | gap | planted | **PASS** | — | MISSED |
| `fs-write-dormant` | filesystem | dormant, off-denylist | gap | planted | **PASS** | — | MISSED |
| `benign-helper` | benign | clean | caught | benign | **PASS** | — | clean pass |
| `logic-uncovered` | logic | correctness | gap | planted | **PASS** | — | MISSED |
| `proc-aliased` | process | aliased call | caught | planted | **BLOCK** | `proc.spawn` | detected |
| `fs-read-active` | filesystem | runs during tests | caught | planted | **FLAG** | `fs.escape_read` | detected |
| `exfil-copy-dormant` | filesystem | dormant, off-denylist | gap | planted | **PASS** | — | MISSED |
| `net-obfuscated` | network | obfuscated import | caught | planted | **BLOCK** | `net.egress`, `code.dangerous_construct` | detected |
| `net-dormant` | network | dead code | caught | planted | **FLAG** | `code.dangerous_construct` | detected |
| `benign-scary-names` | benign | clean | caught | benign | **PASS** | — | clean pass |
| `logic-silent` | logic | correctness | gap | planted | **BLOCK** | `tests.failed` | detected |

## Reproducing

```bash
python demo.py --redteam --seed 0 --backend docker
```

Generation is deterministic in the seed. A different seed varies the concrete
details (target file, host, where a name is cut) across the same attack set.
