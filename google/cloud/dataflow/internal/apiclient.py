# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Dataflow client utility functions."""

import codecs
import json
import logging
import os
import re
import time

from google.cloud.dataflow import utils
from google.cloud.dataflow import version
from google.cloud.dataflow.internal.auth import get_service_credentials
from google.cloud.dataflow.internal.json_value import to_json_value
from google.cloud.dataflow.io import iobase
from google.cloud.dataflow.utils import dependency
from google.cloud.dataflow.utils import retry
from google.cloud.dataflow.utils.names import PropertyNames

from apitools.base.py import encoding
from apitools.base.py import exceptions
from apitools.clients import storage
import apitools.clients.dataflow as dataflow


BIGQUERY_API_SERVICE = 'bigquery.googleapis.com'
COMPUTE_API_SERVICE = 'compute.googleapis.com'
STORAGE_API_SERVICE = 'storage.googleapis.com'


def append_counter(status_object, counter, tentative=False):
  """Appends a counter to the status.

  Args:
    status_object: a work_item_status to which to add this counter
    counter: a counters.Counter object to append
    tentative: whether the value should be reported as tentative
  """
  logging.debug('Appending counter%s %s',
                ' (tentative)' if tentative else '',
                counter)
  append_metric(
      status_object, counter.name, counter.total,
      counter.elements if counter.aggregation_kind == counter.MEAN else None,
      tentative=tentative)


def append_metric(status_object, metric_name, value1, value2=None,
                  step=None, output_user_name=None, tentative=False,
                  worker_id=None, cumulative=True):
  """Creates and adds a MetricUpdate field to the passed-in protobuf.

  Args:
    status_object: a work_item_status to which to add this metric
    metric_name: a string naming this metric
    value1: scalar for a Sum or mean_sum for a Mean
    value2: mean_count for a Mean aggregation (do not provide for a Sum).
    step: the name of the associated step
    output_user_name: the user-visible name to use
    tentative: whether this should be labeled as a tentative metric
    worker_id: the id of this worker.  Specifying a worker_id also
      causes this to be encoded as a metric, not a counter.
    cumulative: Whether this metric is cumulative, default True.
      Set to False for a delta value.
  """
  # Does this look like a counter or like a metric?
  is_counter = not worker_id

  metric_update = dataflow.MetricUpdate()
  metric_update.name = dataflow.MetricStructuredName()
  metric_update.name.name = metric_name
  # Handle attributes stored in the name context
  if step or output_user_name or tentative or worker_id:
    metric_update.name.context = dataflow.MetricStructuredName.ContextValue()

    def append_to_context(key, value):
      metric_update.name.context.additionalProperties.append(
          dataflow.MetricStructuredName.ContextValue.AdditionalProperty(
              key=key, value=value))
    if step:
      append_to_context('step', step)
    if output_user_name:
      append_to_context('output_user_name', output_user_name)
    if tentative:
      append_to_context('tentative', 'true')
    if worker_id:
      append_to_context('workerId', worker_id)
  if cumulative and is_counter:
    metric_update.cumulative = cumulative
  if value2 is None:
    if is_counter:
      # Counters are distinguished by having a kind; metrics do not.
      metric_update.kind = 'Sum'
    metric_update.scalar = to_json_value(value1, with_type=True)
  elif value2 > 0:
    metric_update.kind = 'Mean'
    metric_update.meanSum = to_json_value(value1, with_type=True)
    metric_update.meanCount = to_json_value(value2, with_type=True)
  else:
    # A denominator of 0 will raise an error in the service.
    # What it means is we have nothing to report yet, so don't.
    pass
  logging.debug('Appending metric_update: %s', metric_update)
  status_object.metricUpdates.append(metric_update)


