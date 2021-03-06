from __future__ import division, print_function, absolute_import

import argparse
import errno
import functools
import math
import os
import subprocess
import sys

import six

import perf
from perf._cli import (format_run, format_benchmark, format_checks,
                       multiline_output, display_title, format_result_value)
from perf._bench import _load_suite_from_pipe
from perf._cpu_utils import (format_cpu_list, parse_cpu_list,
                             get_isolated_cpus, set_cpu_affinity)
from perf._formatter import format_timedelta, format_number
from perf._utils import (MS_WINDOWS, popen_killer, abs_executable,
                         create_environ, create_pipe, WritePipe,
                         get_python_names, popen_communicate)
from perf._worker import WorkerProcessTask, BenchCommandTask

try:
    # Optional dependency
    import psutil
except ImportError:
    psutil = None


def strictly_positive(value):
    value = int(value)
    if value <= 0:
        raise ValueError("value must be > 0")
    return value


def positive_or_nul(value):
    if '^' in value:
        x, _, y = value.partition('^')
        x = int(x)
        y = int(y)
        value = x ** y
    else:
        value = int(value)
    if value < 0:
        raise ValueError("value must be >= 0")
    return value


def comma_separated(values):
    values = [value.strip() for value in values.split(',')]
    return list(filter(None, values))


def parse_python_names(names):
    parts = names.split(':')
    if len(parts) != 2:
        raise ValueError("syntax is REF_NAME:CHANGED_NAME")
    return parts


