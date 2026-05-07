"""Bidirectional control plane between the master Pi and worker Pis.

The beat-sync layer (``audio/network.py``) is one-way master→worker. This
package adds the reverse direction:

* worker → master heartbeats with current animal/action and the lists of
  what's available, so the web UI can populate dropdowns,
* master → worker commands (``set_animal``, ``set_action``) sent unicast
  to the IP the master learned from the heartbeat.

UDP, JSON, plain stdlib — same wire style as ``audio/network.py``.
"""
