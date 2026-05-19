"""
Re-queries Jikan for titles in failed_titles.txt.
Shows best MAL candidate for each with NO threshold filter.
Writes retry_mapping.txt for review.
Writes manga_list_retry.xml with ALL candidates (review before importing!).

Note: chapter/volume progress defaults to 0 since we don't re-parse the HTML.
      Cross-reference with manga_list.xml if needed.
"""
import asyncio
import difflib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

FAILED_FILE = "failed_titles.txt"
RETRY_XML = "manga_list_retry.xml"
RETRY_MAPPING = "retry_mapping.txt"
JIKAN_URL = "https://api.jikan.moe/v4/manga"
CONCURRENCY = 5


def _similarity(a, b):
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _fetch(title):
    params = urllib.parse.urlencode({"q": title, "limit": 5})
    url = f"{JIKAN_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "anilist-import/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = data.get("data", [])
        if not results:
            return None, None, 0.0, False  # (mal_id, title, score, rate_limited)
        best = max(results, key=lambda r: _similarity(title, r.get("title", "")))
        score = _similarity(title, best["title"])
        return best["mal_id"], best["title"], score, False
    except urllib.error.HTTPError as e:
        return None, None, 0.0, (e.code == 429)
    except Exception:
        return None, None, 0.0, False


async def lookup_all(titles):
    semaphore = asyncio.Semaphore(CONCURRENCY)
    loop = asyncio.get_event_loop()
    results = [None] * len(titles)
    completed = 0
    total = len(titles)

    async def tracked(i, title):
        async with semaphore:
            for attempt in range(4):
                mal_id, mal_title, score, limited = await loop.run_in_executor(
                    executor, _fetch, title
                )
                if limited:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return i, mal_id, mal_title, score
            return i, None, None, 0.0

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = [asyncio.ensure_future(tracked(i, t)) for i, t in enumerate(titles)]
        for fut in asyncio.as_completed(futures):
            i, mal_id, mal_title, score = await fut
            completed += 1
            title = titles[i]
            if mal_id:
                flag = "???" if score < 0.4 else ("~OK" if score < 0.6 else "OK ")
                status = f"[{flag} {score:.0%}] id={mal_id} {mal_title!r}"
            else:
                status = "[NONE] no results on MAL"
            print(f"[{completed}/{total}] {title[:52]:<52}  {status}")
            results[i] = (mal_id, mal_title, score)

    return results


def build_xml(entries):
    root = ET.Element("myanimelist")
    info = ET.SubElement(root, "myinfo")
    ET.SubElement(info, "user_id").text = "1"
    ET.SubElement(info, "user_name").text = "user"
    ET.SubElement(info, "user_export_type").text = "2"
    ET.SubElement(info, "user_total_manga").text = str(len(entries))
    ET.SubElement(info, "user_total_reading").text = str(len(entries))
    ET.SubElement(info, "user_total_completed").text = "0"
    ET.SubElement(info, "user_total_onhold").text = "0"
    ET.SubElement(info, "user_total_dropped").text = "0"
    ET.SubElement(info, "user_total_plantoread").text = "0"
    for e in entries:
        m = ET.SubElement(root, "manga")
        ET.SubElement(m, "series_mangadb_id").text = str(e["mal_id"])
        ET.SubElement(m, "series_title").text = e["mal_title"]
        ET.SubElement(m, "my_id").text = "0"
        ET.SubElement(m, "my_read_volumes").text = "0"
        ET.SubElement(m, "my_read_chapters").text = "0"
        ET.SubElement(m, "my_status").text = "Reading"
        ET.SubElement(m, "my_score").text = "0"
        ET.SubElement(m, "my_start_date").text = "0000-00-00"
        ET.SubElement(m, "my_finish_date").text = "0000-00-00"
        ET.SubElement(m, "update_on_import").text = "1"
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


async def main():
    with open(FAILED_FILE, encoding="utf-8") as f:
        titles = [line.strip() for line in f if line.strip()]
    print(f"Retrying {len(titles)} failed titles (no threshold filter)...\n")

    t0 = time.monotonic()
    results = await lookup_all(titles)
    elapsed = time.monotonic() - t0

    mapping_lines = []
    candidates = []
    no_results = []

    for title, (mal_id, mal_title, score) in zip(titles, results):
        if mal_id:
            candidates.append({"mal_id": mal_id, "mal_title": mal_title})
            flag = "???" if score < 0.4 else ("~OK" if score < 0.6 else "OK ")
            mapping_lines.append(
                f"[{flag} {score:.0%}]  {title!r:60s} → {mal_title!r} (id={mal_id})"
            )
        else:
            no_results.append(title)
            mapping_lines.append(f"[NONE]     {title!r:60s} → no results on MAL")

    print(f"\nDone in {elapsed:.1f}s")
    print(f"Has candidate: {len(candidates)} | No MAL results: {len(no_results)}")
    print("Flags: OK=≥60%  ~OK=40-59% (review!)  ???=<40% (likely wrong)")

    with open(RETRY_MAPPING, "w", encoding="utf-8") as f:
        f.write("\n".join(mapping_lines) + "\n")
    print(f"\nWrote {RETRY_MAPPING}")

    if candidates:
        xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + build_xml(candidates)
        with open(RETRY_XML, "w", encoding="utf-8") as f:
            f.write(xml_str)
        print(f"Wrote {RETRY_XML} — review retry_mapping.txt before importing!")
        print("WARNING: chapter/volume progress = 0 (update manually on AniList after import)")


if __name__ == "__main__":
    asyncio.run(main())
