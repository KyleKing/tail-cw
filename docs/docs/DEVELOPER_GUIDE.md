# Developer Notes

## Local Development

```sh
git clone https://github.com/kyleking/tail-cw.git
cd tail-cw
uv sync
uv run calcipy-pack pack.install-extras

# See the available tasks
uv run calcipy
# Or use a local 'run' file (so that 'calcipy' can be extended)
./run

# Run the default task list (lint, auto-format, test coverage, etc.)
./run main

# Make code changes and run specific tasks as needed:
./run lint.fix test
```
