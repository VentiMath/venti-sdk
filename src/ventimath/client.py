"""Thin JSON client for the answerKeys and problemTags Cloud Functions."""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterable, Mapping
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_BASE_URL = "https://us-central1-ventimath.cloudfunctions.net"
MAX_IMPORT_RECORDS = 200
MAX_IMPORT_BYTES = 2 * 1024 * 1024


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Function endpoints are direct: never forward a token or change a write's method.
        raise HTTPError(request.full_url, code, message, headers, response)


urlopen = build_opener(_NoRedirectHandler()).open


class VentiMathError(Exception):
    """Base class for transport, protocol, and aggregate import failures."""


class VentiMathAPIError(VentiMathError):
    def __init__(self, status: int, payload: Any, url: str):
        self.status = status
        self.payload = payload
        self.url = url
        super().__init__(f"HTTP {status}: {payload}")


class VentiMathConnectionError(VentiMathError):
    """The request could not complete; the server may have received it."""


class VentiMathProtocolError(VentiMathError):
    """The server returned an unexpected response shape."""


class VentiMathImportError(VentiMathError):
    """An import stopped; report contains counts from completed requests only."""

    def __init__(self, report: dict[str, Any], cause: VentiMathError):
        self.report = report
        self.cause = cause
        self.status = getattr(cause, "status", None)
        self.payload = getattr(cause, "payload", None)
        super().__init__(
            f"Import stopped after {report['processed']}/{report['total']} records: {cause}. "
            "The last request may have committed. Retry the same records safely; "
            "unchanged classifications are not written again."
        )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _set_path(set_id: str) -> str:
    if not isinstance(set_id, str) or not set_id.strip() or "/" in set_id:
        raise ValueError("set_id must be a nonempty document ID without slashes")
    return f"problemTags/sets/{quote(set_id, safe='')}"


def _page_query(limit: int | None, cursor: str | None) -> dict[str, Any]:
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError("limit must be an integer from 1 to 100")
    return {"limit": limit, "cursor": cursor}


def load_vocabulary_csv(path: str | os.PathLike[str]) -> list[dict[str, str]]:
    """Read category/skill/description CSV, including BOM and quoted newlines."""
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        try:
            headers = [value.strip().lower() for value in next(reader)]
            fields = ("skill", "category", "description")
            if any(headers.count(field) != 1 for field in fields):
                raise ValueError("CSV needs one each of skill, category, description headers")
            entries = []
            for row in reader:
                if not any(value.strip() for value in row):
                    continue
                if len(row) != len(headers):
                    raise ValueError(f"CSV line {reader.line_num} has an incorrect column count")
                entry = {field: row[headers.index(field)].strip() for field in fields}
                if not entry["skill"] or not entry["category"]:
                    raise ValueError(f"CSV line {reader.line_num} needs a skill and category")
                entries.append(entry)
        except StopIteration as error:
            raise ValueError("vocabulary CSV is empty") from error
        except csv.Error as error:
            raise ValueError(f"invalid vocabulary CSV: {error}") from error
    if not 1 <= len(entries) <= 500:
        raise ValueError("vocabulary CSV must contain 1–500 skills")
    return entries


def load_classification_files(
    paths: Iterable[str | os.PathLike[str]],
) -> list[Any]:
    """Load individual {result} records or combined arrays with {llmResult}."""
    records = []
    for path in paths:
        with Path(path).open(encoding="utf-8-sig") as source:
            data = json.load(source)
        if isinstance(data, list):
            records.extend(data)
        elif isinstance(data, dict) and "problemId" in data:
            records.append(data)
        else:
            raise ValueError(f"{path} must hold a classifier record or an array of records")
    return records


