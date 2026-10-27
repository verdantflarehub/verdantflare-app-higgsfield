"""Bind publication to the existing execution owner's server lifetime."""
import asyncio
import logging

from .discovery import Config, DiscoveryError, Publisher

logger = logging.getLogger(__name__)


def configured_publisher(env, store, version):
    endpoint = env.get('CHANNEL_DISCOVERY_ETCD_URL')
    scope = env.get('CHANNEL_DISCOVERY_SCOPE_ID')
    if not endpoint and not scope:
        return None
    if not endpoint or not scope:
        raise ValueError('incomplete_discovery_configuration')
    config = Config(endpoint, scope, env.get('ETCD_USERNAME', ''), env.get('ETCD_PASSWORD', ''))
    from .metadata import capabilities
    return Publisher(config, store, version, [{
        'capability_id': row['capability_id'], 'adapter_version': '1',
        'execution_state': row['execution_state']}
        for row in capabilities(env)['capabilities']])


class PublicationLifetime:
    def __init__(self, publisher, servers, set_accepting):
        self.publisher, self.servers, self.set_accepting = publisher, servers, set_accepting
        self.draining = asyncio.Event()
        self.finished = asyncio.Event()
        self.set_accepting(False)

    def begin_draining(self):
        # Called synchronously by the existing server signal handler: reject new
        # submissions immediately; already executing calls keep their identity.
        self.set_accepting(False)
        self.draining.set()

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self.draining.wait(), seconds)
        except TimeoutError:
            pass

    async def run(self, interval=60):
        try:
            while not all(server.started for server in self.servers):
                if self.finished.is_set() or self.draining.is_set():
                    return
                await self._wait(.1)
            await asyncio.to_thread(self.publisher.resume)
            while True:
                if self.draining.is_set():
                    try:
                        await asyncio.to_thread(self.publisher.drain)
                        await asyncio.to_thread(self.publisher.publish)
                    except DiscoveryError:
                        logger.warning('channel_discovery_drain_unpublished')
                    await self.finished.wait()
                    return
                try:
                    await asyncio.to_thread(self.publisher.publish)
                    state = await asyncio.to_thread(self.publisher.state)
                    self.set_accepting(not self.draining.is_set() and state['state'] in ('enabled', 'degraded'))
                except DiscoveryError:
                    self.set_accepting(False)
                    logger.warning('channel_discovery_unavailable')
                    if self.publisher.fenced:
                        return
                await self._wait(interval)
        finally:
            self.set_accepting(False)
            await asyncio.to_thread(self.publisher.close)

    async def finish(self, task):
        self.begin_draining()
        self.finished.set()
        # Repeated cancellation must not cancel a running to_thread registry
        # request or release the execution owner before its lease cleanup ends.
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError
