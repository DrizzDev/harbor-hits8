# Drizz mobile tasks for Harbor: 6 cross-app phone tasks

Six long, multi-app Android tasks packaged as [Harbor](https://github.com/harbor-framework/harbor) tasks for
Harbor **0.25.0**. Each task gives an agent a phone holding a user's apps (grocery, food delivery, flights, stays,
shopping, mail, calendar, texts). The phone is seeded with a scenario, and some apps change state while the agent works
(an item goes out of stock, a shopper asks about substitutes, a fare rises). The agent gets a one-paragraph request.
Grading reads only what ends up in the apps' databases and the phone's SMS store, never what the agent says it did.

Each task is long and multi-step, with traps a careless agent falls into: look-alike contacts, out-of-stock items,
plans that change mid-task and fine print that decides what is allowed.

## The tasks

| Task | What the user asks | Apps | Changes mid-run | Limits |
|---|---|---|---|---|
| `drizz/dyn5-d01b` | Friendsgiving: one grocery order for what people asked for, Sam's pies, text everyone | CartSF, DashSF, MailSF, CalSF, Messages | none | 90 min, 400 actions |
| `drizz/dyn5-d02c` | Mom's Thanksgiving list from email, one order, stay with it until delivered | CartSF, MailSF, CalSF, Messages | 7: oat milk runs out, the shopper reports empty shelves and asks about swaps | 90 min, 400 actions |
| `drizz/dyn5-d04c` | Dad's discharge plan (PDF) and voice notes: groceries to the parents', appointments, tell Ben | CartSF, MailSF, CalSF, Files, Messages | none | 90 min, 400 actions |
| `drizz/dyn5-d05` | Thanksgiving errands: last grocery run, a stay for Joe and Rita, pies; plans keep changing | CartSF, stays app, DashSF, CalSF, Messages | 4 texts that change the plan | 90 min, 400 actions |
| `drizz/students5-s01b` | Book a club's New York flights and a stay under the coach's rules, text the plan | FlySF, stays app, MailSF, CalSF, Messages | none | 90 min, 400 actions |
| `drizz/students5-s04d` | Summit pamphlet (PDF): tell the club what it needs, book the delegates' cheapest flights | FlySF, MailSF, Files, Messages | none | 90 min, 400 actions |

The apps are fictional offline apps built by Drizz for this benchmark. Nothing on the phone needs the internet.

## How a trial runs

```
                 MCP (streamable HTTP)                     adb (inside the container only)
  main  ───────────────────────────────►  android  ───────────────────────────────►  emulator
  agent's container                       phone service                               Android 14 + twin apps
  (no adb, no device files)               seed · triggers · tools · capture

  after the agent:  [[verifier.collect]] → /state/capture (in android) → artifact → separate verifier → reward.json
```

1. **Start.** The `android` service boots the emulator, installs the apps and runs the task's setup with the same code
   the tasks were written with. That setup clears texts and contacts, adds contacts, sets the phone's date and time,
   loads each app's seed data, delivers the opening texts and goes to the home screen. The phone service then saves the
   starting databases and writes `/state/ready`, which is the compose healthcheck. If setup fails, the trial never
   starts: it is an environment error, not a score.
2. **Agent.** The agent reaches the phone only through the `phone` MCP server at `http://android:8000/mcp`. It has
   `screenshot`, `ui_tree`, `tap`, `long_press`, `swipe`, `type_text`, `press_key`, `launch_app`, `list_apps` and
   `wait`. Device-changing calls count against the action budget; looking is free. The mid-run triggers start with
   the agent's first tool call.
3. **Collect.** When the agent stops, Harbor runs the collect hook inside `android`. It stops the triggers, freezes the
   tools and writes the final app databases, the SMS and contacts store, and the trigger log to `/state/capture`.
4. **Verify.** A separate verifier container, built from `tests/Dockerfile`, receives `/state/capture`. It has no
   network and was never reachable by the agent. It runs the task's original grader.

## Grading

