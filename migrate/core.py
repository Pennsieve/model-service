import logging
import os
import os.path
import random
import shlex
import string
import subprocess
import time
from contextlib import contextmanager
from copy import copy
from typing import List, Optional, Set

from auth_middleware import JwtConfig, role
from auth_middleware.claim import Claim, ServiceClaim
from auth_middleware.models import DatasetPermission, RoleType

import generator
from core.clients import PennsieveApiClient
from core.config import Config
from loader import import_to_neo4j
from server.db import (
    Database,
    DatasetId,
    DatasetNodeId,
    OrganizationId,
    PartitionedDatabase,
    UserNodeId,
)
from server.logging import configure_logging
from server.models import DatasetDeletionCounts
from utils.ssh import SSHTunnel
from utils.ssm import SSMParameters

from . import db as migrate_db

# Generated by `make build-neptune-export`
NEPTUNE_EXPORT_VERSION = "4.12.0-SNAPSHOT"
NEPTUNE_EXPORT_JAR = os.path.abspath(
    os.path.join(
        "..",
        f"pennsieve-api/neptune-export/target/scala-2.12/neptune-export-assembly-{NEPTUNE_EXPORT_VERSION}.jar",
    )
)


log = logging.getLogger(__file__)


class PennsieveDatabase:
    def __init__(self, engine, factory):
        self.engine = engine
        self.factory = factory

    def get_organizations(self) -> List[int]:
        return migrate_db.get_organizations(self.engine)

    def get_dataset(self, organization_id: int, dataset_id: int) -> object:
        Dataset = migrate_db.get_dataset_table(organization_id)
        with migrate_db.session_scope(self.factory) as session:
            ds = session.query(Dataset).filter(Dataset.id == dataset_id).one_or_none()
            session.expunge_all()
            return ds

    def get_dataset_ids(self, organization_id: int) -> List[int]:
        Dataset = migrate_db.get_dataset_table(organization_id)
        with migrate_db.session_scope(self.factory) as session:
            return [
                dataset.id
                for dataset in session.query(Dataset)
                .filter(Dataset.state != "DELETING")
                .order_by(Dataset.id)
                .all()
            ]

    def lock_dataset(self, organization_id: int, dataset_id: int):
        raise Exception("locked property no longer support on datasets")
        # Dataset = migrate_db.get_dataset_table(organization_id)
        # with migrate_db.session_scope(self.factory) as session:
        #     dataset = (
        #         session.query(Dataset)
        #         .filter(Dataset.state != "DELETING")
        #         .filter(Dataset.id == dataset_id)
        #         .one()
        #     )
        #     dataset.locked = True

    def unlock_dataset(self, organization_id: int, dataset_id: int):
        raise Exception("locked property no longer support on datasets")
        # Dataset = migrate_db.get_dataset_table(organization_id)
        # with migrate_db.session_scope(self.factory) as session:
        #     dataset = session.query(Dataset).get(dataset_id)
        #     dataset.locked = False


@contextmanager
def with_databases(settings, jumpbox: Optional[str] = "non-prod"):
    with SSHTunnel(
        remote_host=settings.postgres_host,
        remote_port=settings.postgres_port,
        local_port=7777,
        jumpbox=jumpbox,
    ) as postgres_tunnel, SSHTunnel(
        remote_host=settings.neo4j_host,
        remote_port=settings.neo4j_port,
        local_port=8888,
        jumpbox=jumpbox,
    ) as neo4j_tunnel:

        engine, factory = migrate_db.get_postgres(
            f"postgresql://{settings.postgres_user}:{settings.postgres_password}@{postgres_tunnel.host}:{postgres_tunnel.port}/{settings.postgres_db}"
        )

        yield Database(
            uri=f"bolt://{neo4j_tunnel.host}:{neo4j_tunnel.port}",
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            max_connection_lifetime=300,
        ), engine, factory


def generate_data(
    organization_id: int,
    input_file: str,
    environment: str,
    jumpbox: Optional[str] = "non-prod",
    dry_run: bool = True,
):
    assert input_file is not None and os.path.exists(input_file)
    log.info(f"Loading input: {input_file}")

    try:
        from pennsieve import Pennsieve
    except:  # noqa: E722
        raise ValueError("Run `pip install pennsieve` first")

    settings = SSMParameters(environment)
    bf = Pennsieve("Pennsieve_PROD")

    ds_name = f"Test Dataset GENERATED-{''.join([random.choice(string.ascii_uppercase) for _ in range(10)])}"
    ds = bf.create_dataset(ds_name, "Test dataset (generated)")
    log.info(f'Creating dataset "{ds_name}" ({ds.id}, {ds.int_id}')

    with with_databases(settings, jumpbox) as (neo4j_db, _):
        partitioned_db = PartitionedDatabase(
            neo4j_db,
            organization_id=OrganizationId(organization_id),
            dataset_id=DatasetId(ds.int_id),
            user_id=UserNodeId("dataset-generate-migration"),
            dataset_node_id=DatasetNodeId(ds.id),
        )
        generator.load(partitioned_db, input_file)


