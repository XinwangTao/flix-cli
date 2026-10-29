"""Render an Archify diagram HTML from a sidecar JSON using the existing template.

Usage: python make_diagram.py <json-path>
The template's renderer is data-driven, so only the JSON block, <title> and the
footer paths need replacing; html and json stay byte-for-byte consistent.
"""
import io
import json
import re
import sys
from pathlib import Path

DOCS = Path(r"F:\projects\flix-cli\docs\archify")
TEMPLATE = DOCS / "relaydesk_android_v2g_identity_workflow.html"


def main(json_path: Path) -> None:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    html = TEMPLATE.read_text(encoding="utf-8")
    title = data["meta"]["title"]

    html = re.sub(r"<title>[^<]*</title>", "<title>%s</title>" % title, html, count=1)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    html = re.sub(
        r'(<script id="diagram-data" type="application/json">).*?(</script>)',
        lambda m: m.group(1) + payload + m.group(2),
        html, count=1, flags=re.S)

    name = json_path.stem
    html = re.sub(r"docs/archify/[\w\-]+\.json", "docs/archify/%s.json" % name, html)
    html = re.sub(r"docs/product/[\w\-]+_brief\.md", "docs/archify/%s.json" % name, html)

    out = json_path.with_suffix(".html")
    io.open(out, "w", encoding="utf-8", newline="\n").write(html)
    print("wrote", out, len(html), "bytes")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
