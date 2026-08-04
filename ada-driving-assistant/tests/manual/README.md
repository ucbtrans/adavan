# Manual integration scripts

These are exploratory scripts, not unit tests: they print a transcript for a
human to read instead of asserting, and they need live credentials —
AWS (S3, DynamoDB) plus an Anthropic API key. They are deliberately named
`*_check.py` so `pytest` does not collect them; run one directly from the
project root:

```bash
python tests/manual/route_filter_check.py
python tests/manual/offroute_streets_check.py
python tests/manual/perf_response_time.py --trials 5      # times /api/ask against deployed prod
```

## Known stale: the city_objects.json data source

Both scripts load traffic objects from `s3://$S3_BUCKET/CA/Berkeley/city_objects.json`.
That file was retired when events moved to the DynamoDB `ada-events` table
(see `migrate_events_to_dynamo.py`), so the `s3.get_object` call now fails with
`NoSuchKey`. To revive a script, replace that load with the events layer:

```python
from datetime import datetime, timezone
from events import get_events_by_city

ALL_OBJECTS = get_events_by_city("Berkeley", datetime.now(timezone.utc))
```

Until that is done, expect both to fail at the S3 read. What they cover is
otherwise still worth keeping:

- `route_filter_check.py` — fetches a real OSRM route, runs
  `find_objects_along_route()` over it, and verifies Claude's answer does not
  mention off-route streets.
- `offroute_streets_check.py` — nine phrasings of "how is street X?", checking
  `find_streets_mentioned()` detection and that the answer names the street and
  either reports it clear or lists obstacles.
