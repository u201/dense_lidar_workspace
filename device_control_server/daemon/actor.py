"""预约实例执行器（单线程、增量执行）。

每秒重扫 pending 实例：
- 空闲：把过期实例标记 expired；同一 tick 的多个到点实例只执行创建时间
  最新者，其余标记 superseded；
- 执行中：每个 tick 最多推进一步；delay 只记录到期时间，不阻塞主循环。

运行方式（在 device_control_server 目录下）：
    ..\\venv\\Scripts\\python.exe -m daemon.actor
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 允许从任意目录启动：daemon 的上一级即 Django 项目根。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from action import service  # noqa: E402

logger = logging.getLogger('actor')


class Actor:
    def __init__(self, poll_interval: float = 1.0) -> None:
        config = service.load_action_config()
        self.instance_dir = config['instance_dir']
        self.http_port = config['http_port']
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        # 执行上下文：None 表示空闲，否则每 tick 最多推进一步。
        self.current: dict[str, Any] | None = None
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        self.instance_dir.mkdir(parents=True, exist_ok=True)
        self._start_http()
        self._recover_running()
        logger.info('actor started, instance_dir=%s', self.instance_dir)

        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # 单次异常不能终止常驻进程
                logger.exception('actor tick failed')
            self._stop.wait(self.poll_interval)

        self._stop_http()
        logger.info('actor stopped')

    def tick(self) -> None:
        if self.current is not None:
            self._advance()
            return

        now = datetime.now(timezone.utc)
        due: list[dict[str, Any]] = []
        for instance in service.iter_instances('pending'):
            try:
                scheduled_at = service.parse_utc(instance['scheduled_at'])
                service.parse_utc(instance['created_at'])
            except (KeyError, TypeError, ValueError):
                logger.warning('skip malformed instance: %s',
                               instance.get('instance_id'))
                continue

            expire_after = instance.get('expire_after_seconds',
                                        service.get_expire_after_seconds())
            if now - scheduled_at > timedelta(seconds=expire_after):
                self._expire(instance, now, expire_after)
            elif scheduled_at <= now:
                due.append(instance)

        if not due:
            return

        winner = service.select_due_winner(due)
        for instance in due:
            if instance['instance_id'] != winner['instance_id']:
                service.mark_superseded(instance, winner)
        self._start(winner, now)
        if self.current is not None:
            self._advance()

    def _expire(self, instance: dict[str, Any], now: datetime,
                expire_after: int) -> None:
        instance['status'] = 'expired'
        instance['finished_at'] = service.iso_utc(now)
        instance['error'] = {
            'code': 'expired',
            'message': f'超过预约时间 {expire_after} 秒未执行',
        }
        service.save_instance(instance)
        logger.warning('expired %s (scheduled_at=%s)',
                       instance['instance_id'], instance['scheduled_at'])

    def _start(self, instance: dict[str, Any], now: datetime) -> None:
        # 选中与真正开始之间可能被 web 取消，重读确认仍是 pending。
        fresh = service.load_instance(instance['instance_id'])
        if fresh is None or fresh.get('status') != 'pending':
            logger.warning('skip %s: no longer pending',
                           instance.get('instance_id'))
            return

        # 先写 running 占位；异常重启时可据此识别“执行结果未知”。
        fresh['status'] = 'running'
        fresh['started_at'] = service.iso_utc(now)
        service.save_instance(fresh)
        self.current = {
            'instance': fresh,
            'run': service.new_run(fresh['action_id']),
            'next_index': 0,
            'not_before': now,
        }
        logger.info('executing %s (%s)',
                    fresh['instance_id'], fresh['action_id'])

    def _advance(self) -> None:
        """推进一步；delay 未到期时直接返回，不占用主循环。"""
        current = self.current
        now = datetime.now(timezone.utc)
        if now < current['not_before']:
            return

        steps = current['instance'].get('steps', [])
        if current['next_index'] >= len(steps):
            self._finish('succeeded')
            return

        step = steps[current['next_index']]
        instance = current['instance']
        execution = {
            'kind': instance.get('kind', 'scheduled'),
            'instance_id': instance['instance_id'],
            'title': instance.get('title') or instance['action_id'],
            'scheduled_at': instance.get('scheduled_at'),
            'steps': steps,
        }
        step_run = service.execute_step(step, current['run'],
                                        instance['action_id'],
                                        execution=execution)
        current['run']['steps'].append(step_run)
        if step_run['status'] == 'failed':
            self._finish('failed')
            return

        current['next_index'] += 1
        delay = step.get('delay', 0) or 0
        if delay > 0:
            current['not_before'] = now + timedelta(seconds=delay)

    def _finish(self, status: str) -> None:
        instance = self.current['instance']
        run = self.current['run']
        run['status'] = status
        run['finished_at'] = datetime.now(timezone.utc).isoformat()
        instance['run'] = run
        instance['status'] = status
        instance['finished_at'] = run['finished_at']
        service.save_instance(instance)
        self.current = None

        if status == 'succeeded':
            logger.info('succeeded %s', instance['instance_id'])
        else:
            last_error = (run['steps'][-1].get('error')
                          if run['steps'] else None)
            logger.error('failed %s: %s', instance['instance_id'],
                         run.get('error') or last_error)

    def _recover_running(self) -> None:
        """上次进程在 executing 中途退出：结果未知，标记 failed 不重试。"""
        now = service.iso_utc(datetime.now(timezone.utc))
        for instance in service.iter_instances('running'):
            instance['status'] = 'failed'
            instance['finished_at'] = now
            instance['error'] = {
                'code': 'actor_restarted',
                'message': 'actor 重启，上次执行结果未知',
            }
            service.save_instance(instance)
            logger.warning('recovered running instance %s as failed',
                           instance['instance_id'])

    def snapshot(self) -> dict[str, Any]:
        """当前执行状态快照。

        HTTP 线程直接读取，不加锁：引用赋值是原子的，仅接受
        _advance 原地修改带来的极小概率中间态。
        """
        current = self.current
        if current is None:
            return {'running': False}
        instance = current['instance']
        return {
            'running': True,
            'instance_id': instance['instance_id'],
            'action_id': instance['action_id'],
            'status': instance['status'],
            'next_index': current['next_index'],
            'total_steps': len(instance.get('steps', [])),
            'not_before': current['not_before'].isoformat(),
            'run': current['run'],
        }

    def _start_http(self) -> None:
        handler = type('StatusHandler', (_StatusHandler,), {'actor': self})
        try:
            self._http_server = ThreadingHTTPServer(
                ('127.0.0.1', self.http_port), handler)
        except OSError:
            # 状态接口是附加能力，端口被占不应拖垮执行器。
            logger.exception('http status disabled: cannot bind port %s',
                             self.http_port)
            return
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name='actor-http', daemon=True)
        self._http_thread.start()
        logger.info('http status on http://127.0.0.1:%s/current',
                    self.http_port)

    def _stop_http(self) -> None:
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)
            self._http_thread = None


class _StatusHandler(BaseHTTPRequestHandler):
    """GET /current 返回 actor.snapshot()；实例由 _start_http 注入。"""

    actor: Actor

    def do_GET(self) -> None:
        if self.path != '/current':
            self.send_error(404)
            return
        body = json.dumps(self.actor.snapshot(), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug('http: ' + format, *args)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    actor = Actor()

    def handle_signal(signum: int, _frame: Any) -> None:
        logger.info('signal %s received, stopping', signum)
        actor.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    actor.run_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
