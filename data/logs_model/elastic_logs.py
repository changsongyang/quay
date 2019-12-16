import os
import logging
import re
from datetime import datetime, timedelta

from requests_aws4auth import AWS4Auth

from elasticsearch import RequestsHttpConnection
from elasticsearch.exceptions import NotFoundError, AuthorizationException
from elasticsearch_dsl import Index, Document, Integer, Date, Text, Ip, Keyword
from elasticsearch_dsl.connections import connections


logger = logging.getLogger(__name__)

# Name of the connection used for Elasticearch's template API
ELASTICSEARCH_TEMPLATE_CONNECTION_ALIAS = "logentry_template"

# Prefix of autogenerated indices
INDEX_NAME_PREFIX = "logentry_"

# Time-based index date format
INDEX_DATE_FORMAT = "%Y-%m-%d"

# Timeout for default connection
ELASTICSEARCH_DEFAULT_CONNECTION_TIMEOUT = 15

# Timeout for template api Connection
ELASTICSEARCH_TEMPLATE_CONNECTION_TIMEOUT = 60

# Force an index template update
ELASTICSEARCH_FORCE_INDEX_TEMPLATE_UPDATE = os.environ.get("FORCE_INDEX_TEMPLATE_UPDATE", "")

# Valid index prefix pattern
VALID_INDEX_PATTERN = r"^((?!\.$|\.\.$|[-_+])([^A-Z:\/*?\"<>|,# ]){1,255})$"


class LogEntry(Document):
    # random_id is the tie-breaker for sorting in pagination.
    # random_id is also used for deduplication of records when using a "at-least-once" delivery stream.
    # Reference: https://www.elastic.co/guide/en/elasticsearch/reference/current/search-request-search-after.html
    #
    # We use don't use the _id of a document since a `doc_values` is not build for this field:
    # An on-disk data structure that stores the same data in a columnar format
    # for optimized sorting and aggregations.
    # Reference: https://github.com/elastic/elasticsearch/issues/35369
    random_id = Text(fields={"keyword": Keyword()})
    kind_id = Integer()
    account_id = Integer()
    performer_id = Integer()
    repository_id = Integer()
    ip = Ip()
    metadata_json = Text()
    datetime = Date()

    _initialized = False

    @classmethod
    def init(cls, index_prefix, index_settings=None, skip_template_init=False):
        """
        Create the index template, and populate LogEntry's mapping and index settings.
        """
        wildcard_index = Index(name=index_prefix + "*")
        wildcard_index.settings(**(index_settings or {}))
        wildcard_index.document(cls)
        cls._index = wildcard_index
        cls._index_prefix = index_prefix

        if not skip_template_init:
            cls.create_or_update_template()

        # Since the elasticsearch-dsl API requires the document's index being defined as an inner class at the class level,
        # this function needs to be called first before being able to call `save`.
        cls._initialized = True

    @classmethod
    def create_or_update_template(cls):
        assert cls._index and cls._index_prefix
        index_template = cls._index.as_template(cls._index_prefix)
        index_template.save(using=ELASTICSEARCH_TEMPLATE_CONNECTION_ALIAS)

    def save(self, **kwargs):
        # We group the logs based on year, month and day as different indexes, so that
        # dropping those indexes based on retention range is easy.
        #
        # NOTE: This is only used if logging directly to Elasticsearch
        #       When using Kinesis or Kafka, the consumer of these streams
        #       will be responsible for the management of the indices' lifecycle.
        assert LogEntry._initialized
        kwargs["index"] = self.datetime.strftime(self._index_prefix + INDEX_DATE_FORMAT)
        return super(LogEntry, self).save(**kwargs)


