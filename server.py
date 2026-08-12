import json
import os
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
ROOT = Path(__file__).resolve().parent
BASE = "https://treasurehymns.com"

# The state is intentionally kept in memory so the OBS dock and Browser Source
# can communicate without a database.
state = {
    "hymn": None,
    "section": None,
    "stanza": None,
    "settings": {
        "show_title": True,
        "show_hymn_number": True,
        "show_section": False,
        "font_size": 58,
        "max_width": 1500,
        "line_height": 1.35,
        "text_align": "center",
    },
}


class HTMLTextParser(HTMLParser):
    """Extract useful block-level text and anchor text/URLs."""

    BLOCK_TAGS = {
        "p", "div", "br", "li", "ul", "ol",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "article", "section", "header", "footer",
        "main", "aside", "blockquote", "tr", "td", "th", "pre"
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.links = []
        self._buf = []
        self._anchor = None
        self._title = []
        self._in_title = False

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if text:
            self.lines.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self._flush()

        if tag == "title":
            self._in_title = True

        if tag == "a":
            attrs_dict = dict(attrs)
            self._anchor = {
                "href": attrs_dict.get("href"),
                "text": []
            }

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            self._flush()

        if tag == "title":
            self._in_title = False

        if tag == "a" and self._anchor is not None:
            text = re.sub(
                r"\s+",
                " ",
                "".join(self._anchor["text"])
            ).strip()
            self.links.append({
                "href": self._anchor["href"],
                "text": text
            })
            self._anchor = None

    def handle_data(self, data):
        if self._in_title:
            self._title.append(data)

        self._buf.append(data)

        if self._anchor is not None:
            self._anchor["text"].append(data)

    def close(self):
        super().close()
        self._flush()

    @property
    def title(self):
        return re.sub(r"\s+", " ", "".join(self._title)).strip()


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131 Safari/537.36 HymnDock/2.0"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
        },
    )

    with urllib.request.urlopen(request, timeout=25) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def clean_url(href):
    if not href:
        return None
    return urllib.parse.urljoin(BASE, href)


def normalize(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def hymn_number_from_text(text):
    match = re.search(r"\bhymn\s*[-#]?\s*(\d{1,4})\b", text or "", re.I)
    return int(match.group(1)) if match else None


def is_treasure_hymn_url(url):
    if not url:
        return False

    parsed = urllib.parse.urlparse(url)

    if parsed.netloc.lower() not in {
        "treasurehymns.com",
        "www.treasurehymns.com",
    }:
        return False

    path = parsed.path.lower()

    # A real hymn page on the site normally contains /hymn-###-.
    # We deliberately do not require one exact Yoruba category path.
    return bool(re.search(r"/hymn-\d{1,4}(?:-|/)", path))


def find_hymn_url(number):
    """
    Find the individual hymn page.

    The previous implementation collected every href from the search page and
    then tried to infer the correct result from the URL. That is fragile because
    WordPress search-result URLs can change.

    This implementation keeps each anchor's TEXT paired with its URL and first
    matches the search result title, then falls back to URL matching.
    """
    number = int(number)

    # Try the most likely WordPress search forms first.
    search_queries = [
        str(number),
        f"hymn {number}",
        f"hymn-{number}",
    ]

    search_urls = []
    seen = set()

    for query in search_queries:
        encoded = urllib.parse.quote_plus(query)

        candidates = [
            f"{BASE}/?s={encoded}",
            f"{BASE}/yor/?s={encoded}",
            f"{BASE}/yor/youruba-iwe-orin-mimo-anglican-hymnbook/?s={encoded}",
        ]

        for url in candidates:
            if url not in seen:
                seen.add(url)
                search_urls.append(url)

    # We score every candidate rather than stopping at the first weak match.
    scored = []

    for search_url in search_urls:
        try:
            html = fetch(search_url)
        except Exception:
            continue

        parser = HTMLTextParser()
        parser.feed(html)

        for link in parser.links:
            href = clean_url(link.get("href"))
            text = link.get("text", "")

            if not is_treasure_hymn_url(href):
                continue

            link_number = hymn_number_from_text(text)
            href_number = hymn_number_from_text(
                urllib.parse.unquote(
                    urllib.parse.urlparse(href).path
                )
            )

            score = 0

            # Strongest: anchor text says "Hymn 234".
            if link_number == number:
                score += 100

            # Also strong: URL contains hymn-234-.
            if href_number == number:
                score += 80

            # Search result title often contains "Hymn 234 ... Lyrics".
            normalized_text = normalize(text)
            if f"hymn {number}" in normalized_text:
                score += 40

            if f"hymn-{number}-" in normalize(href):
                score += 30

            # Prefer actual lyric pages over category/archive pages.
            if "lyrics" in normalize(text) or "lyrics" in normalize(href):
                score += 10

            if score:
                scored.append((score, href))

    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)

        # Remove duplicates while preserving score order.
        seen_urls = set()
        for score, href in scored:
            if href not in seen_urls:
                seen_urls.add(href)
                return href

    return None


