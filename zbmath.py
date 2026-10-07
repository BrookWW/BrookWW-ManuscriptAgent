"""Bounded, pure normalization of the official zbMATH Open v1 API.

This module does not fetch URLs or execute source text. Bibliography output is
derived from official JSON metadata, never presented as an original BibTeX
download. Reviews/abstracts are deliberately not copied into metadata output.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

API_ROOT = "https://api.zbmath.org/v1"
CONVERTER_VERSION = "zbmath-api-bibtex-v1"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_RESULTS = 100


class ZbMathError(ValueError):
    pass


def search_url(query: str, limit: int = 10, page: int = 0) -> str:
    if (not isinstance(query, str) or not query.strip() or len(query) > 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in query)):
        raise ZbMathError("Use a nonempty zbMATH query of at most 2048 characters.")
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS or type(page) is not int or not 0 <= page <= 100:
        raise ZbMathError("Search limits must be 1–100 and pages 0–100.")
    return API_ROOT + "/document/_search?" + urlencode({"search_string": query.strip(),
                                                         "results_per_page": limit, "page": page})


def record_url(document_id: str | int) -> str:
    identifier = str(document_id)
    if type(document_id) is bool or not re.fullmatch(r"[1-9][0-9]{0,11}", identifier):
        raise ZbMathError("A zbMATH document ID is a positive integer, not a Zbl number or URL.")
    return API_ROOT + "/document/" + identifier


def is_api_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname == "api.zbmath.org"
                and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                and not parsed.fragment and (re.fullmatch(r"/v1/document/[1-9][0-9]{0,11}", parsed.path) is not None
                                            or parsed.path in {"/v1/document/_search", "/v1/document/"}))
    except (TypeError, ValueError):
        return False


def _text(value) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    if (len(value) > 16384 or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "unavailable due to conflicting licenses" in value.casefold()):
        return ""
    return value


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _identifier_value(field: str, value: str) -> str:
    value = unquote(value).strip().casefold()
    value = re.sub(r"\\([_%&#{}])", r"\1", value)
    if field == "doi":
        return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value)
    if field == "zbl":
        return re.sub(r"^zbl\s*:?\s*", "", value)
    if field == "arxiv":
        value = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv\s*:\s*)", "", value)
        return re.sub(r"v[0-9]+$", "", value.removesuffix(".pdf"))
    return value


def _requested_identity(source_url: str) -> tuple[str, str] | None:
    """Recognize only exact identifier lookups, leaving broad queries intact."""
    parsed = urlsplit(source_url)
    parameters = parse_qs(parsed.query)
    if parsed.path == "/v1/document/" and len(parameters.get("DOI", [])) == 1:
        field, value = "doi", parameters["DOI"][0]
    elif parsed.path == "/v1/document/_search" and len(parameters.get("search_string", [])) == 1:
        query = parameters["search_string"][0].strip()
        match = re.fullmatch(r"(doi|an|arxiv|zbl)\s*:\s*(.+)", query, re.I)
        if not match:
            return None
        field, value = match.group(1).lower(), match.group(2).strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        elif any(char.isspace() or char in "*&|!" for char in value):
            return None
    else:
        return None
    if field == "an":
        if re.fullmatch(r"[1-9][0-9]{0,11}", value):
            return "document_id", value
        field = "zbl"
    normalized = _identifier_value(field, value)
    patterns = {"doi": r"10\.[0-9]{4,9}/\S+", "zbl": r"[0-9]{4}\.[0-9]{5}",
                "arxiv": r"(?:[0-9]{4}\.[0-9]{4,5}|[a-z.-]+/[0-9]{7})"}
    if re.fullmatch(patterns[field], normalized, re.I):
        return field, normalized
    return None


def _normalize(document: dict) -> dict:
    identifier = str(document.get("id", ""))
    url = record_url(identifier)
    title = _text(_dict(document.get("title")).get("title"))
    subtitle = _text(_dict(document.get("title")).get("subtitle"))
    if subtitle:
        title = title + ": " + subtitle if title else subtitle
    authors = [_text(_dict(author).get("name"))
               for author in _list(_dict(document.get("contributors")).get("authors"))]
    # A partially redacted author list must not silently become a shorter list.
    if not all(authors):
        authors = []
    year = _text(document.get("year"))
    if not re.fullmatch(r"[12][0-9]{3}", year):
        year = ""
    database = _text(document.get("database"))
    document_type = _text(_dict(document.get("document_type")).get("code"))
    source = _dict(document.get("source"))
    series = [_dict(row) for row in _list(source.get("series"))]
    journal = series[0] if len(series) == 1 and document_type in ("j", "") else {}
    links = [_dict(link) for link in _list(document.get("links"))]
    dois = sorted({_text(link.get("identifier")) for link in links
                   if _text(link.get("type")).lower() == "doi" and _text(link.get("identifier"))})
    arxiv_ids = sorted({_text(link.get("identifier")) for link in links
                        if _text(link.get("type")).lower() == "arxiv" and _text(link.get("identifier"))})
    doi = dois[0] if len(dois) == 1 and re.fullmatch(r"10\.[0-9]{4,9}/\S+", dois[0], re.I) else ""
    arxiv = arxiv_ids[0] if len(arxiv_ids) == 1 else ""
    if arxiv and not re.fullmatch(r"(?:[0-9]{4}\.[0-9]{4,5}|[A-Za-z.-]+/[0-9]{7})(?:v[0-9]+)?", arxiv):
        arxiv = ""
    zbl = _text(document.get("identifier")) if database.casefold() == "zbl" else ""
    if zbl and not re.fullmatch(r"[0-9]{4}\.[0-9]{5}", zbl):
        zbl = ""
    is_preprint = database.casefold() == "arxiv" or document_type == "p"
    if is_preprint:
        journal = {}
    journal_name = _text(journal.get("title")) or _text(journal.get("short_title"))
    references = []
    seen_references = set()
    for reference in _list(document.get("references"))[:10000]:
        value = _dict(_dict(reference).get("zbmath")).get("document_id")
        if type(value) is int and 0 < value < 10**12 and value not in seen_references:
            references.append(str(value))
            seen_references.add(value)
    candidate = {
        "document_id": identifier, "source_url": url, "zbmath_url": "https://zbmath.org/" + identifier,
        "title": title, "authors": authors, "author": " and ".join(authors), "year": year,
        "journal": journal_name, "journal_abbreviation": _text(journal.get("short_title")),
        "volume": _text(journal.get("volume")), "number": _text(journal.get("issue")),
        "pages": _text(source.get("pages")), "doi": doi, "zbl": zbl,
        "arxiv": arxiv, "eprint": arxiv, "archiveprefix": "arXiv" if arxiv else "",
        "database": database, "document_type": document_type,
        "entry_type": "article" if journal_name else "misc",
        "publication_status": "preprint" if is_preprint else "published" if journal_name else "unclassified",
        "references_ids": references,
    }
    candidate["missing_fields"] = [name for name in ("title", "author", "year") if not candidate[name]]
    candidate["warnings"] = []
    if len(dois) > 1:
        candidate["warnings"].append("Multiple DOI identifiers require manual disambiguation.")
    if len(series) > 1:
        candidate["warnings"].append("Multiple publication series require manual disambiguation.")
    if document_type == "j" and not journal_name:
        candidate["missing_fields"].append("journal")
    if document_type in ("a", "b"):
        candidate["warnings"].append("Book or collection metadata requires a source-specific export; this converter does not infer book fields.")
    return candidate


def parse_response(raw: bytes, source_url: str = "") -> dict:
    """Classify a bounded API response without interpreting missing hits as fact.

    ``no_match`` means only that this particular query returned no matching
    records. It never asserts that a work has no publication or no zbMATH entry.
    """
    output = {"schema_version": 1, "provider": "zbmath_api", "status": "schema_error",
              "total_results": None, "candidates": [], "warnings": []}
    if source_url and not is_api_url(source_url):
        output["warnings"].append("The source URL is not an official zbMATH document API endpoint.")
        return output
    if not isinstance(raw, bytes) or len(raw) > MAX_JSON_BYTES:
        output["warnings"].append("The API response exceeds the JSON parsing limit or is not bytes.")
        return output
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError):
        output["warnings"].append("The API response is not readable bounded JSON.")
        return output
    if not isinstance(value, dict) or not isinstance(value.get("status"), dict) or "result" not in value:
        output["warnings"].append("The API response has no recognized result/status envelope.")
        return output
    status = value["status"]
    if status.get("execution_bool") is not True or status.get("status_code") != 200:
        output["status"] = "api_error"
        output["warnings"].append("zbMATH reported an unsuccessful API request; this is not an empty search.")
        return output
    result = value["result"]
    rows = [result] if isinstance(result, dict) else result
    total = status.get("nr_total_results")
    if not isinstance(rows, list) or len(rows) > MAX_RESULTS or type(total) is not int or total < 0 or total < len(rows):
        output["warnings"].append("The API response has an invalid or oversized result collection.")
        return output
    output["total_results"] = total
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ZbMathError("A result record is not an object.")
            candidate = _normalize(row)
        except ZbMathError:
            output["warnings"].append("A malformed result record was omitted; do not treat this response as complete.")
            continue
        output["candidates"].append(candidate)
    if not rows and total == 0:
        output["status"] = "no_match"
        output["warnings"].append("No records matched this query. Try identifiers, title variants, or another authoritative source.")
    elif len(output["candidates"]) != len(rows) or not rows:
        output["status"] = "schema_error"
    else:
        output["status"] = "single" if total == 1 else "multiple"
    if source_url and re.fullmatch(r"/v1/document/[1-9][0-9]{0,11}", urlsplit(source_url).path):
        expected = urlsplit(source_url).path.rsplit("/", 1)[1]
        if output["status"] != "no_match" and (len(output["candidates"]) != 1
                or output["candidates"][0]["document_id"] != expected):
            output["status"] = "schema_error"
            output["warnings"].append("The returned document does not match the requested document ID.")
    requested = _requested_identity(source_url) if source_url else None
    if requested and output["status"] in {"single", "multiple"}:
        field, expected = requested
        matched, rejected = [], []
        for candidate in output["candidates"]:
            actual = _identifier_value(field, candidate.get(field, ""))
            (matched if actual == expected else rejected).append(candidate)
        if rejected:
            output["candidates"] = matched
            output["rejected_document_ids"] = [candidate["document_id"] for candidate in rejected]
            output["warnings"].append("identity_mismatch: returned records without the exact requested identifier were excluded.")
            if not matched:
                output["status"] = "identity_mismatch"
    return output


def normalize_api_records(raw: bytes, source_url: str = "") -> list[dict]:
    result = parse_response(raw, source_url)
    return result["candidates"] if result["status"] in ("single", "multiple") else []


def _tex_literal(value: str) -> str:
    escapes = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "%": r"\%", "&": r"\&",
               "#": r"\#", "_": r"\_", "$": r"\$", "^": r"\textasciicircum{}", "~": r"\textasciitilde{}"}
    return "".join(escapes.get(char, char) for char in value)


_MATH_COMMANDS = frozenset("""
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa varkappa
lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega
Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
mathbb mathcal mathfrak mathrm mathit mathbf mathsf mathtt boldsymbol
text textrm textit textbf textsf texttt operatorname
frac dfrac tfrac binom dbinom tbinom sqrt
overline underline widehat widetilde hat tilde bar vec dot ddot breve check acute grave
sum prod coprod int iint iiint oint lim limsup liminf sup inf max min
sin cos tan cot sec csc sinh cosh tanh log ln exp det dim ker hom gcd Pr
partial nabla infty ell hbar Re Im emptyset varnothing
le leq leqslant ge geq geqslant ne neq equiv approx sim simeq cong asymp propto
ll gg prec preceq succ succeq in notin ni subset subseteq supset supseteq
times cdot div circ bullet star ast cap cup bigcap bigcup wedge vee oplus otimes
setminus smallsetminus pm mp parallel perp mid nmid
to mapsto longmapsto rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow
longrightarrow longleftarrow longleftrightarrow Longrightarrow Longleftarrow Longleftrightarrow
hookrightarrow hookleftarrow uparrow downarrow
forall exists nexists neg land lor imath jmath
ldots cdots vdots ddots dots quad qquad
left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr
langle rangle lvert rvert lVert rVert vert Vert lceil rceil lfloor rfloor
colon bmod pmod mod substack limits nolimits displaystyle textstyle scriptstyle scriptscriptstyle
""".split())
_TEXT_COMMANDS = frozenset("""
TeX LaTeX textit textbf textsc textrm textsf texttt emph textnormal
ae AE oe OE o O aa AA ss l L i j copyright textregistered textendash textemdash
""".split())
_ACCENTS = frozenset("'\"^~=.uvHckrbdt" + chr(96))


def _tex_bibliographic_text(value: str, *, allow_math: bool) -> str:
    """Keep a bounded conventional TeX subset; never execute or guess macros.

    Math delimiters and group balance are checked without a TeX engine. Unknown
    commands cause an explicit conversion fallback instead of corrupting title
    mathematics or copying executable TeX from a remote metadata record.
    """
    if len(value) > 16384 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ZbMathError("Bibliographic text is oversized or contains control characters.")
    if "^^" in value:
        raise ZbMathError("TeX character-code notation is unsupported in bibliographic metadata.")
    result, groups, math = [], 0, None
    cursor = 0
    while cursor < len(value):
        char = value[cursor]
        if char == "\\":
            if cursor + 1 >= len(value):
                raise ZbMathError("Bibliographic TeX ends with an incomplete command; use another verified export.")
            match = re.match(r"\\([A-Za-z]+|.)", value[cursor:])
            command = match.group(1)
            cursor += len(match.group(0))
            if command == "(":
                if not allow_math or math is not None:
                    raise ZbMathError("Bibliographic TeX has nested or disallowed math delimiters.")
                math = ("parenthesis", groups)
            elif command == ")":
                if math != ("parenthesis", groups):
                    raise ZbMathError("Bibliographic TeX has unbalanced inline math or groups.")
                math = None
            elif command in {"{", "}", "%", "&", "#", "_", "$", " ", ",", ";", ":", "!", "/"}:
                pass
            elif command in _ACCENTS or command in _TEXT_COMMANDS or (math is not None and command in _MATH_COMMANDS):
                pass
            else:
                raise ZbMathError("Unsupported TeX command in bibliographic text; use another verified export rather than changing the title.")
            result.append("\\" + command)
            continue
        if char == "$":
            if not allow_math or value[cursor:cursor + 2] == "$$":
                raise ZbMathError("Only balanced inline mathematics is supported in bibliography titles.")
            if math is None:
                math = ("dollar", groups)
            elif math == ("dollar", groups):
                math = None
            else:
                raise ZbMathError("Bibliographic TeX has mixed math delimiters or unbalanced groups.")
            result.append(char)
        elif char == "{":
            groups += 1
            if groups > 32:
                raise ZbMathError("Bibliographic TeX grouping exceeds the conversion limit.")
            result.append(char)
        elif char == "}":
            groups -= 1
            if groups < 0 or (math is not None and groups < math[1]):
                raise ZbMathError("Bibliographic TeX has unbalanced groups.")
            result.append(char)
        elif char in "%&#":
            result.append("\\" + char)
        elif math is None and char in "_^~":
            result.append(_tex_literal(char))
        else:
            result.append(char)
        cursor += 1
    if math is not None or groups:
        raise ZbMathError("Bibliographic TeX has unclosed inline math or groups; use another verified export.")
    return "".join(result)


def deterministic_bibtex(candidate: dict, key: str) -> str:
    """Produce a conservative, reproducible BibTeX candidate from normalized data."""
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}", key):
        raise ZbMathError("Use a bibliography key containing only letters, digits, underscore, dot, colon, plus or hyphen.")
    if not isinstance(candidate, dict) or candidate.get("missing_fields"):
        raise ZbMathError("The official record is incomplete; use a separately verified authoritative bibliography source.")
    if candidate.get("document_type") in ("a", "b") or candidate.get("warnings"):
        raise ZbMathError("This record needs source-specific bibliography disambiguation before conversion.")
    for field in ("title", "author", "year"):
        if not _text(candidate.get(field)):
            raise ZbMathError("The official record lacks a required title, author, or year.")
    entry_type = candidate.get("entry_type")
    if entry_type not in ("article", "misc") or (entry_type == "article" and not _text(candidate.get("journal"))):
        raise ZbMathError("The normalized publication type is inconsistent.")
    record_url(candidate.get("document_id"))
    values = {field: _text(candidate.get(field)) for field in (
        "author", "title", "journal", "volume", "number", "pages", "year", "doi", "zbl", "eprint", "archiveprefix")}
    # Keep article eprints as supplementary identity, never relabel a journal
    # article as a preprint or infer publication from an arXiv record alone.
    values["url"] = "https://doi.org/" + values["doi"] if values["doi"] else candidate["zbmath_url"]
    # The project's plain.bst ignores eprint/archiveprefix/url. Keep a factual
    # visible identifier for indexed preprints without inventing a version.
    if candidate.get("publication_status") == "preprint" and values["eprint"]:
        values["note"] = "Preprint, arXiv:" + values["eprint"]
    if values["pages"]:
        values["pages"] = re.sub(r"(?<=\d)[–-](?=\d)", "--", values["pages"])
    lines = ["% Generated from zbMATH Open API metadata; not an original zbMATH BibTeX export.",
             "% Converter: " + CONVERTER_VERSION + "; record: " + candidate["source_url"],
             "@" + entry_type + "{" + key + ","]
    for field, value in values.items():
        if value:
            escaped = (_tex_bibliographic_text(value, allow_math=field in {"title", "journal"})
                       if field in {"author", "title", "journal"} else _tex_literal(value))
            # Protect proper names/acronyms in source titles from BibTeX case folding.
            lines.append("  " + field + " = {" + ("{" + escaped + "}" if field == "title" else escaped) + "},")
    lines.append("}")
    return "\n".join(lines) + "\n"
