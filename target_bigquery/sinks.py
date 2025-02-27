"""BigQuery target sink class, which handles writing streams."""

from __future__ import annotations
from datetime import datetime, timedelta
from gzip import GzipFile
from gzip import open as gzip_open
from io import BufferedRandom
import json
from pathlib import Path
from tempfile import mkstemp
from typing import IO, BinaryIO, Dict, List, Optional, Sequence
import uuid

from singer_sdk.sinks import BatchSink
from singer_sdk.plugin_base import PluginBase
from singer_sdk.helpers._batch import (
    BaseBatchFileEncoding,
    BatchConfig,
    BatchFileFormat,
    StorageTarget,
)
from fastavro import writer, parse_schema
from google.cloud import bigquery

from google.cloud.bigquery import Dataset
from google.cloud.bigquery import DatasetReference
from google.cloud.exceptions import NotFound
from google.api_core import exceptions

from .bq import column_type, get_client
from .avro import avro_schema, fix_recursive_types_in_dict


class BigQuerySink(BatchSink):
    """BigQuery target sink class."""

    def __init__(
        self,
        target: PluginBase,
        stream_name: str,
        schema: Dict,
        key_properties: Optional[List[str]],
    ) -> None:
        super().__init__(target, stream_name, schema, key_properties)

        self.project_id = target.config["project_id"]
        self.location = target.config.get("location", None)
        self.dataset_id = target.config["dataset"]
        self.table_prefix = target.config.get("table_prefix", None)

        # pipelinewise-tap-postgres can send empty types, for example: sys_period = {}
        self.schema_properties = {
            name: jsontype
            for name, jsontype in self.schema["properties"].items()
            if "type" in jsontype
        }

        self.parsed_schema = parse_schema(
            avro_schema(self.stream_name, self.schema_properties, self.key_properties)
        )

        if not self.key_properties:
            raise Exception("Missing key_properties (e.g. primary key(s)) in schema!")

        self.client: bigquery.Client = get_client(self.project_id, self.location)
        self.once_jobs_done = False

    @property
    def max_size(self) -> int:
        """Get maximum batch size.

        Returns:
            Maximum batch size
        """
        return self.config.get("batch_size", 10000)

    @property
    def table_name(self) -> str:
        """Get table name.

        Returns:
            Table name for this sink
        """
        return f"{self.table_prefix or ''}{self.stream_name}"

    def temp_table_name(self, batch_id: str) -> str:
        """Get temporary table name.

        Returns:
            Temporary table name for this sink and batch
        """
        return f"{self.table_name}_{batch_id}"

    def create_table(self, table_name: str, expires: bool):
        """Creates the table in bigquery"""
        schema = [
            column_type(name, schema, nullable=name not in self.key_properties)
            for (name, schema) in self.schema_properties.items()
        ]

        table = bigquery.Table(
            f"{self.project_id}.{self.dataset_id}.{table_name}", schema
        )

        if expires:
            table.expires = datetime.now() + timedelta(days=1)

        self.client.create_table(table=table, exists_ok=True)



    def update_from_temp_table(self, batch_id: str) -> str:
        """Returns a MERGE query"""
        primary_keys = " AND ".join(
            [f"src.`{k}` = dst.`{k}`" for k in self.key_properties]
        )
        set_values = ", ".join(
            [f"`{key}` = src.`{key}`" for key in self.schema_properties.keys()]
        )
        cols = ", ".join([f"`{key}`" for key in self.schema_properties.keys()])

        return f"""MERGE `{self.dataset_id}`.`{self.table_name}` dst
            USING `{self.dataset_id}`.`{self.temp_table_name(batch_id)}` src
            ON {primary_keys}
            WHEN MATCHED THEN
                UPDATE SET {set_values}
            WHEN NOT MATCHED THEN
                INSERT ({cols}) VALUES ({cols})"""

    def drop_temp_table(self, batch_id: str) -> str:
        """Returns a DROP TABLE statement"""
        return f"DROP TABLE `{self.dataset_id}`.`{self.temp_table_name(batch_id)}`"

    def table_exists(self) -> bool:
        """Check if table already exists"""
        tables = self.query(
            [f"SELECT table_name FROM {self.dataset_id}.INFORMATION_SCHEMA.TABLES"]
        )
        for table in tables:
            table_name = table["table_name"]
            if table_name == self.table_name:
                return True
        return False


    def ensure_dataset(self, project_id, dataset_id, location):
        """
        Given a project id, dataset id and location, creates BigQuery dataset if not exists
        https://googleapis.dev/python/bigquery/latest/generated/google.cloud.bigquery.client.Client.html
        :param project_id, str: GCP project id from target config file. Passed to bigquery.Client().
        :param dataset_id, str: BigQuery dataset id from target config file.
        :param location, str: location for the dataset (US). Passed to bigquery.Client().
        :return: client (BigQuery Client Object) and Dataset (BigQuery dataset)
        """

        client = bigquery.Client(project=project_id, location=location)

        dataset_ref = DatasetReference(project_id, dataset_id)

        try:
            dataset = client.get_dataset(dataset_ref)
            return client, dataset

        except NotFound:
            try:
                client.create_dataset(dataset_ref)
            except exceptions.GoogleAPICallError as e:
                self.logger.critical(f"unable to create dataset {dataset_id} in project {project_id}; Exception {e}")
                return 2  # sys.exit(2)

            return client, Dataset(dataset_ref)

    def ensure_columns_exist(self):
        """Ensures all columns present in the Singer Schema are also present in Big Query"""
        table_ref = self.client.dataset(self.dataset_id).table(self.table_name)
        table = self.client.get_table(table_ref)

        # Column names are case insensitive so lowercase them
        table_columns = [field.name.lower() for field in table.schema]

        columns_to_add = [
            column_type(col, coltype, nullable=True)
            for col, coltype in self.schema_properties.items()
            if col.lower() not in table_columns
        ]

        if columns_to_add:
            new_schema = table.schema.copy()
            for new_col in columns_to_add:
                new_schema.append(new_col)
                self.logger.info(f"[{self.stream_name}] Adding column {new_col.name}")

            table.schema = new_schema
            self.client.update_table(table, ["schema"])

    def query(self, queries: List[str]) -> bigquery.table.RowIterator:
        """Executes the queries and returns when they have completed"""
        job_config = bigquery.QueryJobConfig()
        job_config.query_parameters = []

        joined_queries = ";\n".join(queries)

        query_job = self.client.query(joined_queries, job_config=job_config)

        # Starts job and waits for result
        return query_job.result()

    def load_table_from_file(
        self, filehandle: BinaryIO, batch_id: str
    ) -> bigquery.LoadJob:
        """Starts a load job for the given file."""
        self.ensure_dataset(self.project_id, self.dataset_id, self.location)
        self.create_table(self.temp_table_name(batch_id), expires=True)

        dataset_ref = self.client.dataset(self.dataset_id)
        table_ref = dataset_ref.table(self.temp_table_name(batch_id))

        job_config = bigquery.LoadJobConfig()
        job_config.source_format = bigquery.SourceFormat.AVRO
        job_config.use_avro_logical_types = True
        job_config.write_disposition = "WRITE_TRUNCATE"

        job = self.client.load_table_from_file(
            filehandle, table_ref, job_config=job_config
        )

        return job

    def start_batch(self, context: dict) -> None:
        """Start a new batch with the given context.

        The SDK-generated context will contain `batch_id` (GUID string) and
        `batch_start_time` (datetime).

        Developers may optionally override this method to add custom markers to the
        `context` dict and/or to initialize batch resources - such as initializing a
        local temp file to hold batch records before uploading.

        Args:
            context: Stream partition or context dictionary.
        """
        batch_id = context["batch_id"]
        self.logger.info(f"[{self.stream_name}][{batch_id}] Starting batch")

    def process_batch(self, context: dict) -> None:
        """Write out any prepped records and return once fully written."""
        batch_id = context["batch_id"]

        def transform_record(record):
            if self.include_sdc_metadata_properties:
                self._add_sdc_metadata_to_record(
                    record, {}, context
                )
            else:
                self._remove_sdc_metadata_from_record(record)

            return fix_recursive_types_in_dict(record, self.schema_properties)

        self.logger.info(f"[{self.stream_name}][{batch_id}] Converting to avro...")

        avro_records = (
            transform_record(record)
            for record in context["records"]
        )
        _, temp_file = mkstemp()

        with open(temp_file, "wb") as tempfile:
            writer(tempfile, self.parsed_schema, avro_records)

        self.logger.info(f"[{self.stream_name}][{batch_id}] Uploading LoadJob...")
        self.logger.info("1")
        with open(temp_file, "r+b") as tempfile:
            load_job = self.load_table_from_file(tempfile, batch_id)

        # Delete temp file once we are done with it
        Path(temp_file).unlink()
        self.logger.info("2")
        if not self.once_jobs_done:
            self.create_table(self.table_name, expires=False)
            self.ensure_columns_exist()
            self.once_jobs_done = True

        # Await job to finish
        load_job.result()
        self.logger.info("3")

        self.query(
            [
                self.update_from_temp_table(batch_id),
                self.drop_temp_table(batch_id),
            ]
        )

        self.logger.info(f"[{self.stream_name}][{batch_id}] Finished batch")

    def process_batch_files(
        self,
        encoding: BaseBatchFileEncoding,
        files: Sequence[str],
    ) -> None:
        """Overridden because Meltano 0.11.1 has a bad implementation see
        https://github.com/meltano/sdk/issues/1031
        """
        file: GzipFile | IO
        storage: StorageTarget | None = None

        for path in files:
            head, tail = StorageTarget.split_url(path)

            if self.batch_config:
                storage = self.batch_config.storage
            else:
                storage = StorageTarget.from_url(head)

            if encoding.format == BatchFileFormat.JSONL:
                with storage.fs(create=False) as batch_fs:
                    with batch_fs.open(tail, mode="rb") as file:
                        if encoding.compression == "gzip":
                            file = gzip_open(file)
                        context = {
                            "batch_id": str(uuid.uuid4()),
                            "batch_start_time": datetime.now(),
                        }
                        self.start_batch(context)
                        # Making this a generator instead - no need to store lines in memory for avro conversion
                        context["records"] = (json.loads(line) for line in file)
                        self.process_batch(context)
            else:
                raise NotImplementedError(
                    f"Unsupported batch encoding format: {encoding.format}"
                )
