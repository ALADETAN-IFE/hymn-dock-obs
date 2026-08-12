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


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.hrefs = []
        self._buf = []
        self._in_title = False
        self.title = ""
        self.block_tags = {
            "p", "div", "br", "li", "ul", "ol",
            "h1", "h2", "h3", "h4", "h5", "h6",
            "article", "section", "header", "footer",
            "main", "aside", "blockquote", "tr", "td", "th", "pre"
        }

    def _flush(self):
        text = "".join(self._buf).strip()
        if text:
            self.lines.append(re.sub(r"\s+", " ", text))
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.block_tags:
            self._flush()
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)

    def handle_endtag(self, tag):
        if tag in self.block_tags:
            self._flush()
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131 Safari/537.36 HymnDock/1.1"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def clean_url(href):
    return urllib.parse.urljoin(BASE, href)


def find_hymn_url(number):
    number = str(int(number))
    queries = [
        f"{BASE}/?s=hymn+{urllib.parse.quote_plus(number)}",
        f"{BASE}/yor/?s=hymn+{urllib.parse.quote_plus(number)}",
        f"{BASE}/yor/youruba-iwe-orin-mimo-anglican-hymnbook/?s=hymn+{urllib.parse.quote_plus(number)}",
    ]

    strict = re.compile(
        rf"/yor/[^\"']*/hymn-{re.escape(number)}-[^\"']+-lyrics/?$",
        re.I,
    )

    for search_url in queries:
        try:
            html = fetch(search_url)
            parser = PageParser()
            parser.feed(html)
            candidates = [clean_url(href) for href in parser.hrefs]

            for url in candidates:
                path = urllib.parse.urlparse(url).path.rstrip("/") + "/"
                if strict.search(path):
                    return url

            for url in candidates:
                path = urllib.parse.urlparse(url).path.lower()
                if f"/hymn-{number}-" in path and "lyrics" in path and "/yor/" in path:
                    return url
        except Exception:
            continue

    return None


STOP_LINES = re.compile(
    r"^(Previous:|Next:|Your email address|Comment|Name|Email|Website|"
    r"Search$|Hymns You May Like|Archives|Categories|Share|Related Posts)",
    re.I,
)

# Supports: "1 Text", "1. Text", "1) Text", and "1 - Text".
NUMBERED_STANZA = re.compile(r"^(\d{1,2})(?:[.)]|\\s+-\\s+|\\s+)(.*)$")


def parse_numbered_stanzas(lines):
    stanzas = []
    current = None

    for line in lines:
        if STOP_LINES.match(line):
            if current:
                stanzas.append(current)
            break

        match = NUMBERED_STANZA.match(line)
        if match:
            if current:
                stanzas.append(current)
            number = int(match.group(1))
            first_line = match.group(2).strip()
            # Avoid treating a bare navigation number as a hymn stanza.
            if not first_line:
                current = None
                continue
            current = {"number": number, "lines": [first_line]}
        elif current:
            current["lines"].append(line)

    if current:
        stanzas.append(current)

    return stanzas


