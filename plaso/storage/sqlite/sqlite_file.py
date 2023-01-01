# -*- coding: utf-8 -*-
"""SQLite-based storage file."""

import ast
import json
import os
import pathlib
import sqlite3
import zlib

from acstore import sqlite_store
from acstore.containers import interface as containers_interface

from plaso.containers import event_sources
from plaso.containers import events
from plaso.lib import definitions
from plaso.serializer import json_serializer


class SQLiteStorageFile(sqlite_store.SQLiteAttributeContainerStore):
  """SQLite-based storage file.

  Attributes:
    compression_format (str): compression format.
    serialization_format (str): serialization format.
  """

  _CONTAINER_TYPE_EVENT = events.EventObject.CONTAINER_TYPE
  _CONTAINER_TYPE_EVENT_DATA = events.EventData.CONTAINER_TYPE
  _CONTAINER_TYPE_EVENT_DATA_STREAM = events.EventDataStream.CONTAINER_TYPE
  _CONTAINER_TYPE_EVENT_SOURCE = event_sources.EventSource.CONTAINER_TYPE
  _CONTAINER_TYPE_EVENT_TAG = events.EventTag.CONTAINER_TYPE

  # Container types that are referenced from other container types.
  _REFERENCED_CONTAINER_TYPES = (
      _CONTAINER_TYPE_EVENT,
      _CONTAINER_TYPE_EVENT_DATA,
      _CONTAINER_TYPE_EVENT_DATA_STREAM,
      _CONTAINER_TYPE_EVENT_SOURCE)

  # Container types to not create a table for.
  _NO_CREATE_TABLE_CONTAINER_TYPES = (
      'analyzer_result',
      'hostname',
      'mount_point',
      'operating_system',
      'path',
      'source_configuration')

  def __init__(self):
    """Initializes a SQLite-based storage file."""
    super(SQLiteStorageFile, self).__init__()
    self._serializer = json_serializer.JSONAttributeContainerSerializer
    self._serializers_profiler = None
    self._storage_profiler = None

    self.compression_format = definitions.COMPRESSION_FORMAT_ZLIB
    self.serialization_format = definitions.SERIALIZER_FORMAT_JSON

  def _CheckStorageMetadata(self, metadata_values, check_readable_only=False):
    """Checks the storage metadata.

    Args:
      metadata_values (dict[str, str]): metadata values per key.
      check_readable_only (Optional[bool]): whether the store should only be
          checked to see if it can be read. If False, the store will be checked
          to see if it can be read and written to.

    Raises:
      IOError: if the format version or the serializer format is not supported.
      OSError: if the format version or the serializer format is not supported.
    """
    super(SQLiteStorageFile, self)._CheckStorageMetadata(
        metadata_values, check_readable_only=check_readable_only)

    compression_format = metadata_values.get('compression_format', None)
    if compression_format not in definitions.COMPRESSION_FORMATS:
      raise IOError('Unsupported compression format: {0!s}'.format(
          compression_format))

    serialization_format = metadata_values.get('serialization_format', None)
    if serialization_format != definitions.SERIALIZER_FORMAT_JSON:
      raise IOError('Unsupported serialization format: {0!s}'.format(
          serialization_format))

  def _CreatetAttributeContainerFromRow(
      self, container_type, column_names, row, first_column_index):
    """Creates an attribute container of a row in the database.

    Args:
      container_type (str): attribute container type.
      column_names (list[str]): names of the columns selected.
      row (sqlite.Row): row as a result from a SELECT query.
      first_column_index (int): index of the first column in row.

    Returns:
      AttributeContainer: attribute container.
    """
    schema = self._GetAttributeContainerSchema(container_type)
    if schema:
      container = self._containers_manager.CreateAttributeContainer(
          container_type)

      for column_index, name in enumerate(column_names):
        attribute_value = row[first_column_index + column_index]
        if attribute_value is None:
          continue

        data_type = schema[name]
        if data_type == 'AttributeContainerIdentifier':
          identifier = containers_interface.AttributeContainerIdentifier()
          identifier.CopyFromString(attribute_value)
          attribute_value = identifier

        elif data_type == 'bool':
          attribute_value = bool(attribute_value)

        elif data_type not in self._CONTAINER_SCHEMA_TO_SQLITE_TYPE_MAPPINGS:
          # TODO: add compression support
          attribute_value = self._serializer.ReadSerialized(attribute_value)

        setattr(container, name, attribute_value)

    else:
      if self.compression_format == definitions.COMPRESSION_FORMAT_ZLIB:
        compressed_data = row[first_column_index]
        serialized_data = zlib.decompress(compressed_data)
      else:
        compressed_data = b''
        serialized_data = row[first_column_index]

      if self._storage_profiler:
        self._storage_profiler.Sample(
            'read_create', 'read', container_type, len(serialized_data),
            len(compressed_data))

      container = self._DeserializeAttributeContainer(
          container_type, serialized_data)

    return container

  def _CreateAttributeContainerTable(self, container_type):
    """Creates a table for a specific attribute container type.

    Args:
      container_type (str): attribute container type.

    Raises:
      IOError: when there is an error querying the storage file or if
          an unsupported attribute container is provided.
      OSError: when there is an error querying the storage file or if
          an unsupported attribute container is provided.
    """
    column_definitions = ['_identifier INTEGER PRIMARY KEY AUTOINCREMENT']

    schema = self._GetAttributeContainerSchema(container_type)
    if schema:
      schema_to_sqlite_type_mappings = (
            self._CONTAINER_SCHEMA_TO_SQLITE_TYPE_MAPPINGS)
      for name, data_type in sorted(schema.items()):
        data_type = schema_to_sqlite_type_mappings.get(data_type, 'TEXT')
        column_definitions.append('{0:s} {1:s}'.format(name, data_type))

    else:
      if self.compression_format == definitions.COMPRESSION_FORMAT_ZLIB:
        data_column_type = 'BLOB'
      else:
        data_column_type = 'TEXT'

      column_definitions.append('_data {0:s}'.format(data_column_type))

    column_definitions = ', '.join(column_definitions)
    query = 'CREATE TABLE {0:s} ({1:s});'.format(
        container_type, column_definitions)

    try:
      self._cursor.execute(query)
    except (sqlite3.InterfaceError, sqlite3.OperationalError) as exception:
      raise IOError('Unable to query storage file with error: {0!s}'.format(
          exception))

    if container_type == self._CONTAINER_TYPE_EVENT_TAG:
      query = ('CREATE INDEX event_tag_per_event '
               'ON event_tag (_event_identifier)')
      try:
        self._cursor.execute(query)
      except (sqlite3.InterfaceError, sqlite3.OperationalError) as exception:
        raise IOError('Unable to query storage file with error: {0!s}'.format(
            exception))

  def _DeserializeAttributeContainer(self, container_type, serialized_data):
    """Deserializes an attribute container.

    Args:
      container_type (str): attribute container type.
      serialized_data (bytes): serialized attribute container data.

    Returns:
      AttributeContainer: attribute container or None.

    Raises:
      IOError: if the serialized data cannot be decoded.
      OSError: if the serialized data cannot be decoded.
    """
    if not serialized_data:
      return None

    if self._serializers_profiler:
      self._serializers_profiler.StartTiming(container_type)

    try:
      serialized_string = serialized_data.decode('utf-8')
      container = self._serializer.ReadSerialized(serialized_string)

    except UnicodeDecodeError as exception:
      raise IOError('Unable to decode serialized data: {0!s}'.format(exception))

    except (TypeError, ValueError) as exception:
      # TODO: consider re-reading attribute container with error correction.
      raise IOError('Unable to read serialized data: {0!s}'.format(exception))

    finally:
      if self._serializers_profiler:
        self._serializers_profiler.StopTiming(container_type)

    if container.CONTAINER_TYPE == self._CONTAINER_TYPE_EVENT_DATA:
      serialized_identifier = getattr(
          container, '_event_data_stream_identifier', None)
      if serialized_identifier:
        event_data_stream_identifier = (
            containers_interface.AttributeContainerIdentifier())
        event_data_stream_identifier.CopyFromString(serialized_identifier)
        container.SetEventDataStreamIdentifier(event_data_stream_identifier)

    return container

  def _ReadAndCheckStorageMetadata(self, check_readable_only=False):
    """Reads storage metadata and checks that the values are valid.

    Args:
      check_readable_only (Optional[bool]): whether the store should only be
          checked to see if it can be read. If False, the store will be checked
          to see if it can be read and written to.

    Raises:
      IOError: when there is an error querying the attribute container store.
      OSError: when there is an error querying the attribute container store.
    """
    metadata_values = self._ReadMetadata()

    self._CheckStorageMetadata(
        metadata_values, check_readable_only=check_readable_only)

    self.format_version = metadata_values['format_version']
    self.compression_format = metadata_values['compression_format']
    self.serialization_format = metadata_values['serialization_format']

  def _SerializeAttributeContainer(self, container):
    """Serializes an attribute container.

    Args:
      container (AttributeContainer): attribute container.

    Returns:
      bytes: serialized attribute container.

    Raises:
      IOError: if the attribute container cannot be serialized.
      OSError: if the attribute container cannot be serialized.
    """
    if self._serializers_profiler:
      self._serializers_profiler.StartTiming(container.CONTAINER_TYPE)

    try:
      json_dict = self._serializer.WriteSerializedDict(container)

      if container.CONTAINER_TYPE == self._CONTAINER_TYPE_EVENT_DATA:
        event_data_stream_identifier = container.GetEventDataStreamIdentifier()
        if event_data_stream_identifier:
          json_dict['_event_data_stream_identifier'] = (
              event_data_stream_identifier.CopyToString())

      serialized_string = json.dumps(json_dict)
      if not serialized_string:
        raise IOError('Unable to serialize attribute container: {0:s}.'.format(
            container.CONTAINER_TYPE))

      serialized_string = serialized_string.encode('utf-8')

    finally:
      if self._serializers_profiler:
        self._serializers_profiler.StopTiming(container.CONTAINER_TYPE)

    return serialized_string

  def _WriteExistingAttributeContainer(self, container):
    """Writes an existing attribute container to the store.

    Args:
      container (AttributeContainer): attribute container.

    Raises:
      IOError: when there is an error querying the storage file or if
          an unsupported identifier is provided.
      OSError: when there is an error querying the storage file or if
          an unsupported identifier is provided.
    """
    identifier = container.GetIdentifier()

    schema = self._GetAttributeContainerSchema(container.CONTAINER_TYPE)
    if not schema:
      raise IOError(
          'Unsupported attribute container type: {0:s}'.format(
              container.CONTAINER_TYPE))

    write_cache = self._write_cache.get(container.CONTAINER_TYPE, [])
    if len(write_cache) > 1:
      self._FlushWriteCache(container.CONTAINER_TYPE, write_cache)
      del self._write_cache[container.CONTAINER_TYPE]

    column_names = []
    values = []
    for name, data_type in sorted(schema.items()):
      attribute_value = getattr(container, name, None)
      if attribute_value is not None:
        if data_type == 'AttributeContainerIdentifier' and isinstance(
            attribute_value, containers_interface.AttributeContainerIdentifier):
          attribute_value = attribute_value.CopyToString()

        elif data_type == 'bool':
          attribute_value = int(attribute_value)

        elif data_type not in self._CONTAINER_SCHEMA_TO_SQLITE_TYPE_MAPPINGS:
          # TODO: add compression support
          attribute_value = self._serializer.WriteSerialized(attribute_value)

      column_names.append('{0:s} = ?'.format(name))
      values.append(attribute_value)

    query = 'UPDATE {0:s} SET {1:s} WHERE _identifier = {2:d}'.format(
        container.CONTAINER_TYPE, ', '.join(column_names),
        identifier.sequence_number)

    if self._storage_profiler:
      self._storage_profiler.StartTiming('write_existing')

    try:
      self._cursor.execute(query, values)

    except (sqlite3.InterfaceError, sqlite3.OperationalError) as exception:
      raise IOError('Unable to query storage file with error: {0!s}'.format(
          exception))

    finally:
      if self._storage_profiler:
        self._storage_profiler.StopTiming('write_existing')

  def _WriteMetadata(self):
    """Writes metadata.

    Raises:
      IOError: when there is an error querying the attribute container store.
      OSError: when there is an error querying the attribute container store.
    """
    try:
      self._cursor.execute(self._CREATE_METADATA_TABLE_QUERY)
    except (sqlite3.InterfaceError, sqlite3.OperationalError) as exception:
      raise IOError(
          'Unable to query attribute container store with error: {0!s}'.format(
              exception))

    self._WriteMetadataValue(
        'format_version', '{0:d}'.format(self._FORMAT_VERSION))
    self._WriteMetadataValue('compression_format', self.compression_format)
    self._WriteMetadataValue('serialization_format', self.serialization_format)

  def _WriteNewAttributeContainer(self, container):
    """Writes a new attribute container to the store.

    The table for the container type must exist.

    Args:
      container (AttributeContainer): attribute container.

    Raises:
      IOError: when there is an error querying the storage file.
      OSError: when there is an error querying the storage file.
    """
    next_sequence_number = self._GetAttributeContainerNextSequenceNumber(
        container.CONTAINER_TYPE)

    identifier = containers_interface.AttributeContainerIdentifier(
        name=container.CONTAINER_TYPE, sequence_number=next_sequence_number)
    container.SetIdentifier(identifier)

    schema = self._GetAttributeContainerSchema(container.CONTAINER_TYPE)

    if schema:
      column_names = []
      values = []
      for name, data_type in sorted(schema.items()):
        attribute_value = getattr(container, name, None)
        if attribute_value is not None:
          if data_type == 'AttributeContainerIdentifier' and isinstance(
              attribute_value,
              containers_interface.AttributeContainerIdentifier):
            attribute_value = attribute_value.CopyToString()

          elif data_type == 'bool':
            attribute_value = int(attribute_value)

          elif data_type not in self._CONTAINER_SCHEMA_TO_SQLITE_TYPE_MAPPINGS:
            # TODO: add compression support
            attribute_value = self._serializer.WriteSerialized(attribute_value)

        column_names.append(name)
        values.append(attribute_value)

    else:
      serialized_data = self._SerializeAttributeContainer(container)

      if self.compression_format == definitions.COMPRESSION_FORMAT_ZLIB:
        compressed_data = zlib.compress(serialized_data)
        serialized_data = sqlite3.Binary(compressed_data)
      else:
        compressed_data = ''

      if self._storage_profiler:
        self._storage_profiler.Sample(
            'write_new', 'write', container.CONTAINER_TYPE,
            len(serialized_data), len(compressed_data))

      column_names = ['_data']
      values = [serialized_data]

    write_cache = self._write_cache.get(
        container.CONTAINER_TYPE, [column_names])
    write_cache.append(values)

    if len(write_cache) >= self._MAXIMUM_WRITE_CACHE_SIZE:
      self._FlushWriteCache(container.CONTAINER_TYPE, write_cache)
      write_cache = [column_names]

    self._write_cache[container.CONTAINER_TYPE] = write_cache

    self._CacheAttributeContainerByIndex(container, next_sequence_number - 1)

  def GetAttributeContainerByIndex(self, container_type, index):
    """Retrieves a specific attribute container.

    Args:
      container_type (str): attribute container type.
      index (int): attribute container index.

    Returns:
      AttributeContainer: attribute container or None if not available.

    Raises:
      IOError: when the store is closed or when there is an error querying
          the storage file.
      OSError: when the store is closed or when there is an error querying
          the storage file.
    """
    container = self._GetCachedAttributeContainer(container_type, index)
    if container:
      return container

    write_cache = self._write_cache.get(container_type, [])
    if len(write_cache) > 1:
      self._FlushWriteCache(container_type, write_cache)
      del self._write_cache[container_type]

    schema = self._GetAttributeContainerSchema(container_type)

    if schema:
      column_names = sorted(schema.keys())
    else:
      column_names = ['_data']

    row_number = index + 1
    query = 'SELECT {0:s} FROM {1:s} WHERE rowid = {2:d}'.format(
        ', '.join(column_names), container_type, row_number)

    try:
      self._cursor.execute(query)
    except (sqlite3.InterfaceError, sqlite3.OperationalError) as exception:
      raise IOError('Unable to query storage file with error: {0!s}'.format(
          exception))

    if self._storage_profiler:
      self._storage_profiler.StartTiming('get_container_by_index')

    try:
      row = self._cursor.fetchone()

    finally:
      if self._storage_profiler:
        self._storage_profiler.StopTiming('get_container_by_index')

    if not row:
      return None

    container = self._CreatetAttributeContainerFromRow(
        container_type, column_names, row, 0)

    identifier = containers_interface.AttributeContainerIdentifier(
        name=container_type, sequence_number=row_number)
    container.SetIdentifier(identifier)

    self._CacheAttributeContainerByIndex(container, index)
    return container

  def GetAttributeContainers(self, container_type, filter_expression=None):
    """Retrieves a specific type of stored attribute containers.

    Args:
      container_type (str): attribute container type.
      filter_expression (Optional[str]): expression to filter the resulting
          attribute containers by.

    Returns:
      generator(AttributeContainer): attribute container generator.

    Raises:
      IOError: when there is an error querying the storage file.
      OSError: when there is an error querying the storage file.
    """
    schema = self._GetAttributeContainerSchema(container_type)

    if schema:
      column_names = sorted(schema.keys())
    else:
      column_names = ['_data']

    sql_filter_expression = None
    if filter_expression:
      expression_ast = ast.parse(filter_expression, mode='eval')
      sql_filter_expression = sqlite_store.PythonAST2SQL(expression_ast.body)

    return self._GetAttributeContainersWithFilter(
        container_type, column_names=column_names,
        filter_expression=sql_filter_expression)

  def GetSortedEvents(self, time_range=None):
    """Retrieves the events in increasing chronological order.

    Args:
      time_range (Optional[TimeRange]): time range used to filter events
          that fall in a specific period.

    Returns:
      generator(EventObject): event generator.
    """
    schema = self._GetAttributeContainerSchema(self._CONTAINER_TYPE_EVENT)
    column_names = sorted(schema.keys())

    filter_expression = None
    if time_range:
      filter_expression = []

      if time_range.start_timestamp:
        filter_expression.append('timestamp >= {0:d}'.format(
            time_range.start_timestamp))

      if time_range.end_timestamp:
        filter_expression.append('timestamp <= {0:d}'.format(
            time_range.end_timestamp))

      filter_expression = ' AND '.join(filter_expression)

    return self._GetAttributeContainersWithFilter(
        self._CONTAINER_TYPE_EVENT, column_names=column_names,
        filter_expression=filter_expression, order_by='timestamp')

  def Open(self, path=None, read_only=True, **unused_kwargs):
    """Opens the store.

    Args:
      path (Optional[str]): path to the storage file.
      read_only (Optional[bool]): True if the file should be opened in
          read-only mode.

    Raises:
      IOError: if the storage file is already opened or if the database
          cannot be connected.
      OSError: if the storage file is already opened or if the database
          cannot be connected.
      ValueError: if path is missing.
    """
    if self._is_open:
      raise IOError('Storage file already opened.')

    if not path:
      raise ValueError('Missing path.')

    path = os.path.abspath(path)

    try:
      path_uri = pathlib.Path(path).as_uri()
      if read_only:
        path_uri = '{0:s}?mode=ro'.format(path_uri)

    except ValueError:
      path_uri = None

    detect_types = sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES

    if path_uri:
      connection = sqlite3.connect(
          path_uri, detect_types=detect_types, isolation_level='DEFERRED',
          uri=True)
    else:
      connection = sqlite3.connect(
          path, detect_types=detect_types, isolation_level='DEFERRED')

    try:
      # Use in-memory journaling mode to reduce IO.
      connection.execute('PRAGMA journal_mode=MEMORY')

      # Turn off insert transaction integrity since we want to do bulk insert.
      connection.execute('PRAGMA synchronous=OFF')

    except (sqlite3.InterfaceError, sqlite3.OperationalError) as exception:
      raise IOError(
          'Unable to query attribute container store with error: {0!s}'.format(
              exception))

    cursor = connection.cursor()
    if not cursor:
      return

    self._connection = connection
    self._cursor = cursor
    self._is_open = True
    self._read_only = read_only

    if read_only:
      self._ReadAndCheckStorageMetadata(check_readable_only=True)
    else:
      if not self._HasTable('metadata'):
        self._WriteMetadata()
      else:
        self._ReadAndCheckStorageMetadata()

        # Update the storage metadata format version in case we are adding
        # new format features that are not backwards compatible.
        self._UpdateStorageMetadataFormatVersion()

      # TODO: create table on demand.
      for container_type in self._containers_manager.GetContainerTypes():
        if container_type in self._NO_CREATE_TABLE_CONTAINER_TYPES:
          continue

        if not self._HasTable(container_type):
          self._CreateAttributeContainerTable(container_type)

      self._connection.commit()

    # Initialize next_sequence_number based on the file contents so that
    # AttributeContainerIdentifier points to the correct attribute container.
    for container_type in self._REFERENCED_CONTAINER_TYPES:
      next_sequence_number = self.GetNumberOfAttributeContainers(
          container_type)
      self._SetAttributeContainerNextSequenceNumber(
          container_type, next_sequence_number)

  def SetSerializersProfiler(self, serializers_profiler):
    """Sets the serializers profiler.

    Args:
      serializers_profiler (SerializersProfiler): serializers profiler.
    """
    self._serializers_profiler = serializers_profiler

  def SetStorageProfiler(self, storage_profiler):
    """Sets the storage profiler.

    Args:
      storage_profiler (StorageProfiler): storage profiler.
    """
    self._storage_profiler = storage_profiler
