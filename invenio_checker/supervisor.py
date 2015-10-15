# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2015 CERN.
#
# Invenio is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA 02111-1307, USA.
#
# In applying this license, CERN does not
# waive the privileges and immunities granted to it by virtue of its status
# as an Intergovernmental Organization or submit itself to any jurisdiction.

"""Load, construct and supervise tasks."""

import itertools as it
import signal
import sys
import time
import os

import jsonpatch
from pytest import main
from celery.exceptions import TimeoutError
from celery import chord, uuid
from six import reraise
from invenio.celery import celery
from invenio.base.helpers import with_app_context
from functools import partial
from invenio_records.api import get_record as get_record_orig

from .master import RedisMaster, StatusMaster
from .worker import RedisWorker, StatusWorker
from .redis_helpers import cleanup_failed_runs
from eliot import (
    Message,
    to_file,
    start_action,
    start_task,
    Action,
    Logger,
)
from .config import get_eliot_log_path
eliot_log_path = get_eliot_log_path()

from _pytest.main import (
    EXIT_OK,
    EXIT_TESTSFAILED,
    EXIT_INTERRUPTED,
    EXIT_INTERNALERROR,
    EXIT_USAGEERROR,
)

def _exclusive_paths(path1, path2):
    if not path1.endswith('/'):
        path1 += '/'
    if not path2.endswith('/'):
        path2 += '/'
    return path1.startswith(path2) or path2.startswith(path1)


def _touch_common_paths(paths1, paths2):
    if not paths1:
        paths1 = frozenset({'/'})
    if not paths2:
        paths2 = frozenset({'/'})
    for a, b in it.product(paths1, paths2):
        if _exclusive_paths(a, b):
            return True
    return False


def _touch_common_records(recids1, recids2):
    return recids1 & recids2


def _are_compatible(worker1, worker2):
    return not (_touch_common_records(worker1.allowed_recids, worker2.allowed_recids) and \
        _touch_common_paths(worker1.allowed_paths, worker2.allowed_paths))


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    if n == 0:
        yield []
    else:
        for i in xrange(0, len(l), n):
            yield l[i:i+n]


def rules_to_bundles(rules, all_recids):
    max_chunk_size = 1000
    max_chunks = 1
    rule_bundles = {}
    for rule in rules:
        modified_requested_recids = rule.modified_requested_recids
        chunk_size = len(modified_requested_recids)/max_chunks
        if chunk_size > max_chunk_size:
            chunk_size = max_chunk_size
        rule_bundles[rule] = chunks(modified_requested_recids, chunk_size)
    # assert len(rule_bundles) == max_chunks
    return rule_bundles


def run_task(task_name):
    from .models import CheckerRuleExecution
    from invenio.ext.sqlalchemy import db
    from datetime import datetime
    from flask_login import current_user
    from invenio_accounts.models import User

    cur_user_id = current_user.get_id()
    owner = User.query.get(cur_user_id)
    if owner is None:
        owner = User.query.filter(User.nickname=='admin').one()

    master_id = uuid()
    try:
        new_exec = CheckerRuleExecution(
            uuid=master_id,
            id_rule=task_name,
            start_date=datetime.now(),
            status=StatusMaster.booting,
            owner=owner,
        )
        db.session.add(new_exec)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    _run_task.apply_async(args=(task_name, master_id), task_id=master_id)


@celery.task()
def _run_task(rule_name, master_id):
    del Logger._destinations._destinations[:]
    to_file(open(os.path.join(eliot_log_path, master_id), "ab"))

    with start_task(action_type="invenio_checker:supervisor:_run_task",
                    master_id=master_id) as eliot_task:
        from .models import CheckerRule
        # cleanup_failed_runs()

        redis_master = None

        def cleanup_session():
            print 'Cleaning up'
            if redis_master is not None:
                redis_master.zap()

        def sigint_hook(rcv_signal, frame):
            cleanup_session()

        def except_hook(type_, value, tback):
            cleanup_session()
            reraise(type_, value, tback)

        signal.signal(signal.SIGINT, sigint_hook)
        signal.signal(signal.SIGTERM, sigint_hook)
        sys.excepthook = except_hook

        with start_action(action_type='create master'):
            eliot_task_id = eliot_task.serialize_task_id()
            redis_master = RedisMaster(master_id, eliot_task_id, rule_name)

        with start_action(action_type='create subtasks'):
            rules = CheckerRule.from_ids((rule_name,))
            bundles = rules_to_bundles(rules, redis_master.all_recids)

            subtasks = []
            errback = handle_error.s()
            for rule, rule_chunks in bundles.iteritems():
                for chunk in rule_chunks:
                    task_id = uuid()
                    redis_master.workers_append(task_id)
                    eliot_task_id = eliot_task.serialize_task_id()
                    RedisWorker(task_id, eliot_task_id, chunk)
                    subtasks.append(run_test.subtask(args=(rule.filepath,
                                                           redis_master.master_id,
                                                           task_id),
                                                     task_id=task_id,
                                                     link_error=[errback]))

            Message.log(message_type='registered subtasks', value=str(redis_master.workers))

        with start_action(action_type='run chord'):
            redis_master.status = StatusMaster.running
            header = subtasks
            callback = handle_results.subtask(link_error=[handle_errors.s(redis_master.master_id)])
            my_chord = chord(header)
            result = my_chord(callback)
            redis_master.status = StatusMaster.running


