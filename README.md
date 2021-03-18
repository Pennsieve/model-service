# model-service

This project provides the model-service that interacts with the Neo4j database.

## Installing dependencies

To install all the required packages, run:

    $ make install

## Testing

Start a local Neo4j instance:

    $ docker-compose up -d neo4j

Then run the tests:

    $ make test

To skip integration tests that connect to the Pennsieve platform using the
Pennsieve Python client:

    $ pytest --skip-integration

## Types and Formatting

The CI build runs `mypy`, `black` and `isort` to typecheck the project and
detect any style issues.  Before submitting a PR, run the following to fix any
problems with code formatting:

    $ make format

To check that the `mypy` types line up, run

    $ make typecheck

Note: `make test` runs both `make format` and `make typecheck` under the hood.


## Logging

Logging levels can be toggled via the `LOG_LEVEL` environment variable. The
levels `DEBUG`, `INFO`, `WARN`, and, `ERROR` are supported.