def parse_hymn(url):
    html = fetch(url)
    parser = PageParser()
    parser.feed(html)
    lines = [line.strip() for line in parser.lines if line.strip()]

    # Find a useful hymn title.
    title = ""
    title_index = -1

    for i, line in enumerate(lines):
        if re.match(r"^Hymn\s+\d+\b", line, re.I):
            title = line
            title_index = i
            break

    if not title:
        match = re.search(r"Hymn\s+\d+\b[^<\n]*", parser.title, re.I)
        title = match.group(0).strip() if match else "Hymn"

    hymn_number_match = re.search(r"Hymn\s+(\d+)", title, re.I)
    hymn_number = int(hymn_number_match.group(1)) if hymn_number_match else None

    # First try the existing APA-style structure.
    sections = {}
    current_section = None
    current_stanza = None
    body = lines[max(title_index + 1, 0):]

    for line in body:
        section_match = re.fullmatch(r"APA\s+(.+)", line, re.I)
        if section_match:
            current_section = line.upper()
            sections.setdefault(current_section, [])
            current_stanza = None
            continue

        if current_section and STOP_LINES.match(line):
            break

        match = NUMBERED_STANZA.match(line)
        if match and current_section:
            current_stanza = {
                "number": int(match.group(1)),
                "lines": [match.group(2).strip()],
            }
            sections[current_section].append(current_stanza)
        elif current_section and current_stanza:
            current_stanza["lines"].append(line)

    sections = {name: verses for name, verses in sections.items() if verses}

    # NEW: many Treasure Hymns pages have no APA headings.
    # In that case, treat the numbered hymn verses as one section called "Hymn".
    if not sections:
        numbered_lines = body

        # Remove common navigation/header noise before looking for verse 1.
        # We start at the first plausible numbered stanza.
        first_stanza_index = None
        for i, line in enumerate(numbered_lines):
            match = NUMBERED_STANZA.match(line)
            if match and match.group(2).strip():
                if int(match.group(1)) == 1:
                    first_stanza_index = i
                    break

        if first_stanza_index is not None:
            fallback = parse_numbered_stanzas(numbered_lines[first_stanza_index:])
            if fallback:
                sections["Hymn"] = fallback

    if not sections:
        raise ValueError(
            "Could not detect hymn verses on the page. "
            "Try pasting the full Treasure Hymns hymn URL."
        )

    # Previous / Next hymn links.
    previous = None
    next_url = None

    class LinkTextParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.current = None
            self.links = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.current = {
                    "href": dict(attrs).get("href"),
                    "text": [],
                }

        def handle_endtag(self, tag):
            if tag == "a" and self.current:
                self.links.append(self.current)
                self.current = None

        def handle_data(self, data):
            if self.current:
                self.current["text"].append(data)

    link_parser = LinkTextParser()
    link_parser.feed(html)

    for link in link_parser.links:
        href = link.get("href")
        if not href:
            continue
        text = " ".join(link["text"]).strip()
        full_url = clean_url(href)

        if re.match(r"Previous:", text, re.I):
            previous = full_url
        elif re.match(r"Next:", text, re.I):
            next_url = full_url

    return {
        "number": hymn_number,
        "title": title,
        "url": url,
        "sections": sections,
        "previous": previous,
        "next": next_url,
    }


def json_response(handler, payload, status=200):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
                        raise ValueError("Only Treasure Hymns URLs are supported.")
                elif "number" in query:
                    url = find_hymn_url(query["number"][0])
                    if not url:
                        raise ValueError(
                            f"Could not find Hymn {query['number'][0]} on Treasure Hymns."
                        )
                else:
                    raise ValueError("Provide a hymn number or URL.")

                hymn = parse_hymn(url)
                json_response(self, {"ok": True, "hymn": hymn})
            except Exception as exc:
                json_response(self, {"ok": False, "error": str(exc)}, 400)
            return

        if path == "/api/state":
            json_response(self, {"ok": True, "state": state})
            return

        if path in ("/", "/dock"):
            filename = "dock.html"
        elif path == "/display":
            filename = "display.html"
        elif path in ("/display_bottom", "/display-bottom"):
            filename = "display_bottom.html"
        else:
            filename = path.lstrip("/") or "dock.html"

        target = (ROOT / filename).resolve()
        root = ROOT.resolve()

        if not str(target).startswith(str(root)) or not target.is_file():
            self.send_error(404)
            return

        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(target.suffix.lower(), "application/octet-stream")

        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path != "/api/state":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            incoming = json.loads(body.decode("utf-8"))

            if "hymn" in incoming:
                state["hymn"] = incoming["hymn"]
            if "section" in incoming:
                state["section"] = incoming["section"]
            if "stanza" in incoming:
                state["stanza"] = incoming["stanza"]
            if "settings" in incoming:
                state["settings"].update(incoming["settings"])

            json_response(self, {"ok": True, "state": state})
        except Exception as exc:
            json_response(self, {"ok": False, "error": str(exc)}, 400)


def main():
    print("=" * 60)
    print("Hymn Dock for OBS")
    print(f"Listening on {HOST}:{PORT}")
    print(f"Dock:          /dock")
    print(f"Transparent:   /display")
    print(f"Bottom style:  /display_bottom")
    print("=" * 60)

    server = ThreadingHTTPServer((HOST, PORT), Handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Hymn Dock...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()