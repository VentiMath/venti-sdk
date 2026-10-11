# VentiMath Python SDK

Download answer keys, import your problem classifications, and find problems by skill.
Requires Python 3.10 or newer, with no runtime dependencies.

## Install

```sh
pip install ventimath
# Or add it to a uv project:
uv add ventimath
```

The default API base URL is
`https://us-central1-ventimath.cloudfunctions.net`; use `base_url=` for a deployed
test environment or functions emulator.

## Download answer keys for the existing classifier

Answer-key reads are public and always omit authentication, even when the client
has a token. Download a file; the classifier's existing `--answer-key` reads a
local JSON path, not a URL.

```python
from ventimath import VentiMathClient

public = VentiMathClient()
result = public.download_answer_key("answer-key.json", exam="EXAM_ID")
print("Missing problems:", result["missing"])
print("No usable key:", result["unavailable"])

# Or select up to 100 canonical problem IDs:
keys = public.answer_keys(ids=["PROBLEM_ID_1", "PROBLEM_ID_2"])
```

```sh
python classify_skills.py PROBLEM_ID --answer-key answer-key.json
```

The response and saved file have the same shape:

```json
{
  "problems": {
    "PROBLEM_ID": {"correctChoice": "B", "answerText": "2"}
  },
  "missing": [],
  "unavailable": []
}
```

Text-answer records carry `answerText`; accepted alternatives may also appear in
`allowedAnswers`. Exam requests are bounded to 500 placements. Unknown canonical
IDs appear in `missing`; proofs, absent keys, and placeholders appear in
`unavailable`.

## Import classifications on your own profile

Create your VentiMath account and mint **your own** personal access token in
Settings → Developer. Select `tags.read` and `tags.write`. Keep the token in your
environment rather than in source files. The authenticated account owns the set;
there is no option to assign your upload to someone else's account.

`VentiMathClient(token=...)` uses an explicit token. `VentiMathClient.from_env()`
reads `VENTI_API_KEY` and optionally `VENTI_API_URL`. Neither stores credentials.
The ordinary `VentiMathClient()` constructor remains anonymous even if a token
environment variable exists.

Sets are **public by default**. Pass `public=False` at creation to keep a set
private to your account, as this import example does:

```python
from pathlib import Path
from ventimath import (
    VentiMathClient,
    load_classification_files,
    load_vocabulary_csv,
)

client = VentiMathClient.from_env()
vocabulary = load_vocabulary_csv("skill-lists/skills-merged.csv")
created = client.create_tag_set(
    "amc-skills",
    "My AMC skill classifications",
    public=False,
    vocabulary=vocabulary,
)
set_id = created["set"]["id"]

# Use saved per-problem output from a single classifier run folder.
records = load_classification_files(sorted(Path("output/skills/MODEL_DIRECTORY").glob("*.json")))

# Or use the classifier's combined output instead:
# records = load_classification_files(["combined.json"])

preview = client.import_classifications(set_id, records, dry_run=True)
print(preview)

# After inspecting the preview, upload the same records.
report = client.import_classifications(set_id, records)
print("Imported:", report["imported"], "unchanged:", report["unchanged"])
for rejected in report["rejected"]:
    print(rejected)
```

Individual records use `{problemId, result, model, reasoningEffort}`. Combined
files use `{problemId, llmResult}` and usually omit model provenance; supply
`model="MODEL_USED"` and `reasoning_effort="EFFORT_USED"` to `import_classifications`
when those values are known. Per-record values take precedence. Both shapes pass
through to the API unchanged. The server validates each classification and
discards copied prompts, raw replies, statements, and answer fields.

Vocabulary CSV headers are `category`, `skill`, and `description`, in any order.
Quoted commas/newlines, escaped quotes, and a UTF-8 BOM are supported. On import,
`vocabulary=` merges by skill name; supplied entries update matching descriptions
and categories. Accepted records add unseen skill names. To explicitly replace
the vocabulary, use `update_tag_set(set_id, vocabulary=...)`.

## Retrieve problems by skill

```python
# Your own sets, including private sets; needs tags.read.
mine = client.list_tag_sets()

# Public discovery always omits the token, even on an authenticated client.
shared = client.list_public_tag_sets(owner="OWNER_NUMERIC_ID", limit=20)
# Pass shared["nextCursor"] as cursor= to request the next page.

# Set listing returns metadata. Fetch a set to get its vocabulary.
tag_set = client.get_tag_set(set_id)
print(tag_set["set"]["vocabulary"])

# One page, preserving the response envelope and pagination cursor:
page = client.tagged_problems(
    set_id, skill="Casework", importance="essential", limit=25
)

# Or iterate across every page:
for problem in client.iter_tagged_problems(set_id, skill="Casework"):
    print(problem["id"], problem["details"]["statement"])
    print(problem["classification"])
```

Skill names match exactly. Importance is `essential`, `time-saving`, or `helpful`
and requires a skill filter.
Use `public=True` on `get_tag_set`, `tagged_problems`, or `iter_tagged_problems`
to read a public set without sending the client's token. Public problem records
omit catalog answer fields; their attached classifications remain attributed to
the set owner.

## Reports, errors, and retries

Imports are split into requests of at most 200 records and 2 MiB of UTF-8 JSON.
All input is checked for JSON encodability and individual request size before
any request is sent. The aggregate response contains `imported`, `unchanged`,
`rejected`, `dryRun`, `total`, `processed`, and `remaining`. Rejected entries retain
the server's error code and problem ID; their zero-based `index` refers to your
complete input sequence. Row rejections do not raise an exception; inspect them.

Dry runs validate each batch against the currently stored set without writing.
For imports spanning batches, repeated problem IDs or the cumulative vocabulary
limit can make actual counts differ from the preview. Inspect the final report.

```python
from ventimath import VentiMathAPIError, VentiMathImportError

try:
    report = client.import_classifications(set_id, records)
except VentiMathImportError as error:
    print("Completed requests:", error.report)
    print("HTTP status, if available:", error.status)
    print("Server payload, if available:", error.payload)
    # A failed response does not prove the last request was uncommitted.
    # Retrying the identical input is safe; unchanged records are not rewritten.
except VentiMathAPIError as error:
    print(error.status, error.payload)
```

Other methods raise `VentiMathAPIError` with the original HTTP status and server
payload for HTTP failures. `VentiMathConnectionError` covers transport failures,
including interrupted response bodies;
`VentiMathProtocolError` covers unexpected responses. Invalid local arguments
raise `ValueError`. Imports wrap transport/protocol/HTTP failures in
`VentiMathImportError` to preserve the counts from completed requests.

These methods call the actual REST routes: `GET /answerKeys`,
`GET|POST /problemTags/sets`, `GET|PATCH /problemTags/sets/{id}`,
`POST /problemTags/sets/{id}/import`, and
`GET /problemTags/sets/{id}/problems`. You can call those endpoints directly from
your existing Python HTTP code; the SDK is optional.

## Development

Install from a source checkout with `python -m pip install .`.

From this package directory, without installing or contacting a server:

```sh
PYTHONPATH=src python -B -m unittest discover -s tests -v
```

All transport calls in these tests are mocked. They exercise exact URLs and
headers, anonymous discovery, private ownership request shaping, both classifier
record formats, CSV parsing, Unicode batch limits, pagination, rejected redirects,
and partial-failure reports.
