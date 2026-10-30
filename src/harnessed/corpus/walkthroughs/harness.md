# Harness: what runs where

Source: `harnessed/cli/doctor_cmd.py` prints the platform section, `harnessed/tasks/installer.py`
chooses the scheduler entry, and `harnessed/cli/agents_cmd.py` lists the declared agents.

None of these three commands takes `--provider`; set `HARNESSED_PROVIDER=fake` once in the shell and
they stay offline. A line `...` in a block means the rest of the output is not compared.

Block markers: a fence written `console linux`, `console macos` or `console windows` shows what that
OS prints and is run only there. A bare `console` fence is the same on every OS.

`doctor` checks the home and ends with the platform section, one line per seam:

```console linux
$ harnessed doctor
home: <path>
env: no .env in this folder
ok    python.version  <version>
ok    sqlite.fts5  <detail>
ok    database.wal  <path> open in wal mode
ok    migrations.current  at version 28 of 28, checksums match, nothing outstanding
ok    fixtures.corpus  22 scopes under fake; vendors: fake, jev
ok    provider.configured  HARNESSED_PROVIDER=fake needs no API key
The 5 rungs:
  rung 1  CLI           live      provider fake, harnessed run / eval / memory<routing>
  rung 2  Coding agent  not yet   harnessed agents add - templates: claude-code, codex, pi, grok, hermes, deepseek, generic-cli
  rung 3  Gateway       <state>   <detail>
  rung 4  Dashboard     <state>   <detail>
  rung 5  router.probe  not yet   harnessed doctor --live - one noul, one choice and one score to the pinned router model
schedule: <schedule>: not installed (harnessed schedule install); never ran
platform: host: <host>
platform: jail: <jail>
platform: process identity: <identity>
platform: scheduler: <scheduler>
platform: file mode: chmod 0600
platform: console encoding: <encoding>
```

```console macos
$ harnessed doctor
home: <path>
env: no .env in this folder
ok    python.version  <version>
ok    sqlite.fts5  <detail>
ok    database.wal  <path> open in wal mode
ok    migrations.current  at version 28 of 28, checksums match, nothing outstanding
ok    fixtures.corpus  22 scopes under fake; vendors: fake, jev
ok    provider.configured  HARNESSED_PROVIDER=fake needs no API key
The 5 rungs:
  rung 1  CLI           live      provider fake, harnessed run / eval / memory<routing>
  rung 2  Coding agent  not yet   harnessed agents add - templates: claude-code, codex, pi, grok, hermes, deepseek, generic-cli
  rung 3  Gateway       <state>   <detail>
  rung 4  Dashboard     <state>   <detail>
  rung 5  router.probe  not yet   harnessed doctor --live - one noul, one choice and one score to the pinned router model
schedule: <schedule>: not installed (harnessed schedule install); never ran
platform: host: <host>
platform: jail: <jail>
platform: process identity: <identity>
platform: scheduler: <scheduler>
platform: file mode: <mode>
platform: console encoding: <encoding>
```

```console windows
$ harnessed doctor
home: <path>
env: no .env in this folder
ok    python.version  <version>
ok    sqlite.fts5  <detail>
ok    database.wal  <path> open in wal mode
ok    migrations.current  at version 28 of 28, checksums match, nothing outstanding
ok    fixtures.corpus  22 scopes under fake; vendors: fake, jev
ok    provider.configured  HARNESSED_PROVIDER=fake needs no API key
The 5 rungs:
  rung 1  CLI           live      provider fake, harnessed run / eval / memory<routing>
  rung 2  Coding agent  not yet   harnessed agents add - templates: claude-code, codex, pi, grok, hermes, deepseek, generic-cli
  rung 3  Gateway       <state>   <detail>
  rung 4  Dashboard     <state>   <detail>
  rung 5  router.probe  not yet   harnessed doctor --live - one noul, one choice and one score to the pinned router model
schedule: <schedule>: not installed (harnessed schedule install); never ran
platform: host: <host>
platform: jail: <jail>
platform: process identity: <identity>
platform: scheduler: <scheduler>
platform: file mode: <mode>
platform: console encoding: <encoding>
```

`schedule install --dry-run` prints the one entry the OS would get and writes nothing. The entry
differs by OS:

```console linux
$ harnessed schedule install --dry-run
<scheduler>, dry run: nothing written.
...
```

```console macos
$ harnessed schedule install --dry-run
launchd, dry run: nothing written.
launchctl bootstrap gui/<uid> <path>
...
```

```console windows
$ harnessed schedule install --dry-run
Task Scheduler, dry run: nothing written.
schtasks /Create /TN "<task>" /XML <file> /F
...
```

`agents list` prints every declared agent. A fresh home declares none:

```console
$ harnessed agents list
no agent CLIs configured: see `harnessed agents add`
```
