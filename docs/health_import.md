# Healthcare import — Issue #34

The health jobs read the municipal `.xls`, the MOH accreditation `.xlsx`, and
the regional `comunal_healthcare_institutions_lvivska.xlsx`. They prepare
source identities, service categories and exact address matches, then plan an
idempotent import into existing `points`. No migrations, API routes, scoring
formulas, other categories or point-type radii are changed.

## Sources and meaning

- Municipal file: one named service location per row. Existing
  `lviv_city_council_health` keys remain unchanged.
- MOH accreditation file: one organization can list several addresses. Local
  addresses are split while retaining EDRPOU and locality. The EDRPOU is padded
  to eight digits and combined with an address hash, since an organization is
  not a single physical branch. Administrative territorial associations are
  left for review instead of treating every listed address as a hospital.
- [Regional registry](https://data.loda.gov.ua/en/dataset/5340a2b5-0e06-4e02-9172-9407aa9e285d):
  structured names, EDRPOU and addresses. Scope includes Lviv and the nearby
  localities configured in `health_sources.LOCALITIES`; the DB grid is the
  final geographic gate.
- Nominatim-compatible address geocoder: only exact locality, street,
  street kind, and house matches within the Lviv bounds can be approved. House
  suffixes, fractions and ranges remain distinct. Street or town centroids,
  wrong houses, ambiguous coordinate results and unrelated provider names are
  never substituted. The medical registry establishes provider identity.
- Optional `--type-profiles health-gis.geojson` corroborates inpatient types
  using the [regional social-infrastructure layer](https://www.arcgis.com/home/item.html?id=07821114e0ba4a95aee0f3a51e4d5514).
  This archived GIS layer contains suspect positions and repeated building rows;
  its coordinates are not imported. A diagnostic centre can be reclassified as
  `hospital` only with the same provider identity, exact city/address, an explicit
  hospital type and positive bed count. Explicit outpatient departments retain
  their own type. The matched GlobalID is kept in `category_source`.

Only `hospital`, `polyclinic` and `dentistry` are mapped. Dental clinics take
precedence over the generic word “polyclinic”. Explicit outpatient diagnostic
centres map to `polyclinic`; uncertain centres, rehabilitation services and
administrative organizations remain visible for review. No new radii are
invented. Import also verifies that types belong to `health` and have positive,
finite radii in the chosen database.

## Prepare and review

Install `requirements.txt`. Run from the repository root. Keep source files,
caches, snapshots, CSVs and reports in an external working directory, outside
Git. Existing reviewed staging CSVs remain compatible.

```powershell
python -m app.jobs.health_sources `
  --file "C:/data/health.xls" `
  --file "C:/data/accredited.xlsx" `
  --file "C:/data/comunal_healthcare_institutions_lvivska.xlsx" `
  --existing-points "C:/work/health-database-snapshot.json" `
  --cache "C:/work/health-geocoder-cache.json" `
  --output "C:/work/health-reviewed.csv"
```

`--existing-points` is optional. The JSON shape is `{"points":[...]}` with
`source`, `external_id`, `latitude`, `longitude`, and `h3_id` from a read-only DB
query. Coordinates are reused only for the exact source identity and a matching
H3 resolution 9 cell. `--serp-cache` optionally reuses legacy SerpAPI responses.
Preparation makes no network requests by default. Cached negative results are
also reused. No database is accessed by the source reader.

Live address geocoding requires `--live --limit N --endpoint URL --user-agent ID`.
The endpoint must implement the Nominatim search API. A deliberate small second
pass can add `--normalize-query`, which removes initials and titles in the
query, retains exact result validation, and caches its results for normal reruns.

For the **public** service, the developer must first read and accept the
[Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).
Use it only for a small, one-time task, on one machine and one thread, with an
identifying User-Agent and persistent cache. The client waits 1.1 seconds after
every live request. Do not run concurrent instances, scheduled bulk jobs, or
area-wide POI searches against that service. For recurring or larger tasks,
configure another compatible service or a self-hosted instance.

Geocoding attribution: **© OpenStreetMap contributors**, data under
[ODbL](https://www.openstreetmap.org/copyright). Preserve this attribution with
shared derived datasets. Raw workbooks are never overwritten.

Same-provider records are merged only with identity and normalized-address
evidence. Different departments in the municipal registry remain separate,
including at identical coordinates. Unresolved cross-source identities at one
address are held for review. DB matching also preserves existing source keys;
across sources it skips identically named, same-type providers within 80 m
instead of creating another source identity. Within the OSM source, stable
object IDs are retained and the source reader uses address/contact evidence to
resolve duplicates without collapsing separate practitioners. No point is deleted.

## Database dry-run and explicit write

Offline validation does not load DB configuration or create an engine:

```powershell
python -m app.jobs.health_pipeline import --input "C:/work/health-reviewed.csv"
```

To check real point types, existing records and the H3 grid, use a PostgreSQL
read-only transaction. This produces separate insert/update/unchanged/skip
counts and a safe target fingerprint:

```powershell
python -m app.jobs.health_pipeline import `
  --input "C:/work/health-reviewed.csv" --check-db `
  --env-file ".env" --report "C:/work/health-db-plan.json"
```

After the responsible user explicitly approves that database and the concrete
plan, a write requires all three switches below. Substitute the fingerprint
from the checked plan:

```powershell
python -m app.jobs.health_pipeline import `
  --input "C:/work/health-reviewed.csv" --write-db --approved-reviewed-file `
  --expected-target APPROVED_FINGERPRINT --env-file ".env" `
  --report "C:/work/health-written.json"
```

The importer rechecks current DB state inside the transaction. It excludes
points outside existing resolution 9 cells, rejects supplied H3 IDs that do
not match coordinates, and rejects collisions with another category's keys.
It uses bounded parameterized upserts on `(source, external_id)`, correct
`POINT(longitude latitude)` semantics, SRID 4326, and one transaction. Matching
reruns issue no upserts. It never creates a hexagon or point type, deletes
points, or recalculates scores. A target mismatch fails before connecting.
After a successful commit, verify counts independently:

```sql
SELECT p.source, p.type, count(*)
FROM points p JOIN point_types t ON t.type = p.type
WHERE t.category = 'health'
GROUP BY p.source, p.type ORDER BY p.source, p.type;
```

## Tests

```powershell
python -m pytest tests/test_health_import.py tests/test_health_pipeline.py tests/test_health_sources.py -q
```

These tests use source fixtures and mocked network calls, not production
credentials. They cover malformed sources, address splitting, coordinates,
departments, stable identities, grid filtering, repeated planning, cache reuse,
type configuration and target protection. A real write is intentionally
separate from offline test execution.

## Private clinics and dentistry from OpenStreetMap

`health_osm` supplements registries with named medical POIs regardless of
ownership. It reads an offline `.osm.pbf` extract, not an area-wide Nominatim
search. Use the [Lviv oblast extract](https://download.openstreetmap.fr/extracts/europe/ukraine/)
and preserve its timestamp, checksum and OSM attribution. Keep the large raw
extract outside Git. `osmium` and Shapely 2 are required by this reader.

```powershell
python -m app.jobs.health_osm `
  --file "C:/data/lviv_oblast-latest.osm.pbf" `
  --snapshot "C:/work/health-database-snapshot.json" `
  --known-records "C:/work/health-reviewed.csv" `
  --as-of 2026-11-02 --types dentistry polyclinic `
  --output "C:/work/health-private-reviewed.csv"
```

The snapshot contains `points`, `hex_ids` and `types` from read-only DB queries.
Known staging CSVs can supply addresses and contact evidence for existing keys.
Repeated `--known-records` options are supported. Import the output with the
same guarded `health_pipeline import --check-db` / approved write workflow.

OSM `dentist` maps to `dentistry`, and `clinic`/`doctor` to `polyclinic`. Ownership
is not used as an inclusion filter: public, private and unknown ownership all
remain eligible. Do not interpret missing ownership tags as proof of state or
private ownership. A hospital category is supported for explicit, separately
reviewed uses; the extension command above selects outpatient and dental care.
Labs, veterinary services, alternative-care tags and unconfigured specialties
are held out instead of inventing new radii.

Nodes retain their source coordinates. Valid provider polygons use an interior
point, including for polygons with holes; no street/settlement centres are
substituted. Missing addresses can be filled from a single unambiguous containing
OSM building. Its name, contacts and ownership are never copied to its tenants.
Unnamed providers, closed objects, invalid geometry, insufficient generic
identity, points outside the existing grid and unresolved municipal identities
remain in the audit report. A different private provider is not merged merely
because it shares coordinates with a municipal clinic.

Stable IDs are `openstreetmap_health` plus `node/ID`, `way/ID` or `relation/ID`.
Duplicate node/area representations use name, address, contact and geometry
evidence. Distinct nearby branches with different houses, and unrelated
providers at the same address, retain separate identities. Source object URLs,
address-building URLs and edit timestamps remain in the staging metadata.

The extract includes private practices absent from accreditation lists, but is
not a complete current licensing register. OSM may omit providers or retain old
labels; review uncertain objects and refresh the extract when updating data.
For the OSM reader tests, add `tests/test_health_osm.py` to the test command.
