"""Enrichment do site: puxa RSS dos canais YouTube e injeta VideoObject schema nas landings.

- Sem API key (usa o feed publico de cada canal)
- Resolve @handle -> channelId scrapando a pagina do canal
- Injeta bloco JSON-LD entre marcadores HTML (idempotente)
- Roda 1x/dia via GitHub Action ou manualmente: `python scripts/enrich_site.py`
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

CHANNELS = {
    "pt": {
        "plumanoturna":  "@plumanoturna",
        "plumasagrada":  "@plumasagrada",
        "plumaabissal":  "@plumaabissal",
        "plumaestelar":  "@plumaestelar",
    },
    "en": {
        "nightfeather":  "@nightfeathersleepstories",
        "sacredfeather": "@sacredfeathermysteries",
        "abyssalplume":  "@abyssalplume",
        "stellarfeather":"@stellarfeather",
    },
}

VIDEOS_PER_CHANNEL = 2
UA = "Mozilla/5.0 (compatible; PlumaHistoriasEnrich/1.0; +https://www.plumanoturna.com.br/)"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt":    "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def resolve_channel_id(handle: str) -> str:
    """@handle -> UCxxx scrapando a pagina publica do canal."""
    url = f"https://www.youtube.com/{handle}"
    html = http_get(url).decode("utf-8", errors="ignore")
    for pat in (
        r'"externalId":"(UC[\w-]{22,})"',
        r'"channelId":"(UC[\w-]{22,})"',
        r'channel_id=(UC[\w-]{22,})',
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    raise ValueError(f"channelId nao encontrado para {handle}")


def fetch_videos(channel_id: str, limit: int) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    root = ET.fromstring(http_get(url))
    out = []
    for entry in root.findall("atom:entry", NS)[:limit]:
        vid = entry.findtext("yt:videoId", default="", namespaces=NS)
        title = entry.findtext("atom:title", default="", namespaces=NS)
        published = entry.findtext("atom:published", default="", namespaces=NS)
        author = entry.findtext("atom:author/atom:name", default="", namespaces=NS)
        mg = entry.find("media:group", NS)
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        description = title
        if mg is not None:
            t = mg.find("media:thumbnail", NS)
            if t is not None and "url" in t.attrib:
                thumb = t.attrib["url"]
            d = mg.find("media:description", NS)
            if d is not None and d.text:
                description = d.text.strip()[:280]
        if not vid:
            continue
        out.append({
            "@type": "VideoObject",
            "@id": f"https://www.youtube.com/watch?v={vid}",
            "name": title,
            "description": description,
            "thumbnailUrl": thumb,
            "uploadDate": published,
            "contentUrl": f"https://www.youtube.com/watch?v={vid}",
            "embedUrl": f"https://www.youtube.com/embed/{vid}",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "author": {"@type": "Organization", "name": author},
            "publisher": {"@type": "Organization", "name": author},
        })
    return out


def build_schema_graph(locale: str) -> dict | None:
    videos: list[dict] = []
    for slug, handle in CHANNELS[locale].items():
        try:
            cid = resolve_channel_id(handle)
            vids = fetch_videos(cid, VIDEOS_PER_CHANNEL)
            print(f"  [{locale}] {handle} -> {cid}: {len(vids)} videos")
            videos.extend(vids)
        except Exception as e:
            print(f"  [{locale}] WARN {handle}: {e}", file=sys.stderr)
    if not videos:
        return None
    return {"@context": "https://schema.org", "@graph": videos}


MARK_START = "<!-- VIDEOS_JSONLD_START -->"
MARK_END   = "<!-- VIDEOS_JSONLD_END -->"


def inject(html_path: Path, schema: dict) -> bool:
    html = html_path.read_text(encoding="utf-8")
    payload = (
        f"{MARK_START}\n"
        f'<script type="application/ld+json">\n'
        f"{json.dumps(schema, indent=2, ensure_ascii=False)}\n"
        f"</script>\n"
        f"{MARK_END}"
    )
    if MARK_START in html and MARK_END in html:
        new = re.sub(
            re.escape(MARK_START) + r".*?" + re.escape(MARK_END),
            lambda _m: payload,
            html,
            count=1,
            flags=re.DOTALL,
        )
    else:
        new = html.replace("</head>", f"    {payload}\n</head>", 1)
    if new != html:
        html_path.write_text(new, encoding="utf-8")
        return True
    return False


def main() -> int:
    site = Path(__file__).resolve().parent.parent
    targets = [
        ("pt", site / "index.html"),
        ("en", site / "nightfeather" / "index.html"),
    ]
    changed = 0
    for locale, path in targets:
        print(f"[{locale}] fetching {path.relative_to(site)}")
        schema = build_schema_graph(locale)
        if schema is None:
            print(f"  [{locale}] no videos, skipping inject")
            continue
        if inject(path, schema):
            print(f"  [{locale}] injected {len(schema['@graph'])} VideoObjects")
            changed += 1
        else:
            print(f"  [{locale}] no-op (schema unchanged)")
    print(f"\ndone. {changed} file(s) changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
