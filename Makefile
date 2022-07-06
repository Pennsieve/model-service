.PHONY:	clean install install-apoc start-neo4j-empty start-neo4j-ppmi load-ppmi run setup-indexes test test-container test-ci lint typecheck docs

LOG_LEVEL          ?= "INFO"
USE_CACHE          ?= "0"
NEO4J_APOC_VERSION ?= "3.5.0.13"
PIP_VERSION        := "22.0.3"
IMAGE_TAG          ?= "latest"
PORT               ?= 8080

QUOTED_NEXUS_USER := $(shell python3 -c "import urllib.parse; print(urllib.parse.quote('$(PENNSIEVE_NEXUS_USER)'))")
QUOTED_NEXUS_PW   := $(shell python3 -c "import urllib.parse; print(urllib.parse.quote('$(PENNSIEVE_NEXUS_PW)'))")

all: start-neo4j-empty

install-apoc:
	mkdir -p $(PWD)/plugins
	bin/install-neo4j-apoc.sh $(PWD)/plugins $(NEO4J_APOC_VERSION)

neo4j:
	docker-compose up -d neo4j

start-neo4j-empty: docker-clean install-apoc
	docker-compose --compatibility up neo4j

docker-clean:
	docker-compose down

clean: docker-clean
	[ -d conf/ ] && rm -rf conf/* || return 0
	rm -rf data/*
	rm -f plugins/*
	rm -f generator/output/*
	$(MAKE) clean -C docs

# There is a bug with Pip 19.2.0 with "@" in the Nexus index-url
install:
	pip install pip==$(PIP_VERSION)
	pip install --upgrade --extra-index-url "https://$(QUOTED_NEXUS_USER):$(QUOTED_NEXUS_PW)@nexus.pennsieve.cc:443/repository/pypi-prod/simple" --pre -r requirements.txt -r requirements-dev.txt

setup-indexes:
	python -m server.db.index

# Third-party test code is using a deprecated feature of requests resulting in thousands of warnings polluting logs
test: typecheck format
	pytest -s -v tests

jwt:
	python bin/generate_jwt.py

format:
	isort -rc .
	black .

typecheck:
	@mypy -p server -p publish

lint:
	@isort -rc --check-only .
	@black --check .

docs:
	 $(MAKE) -C docs

test-container:
	@IMAGE_TAG=$(IMAGE_TAG) docker-compose build model-service-test

test-ci: install-apoc
	docker-compose down --remove-orphans
	mkdir -p data plugins conf logs
	chmod -R 777 conf
	@IMAGE_TAG=$(IMAGE_TAG) docker-compose up --exit-code-from=model-service-test model-service-test

lint-ci:
	docker-compose down --remove-orphans
	@IMAGE_TAG=$(IMAGE_TAG) docker-compose up --exit-code-from=model-service-lint model-service-lint

containers:
	@IMAGE_TAG=$(IMAGE_TAG) docker-compose build model-service model-publish

run:
	python main.py --port=$(PORT)

run-container: containers
	docker run -it -e ENVIRONMENT=dev -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN -p "$(PORT):$(PORT)" pennsieve/model-service:latest
