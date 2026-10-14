'''
Python 3 running on Raspberry Pi 3B

Change how often a node reads its sensors, over the radio, without
re-flashing it:

    python3 set_interval.py 2 10     — node 2 reads its sensors every 10 s

radio_nodes.py sends it with its next command (so only to a node on the
radio, and awake); the node answers with set_interval_ack. The new interval
lasts until the node restarts, when it goes back to nodes.json's
sense_interval_s.
'''

import sys

from sync_indoor import request_set_interval, wait_for_interval_ack


def main(argv):
    if len(argv) != 3:
        sys.exit("Usage: python3 set_interval.py <node> <seconds>")
    try:
        node_id, seconds = int(argv[1]), int(argv[2])
    except ValueError:
        sys.exit("The node and the seconds must be whole numbers.")
    if seconds <= 0:
        sys.exit("The interval must be at least 1 second.")
    request_set_interval(seconds, node_id=node_id)
    wait_for_interval_ack()


if __name__ == "__main__":
    main(sys.argv)
