# Horus Heresy Repertorium

A static site that indexes every mention of characters, legions, factions, and locations across the Horus Heresy novel series. Point it at your extracted EPUB collection and it builds a searchable, filterable reference that shows which books feature whom and how prominently.

## How it works

1. **Build** `build.py` scans a directory of extracted EPUB folders, parses each book's metadata, counts regex matches for every entity defined in `entities.yaml`, and writes `docs/index.json`.
Results are cached in `.build_cache.json`, so only changed books are reprocessed on subsequent runs.
2. **Frontend** `docs/index.html` is a self-contained single-file app (no framework, no build step) that reads `index.json` and renders a two-pane UI: entity list on the left, book cover grid on the right.

## Setup

### Prerequisites

- Python 3.10+
- [`just`](https://github.com/casey/just)
- Your Horus Heresy EPUBs extracted into a flat directory, each subfolder named `<number>-<slug>` (e.g. `01-horus-rising`)

### Install

```bash
just install
```

Creates a `.venv` and installs dependencies (`beautifulsoup4`, `lxml`, `pyyaml`, `livereload`, `Pillow`).

### Configure

Create a `.env` file at the project root:

```env
BOOKS=/path/to/your/extracted/epubs
```

### Build & serve

Build then serve at http://localhost:8000
```bash
just dev
```

Build only (writes `docs/index.json` and `docs/covers/`)
```bash
just build
```

Serve only, with hot-reloading
```bash
just serve
```

## Adding entities

Edit `entities.yaml`. Each entry takes a canonical `name` and optional `aliases`:

```yaml
characters:
  - name: Horus
    aliases: ["Lupercal", "the Warmaster"]
```

All aliases are merged into a single word-boundary regex (case-insensitive). Re-run `just build` to recount.

Categories: `characters`, `legions`, `factions`, `locations`, `authors`.

## Legal

This is an unofficial fan project, created under the [Games Workshop Fan Site Guidelines](https://www.warhammer.com/en-GB/legal#IntellectualPropertyGuidlines). It is not endorsed by or affiliated with Games Workshop or Black Library. Warhammer, The Horus Heresy, Black Library, and all related marks and characters are trademarks and/or © Games Workshop Limited.

## Credits

- **Font** [EB Garamond](https://github.com/octaviopardo/EBGaramond12) by Georg Duffner & Octavio Pardo, licensed under the [SIL Open Font License 1.1](https://openfontlicense.org)
