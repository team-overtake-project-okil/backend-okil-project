# Backend data jobs

Run commands from the repository root with the project's virtual environment
active (on Windows, `.\.venv\Scripts\python.exe` can replace `python`).

```sh
python -m app.jobs --help
python -m app.jobs --dry-run
python -m app.jobs
```

`--dry-run` prints the execution plan and input paths only. It works without
`.env`, third-party dependencies, source files or a database. It does **not**
validate the inputs or call the individual jobs' `--dry-run` implementations,
some of which access the database or paid geocoding APIs.

## Prerequisites

1. Use Python 3.11+ and install `python -m pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and set `DB_URL` to the intended PostgreSQL
   database. The existing database client requires SSL. The schema must match
   `app/models.py`, with PostGIS, h3 and pgvector installed. The API's schema
   initialization can create missing tables/extensions; it does not migrate
   existing tables or seed `point_types`. This runner does not initialize the
   database or start FastAPI, Redis or the AI model.
3. Populate `point_types` with the team's category assignments and all three
   influence radii before the run. See the [catalog](point_type_catalog.md).
   Jobs validate their required types and require finite radii with
   `0 < r1 <= r2 <= r3`. They do not insert incomplete types or invent radii.
   Prepare any reviewed health, study and playground data separately before
   refreshing scores; existing points in these categories are used as-is.
4. Set `SERPAPI_KEYS` in `.env` to one or more comma-separated SerpAPI keys with
   available quota. The full run performs live Google Maps searches and consumes
   quota (multiple queries per grid center, potentially several pages each).
   Network access to `https://serpapi.com` is required. `SERPAPI_KEY` (singular)
   belongs to separate manual scripts and is not used by this runner.
5. Provide the EasyWay GTFS ZIP, by default `data/static.zip` (included in the
   repository). It must contain `stops.txt`, `routes.txt`, `trips.txt` and
   `stop_times.txt` at its root with usable stops and routes. `shapes.txt` is
   optional: segments fall back to straight lines between stops without shapes.
6. Obtain the municipal shelter source files from the team's source dataset and
   place both in `data/Sources/` (they are not included in Git):
   - `2. Найпростіші укриття.xls`: `x`/`y` coordinates; optional `object`,
     `street_name`, `housenumber` for labels.
   - `3. Захисні споруди.xls`: `longitude`/`latitude`, `object_type`, `status`;
     optional `street_name`, `housenumber`. The existing status/type filters apply.

Alternative source locations, including paths with spaces:

```sh
python -m app.jobs --gtfs-archive "C:/data/transport/static.zip" --sources-dir "C:/data/Sources"
```

Relative paths resolve from the current working directory. Both transport jobs
always receive the same GTFS archive. The runner checks Python imports, the
SerpAPI setting, required ZIP members, both shelter files and the database's
required point types before starting any job. Data parsing and external service
availability are still checked by the jobs themselves.

## Execution order and scope

| Order | Job | Reason |
| --- | --- | --- |
| 1 | `load_hexagons` | Create the base H3 grid used by imports. |
| 2 | `load_gtfs` | Replace EasyWay stops, routes and stop links; ensure stop hexagons exist. |
| 3 | `load_segments` | Rebuild route segments and links using the newly imported stops/routes. |
| 4 | `googlemaps` | Upsert daily, sport, food and leisure POIs and their missing hexagons. |
| 5 | `load_shelters` | Import shelters inside the resulting grid. |
| 6 | `count_category_ratings` | Recalculate stored category scores after all automatic imports. |

Mobility is computed by the existing API service, not stored in `hex_table`.
The runner does not need TravelTime, HERE or Groq credentials.

The full run means the six repeatable automatic jobs above. It is a refresh of
a prepared database, not a complete bootstrap of every category. These files
are deliberately outside the automatic run:

- `health/*`: source preparation, geocoding, review and guarded imports with
  explicitly chosen input files/target. Follow [health import](health_import.md).
- `parse_education.py` and `parse_playgrounds.py`: one-off municipal imports
  with hardcoded author-specific source paths, geocoding caches/review files
  and separate credentials (`SERPAPI_KEY` / `GEOAPIFY_API_KEY`). They also use
  the optional `calamine` Excel engine. They need separate preparation before
  use and are not imported by the runner.
- `load_leisure_clean.py`: a curated cleanup which deletes/replaces leisure
  data and relies on `leisure_ids` and `parse_leisure_google` modules/cache
  absent from this repository. It is not a general refresh job.
- `runner.py`, `validation.py` and package entry points are orchestration
  helpers, not additional data jobs.

## Progress and failures

Each job prints `[n/6] START <name>` before it runs and `[n/6] OK <name>` only
after successful completion; the job's own output remains visible. All jobs
share one asyncio event loop, and the database engine is disposed afterward.
The CLI writes UTF-8 output, including redirected logs on Windows, so Ukrainian
paths, messages and emoji do not fail with a console encoding error.

On the first failure the runner prints `FAILED <job or prerequisite>: <reason>`
to stderr, skips every subsequent job and exits with status **1**. Missing or
invalid input, empty required imports and exhausted SerpAPI keys are failures,
not successful skips. Success exits **0**, invalid CLI arguments exit **2**,
and Ctrl+C exits **130**. PowerShell exposes the status as `$LASTEXITCODE`;
POSIX shells use `$?`.

Each job keeps its existing transaction behavior; the whole run is not atomic.
Earlier commits remain after a later failure (and scoring commits in chunks).
Fix the cause and rerun the command. GTFS replaces its existing source data,
Google Maps upserts by source/external ID, and shelters keep existing records
with the same source/external ID. Rerunning also repeats live SerpAPI searches.
