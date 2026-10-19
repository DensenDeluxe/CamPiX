import asyncio
import logging
import threading

LOG = logging.getLogger(__name__)


class EventBus:
    """Bounded per-subscriber queues: slow consumers never block capture."""

    def __init__(self, db):
        self.db = db
        self.loop = None
        self.subscribers = set()
        self.listeners = set()
        self.lock = threading.Lock()

    def start(self):
        self.loop = asyncio.get_running_loop()

    def add_listener(self, callback):
        with self.lock:
            self.listeners.add(callback)

    def publish(self, kind, message, camera_id=None, data=None):
        event = self.db.event(kind, message, camera_id, data)
        with self.lock:
            listeners = tuple(self.listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                # A delivery listener must not break capture or the stored event.
                LOG.warning("Event listener failed for %s", kind)
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._broadcast, event)
        return event

    def _broadcast(self, event):
        for queue in tuple(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

    def subscribe(self):
        queue = asyncio.Queue(maxsize=128)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)
