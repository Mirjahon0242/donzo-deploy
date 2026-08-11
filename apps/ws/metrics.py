"""
In-memory WebSocket metrics tracker.

Tracks active connections count and event timestamps for calculating
events-per-minute. Since Django Channels with InMemoryChannelLayer
does not expose built-in connection stats, this module provides a
lightweight alternative that works in a single-process deployment.

For multi-process / production deployments, replace this with
Redis-based counters.
"""

import time
from collections import deque
from threading import Lock


class WsMetrics:
    """
    Thread-safe in-memory metrics for WebSocket connections.
    """

    def __init__(self):
        self._lock = Lock()
        self._active_connections = 0
        # Keep last 5 minutes of event timestamps for EPM calculation
        self._event_timestamps: deque = deque(maxlen=10000)
        self._latest_event_type = ''
        self._latest_event_time = ''
        self._total_events = 0

    # ── Connection tracking ──

    def increment_connections(self):
        with self._lock:
            self._active_connections += 1

    def decrement_connections(self):
        with self._lock:
            if self._active_connections > 0:
                self._active_connections -= 1

    # ── Event tracking ──

    def record_event(self, event_type: str):
        now = time.time()
        with self._lock:
            self._event_timestamps.append(now)
            self._total_events += 1
            self._latest_event_type = event_type
            self._latest_event_time = now

    # ── Reset (used when sales stats are zeroed) ──

    def reset(self):
        with self._lock:
            self._active_connections = 0
            self._event_timestamps.clear()
            self._latest_event_type = ''
            self._latest_event_time = ''
            self._total_events = 0

    # ── Queries ──

    def get_active_connections(self) -> int:
        with self._lock:
            return self._active_connections

    def get_events_per_minute(self) -> float:
        """Calculate events per minute based on the last 60 seconds."""
        now = time.time()
        cutoff = now - 60
        with self._lock:
            # Prune old timestamps while we have the lock
            while self._event_timestamps and self._event_timestamps[0] < cutoff:
                self._event_timestamps.popleft()
            count_in_last_minute = len(self._event_timestamps)
        return round(count_in_last_minute, 1)

    def get_latest_event(self) -> dict:
        with self._lock:
            if not self._latest_event_time:
                return {'type': '', 'timestamp': ''}
            return {
                'type': self._latest_event_type,
                'timestamp': self._latest_event_time,
            }

    def get_total_events(self) -> int:
        with self._lock:
            return self._total_events

    def get_snapshot(self) -> dict:
        """Return all metrics as a dict (for the API response)."""
        latest = self.get_latest_event()
        return {
            'active_connections': self.get_active_connections(),
            'events_per_minute': self.get_events_per_minute(),
            'total_events': self.get_total_events(),
            'latest_event_type': latest['type'],
            'latest_event_timestamp': latest['timestamp'],
        }


# Global singleton — import this everywhere
metrics = WsMetrics()
