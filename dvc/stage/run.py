import logging
import os
import signal
import subprocess
import threading
from contextlib import contextmanager

from dvc.env import DVC_CHECKPOINT, DVC_ROOT
from dvc.utils import fix_env

from .decorators import relock_repo, unlocked_repo
from .exceptions import StageCmdFailedError

logger = logging.getLogger(__name__)


CHECKPOINT_SIGNAL_FILE = "DVC_CHECKPOINT"


class CheckpointKilledError(StageCmdFailedError):
    pass


def _make_cmd(executable, cmd):
    if executable is None:
        return cmd
    opts = {"zsh": ["--no-rcs"], "bash": ["--noprofile", "--norc"]}
    name = os.path.basename(executable).lower()
    return [executable] + opts.get(name, []) + ["-c", cmd]


def warn_if_fish(executable):
    if (
        executable is None
        or os.path.basename(os.path.realpath(executable)) != "fish"
    ):
        return

    logger.warning(
        "DVC detected that you are using fish as your default "
        "shell. Be aware that it might cause problems by overwriting "
        "your current environment variables with values defined "
        "in '.fishrc', which might affect your command. See "
        "https://github.com/iterative/dvc/issues/1307. "
    )


@unlocked_repo
def cmd_run(stage, *args, checkpoint_func=None, **kwargs):
    kwargs = {"cwd": stage.wdir, "env": fix_env(None), "close_fds": True}
    cmd = stage.cmd if isinstance(stage.cmd, list) else [stage.cmd]
    if checkpoint_func:
        # indicate that checkpoint cmd is being run inside DVC
        kwargs["env"].update(_checkpoint_env(stage))

    if os.name == "nt":
        kwargs["shell"] = True
        executable = None
    else:
        # NOTE: when you specify `shell=True`, `Popen` [1] will default to
        # `/bin/sh` on *nix and will add ["/bin/sh", "-c"] to your command.
        # But we actually want to run the same shell that we are running
        # from right now, which is usually determined by the `SHELL` env
        # var. So instead, we compose our command on our own, making sure
        # to include special flags to prevent shell from reading any
        # configs and modifying env, which may change the behavior or the
        # command we are running. See [2] for more info.
        #
        # [1] https://github.com/python/cpython/blob/3.7/Lib/subprocess.py
        #                                                            #L1426
        # [2] https://github.com/iterative/dvc/issues/2506
        #                                           #issuecomment-535396799
        kwargs["shell"] = False
        executable = os.getenv("SHELL") or "/bin/sh"
        warn_if_fish(executable)

    main_thread = isinstance(
        threading.current_thread(),
        threading._MainThread,  # pylint: disable=protected-access
    )
    for _cmd in cmd:
        logger.info("$ %s", _cmd)
        old_handler = None
        p = None

        try:
            p = subprocess.Popen(_make_cmd(executable, _cmd), **kwargs)
            if main_thread:
                old_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)

            killed = threading.Event()
            with checkpoint_monitor(stage, checkpoint_func, p, killed):
                p.communicate()
        finally:
            if old_handler:
                signal.signal(signal.SIGINT, old_handler)

        retcode = None if not p else p.returncode
        if retcode != 0:
            if killed.is_set():
                raise CheckpointKilledError(_cmd, retcode)
            raise StageCmdFailedError(_cmd, retcode)


def run_stage(stage, dry=False, force=False, checkpoint_func=None, **kwargs):
    if not (dry or force or checkpoint_func):
        from .cache import RunCacheNotFoundError

        try:
            stage.repo.stage_cache.restore(stage, **kwargs)
            return
        except RunCacheNotFoundError:
            pass

    callback_str = "callback " if stage.is_callback else ""
    logger.info(
        "Running %s" "stage '%s':", callback_str, stage.addressing,
    )
    if not dry:
        cmd_run(stage, checkpoint_func=checkpoint_func)


def _checkpoint_env(stage):
    return {DVC_CHECKPOINT: "1", DVC_ROOT: stage.repo.root_dir}


@contextmanager
def checkpoint_monitor(stage, callback_func, proc, killed):
    if not callback_func:
        yield None
        return

    logger.debug(
        "Monitoring checkpoint stage '%s' with cmd process '%d'",
        stage,
        proc.pid,
    )
    done = threading.Event()
    monitor_thread = threading.Thread(
        target=_checkpoint_run,
        args=(stage, callback_func, done, proc, killed),
    )

    try:
        monitor_thread.start()
        yield monitor_thread
    finally:
        done.set()
        monitor_thread.join()


def _checkpoint_run(stage, callback_func, done, proc, killed):
    """Run callback_func whenever checkpoint signal file is present."""
    signal_path = os.path.join(stage.repo.tmp_dir, CHECKPOINT_SIGNAL_FILE)
    while True:
        if os.path.exists(signal_path):
            try:
                _run_callback(stage, callback_func)
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "Error generating checkpoint, %s will be aborted", stage
                )
                _kill(proc)
                killed.set()
            finally:
                logger.debug("Remove checkpoint signal file")
                os.remove(signal_path)
        if done.wait(1):
            return


def _kill(proc):
    if os.name == "nt":
        return _kill_nt(proc)
    proc.terminate()
    proc.wait()


def _kill_nt(proc):
    # windows stages are spawned with shell=True, proc is the shell process and
    # not the actual stage process - we have to kill the entire tree
    subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)])


@relock_repo
def _run_callback(stage, callback_func):
    stage.save(allow_missing=True)
    stage.commit(allow_missing=True)
    for out in stage.outs:
        if not out.use_scm_ignore and out.is_in_repo:
            stage.repo.scm.track_file(os.fspath(out.path_info))
    stage.repo.scm.track_changed_files()
    logger.debug("Running checkpoint callback for stage '%s'", stage)
    callback_func()
