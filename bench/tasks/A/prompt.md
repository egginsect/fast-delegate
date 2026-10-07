# Task A: Parse CLI Module

You have been given a Python CLI module in `cli.py`. Extract the following information:

1. **Subcommands**: For each argparse subcommand, record its name, the handler function it calls (via set_defaults), and the line number where the subparser is added.

2. **Exit Codes**: For each exit-code constant defined in the module, record its name, its numeric value, and the line number where it is defined.

Write your answer to `answer.json` in the current directory with the following structure:

```json
{
  "subcommands": [
    {
      "name": "<subcommand name>",
      "handler": "<handler function name>",
      "line": <line number>
    }
  ],
  "exit_codes": [
    {
      "name": "<constant name>",
      "value": <numeric value>,
      "line": <line number>
    }
  ]
}
```

**Acceptance criteria**: All subcommands and exit codes must match exactly (by name and value); line numbers must be within ±1 of the correct line.
