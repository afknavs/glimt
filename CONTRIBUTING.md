# Contributing

The most useful contributions, in order:

1. **A device report.** Run `glimt bridge test <type> --write` on real hardware and
   open an issue with the output, pass or fail. Three of the bridges have only met
   simulated devices so far.
2. **A new bridge.** One file; the contract is [docs/bridges.md](docs/bridges.md).
3. **A fix**, with a test that fails without it.

## Working on it

```bash
git clone https://github.com/afknavs/glimt && cd glimt
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[all,dev]"
pytest -q
```

Try changes against simulated devices without touching your real setup:

```bash
export GLIMT_HOME=/tmp/glimt-dev GLIMT_PORT=18799
glimt demo
```

## Ground rules

- **Tests never touch a real home.** `tests/conftest.py` points every test at a
  throwaway `GLIMT_HOME`; do not work around it. No test may need hardware or the
  network: fake the device (see `tests/test_bridge_*.py`).
- **Fast is a feature.** A bridge keeps its connection open, never reads before
  writing, and never waits for confirmation inside `apply()`. The conformance check
  enforces the last one.
- **Nothing brand-specific leaks out of a bridge.** State and commands use the common
  vocabulary in [docs/bridges.md](docs/bridges.md).
- **No credentials** in config, logs, error messages, tests or issues.
- **Error messages are for a person standing in the room:** what is wrong, what to try.
- Keep the style of the file you are in: typed, compact, comments that explain *why*.

## Adding a built-in bridge

1. `src/glimt/bridges/<type>.py`, registered in `BUILTIN` in `bridges/__init__.py`.
2. `tests/test_bridge_<type>.py` with a fake device, including a `conformance.check`.
3. `docs/setup/<type>.md`, and a row in the README table that says honestly what it has
   been tested against.
4. If setup needs a credential or a pairing step, a method on the wizard in
   `src/glimt/setup/wizard.py`; otherwise implementing `discover()` is enough for
   `glimt setup <type>` to work.