def delete_orphaned_datasets_impl(
    bf_database: PennsieveDatabase,
    db: Database,
    organization_id: int,
    dry_run: bool = True,
):
    def completely_delete(partitioned_db):
        cumulative_counts = DatasetDeletionCounts.empty()
        sequential_failures = 0

        while True:
            try:
                summary = partitioned_db.delete_dataset(batch_size=1000, duration=2000)
                if summary.done:
                    return summary.update_counts(cumulative_counts)
                else:
                    cumulative_counts = cumulative_counts.update(summary.counts)
                sequential_failures = 0
                time.sleep(0.5)
            except Exception as e:
                sequential_failures += 1
                log.warn("FAIL({sequential_failures}): {str(e)}")
                log.warn("WAITING 2s")
                time.sleep(2.0)
                if sequential_failures >= 5:
                    raise e

    model_service_dataset_ids = db.get_dataset_ids(OrganizationId(organization_id))
    api_dataset_ids = bf_database.get_dataset_ids(organization_id)
    orphaned_dataset_ids = set(model_service_dataset_ids) - set(api_dataset_ids)

    if dry_run:
        log.info(f"""{"*" * 20} DRY RUN {"*" * 20}""")
        log.info(f"Found {len(orphaned_dataset_ids)} orphaned dataset(s)")
        for dataset_id in orphaned_dataset_ids:
            ds = bf_database.get_dataset(organization_id, dataset_id)
            assert ds is None or ds.state == "DELETING"
            log.info(
                f"Deleting: organization={organization_id} / dataset={dataset_id} ({db.count_child_nodes(organization_id, dataset_id)}) => {ds}"
            )
    else:
        log.info(f"Found {len(orphaned_dataset_ids)} orphaned dataset(s)")
        for dataset_id in orphaned_dataset_ids:
            ds = bf_database.get_dataset(organization_id, dataset_id)
            assert ds is None or ds.state == "DELETING"
            partitioned_db = PartitionedDatabase(
                db,
                OrganizationId(organization_id),
                DatasetId(dataset_id),
                UserNodeId("dataset-delete-migration"),
            )
            log.info(
                f"Deleting: organization={organization_id} / dataset={dataset_id} ({db.count_child_nodes(organization_id, dataset_id)}) => {ds}"
            )
            summary = completely_delete(partitioned_db)
            log.info(str(summary))

    log.info("Done")


def delete_orphaned_datasets(
    organization_id: int,
    environment: str,
    jumpbox: Optional[str] = "non-prod",
    dry_run: bool = True,
):
    settings = SSMParameters(environment)

    with with_databases(settings, jumpbox) as (
        neo4j_db,
        postgres_db_engine,
        postgres_db_factory,
    ):
        bf_database = PennsieveDatabase(postgres_db_engine, postgres_db_factory)
        delete_orphaned_datasets_impl(bf_database, neo4j_db, organization_id, dry_run)


def delete_all_orphaned_datasets(
    environment: str,
    jumpbox: Optional[str] = "non-prod",
    dry_run: bool = True,
):
    settings = SSMParameters(environment)

    with with_databases(settings, jumpbox) as (
        neo4j_db,
        postgres_db_engine,
        postgres_db_factory,
    ):
        bf_database = PennsieveDatabase(postgres_db_engine, postgres_db_factory)
        for organization_id in bf_database.get_organizations():
            log.info(f"*** Organization: {organization_id} ***")
            delete_orphaned_datasets_impl(
                bf_database, neo4j_db, organization_id, dry_run
            )


