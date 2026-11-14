"""Bounded live subscriptions for conversation events.

Decision events are hints; the card GET is authoritative. Clients must re-read
the card before showing or acting on a decision. After reconnecting, replay
plus a card re-read recovers missed events.
"""
import asyncio
import json
import queue
import threading
import time
from datetime import datetime, timezone

from fastapi.responses import StreamingResponse
from erpclaw_assistant import store


class Streams:
    def __init__(self, poll_interval=0.25):
        self.lock = threading.Lock()
        self.subscribers = {}
        self.poll_interval = poll_interval

    def subscribe(self, principal, conversation):
        with self.lock:
            if sum(1 for p, _, _ in self.subscribers.values() if p == principal) >= 3:
                return None
            key = object()
            self.subscribers[key] = (principal, conversation, queue.Queue(maxsize=100))
            return key

    def remove(self, key):
        with self.lock:
            self.subscribers.pop(key, None)

    def emit(self, conversation, approval_id, status):
        if not conversation:
            return
        message = {'type': 'decision', 'approval_id': approval_id, 'status': status,
                   'at': datetime.now(timezone.utc).isoformat()}
        with self.lock:
            for _, cid, inbox in self.subscribers.values():
                if cid == conversation:
                    try:
                        inbox.put_nowait(message)
                    except queue.Full:
                        pass

    def response(self, key, conversation, cursor, principal, authorized):
        inbox = self.subscribers[key][2]
        async def generate():
            nonlocal cursor
            working = False
            keepalive = 0.0
            try:
                while True:
                    if (not await asyncio.to_thread(authorized) or
                            await asyncio.to_thread(store.conversation_owner, conversation) != principal):
                        return
                    page = await asyncio.to_thread(store.history_page, conversation, cursor, 200)
                    for item in page['items']:
                        if not await asyncio.to_thread(authorized):
                            return
                        payload = item['event'] if item['kind'] == 'engine' else {
                            'type': 'message', 'text': item['text'], 'author': item['author']}
                        yield f"id: {item['seq']}\nevent: {payload['type']}\ndata: {json.dumps(payload)}\n\n"
                    cursor = page['next']
                    live = await asyncio.to_thread(store.live_turn, conversation)
                    if live and live['live'] and not working:
                        if not await asyncio.to_thread(authorized):
                            return
                        yield 'event: working\ndata: {"type":"working","text":"Working."}\n\n'
                        working = True
                    elif not live or not live['live']:
                        working = False
                    while True:
                        try:
                            message = inbox.get_nowait()
                        except queue.Empty:
                            break
                        if not await asyncio.to_thread(authorized):
                            return
                        yield f"event: decision\ndata: {json.dumps(message)}\n\n"
                    if len(page['items']) == 200:
                        continue
                    if time.monotonic() >= keepalive:
                        yield ': keepalive\n\n'
                        keepalive = time.monotonic() + 15
                    await asyncio.sleep(self.poll_interval)
            finally:
                self.remove(key)
        return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control': 'no-store'})
