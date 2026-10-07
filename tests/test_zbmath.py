import copy
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zbmath import (CONVERTER_VERSION, MAX_JSON_BYTES, ZbMathError, deterministic_bibtex,
                    is_api_url, normalize_api_records, parse_response, record_url, search_url)


def document_fixture(identifier=12345):
    return {"id": identifier, "database": "Zbl", "identifier": "1234.56789", "year": "2024",
            "title": {"title": "An estimate for PDE solutions", "subtitle": None},
            "contributors": {"authors": [{"name": "Example, Alice"}, {"name": "Author, Bob"}]},
            "document_type": {"code": "j"},
            "links": [{"type": "doi", "identifier": "10.1234/example"},
                      {"type": "arxiv", "identifier": "2301.12345"}],
            "source": {"pages": "21-38", "series": [{"title": "Journal of Examples",
                "short_title": "J. Examples", "volume": "12", "issue": "3"}]},
            "references": [{"text": "zbMATH Open Web Interface contents unavailable due to conflicting licenses.",
                            "zbmath": {"document_id": 23456}}, {"zbmath": {"document_id": 23456}}]}


def response_fixture(documents=None, *, total=None, record=True):
    documents = [document_fixture()] if documents is None else documents
    return json.dumps({"result": documents[0] if record and documents else documents,
                       "status": {"execution_bool": True, "status_code": 200,
                                  "nr_total_results": len(documents) if total is None else total}}).encode()


