# gateway-api-ui — dev & build helpers.
# The frontend is build-free (handcrafted CSS + vendored Alpine/Lucide), so there
# is no Node/Tailwind step: just run the FastAPI app.

IMAGE ?= gateway-api-ui:dev
PORT  ?= 8000

# Optional runtime config, passed through to the app. Examples:
#   make dev WRITE_ENABLED=true
#   make dev PROMETHEUS_URL=http://localhost:9090
#   make dev-write
WRITE_ENABLED  ?=
PROMETHEUS_URL ?=
RUN_ENV = WRITE_ENABLED=$(WRITE_ENABLED) PROMETHEUS_URL=$(PROMETHEUS_URL)

.PHONY: install dev dev-write run docker deploy undeploy

install:           ## install python deps into the current environment
	pip install -r code/requirements.txt

dev:               ## run locally with autoreload (pass WRITE_ENABLED / PROMETHEUS_URL to enable)
	cd code && $(RUN_ENV) uvicorn app:app --reload --host 0.0.0.0 --port $(PORT)

dev-write:         ## run locally with write mode (create/edit/delete) enabled
	cd code && WRITE_ENABLED=true PROMETHEUS_URL=$(PROMETHEUS_URL) uvicorn app:app --reload --host 0.0.0.0 --port $(PORT)

run:               ## run locally (no reload)
	cd code && $(RUN_ENV) uvicorn app:app --host 0.0.0.0 --port $(PORT)

docker:            ## build the container image
	docker build -t $(IMAGE) .

deploy:            ## apply read-only RBAC + Deployment + Service to the cluster
	kubectl apply -f deploy/

undeploy:
	kubectl delete -f deploy/ --ignore-not-found
