# node/ — agent that runs on every device

Daemon lifecycle (`agent.py`), LAN discovery (`discovery.py`), honest resource
probing (`resources.py`), model runtime adapter over llama-server/Ollama
(`model_runtime.py`), heartbeats (`heartbeat.py`), and single-task execution
(`executor.py`). See ../SPEC.md for the file map and library choices.
