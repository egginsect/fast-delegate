# Contributing

fast-delegate is a public routing skill. It recommends a model for one predefined
delegation task. Public issues and pull requests are welcome.

## Development

- The router (`skills/fast-delegate/scripts/fdel.py`) is Python 3 and uses the
  standard library only.
- Run the test suite with `make acceptance` or
  `python3 -m unittest discover -s tests`.
- Keep changes scoped. Do not add network calls or new runtime dependencies to
  the router.
- All contributions are under the MIT license.

## Documentation

- `README.md` — install and usage
- `skills/fast-delegate/SKILL.md` — operational core
- `skills/fast-delegate/REFERENCE.md` — flags, pricing, quota, and configuration

## Pull requests

Describe the change and how you tested it. Do not include credentials, personal
paths, or unpublished project names.
