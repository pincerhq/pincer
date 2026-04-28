PROFILE ?= stdio

COMPOSE := docker compose --profile $(PROFILE)

.PHONY: help env build up up-fg down destroy

.DEFAULT_GOAL := help

help:
	@echo "Usage: make <target> [PROFILE=<name>]"
	@echo ""
	@echo "Targets:"
	@echo "  setup             Copy .env.example to .env (skips if exists), copy pincer.toml to pincer.local.toml"
	@echo "  build            Build docker compose stack"
	@echo "  up-bg            Start docker compose stack (detached/background)"
	@echo "  up               Start docker compose stack (attached/foreground)"
	@echo "  down             Stop docker compose stack"
	@echo "  destroy          Stop stack and remove containers + volumes"
	@echo ""
	@echo "Options:"
	@echo "  PROFILE=<name>     Use docker compose profile <name> (default: stdio)"
	@echo "                   Available: stdio, http"

setup:
	@if [ -f .env ]; then \
		echo ".env already exists, skipping"; \
	else \
		cp .env.example .env; \
		echo "Created .env from .env.example"; \
	fi
	@if [ -f pincer.local.toml ]; then \
		echo "pincer.local.toml already exists, skipping"; \
	else \
		cp pincer.toml pincer.local.toml; \
		echo "Created pincer.local.toml from pincer.toml"; \
	fi

build:
	$(COMPOSE) build

build-nocache:
	$(COMPOSE) build --no-cache

up-bg:
	$(COMPOSE) up -d

up:
	$(COMPOSE) up

down:
	$(COMPOSE) down --remove-orphans

rm:
	$(COMPOSE) rm -f