class Step(object):
  """Wrapper for a dataflow Step protobuf."""

  def __init__(self, step_kind, step_name):
    self.proto = dataflow.Step(kind=step_kind, name=step_name)
    self.proto.properties = {}

  def add_property(self, name, value, with_type=False):
    self.proto.properties.additionalProperties.append(
        dataflow.Step.PropertiesValue.AdditionalProperty(
            key=name, value=to_json_value(value, with_type=with_type)))

  def _get_outputs(self):
    """Returns a list of all output labels for a step."""
    outputs = []
    for p in self.proto.properties.additionalProperties:
      if p.key == PropertyNames.OUTPUT_INFO:
        for entry in p.value.array_value.entries:
          for entry_prop in entry.object_value.properties:
            if entry_prop.key == PropertyNames.OUTPUT_NAME:
              outputs.append(entry_prop.value.string_value)
    return outputs

  def get_output(self, tag=None):
    """Returns name if it is one of the outputs or first output if name is None.

    Args:
      tag: tag of the output as a string or None if we want to get the
        name of the first output.

    Returns:
      The name of the output associated with the tag or the first output
      if tag was None.

    Raises:
      ValueError: if the tag does not exist within outputs.
    """
    outputs = self._get_outputs()
    if tag is None:
      return outputs[0]
    else:
      name = '%s_%s' % (PropertyNames.OUT, tag)
      if name not in outputs:
        raise ValueError(
            'Cannot find named output: %s in %s.' % (name, outputs))
      return name


class Environment(object):
  """Wrapper for a dataflow Environment protobuf."""

  def __init__(self, packages, options, environment_version):
    self.options = options
    self.proto = dataflow.Environment()
    self.proto.clusterManagerApiService = COMPUTE_API_SERVICE
    self.proto.dataset = '%s/cloud_dataflow' % BIGQUERY_API_SERVICE
    self.proto.tempStoragePrefix = self.options.temp_location.replace(
        'gs:/', STORAGE_API_SERVICE)
    # User agent information.
    self.proto.userAgent = dataflow.Environment.UserAgentValue()
    self.local = 'localhost' in self.options.dataflow_endpoint

    version_string = version.__version__

    self.proto.userAgent.additionalProperties.extend([
        dataflow.Environment.UserAgentValue.AdditionalProperty(
            key='name',
            value=to_json_value('Google Cloud Dataflow SDK for Python')),
        dataflow.Environment.UserAgentValue.AdditionalProperty(
            key='version', value=to_json_value(version_string))])
    # Version information.
    self.proto.version = dataflow.Environment.VersionValue()
    if self.options.is_streaming:
      job_type = 'PYTHON_STREAMING'
    else:
      job_type = 'PYTHON_BATCH'
    self.proto.version.additionalProperties.extend([
        dataflow.Environment.VersionValue.AdditionalProperty(
            key='job_type',
            value=to_json_value(job_type)),
        dataflow.Environment.VersionValue.AdditionalProperty(
            key='major', value=to_json_value(environment_version))])
    # Worker pool(s) information.
    package_descriptors = []
    for package in packages:
      package_descriptors.append(
          dataflow.Package(
              location='%s/%s' % (
                  self.options.staging_location.replace(
                      'gs:/', STORAGE_API_SERVICE),
                  package),
              name=package))

    pool = dataflow.WorkerPool(
        kind='local' if self.local else 'harness',
        packages=package_descriptors,
        taskrunnerSettings=dataflow.TaskRunnerSettings(
            parallelWorkerSettings=dataflow.WorkerSettings(
                baseUrl='https://dataflow.googleapis.com',
                servicePath=self.options.dataflow_endpoint)))
    # Set worker pool options received through command line.
    if self.options.num_workers:
      pool.numWorkers = self.options.num_workers
    if self.options.machine_type:
      pool.machineType = self.options.machine_type
    if self.options.disk_size_gb:
      pool.diskSizeGb = self.options.disk_size_gb
    if self.options.disk_type:
      pool.diskType = self.options.disk_type
    if self.options.disk_source_image:
      pool.diskSourceImage = self.options.disk_source_image
    if self.options.zone:
      pool.zone = self.options.zone
    if self.options.network:
      pool.network = self.options.network
    if self.options.teardown_policy:
      if self.options.teardown_policy == 'TEARDOWN_NEVER':
        pool.teardownPolicy = (
            dataflow.WorkerPool.TeardownPolicyValueValuesEnum.TEARDOWN_NEVER)
      elif self.options.teardown_policy == 'TEARDOWN_ALWAYS':
        pool.teardownPolicy = (
            dataflow.WorkerPool.TeardownPolicyValueValuesEnum.TEARDOWN_ALWAYS)
      elif self.options.teardown_policy == 'TEARDOWN_ON_SUCCESS':
        pool.teardownPolicy = (
            dataflow.WorkerPool
            .TeardownPolicyValueValuesEnum.TEARDOWN_ON_SUCCESS)

    if self.options.is_streaming:
      # Use separate data disk for streaming.
      disk = dataflow.Disk()
      if self.local:
        disk.diskType = 'local'
      # TODO(ccy): allow customization of disk.
      pool.dataDisks.append(disk)
    self.proto.workerPools.append(pool)


