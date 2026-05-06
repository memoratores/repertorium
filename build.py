#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from importlib.util import find_spec
from pathlib import Path

try:
    import yaml
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    from PIL import Image

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    print("Missing dependencies. Run: pip install -r requirements.txt")
    sys.exit(1)

ROOT = Path(__file__).parent
DOCS_DIR = ROOT / "docs"
ENTITIES_FILE = ROOT / "entities.yaml"
OUTPUT_FILE = DOCS_DIR / "index.json"
CACHE_FILE = ROOT / ".build_cache.json"

COVER_MAX_WIDTH = 200  # px, covers displayed at ~85px; 200px covers 2× retina
CACHE_VERSION = 3  # bump whenever extraction logic changes to force a full rebuild


# entity loading


def load_author_aliases(path: Path) -> dict[str, str]:
    """lowercase alias → canonical author name"""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    mapping: dict[str, str] = {}
    for entry in raw.get("authors", []):
        canonical: str = entry["name"]
        mapping[canonical.lower()] = canonical
        for alias in entry.get("aliases", []):
            mapping[alias.lower()] = canonical
    return mapping


def _normalize_name(name: str, aliases: dict[str, str]) -> str:
    return aliases.get(name.strip().lower(), name.strip())


def load_entities(path: Path) -> dict[str, dict[str, re.Pattern]]:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    compiled: dict[str, dict[str, re.Pattern]] = {}
    for category, entries in raw.items():
        if category == "authors":
            continue
        compiled[category] = {}
        for entry in entries:
            name: str = entry["name"]
            aliases: list[str] = entry.get("aliases", [])
            terms = sorted([name] + aliases, key=len, reverse=True)
            pattern = re.compile(
                r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b",
                re.IGNORECASE,
            )
            compiled[category][name] = pattern
    return compiled


# book metadata


