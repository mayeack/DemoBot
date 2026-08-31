# Releasing DemoBot

How DemoBot is versioned, tagged, and published. Adopted 2026-08-31, starting at
**v4.1.0** — the repo had no tags before that, so there is no earlier history to
reconcile.

## Versioning

Semantic versioning, `MAJOR.MINOR.PATCH`.

| Bump | When | Examples |
|---|---|---|
| **MAJOR** | A breaking change to the HTTP API, the `.env` contract, or the settings-store schema — anything that makes an existing deployment stop working until someone edits config. | removing an endpoint, renaming an `.env` key without a fallback |
| **MINOR** | Additive behaviour. New endpoints, new UI, new integrations, new `.env` keys that default sensibly. | the Settings integration cards (4.1.0) |
| **PATCH** | Fixes and copy changes only, no new surface. | a wrong default sourcetype, a stale banner string |

### The two sources of truth

Both must move together, in the same commit:

- `backend/config.py` → `app_version` (the default)
- `.env.example` → `APP_VERSION` (the documented value)

`app_name` (`"DemoBot v4"`) tracks the **MAJOR line only**. Leave it alone on a
minor or patch bump — it also appears in `run.sh` banners, `Containerfile`, and
`requirements.txt`, and those say "v4" for the whole 4.x series.

> **A box can report a stale version.** `APP_VERSION` in a host's local `.env`
> overrides the code default for that process, and `.env` is not tracked. A
> replica deployed before a bump keeps reporting the old number until its `.env`
> is updated. `GET /health` reports what that box actually has, not what `main`
> says.

The version is surfaced through the FastAPI app version (so `/docs` and
`/openapi.json`), `GET /health`, and `GET /`.

## Tagging

- **Annotated** tags, named `vX.Y.Z` — `git tag -a`, never a lightweight tag, so
  the tagger and date are recorded.
- Tag message: `DemoBot X.Y.Z`.
- **Tag `main`, and only after the PR has merged.** Never tag a feature branch:
  the merge commit is what ships, and a branch tip can be rebased or deleted out
  from under the tag.
- One tag per released version. To correct a mistake, cut a new patch version —
  do not move a published tag.

## Publishing a release

After the version-bump PR merges:

```bash
git checkout main && git pull
```

Confirm `main` actually carries the bump before tagging — this is the step that
catches tagging the wrong commit:

```bash
grep app_version backend/config.py
```

Then tag and push:

```bash
git tag -a v4.1.0 -m "DemoBot 4.1.0"
git push origin v4.1.0
```

Then publish the GitHub release. `--generate-notes` builds the changelog from the
merged PRs since the previous tag, which is why the tag has to exist first:

```bash
GH_TOKEN=$(gh auth token --user mayeack) \
  gh release create v4.1.0 --title "DemoBot 4.1.0" --generate-notes
```

For the first release, or any time you want the notes to lead with something
other than a PR list, write them by hand with `--notes "..."` instead and keep
the summary to what an operator would notice: new surface, changed defaults, and
anything that needs a restart or a config edit on their box.

## Checklist

1. Feature PR is reviewed and green.
2. Bump `app_version` and `APP_VERSION` in one commit on that branch.
3. Merge the PR (merge commit — this repo does not squash; see #49–#52).
4. `git checkout main && git pull`, and verify the bump is present.
5. `git tag -a vX.Y.Z -m "DemoBot X.Y.Z"` and `git push origin vX.Y.Z`.
6. `gh release create vX.Y.Z --title "DemoBot X.Y.Z" --generate-notes`.
7. If a deployed box should report the new version, update `APP_VERSION` in its
   `.env` and restart it — the tag alone does not change a running replica.
