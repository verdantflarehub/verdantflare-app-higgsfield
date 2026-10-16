import asyncio
from contextlib import contextmanager
from unittest.mock import patch
import unittest
from types import SimpleNamespace

from higgsfield_gateway import runtime


class FakeServer:
    def __init__(self, failure=False):
        self.started = False
        self.should_exit = False
        self.failure = failure
        self.entered = asyncio.Event()
        self.draining = asyncio.Event()
        self.finish = asyncio.Event()
        self.done = False

    async def serve(self, sockets):
        self.started = True
        self.entered.set()
        if self.failure:
            raise RuntimeError("private provider diagnostic")
        while not self.should_exit:
            await asyncio.sleep(0)
        self.draining.set()
        await self.finish.wait()
        self.done = True


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_drains_both_without_cancelling_active_checks(self):
        servers, stop = [FakeServer(), FakeServer()], asyncio.Event()
        task = asyncio.create_task(runtime.serve_pair(servers, [object(), object()], stop))
        await asyncio.gather(*(s.entered.wait() for s in servers))
        stop.set()
        await asyncio.gather(*(s.draining.wait() for s in servers))
        self.assertFalse(task.done())
        servers[0].finish.set()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        servers[1].finish.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(all(s.done for s in servers))

    async def test_listener_failure_stops_and_drains_peer_before_reporting(self):
        servers = [FakeServer(failure=True), FakeServer()]
        task = asyncio.create_task(runtime.serve_pair(servers, [object(), object()], asyncio.Event()))
        await servers[1].draining.wait()
        self.assertFalse(task.done())
        servers[1].finish.set()
        with self.assertRaisesRegex(RuntimeError, "^gateway_listener_failed$"):
            await asyncio.wait_for(task, 1)
        self.assertTrue(servers[1].done)

    async def test_coordinator_cancellation_still_drains_workers(self):
        servers = [FakeServer(), FakeServer()]
        task = asyncio.create_task(runtime.serve_pair(servers, [object(), object()], asyncio.Event()))
        await asyncio.gather(*(s.entered.wait() for s in servers))
        task.cancel()
        await asyncio.gather(*(s.draining.wait() for s in servers))
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        for server in servers:
            server.finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(all(s.done for s in servers))

    async def test_runtime_shares_exact_owner_and_closes_it_after_listener_drain(self):
        owner, events = SimpleNamespace(binding="fixture", organization="fixture-org"), []
        @contextmanager
        def own():
            events.append("lock")
            yield owner
            events.append("unlock")
        def execution(gateway, token):
            self.assertIs(gateway, owner)
            return "execution"
        def management(gateway, **kwargs):
            self.assertIs(gateway, owner)
            return "management"
        async def serve(servers, sockets, stop):
            self.assertEqual(events, ["lock"])
            self.assertEqual(len(servers), 2)
            events.append("drained")
        env = {"HIGGSFIELD_INTERNAL_TOKEN": "e" * 32, "HIGGSFIELD_MANAGEMENT_TOKEN": "m" * 32,
               "HIGGSFIELD_STATION_ID": "station-test"}
        with patch.dict(runtime.os.environ, env), patch.object(runtime, "DashboardAssets"), \
                patch.object(runtime, "gateway_owner", own), patch.object(runtime, "signals"), \
                patch.object(runtime.socket, "socket"), patch.object(runtime, "Server"), \
                patch.object(runtime, "VideoChannel"), \
                patch.object(runtime, "create_app", execution), patch.object(runtime, "create_management_app", management), \
                patch.object(runtime, "serve_pair", serve):
            await runtime.run()
        self.assertEqual(events, ["lock", "drained", "unlock"])


if __name__ == "__main__":
    unittest.main()
