# MangaUpdates → AniList Importer

Imports a MangaUpdates reading list into AniList via the GraphQL API.

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

Each phase saves progress — safe to interrupt and resume.

## Legacy scripts

These were earlier approaches and are no longer needed:

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