class Job(object):
  """Wrapper for a dataflow Job protobuf."""

  def __str__(self):
    def encode_shortstrings(input_buffer, errors='strict'):
      """Encoder (from Unicode) that suppresses long base64 strings."""
      original_len = len(input_buffer)
      if original_len > 150:
        if self.base64_str_re.match(input_buffer):
          input_buffer = '<string of %d bytes>' % original_len
          input_buffer = input_buffer.encode('ascii', errors=errors)
        else:
          matched = self.coder_str_re.match(input_buffer)
          if matched:
            input_buffer = '%s<string of %d bytes>' % (
                matched.group(1), matched.end(2) - matched.start(2))
            input_buffer = input_buffer.encode('ascii', errors=errors)
      return input_buffer, original_len

    def decode_shortstrings(input_buffer, errors='strict'):
      """Decoder (to Unicode) that suppresses long base64 strings."""
      shortened, length = encode_shortstrings(input_buffer, errors)
      return unicode(shortened), length

    def shortstrings_registerer(encoding_name):
      if encoding_name == 'shortstrings':
        return codecs.CodecInfo(name='shortstrings',
                                encode=encode_shortstrings,
                                decode=decode_shortstrings)
      return None

    codecs.register(shortstrings_registerer)

    # Use json "dump string" method to get readable formatting;
    # further modify it to not output too-long strings, aimed at the
    # 10,000+ character hex-encoded "serialized_fn" values.
    return json.dumps(
        json.loads(encoding.MessageToJson(self.proto), encoding='shortstrings'),
        indent=2, sort_keys=True)

  def __init__(self, options):
    self.options = options
    required = ['project', 'job_name', 'staging_location', 'temp_location']
    missing = [option for option in required if not getattr(options, option)]
    if missing:
      raise ValueError(
          'Missing required configuration parameters: %s' % missing)
    # Make the staging and temp locations job name and time specific. This is
    # needed to avoid clashes between job submissions using the same staging
    # area or team members using same job names. This method is not entirely
    # foolproof since two job submissions with same name can happen at exactly
    # the same time. However the window is extremely small given that
    # time.time() has at least microseconds granularity. We add the suffix only
    # for GCS staging locations where the potential for such clashes is high.
    if self.options.staging_location.startswith('gs://'):
      path_suffix = '%s.%f' % (self.options.job_name, time.time())
      self.options.staging_location = utils.path.join(
          self.options.staging_location, path_suffix)
      self.options.temp_location = utils.path.join(
          self.options.temp_location, path_suffix)
    self.proto = dataflow.Job(name=self.options.job_name)
    if self.options.is_streaming:
      self.proto.type = dataflow.Job.TypeValueValuesEnum.JOB_TYPE_STREAMING
    else:
      self.proto.type = dataflow.Job.TypeValueValuesEnum.JOB_TYPE_BATCH
    self.base64_str_re = re.compile(r'^[A-Za-z0-9+/]*=*$')
    self.coder_str_re = re.compile(r'^([A-Za-z]+\$)([A-Za-z0-9+/]*=*)$')


