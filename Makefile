.PHONY: run tunnel dev install

run:
	.venv/bin/python server.py

tunnel:
	tailscale funnel 8002

dev:
	tmux new-session -d -s linkedin 'make run' \; split-window -h 'make tunnel' \; attach

install:
	python -m venv .venv
	.venv/bin/pip install -r requirements.txt