class VentiMathClient:
    """Explicit-token client; token=None is anonymous and never reads credentials."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60,
    ):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("base_url must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain credentials, a query, or a fragment")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token = token

    @classmethod
    def from_env(cls, *, timeout: float = 60) -> VentiMathClient:
        """Read VENTI_API_KEY/VENTI_API_URL without writing a credential file."""
        return cls(
            token=os.environ.get("VENTI_API_KEY"),
            base_url=os.environ.get("VENTI_API_URL") or DEFAULT_BASE_URL,
            timeout=timeout,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
        public: bool = False,
        require_token: bool = False,
    ) -> dict[str, Any]:
        if require_token and not self._token:
            raise ValueError("this write requires your own VentiMath token with tags.write")
        url = f"{self.base_url}/{path}"
        if query:
            encoded = urlencode({key: value for key, value in query.items() if value is not None})
            if encoded:
                url += f"?{encoded}"
        headers = {"Accept": "application/json", "User-Agent": "ventimath-python/0.1.0"}
        if self._token and not public:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if body is not None:
            data = _json_bytes(body)
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8")
        except HTTPError as error:
            try:
                with error:
                    text = error.read().decode("utf-8", errors="replace")
            except (URLError, OSError, HTTPException) as read_error:
                raise VentiMathConnectionError(
                    f"request to {url} failed while reading HTTP {error.code}: {read_error}"
                ) from read_error
            try:
                payload = json.loads(text)
            except ValueError:
                payload = text
            raise VentiMathAPIError(error.code, payload, url) from error
        except (URLError, OSError, HTTPException) as error:
            raise VentiMathConnectionError(f"request to {url} failed: {error}") from error
        except UnicodeError as error:
            raise VentiMathProtocolError(f"invalid UTF-8 response from {url}") from error
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise VentiMathProtocolError(f"invalid JSON response from {url}") from error
        if not isinstance(payload, dict):
            raise VentiMathProtocolError(f"expected a JSON object from {url}")
        return payload

    def answer_keys(
        self, *, ids: Iterable[str] | str | None = None, exam: str | None = None
    ) -> dict[str, Any]:
        """Public {problems, missing, unavailable}; exactly one selector is required."""
        if (ids is None) == (exam is None):
            raise ValueError("provide exactly one of ids or exam")
        query = {"exam": exam}
        if ids is not None:
            values = ids.split(",") if isinstance(ids, str) else list(ids)
            if not 1 <= len(values) <= 100 or any(
                not isinstance(value, str) or not value.strip() or "," in value for value in values
            ):
                raise ValueError("ids must contain 1–100 nonempty canonical problem IDs")
            query = {"ids": ",".join(value.strip() for value in values)}
        elif not isinstance(exam, str) or not exam.strip():
            raise ValueError("exam must be a nonempty exam ID")
        return self._request("GET", "answerKeys", query=query, public=True)

    def download_answer_key(
        self,
        path: str | os.PathLike[str],
        *,
        ids: Iterable[str] | str | None = None,
        exam: str | None = None,
    ) -> dict[str, Any]:
        """Save JSON accepted by the classifier's existing local --answer-key option."""
        payload = self.answer_keys(ids=ids, exam=exam)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return payload

    def list_tag_sets(
        self,
        *,
        owner: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        public: bool = False,
    ) -> dict[str, Any]:
        """One {sets, nextCursor} page; omit vocabulary until get_tag_set is called."""
        return self._request(
            "GET",
            "problemTags/sets",
            query={"owner": owner, **_page_query(limit, cursor)},
            public=public,
        )

    def list_public_tag_sets(self, **kwargs: Any) -> dict[str, Any]:
        """Discover public sets anonymously, even if this client has a token."""
        return self.list_tag_sets(**{**kwargs, "public": True})

    def get_tag_set(self, set_id: str, *, public: bool = False) -> dict[str, Any]:
        """Return {set}, including vocabulary."""
        return self._request("GET", _set_path(set_id), public=public)

    def create_tag_set(
        self,
        slug: str,
        name: str,
        *,
        public: bool = False,
        vocabulary: Iterable[Mapping[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Create a private set on the token owner's profile unless public=True."""
        if type(public) is not bool:
            raise ValueError("public must be a boolean")
        body: dict[str, Any] = {
            "slug": slug,
            "name": name,
            "visibility": "public" if public else "private",
        }
        if vocabulary is not None:
            body["vocabulary"] = list(vocabulary)
        return self._request("POST", "problemTags/sets", body=body, require_token=True)

    def update_tag_set(
        self,
        set_id: str,
        *,
        name: str | None = None,
        public: bool | None = None,
        vocabulary: Iterable[Mapping[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Update only specified metadata; vocabulary replaces the existing list."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if public is not None:
            if type(public) is not bool:
                raise ValueError("public must be a boolean")
            body["visibility"] = "public" if public else "private"
        if vocabulary is not None:
            body["vocabulary"] = list(vocabulary)
        if not body:
            raise ValueError("provide name, public, or vocabulary to update")
        return self._request("PATCH", _set_path(set_id), body=body, require_token=True)

    def import_classifications(
        self,
        set_id: str,
        records: Iterable[Any] | Mapping[str, Any],
        *,
        vocabulary: Iterable[Mapping[str, str]] | None = None,
        dry_run: bool = False,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Import saved records with bounded requests and one aggregate report.

        Individual `result` and combined `llmResult` shapes pass through unchanged.
        Row-level rejection details pass through, with indices adjusted globally.
        Dry runs preview each batch against currently persisted state.
        """
        if not self._token:
            raise ValueError("imports require your own VentiMath token with tags.write")
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")
        path = f"{_set_path(set_id)}/import"
        items = [records] if isinstance(records, Mapping) else list(records)
        if not items:
            raise ValueError("no classifier records supplied")
        options: dict[str, Any] = {"dryRun": dry_run}
        if vocabulary is not None:
            options["vocabulary"] = list(vocabulary)
        if model is not None:
            options["model"] = model
        if reasoning_effort is not None:
            options["reasoningEffort"] = reasoning_effort
        batches: list[list[Any]] = []
        batch: list[Any] = []
        for index, record in enumerate(items):
            if len(_json_bytes({**options, "records": [record]})) > MAX_IMPORT_BYTES:
                raise ValueError(f"record {index} plus vocabulary exceeds the 2 MiB request limit")
            candidate = [*batch, record]
            if batch and (
                len(candidate) > MAX_IMPORT_RECORDS
                or len(_json_bytes({**options, "records": candidate})) > MAX_IMPORT_BYTES
            ):
                batches.append(batch)
                batch = []
            batch.append(record)
        batches.append(batch)
        report: dict[str, Any] = {
            "imported": 0,
            "unchanged": 0,
            "rejected": [],
            "dryRun": dry_run,
            "total": len(items),
            "processed": 0,
            "remaining": len(items),
        }
        for chunk in batches:
            try:
                result = self._request(
                    "POST", path, body={**options, "records": chunk}, require_token=True
                )
                if (
                    type(result.get("imported")) is not int
                    or type(result.get("unchanged")) is not int
                    or result["imported"] < 0
                    or result["unchanged"] < 0
                    or not isinstance(result.get("rejected"), list)
                    or result["imported"] + result["unchanged"] + len(result["rejected"])
                    != len(chunk)
                    or any(
                        not isinstance(rejected, dict)
                        or type(rejected.get("index")) is not int
                        or not 0 <= rejected["index"] < len(chunk)
                        for rejected in result["rejected"]
                    )
                ):
                    raise VentiMathProtocolError(
                        "invalid import response; inspect server state before retrying"
                    )
            except VentiMathError as error:
                raise VentiMathImportError(report, error) from error
            report["imported"] += result["imported"]
            report["unchanged"] += result["unchanged"]
            report["rejected"].extend(
                {**rejected, "index": report["processed"] + rejected["index"]}
                for rejected in result["rejected"]
            )
            report["processed"] += len(chunk)
            report["remaining"] = len(items) - report["processed"]
        return report

    def tagged_problems(
        self,
        set_id: str,
        *,
        skill: str | None = None,
        importance: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        public: bool = False,
    ) -> dict[str, Any]:
        """One {set, problems, nextCursor} page with each problem's classification."""
        if importance is not None and importance not in {"essential", "time-saving", "helpful"}:
            raise ValueError("importance must be essential, time-saving, or helpful")
        if importance and not skill:
            raise ValueError("importance requires a skill")
        return self._request(
            "GET",
            f"{_set_path(set_id)}/problems",
            query={"skill": skill, "importance": importance, **_page_query(limit, cursor)},
            public=public,
        )

    def iter_tagged_problems(self, set_id: str, **kwargs: Any) -> Iterable[dict[str, Any]]:
        """Yield problems across pages, including empty pages with a next cursor."""
        cursor = kwargs.pop("cursor", None)
        seen = {cursor} if cursor else set()
        while True:
            page = self.tagged_problems(set_id, cursor=cursor, **kwargs)
            if not isinstance(page.get("problems"), list):
                raise VentiMathProtocolError("tagged-problem response needs a problems list")
            yield from page["problems"]
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise VentiMathProtocolError(
                    "server returned an invalid or repeated pagination cursor"
                )
            seen.add(cursor)
