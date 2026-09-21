# node/ — agent that runs on every device

Daemon lifecycle (`agent.py`), LAN discovery (`discovery.py`), honest resource
probing (`resources.py`), model runtime adapter over llama-server/Ollama
(`model_runtime.py`), heartbeats (`heartbeat.py`), single-task execution
(`executor.py`), and the agent tool loop (`tools.py`). See
../CODE_EDIT_PLAN.md for the file map and library choices.
