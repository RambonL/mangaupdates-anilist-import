---
title: anilistimport
tags:
  - tool
  - python
  - anilist
---

# MangaUpdates → AniList Importer

Imports a MangaUpdates reading list into AniList via the GraphQL API.

> [!info] Three phases, each resumable
> `search` (network, ~3 min) → `review` (offline, interactive) → `import` (network, writes to AniList).
> Every phase writes its progress to disk, so interrupting is safe at any point.

## Setup

1. Export your MangaUpdates list as JSON (rename to match `JSON_FILE` in the script)
2. Create an AniList API client at https://anilist.co/settings/developer
3. Get a token:
   ```
   python3 anilist_import.py auth 12345
   ```
   Open the printed URL, copy `access_token` from the redirect URL.
4. Create `.env`:
   ```
   ANILIST_TOKEN=your_token_here
   ```

> [!danger] `.env` holds a write-scoped token
> `ANILIST_TOKEN` can modify your AniList list. Keep it out of git, and revoke the client
> at https://anilist.co/settings/developer once the import is done.

## Usage

```bash
# Phase 1: search all titles on AniList (~3 min, resumable)
python3 anilist_import.py search

# Phase 2: review low-confidence matches interactively (no network)
python3 anilist_import.py review
# Commands: 1-5 = pick candidate, id:12345 = manual AniList ID, s = skip, q = quit

# Phase 3: batch import decisions to AniList
python3 anilist_import.py import
```

> [!warning] Run `review` before `import`
> `search` only produces candidates. Anything below the confidence threshold stays out of
> `decisions.json` until you confirm it — skipping `review` silently drops those titles.

## Legacy scripts

> [!failure] Superseded — kept for reference only
> The MAL-XML detour below was replaced by direct AniList API calls. Don't start here.

| File | Description |
|------|-------------|
| `generate_mal_xml.py` | Generates a MAL-format XML via Jikan API lookups (replaced by direct AniList API) |
| `retry_failed.py` | Retries unmatched titles from the MAL XML approach |

## Output files

| File | Description |
|------|-------------|
| `candidates.json` | Search results per title |
| `decisions.json` | Accepted matches ready for import |
| `skipped.json` | Skipped entries with top-5 candidates |
| `import_done.json` | Successfully imported AniList IDs |
