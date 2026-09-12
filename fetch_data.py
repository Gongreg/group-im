#!/usr/bin/env python3
"""Fetch the OSRS quest list and WikiSync data for each player, write data.js.

Usage: python3 fetch_data.py   (then open osrs-quests.html)

Only stdlib is used. Player data comes from WikiSync, which only exists for
players running the WikiSync RuneLite plugin. Note the wiki asks third parties
not to build on that API; this is for personal use by the five of you.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PLAYERS = ["gaudyk", "uncle sacks", "Moms klit", "knysliukas", "cluescrollas"]

WIKI_LIST_URL = (
    "https://oldschool.runescape.wiki/api.php"
    "?action=parse&page=Quests/List&prop=text&formatversion=2&format=json"
)
SYNC_URL = "https://sync.runescape.wiki/runelite/player/{name}/STANDARD"
# The wiki blocks generic user agents; use a descriptive one.
USER_AGENT = "osrs-quest-tracker/1.0 (personal tool for a friend group)"


def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    print("Fetching quest list...")
    list_json = get_json(WIKI_LIST_URL)
    list_html = list_json["parse"]["text"]

    players = {}
    for name in PLAYERS:
        url = SYNC_URL.format(name=urllib.parse.quote(name))
        try:
            data = get_json(url)
            done = sum(1 for v in data.get("quests", {}).values() if v == 2)
            print(f"  {name}: {done} quests completed")
            players[name] = data
        except urllib.error.HTTPError as e:
            print(f"  {name}: HTTP {e.code} (no WikiSync data?)")
            players[name] = None
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: {e}")
            players[name] = None
        time.sleep(0.5)  # be polite

    payload = {
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "listHtml": list_html,
        "players": players,
    }
    # "</" inside a <script> would terminate the tag in HTML; escape it.
    js = "window.QUEST_DATA = " + json.dumps(payload).replace("</", "<\\/") + ";\n"
    with open("data.js", "w", encoding="utf-8") as f:
        f.write(js)
    print(f"Wrote data.js ({len(js) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
