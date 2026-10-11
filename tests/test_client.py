import io
import json
import os
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPHandler, build_opener
from urllib.response import addinfourl

from ventimath import (
    DEFAULT_BASE_URL,
    VentiMathAPIError,
    VentiMathClient,
    VentiMathImportError,
    VentiMathProtocolError,
    load_classification_files,
    load_vocabulary_csv,
)
from ventimath.client import _NoRedirectHandler


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode("utf-8"))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ventimath-sdk-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transport = patch("ventimath.client.urlopen")
        self.urlopen = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.client = VentiMathClient("venti_pat_own", base_url="https://api.example.test/")

    def reply(self, payload):
        self.urlopen.return_value = Response(payload)

    def request(self, index=-1):
        return self.urlopen.call_args_list[index].args[0]

    def body(self, index=-1):
        return json.loads(self.request(index).data)

    def file(self, name, payload):
        path = self.root / name
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )
        return path

    def test_anonymous_constructor_and_explicit_env_convenience(self):
        with patch.dict(
            os.environ,
            {"VENTI_API_KEY": "venti_pat_env", "VENTI_API_URL": "https://env.example.test"},
        ):
            self.reply({"sets": [], "nextCursor": None})
            VentiMathClient().list_tag_sets()
            self.assertTrue(self.request().full_url.startswith(DEFAULT_BASE_URL))
            self.assertIsNone(self.request().get_header("Authorization"))
            self.reply({"sets": [], "nextCursor": None})
            VentiMathClient.from_env().list_tag_sets()
            self.assertEqual(self.request().get_header("Authorization"), "Bearer venti_pat_env")
            self.assertTrue(self.request().full_url.startswith("https://env.example.test/"))

    def test_public_key_download_omits_auth_and_preserves_classifier_shape(self):
        payload = {
            "problems": {"p1": {"correctChoice": "B", "answerText": "2"}},
            "missing": ["p2"],
            "unavailable": [],
        }
        self.reply(payload)
        destination = self.root / "keys" / "exam.json"
        result = self.client.download_answer_key(destination, ids=["p1", "p2"])
        self.assertEqual(self.request().full_url, "https://api.example.test/answerKeys?ids=p1%2Cp2")
        self.assertEqual(self.request().method, "GET")
        self.assertIsNone(self.request().get_header("Authorization"))
        self.assertEqual(json.loads(destination.read_text()), payload)
        self.assertEqual(result, payload)
        self.reply(payload)
        self.client.answer_keys(exam="exam id")
        self.assertEqual(
            self.request().full_url, "https://api.example.test/answerKeys?exam=exam+id"
        )

    def test_answer_selectors_are_validated_before_transport(self):
        for selectors in [
            {},
            {"ids": [], "exam": "e"},
            {"ids": []},
            {"ids": ["p"] * 101},
            {"ids": "p,,q"},
            {"exam": ""},
        ]:
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                self.client.answer_keys(**selectors)
        self.urlopen.assert_not_called()

    def test_public_discovery_omits_existing_token_and_private_reads_use_it(self):
        self.reply({"sets": [], "nextCursor": "later"})
        self.client.list_public_tag_sets(owner="123", limit=12, cursor="a+b")
        self.assertEqual(
            self.request().full_url,
            "https://api.example.test/problemTags/sets?owner=123&limit=12&cursor=a%2Bb",
        )
        self.assertIsNone(self.request().get_header("Authorization"))
        self.reply({"set": {"id": "123_skills", "vocabulary": []}})
        self.client.get_tag_set("123_skills")
        self.assertEqual(self.request().get_header("Authorization"), "Bearer venti_pat_own")
        self.reply({"set": {"id": "123_skills"}})
        self.client.get_tag_set("123_skills", public=True)
        self.assertIsNone(self.request().get_header("Authorization"))

    def test_redirects_do_not_forward_tokens_or_rewrite_requests(self):
        requests = []

        class RedirectResponse(HTTPHandler):
            def http_open(self, request):
                requests.append(request)
                response = addinfourl(
                    io.BytesIO(b"redirect refused"),
                    {"Location": "http://other.example.test/stolen"},
                    request.full_url,
                    status,
                )
                response.msg = "Redirect"
                return response

        direct = VentiMathClient("venti_pat_own", base_url="http://api.example.test")
        opener = build_opener(_NoRedirectHandler(), RedirectResponse())
        for status in (301, 302, 303, 307, 308):
            for method in ("GET", "POST"):
                requests.clear()
                with self.subTest(status=status, method=method):
                    with patch("ventimath.client.urlopen", opener.open):
                        with self.assertRaises(VentiMathAPIError) as raised:
                            if method == "GET":
                                direct.get_tag_set("123_amc")
                            else:
                                direct.create_tag_set("amc", "My tags")
                    self.assertEqual(raised.exception.status, status)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].get_method(), method)
                    self.assertEqual(urlsplit(requests[0].full_url).hostname, "api.example.test")

    def test_create_is_public_by_default_and_update_changes_only_requested_metadata(self):
        vocabulary = [{"skill": "Casework", "category": "Counting", "description": "Cases"}]
        self.reply({"set": {"id": "123_amc"}})
        self.client.create_tag_set("amc", "My AMC tags", vocabulary=vocabulary)
        self.assertEqual(self.request().method, "POST")
        self.assertEqual(
            self.body(),
            {
                "slug": "amc",
                "name": "My AMC tags",
                "visibility": "public",
                "vocabulary": vocabulary,
            },
        )
        self.assertEqual(self.request().get_header("Authorization"), "Bearer venti_pat_own")
        self.assertEqual(self.request().get_header("Content-type"), "application/json")
        self.reply({"set": {"id": "123_amc"}})
        self.client.update_tag_set("123_amc", public=True)
        self.assertEqual(self.request().method, "PATCH")
        self.assertEqual(self.body(), {"visibility": "public"})
        self.reply({"set": {"id": "123_amc"}})
        self.client.update_tag_set("123_amc", name="Renamed")
        self.assertEqual(self.body(), {"name": "Renamed"})

    def test_explicit_private_set_is_private_from_the_first_and_only_request(self):
        payload = {"set": {"id": "123_amc", "visibility": "private"}}
        self.reply(payload)
        result = self.client.create_tag_set("amc", "My AMC tags", public=False)
        self.assertEqual(result, payload)
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertEqual(self.request().method, "POST")
        self.assertEqual(self.request().full_url, "https://api.example.test/problemTags/sets")
        self.assertEqual(
            self.body(),
            {"slug": "amc", "name": "My AMC tags", "visibility": "private"},
        )

    def test_writes_require_own_token_and_boolean_publication_choice(self):
        anonymous = VentiMathClient()
        with self.assertRaisesRegex(ValueError, "tags.write"):
            anonymous.create_tag_set("amc", "My tags")
        with self.assertRaisesRegex(ValueError, "tags.write"):
            anonymous.import_classifications("123_amc", [{"problemId": "p1", "result": {}}])
        with self.assertRaises(ValueError):
            self.client.update_tag_set("123_amc", public="false")
        self.urlopen.assert_not_called()

    def test_pagination_follows_empty_pages_and_preserves_problem_classification(self):
        problem = {
            "id": "p2",
            "details": {"statement": "Example"},
            "classification": {"skills": []},
        }
        self.urlopen.side_effect = [
            Response({"problems": [], "nextCursor": "p1"}),
            Response({"problems": [problem], "nextCursor": None}),
        ]
        self.assertEqual(
            list(
                self.client.iter_tagged_problems(
                    "123_amc", skill="Cases & counting", importance="essential", public=True
                )
            ),
            [problem],
        )
        self.assertEqual(
            parse_qs(urlsplit(self.request().full_url).query),
            {"skill": ["Cases & counting"], "importance": ["essential"], "cursor": ["p1"]},
        )
        self.assertTrue(
            all(
                call.args[0].get_header("Authorization") is None
                for call in self.urlopen.call_args_list
            )
        )

    def test_iterator_refuses_repeated_cursor_instead_of_looping(self):
        self.urlopen.side_effect = [
            Response({"problems": [], "nextCursor": "same"}),
            Response({"problems": [], "nextCursor": "same"}),
        ]
        with self.assertRaises(VentiMathProtocolError):
            list(self.client.iter_tagged_problems("123_amc"))
        self.assertEqual(self.urlopen.call_count, 2)

    def test_csv_loader_and_existing_classification_files(self):
        csv_path = self.file(
            "skills.csv",
            "\ufeffdescription,category,skill\r\n"
            '"Count, then\ncheck ""again""",Counting,Casework\r\n',
        )
        self.assertEqual(
            load_vocabulary_csv(csv_path),
            [
                {
                    "description": 'Count, then\ncheck "again"',
                    "category": "Counting",
                    "skill": "Casework",
                }
            ],
        )
        single = {"problemId": "p1", "result": {"skills": []}, "model": "original"}
        combined = [{"problemId": "p2", "llmResult": {"skills": []}}]
        self.assertEqual(
            load_classification_files(
                [self.file("one.json", single), self.file("combined.json", combined)]
            ),
            [single, *combined],
        )
        with self.assertRaises(ValueError):
            load_vocabulary_csv(self.file("bad.csv", 'skill,category,description\n"unclosed'))
        with self.assertRaises(ValueError):
            load_vocabulary_csv(self.file("duplicate.csv", "skill,skill,description\na,b,c"))

    def test_import_chunks_records_preserves_shapes_and_adjusts_rejection_indices(self):
        items = [{"problemId": f"p{index}", "llmResult": {"skills": []}} for index in range(200)]
        single = {
            "problemId": "p200",
            "result": None,
            "model": "original",
            "reasoningEffort": "high",
        }
        items.append(single)
        vocabulary = [{"skill": "Casework", "category": "Counting", "description": "Cases"}]
        self.urlopen.side_effect = [
            Response({"imported": 199, "unchanged": 1, "rejected": [], "dryRun": True}),
            Response(
                {
                    "imported": 0,
                    "unchanged": 0,
                    "rejected": [{"index": 0, "problemId": "p200", "error": "invalid_result"}],
                    "dryRun": True,
                }
            ),
        ]
        report = self.client.import_classifications(
            "123_amc",
            items,
            vocabulary=vocabulary,
            dry_run=True,
            model="default",
            reasoning_effort="medium",
        )
        self.assertEqual([len(self.body(index)["records"]) for index in range(2)], [200, 1])
        self.assertEqual(self.body()["records"], [single])
        self.assertEqual(self.body()["vocabulary"], vocabulary)
        self.assertEqual(self.body()["model"], "default")
        self.assertEqual(self.body()["reasoningEffort"], "medium")
        self.assertTrue(self.body()["dryRun"])
        self.assertEqual(
            report,
            {
                "imported": 199,
                "unchanged": 1,
                "rejected": [{"index": 200, "problemId": "p200", "error": "invalid_result"}],
                "dryRun": True,
                "total": 201,
                "processed": 201,
                "remaining": 0,
            },
        )

    def test_utf8_payload_size_also_splits_import_requests(self):
        items = [
            {"problemId": f"p{index}", "result": {"summary": "漢" * 30000}} for index in range(30)
        ]
        self.urlopen.side_effect = lambda request, **kwargs: Response(
            {"imported": len(json.loads(request.data)["records"]), "unchanged": 0, "rejected": []}
        )
        report = self.client.import_classifications("123_amc", items)
        self.assertGreater(self.urlopen.call_count, 1)
        self.assertTrue(
            all(len(call.args[0].data) <= 2 * 1024 * 1024 for call in self.urlopen.call_args_list)
        )
        self.assertEqual(report["imported"], 30)

    def test_oversized_or_invalid_json_record_fails_before_any_import(self):
        for bad in [
            {"problemId": "big", "rawReply": "x" * (2 * 1024 * 1024)},
            {"problemId": "bad", "value": float("nan")},
        ]:
            with self.subTest(bad=bad["problemId"]), self.assertRaises(ValueError):
                self.client.import_classifications(
                    "123_amc", [{"problemId": "good", "result": {}}, bad]
                )
        self.urlopen.assert_not_called()

    def test_api_errors_retain_http_status_and_original_payload(self):
        payload = {"error": "scope_required", "scope": "tags.read"}
        self.urlopen.side_effect = HTTPError(
            "https://example.test", 403, "Forbidden", {}, io.BytesIO(json.dumps(payload).encode())
        )
        with self.assertRaises(VentiMathAPIError) as raised:
            self.client.get_tag_set("123_amc")
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(raised.exception.payload, payload)

    def test_failed_later_import_preserves_completed_report_and_retry_guidance(self):
        items = [{"problemId": f"p{index}", "result": {}} for index in range(201)]
        self.urlopen.side_effect = [
            Response({"imported": 200, "unchanged": 0, "rejected": []}),
            HTTPError(
                "https://example.test",
                503,
                "Unavailable",
                {},
                io.BytesIO(b'{"error":"unavailable"}'),
            ),
        ]
        with self.assertRaises(VentiMathImportError) as raised:
            self.client.import_classifications("123_amc", items)
        error = raised.exception
        self.assertEqual(error.status, 503)
        self.assertEqual(error.payload, {"error": "unavailable"})
        self.assertEqual(error.report["imported"], 200)
        self.assertEqual(error.report["processed"], 200)
        self.assertEqual(error.report["remaining"], 1)
        self.assertIn("last request may have committed", str(error))
        self.assertIn("Retry the same records safely", str(error))

    def test_network_failure_also_preserves_import_report(self):
        self.urlopen.side_effect = URLError("offline")
        with self.assertRaises(VentiMathImportError) as raised:
            self.client.import_classifications("123_amc", {"problemId": "p1", "result": {}})
        self.assertEqual(raised.exception.report["processed"], 0)
        self.assertEqual(raised.exception.report["remaining"], 1)

    def test_interrupted_response_body_preserves_completed_import_report(self):
        class InterruptedResponse(Response):
            def read(self, *args):
                raise IncompleteRead(b'{"imported":', 100)

        items = [{"problemId": f"p{index}", "result": {}} for index in range(201)]
        for failed in (
            InterruptedResponse({}),
            HTTPError("https://example.test", 503, "Unavailable", {}, InterruptedResponse({})),
        ):
            with self.subTest(response=type(failed).__name__):
                self.urlopen.side_effect = [
                    Response({"imported": 200, "unchanged": 0, "rejected": []}),
                    failed,
                ]
                with self.assertRaises(VentiMathImportError) as raised:
                    self.client.import_classifications("123_amc", items)
                self.assertEqual(raised.exception.report["processed"], 200)
                self.assertEqual(raised.exception.report["remaining"], 1)
                self.assertIn("last request may have committed", str(raised.exception))

    def test_filters_and_endpoint_config_reject_invalid_input(self):
        for kwargs in [
            {"limit": 0},
            {"limit": 101},
            {"limit": True},
            {"importance": "essential"},
            {"importance": "maybe", "skill": "x"},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.client.tagged_problems("123_amc", **kwargs)
        with self.assertRaises(ValueError):
            VentiMathClient(base_url="https://token@example.test")
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