def migrate_dataset(
    organization_id: int,
    dataset_ids: Optional[List[int]] = None,
    remove_existing: bool = False,
    environment: str = "dev",
    jumpbox: Optional[str] = "non-prod",
    smoke_test: bool = True,
    remap_ids: bool = False,
):  # TODO does this need node IDs?

    if dataset_ids is None and remove_existing:
        raise Exception(
            f"Cannot remove existing data from Neo4j while migrating the entire organization {organization_id}"
        )
    elif dataset_ids is None and remap_ids:
        raise Exception(f"Can only remap IDs for a single dataset")
    elif dataset_ids is None:
        entire_organization = True
    else:
        entire_organization = False

    settings = SSMParameters(environment)

    with SSHTunnel(
        remote_host=settings.postgres_host,
        remote_port=settings.postgres_port,
        local_port=7777,
        jumpbox=jumpbox,
    ) as postgres_tunnel, SSHTunnel(
        remote_host=settings.neo4j_host,
        remote_port=settings.neo4j_port,
        local_port=8888,
        jumpbox=jumpbox,
    ) as neo4j_tunnel:

        engine, factory = migrate_db.get_postgres(
            f"postgresql://{settings.postgres_user}:{settings.postgres_password}@{postgres_tunnel.host}:{postgres_tunnel.port}/{settings.postgres_db}"
        )

        neo4j = Database(
            uri=f"bolt://{neo4j_tunnel.host}:{neo4j_tunnel.port}",
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            max_connection_lifetime=300,
        )

        bf_database = PennsieveDatabase(engine, factory, organization_id)

        # 1) Get the target datasets for the migration
        if dataset_ids is None:
            dataset_ids = bf_database.get_dataset_ids(organization_id)

        for dataset_id in dataset_ids:
            print(f"Migrating dataset {dataset_id}")

            partitioned_db = PartitionedDatabase(
                neo4j, organization_id=organization_id, dataset_id=dataset_id, user_id=0
            )

            # 3) Lock dataset in Pennsieve DB
            bf_database.lock_dataset(organization_id, dataset_id)
            print(f"Got dataset {dataset_id}")

            try:
                # 4) Export data to S3 from Neptune
                export_from_neptune(
                    settings,
                    postgres_tunnel=postgres_tunnel,
                    organization_id=organization_id,
                    dataset_id=dataset_id,
                    jumpbox=jumpbox,
                    smoke_test=smoke_test,
                )

                # 5) Import into Neo4j from S3
                import_to_neo4j.load(
                    dataset=f"{organization_id}/{dataset_id}",
                    bucket=settings.export_bucket,
                    db=partitioned_db,
                    cutover=True,
                    remove_existing=remove_existing,
                    smoke_test=smoke_test,
                    remap_ids=remap_ids,
                )

            finally:
                # Whatever happens, unlock the dataset
                bf_database.unlock_dataset(organization_id, dataset_id)

        # 6) Sanity check that all datasets in the organization have been
        # migrated, then mark the organization as migrated.
        if entire_organization:
            print("Validating migration....")

            for dataset_id in bf_database.get_dataset_ids(organization_id):
                partitioned_db = PartitionedDatabase(
                    neo4j,
                    organization_id=organization_id,
                    dataset_id=dataset_id,
                    user_id=0,
                )

            neo4j.toggle_service_for_organization(organization_id=organization_id)

        print("Done.")


def export_from_neptune(
    settings, postgres_tunnel, organization_id, dataset_id, jumpbox, smoke_test
):
    with SSHTunnel(
        remote_host=settings.neptune_host,
        remote_port=settings.neptune_port,
        local_port=9999,
        jumpbox=jumpbox,
    ) as neptune_tunnel, SSHTunnel(
        remote_host=settings.redis_host,
        remote_port=settings.redis_port,
        local_port=11111,
        jumpbox=jumpbox,
    ) as redis_tunnel:

        env = copy(os.environ)
        env.update(
            {
                "ORGANIZATION_ID": str(organization_id),
                "DATASET_ID": str(dataset_id),
                "S3_EXPORT_BUCKET": settings.export_bucket,
                "POSTGRES_HOST": postgres_tunnel.host,
                "POSTGRES_PORT": str(postgres_tunnel.port),
                "POSTGRES_USER": settings.postgres_user,
                "POSTGRES_PASSWORD": settings.postgres_password,
                "NEPTUNE_HOST": neptune_tunnel.host,
                "NEPTUNE_PORT": str(neptune_tunnel.port),
                "REDIS_HOST": redis_tunnel.host,
                "REDIS_PORT": str(redis_tunnel.port),
                "GENERATE_SAMPLES": str(smoke_test),
            }
        )

        run_command(shlex.split(f"java -jar {NEPTUNE_EXPORT_JAR}"), env=env)


def run_command(command, env=None):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, env=env)
    while True:
        output = process.stdout.readline()
        if not output and process.poll() is not None:
            break
        if output:
            print(output.strip())

    if process.poll() != 0:
        raise Exception("Error running subprocess", command)
