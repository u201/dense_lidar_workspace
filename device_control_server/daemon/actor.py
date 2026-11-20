"""预约实例执行器（单线程、顺序执行）。

每秒重扫 pending 实例，按 (scheduled_at, created_at) 排序：
- 到点且未过期：依次调用 action.service.execute_instance 执行；
- 超过 scheduled_at 且已过 expire_after_seconds：标记 expired，不执行。

运行方式（在 device_control_server 目录下）：
    ..\\venv\\Scripts\\python.exe -m daemon.actor
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 允许从任意目录启动：daemon 的上一级即 Django 项目根。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from action import service  # noqa: E402

logger = logging.getLogger('actor')


class Actor:
    def __init__(self, poll_interval: float = 1.0) -> None:
        self.instance_dir = Path(settings.ACTION_INSTANCE_DIR)
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        self.instance_dir.mkdir(parents=True, exist_ok=True)
        self._recover_running()
        logger.info('actor started, instance_dir=%s', self.instance_dir)

        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # 单次异常不能终止常驻进程
                logger.exception('actor tick failed')
            self._stop.wait(self.poll_interval)

        logger.info('actor stopped')

    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        entries: list[tuple[tuple[datetime, datetime], dict[str, Any]]] = []
        for instance in service.iter_instances('pending'):
            try:
                key = (service.parse_utc(instance['scheduled_at']),
                       service.parse_utc(instance['created_at']))
            except (KeyError, TypeError, ValueError):
                logger.warning('skip malformed instance: %s',
                               instance.get('instance_id'))
                continue
            entries.append((key, instance))

        entries.sort(key=lambda entry: entry[0])
        for (scheduled_at, _), instance in entries:
            expire_after = instance.get('expire_after_seconds',
                                        service.EXPIRE_AFTER_SECONDS)
            if now - scheduled_at > timedelta(seconds=expire_after):
                self._expire(instance, now, expire_after)
            elif scheduled_at <= now:
                self._execute(instance)

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

    def _execute(self, instance: dict[str, Any]) -> None:
        # 先写 running 占位；异常重启时可据此识别“执行结果未知”。
        instance['status'] = 'running'
        instance['started_at'] = service.iso_utc(datetime.now(timezone.utc))
        service.save_instance(instance)
        logger.info('executing %s (%s)',
                    instance['instance_id'], instance['action_id'])

        run = service.execute_instance(instance)
        instance['run'] = run
        instance['status'] = run['status']
        instance['finished_at'] = run['finished_at']
        service.save_instance(instance)

        if run['status'] == 'succeeded':
            logger.info('succeeded %s', instance['instance_id'])
        else:
            logger.error('failed %s: %s', instance['instance_id'],
                         run.get('error') or run['steps'][-1].get('error'))

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