class Runner:
    # Default parameters are chosen to have approximatively a run of 0.5 second
    # and so a total duration of 5 seconds by default
    def __init__(self, values=None, warmups=None, processes=None,
                 loops=0, min_time=0.1, max_time=1.0, metadata=None,
                 show_name=True,
                 program_args=None, add_cmdline_args=None,
                 _argparser=None):
        has_jit = perf.python_has_jit()
        if not values:
            if has_jit:
                # Since PyPy JIT has less processes:
                # run more values per process
                values = 10
            else:
                values = 3
        if not warmups:
            if has_jit:
                # PyPy JIT needs a longer warmup (at least 1 second)
                warmups = int(math.ceil(1.0 / min_time))
            else:
                warmups = 1
        if not processes:
            if has_jit:
                # Use less processes than non-JIT, because JIT requires more
                # warmups and so each worker is slower
                processes = 6
            else:
                processes = 20

        if metadata is not None:
            self.metadata = metadata
        else:
            self.metadata = {}

        # Worker task identifier: count how many times _worker() was called,
        # see the --worker-task command line option
        self._worker_task = 0

        # Set used to check that benchmark names are unique
        self._bench_names = set()

        # result of argparser.parse_args()
        self.args = None

        # callback used to prepare command line arguments to spawn a worker
        # child process. The callback is called with prepare(runner.args, cmd).
        # args must be modified in-place.
        self._add_cmdline_args = add_cmdline_args

        # Command list arguments to call the program: (sys.argv[0],) by
        # default.
        #
        # For example, "python3 -m perf timeit" sets program_args to
        # ('-m', 'perf', 'timeit').
        if program_args:
            self._program_args = program_args
        else:
            self._program_args = (sys.argv[0],)
        self._show_name = show_name

        if _argparser is not None:
            parser = _argparser
        else:
            parser = argparse.ArgumentParser()
        parser.description = 'Benchmark'
        parser.add_argument('--rigorous', action="store_true",
                            help='Spend longer running tests '
                                 'to get more accurate results')
        parser.add_argument('--fast', action="store_true",
                            help='Get rough answers quickly')
        parser.add_argument("--debug-single-value", action="store_true",
                            help="Debug mode, only compute a single value")
        parser.add_argument('-p', '--processes',
                            type=strictly_positive, default=processes,
                            help='number of processes used to run benchmarks '
                                 '(default: %s)' % processes)
        parser.add_argument('-n', '--values', dest="values",
                            type=strictly_positive, default=values,
                            help='number of values per process (default: %s)'
                                 % values)
        parser.add_argument('-w', '--warmups', dest="warmups",
                            type=positive_or_nul, default=warmups,
                            help='number of skipped values per run used '
                                 'to warmup the benchmark (default: %s)'
                                 % warmups)
        parser.add_argument('-l', '--loops',
                            type=positive_or_nul, default=loops,
                            help='number of loops per value, 0 means '
                                 'automatic calibration (default: %s)'
                            % loops)
        parser.add_argument('-v', '--verbose', action="store_true",
                            help='enable verbose mode')
        parser.add_argument('-q', '--quiet', action="store_true",
                            help='enable quiet mode')
        parser.add_argument('--pipe', type=int, metavar="FD",
                            help='Write benchmarks encoded as JSON '
                                 'into the pipe FD')
        parser.add_argument('-o', '--output', metavar='FILENAME',
                            help='write results encoded to JSON into FILENAME')
        parser.add_argument('--append', metavar='FILENAME',
                            help='append results encoded to JSON into FILENAME')
        parser.add_argument('--min-time', type=float, default=min_time,
                            help='Minimum duration in seconds of a single '
                                 'value, used to calibrate the number of '
                                 'loops (default: %s)'
                            % format_timedelta(min_time))
        parser.add_argument('--worker', action='store_true',
                            help='Worker process, run the benchmark.')
        parser.add_argument('--worker-task', type=positive_or_nul, metavar='TASK_ID',
                            help='Identifier of the worker task: '
                                 'only execute the benchmark function TASK_ID')
        parser.add_argument('--calibrate', action="store_true",
                            help="only calibrate the benchmark, "
                                 "don't compute values")
        parser.add_argument('-d', '--dump', action="store_true",
                            help='display benchmark run results')
        parser.add_argument('--metadata', '-m', action="store_true",
                            help='show metadata')
        parser.add_argument('--hist', '-g', action="store_true",
                            help='display an histogram of values')
        parser.add_argument('--stats', '-t', action="store_true",
                            help='display statistics (min, max, ...)')
        parser.add_argument("--affinity", metavar="CPU_LIST", default=None,
                            help='Specify CPU affinity for worker processes. '
                                 'This way, benchmarks can be forced to run '
                                 'on a given set of CPUs to minimize run to '
                                 'run variation. By default, worker processes '
                                 'are pinned to isolate CPUs if isolated CPUs '
                                 'are found.')
        parser.add_argument("--inherit-environ", metavar='VARS',
                            type=comma_separated,
                            help='Comma-separated list of environment '
                                 'variables inherited by worker child '
                                 'processes.')
        parser.add_argument("--no-locale",
                            dest="locale", action="store_false", default=True,
                            help="Don't copy locale environment variables "
                                 "like LANG or LC_CTYPE.")
        parser.add_argument("--python", default=sys.executable,
                            help='Python executable '
                                 '(default: use running Python, '
                                 'sys.executable)')
        parser.add_argument("--compare-to", metavar="REF_PYTHON",
                            help='Run benchmark on the Python executable REF_PYTHON, '
                                 'run benchmark on Python executable PYTHON, '
                                 'and then compare REF_PYTHON result to PYTHON result')
        parser.add_argument("--python-names", metavar="REF_NAME:CHANGED_NAMED",
                            type=parse_python_names,
                            help='option used with --compare-to to name '
                                 'PYTHON as CHANGED_NAME '
                                 'and REF_PYTHON as REF_NAME in results')

        memory = parser.add_mutually_exclusive_group()
        memory.add_argument('--tracemalloc', action="store_true",
                            help='Trace memory allocations using tracemalloc')
        memory.add_argument('--track-memory', action="store_true",
                            help='Track memory usage using a thread')

        self.argparser = parser

    def _multiline_output(self):
        return self.args.verbose or multiline_output(self.args)

    def _process_args(self):
        args = self.args

        if args.pipe:
            args.quiet = True
            args.verbose = False
        elif args.quiet:
            args.verbose = False

        nprocess = self.argparser.get_default('processes')
        nvalues = self.argparser.get_default('values')
        if args.rigorous:
            args.processes = nprocess * 2
            # args.values = nvalues * 5 // 3
        elif args.fast:
            # use at least 3 processes to benchmark 3 different (randomized)
            # hash functions
            args.processes = max(nprocess // 2, 3)
            args.values = max(nvalues * 2 // 3, 2)
        elif args.debug_single_value:
            args.processes = 1
            args.warmups = 0
            args.values = 1
            args.loops = 1
            args.min_time = 1e-9

        if args.calibrate:
            if not args.worker:
                print("ERROR: Calibration can only be done "
                      "in a worker process")
                sys.exit(1)

            args.loops = 0
            # calibration values will be stored as warmup values
            args.warmups = 0
            args.values = 0

        filename = args.output
        if filename and os.path.exists(filename):
            print("ERROR: The JSON file %r already exists" % filename)
            sys.exit(1)

        if args.worker_task and not args.worker:
            print("ERROR: --worker-task can only be used with --worker")
            sys.exit(1)

        if args.tracemalloc:
            try:
                import tracemalloc   # noqa
            except ImportError as exc:
                print("ERROR: fail to import tracemalloc: %s" % exc)
                sys.exit(1)

        if args.track_memory:
            if MS_WINDOWS:
                from perf._win_memory import check_tracking_memory
            else:
                from perf._memory import check_tracking_memory
            err_msg = check_tracking_memory()
            if err_msg:
                print("ERROR: unable to track the memory usage "
                      "(--track-memory): %s" % err_msg)
                sys.exit(1)

        args.python = abs_executable(args.python)
        if args.compare_to:
            args.compare_to = abs_executable(args.compare_to)

        if args.compare_to:
            for option in ('output', 'append'):
                if getattr(args, option):
                    print("ERROR: --%s option is incompatible "
                          "with --compare-to option" % option)
                    sys.exit(1)

    def _set_args(self, args):
        if self.args is not None:
            raise RuntimeError("arguments already parsed")

        self.args = args
        self._process_args()

    def parse_args(self, args=None):
        if self.args is not None and args is None:
            return self.args

        args = self.argparser.parse_args(args)
        self._set_args(args)
        return args

    def _range(self):
        for warmup in six.moves.xrange(self.args.warmups):
            yield (True, 1 + warmup)
        for run in six.moves.xrange(self.args.values):
            yield (False, 1 + run)

    def _cpu_affinity(self):
        cpus = self.args.affinity
        if not cpus:
            # --affinity option is not set: detect isolated CPUs
            isolated = True
            cpus = get_isolated_cpus()
            if not cpus:
                # no isolated CPUs or unable to get the isolated CPUs
                return
        else:
            isolated = False
            cpus = parse_cpu_list(cpus)

        if set_cpu_affinity(cpus):
            if self.args.verbose:
                if isolated:
                    text = ("Pin process to isolated CPUs: %s"
                            % format_cpu_list(cpus))
                else:
                    text = ("Pin process to CPUs: %s"
                            % format_cpu_list(cpus))
                print(text)

            if isolated:
                self.args.affinity = format_cpu_list(cpus)
        else:
            if not isolated:
                print("ERROR: CPU affinity not available.", file=sys.stderr)
                print("Use Python 3.3 or newer, or install psutil dependency")
                sys.exit(1)
            elif not self.args.quiet:
                print("WARNING: unable to pin worker processes to "
                      "isolated CPUs, CPU affinity not available")
                print("Use Python 3.3 or newer, or install psutil dependency")

    def _worker(self, task):
        self._cpu_affinity()
        run = task.create_run()
        bench = perf.Benchmark((run,))
        self._display_result(bench, checks=False)
        return bench

    def _check_worker_task(self):
        args = self.parse_args()

        if args.worker_task is None:
            return True

        if args.worker_task != self._worker_task:
            # Skip the benchmark if it's not the expected worker task
            self._worker_task += 1
            return False

        return True

    def _main(self, task):
        if task.name in self._bench_names:
            raise ValueError("duplicated benchmark name: %r" % task.name)
        self._bench_names.add(task.name)

        args = self.parse_args()
        try:
            if args.worker:
                bench = self._worker(task)
            elif args.compare_to:
                self._compare_to()
                bench = None
            else:
                bench = self._master()
        except KeyboardInterrupt:
            what = "Benchmark worker" if args.worker else "Benchmark"
            print("%s interrupted: exit" % what, file=sys.stderr)
            sys.exit(1)

        self._worker_task += 1
        return bench

    @staticmethod
    def _no_keyword_argument(kwargs):
        if not kwargs:
            return

        args = ', '.join(map(repr, sorted(kwargs)))
        raise TypeError('unexpected keyword argument %s' % args)

    def bench_time_func(self, name, time_func, *args, **kwargs):
        inner_loops = kwargs.pop('inner_loops', None)
        metadata = kwargs.pop('metadata', None)
        self._no_keyword_argument(kwargs)

        if not metadata:
            metadata = {'unit': 'second'}
        elif 'unit' not in metadata:
            metadata['unit'] = 'second'

        if not self._check_worker_task():
            return None

        def task_func(task, loops):
            return time_func(loops, *args)

        task = WorkerProcessTask(self, name, task_func, metadata)
        task.inner_loops = inner_loops
        return self._main(task)

    def bench_func(self, name, func, *args, **kwargs):
        """"Benchmark func(*args)."""

        inner_loops = kwargs.pop('inner_loops', None)
        metadata = kwargs.pop('metadata', None)
        self._no_keyword_argument(kwargs)

        if not self._check_worker_task():
            return None

        if args:
            func = functools.partial(func, *args)

        def task_func(task, loops):
            # use fast local variables
            local_timer = perf.perf_counter
            local_func = func
            if loops != 1:
                range_it = range(loops)

                t0 = local_timer()
                for _ in range_it:
                    local_func()
                dt = local_timer() - t0
            else:
                t0 = local_timer()
                local_func()
                dt = local_timer() - t0

            return dt

        task = WorkerProcessTask(self, name, task_func, metadata)
        task.inner_loops = inner_loops
        return self._main(task)

    def timeit(self, name, stmt, setup="pass", inner_loops=None,
               duplicate=None, metadata=None, globals=None):

        if not self._check_worker_task():
            return None

        from perf._timeit import bench_timeit
        return bench_timeit(self, name, stmt,
                            setup=setup,
                            inner_loops=inner_loops,
                            duplicate=duplicate,
                            func_metadata=metadata,
                            globals=globals)

    def _worker_cmd(self, python, calibrate, wpipe):
        args = self.args

        cmd = [python]
        cmd.extend(self._program_args)
        cmd.extend(('--worker', '--pipe', str(wpipe),
                    '--worker-task=%s' % self._worker_task,
                    '--values', str(args.values),
                    '--warmups', str(args.warmups),
                    '--loops', str(args.loops),
                    '--min-time', str(args.min_time)))
        if calibrate:
            cmd.append('--calibrate')
        if args.verbose:
            cmd.append('-' + 'v' * args.verbose)
        if args.affinity:
            cmd.append('--affinity=%s' % args.affinity)
        if args.tracemalloc:
            cmd.append('--tracemalloc')
        if args.track_memory:
            cmd.append('--track-memory')

        if self._add_cmdline_args:
            self._add_cmdline_args(cmd, self.args)

        return cmd

    def _spawn_worker(self, python=None, calibrate=False):
        if not python:
            python = self.args.python

        env = create_environ(self.args.inherit_environ,
                             self.args.locale)

        rpipe, wpipe = create_pipe()
        with rpipe:
            with wpipe:
                warg = wpipe.to_subprocess()
                cmd = self._worker_cmd(python, calibrate, warg)

                kw = {}
                if MS_WINDOWS:
                    # Set close_fds to False to call CreateProcess() with
                    # bInheritHandles=True. For pass_handles, see
                    # http://bugs.python.org/issue19764
                    kw['close_fds'] = False
                elif sys.version_info >= (3, 2):
                    kw['pass_fds'] = [wpipe.fd]

                proc = subprocess.Popen(cmd, env=env, **kw)

            with popen_killer(proc):
                with rpipe.open_text() as rfile:
                    bench_json = rfile.read()

                exitcode = proc.wait()

        if exitcode:
            raise RuntimeError("%s failed with exit code %s"
                               % (cmd[0], exitcode))

        return _load_suite_from_pipe(bench_json)

    def _display_result(self, bench, checks=True):
        args = self.args

        # Display the average +- stdev
        if self.args.quiet:
            checks = False

        if args.pipe is not None:
            wpipe = WritePipe.from_subprocess(args.pipe)

            with wpipe.open_text() as wfile:
                try:
                    bench.dump(wfile)
                except IOError as exc:
                    if exc.errno != errno.EPIPE:
                        raise
                    # ignore broken pipe error
        else:
            lines = format_benchmark(bench,
                                     checks=checks,
                                     metadata=args.metadata,
                                     dump=args.dump,
                                     stats=args.stats,
                                     hist=args.hist,
                                     show_name=self._show_name)
            for line in lines:
                print(line)

            sys.stdout.flush()

        if args.append:
            perf.add_runs(args.append, bench)

        if args.output:
            if self._worker_task >= 1:
                perf.add_runs(args.output, bench)
            else:
                bench.dump(args.output)

    def _spawn_workers(self, python=None, newline=True):
        bench = None
        args = self.args
        verbose = args.verbose
        quiet = args.quiet
        nprocess = args.processes
        old_loops = self.args.loops
        need_calibration = (not args.loops)
        if need_calibration:
            nprocess += 1
        calibrate = need_calibration

        if verbose and self._worker_task > 0:
            print()

        for process in range(1, nprocess + 1):
            suite = self._spawn_worker(python, calibrate)
            if suite is None:
                raise RuntimeError("perf worker process didn't produce JSON result")

            benchmarks = suite.get_benchmarks()
            if len(benchmarks) != 1:
                raise ValueError("worker produced %s benchmarks instead of 1"
                                 % len(benchmarks))
            worker_bench = benchmarks[0]

            if verbose:
                run = worker_bench.get_runs()[-1]
                run_index = '%s/%s' % (process, nprocess)
                for line in format_run(worker_bench, run_index, run):
                    print(line)
            elif not quiet:
                print(".", end='')

            if calibrate:
                # Use the first worker to calibrate the benchmark. Use a worker
                # process rather than the main process because worker is a
                # little bit more isolated and so should be more reliable.
                first_run = worker_bench.get_runs()[0]
                args.loops = first_run._get_loops()
                if verbose:
                    print("Calibration: use %s loops" % format_number(args.loops))
            calibrate = False

            if bench is not None:
                bench.add_runs(worker_bench)
            else:
                bench = worker_bench

            sys.stdout.flush()

        if not quiet and newline:
            print()

        # restore the old value of loops, to recalibrate for the next
        # benchmark function if loops=0
        args.loops = old_loops

        return bench

    def _master(self):
        bench = self._spawn_workers()
        self._display_result(bench)
        return bench

    def _compare_to(self):
        from perf._compare import timeit_compare_benchs

        args = self.args
        python_ref = args.compare_to
        python_changed = args.python

        multiline = self._multiline_output()
        if args.python_names:
            name_ref, name_changed = args.python_names
        else:
            name_ref, name_changed = get_python_names(python_ref, python_changed)

        benchs = []
        for python, name in ((python_ref, name_ref), (python_changed, name_changed)):
            if multiline:
                display_title('Benchmark %s' % name)
            elif not args.quiet:
                print(name, end=': ')

            bench = self._spawn_workers(python=python, newline=False)
            benchs.append(bench)

            if multiline:
                self._display_result(bench)
            elif not args.quiet:
                print(' ' + format_result_value(bench))

            if multiline:
                print()
            elif not args.quiet:
                warnings = format_checks(bench)
                if warnings:
                    print()
                    for line in warnings:
                        print(line)
                    print()

        if multiline:
            display_title('Compare')
        elif not args.quiet:
            print()
        timeit_compare_benchs(name_ref, benchs[0], name_changed, benchs[1], args)

    def bench_command(self, name, command):
        if not self._check_worker_task():
            return None

        command_str = ' '.join(map(repr, command))
        metadata = {'command': command_str}

        path = os.path.dirname(__file__)
        script = os.path.join(path, '_process_time.py')
        run_script = [sys.executable, script]

        def task_func(task, loops):
            args = run_script + [str(loops)] + command
            proc = subprocess.Popen(args,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    universal_newlines=True)
            output = popen_communicate(proc)[0]
            if proc.returncode:
                raise Exception("Command failed with exit code %s"
                                % proc.returncode)

            rss = None
            try:
                lines = output.splitlines()
                timing = float(lines[0])
                if len(lines) >= 2:
                    rss = int(lines[1])
            except ValueError:
                raise ValueError("failed to parse script output: %r" % output)

            if rss:
                # store the maximum
                max_rss = task.metadata.get('command_max_rss', 0)
                task.metadata['command_max_rss'] = max(max_rss, rss)
            return timing

        task = BenchCommandTask(self, name, task_func, metadata)
        return self._main(task)
