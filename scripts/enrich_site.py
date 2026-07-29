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
    "en": {
        "nightfeather":  "@nightfeathersleepstories",
        "sacredfeather": "@sacredfeathermysteries",
        "abyssalplume":  "@abyssalplume",
        "stellarfeather":"@stellarfeather",
    },
}

# PT usa o feed da PLAYLIST de long-form, nao o do canal.
#
# O feed do canal (videos.xml?channel_id=) mistura shorts, e como eles saem em
# volume maior ocupam sempre o topo — a thumb do card no site acabava sendo a de
# um short em vez do episodio. Estes sao os ids das playlists "video_long" que o
# publisher alimenta sozinho a cada episodio (channels/{slug}.yaml, campo
# platforms.youtube.defaults.playlists.video_long), entao so tem long-form ali.
# Ao adicionar canal novo, pegar o id de la.
#
# O feed de playlist traz author, thumbnail e description iguais aos do feed de
# canal, entao o resto do script e o JS do site nao precisaram mudar.
PLAYLISTS_LONGFORM = {
    "plumasagrada": "PL0KgwMAr-M3WFjbfiXc3UAuAV8erc7KRo",
    "plumaabissal": "PLMRfHsuvj3DusHB82Jp7GIFQ1iRS7T3rd",
    "plumaestelar": "PLsW-azZ7uKyQea3Lf-cLk3cD4O1_AHRsm",
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


def best_thumbnail(vid: str, fallback: str) -> str:
    """Maior thumb que o video realmente tiver.

    O RSS entrega `hqdefault.jpg`, que e 480x360 em 4:3 — com o card em 16/9 e
    object-fit: cover sobram uns 480x270 uteis. Isso bastava quando o card tinha
    ~330px, mas os cards empilhados tem 680px, entao virava ampliacao (2.8x em
    tela retina) e a imagem ficava borrada.

    `maxresdefault` e 1280x720 nativo 16:9 e existe sempre que a thumb foi subida
    em HD — que e o caso de tudo que sai do publisher. Mesmo assim testamos antes
    de gravar: video antigo ou sem thumb custom devolve 404 aqui.
    """
    for nome in ("maxresdefault", "sddefault"):
        url = f"https://i.ytimg.com/vi/{vid}/{nome}.jpg"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return url
        except Exception:
            continue
    return fallback


def fetch_videos(feed_url: str, limit: int) -> list[dict]:
    """Le um feed de videos do YouTube. Serve tanto pra `channel_id=` quanto pra
    `playlist_id=` — os dois tem o mesmo formato Atom."""
    root = ET.fromstring(http_get(feed_url))
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
        thumb = best_thumbnail(vid, thumb)
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

    if locale == "pt":
        # Playlists de long-form — ver nota em PLAYLISTS_LONGFORM.
        for slug, playlist_id in PLAYLISTS_LONGFORM.items():
            try:
                url = f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"
                vids = fetch_videos(url, VIDEOS_PER_CHANNEL)
                print(f"  [pt] {slug} (playlist long-form): {len(vids)} videos")
                videos.extend(vids)
            except Exception as e:
                print(f"  [pt] WARN {slug}: {e}", file=sys.stderr)
    else:
        for slug, handle in CHANNELS[locale].items():
            try:
                cid = resolve_channel_id(handle)
                url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
                vids = fetch_videos(url, VIDEOS_PER_CHANNEL)
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