class ZbMathTests(unittest.TestCase):
    def test_current_search_query_and_record_paths(self):
        value = search_url('doi:10.1234/example & ti:"elliptic equation"', 7, 2)
        self.assertEqual(urlsplit(value).path, "/v1/document/_search")
        self.assertEqual(parse_qs(urlsplit(value).query), {
            "search_string": ['doi:10.1234/example & ti:"elliptic equation"'],
            "results_per_page": ["7"], "page": ["2"]})
        self.assertEqual(record_url(12345), "https://api.zbmath.org/v1/document/12345")
        for query in ("", "\n", "x\x00", "x" * 2049):
            with self.subTest(query=query[:30]), self.assertRaises(ZbMathError):
                search_url(query)
        for value in (True, "1234.56789", "../12345", "https://example.com", 0, -1):
            with self.subTest(value=value), self.assertRaises(ZbMathError):
                record_url(value)

    def test_official_api_guard_rejects_aliases_credentials_and_other_endpoints(self):
        for url in (record_url(12345), search_url("elliptic")):
            self.assertTrue(is_api_url(url))
        for url in ("http://api.zbmath.org/v1/document/12345", "https://api.zbmath.org.evil.org/v1/document/12345",
                    "https://api.zbmath.org@evil.org/v1/document/12345", "https://api.zbmath.org:444/v1/document/12345",
                    "https://api.zbmath.org/v1/document/12345#other", "https://api.zbmath.org/v1/openapi.json"):
            with self.subTest(url=url):
                self.assertFalse(is_api_url(url))
                self.assertEqual(normalize_api_records(response_fixture(), url), [])

    def test_journal_metadata_and_redacted_reference_identity(self):
        result = parse_response(response_fixture(), record_url(12345))
        self.assertEqual(result["status"], "single")
        entry = result["candidates"][0]
        self.assertEqual(entry["author"], "Example, Alice and Author, Bob")
        self.assertEqual(entry["journal"], "Journal of Examples")
        self.assertEqual(entry["journal_abbreviation"], "J. Examples")
        self.assertEqual(entry["doi"], "10.1234/example")
        self.assertEqual(entry["publication_status"], "published")
        self.assertEqual(entry["references_ids"], ["23456"])
        self.assertNotIn("unavailable", json.dumps(entry))
        bib = deterministic_bibtex(entry, "Example2024")
        self.assertIn("@article{Example2024,", bib)
        self.assertIn("pages = {21--38}", bib)
        self.assertIn("title = {{An estimate for PDE solutions}}", bib)
        self.assertIn("not an original zbMATH BibTeX export", bib)
        self.assertIn(CONVERTER_VERSION, bib)
        self.assertEqual(bib, deterministic_bibtex(copy.deepcopy(entry), "Example2024"))

    def test_indexed_preprint_remains_preprint(self):
        doc = document_fixture()
        doc.update(database="arXiv", identifier="arXiv:2301.12345", document_type={"code": None})
        doc["source"] = {"series": [], "pages": None, "source": "Preprint, arXiv:2301.12345 (2024)"}
        doc["links"] = [{"type": "arxiv", "identifier": "2301.12345"}]
        entry = normalize_api_records(response_fixture([doc]))[0]
        self.assertEqual(entry["publication_status"], "preprint")
        self.assertEqual((entry["journal"], entry["doi"], entry["zbl"]), ("", "", ""))
        self.assertEqual(entry["eprint"], "2301.12345")
        bib = deterministic_bibtex(entry, "ExamplePreprint")
        self.assertIn("@misc", bib)
        self.assertNotIn("journal =", bib)
        self.assertIn("archiveprefix = {arXiv}", bib)
        self.assertIn("note = {Preprint, arXiv:2301.12345}", bib)
        self.assertNotIn("2301.12345v", bib)

    def test_plain_style_preprint_note_preserves_only_recorded_version_and_math(self):
        doc = document_fixture()
        doc.update(database="arXiv", identifier="arXiv:2301.12345v2", document_type={"code": None})
        doc["source"] = {"series": [], "pages": None}
        doc["title"]["title"] = r"$L^p$ bounds in \(\mathbb{R}\)"
        doc["links"] = [{"type": "arxiv", "identifier": "2301.12345v2"}]
        entry = normalize_api_records(response_fixture([doc]))[0]
        bib = deterministic_bibtex(entry, "PreprintMath")
        self.assertIn("  note = {Preprint, arXiv:2301.12345v2},\n", bib)
        self.assertIn("  eprint = {2301.12345v2},\n", bib)
        self.assertIn("  title = {{" + doc["title"]["title"] + "}},\n", bib)
        article = deterministic_bibtex(normalize_api_records(response_fixture())[0], "Published")
        self.assertNotIn("note =", article)

    def test_empty_multiple_and_malformed_are_distinct(self):
        empty = parse_response(response_fixture([], record=False), search_url("no match"))
        self.assertEqual(empty["status"], "no_match")
        self.assertIn("No records matched this query", empty["warnings"][0])
        multiple = parse_response(response_fixture([document_fixture()], total=7, record=False), search_url("equation"))
        self.assertEqual(multiple["status"], "multiple")
        wrong = parse_response(response_fixture(), record_url(99999))
        self.assertEqual(wrong["status"], "schema_error")
        for raw in (b"<html>challenge</html>", b"[]", b'{"result":[]}', b"[" * 5000,
                    b" " * (MAX_JSON_BYTES + 1), response_fixture([], total=12, record=False)):
            with self.subTest(length=len(raw)):
                self.assertEqual(parse_response(raw)["status"], "schema_error")
        failed = json.loads(response_fixture())
        failed["status"]["execution_bool"] = False
        self.assertEqual(parse_response(json.dumps(failed).encode())["status"], "api_error")

    def test_exact_identifier_lookup_does_not_accept_an_unrelated_record(self):
        queries = ("doi:10.9999/unrelated", "an:9999.99999", "an:99999", "arxiv:2401.54321")
        for query in queries:
            with self.subTest(query=query):
                result = parse_response(response_fixture(record=False), search_url(query))
                self.assertEqual(result["status"], "identity_mismatch")
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["rejected_document_ids"], ["12345"])
                self.assertEqual(normalize_api_records(response_fixture(record=False), search_url(query)), [])
        structured = "https://api.zbmath.org/v1/document/?DOI=10.9999%2Funrelated"
        self.assertEqual(parse_response(response_fixture(record=False), structured)["status"], "identity_mismatch")

    def test_exact_doi_lookup_normalizes_case_url_and_escaped_punctuation(self):
        doc = document_fixture()
        doc["links"][0]["identifier"] = "10.1234/Some_Result"
        for query in ("doi:10.1234/some_result", r'doi:"10.1234/SOME\_RESULT"',
                      "doi:https://doi.org/10.1234/Some%5FResult"):
            with self.subTest(query=query):
                result = parse_response(response_fixture([doc], record=False), search_url(query))
                self.assertEqual(result["status"], "single")
                self.assertEqual(result["candidates"][0]["doi"], "10.1234/Some_Result")
        structured = "https://api.zbmath.org/v1/document/?DOI=10.1234%2Fsome_result"
        self.assertEqual(parse_response(response_fixture([doc], record=False), structured)["status"], "single")

    def test_versioned_arxiv_lookup_matches_catalog_base_without_inventing_version(self):
        result = parse_response(response_fixture(record=False), search_url("arxiv:2301.12345v4"))
        self.assertEqual(result["status"], "single")
        self.assertEqual(result["candidates"][0]["arxiv"], "2301.12345")
        self.assertEqual(parse_response(response_fixture(record=False), search_url("an:1234.56789"))["status"], "single")
        self.assertEqual(parse_response(response_fixture(record=False), search_url("an:12345"))["status"], "single")

    def test_multiple_matching_versions_stay_ambiguous_and_unrelated_hits_are_filtered(self):
        same = document_fixture(12346)
        unrelated = document_fixture(12347)
        unrelated["links"][0]["identifier"] = "10.9999/other"
        raw = response_fixture([document_fixture(), same, unrelated], record=False)
        result = parse_response(raw, search_url("doi:10.1234/example"))
        self.assertEqual(result["status"], "multiple")
        self.assertEqual(result["total_results"], 3)
        self.assertEqual([item["document_id"] for item in result["candidates"]], ["12345", "12346"])
        self.assertEqual(result["rejected_document_ids"], ["12347"])
        broad = parse_response(raw, search_url("doi:10.1234/example | ti:equations"))
        self.assertEqual(len(broad["candidates"]), 3)
        self.assertNotIn("rejected_document_ids", broad)

    def test_redacted_required_metadata_cannot_generate_fake_bib(self):
        for field in ("title", "author", "year"):
            doc = document_fixture()
            missing = "zbMATH Open Web Interface contents unavailable due to conflicting licenses."
            if field == "title":
                doc["title"]["title"] = missing
            elif field == "author":
                doc["contributors"]["authors"][0]["name"] = missing
            else:
                doc["year"] = missing
            entry = normalize_api_records(response_fixture([doc]))[0]
            self.assertIn(field, entry["missing_fields"])
            with self.assertRaises(ZbMathError):
                deterministic_bibtex(entry, "key")

    def test_remote_dangerous_tex_requires_fallback_and_key_cannot_inject_bib(self):
        doc = document_fixture()
        for unsafe in (r"\input{/secret}", r"$\input{/secret}$", r"\write18{shell}",
                       r"$\csname input\endcsname{file}$", r"\newcommand{\x}{malicious}",
                       r"$^^5cinput{file}$", r"$\unknownLocalMacro{x}$"):
            doc["title"]["title"] = unsafe
            entry = normalize_api_records(response_fixture([doc]))[0]
            with self.subTest(title=unsafe), self.assertRaises(ZbMathError):
                deterministic_bibtex(entry, "safe-key")
        entry = normalize_api_records(response_fixture())[0]
        for key in ("x,\n@article{evil", "bad key", "../file", "", "x" * 129):
            with self.subTest(key=key), self.assertRaises(ZbMathError):
                deterministic_bibtex(entry, key)

    def test_mathematical_titles_preserve_tex_in_bibtex_output(self):
        for title in (r"$L^p$ estimates for weak solutions",
                      r"Elliptic regularity in \(\mathbb{R}^n\)",
                      r"Sharp $C^{0,\alpha}$ bounds and $\frac{2}{p+1}$ scaling",
                      r"On $W^{1,p}_0(\Omega)$ and $\lVert u\rVert_{L^p}$",
                      r"{Hölder} bounds for $\lambda\to\infty$"):
            doc = document_fixture()
            doc["title"]["title"] = title
            entry = normalize_api_records(response_fixture([doc]))[0]
            bib = deterministic_bibtex(entry, "MathTitle")
            with self.subTest(title=title):
                self.assertIn("  title = {{" + title + "}},\n", bib)
                self.assertNotIn(r"\textbackslash", bib)
                self.assertNotIn(r"\$L", bib)

    def test_unicode_and_standard_tex_accents_preserve_author_identity(self):
        for name in ("Dávila, Juan", "Müller, Anna", r"D\'avila, Juan", r"M{\"u}ller, Anna"):
            doc = document_fixture()
            doc["contributors"]["authors"] = [{"name": name}]
            doc["title"]["title"] = "Hölder estimates & comparisons"
            bib = deterministic_bibtex(normalize_api_records(response_fixture([doc]))[0], "Accent")
            with self.subTest(name=name):
                self.assertIn("  author = {" + name + "},\n", bib)
                self.assertIn(r"  title = {{Hölder estimates \& comparisons}}," + "\n", bib)
                self.assertNotIn(r"\textbackslash", bib)

    def test_unbalanced_or_excessively_nested_tex_requires_explicit_fallback(self):
        for title in (r"$L^p", r"\(\mathbb{R}", r"$x\)", r"$x_{a$", r"{group", r"group}",
                      r"$$display math$$", "{" * 33 + "title" + "}" * 33):
            doc = document_fixture()
            doc["title"]["title"] = title
            with self.subTest(title=title), self.assertRaises(ZbMathError):
                deterministic_bibtex(normalize_api_records(response_fixture([doc]))[0], "Unbalanced")

    def test_ambiguous_or_unsupported_publication_needs_other_export(self):
        for change in ("doi", "series", "book"):
            doc = document_fixture()
            if change == "doi":
                doc["links"].append({"type": "doi", "identifier": "10.9999/ambiguous"})
            elif change == "series":
                doc["source"]["series"].append({"title": "Another Journal"})
            else:
                doc["document_type"]["code"] = "b"
            with self.subTest(change=change), self.assertRaises(ZbMathError):
                deterministic_bibtex(normalize_api_records(response_fixture([doc]))[0], "key")


if __name__ == "__main__":
    unittest.main()
