'''
Python 3 running on Raspberry Pi 3B

Which link the Pi uses for a node that has both the radio and Wi-Fi
(nodes.json "link": ["wifi", "radio"], in order of preference).

The firmware answers on both, so the choice is the Pi's alone: each minute
the node is polled over the first link in its list that's up, and only that
one, so a reading never arrives (and is stored) twice. The radio loop
(main.py) and the Wi-Fi service (wifi_nodes.py) are separate programs, so
each writes a small status file the other reads:

  - Wi-Fi is up when garden-wifi has heard from the node in the last
    FRESH_S seconds (wifi_nodes.STATUS_FILE, "last_seen").
  - The radio is up when main.py still counts the node as reachable
    (RADIO_STATUS_FILE, from its NodeHealth), and main.py wrote that in the
    last FRESH_S seconds (so a stopped radio loop counts as down).

If none is up, the first link keeps trying. `python3 node_setup.py prefer
N wifi` reorders the list; both services pick that up within a minute, with
no restart (NodeList reloads nodes.json when it changes).

Written by Fletcher Meyers
October 2026
'''

import json
import os
import time
from datetime import datetime
from pathlib import Path

import nodes
from nodes import NodeConfigError, links_of, load_nodes

RADIO_STATUS_FILE = "/tmp/garden_radio_nodes.json"
WIFI_STATUS_FILE  = "/tmp/garden_wifi_nodes.json"   # written by wifi_nodes.py
FRESH_S = 180   # seconds: three missed polls and a link counts as down

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _now():
    return datetime.now()


def _age_s(stamp, now):
    try:
        return (now - datetime.strptime(stamp, ISO_FORMAT)).total_seconds()
    except (TypeError, ValueError):
        return None


def _read(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def wifi_up(node_id, now=None, status_file=WIFI_STATUS_FILE):
    entry = _read(status_file).get(str(node_id))
    if not isinstance(entry, dict):
        return False
    age = _age_s(entry.get("last_seen") or entry.get("since"), now or _now())
    return age is not None and age <= FRESH_S


def radio_up(node_id, now=None, status_file=RADIO_STATUS_FILE):
    status = _read(status_file)
    age = _age_s(status.get("_updated"), now or _now())
    if age is None or age > FRESH_S:
        return False
    return bool(status.get("nodes", {}).get(str(node_id), {}).get("reachable"))


def active_link(node_id, node, now=None, wifi_file=WIFI_STATUS_FILE, radio_file=RADIO_STATUS_FILE):
    '''The link to reach node_id over right now: the first in its list
    that's up, or the first if none is.'''
    links = links_of(node)
    if len(links) == 1:
        return links[0]
    up = {"wifi": lambda: wifi_up(node_id, now, wifi_file),
          "radio": lambda: radio_up(node_id, now, radio_file)}
    return next((link for link in links if up[link]()), links[0])


def write_radio_status(reachable, status_file=RADIO_STATUS_FILE):
    '''main.py: which radio nodes are answering ({node_id: bool}).'''
    status = {"_updated": _now().strftime(ISO_FORMAT),
              "nodes": {str(n): {"reachable": bool(ok)} for n, ok in reachable.items()}}
    try:
        tmp = Path(f"{status_file}.tmp")
        tmp.write_text(json.dumps(status))
        os.replace(tmp, status_file)
    except OSError as e:
        print(f"[LINKS] Could not write {status_file}: {e}")


class NodeList:
    '''nodes.json, read again whenever it changes (checked at most every
    check_s seconds), so a changed preference needs no restart. Keeps the
    last good list if the file can't be read.'''

    def __init__(self, path=None, check_s=5):   # None: the Pi's own list
        self.path, self.check_s = path, check_s
        self._nodes, self._mtime, self._checked = {}, None, -check_s

    def get(self):
        now = time.monotonic()
        if now - self._checked >= self.check_s:
            self._checked = now
            try:
                mtime = os.stat(self.path or nodes.NODES_FILE).st_mtime
            except OSError:
                mtime = None
            if mtime != self._mtime or not self._nodes:
                try:
                    self._nodes = load_nodes(self.path, skip_invalid=True)
                    self._mtime = mtime
                except NodeConfigError as e:
                    print(f"[LINKS] Could not read the node list: {e}")
        return self._nodes
