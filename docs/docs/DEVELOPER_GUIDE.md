# Developer Notes

## Local Development

```sh
git clone https://github.com/kyleking/tail-cw.git
cd tail-cw
uv sync --all-extras
```

Four gates, and all four run before a change is done:

```sh
uv run ruff check --fix --unsafe-fixes && uv run ruff format
uv run pytest -q -n auto     # about 8s; drop -n auto to use --pdb
uv run mypy
uv run pyright
```

`-n auto` stays out of `addopts` so the default invocation is debuggable.
Every fixture is per-test, so parallel runs are safe.

The `./run` wrapper and `noxfile.py` are calcipy task runners this project has outgrown.
Neither one currently works, so use the commands above.

### Documentation

```sh
uv run python docs/gen_ref_nav.py   # writes docs/reference/ stubs for mkdocstrings
uv run mkdocs build --strict
```

The generator runs first because mkdocs collects its files before any plugin does, so a
stub written during a build is not picked up until the next one.
`--strict` is worth keeping: a relative link in a docstring resolves from the source tree
and not from the generated reference page, so it fails the build rather than shipping
broken.

### Maintenance

Dependency upgrades can be accomplished with:

```sh
uv lock --upgrade
uv sync --all-extras
```

`botocore` is pinned to the window `aiobotocore` accepts, so an upgrade that moves it
alone will fail to resolve.
Check aiobotocore's supported range first
([ADR 0011](adr/0011-async-aws-io-and-blocking-work.md)).

## Publishing

Publishing is automated via GitHub Actions using PyPI Trusted Publishing. Tag creation triggers automated publishing.

```sh
uv run cz bump   # Bumps the version from the commit history, writes the changelog, tags
git push --follow-tags
```

`commitizen` derives the bump from Conventional Commit subjects and rewrites both
`pyproject.toml` and `tail_cw/__init__.py`, so the version is never edited by hand.

### Initial Setup

One-time setup to enable PyPI Trusted Publishing:

**Configure GitHub Environments**

Repository Settings → Environments:
- Create `testpypi` environment (no protection rules)
- Create `pypi` environment with "Required reviewers" enabled

**Register Trusted Publishers**

PyPI: https://pypi.org/manage/project/tail_cw/settings/publishing/
- Owner: `kyleking`
- Repository: `tail-cw`
- Workflow: `publish.yml`
- Environment: `pypi`
    - Or environment `testpypi` (for [TestPyPI](https://test.pypi.org/manage/account/publishing))

### Manual Publishing

For emergency manual publish:

```sh
export UV_PUBLISH_TOKEN=pypi-...
uv build
uv publish
```

## Current Status

<!-- {cts} COVERAGE -->

<!-- {cte} -->