STOP_LINES = re.compile(
    r"^(Previous:|Next:|Your email address|Comment|Name|Email|Website|"
    r"Search$|Search Results for|Hymns You May Like|Archives|Categories|"
    r"Share|Related Posts|Leave a Reply|Post navigation)",
    re.I,
)

NUMBERED_STANZA = re.compile(
    r"^\s*(\d{1,2})\s*(?:[.)]|[-:])\s*(.*)$"
)

# Also supports "1 Text" without punctuation.
NUMBERED_STANZA_SPACE = re.compile(
    r"^\s*(\d{1,2})\s+(.+?)\s*$"
)

APA_HEADING = re.compile(
    r"^\s*APA\s*([IVXLCDM]+|\d+)?\s*$",
    re.I
)


def is_probable_stanza(line):
    """Return (number, first_text) or None."""
    match = NUMBERED_STANZA.match(line)
    if match and match.group(2).strip():
        return int(match.group(1)), match.group(2).strip()

    match = NUMBERED_STANZA_SPACE.match(line)
    if match and match.group(2).strip():
        number = int(match.group(1))
        # Do not mistake normal page headings like "234 Search" for a verse.
        if number <= 99:
            return number, match.group(2).strip()

    return None


def append_line(stanza, line):
    line = re.sub(r"\s+", " ", line).strip()
    if not line:
        return
    if stanza["lines"] and stanza["lines"][-1] == line:
        return
    stanza["lines"].append(line)


def parse_sectioned_verses(lines):
    """
    Parse pages that explicitly contain APA I, APA II, etc.
    """
    sections = {}
    current_section = None
    current_stanza = None

    for line in lines:
        if STOP_LINES.match(line):
            if current_section:
                break
            continue

        heading = APA_HEADING.match(line)
        if heading:
            suffix = heading.group(1)
            current_section = (
                f"APA {suffix.upper()}" if suffix else "APA"
            )
            sections.setdefault(current_section, [])
            current_stanza = None
            continue

        if current_section is None:
            continue

        stanza = is_probable_stanza(line)
        if stanza:
            number, first_text = stanza

            # If a number is repeated, treat it as a new stanza.
            current_stanza = {
                "number": number,
                "lines": [first_text]
            }
            sections[current_section].append(current_stanza)
        elif current_stanza:
            append_line(current_stanza, line)

    return {
        name: verses
        for name, verses in sections.items()
        if verses
    }


def parse_numbered_verses(lines):
    """
    Parse ordinary hymns without APA headings.

    It looks for verse 1 first, then collects sequential numbered verses.
    This prevents page navigation numbers from being interpreted as lyrics.
    """
    first_index = None

    for i, line in enumerate(lines):
        stanza = is_probable_stanza(line)
        if stanza and stanza[0] == 1:
            first_index = i
            break

    if first_index is None:
        return []

    verses = []
    current = None
    expected = 1

    for line in lines[first_index:]:
        if STOP_LINES.match(line):
            break

        stanza = is_probable_stanza(line)

        if stanza:
            number, first_text = stanza

            # After verse 1, only accept sensible verse numbering. This helps
            # avoid sidebar/search numbers later in the page.
            if current is not None:
                if number == current["number"]:
                    append_line(current, first_text)
                    continue

                # Normal next verse.
                if number == current["number"] + 1:
                    verses.append(current)
                    current = {
                        "number": number,
                        "lines": [first_text]
                    }
                    expected = number + 1
                    continue

                # Some pages skip numbers; accept a larger verse number if
                # we already have a meaningful sequence.
                if number > current["number"] and number <= current["number"] + 3:
                    verses.append(current)
                    current = {
                        "number": number,
                        "lines": [first_text]
                    }
                    expected = number + 1
                    continue

                # Otherwise it is probably unrelated page content.
                break

            current = {
                "number": number,
                "lines": [first_text]
            }
            expected = number + 1

        elif current:
            append_line(current, line)

    if current:
        verses.append(current)

    # Require actual verse content, not just a navigation number.
    verses = [
        verse for verse in verses
        if verse["number"] >= 1 and any(
            len(text) > 2 for text in verse["lines"]
        )
    ]

    return verses


def find_hymn_title(lines, requested_number=None):
    if requested_number:
        pattern = re.compile(
            rf"^Hymn\s+{requested_number}\b.*",
            re.I
        )
        for line in lines:
            if pattern.match(line):
                return line

    for line in lines:
        if re.match(r"^Hymn\s+\d+\b", line, re.I):
            return line

    return ""