def find_cover(book_dir: Path, opf_path: Path) -> Path | None:
    try:
        text = opf_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(
            r'<meta\b[^>]*\bname=["\']cover["\'][^>]*\bcontent=["\']([^"\']+)["\']',
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not m:
            m = re.search(
                r'<meta\b[^>]*\bcontent=["\']([^"\']+)["\'][^>]*\bname=["\']cover["\']',
                text,
                re.IGNORECASE | re.DOTALL,
            )
        if m:
            cover_id = m.group(1)
            for item_m in re.finditer(
                r"<item\b([^>]+?)/?>", text, re.IGNORECASE | re.DOTALL
            ):
                attrs = item_m.group(1)
                id_m = re.search(r'\bid=["\']([^"\']+)["\']', attrs)
                href_m = re.search(r'\bhref=["\']([^"\']+)["\']', attrs)
                media_m = re.search(r'\bmedia-type=["\']([^"\']+)["\']', attrs)
                if id_m and href_m and id_m.group(1) == cover_id:
                    if media_m and "image" in media_m.group(1):
                        candidate = (opf_path.parent / href_m.group(1)).resolve()
                        if candidate.exists():
                            return candidate
    except Exception:
        pass

    for rel in [
        Path("cover.jpeg"),
        Path("cover.jpg"),
        Path("OEBPS") / "Images" / "cover.jpeg",
        Path("OEBPS") / "Images" / "cover.jpg",
        Path("OEBPS") / "images" / "cover.jpeg",
        Path("OEBPS") / "images" / "cover.jpg",
    ]:
        candidate = book_dir / rel
        if candidate.exists():
            return candidate

    return None


def parse_opf(opf_path: Path, author_aliases: dict[str, str] | None = None) -> dict:
    if author_aliases is None:
        author_aliases = {}
    text = opf_path.read_text(encoding="utf-8", errors="replace")

    title_m = re.search(r"<dc:title[^>]*>([^<]+)</dc:title>", text)
    title = title_m.group(1).strip() if title_m else ""

    story_authors: list[str] = []
    editors: list[str] = []
    for m in re.finditer(r"<dc:creator([^>]*)>([^<]+)</dc:creator>", text):
        attrs = m.group(1)
        name = _normalize_name(m.group(2), author_aliases)
        if not name:
            continue
        role_m = re.search(r'opf:role=["\'](\w+)["\']', attrs)
        role = role_m.group(1) if role_m else "aut"
        if role == "edt":
            editors.append(name)
        else:
            story_authors.append(name)

    return {
        "title": title,
        "author": ", ".join(story_authors),
        "editors": ", ".join(editors),
    }


def locate_book(book_dir: Path) -> Path | None:
    oebps_opf = book_dir / "OEBPS" / "content.opf"
    if oebps_opf.exists():
        return oebps_opf
    flat_opf = book_dir / "content.opf"
    if flat_opf.exists():
        return flat_opf
    return None


# text extraction

_HAVE_LXML = find_spec("lxml") is not None
_HTML_PARSER = "lxml" if _HAVE_LXML else "html.parser"
_XML_PARSER = "lxml-xml" if _HAVE_LXML else "html.parser"

# NCX navLabel text that identifies front/back matter to skip during entity counting.
_EXCLUDED_NCX_LABELS = frozenset(
    {
        "cover",
        "backlist",
        "back list",
        "title page",
        "also by",
        "also available",
        "copyright",
        "copyrights",
        "legal notice",
        "legal notices",
        "table of contents",
        "contents",
        "about the author",
        "about the authors",
        "afterword",
        "foreword",
        "preface",
        "acknowledgements",
        "acknowledgments",
    }
)
_EXCLUDED_NCX_PREFIXES = ("an extract from",)


def _is_excluded_label(label: str) -> bool:
    s = label.strip().lower()
    return s in _EXCLUDED_NCX_LABELS or any(
        s.startswith(p) for p in _EXCLUDED_NCX_PREFIXES
    )


def _get_spine_paths(opf_path: Path) -> list[Path]:
    """Return ordered list of HTML/XHTML file paths declared in the OPF spine."""
    opf_dir = opf_path.parent
    text = opf_path.read_text(encoding="utf-8", errors="replace")

    id_to_href: dict[str, str] = {}
    for m in re.finditer(r"<item\b([^>]+?)/>", text, re.IGNORECASE | re.DOTALL):
        attrs = m.group(1)
        id_m = re.search(r'\bid=["\']([^"\']+)["\']', attrs)
        href_m = re.search(r'\bhref=["\']([^"\']+)["\']', attrs)
        media_m = re.search(r'\bmedia-type=["\']([^"\']+)["\']', attrs)
        if id_m and href_m and media_m and "html" in media_m.group(1).lower():
            id_to_href[id_m.group(1)] = href_m.group(1)

    spine_m = re.search(r"<spine\b[^>]*>(.*?)</spine>", text, re.DOTALL | re.IGNORECASE)
    files: list[Path] = []
    if spine_m:
        for item_m in re.finditer(
            r"<itemref\b([^>]+?)/>", spine_m.group(1), re.IGNORECASE | re.DOTALL
        ):
            attrs = item_m.group(1)
            idref_m = re.search(r'\bidref=["\']([^"\']+)["\']', attrs)
            if idref_m:
                href = id_to_href.get(idref_m.group(1))
                if href:
                    p = (opf_dir / href).resolve()
                    if p.exists():
                        files.append(p)
    return files


def _get_excluded_files(opf_path: Path) -> set[Path]:
    """Return absolute paths of front/back matter files to skip."""
    excluded: set[Path] = set()
    opf_dir = opf_path.parent
    opf_text = opf_path.read_text(encoding="utf-8", errors="replace")

    # OPF guide: cover and toc reference types mark non-content pages.
    guide_m = re.search(r"<guide>(.*?)</guide>", opf_text, re.DOTALL | re.IGNORECASE)
    if guide_m:
        for ref_m in re.finditer(
            r"<reference\b([^>]+?)/>", guide_m.group(1), re.IGNORECASE | re.DOTALL
        ):
            attrs = ref_m.group(1)
            type_m = re.search(r'\btype=["\']([^"\']+)["\']', attrs)
            href_m = re.search(r'\bhref=["\']([^"\']+)["\']', attrs)
            if type_m and href_m and type_m.group(1).lower() in {"cover", "toc"}:
                excluded.add((opf_dir / href_m.group(1).split("#")[0]).resolve())

    # NCX: navLabel text identifies pages like "Backlist", "Title Page", etc.
    ncx_href_m = re.search(
        r'<item\b[^>]*\bmedia-type=["\']application/x-dtbncx\+xml["\'][^>]*\bhref=["\']([^"\']+)["\']',
        opf_text,
        re.IGNORECASE,
    )
    if not ncx_href_m:
        ncx_href_m = re.search(
            r'<item\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*\bmedia-type=["\']application/x-dtbncx\+xml["\']',
            opf_text,
            re.IGNORECASE,
        )
    ncx_path = (
        (opf_dir / ncx_href_m.group(1)).resolve() if ncx_href_m else opf_dir / "toc.ncx"
    )
    if ncx_path.exists():
        try:
            ncx_text = ncx_path.read_text(encoding="utf-8", errors="replace")
            # Scan <text> and <content src> tokens in document order.
            # Each navPoint's label immediately precedes its <content>, even when nested,
            # so navPoint-block matching (which breaks on nesting) is unnecessary.
            pending_label: str | None = None
            for tok in re.finditer(
                r'<text>([^<]+)</text>|<content\b[^>]*\bsrc=["\']([^"\']+)["\']',
                ncx_text,
                re.IGNORECASE,
            ):
                text_val, src_val = tok.group(1), tok.group(2)
                if text_val is not None:
                    pending_label = text_val.strip()
                elif src_val is not None and pending_label is not None:
                    if _is_excluded_label(pending_label):
                        excluded.add(
                            (ncx_path.parent / src_val.split("#")[0]).resolve()
                        )
                    pending_label = None
        except Exception:
            pass

    return excluded


def extract_text(opf_path: Path) -> str:
    """Extract plain text from spine content files, skipping front/back matter."""
    excluded = _get_excluded_files(opf_path)
    files = [f for f in _get_spine_paths(opf_path) if f not in excluded]
    parts: list[str] = []
    for html_file in files:
        parser = _XML_PARSER if html_file.suffix == ".xhtml" else _HTML_PARSER
        raw = html_file.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(raw, parser)
        text = soup.get_text(" ")
        text = text.replace("‘", "'").replace("’", "'")
        text = text.replace("“", '"').replace("”", '"')
        parts.append(text)
    return "\n".join(parts)


# cache


def _entities_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _text_mtime(opf_path: Path) -> float:
    files = _get_spine_paths(opf_path)
    return max((f.stat().st_mtime for f in files), default=0.0)


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


# _process_book must be module-level for multiprocessing pickling


def _process_book(book_dir_str: str, covers_dir_str: str) -> tuple[str, dict]:
    book_dir = Path(book_dir_str)
    covers_dir = Path(covers_dir_str)

    book_id = book_dir.name
    number = int(book_id.split("-")[0])

    opf_path = locate_book(book_dir)
    if opf_path is None:
        raise ValueError(f"no content.opf in {book_id}")

    author_aliases = load_author_aliases(ENTITIES_FILE)
    meta = parse_opf(opf_path, author_aliases)
    if not meta["title"]:
        meta["title"] = book_id.replace("-", " ").replace("_", " ").title()

    text = extract_text(opf_path)

    entities = load_entities(ENTITIES_FILE)
    counts: dict[str, dict[str, int]] = {}
    for cat, patterns in entities.items():
        counts[cat] = {}
        for name, pattern in patterns.items():
            c = len(pattern.findall(text))
            if c:
                counts[cat][name] = c

    cover = None
    if os.environ.get("EXPORT_COVERS", "").strip().lower() in ("1", "true", "yes"):
        cover_src = find_cover(book_dir, opf_path)
        if cover_src:
            dest = covers_dir / f"{book_id}.webp"
            with Image.open(cover_src) as img:
                if img.width > COVER_MAX_WIDTH:
                    ratio = COVER_MAX_WIDTH / img.width
                    img = img.resize(
                        (COVER_MAX_WIDTH, round(img.height * ratio)),
                        Image.Resampling.LANCZOS,
                    )
                img.convert("RGB").save(dest, "WEBP", quality=85, method=6)
            cover = str(dest.relative_to(DOCS_DIR))

    book_meta = {
        "id": book_id,
        "title": meta["title"],
        "author": meta["author"],
        "editors": meta["editors"],
        "number": number,
        "cover": cover,
    }
    return book_id, {"book": book_meta, "counts": counts}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Horus Heresy entity index.")
    parser.add_argument(
        "books_dir", type=Path, help="directory containing extracted EPUB folders"
    )
    args = parser.parse_args()
    books_dir = args.books_dir.resolve()

    start = time.monotonic()
    print(f"Books directory : {books_dir}")

    ents_hash = _entities_hash(ENTITIES_FILE)
    cache = _load_cache()

    covers_dir = DOCS_DIR / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)

    book_dirs = sorted(
        d for d in books_dir.iterdir() if d.is_dir() and d.name[0].isdigit()
    )

    to_process: list[tuple[Path, float]] = []
    skipped: list[str] = []
    cache_hits: dict[str, dict] = {}

    for book_dir in book_dirs:
        book_id = book_dir.name
        opf_path = locate_book(book_dir)
        if opf_path is None:
            skipped.append(book_id)
            continue
        mtime = _text_mtime(opf_path)
        entry = cache.get(book_id)
        if (
            entry
            and entry.get("cache_version") == CACHE_VERSION
            and entry.get("ents_hash") == ents_hash
            and abs(entry.get("mtime", 0.0) - mtime) < 1.0
        ):
            cache_hits[book_id] = entry
        else:
            to_process.append((book_dir, mtime))

    for bid in skipped:
        print(f"  SKIP {bid}: no content.opf found")

    n_cached = len(cache_hits)
    n_fresh = len(to_process)
    print(f"  {n_cached} cached, {n_fresh} to process\n")

    new_results: dict[str, dict] = {}

    if to_process:
        workers = min(n_fresh, os.cpu_count() or 4)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_process_book, str(bd), str(covers_dir)): (bd.name, mt)
                for bd, mt in to_process
            }
            for future in as_completed(futures):
                book_id, mtime = futures[future]
                try:
                    _, payload = future.result()
                    new_results[book_id] = payload
                    meta = payload["book"]
                    print(f"  [{meta['number']:02d}] {meta['title']} … done")
                except Exception as exc:
                    print(f"  ERROR {book_id}: {exc}")

    all_results = {**cache_hits, **new_results}

    books: list[dict] = []
    entity_counts: dict[str, dict[str, dict[str, int]]] = {}

    for book_dir in book_dirs:
        book_id = book_dir.name
        if book_id not in all_results:
            continue
        payload = all_results[book_id]
        books.append(payload["book"])
        for cat, cat_counts in payload["counts"].items():
            entity_counts.setdefault(cat, {})
            for name, count in cat_counts.items():
                entity_counts[cat].setdefault(name, {})[book_id] = count

    mtime_by_id = {bd.name: mt for bd, mt in to_process}
    updated_cache = dict(cache)
    for book_id, payload in new_results.items():
        updated_cache[book_id] = {
            "cache_version": CACHE_VERSION,
            "ents_hash": ents_hash,
            "mtime": mtime_by_id[book_id],
            "book": payload["book"],
            "counts": payload["counts"],
        }
    valid_ids = {bd.name for bd in book_dirs}
    updated_cache = {k: v for k, v in updated_cache.items() if k in valid_ids}
    _save_cache(updated_cache)

    output = {"books": books, "entities": entity_counts}
    OUTPUT_FILE.write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    elapsed = time.monotonic() - start
    size_kb = OUTPUT_FILE.stat().st_size // 1024
    print(f"\nWrote {OUTPUT_FILE.name} ({size_kb} KB)")
    print(f"Indexed {len(books)} books in {elapsed:.1f}s.")


if __name__ == "__main__":
    main()