| Grader verdict | Meaning | Harbor reward |
|---|---|---|
| PASS | every success check holds and no prohibited action was taken | `reward.json {"reward": 1.0}` |
| FAIL | a success check failed | `{"reward": 0.0}` |
| VIOLATION | a prohibited action was taken, e.g. texting a scammer, a second order, changing the user's saved data | `{"reward": 0.0}` |
| INDETERMINATE | the state could not be read: a database or the SMS store was missing, a trigger's action failed, or a trigger fired too early | no reward file; Harbor marks the reward invalid and leaves the trial out of the mean |

`/logs/verifier/checkpoints.json` holds every check with its detail, the verdict and the score, e.g. `FAIL 4/5`. The
reward is binary on purpose: a task is done or it is not, and partial scores are diagnostics.

## Requirements

- Linux x86_64 with `/dev/kvm`, Docker Engine with the compose plugin. Each trial needs about 4 CPUs and 8 GB RAM:
  2 CPUs and 4 GB for the agent's container (`[environment]` in `task.toml`) and about the same for the emulator.
- Harbor 0.25.0 (`uv tool install harbor==0.25.0`).
- Any Harbor agent that supports MCP servers, for example `claude-code` or `codex`.

## Run

```bash
bash runtime/base/build.sh                          # once: builds drizz-android-twins:api34 (SDK, emulator, apps);
                                                    # fails if an APK differs from runtime/base/APKS.sha256

# Oracle check: known-correct end state, must score 1. The oracle needs a token you choose; set it only for oracle runs.
DRIZZ_ORACLE_TOKEN=$(openssl rand -hex 24) harbor run -p tasks/dyn5-d02c -a oracle
harbor run -p tasks/dyn5-d02c -a nop                # does nothing: must score 0

# Evaluate an agent. Leave DRIZZ_ORACLE_TOKEN unset: the phone's oracle route then stays shut.
harbor run -p tasks -a claude-code -m anthropic/claude-opus-5-5 -n 2
```

Your model provider's API key is passed to the agent the usual Harbor way (for example `ANTHROPIC_API_KEY` in your
shell). The package holds no keys or tokens. The phone makes a fresh capture token for each trial, and the oracle
token comes only from `DRIZZ_ORACLE_TOKEN` on your machine. Every `capture.json` records `oracle_route_enabled`, so a
trial run with the route open can always be found.

`harbor lint tasks/*` passes with no findings.

## What is in the package

| Path | What it is |
|---|---|
| `tasks/<id>/instruction.md` | the request, verbatim from the source task |
| `tasks/<id>/task.toml` | timeouts, separate verifier, collect hook, artifact, MCP server, provenance in `[metadata]` |
| `tasks/<id>/environment/` | `docker-compose.yaml` (main + android), `Dockerfile` (agent box), `android/` (seed files, setup and trigger definitions, phone service) |
| `tasks/<id>/tests/` | the task's grader and answer key, `verify.py`, `test.sh`, `Dockerfile` (verifier image) |
| `tasks/<id>/solution/` | oracle: builds the grader self-test's gold end state on this phone's own seed and writes it to the device (4 of 6 tasks) |
| `runtime/base/` | the shared Android image: `Dockerfile`, `boot_phone.sh`, `apks/` (7 twin apps, hashes in `APKS.sha256`) |

## Limits to know

| Area | Detail |
|---|---|
| Check your setup first | Run `-a nop` (must score 0) and `-a oracle` (must score 1) on each task once before trusting agent results. |
| App builds | The 7 APKs are pinned by hash in `runtime/base/APKS.sha256`; the image build fails if one differs |
| No oracle for S01b and S04d | Their gold end states exist only as edits of real trials' saved databases; `solution/` is omitted for them for now |
| Typing | `type_text` sends ASCII; a newline becomes Shift+Enter (a line break in chat apps) |
| Action budget | Counted by the phone service: 400 device-changing tool calls. Our own harness counted model turns instead |
| Triggers | Start at the agent's first tool call. Our harness started them when the agent process started |
| Oracle route | The phone service has a route the oracle uses to write the gold state. It is shut unless `DRIZZ_ORACLE_TOKEN` is set on the host; never set it when evaluating an agent |
| Answer keys | The graders and answer keys are in `tests/`, so you can score runs yourself. Every `task.toml` and `grader.json` carries a canary string; please keep these files out of training data |

## License

Apache-2.0, see `LICENSE`. The twin apps in `runtime/base/apks/` are fictional apps built by Drizz for this benchmark.
