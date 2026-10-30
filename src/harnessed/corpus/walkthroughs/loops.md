# Loops: one run, and the trace it left

Source: `harnessed/cli/run_cmd.py` starts the loop and `harnessed/loop/runs.py` reads the journal
back. The prompt `ping` has a recorded reply in the shipped fixtures.

Set `HARNESSED_PROVIDER=fake` once in the shell, and pass `--provider fake` wherever the command
takes the option. The run id is written `<run_id>`; the trace names the one `harnessed run` printed.

```console
$ harnessed run ping --provider fake
pong
run <run_id>  1 iteration(s), 0 tool call(s), ended on no_more_tool_calls
```

The journal lists the run:

```console
$ harnessed runs list
1 run(s), newest first
  <run_id>  session
```

And the trace shows one object per iteration:

```console
$ harnessed runs trace <run_id>
1 iteration(s) for run <run_id>
  iteration 1
```