class ElasticsearchLogs(object):
    """
    Model for logs operations stored in an Elasticsearch cluster.
    """

    def __init__(
        self,
        host=None,
        port=None,
        access_key=None,
        secret_key=None,
        aws_region=None,
        index_settings=None,
        use_ssl=True,
        index_prefix=INDEX_NAME_PREFIX,
    ):
        # For options in index_settings, refer to:
        # https://www.elastic.co/guide/en/elasticsearch/guide/master/_index_settings.html
        # some index settings are set at index creation time, and therefore, you should NOT
        # change those settings once the index is set.
        self._host = host
        self._port = port
        self._access_key = access_key
        self._secret_key = secret_key
        self._aws_region = aws_region
        self._index_prefix = index_prefix
        self._index_settings = index_settings
        self._use_ssl = use_ssl

        self._client = None
        self._initialized = False

    def _initialize(self):
        """
        Initialize a connection to an ES cluster and creates an index template if it does not exist.
        """
        if not self._initialized:
            http_auth = None
            if self._access_key and self._secret_key and self._aws_region:
                http_auth = AWS4Auth(self._access_key, self._secret_key, self._aws_region, "es")
            elif self._access_key and self._secret_key:
                http_auth = (self._access_key, self._secret_key)
            else:
                logger.warn("Connecting to Elasticsearch without HTTP auth")

            self._client = connections.create_connection(
                hosts=[{"host": self._host, "port": self._port}],
                http_auth=http_auth,
                use_ssl=self._use_ssl,
                verify_certs=True,
                connection_class=RequestsHttpConnection,
                timeout=ELASTICSEARCH_DEFAULT_CONNECTION_TIMEOUT,
            )

            # Create a second connection with a timeout of 60s vs 10s.
            # For some reason the PUT template API can take anywhere between
            # 10s and 30s on the test cluster.
            # This only needs to be done once to initialize the index template
            connections.create_connection(
                alias=ELASTICSEARCH_TEMPLATE_CONNECTION_ALIAS,
                hosts=[{"host": self._host, "port": self._port}],
                http_auth=http_auth,
                use_ssl=self._use_ssl,
                verify_certs=True,
                connection_class=RequestsHttpConnection,
                timeout=ELASTICSEARCH_TEMPLATE_CONNECTION_TIMEOUT,
            )

            try:
                force_template_update = ELASTICSEARCH_FORCE_INDEX_TEMPLATE_UPDATE.lower() == "true"
                self._client.indices.get_template(self._index_prefix)
                LogEntry.init(
                    self._index_prefix,
                    self._index_settings,
                    skip_template_init=not force_template_update,
                )
            except NotFoundError:
                LogEntry.init(self._index_prefix, self._index_settings, skip_template_init=False)
            finally:
                try:
                    connections.remove_connection(ELASTICSEARCH_TEMPLATE_CONNECTION_ALIAS)
                except KeyError as ke:
                    logger.exception(
                        "Elasticsearch connection not found to remove %s: %s",
                        ELASTICSEARCH_TEMPLATE_CONNECTION_ALIAS,
                        ke,
                    )

            self._initialized = True

    def index_name(self, day):
        """
        Return an index name for the given day.
        """
        return self._index_prefix + day.strftime(INDEX_DATE_FORMAT)

    def index_exists(self, index):
        try:
            return index in self._client.indices.get(index)
        except NotFoundError:
            return False

    @staticmethod
    def _valid_index_prefix(prefix):
        """
        Check that the given index prefix is valid with the set of indices used by this class.
        """
        return re.match(VALID_INDEX_PATTERN, prefix) is not None

    def _valid_index_name(self, index):
        """
        Check that the given index name is valid and follows the format:

        <index_prefix>YYYY-MM-DD
        """
        if not ElasticsearchLogs._valid_index_prefix(index):
            return False

        if not index.startswith(self._index_prefix) or len(index) > 255:
            return False

        index_dt_str = index.split(self._index_prefix, 1)[-1]
        try:
            datetime.strptime(index_dt_str, INDEX_DATE_FORMAT)
            return True
        except ValueError:
            logger.exception("Invalid date format (YYYY-MM-DD) for index: %s", index)
            return False

    def can_delete_index(self, index, cutoff_date):
        """
        Check if the given index can be deleted based on the given index's date and cutoff date.
        """
        assert self._valid_index_name(index)
        index_dt = datetime.strptime(index[len(self._index_prefix) :], INDEX_DATE_FORMAT)
        return index_dt < cutoff_date and cutoff_date - index_dt >= timedelta(days=1)

    def list_indices(self):
        self._initialize()
        try:
            return list(self._client.indices.get(self._index_prefix + "*").keys())
        except NotFoundError as nfe:
            logger.exception("`%s` indices not found: %s", self._index_prefix, nfe.info)
            return []
        except AuthorizationException as ae:
            logger.exception("Unauthorized for indices `%s`: %s", self._index_prefix, ae.info)
            return None

    def delete_index(self, index):
        self._initialize()
        assert self._valid_index_name(index)

        try:
            self._client.indices.delete(index)
            return index
        except NotFoundError as nfe:
            logger.exception("`%s` indices not found: %s", index, nfe.info)
            return None
        except AuthorizationException as ae:
            logger.exception("Unauthorized to delete index `%s`: %s", index, ae.info)
            return None


def configure_es(
    host,
    port,
    access_key=None,
    secret_key=None,
    aws_region=None,
    index_prefix=None,
    use_ssl=True,
    index_settings=None,
):
    """
    For options in index_settings, refer to:

    https://www.elastic.co/guide/en/elasticsearch/guide/master/_index_settings.html
    some index settings are set at index creation time, and therefore, you should NOT
    change those settings once the index is set.
    """
    es_client = ElasticsearchLogs(
        host=host,
        port=port,
        access_key=access_key,
        secret_key=secret_key,
        aws_region=aws_region,
        index_prefix=index_prefix or INDEX_NAME_PREFIX,
        use_ssl=use_ssl,
        index_settings=index_settings,
    )
    es_client._initialize()
    return es_client