def with_eliot(action_type, master_id=None, worker_id=None):
    assert master_id or worker_id
    if worker_id:
        # print "WITH {}".format(worker_id)
        master_id = RedisWorker(worker_id).master.master_id
    master = RedisMaster(master_id)
    eliot_task_id = master.eliot_task_id
    del Logger._destinations._destinations[:]
    to_file(open(eliot_log_path + master_id, "ab"))
    with Action.continue_task(task_id=eliot_task_id):
        return start_action(action_type=action_type)


@celery.task
def handle_results(task_ids):
    """Commit patches.

    :type task_ids: list of str
    :param task_ids: values returned by `run_test` instances
    """
    with with_eliot(action_type='handle results', worker_id=task_ids[0]):
        for task_id in task_ids:
            redis_worker = RedisWorker(task_id)
            for recid, patches in redis_worker.all_patches.items():
                record = get_record_orig(recid)
                for patch in patches:
                    jsonpatch.apply_patch(record, patch, in_place=True)
                record.commit()
            redis_worker.master.rule.mark_recids_as_checked(redis_worker.bundle_requested_recids)
            redis_worker.status = StatusWorker.committed


@celery.task
def handle_errors(_, failed_master_id):
    with with_eliot('handle errors', master_id=failed_master_id):
        for redis_worker in RedisMaster(failed_master_id).redis_workers:
            redis_worker = RedisWorker(redis_worker.task_id)
            redis_worker.status = StatusWorker.failed
            print 'FAILED {}'.format(failed_master_id)


# XXX: This is called chunk-times per failed-task-id
@celery.task
def handle_error(failed_task_id):
    """
    :type res: list of retvals or list of exception strings
    """
    redis_worker = RedisWorker(failed_task_id)
    redis_worker.status = StatusWorker.failed

    # TODO (zap things)
    # RedisMaster(failed_master_id).zap()
    # failed_result = AsyncResult(id=failed_task_id)
    # failed_result.maybe_reraise()


class CustomRetry(Exception):
    def __init__(self, reason, last_run_still_valid, countdown=None):
        """
        :param countdown: retry countdown in seconds, if needs overriding
        :type countdown: int
        """
        super(CustomRetry, self).__init__(reason)
        self.countdown = countdown
        self.last_run_still_valid = last_run_still_valid


@celery.task(bind=True, max_retries=None, default_retry_delay=5)
@with_app_context()
def run_test(self, filepath, master_id, task_id, retval=None):
    redis_worker = RedisWorker(task_id)
    print 'ENTER {} of GROUP {}'.format(task_id, redis_worker.master.uuid)

    if retval is None:
        redis_worker.status = StatusWorker.booting
        # We set `-c` so that our environment does not affect the test run.
        this_file_dir = os.path.dirname(os.path.realpath(__file__))
        conftest_file = os.path.join(this_file_dir, 'conftest2.ini')
        print 'RUNNING {} of {}'.format(task_id, redis_worker.master.uuid)
        retval = main(args=['-s', '-v', '--tb=long',
                            '-p', 'invenio_checker.conftest2',
                            '-c', conftest_file,
                            '--invenio-task-id', task_id,
                            '--invenio-master-id', master_id,
                            filepath])

    # If pytest exited for reason different than a failed check, then something
    # really did break
    exit_success = (EXIT_OK,)
    exit_failure = (EXIT_INTERRUPTED,
                    EXIT_INTERNALERROR,
                    EXIT_USAGEERROR,)
    exit_expected_failure = (EXIT_TESTSFAILED,)

    if retval in exit_expected_failure:
        raise Exception("Some tests failed.")
    if retval in exit_failure:
        raise Exception("Worker failed with {}.".format(retval))

    # Always pass `exc != None` to `self.retry`, because `None` breaks cleanup
    # of workers that are in retry queue (celery/celery#2560)
    try:
        retry_after_ids = redis_worker.retry_after_ids
        if retry_after_ids:
            raise CustomRetry(
                'Waiting for conflicting checks to finish before running: {}'.\
                format(retry_after_ids), False)
        # We are looking to commit below this line
        redis_worker.status = StatusWorker.ran
        if redis_worker.a_used_dependency_of_ours_has_failed:
            redis_worker.on_others_failure()
            raise CustomRetry(
                'A used dependency of ours failed, so we want to run again',
                False)
        if not redis_worker.our_used_dependencies_have_committed:
            raise CustomRetry('Waiting for dependencies to commit', True)
    except CustomRetry as exc:
        if exc.countdown is not None:
            self.retry = partial(self.retry, countdown=exc.countdown)
        if exc.last_run_still_valid:
            self.request.kwargs.update(retval=retval)
        else:
            self.request.kwargs.pop('retval', None)
        self.retry(exc=exc, kwargs=self.request.kwargs)

    print 'RETURNING {} {}'.format(task_id, master_id)
    return task_id


@celery.task
def beat():
    from invenio.ext.sqlalchemy import db
    from .models import CheckerRule as CR
    from croniter import croniter
    from datetime import datetime
    from .redis_helpers import get_lock_manager

    lock_manager = get_lock_manager()
    my_lock = lock_manager.lock("invenio_checker:beat_lock", 30000)
    if not my_lock:
        return
    try:
        for rule in CR.query.filter(CR.schedule != None).all():
            iterator = croniter(rule.schedule, rule.last_run)
            next_run = iterator.get_next(datetime)
            now = datetime.now()
            if next_run <= now:
                run_task(rule.name)
                rule.last_run = now
                db.session.add(rule)
                db.session.commit()
    except Exception:
        db.session.rollback()
        lock_manager.unlock(my_lock)
        raise
