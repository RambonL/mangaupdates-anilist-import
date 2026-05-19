"""
MangaUpdates JSON → AniList importer.

Requires .env with:
  ANILIST_TOKEN=your_token

Usage:
  python3 anilist_import.py search    # Phase 1: search all titles → candidates.json
  python3 anilist_import.py review    # Phase 2: local interactive review → decisions.json
  python3 anilist_import.py import    # Phase 3: batch add to AniList
  python3 anilist_import.py auth URL  # print OAuth URL (pass your client_id as URL)
"""
import asyncio
import difflib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

JSON_FILE = "export-read_id0_2026-05-19.json"
CANDIDATES_FILE = "candidates.json"
DECISIONS_FILE = "decisions.json"
SKIPPED_FILE = "skipped.json"
IMPORT_DONE_FILE = "import_done.json"

ANILIST_URL = "https://graphql.anilist.co"
AUTO_THRESHOLD = 0.60
CONCURRENCY = 5
RATE_DELAY = 2.0  # safe for degraded 30 req/min with CONCURRENCY=5


# ── GraphQL ───────────────────────────────────────────────────────────────────

SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 5) {
    media(search: $search, type: MANGA) {
      id
      title { romaji english native }
      format
      status
    }
  }
}
"""

SAVE_MUTATION = """
mutation ($mediaId: Int, $status: MediaListStatus, $progress: Int, $progressVolumes: Int) {
  SaveMediaListEntry(
    mediaId: $mediaId
    status: $status
    progress: $progress
    progressVolumes: $progressVolumes
  ) { id }
}
"""


def gql_sync(query, variables, token=None):
    payload = json.dumps({"query": query, "variables": variables}).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "mu-anilist-import/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(ANILIST_URL, data=payload, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read()), None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", 60))
                print(f"  Rate limited, waiting {retry_after}s (Retry-After)...")
                time.sleep(retry_after)
                continue
            body = e.read().decode(errors="replace")
            return None, f"HTTP {e.code}: {body[:200]}"
        except Exception as ex:
            return None, str(ex)
    return None, "max retries exceeded"


# ── Similarity ────────────────────────────────────────────────────────────────

def similarity(a, b):
    if not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def best_similarity(query, media):
    t = media.get("title", {})
    scores = [similarity(query, t.get(k, "")) for k in ("romaji", "english", "native")]
    return max(scores)


def display_title(media):
    t = media.get("title", {})
    return t.get("english") or t.get("romaji") or t.get("native") or "?"


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_env(path=".env"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Phase 1: search ───────────────────────────────────────────────────────────

def _search_one(title):
    data, err = gql_sync(SEARCH_QUERY, {"search": title})
    time.sleep(RATE_DELAY)
    if err or not data:
        return []
    return data.get("data", {}).get("Page", {}).get("media", [])


async def phase_search():
    with open(JSON_FILE) as f:
        entries = json.load(f)

    existing = {c["mu_title"]: c for c in load_json(CANDIDATES_FILE, [])}
    todo = [e for e in entries if e["title"] not in existing]

    print(f"Total: {len(entries)} | Already searched: {len(existing)} | Remaining: {len(todo)}")
    if not todo:
        print("All done. Run: python3 anilist_import.py review")
        return

    loop = asyncio.get_event_loop()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    completed = 0

    async def search_one(entry):
        async with semaphore:
            results = await loop.run_in_executor(executor, _search_one, entry["title"])
            return entry, results

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = [asyncio.ensure_future(search_one(e)) for e in todo]
        for fut in asyncio.as_completed(futures):
            entry, results = await fut
            completed += 1

            scored = sorted(
                [(best_similarity(entry["title"], m), i, m) for i, m in enumerate(results)],
                key=lambda x: x[0],
                reverse=True,
            )
            best_score = scored[0][0] if scored else 0.0
            auto = best_score >= AUTO_THRESHOLD

            top5 = [
                {
                    "id": m["id"],
                    "title_romaji": m["title"].get("romaji"),
                    "title_english": m["title"].get("english"),
                    "format": m.get("format"),
                    "status": m.get("status"),
                    "similarity": round(sc, 3),
                }
                for sc, _, m in scored
            ]

            flag = "AUTO" if auto else f"low({best_score:.0%})"
            print(f"[{len(existing)+completed}/{len(entries)}] {entry['title'][:55]:<55}  {flag}")

            existing[entry["title"]] = {
                "mu_title": entry["title"],
                "chapter": entry.get("chapter") or 0,
                "volume": entry.get("volume") or 0,
                "auto_match": auto,
                "top5": top5,
            }
            save_json(CANDIDATES_FILE, list(existing.values()))

    print(f"\nDone. Wrote {CANDIDATES_FILE}")
    print("Next: python3 anilist_import.py review")


# ── Phase 2: review ───────────────────────────────────────────────────────────

def phase_review():
    candidates = load_json(CANDIDATES_FILE, [])
    if not candidates:
        sys.exit(f"No {CANDIDATES_FILE} found. Run search first.")

    existing_decisions = {d["mu_title"]: d for d in load_json(DECISIONS_FILE, [])}
    existing_skipped = {s["mu_title"]: s for s in load_json(SKIPPED_FILE, [])}

    todo = [
        c for c in candidates
        if c["mu_title"] not in existing_decisions and c["mu_title"] not in existing_skipped
    ]

    auto = [c for c in todo if c["auto_match"]]
    manual = [c for c in todo if not c["auto_match"]]

    print(f"Auto-accepting {len(auto)} matches.")
    for c in auto:
        best = c["top5"][0]
        existing_decisions[c["mu_title"]] = {
            "mu_title": c["mu_title"],
            "chapter": c["chapter"],
            "volume": c["volume"],
            "anilist_id": best["id"],
            "anilist_title": best["title_english"] or best["title_romaji"],
        }
    save_json(DECISIONS_FILE, list(existing_decisions.values()))

    if not manual:
        print("No manual review needed.")
        print(f"Next: python3 anilist_import.py import")
        return

    print(f"\n{len(manual)} entries need manual review.")
    print("Commands: 1-5 = pick, s = skip, q = quit\n")

    for i, c in enumerate(manual, 1):
        print(f"[{i}/{len(manual)}] {c['mu_title']}")
        if not c["top5"]:
            print("  No results on AniList → skip")
            existing_skipped[c["mu_title"]] = {"mu_title": c["mu_title"], "chapter": c["chapter"], "volume": c["volume"], "top5": []}
            save_json(SKIPPED_FILE, list(existing_skipped.values()))
            continue

        for j, cand in enumerate(c["top5"], 1):
            title = cand.get("title_english") or cand.get("title_romaji") or "?"
            print(f"  {j}. [{cand['similarity']:.0%}] {title}  [{cand.get('format')}, {cand.get('status')}]  (id={cand['id']})")
        print("  s. Skip  |  id:12345 = manual AniList ID")

        while True:
            choice = input("  Choice: ").strip()
            if choice.lower() == "q":
                save_json(DECISIONS_FILE, list(existing_decisions.values()))
                save_json(SKIPPED_FILE, list(existing_skipped.values()))
                print("Quit. Run again to continue.")
                return
            elif choice.lower() == "s":
                existing_skipped[c["mu_title"]] = {
                    "mu_title": c["mu_title"],
                    "chapter": c["chapter"],
                    "volume": c["volume"],
                    "top5": c["top5"],
                }
                save_json(SKIPPED_FILE, list(existing_skipped.values()))
                break
            elif choice.lower().startswith("id:"):
                raw = choice[3:].strip()
                if raw.isdigit():
                    existing_decisions[c["mu_title"]] = {
                        "mu_title": c["mu_title"],
                        "chapter": c["chapter"],
                        "volume": c["volume"],
                        "anilist_id": int(raw),
                        "anilist_title": f"manual (id={raw})",
                    }
                    save_json(DECISIONS_FILE, list(existing_decisions.values()))
                    print(f"  Added manual id={raw}")
                    break
                else:
                    print("  Invalid ID — use id:12345 with a number.")
            elif choice.isdigit() and 1 <= int(choice) <= len(c["top5"]):
                chosen = c["top5"][int(choice) - 1]
                existing_decisions[c["mu_title"]] = {
                    "mu_title": c["mu_title"],
                    "chapter": c["chapter"],
                    "volume": c["volume"],
                    "anilist_id": chosen["id"],
                    "anilist_title": chosen.get("title_english") or chosen.get("title_romaji"),
                }
                save_json(DECISIONS_FILE, list(existing_decisions.values()))
                break
            else:
                print(f"  Invalid — enter 1-{len(c['top5'])}, id:12345, 's', or 'q'.")

    print(f"\nReview done. Decisions: {len(existing_decisions)} | Skipped: {len(existing_skipped)}")
    print(f"Next: python3 anilist_import.py import")


# ── Phase 3: import ───────────────────────────────────────────────────────────

BATCH_SIZE = 10


def build_batch_mutation(batch):
    parts = []
    variables = {}
    for i, d in enumerate(batch):
        parts.append(
            f"  m{i}: SaveMediaListEntry("
            f"mediaId: $mediaId{i}, status: $status{i}, "
            f"progress: $progress{i}, progressVolumes: $progressVolumes{i}"
            f") {{ id }}"
        )
        variables[f"mediaId{i}"] = d["anilist_id"]
        variables[f"status{i}"] = "CURRENT"
        variables[f"progress{i}"] = d["chapter"]
        variables[f"progressVolumes{i}"] = d["volume"]

    var_defs = ", ".join(
        f"$mediaId{i}: Int, $status{i}: MediaListStatus, "
        f"$progress{i}: Int, $progressVolumes{i}: Int"
        for i in range(len(batch))
    )
    query = f"mutation ({var_defs}) {{\n" + "\n".join(parts) + "\n}"
    return query, variables


def phase_import(token):
    decisions = load_json(DECISIONS_FILE, [])
    if not decisions:
        sys.exit(f"No {DECISIONS_FILE} found. Run review first.")

    done = set(load_json(IMPORT_DONE_FILE, []))
    todo = [d for d in decisions if str(d["anilist_id"]) not in done]

    print(f"Total: {len(decisions)} | Done: {len(done)} | Remaining: {len(todo)}")

    added = errors = 0
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]

    for b_idx, batch in enumerate(batches, 1):
        titles = ", ".join(d["mu_title"][:25] for d in batch)
        print(f"[batch {b_idx}/{len(batches)}] {titles[:70]}", end="  ", flush=True)

        query, variables = build_batch_mutation(batch)
        resp, err = gql_sync(query, variables, token=token)
        time.sleep(RATE_DELAY)

        if err:
            print(f"ERROR: {err}")
            errors += len(batch)
            continue

        data = resp.get("data", {}) if resp else {}
        batch_errors = resp.get("errors", []) if resp else []

        for i, d in enumerate(batch):
            if data.get(f"m{i}"):
                added += 1
                done.add(str(d["anilist_id"]))
            else:
                errors += 1

        save_json(IMPORT_DONE_FILE, list(done))

        if batch_errors:
            print(f"OK ({added} added, {len(batch_errors)} errors: {batch_errors[0].get('message')})")
        else:
            print(f"OK ({len(batch)} added)")

    print(f"\nDone. Added: {added} | Errors: {errors}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("search", "review", "import", "auth"):
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "auth":
        client_id = sys.argv[2] if len(sys.argv) > 2 else input("Client ID: ").strip()
        print(f"\nhttps://anilist.co/api/v2/oauth/authorize?client_id={client_id}&response_type=token\n")
        return

    if cmd == "search":
        asyncio.run(phase_search())
        return

    if cmd == "review":
        phase_review()
        return

    if cmd == "import":
        env = load_env()
        token = env.get("ANILIST_TOKEN") or os.environ.get("ANILIST_TOKEN")
        if not token:
            sys.exit("No ANILIST_TOKEN in .env")
        phase_import(token)


if __name__ == "__main__":
    main()
