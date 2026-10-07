# Examples

This directory contains experiment scripts and their markdown summaries.

## Experiment Scripts

- `routing_experiment.py` - Basic routing comparison
- `full_cli_experiment.py` - Full CLI routing test
- `live_ranked_experiment.py` - Live ranked routing experiment
- `paired_cost_replay.py` - Cost replay analysis
- `compact_routing_payload.py` - Compact payload demonstration

## Regenerating Raw Outputs

The raw JSON outputs from these experiments (typically multi-megabyte files) are not included in the repository. To regenerate them, run the corresponding experiment scripts with appropriate configuration.

For example:
```bash
python routing_experiment.py --live
python full_cli_experiment.py --live
python live_ranked_experiment.py --live
```

The markdown files in this directory contain summaries and analysis of the experiment results.
