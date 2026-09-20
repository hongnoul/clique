"""Node agent daemon (MVP implementation).

Registers with the server (discovered via mDNS or --server URL), opens
the agent WebSocket, heartbeats, and executes assigned tasks on the
local model runtime. One task at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import httpx
import websockets

from common import protocol
from common.config import Config, generate_or_load_keypair, load
from common.types import NodeStatus, TaskAssignment, TaskRequest, TaskResult, TaskState
from node import resources as res
from node.model_runtime import BaseRuntime, build_runtime, spec_from_config

log = logging.getLogger("clique.agent")


class NodeAgent:
    def __init__(self, config: Config, server_url: str) -> None:
        self.config = config
        self.server_url = server_url.rstrip("/")
        self.ws_url = self.server_url.replace("http", "ws", 1) + "/ws/agent"
        self.runtime: BaseRuntime = build_runtime(
            config.node.model_runtime,
            config.node.openai_base_url,
            config.node.openai_model_name,
        )
        self.model = spec_from_config(config.node)
        self.node_id = ""
        self.token = ""
        self.current_task_id: str | None = None  # legacy: one of the running ids
        self.running: dict[str, asyncio.Task] = {}  # task_id -> job
        self.current_task_id: str | None = None
        self.workspace_seq: int = 0  # latest live workspace seq seen
        self.workspace_paths: list[str] = []  # paths touched since task start
        self._stop = asyncio.Event()
        self._task_job: asyncio.Task | None = None
        self._ws = None

    # -------------------------------------------------------------- lifecycle

    async def register(self) -> None:
        pub, seed = generate_or_load_keypair(self.config.node.data_dir)
        name = self.config.node.display_name
        body = {
            "display_name": name,
            "public_key": pub,
            "signature": protocol.sign_payload(name.encode(), seed),
            "model": self.model.model_dump(mode="json"),
            "resources": res.probe().model_dump(mode="json"),
            "role": "client",
        }
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{self.server_url}/v1/register", json=body)
            r.raise_for_status()
            data = r.json()
        self.node_id = data["node_id"]
        self.token = data["token"]
        log.info("registered as %s (%s, op=%s)", name, self.node_id, data["op_level"])

    async def run(self) -> None:
        if not await self.runtime.health():
            raise SystemExit(
                f"model runtime '{self.config.node.model_runtime}' not healthy "
                f"({self.config.node.openai_base_url})")
        await self.register()
        backoff = 1.0
        disconnected_since: float | None = None
        giveup_after = self.config.node.reconnect_giveup_s
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                        f"{self.ws_url}?token={self.token}", max_size=None) as ws:
                    backoff = 1.0
                    disconnected_since = None
                    await self._session(ws)
            except (OSError, websockets.WebSocketException) as e:
                if self._stop.is_set():
                    return
                if disconnected_since is None:
                    disconnected_since = time.monotonic()
                down_for = time.monotonic() - disconnected_since
                if giveup_after and down_for > giveup_after:
                    log.warning("server unreachable for %.0fs (> %.0fs); giving up",
                               down_for, giveup_after)
                    return
                log.warning("connection lost (%s); retry in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                with contextlib.suppress(Exception):
                    await self.register()  # token may be gone if server restarted

    async def _session(self, ws) -> None:
        self._ws = ws
        hb = asyncio.create_task(self._heartbeat_loop(ws))
        try:
            async for raw in ws:
                msg = protocol.loads(raw)
                if msg["type"] == protocol.ASSIGN:
                    assignment = TaskAssignment.model_validate(msg["assignment"])
                    request = TaskRequest.model_validate(msg["request"])
                    slots = self.model.parallel_slots or 1
                    if len(self.running) >= slots:
                        await ws.send(protocol.dumps(protocol.msg_result(TaskResult(
                            task_id=assignment.task_id, attempt_id=assignment.attempt_id,
                            state=TaskState.FAILED, error="node busy (race)"))))
                        continue
                    job = asyncio.create_task(self._execute(ws, assignment, request))
                    self.running[assignment.task_id] = job
                    self.current_task_id = assignment.task_id
                    self._task_job = job
                elif msg["type"] == protocol.REVOKE:
                    job = self.running.get(msg["task_id"])
                    if job is not None:
                        await self.runtime.cancel()
                        job.cancel()
                elif msg["type"] == protocol.SHUTDOWN:
                    log.info("server is shutting down (%s); disconnecting",
                             msg.get("reason", ""))
                    await self.stop(drain=False, reason=msg.get("reason", "server shutdown"))
                elif msg["type"] == protocol.WS_INVALIDATE:
                    # live workspace moved under us: record latest seq so
                    # the executor can reread before its next chunk
                    self.workspace_seq = msg.get("seq", 0)
                    self.workspace_paths = msg.get("paths", [])
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb

    async def _execute(self, ws, assignment: TaskAssignment, request: TaskRequest) -> None:
        start = time.monotonic()
        state, error = TaskState.SUCCEEDED, None
        output = ""
        try:
            from node.executor import execute
            output = await execute(ws, self.runtime, assignment, request)
        except asyncio.CancelledError:
            # abrupt kill: report nothing; the server's lease/heartbeat
            # machinery will requeue this attempt elsewhere
            self.running.pop(assignment.task_id, None)
            self.current_task_id = next(iter(self.running), None)
            raise
        except Exception as e:
            state, error = TaskState.FAILED, str(e)
            log.warning("task %s failed: %s", assignment.task_id, e)
        finally:
            result = TaskResult(
                task_id=assignment.task_id, attempt_id=assignment.attempt_id,
                state=state, output=output if state == TaskState.SUCCEEDED else None,
                error=error, prompt_tokens=request.est_prompt_tokens(),
                output_tokens=max(1, len(output) // 4),
                wall_time_s=time.monotonic() - start)
            with contextlib.suppress(Exception):
                await ws.send(protocol.dumps(protocol.msg_result(result)))
            self.running.pop(assignment.task_id, None)
            self.current_task_id = next(iter(self.running), None)

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            slots = self.model.parallel_slots or 1
            status = NodeStatus.BUSY if len(self.running) >= slots else NodeStatus.READY
            await ws.send(protocol.dumps(protocol.msg_heartbeat(
                status.value, res.probe(), self.current_task_id, self.model)))
            await asyncio.sleep(self.config.node.heartbeat_interval_s)

    async def stop(self, drain: bool = True, reason: str = "operator disconnect") -> None:
        """Graceful shutdown: optionally finish the in-flight task, tell the
        server we're leaving (so it drops us and requeues our task
        immediately instead of waiting out the heartbeat timeout), then
        close the connection."""
        self._stop.set()
        if drain and self._task_job:
            with contextlib.suppress(Exception):
                await self._task_job
        else:
            await self.runtime.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(protocol.dumps(protocol.msg_leave(reason)))
            with contextlib.suppress(Exception):
                await self._ws.close()

    async def kill(self) -> None:
        """Abrupt death (crash simulation / immediate quit): no drain,
        no leave message, connection dropped mid-task."""
        self._stop.set()
        if self._task_job:
            self._task_job.cancel()
        await self.runtime.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()


async def resolve_server(explicit: str | None, timeout_s: float = 5.0) -> str:
    if explicit:
        return explicit if explicit.startswith("http") else f"http://{explicit}"
    from node.discovery import find_server
    ann = await find_server(timeout_s=timeout_s)
    if ann is None:
        raise SystemExit("no clique server found on this network"
                         " (start one with `clique-server` or pass --server)")
    return f"http://{ann.api_address}"


def main() -> None:
    import argparse
    import signal
    parser = argparse.ArgumentParser("clique-agent")
    parser.add_argument("--server", help="server URL (skip mDNS discovery)")
    parser.add_argument("--name", help="override display name")
    parser.add_argument("--runtime", choices=["echo", "openai-compat"])
    parser.add_argument("--model-name", help="openai-compat model name, e.g. qwen2.5-coder:7b")
    parser.add_argument("--param-b", type=float, help="model size in B params (routing)")
    parser.add_argument("--parallel-slots", type=int,
                        help="concurrent tasks the runtime can batch (vLLM: 8+)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    config = load()
    if args.name:
        config.node.display_name = args.name
    if args.runtime:
        config.node.model_runtime = args.runtime
    if args.model_name:
        config.node.openai_model_name = args.model_name
        config.node.model_family = args.model_name.split(":")[0]
    if args.param_b:
        config.node.model_parameter_b = args.param_b
    if args.parallel_slots:
        config.node.parallel_slots = args.parallel_slots

    async def run() -> None:
        server_url = await resolve_server(args.server)
        agent = NodeAgent(config, server_url)
        loop = asyncio.get_running_loop()
        disconnecting = False

        def on_signal() -> None:
            nonlocal disconnecting
            if disconnecting:
                log.warning("forcing immediate disconnect (dropping any in-flight task)")
                asyncio.ensure_future(agent.kill())
                return
            disconnecting = True
            log.info("disconnecting: finishing any in-flight task and leaving "
                     "the clique... (send the signal again to force quit)")
            asyncio.ensure_future(agent.stop())

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, on_signal)

        try:
            await agent.run()
        except KeyboardInterrupt:
            # only reached where add_signal_handler isn't available (Windows)
            await agent.stop()
        log.info("disconnected from %s", server_url)

    asyncio.run(run())


if __name__ == "__main__":
    main()