class DataflowApplicationClient(object):
  """A Dataflow API client used by application code to create and query jobs."""

  def __init__(self, options, environment_version):
    """Initializes a Dataflow API client object."""
    self.options = options
    self.environment_version = environment_version
    if self.options.no_auth:
      credentials = None
    else:
      credentials = get_service_credentials()
    self._client = dataflow.DataflowV1b3(
        url=self.options.dataflow_endpoint,
        credentials=credentials,
        get_credentials=(not self.options.no_auth))
    self._storage_client = storage.StorageV1(
        url='https://www.googleapis.com/storage/v1',
        credentials=credentials,
        get_credentials=(not self.options.no_auth))

  # TODO(silviuc): Refactor so that retry logic can be applied.
  @retry.no_retries  # Using no_retries marks this as an integration point.
  def _gcs_file_copy(self, from_path, to_path):
    to_folder, to_name = os.path.split(to_path)
    with open(from_path, 'rb') as f:
      self.stage_file(to_folder, to_name, f)

  def stage_file(self, gcs_or_local_path, file_name, stream,
                 mime_type='application/octet-stream'):
    """Stages a file at a GCS or local path with stream-supplied contents."""
    if not gcs_or_local_path.startswith('gs://'):
      local_path = os.path.join(gcs_or_local_path, file_name)
      logging.info('Staging file locally to %s', local_path)
      with open(local_path, 'wb') as f:
        f.write(stream.read())
      return
    gcs_location = gcs_or_local_path + '/' + file_name
    bucket, name = gcs_location[5:].split('/', 1)

    request = storage.StorageObjectsInsertRequest(
        bucket=bucket, name=name)
    logging.info('Starting GCS upload to %s...', gcs_location)
    upload = storage.Upload(stream, mime_type)
    try:
      response = self._storage_client.objects.Insert(request, upload=upload)
    except exceptions.HttpError as e:
      reportable_errors = {
          403: 'access denied',
          404: 'bucket not found',
      }
      if e.status_code in reportable_errors:
        raise IOError(('Could not upload to GCS path %s: %s. Please verify '
                       'that credentials are valid and that you have write '
                       'access to the specified path. Stale credentials can be '
                       'refreshed by executing "gcloud auth login".') %
                      (gcs_or_local_path, reportable_errors[e.status_code]))
      raise
    logging.info('Completed GCS upload to %s', gcs_location)
    return response

  # TODO(silviuc): Refactor so that retry logic can be applied.
  @retry.no_retries  # Using no_retries marks this as an integration point.
  def create_job(self, job):
    """Submits for remote execution a job described by the workflow proto."""

    # Checks the whitelisting status of this account. This is just an early
    # courtesy check to show a warning in case of potential whitelisting errors.
    # It will not block job submission. Jobs submitted from non-whitelisted
    # projects will fail to download required files, make no progress and fail
    # eventually.
    #
    # This check will provide a false warning if a project is whitelisted but
    # not the current user. In that case job will still execute successfully
    # in the service.
    #
    # TODO(altay): Remove once the whitelisting requirements are lifted.
    try:
      request = storage.StorageObjectsListRequest(
          bucket='dataflow-python-docker')
      self._storage_client.objects.List(request)
    except exceptions.HttpError as e:
      if e.status_code == 403:
        logging.error(
            '\n*************************************************************\n'
            'This account is not whitelisted to run Python-based pipelines '
            'using the Google Cloud Dataflow service. '
            'Make sure that your project is whitelisted before submitting your '
            'job. \nPlease see documentation for getting more information on '
            'getting your project whitelisted.'
            '\n*************************************************************\n')
      else:
        logging.warning('Could not verify whitelisting status.')

    # Stage job resources and add an environment proto with their paths.
    resources = dependency.stage_job_resources(
        job.options, file_copy=self._gcs_file_copy)
    job.proto.environment = Environment(
        packages=resources, options=job.options,
        environment_version=self.environment_version).proto
    # TODO(silviuc): Remove the debug logging eventually.
    logging.info('JOB: %s', job)
    request = dataflow.DataflowProjectsJobsCreateRequest()
    request.projectId = self.options.project
    request.job = job.proto

    try:
      response = self._client.projects_jobs.Create(request)
    except exceptions.BadStatusCodeError as e:
      logging.error('HTTP status %d trying to create job'
                    ' at dataflow service endpoint %s',
                    e.response.status, self.options.dataflow_endpoint)
      logging.fatal('details of server error: %s', e)
      raise
    logging.info('Create job: %s', response)
    # The response is a Job proto with the id for the new job.
    logging.info('Created job with id: [%s]', response.id)
    return response

  @retry.with_exponential_backoff()  # Using retry defaults from utils/retry.py
  def modify_job_state(self, job_id, new_state):
    """Modify the run state of the job.

    Args:
      job_id: The id of the job.
      new_state: A string representing the new desired state. It could be set to
      either 'JOB_STATE_DONE', 'JOB_STATE_CANCELLED' or 'JOB_STATE_DRAINING'.

    Returns:
      True if the job was modified successfully.
    """
    if new_state == 'JOB_STATE_DONE':
      new_state = dataflow.Job.RequestedStateValueValuesEnum.JOB_STATE_DONE
    elif new_state == 'JOB_STATE_CANCELLED':
      new_state = dataflow.Job.RequestedStateValueValuesEnum.JOB_STATE_CANCELLED
    elif new_state == 'JOB_STATE_DRAINING':
      new_state = dataflow.Job.RequestedStateValueValuesEnum.JOB_STATE_DRAINING
    else:
      # Other states could only be set by the service.
      return False

    request = dataflow.DataflowProjectsJobsUpdateRequest()
    request.jobId = job_id
    request.projectId = self.options.project
    request.job = dataflow.Job(requestedState=new_state)

    self._client.projects_jobs.Update(request)
    return True

  @retry.with_exponential_backoff()  # Using retry defaults from utils/retry.py
  def get_job(self, job_id):
    """Gets the job status for a submitted job.

    Args:
      job_id: A string representing the job_id for the workflow as returned
        by the a create_job() request.

    Returns:
      A Job proto. See below for interesting fields.

    The Job proto returned from a get_job() request contains some interesting
    fields:
      currentState: An object representing the current state of the job. The
        string representation of the object (str() result) has the following
        possible values: JOB_STATE_UNKNONW, JOB_STATE_STOPPED,
        JOB_STATE_RUNNING, JOB_STATE_DONE, JOB_STATE_FAILED,
        JOB_STATE_CANCELLED.
      createTime: UTC time when the job was created
        (e.g. '2015-03-10T00:01:53.074Z')
      currentStateTime: UTC time for the current state of the job.
    """
    request = dataflow.DataflowProjectsJobsGetRequest()
    request.jobId = job_id
    request.projectId = self.options.project
    response = self._client.projects_jobs.Get(request)
    return response

  @retry.with_exponential_backoff()  # Using retry defaults from utils/retry.py
  def list_messages(
      self, job_id, start_time=None, end_time=None, page_token=None,
      minimum_importance=None):
    """List messages associated with the execution of a job.

    Args:
      job_id: A string representing the job_id for the workflow as returned
        by the a create_job() request.
      start_time: If specified, only messages generated after the start time
        will be returned, otherwise all messages since job started will be
        returned. The value is a string representing UTC time
        (e.g., '2015-08-18T21:03:50.644Z')
      end_time: If specified, only messages generated before the end time
        will be returned, otherwise all messages up to current time will be
        returned. The value is a string representing UTC time
        (e.g., '2015-08-18T21:03:50.644Z')
      page_token: A string to be used as next page token if the list call
        returned paginated results.
      minimum_importance: Filter for messages based on importance. The possible
        string values in increasing order of importance are: JOB_MESSAGE_DEBUG,
        JOB_MESSAGE_DETAILED, JOB_MESSAGE_BASIC, JOB_MESSAGE_WARNING,
        JOB_MESSAGE_ERROR. For example, a filter set on warning will allow only
        warnings and errors and exclude all others.

    Returns:
      A tuple consisting of a list of JobMessage instances and a
      next page token string.

    Raises:
      RuntimeError: if an unexpected value for the message_importance argument
        is used.

    The JobMessage objects returned by the call contain the following  fields:
      id: A unique string identifier for the message.
      time: A string representing the UTC time of the message
        (e.g., '2015-08-18T21:03:50.644Z')
      messageImportance: An enumeration value for the message importance. The
        value if converted to string will have the following possible values:
        JOB_MESSAGE_DEBUG, JOB_MESSAGE_DETAILED, JOB_MESSAGE_BASIC,
        JOB_MESSAGE_WARNING, JOB_MESSAGE_ERROR.
     messageText: A message string.
    """
    request = dataflow.DataflowProjectsJobsMessagesListRequest(
        jobId=job_id, projectId=self.options.project)
    if page_token is not None:
      request.pageToken = page_token
    if start_time is not None:
      request.startTime = start_time
    if end_time is not None:
      request.endTime = end_time
    if minimum_importance is not None:
      if minimum_importance == 'JOB_MESSAGE_DEBUG':
        request.minimumImportance = (
            dataflow.DataflowProjectsJobsMessagesListRequest
            .MinimumImportanceValueValuesEnum
            .JOB_MESSAGE_DEBUG)
      elif minimum_importance == 'JOB_MESSAGE_DETAILED':
        request.minimumImportance = (
            dataflow.DataflowProjectsJobsMessagesListRequest
            .MinimumImportanceValueValuesEnum
            .JOB_MESSAGE_DETAILED)
      elif minimum_importance == 'JOB_MESSAGE_BASIC':
        request.minimumImportance = (
            dataflow.DataflowProjectsJobsMessagesListRequest
            .MinimumImportanceValueValuesEnum
            .JOB_MESSAGE_BASIC)
      elif minimum_importance == 'JOB_MESSAGE_WARNING':
        request.minimumImportance = (
            dataflow.DataflowProjectsJobsMessagesListRequest
            .MinimumImportanceValueValuesEnum
            .JOB_MESSAGE_WARNING)
      elif minimum_importance == 'JOB_MESSAGE_ERROR':
        request.minimumImportance = (
            dataflow.DataflowProjectsJobsMessagesListRequest
            .MinimumImportanceValueValuesEnum
            .JOB_MESSAGE_ERROR)
      else:
        raise RuntimeError(
            'Unexpected value for minimum_importance argument: %r',
            minimum_importance)
    response = self._client.projects_jobs_messages.List(request)
    return response.jobMessages, response.nextPageToken


