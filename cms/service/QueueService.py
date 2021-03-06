#!/usr/bin/env python2
# -*- coding: utf-8 -*-

# Contest Management System - http://cms-dev.github.io/
# Copyright © 2010-2014 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2017 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2012 Matteo Boscariol <boscarim@hotmail.com>
# Copyright © 2013-2015 Luca Wehrstedt <luca.wehrstedt@gmail.com>
# Copyright © 2013 Bernard Blackham <bernard@largestprime.net>
# Copyright © 2014 Artem Iglikov <artem.iglikov@gmail.com>
# Copyright © 2016 Luca Versari <veluca93@gmail.com>
# Copyright © 2017 Amir Keivan Mohtashami <akmohtashami97@gmail.com>
# Copyright © 2017 Kiarash Golezardi <kiarashgolezardi@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Evaluation service. It takes care of receiving submissions from the
contestants, transforming them in operations (compilation, execution,
...), queuing them with the right priority, and dispatching them to
the workers. Also, it collects the results from the workers and build
the current ranking.

"""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import logging
import json

from datetime import datetime, timedelta
from functools import wraps

import gevent.event
import gevent.lock

from cms import ServiceCoord, get_service_shards, random_service
from cms.io import Executor, TriggeredService, rpc_method
from cms.db import SessionGen, Dataset
from cms.grading.Job import JobGroup, Job
from cms.io import PriorityQueue
from cms.service import get_submissions, get_submission_results

from .esoperations import ESOperation, get_relevant_operations, \
    get_submissions_operations, get_user_tests_operations
from .workerpool import WorkerPool

logger = logging.getLogger(__name__)


def report_length(attr, name):
    def decr(method):
        def wrapper(self, *args, **kwargs):
            ret = method(self, *args, **kwargs)
            logger.metric(name, value=len(getattr(self, attr)))
            return ret
        return wrapper
    return decr


class PendingResults(object):
    """Class to hold pending results.

    A result is pending when it arrives from a worker and it has not
    yet be written. This includes results not yet sent to ES, or
    results sent but for which an acknowledgment of the write has not
    yet been received.

    """

    def __init__(self):
        # Lock for pending results and writes data structures.
        self._lock = gevent.lock.RLock()
        # Event for waking up the result dispatcher greenlet when
        # there is a new result to send.
        self._event = gevent.event.Event()
        # A map from operation to job, containing the operations for
        # which we have the result from a Worker, but haven't been sent
        # to EvaluationService for writing.
        self._results = dict()
        # A set containing the operations sent to EvaluationService
        # for writing, but not yet written.
        self._writes = set()

    def __contains__(self, operation):
        """Return true if the operation is pending

        The operation is pending either if it is still waiting to be
        sent to ES or if it was sent and waiting for confirmation that
        it was written.

        """
        with self._lock:
            return operation in self._results or operation in self._writes

    def wait(self):
        """Wait until there is a result available."""
        self._event.wait()

    def add_result(self, operation, job):
        """Add one result to the pending operations.

        operation (ESOperation): the operation performed.
        job (Job): job containing the result.

        """
        with self._lock:
            self._results[operation] = job
            self._event.set()

    def pop(self):
        """Extract one of the pending result for writing.

        return ((ESOperation, Job)): operation and results (contained in the
            job).

        raise (IndexError): if no results are available

        """
        with self._lock:
            if len(self._results) == 0:
                raise IndexError("No results available.")
            operation, job = self._results.popitem()
            if len(self._results) == 0:
                self._event.clear()
            self._writes.add(operation)
            return operation, job

    def finalize(self, operation):
        """Mark the operation as fully completed and written."""
        with self._lock:
            self._writes.remove(operation)


class EvaluationExecutor(Executor):

    # Real maximum number of operations to be sent to a worker.
    MAX_OPERATIONS_PER_BATCH = 5

    def __init__(self, evaluation_service):
        """Create the single executor for ES.

        The executor just delegates work to the worker pool.

        """
        super(EvaluationExecutor, self).__init__(True)

        self.evaluation_service = evaluation_service
        self.pool = WorkerPool(self.evaluation_service)

        # List of QueueItem (ESOperation) we have extracted from the
        # queue, but not yet finished to execute.
        self._currently_executing = []

        # Lock used to guard the currently executing operations
        self._current_execution_lock = gevent.lock.RLock()

        # Whether execute need to drop the currently executing
        # operation.
        self._drop_current = False

        for i in xrange(get_service_shards("Worker")):
            worker = ServiceCoord("Worker", i)
            self.pool.add_worker(worker)

    def __contains__(self, item):
        """Return whether the item is in execution.

        item (QueueItem): an item to search.

        return (bool): True if item is in the queue, or if it is the
            item already extracted but not given to the workers yet,
            or if it is being executed by a worker.

        """
        return super(EvaluationExecutor, self).__contains__(item) or \
            item in self._currently_executing or \
            item in self.pool

    def max_operations_per_batch(self):
        """Return the maximum number of operations per batch.

        We derive the number from the length of the queue divided by
        the number of workers, with a cap at MAX_OPERATIONS_PER_BATCH.

        """
        # TODO: len(self.pool) is the total number of workers,
        # included those that are disabled.
        ratio = len(self._operation_queue) // len(self.pool) + 1
        ret = min(max(ratio, 1), EvaluationExecutor.MAX_OPERATIONS_PER_BATCH)
        logger.info("Ratio is %d, executing %d operations together.",
                    ratio, ret)
        return ret

    def execute(self, entries):
        """Execute a batch of operations in the queue.

        The operations might not be executed immediately because of
        lack of workers.

        entries ([QueueEntry]): entries containing the operations to
            perform.

        """
        with self._current_execution_lock:
            self._currently_executing = []
            for entry in entries:
                operation = entry.item

                # Side data is attached to the operation sent to the
                # worker pool. In case the operation is lost, the pool
                # will return it to us, and we will use it to
                # re-enqueue it.
                operation.side_data = (entry.priority, entry.timestamp)
                self._currently_executing.append(operation)
        res = None
        while len(self._currently_executing) > 0:
            self.pool.wait_for_workers()
            with self._current_execution_lock:
                if len(self._currently_executing) == 0:
                    break
                res = self.pool.acquire_worker(self._currently_executing)
                if res is not None:
                    self._drop_current = False
                    self._currently_executing = []
                    break

    def enqueue(self, *args, **kwargs):
        return super(EvaluationExecutor, self).enqueue(*args, **kwargs)

    def dequeue(self, operation):
        """Remove an item from the queue.

        We need to override dequeue because the operation to dequeue
        might have already been extracted, but not yet executed.

        operation (ESOperation)

        """
        try:
            super(EvaluationExecutor, self).dequeue(operation)
        except KeyError:
            with self._current_execution_lock:
                for i in range(len(self._currently_executing)):
                    if self._currently_executing[i] == operation:
                        del self._currently_executing[i]
                        return
            raise


def with_post_finish_lock(func):
    """Decorator for locking on self.post_finish_lock.

    Ensures that no more than one decorated function is executing at
    the same time.

    """
    @wraps(func)
    def wrapped(self, *args, **kwargs):
        with self.post_finish_lock:
            return func(self, *args, **kwargs)
    return wrapped


class QueueService(TriggeredService):
    """Queue service.

    """

    # How often we check for stale workers.
    WORKER_TIMEOUT_CHECK_TIME = timedelta(seconds=300)

    # How often we check if a worker is connected.
    WORKER_CONNECTION_CHECK_TIME = timedelta(seconds=10)

    def __init__(self, shard, contest_id=None):
        super(QueueService, self).__init__(shard)

        self.contest_id = contest_id

        # This lock is used to avoid inserting in the queue (which
        # itself is already thread-safe) an operation which is already
        # being processed. Such operation might be in one of the
        # following state:
        # 1. in the queue;
        # 2. extracted from the queue by the executor, but not yet
        #    dispatched to a worker;
        # 3. being processed by a worker ("in the worker pool");
        # 4. being processed by action_finished, but with the results
        #    not yet written to the database.
        # 5. with results written in the database.
        #
        # The methods enqueuing operations already check that the
        # operation is not in state 5, and enqueue() checks that it is
        # not in the first three states.
        #
        # Therefore, the lock guarantees that the methods adding
        # operations to the queue (_missing_operations,
        # invalidate_submission, enqueue) are not executed
        # concurrently with action_finished to avoid picking
        # operations in state 4.
        self.post_finish_lock = gevent.lock.RLock()

        # Data structure holding pending results.
        self.pending = PendingResults()
        # Neverending greenlet consuming results, by sending them to ES.
        gevent.spawn(self.process_results)

        self.evaluation_services = [
            self.connect_to(ServiceCoord("EvaluationService", i))
            for i in range(get_service_shards("EvaluationService"))]

        # How many calls to the sweeper will be ignored. Used to
        # temporarily disable it in case of invalidate.
        self.avoid_next_sweepers = 0
        self.add_executor(EvaluationExecutor(self))
        self.start_sweeper(1200.0)

        self.add_timeout(self.check_workers_timeout, None,
                         QueueService.WORKER_TIMEOUT_CHECK_TIME
                         .total_seconds(),
                         immediately=False)
        self.add_timeout(self.check_workers_connection, None,
                         QueueService.WORKER_CONNECTION_CHECK_TIME
                         .total_seconds(),
                         immediately=False)

    @with_post_finish_lock
    def _missing_operations(self):
        """Look in the database for submissions that have not been compiled or
        evaluated for no good reasons. Put the missing operation in
        the queue.

        """
        # Shortcircuit the sweeper if instructed to do so by the rest of QS.
        if self.avoid_next_sweepers > 0:
            self.avoid_next_sweepers -= 1
            logger.info("Sweeper is paused, not running this and next %d.",
                        self.avoid_next_sweepers)
            return 0

        counter = 0
        with SessionGen() as session:
            for operation, timestamp, priority in \
                    get_user_tests_operations(session, self.contest_id):
                if self.enqueue(operation, timestamp, priority):
                    counter += 1

            submissions_and_datasets = \
                get_submissions_operations(session, self.contest_id)
            # Crude approximation, in case of evaluations the number
            # of operations will be much more than this.
            counter += len(submissions_and_datasets)

        submissions_by_dataset = dict()
        for submission_id, dataset_id in submissions_and_datasets:
            if dataset_id not in submissions_by_dataset:
                submissions_by_dataset[dataset_id] = set()
            submissions_by_dataset[dataset_id].add(submission_id)

        for dataset_id in submissions_by_dataset:
            submission_ids = list(submissions_by_dataset[dataset_id])
            for idx in range(0, len(submission_ids), 500):
                random_service(self.evaluation_services)\
                    .get_submissions_ops(
                        submission_ids=submission_ids[
                            idx:min(idx + 500, len(submission_ids))],
                        dataset_id=dataset_id,
                        callback=self._missing_operations_enqueue
                    )

        return counter

    def _missing_operations_enqueue(self, operations, error=None):
        for operation, timestamp, priority, job in operations:
            self.enqueue(operation, timestamp, priority, job)

    @rpc_method
    def workers_status(self):
        """Returns a dictionary (indexed by shard number) whose values
        are the information about the corresponding worker. See
        WorkerPool.get_status for more details.

        returns (dict): the dict with the workers information.

        """
        return self.get_executor().pool.get_status()

    def check_workers_timeout(self):
        """We ask WorkerPool for the unresponsive workers, and we put
        again their operations in the queue.

        """
        lost_operations = self.get_executor().pool.check_timeouts()
        for operation in lost_operations:
            logger.info("Operation %s put again in the queue because of "
                        "worker timeout.", operation)
            priority, timestamp = operation.side_data
            self.enqueue(operation, priority, timestamp)
        return True

    def check_workers_connection(self):
        """We ask WorkerPool for the unconnected workers, and we put
        again their operations in the queue.

        """
        lost_operations = self.get_executor().pool.check_connections()
        for operation in lost_operations:
            logger.info("Operation %s put again in the queue because of "
                        "disconnected worker.", operation)
            priority, timestamp = operation.side_data
            self.enqueue(operation, priority, timestamp)
        return True

    @with_post_finish_lock
    @rpc_method
    def enqueue(self, operation, priority, timestamp, job=None):
        """Push an operation in the queue.

        Push an operation in the operation queue if the submission is
        not already in the queue or assigned to a worker.

        operation (ESOperation|list): the operation to put in the queue.
        priority (int): the priority of the operation.
        timestamp (datetime|float): the time of the submission.

        return (bool): True if pushed, False if not.

        """
        if not isinstance(timestamp, datetime):
            timestamp = datetime.utcfromtimestamp(timestamp)
        if isinstance(operation, list):
            operation = ESOperation.from_list(operation)

        if operation in self.get_executor() or operation in self.pending:
            return False

        if isinstance(job, dict):
            operation.job = Job.import_from_dict_with_type(job)
        else:
            logger.info("Operation without job!")

        # enqueue() returns the number of successful pushes.
        return super(QueueService, self).enqueue(
            operation, priority, timestamp) > 0

    @with_post_finish_lock
    def action_finished(self, data, shard, error=None):
        """Callback from a worker, to signal that is finished some
        action (compilation or evaluation).

        data (dict): the JobGroup, exported to dict.
        shard (int): the shard finishing the action.

        """
        # We notify the pool that the worker is available again for
        # further work (no matter how the current request turned out,
        # even if the worker encountered an error). If the pool
        # informs us that the data produced by the worker has to be
        # ignored (by returning True) we interrupt the execution of
        # this method and do nothing because in that case we know the
        # operation has returned to the queue and perhaps already been
        # reassigned to another worker.
        to_ignore = self.get_executor().pool.release_worker(shard)
        if to_ignore is True:
            logger.info("Ignored result from worker %s as requested.", shard)
            return

        job_group = None
        job_group_success = True
        if error is not None:
            logger.error("Received error from Worker: `%s'.", error)
            job_group_success = False

        else:
            try:
                job_group = JobGroup.import_from_dict(data)
            except:
                logger.error("Couldn't build JobGroup for data %s.", data,
                             exc_info=True)
                job_group_success = False

        if job_group_success:
            for job in job_group.jobs:
                operation = ESOperation.from_dict(job.operation)
                logger.info("`%s' completed. Success: %s.",
                            operation, job.success)
                if isinstance(to_ignore, list) and operation in to_ignore:
                    logger.info("`%s' result ignored as requested", operation)
                else:
                    self.pending.add_result(operation, job)

    def process_results(self):
        """A background greenlet continuously sending results to ES."""
        while True:
            self.pending.wait()
            try:
                operation, job = self.pending.pop()
            except IndexError:
                continue

            logger.info("Sending results for operation %s to ES.", operation)
            try:
                random_service(self.evaluation_services).write_result(
                    operation=operation.to_dict(),
                    job=job.export_to_dict(),
                    callback=self.result_written,
                    plus=operation)
            except IndexError:
                logger.error("No EvaluationServices are connected, "
                             "result will be discarded")
                try:
                    self.pending.finalize(operation)
                except KeyError:
                    logger.warning("Operation written %s was not pending, "
                                   "ignoring.",
                                   operation)

    def result_written(self, success_new_operations, operation, error=None):
        success, new_operations = None, []
        if success_new_operations is not None:
            success, new_operations = success_new_operations

        logger.info("Result for operation %s written, success: %s",
                    operation, success)
        try:
            self.pending.finalize(operation)
        except KeyError:
            logger.warning("Operation written %s was not pending, ignoring.",
                           operation)
        if error is not None:
            logger.warning("Operation %s writing error (%s); re-enqueuing.",
                           operation, error)
            # The operation has been created by using from_dict.
            # We can't use side_data here so we can't access
            # priority, and timestamp of the operation.
            self.enqueue(operation, PriorityQueue.PRIORITY_LOW, datetime.now())
        else:
            for operation, priority, timestamp, job in new_operations:
                self.enqueue(
                    ESOperation.from_dict(operation), priority, timestamp, job)

    @rpc_method
    @with_post_finish_lock
    def invalidate_submission(self,
                              contest_id=None,
                              submission_id=None,
                              dataset_id=None,
                              participation_id=None,
                              task_id=None,
                              testcases=None,
                              overwrite=None,
                              force_priority=None,
                              level="compilation"):
        """Request to invalidate some computed data.

        Invalidate the compilation and/or evaluation data of the
        SubmissionResults that:
        - belong to submission_id or, if None, to any submission of
          participation_id and/or task_id or, if both None, to any
          submission of the contest asked for, or, if all three are
          None, the contest this service is running for (or all contests).
        - belong to dataset_id or, if None, to any dataset of task_id
          or, if None, to any dataset of any task of the contest this
          service is running for.

        The data is cleared, the operations involving the submissions
        currently enqueued are deleted, and the ones already assigned to
        the workers are ignored. New appropriate operations are
        enqueued.

        submission_id (int|None): id of the submission to invalidate,
            or None.
        dataset_id (int|None): id of the dataset to invalidate, or
            None.
        participation_id (int|None): id of the participation to
            invalidate, or None.
        task_id (int|None): id of the task to invalidate, or None.
        level (string): 'compilation' or 'evaluation'

        """
        logger.info("Invalidation request received.")

        # Avoid running the sweeper for the next 10-ish minutes, to
        # avoid race conditions between invalidate's requeuing of
        # submissions and the sweeper's.
        self.avoid_next_sweepers = 1

        # Validate arguments
        # TODO Check that all these objects belong to this contest.
        if level not in ("compilation", "evaluation"):
            raise ValueError(
                "Unexpected invalidation level `%s'." % level)

        if contest_id is None:
            contest_id = self.contest_id

        with SessionGen() as session:
            # First we load all involved submissions.
            if (dataset_id is not None) and (submission_id is None):
                dataset = Dataset.get_from_id(dataset_id, session)
                task_id_for_submissions = dataset.task_id
            else:
                task_id_for_submissions = task_id
            submissions = get_submissions(
                # Give contest_id only if all others are None.
                contest_id
                if {participation_id, task_id_for_submissions, submission_id} == {None}
                else None,
                participation_id, task_id_for_submissions, submission_id, session)

            # Then we get all relevant operations, and we remove them
            # both from the queue and from the pool (i.e., we ignore
            # the workers involved in those operations).
            operations = get_relevant_operations(
                level, submissions, dataset_id, testcases)
            for operation in operations:
                try:
                    self.dequeue(operation)
                except KeyError:
                    pass  # Ok, the operation wasn't in the queue.
                try:
                    self.get_executor().pool.ignore_operation(operation)
                except LookupError:
                    pass  # Ok, the operation wasn't in the pool.

            # Then we find all existing results in the database, and
            # we remove them.
            submission_results = get_submission_results(
                # Give contest_id only if all others are None.
                contest_id
                if {participation_id,
                    task_id,
                    submission_id,
                    dataset_id} == {None}
                else None,
                participation_id, task_id, submission_id, dataset_id, session)
            logger.info("Submission results to invalidate %s for: %d.",
                        level, len(submission_results))
            for submission_result in submission_results:
                # We invalidate the appropriate data and queue the
                # operations to recompute those data.
                if level == "compilation":
                    submission_result.invalidate_compilation(
                        testcases if overwrite is not False else [])
                elif level == "evaluation":
                    submission_result.invalidate_evaluation(
                        testcases if overwrite is not False else [])

            # There is a small chance that an invalidate won't
            # succeed: if a result is in the pending structure, it
            # might be written after the commit here.
            session.commit()

            # Collecting the ids so that we can close the session
            # before the rpcs.
            submission_ids = [submission.id for submission in submissions]

        # Finally, we re-enqueue the operations for the submissions.
        for idx in range(0, len(submission_ids), 500):
            random_service(self.evaluation_services).new_submissions(
                submission_ids=submission_ids[idx:min(idx + 500, len(submission_ids))],
                dataset_id=dataset_id,
                force_priority=force_priority
            )

        logger.info("Invalidate successfully completed.")

    @rpc_method
    def disable_worker(self, shard):
        """Disable a specific worker (recovering its assigned operations).

        shard (int): the shard of the worker.

        returns (bool): True if everything went well.

        """
        logger.info("Received request to disable worker %s.", shard)

        lost_operations = []
        try:
            lost_operations = self.get_executor().pool.disable_worker(shard)
        except ValueError:
            return False

        for operation in lost_operations:
            logger.info("Operation %s put again in the queue because "
                        "the worker was disabled.", operation)
            priority, timestamp = operation.side_data
            self.enqueue(operation, priority, timestamp)
        return True

    @rpc_method
    def enable_worker(self, shard):
        """Enable a specific worker.

        shard (int): the shard of the worker.

        returns (bool): True if everything went well.

        """
        logger.info("Received request to enable worker %s.", shard)
        try:
            self.get_executor().pool.enable_worker(shard)
        except ValueError:
            return False

        return True

    @rpc_method
    def queue_status(self):
        """Return the status of the queue.

        Parent method returns list of queues of each executor, but in
        QueueService we have only one executor, so we can just take
        the first queue.

        As evaluate operations are split by testcases, there are too
        many entries in the queue to display, so we just take only one
        operation of each (type, object_id, dataset_id)
        tuple. Generally, we will see only one evaluate operation for
        each submission in the queue status with the number of
        testcase which will be evaluated next. Moreover, we pass also
        the number of testcases in the queue.

        The entries are then ordered by priority and timestamp (the
        same criteria used to look at what to complete next).

        return ([QueueEntry]): the list with the queued elements.

        """
        entries = super(QueueService, self).queue_status()[0]
        entries_by_key = dict()
        for entry in entries:
            key = (str(entry["item"]["type"]),
                   str(entry["item"]["object_id"]),
                   str(entry["item"]["dataset_id"]))
            if key in entries_by_key:
                entries_by_key[key]["item"]["multiplicity"] += 1
            else:
                entries_by_key[key] = entry
                entries_by_key[key]["item"]["multiplicity"] = 1
        return sorted(
            entries_by_key.values(),
            lambda x, y: cmp((x["priority"], x["timestamp"]),
                             (y["priority"], y["timestamp"])))