def parse_previous_next(html):
    parser = HTMLTextParser()
    parser.feed(html)

    previous = None
    next_url = None

    for link in parser.links:
        text = normalize(link.get("text"))
        href = clean_url(link.get("href"))

        if not href:
            continue

        if text.startswith("previous:") or text == "previous":
            previous = href
        elif text.startswith("next:") or text == "next":
            next_url = href

    return previous, next_url


def parse_hymn(url):
    html = fetch(url)

    parser = HTMLTextParser()
    parser.feed(html)

    lines = [
        line.strip()
        for line in parser.lines
        if line.strip()
    ]

    requested_number = hymn_number_from_text(url)
    title = find_hymn_title(lines, requested_number)

    if not title:
        # Search the HTML title as a fallback.
        title = parser.title or "Hymn"

    hymn_number = (
        hymn_number_from_text(title)
        or requested_number
    )

    # Start parsing after the hymn title where possible.
    title_index = -1
    for i, line in enumerate(lines):
        if title and normalize(line) == normalize(title):
            title_index = i
            break

    body = lines[title_index + 1:] if title_index >= 0 else lines

    # First: explicit APA sections.
    sections = parse_sectioned_verses(body)

    # Second: ordinary numbered verses.
    if not sections:
        numbered = parse_numbered_verses(body)
        if numbered:
            sections = {"Hymn": numbered}

    # Some pages put the title before the content but have extra headings
    # that caused the first pass to start too late. Retry against the full
    # text before giving up.
    if not sections:
        sections = parse_sectioned_verses(lines)

    if not sections:
        numbered = parse_numbered_verses(lines)
        if numbered:
            sections = {"Hymn": numbered}

    if not sections:
        raise ValueError(
            "Could not detect hymn verses on the individual Treasure Hymns "
            "page. The search result was found, but its lyric structure "
            "could not be parsed."
        )

    previous, next_url = parse_previous_next(html)

    return {
        "number": hymn_number,
        "title": title,
        "url": url,
        "sections": sections,
        "previous": previous,
        "next": next_url,
    }


def json_response(handler, payload, status=200):
    data = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(status)
    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8"
    )
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type"
    )
    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, POST, OPTIONS"
    )
    handler.send_header(
        "Content-Length",
        str(len(data))
    )
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(fmt % args)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/hymn":
            try:
                if "url" in query:
                    url = query["url"][0]

                    if not url.startswith(BASE + "/"):
                        raise ValueError(
                            "Only Treasure Hymns URLs are supported."
                        )

                elif "number" in query:
                    number = query["number"][0]
                    url = find_hymn_url(number)

                    if not url:
                        raise ValueError(
                            f"Could not find Hymn {number} on "
                            "Treasure Hymns."
                        )

                else:
                    raise ValueError(
                        "Provide a hymn number or Treasure Hymns URL."
                    )

                hymn = parse_hymn(url)

                json_response(
                    self,
                    {
                        "ok": True,
                        "hymn": hymn
                    }
                )

            except Exception as exc:
                print("Hymn request failed:", repr(exc))

                json_response(
                    self,
                    {
                        "ok": False,
                        "error": str(exc)
                    },
                    400
                )

            return

        if path == "/api/state":
            json_response(
                self,
                {
                    "ok": True,
                    "state": state
                }
            )
            return

        if path in ("/", "/dock"):
            filename = "dock.html"
        elif path == "/display":
            filename = "display.html"
        elif path in (
            "/display_bottom",
            "/display-bottom"
        ):
            filename = "display_bottom.html"
        else:
            filename = path.lstrip("/") or "dock.html"

        target = (ROOT / filename).resolve()
        root = ROOT.resolve()

        if (
            not str(target).startswith(str(root))
            or not target.is_file()
        ):
            self.send_error(404)
            return

        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(
            target.suffix.lower(),
            "application/octet-stream"
        )

        data = target.read_bytes()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Length",
            str(len(data))
        )
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path != "/api/state":
            self.send_error(404)
            return

        try:
            length = int(
                self.headers.get("Content-Length", "0")
            )
            body = self.rfile.read(length)

            incoming = json.loads(
                body.decode("utf-8")
            )

            if "hymn" in incoming:
                state["hymn"] = incoming["hymn"]

            if "section" in incoming:
                state["section"] = incoming["section"]

            if "stanza" in incoming:
                state["stanza"] = incoming["stanza"]

            if "settings" in incoming:
                state["settings"].update(
                    incoming["settings"]
                )

            json_response(
                self,
                {
                    "ok": True,
                    "state": state
                }
            )

        except Exception as exc:
            json_response(
                self,
                {
                    "ok": False,
                    "error": str(exc)
                },
                400
            )


def main():
    print("=" * 60)
    print("Hymn Dock for OBS")
    print(f"Listening on {HOST}:{PORT}")
    print("Dock:         /dock")
    print("Display:      /display")
    print("Bottom:       /display_bottom")
    print("=" * 60)

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Hymn Dock...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()