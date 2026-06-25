# gateway-api-ui — dev & build helpers.
# The frontend is build-free (handcrafted CSS + vendored Alpine/Lucide), so there
# is no Node/Tailwind step: just run the FastAPI app.

IMAGE ?= gateway-api-ui:dev
PORT  ?= 8000

.PHONY: install dev run docker deploy undeploy

install:           ## install python deps into the current environment
	pip install -r code/requirements.txt

dev:               ## run locally with autoreload against your kubeconfig
	cd code && uvicorn app:app --reload --host 0.0.0.0 --port $(PORT)

run:               ## run locally (no reload)
	cd code && uvicorn app:app --host 0.0.0.0 --port $(PORT)

docker:            ## build the container image
	docker build -t $(IMAGE) .

deploy:            ## apply read-only RBAC + Deployment + Service to the cluster
	kubectl apply -f deploy/

undeploy:
	kubectl delete -f deploy/ --ignore-not-found
