import pydoc
import traceback
from multiprocessing import Value
from multiprocessing.process import current_process
from multiprocessing.queues import Queue

from django import core
from django.apps.registry import apps
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

try:
    apps.check_apps_ready()
except core.exceptions.AppRegistryNotReady:
    import django

    django.setup()

from django_q.conf import Conf, error_reporter, logger, resource, setproctitle
from django_q.signals import post_spawn, pre_execute
from django_q.utils import close_old_django_connections, get_func_repr
from django_q.brokers import get_broker
try:
    import psutil
except ImportError:
    psutil = None

try:
    import setproctitle
except ModuleNotFoundError:
    setproctitle = None


def _check_task_timed_out(key, task: dict):
    result = None
    broker = get_broker()
    cache = broker.cache
    working_set = cache.get(key) or set()
    if task["id"] in working_set:
        # the previous worker has timedout and wasn't given chance to clear
        raise Exception(f"Task Timed-out: {task}.")
    else:
        working_set.add(task['id'])
        cache.set(key, working_set, timeout=Conf.RETRY * 3)
    return result


def _clear_task_timeout_cache(key, task):
    broker = get_broker()
    cache = broker.cache
    working_set = cache.get(key) or set()
    if task["id"] in working_set:
        working_set.remove(task['id'])
        cache.set(key, working_set, timeout=Conf.RETRY * 3)


def worker(
    task_queue: Queue, result_queue: Queue, timer: Value, timeout: int = Conf.TIMEOUT
):
    """
    Takes a task from the task queue, tries to execute it and puts the result back in
    the result queue
    :param timeout: number of seconds wait for a worker to finish.
    :type task_queue: multiprocessing.Queue
    :type result_queue: multiprocessing.Queue
    :type timer: multiprocessing.Value
    """
    proc_name = current_process().name
    logger.info(
        _("%(proc_name)s ready for work at %(id)s")
        % {"proc_name": proc_name, "id": current_process().pid}
    )
    post_spawn.send(sender="django_q", proc_name=proc_name)
    if setproctitle:
        setproctitle.setproctitle(f"qcluster {proc_name} idle")
    task_count = 0
    if timeout is None:
        timeout = -1

    working_tasks_key = "DJANGO-Q-WORKING-TASKS"
    # Start reading the task queue
    for task in iter(task_queue.get, "STOP"):
        result = None
        timer.value = -1  # Idle
        task_count += 1
        f = task["func"]

        # Log task creation and set process name
        # Get the function from the task
        func_name = get_func_repr(f)
        task_name = task["name"]
        task_desc = _("%(proc_name)s processing %(task_name)s '%(func_name)s'") % {
            "proc_name": proc_name,
            "func_name": func_name,
            "task_name": task_name,
        }
        if "group" in task:
            task_desc += f" [{task['group']}]"
        logger.info(task_desc)

        if setproctitle:
            proc_title = f"qcluster {proc_name} processing {task_name} '{func_name}'"
            if "group" in task:
                proc_title += f" [{task['group']}]"
            setproctitle.setproctitle(proc_title)

        # if it's not an instance try to get it from the string
        if not callable(f):
            # locate() returns None if f cannot be loaded
            f = pydoc.locate(f)
        close_old_django_connections()
        timer_value = task.pop("timeout", timeout)
        # signal execution
        pre_execute.send(sender="django_q", func=f, task=task)
        # execute the payload
        timer.value = timer_value  # Busy

        try:
            if f is None:
                # raise a meaningfull error if task["func"] is not a valid function
                raise ValueError(f"Function {task['func']} is not defined")
            if Conf.FAIL_ON_TIMEOUT:
                _check_task_timed_out(working_tasks_key, task)

            res = f(*task["args"], **task["kwargs"])
            result = (res, True)
        except Exception as e:
            result = (f"{e} : {traceback.format_exc()}", False)
            if error_reporter:
                error_reporter.report()
            if task.get("sync", False):
                raise

        if Conf.FAIL_ON_TIMEOUT:
            _clear_task_timeout_cache(working_tasks_key, task)

        with timer.get_lock():
            # Process result
            task["result"] = result[0]
            task["success"] = result[1]
            task["stopped"] = timezone.now()
            result_queue.put(task)
            timer.value = -1  # Idle
            if setproctitle:
                setproctitle.setproctitle(f"qcluster {proc_name} idle")
            # Recycle
            if task_count == Conf.RECYCLE or rss_check():
                timer.value = -2  # Recycled
                break
    logger.info(_("%(proc_name)s stopped doing work") % {"proc_name": proc_name})


def rss_check():
    if Conf.MAX_RSS:
        if resource:
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss >= Conf.MAX_RSS
        elif psutil:
            return psutil.Process().memory_info().rss >= Conf.MAX_RSS * 1024
    return False
