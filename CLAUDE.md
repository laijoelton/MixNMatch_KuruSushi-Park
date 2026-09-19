# Mix_and_Match — Grand Park Auto Dispatcher

HackMY IoT 2026 · Track 2. A FastAPI dispatcher on `:8080` driving a closed
simulator on `:9898`. We cannot modify the simulator.

## 1. The engineering log is the source of truth

`docs/ENGINEERING_LOG.md`

- **Read it before changing anything.** It records why each fix is shaped the
  way it is. Several of them look wrong until you know what they defend
  against.
- **Append to it after every change, in the same session.** New bug → a
  `### 4.x` subsection with symptom, root cause, fix, and how it was verified.
  New behaviour or setting → update §5 (edge cases) or §6 (configuration).
- If a change contradicts something already written there, correct that text.
  Do not leave two versions of the truth in the file.

## 2. Keep the launchers working

`START.bat` / `STOP.bat` are how the team runs this, including during the demo.
If a change adds a dependency, a port, a service, a URL, or a required `.env`
key, update them in the same commit — and say so, so they get re-run.

## 3. Check the simulator guidelines before guessing

`Simulator Guidelines/*.pdf` — API, Webhooks, Penalties, Settings, Components,
Level 1. Read the relevant one before implementing against the simulator.

**But treat them as a hypothesis, not a contract.** The documented signature
recipe, the `detectedCars` shape, and the billing rules each contradict what
the binary actually does. Where the docs and live traffic disagree, live
traffic wins — and the disagreement goes in the log.

## House rules

- `AUTOPILOT=false` is dry-run: nothing reaches the simulator. Check
  `/healthz` before concluding a command "did nothing".
- Do not commit or push to `main` unless asked. Work on a branch.
- Wall-clock waits must scale by `settings.game_speed`, or they stop being
  proportional the moment the game speed changes.
