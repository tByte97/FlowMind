from websockets.sync.client import connect
from websockets.sync.server import serve
import threading
import time

class Publisher:
    def __init__(self):
        self.clients = []
    def handler(self, websocket):
        print('handler start')
        self.clients.append(websocket)
        try:
            while True:
                try:
                    msg = websocket.recv(timeout=0.2)
                except TimeoutError:
                    continue
                print('recv', msg)
                if msg is None:
                    break
        finally:
            self.clients.remove(websocket)
            print('handler end')
    def publish(self, payload):
        print('publishing', payload)
        for ws in list(self.clients):
            ws.send(payload)

p = Publisher()
with serve(p.handler, '127.0.0.1', 8769):
    print('server started')
    with connect('ws://127.0.0.1:8769') as ws:
        print('client connected')
        p.publish('hello')
        print(ws.recv())