class DataflowWorkerClient(object):
  """A Dataflow API client used by worker code to lease work items."""

  def __init__(self, worker, skip_get_credentials=False):
    """Initializes a Dataflow API client object with worker functionality.

    Args:
      worker: A Worker instance.
      skip_get_credentials: If true disables credentials loading logic.
    """
    self._client = (
        dataflow.DataflowV1b3(
            url=worker.service_path,
            get_credentials=(not skip_get_credentials)))

  @retry.with_exponential_backoff()  # Using retry defaults from utils/retry.py
  def lease_work(self, worker):
    """Leases a work item from the service."""
    work_request = dataflow.LeaseWorkItemRequest()
    work_request.workerId = worker.worker_id
    work_request.requestedLeaseDuration = worker.desired_lease_duration()
    work_request.currentWorkerTime = worker.current_time
    work_request.workerCapabilities.append(worker.worker_id)
    for value in worker.capabilities:
      work_request.workerCapabilities.append(value)
    for value in worker.work_types:
      work_request.workItemTypes.append(value)
    request = dataflow.DataflowProjectsJobsWorkItemsLeaseRequest()
    request.jobId = worker.job_id
    request.projectId = worker.project_id
    try:
      request.leaseWorkItemRequest = work_request
    except AttributeError:
      request.lease_work_item_request = work_request
    logging.debug('lease_work: %s', request)
    response = self._client.projects_jobs_workItems.Lease(request)
    logging.debug('lease_work: %s', response)
    return response

  def report_status(self,
                    worker,
                    work_item,
                    completed,
                    progress,
                    dynamic_split_result_to_report=None,
                    exception_details=None):
    """Reports status for a work item (success or failure).

    This is an integration point. The @retry decorator is used on callers
    of this method defined in google/cloud/dataflow/worker/worker.py because
    there are different retry strategies for a completed versus in progress
    work item.

    Args:
      worker: The Worker instance executing the work item.
      work_item: The work item for which to report status.
      completed: True if there is no further work to be done on this work item
        either because it succeeded or because it failed. False if this is a
        progress report.
      progress: A SourceReaderProgress that gives the progress of worker
        handling the work item.
      dynamic_split_result_to_report: A successful dynamic split result that
        should be sent to the Dataflow service along with the status report.
      exception_details: A string representation of the stack trace for an
        exception raised while executing the work item. The string is the
        output of the standard traceback.format_exc() function.

    Returns:
      A protobuf containing the response from the service for the status
      update (WorkItemServiceState).

    Raises:
      TypeError: if progress is of an unknown type
      RuntimeError: if dynamic split request is of an unknown type.
    """
    work_item_status = dataflow.WorkItemStatus()
    work_item_status.completed = completed

    if not completed:
      work_item_status.requestedLeaseDuration = worker.desired_lease_duration()

    if progress is not None:
      work_item_progress = dataflow.ApproximateProgress()
      work_item_status.progress = work_item_progress

      if progress.position is not None:
        work_item_progress.position = (
            reader_position_to_cloud_position(progress.position))
      elif progress.percent_complete is not None:
        work_item_progress.percentComplete = progress.percent_complete
      elif progress.remaining_time is not None:
        work_item_progress.remainingTime = progress.remaining_time
      else:
        raise TypeError('Unknown type of progress')

    if dynamic_split_result_to_report is not None:
      assert isinstance(dynamic_split_result_to_report,
                        iobase.DynamicSplitResult)

      if isinstance(dynamic_split_result_to_report,
                    iobase.DynamicSplitResultWithPosition):
        work_item_status.stopPosition = (
            dynamic_split_result_with_position_to_cloud_stop_position(
                dynamic_split_result_to_report))
      else:
        raise RuntimeError('Unknown type of dynamic split result.')

    # The service keeps track of the report indexes in order to handle lost
    # and duplicate message.
    work_item_status.reportIndex = work_item.next_report_index
    work_item_status.workItemId = str(work_item.proto.id)

    # Add exception information if any.
    if exception_details is not None:
      status = dataflow.Status()
      # TODO(silviuc): Replace Code.UNKNOWN with a generated definition.
      status.code = 2
      # TODO(silviuc): Attach the stack trace as exception details.
      status.message = exception_details
      work_item_status.errors.append(status)

    # Look through the work item for metrics to send.
    for op in work_item.map_task.executed_operations:
      for counter in op.itercounters():
        append_counter(work_item_status, counter, tentative=not completed)

    report_request = dataflow.ReportWorkItemStatusRequest()
    report_request.currentWorkerTime = worker.current_time
    report_request.workerId = worker.worker_id
    report_request.workItemStatuses.append(work_item_status)

    request = dataflow.DataflowProjectsJobsWorkItemsReportStatusRequest()
    request.jobId = worker.job_id
    request.projectId = worker.project_id
    try:
      request.reportWorkItemStatusRequest = report_request
    except AttributeError:
      request.report_work_item_status_request = report_request
    logging.debug('report_status: %s', request)
    response = self._client.projects_jobs_workItems.ReportStatus(request)
    logging.debug('report_status: %s', response)
    return response

