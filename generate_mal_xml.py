import asyncio
import difflib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup


HTML_FILE = "Reading List - MangaUpdates.html"
OUTPUT_XML = "manga_list.xml"
FAILED_FILE = "failed_titles.txt"
MAPPING_FILE = "mapping.txt"
JIKAN_URL = "https://api.jikan.moe/v4/manga"
CONCURRENCY = 5
SIMILARITY_THRESHOLD = 0.60


def parse_html(path):
    with open(path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    table = soup.find("div", class_=lambda c: c and "list_table" in c)
    rows = table.find_all("div", class_="row g-0")

    entries = []
    for row in rows:
        link = row.find("a", href=lambda h: h and "/series/" in h)
        if not link:
            continue
        title = link.get_text(strip=True)

        vol, chap = 1, 0
        for a in row.find_all("a", title="Increment Volume"):
            txt = a.get_text(strip=True)
            if txt.startswith("v."):
                try:
                    vol = int(txt[2:])
                except ValueError:
                    pass
            elif txt.startswith("c."):
                try:
                    chap = int(txt[2:])
                except ValueError:
                    pass

        entries.append({"title": title, "vol": vol, "chap": chap})

    return entries


def _similarity(a, b):
    a = a.lower().strip()
    b = b.lower().strip()
    return difflib.SequenceMatcher(None, a, b).ratio()


def _fetch(title):
    params = urllib.parse.urlencode({"q": title, "limit": 5})
    url = f"{JIKAN_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "anilist-import/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = data.get("data", [])
        if not results:
            return None, None, None, 0.0
        best = max(results, key=lambda r: _similarity(title, r.get("title", "")))
        score = _similarity(title, best["title"])
        return best["mal_id"], best["title"], None, score
    except urllib.error.HTTPError as e:
        return None, None, e.code, 0.0
    except Exception as e:
        return None, None, str(e), 0.0


async def jikan_lookup(semaphore, executor, loop, title):
    """Returns (accepted_id, accepted_title, score, candidate_id, candidate_title)"""
    async with semaphore:
        for attempt in range(4):
            mal_id, mal_title, err, score = await loop.run_in_executor(executor, _fetch, title)
            if err == 429:
                await asyncio.sleep(2 ** attempt)
                continue
            if mal_id and score >= SIMILARITY_THRESHOLD:
                return mal_id, mal_title, score, mal_id, mal_title
            return None, None, score, mal_id, mal_title
        return None, None, 0.0, None, None


async def lookup_all(entries):
    semaphore = asyncio.Semaphore(CONCURRENCY)
    loop = asyncio.get_event_loop()
    completed = 0
    total = len(entries)
    results = [None] * total

    async def tracked(i, entry):
        result = await jikan_lookup(semaphore, executor, loop, entry["title"])
        return (i,) + result

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = [asyncio.ensure_future(tracked(i, e)) for i, e in enumerate(entries)]
        for fut in asyncio.as_completed(futures):
            i, mal_id, mal_title, score, cand_id, cand_title = await fut
            completed += 1
            title = entries[i]["title"]
            if mal_id:
                status = f"→ {mal_id} ({mal_title[:30]}) [{score:.0%}]"
            else:
                status = f"→ NOT FOUND [{score:.0%}]"
            print(f"[{completed}/{total}] {title[:52]:<52}  {status}")
            results[i] = (mal_id, mal_title, score, cand_id, cand_title)

    return results


def build_xml(matched):
    root = ET.Element("myanimelist")

    info = ET.SubElement(root, "myinfo")
    ET.SubElement(info, "user_id").text = "1"
    ET.SubElement(info, "user_name").text = "user"
    ET.SubElement(info, "user_export_type").text = "2"
    ET.SubElement(info, "user_total_manga").text = str(len(matched))
    ET.SubElement(info, "user_total_reading").text = str(len(matched))
    ET.SubElement(info, "user_total_completed").text = "0"
    ET.SubElement(info, "user_total_onhold").text = "0"
    ET.SubElement(info, "user_total_dropped").text = "0"
    ET.SubElement(info, "user_total_plantoread").text = "0"

    for entry in matched:
        m = ET.SubElement(root, "manga")
        ET.SubElement(m, "series_mangadb_id").text = str(entry["mal_id"])
        ET.SubElement(m, "series_title").text = entry["mal_title"]
        ET.SubElement(m, "my_id").text = "0"
        ET.SubElement(m, "my_read_volumes").text = str(entry["vol"])
        ET.SubElement(m, "my_read_chapters").text = str(entry["chap"])
        ET.SubElement(m, "my_status").text = "Reading"
        ET.SubElement(m, "my_score").text = "0"
        ET.SubElement(m, "my_start_date").text = "0000-00-00"
        ET.SubElement(m, "my_finish_date").text = "0000-00-00"
        ET.SubElement(m, "update_on_import").text = "1"

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


async def main():
    print("Parsing HTML...")
    entries = parse_html(HTML_FILE)
    print(f"Found {len(entries)} entries\n")

    t0 = time.monotonic()
    lookup_results = await lookup_all(entries)
    elapsed = time.monotonic() - t0

    matched = []
    failed = []
    mapping_lines = []
    for entry, (mal_id, mal_title, score, cand_id, cand_title) in zip(entries, lookup_results):
        if mal_id:
            matched.append({**entry, "mal_id": mal_id, "mal_title": mal_title})
            mapping_lines.append(f"OK   [{score:.0%}]  {entry['title']!r:60s} → {mal_title!r} (id={mal_id})")
        else:
            failed.append(entry["title"])
            if cand_title:
                mapping_lines.append(f"FAIL [{score:.0%}]  {entry['title']!r:60s} → best candidate: {cand_title!r} (id={cand_id})")
            else:
                mapping_lines.append(f"FAIL [---]  {entry['title']!r:60s} → no results on MAL")

    print(f"\nDone in {elapsed:.1f}s — matched: {len(matched)}, failed: {len(failed)}")

    xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + build_xml(matched)
    with open(OUTPUT_XML, "w", encoding="utf-8") as f:
        f.write(xml_str)
    print(f"Wrote {OUTPUT_XML}")

    with open(MAPPING_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(mapping_lines) + "\n")
    print(f"Wrote {MAPPING_FILE}")

    if failed:
        with open(FAILED_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(failed) + "\n")
        print(f"Wrote {FAILED_FILE} ({len(failed)} titles)")


if __name__ == "__main__":
    asyncio.run(main())
