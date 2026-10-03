"""Bounded in-process metrics, correlated request logs and optional alerts."""
from collections import defaultdict
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import time
import uuid

import httpx

log = logging.getLogger(__name__)
request_id_var = ContextVar('request_id', default='')


class Observability:
    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()
        self._requests = defaultdict(lambda: [0, 0.0])
        self._operations = defaultdict(lambda: [0, 0.0])
        self._alerts = queue.Queue(maxsize=20)
        self._sent = {}
        self._thread = None
        self._stop = threading.Event()
        self.webhook = os.getenv('ALERT_WEBHOOK_URL', '')
        self.disk_threshold = int(os.getenv('MIN_FREE_DISK_BYTES', str(512 * 1024 * 1024)))

    def start(self):
        if not self.webhook:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._deliver, daemon=True, name='alert-webhook')
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _deliver(self):
        while not self._stop.is_set():
            try:
                payload = self._alerts.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with httpx.Client(timeout=5, follow_redirects=False) as client:
                    response = client.post(self.webhook, json=payload)
                    response.raise_for_status()
            except Exception:
                # The webhook URL may contain credentials; never log it or the exception URL.
                log.warning('alert delivery failed service=%s', self.service)
            finally:
                self._alerts.task_done()

    def alert(self, code, *, job_id=None):
        if not self.webhook:
            return
        now = time.monotonic()
        key = (code, job_id)
        with self._lock:
            if now - self._sent.get(key, -300) < 300:
                return
            if len(self._sent) >= 128:
                self._sent.clear()
            self._sent[key] = now
        try:
            self._alerts.put_nowait({'service': self.service, 'code': code, 'job_id': job_id})
        except queue.Full:
            log.warning('alert queue full service=%s', self.service)

    def record_operation(self, status, seconds):
        with self._lock:
            bucket = self._operations[status]
            bucket[0] += 1
            bucket[1] += seconds
        if status == 'error':
            self.alert('operation_failed')

    def disk(self, path):
        path = Path(path)
        while not path.exists():
            path = path.parent
        usage = shutil.disk_usage(path)
        low = usage.free < self.disk_threshold
        if low:
            self.alert('disk_space_low')
        return {'free_bytes': usage.free, 'total_bytes': usage.total, 'low_space': low}

    def attach(self, app):
        @app.middleware('http')
        async def correlate(request, call_next):
            incoming = request.headers.get('x-request-id', '')
            request_id = incoming if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', incoming) else uuid.uuid4().hex
            request.state.request_id = request_id
            token = request_id_var.set(request_id)
            started = time.monotonic()
            status = 500
            try:
                response = await call_next(request)
                status = response.status_code
                response.headers['X-Request-ID'] = request_id
                return response
            finally:
                elapsed = time.monotonic() - started
                request_id_var.reset(token)
                method = request.method if request.method in ('GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS') else 'OTHER'
                route = getattr(request.scope.get('route'), 'path', '__unmatched__')
                with self._lock:
                    values = self._requests[(method, route, status)]
                    values[0] += 1
                    values[1] += elapsed
                if status >= 500:
                    self.alert('http_server_error')
                log.info(json.dumps({'service': self.service, 'request_id': request_id,
                                     'method': request.method, 'route': route, 'status': status,
                                     'duration_seconds': round(elapsed, 4)}))

    def render(self, gauges=None):
        lines = []
        with self._lock:
            for (method, route, status), (count, duration) in sorted(self._requests.items()):
                labels = 'service=' + json.dumps(self.service) + ',method=' + json.dumps(method) + ',route=' + json.dumps(route) + ',status=' + json.dumps(str(status))
                lines += [f'finance_http_requests_total{{{labels}}} {count}',
                          f'finance_http_request_duration_seconds_sum{{{labels}}} {duration}',
                          f'finance_http_request_duration_seconds_count{{{labels}}} {count}']
            for status, (count, duration) in sorted(self._operations.items()):
                labels = 'service=' + json.dumps(self.service) + ',status=' + json.dumps(status)
                lines += [f'finance_operations_total{{{labels}}} {count}',
                          f'finance_operation_duration_seconds_sum{{{labels}}} {duration}',
                          f'finance_operation_duration_seconds_count{{{labels}}} {count}']
        for key, value in (gauges or {}).items():
            lines.append(f'finance_{key}{{service={json.dumps(self.service)}}} {float(value)}')
        return '\n'.join(lines) + '\n'