# Utility functions for translating cloud reader objects to corresponding SDK
# reader objects and vice versa.


def reader_progress_to_cloud_progress(reader_progress):
  """Converts a given 'ReaderProgress' to corresponding cloud format."""

  cloud_progress = dataflow.ApproximateProgress()
  if reader_progress.position is not None:
    cloud_progress.position = reader_position_to_cloud_position(
        reader_progress.position)
  if reader_progress.percent_complete is not None:
    cloud_progress.percentComplete = reader_progress.percent_complete
  if reader_progress.remaining_time is not None:
    cloud_progress.remainingTime = reader_progress.remaining_time

  return cloud_progress


def reader_position_to_cloud_position(reader_position):
  """Converts a given 'ReaderPosition' to corresponding cloud format."""

  cloud_position = dataflow.Position()
  if reader_position.end is not None:
    cloud_position.end = reader_position.end
  if reader_position.key is not None:
    cloud_position.key = reader_position.key
  if reader_position.byte_offset is not None:
    cloud_position.byteOffset = reader_position.byte_offset
  if reader_position.record_index is not None:
    cloud_position.recordIndex = reader_position.record_index
  if reader_position.shuffle_position is not None:
    cloud_position.shufflePosition = reader_position.shuffle_position
  if reader_position.concat_position is not None:
    concat_position = dataflow.ConcatPosition()
    concat_position.index = reader_position.concat_position.index
    concat_position.position = reader_position_to_cloud_position(
        reader_position.concat_position.position)
    cloud_position.concatPosition = concat_position

  return cloud_position


def dynamic_split_result_with_position_to_cloud_stop_position(split_result):
  """Converts a given 'DynamicSplitResultWithPosition' to cloud format."""

  return reader_position_to_cloud_position(split_result.stop_position)


def cloud_progress_to_reader_progress(cloud_progress):
  reader_position = None
  if cloud_progress.position is not None:
    reader_position = cloud_position_to_reader_position(cloud_progress.position)
  return iobase.ReaderProgress(reader_position, cloud_progress.percentComplete,
                               cloud_progress.remainingTime)


def cloud_position_to_reader_position(cloud_position):
  concat_position = None
  if cloud_position.concatPosition is not None:
    inner_position = cloud_position_to_reader_position(
        cloud_position.concatPosition.position)
    concat_position = iobase.ConcatPosition(cloud_position.index,
                                            inner_position)

  return iobase.ReaderPosition(cloud_position.end, cloud_position.key,
                               cloud_position.byteOffset,
                               cloud_position.recordIndex,
                               cloud_position.shufflePosition, concat_position)


def approximate_progress_to_dynamic_split_request(approximate_progress):
  return iobase.DynamicSplitRequest(cloud_progress_to_reader_progress(
      approximate_progress))
