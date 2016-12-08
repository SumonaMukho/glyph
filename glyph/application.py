"""Convenience classes and functions that allow you to quickly build gp apps."""

import os
import sys
import time
import dill
import numpy
import random
import logging
import argparse
import operator
from contextlib import contextmanager
import toolz
import deap
import deap.tools

from . import gp
from . import utils


class GPRunner(object):
    """Runner for gp problem sets.

    Takes care of propper initialization, execution, and accounting of a gp run
    (i.e. population creation, random state, generation count, hall of fame, and
    logbook). The method init() has to be called once before stepping through
    the evolution process with method step(); init() and step() invoke the
    assessment runner.
    """

    def __init__(self, IndividualClass, algorithm_factory, assessment_runner):
        """Init GPRunner.

        :params IndividualClass: Class inherited from gp.AExpressionTree.
        :params algorithm_factory: callable() -> gp algorithm, as defined in
                                   gp.algorithms.
        :params assessment_runner: callable(population) -> None, updates fitness
                                   values of each invalid individual in population.
        """
        self.IndividualClass = IndividualClass
        self.algorithm_factory = algorithm_factory
        self.assessment_runner = assessment_runner
        self.halloffame = []
        self.logbook = ''

    def init(self, pop_size):
        """Initialize the gp run."""
        self.halloffame = deap.tools.ParetoFront()
        self.logbook = deap.tools.Logbook()
        self.mstats = None
        self.step_count = 0
        with random_state(self):
            self.population = self.IndividualClass.create_population(pop_size)

        self.algorithm = self.algorithm_factory()
        self._update()

    def step(self):
        """Step through the evolution process."""
        with random_state(self):
            self.population = self.algorithm.evolve(self.population)
        self.step_count += 1
        self._update()

    def _update(self):
        self.assessment_runner(self.population)
        self.halloffame.update(self.population)
        if not self.mstats:
            self.mstats = create_stats(len(self.population[0].fitness.values))
        record = self.mstats.compile(self.population)
        self.logbook.record(gen=self.step_count, **record)


@contextmanager
def random_state(cls, rng=random):
    cls._tmp_state = rng.getstate()
    rng.setstate(getattr(cls, '_prev_state', rng.getstate()))
    yield
    cls._prev_state = rng.getstate()
    rng.setstate(getattr(cls, '_tmp_state', rng.getstate()))


def default_gprunner(Individual, assessment_runner, **kwargs):
    """Create a default GPRunner instance.

    For config options see MateFactory, MutateFactory, and AlgorithmFactory.
    """
    default_config = dict(mating='cxonepoint', mating_max_height=20,
                          mutation='mutuniform', mutation_max_height=20,
                          algorithm='nsga2', crossover_prob=0.5,
                          mutation_prob=0.2, tournament_size=2)
    default_config.update(kwargs)
    mate = MateFactory.create(default_config, Individual)
    mutate = MutateFactory.create(default_config, Individual)
    AlgorithmFactory.create(default_config, mate, mutate)  # A test run to check config params.
    algorithm_factory = toolz.partial(AlgorithmFactory.create, default_config, mate, mutate)
    return GPRunner(Individual, algorithm_factory, assessment_runner)


class Application(object):
    """An application based on GPRunner.

    Controls execution of the runner and adds checkpointing and logging
    functionality; also defines a set of available command line options and
    their default values.

    To create a full console application one can use the factory function
    create_console_app().
    """

    def __init__(self, config, gp_runner, checkpoint_file):
        self.args = to_argparse_namespace(config)
        self.gp_runner = gp_runner
        self.checkpoint_file = checkpoint_file
        self.pareto_fronts = []
        self.logger = logging.getLogger(__name__)
        self._initialized = False

    @property
    def assessment_runner(self):
        return self.gp_runner.assessment_runner

    def run(self, break_condition=None):
        """Run gp app.

        :param break_condition: callable(application), is called after every
                         evolutionary step.
        :return: number of iterations executed during run.
        """
        if break_condition is None:
            break_condition = lambda app: False
        iterations = 0
        if self.args.pop_size < 1:
            return iterations
        if not self._initialized:
            random.seed(self.args.seed)
            self.gp_runner.init(self.args.pop_size)
            self._update()
            iterations += 1
        while self.gp_runner.step_count < self.args.num_generations and not break_condition(self):
            self.gp_runner.step()
            self._update()
            iterations += 1
        return iterations

    def _update(self):
        self.logger.info(self.gp_runner.logbook.stream)
        self.pareto_fronts.append(self.gp_runner.halloffame[:])
        if self.valid_checkpointing and (self.gp_runner.step_count % self.args.checkpoint_frequency == 0):
            self.checkpoint()

    @property
    def valid_checkpointing(self):
        return self.checkpoint_file is not None and isinstance(self.args.checkpoint_frequency, int)

    def checkpoint(self):
        """Checkpoint current state of evolution."""
        safe(self.checkpoint_file, args=self.args, runner=self.gp_runner,
             random_state=random.getstate(), pareto_fronts=self.pareto_fronts)
        self.logger.debug('Saved checkpoint to {}'.format(self.checkpoint_file))

    @property
    def workdir(self):
        return os.path.dirname(os.path.abspath(self.checkpoint_file))

    @classmethod
    def from_checkpoint(cls, file_name):
        """Create application from checkpoint file."""
        cp = load(file_name)
        app = cls(cp['args'], cp['runner'], file_name)
        app.pareto_fronts = cp['pareto_fronts']
        app._initialized = True
        random.setstate(cp['random_state'])
        return app

    @staticmethod
    def add_options(parser):
        """Add available parser options."""
        parser.add_argument('--pop-size', '-p', dest='pop_size', metavar='n',
                            type=utils.argparse.non_negative_int, default=10,
                            help='initial population size (default: 10)')
        parser.add_argument('--num-generations', '-n', dest='num_generations', metavar='n',
                            type=utils.argparse.non_negative_int, default=10,
                            help='number of generations to evolve (default: 10)')
        parser.add_argument('--seed', dest='seed', metavar='n', type=utils.argparse.non_negative_int,
                            default=random.randint(0, sys.maxsize),
                            help='a seed for the random genrator (default: random.randint(0, sys.maxsize))')
        parser.add_argument('--checkpoint-frequency', '-f', dest='checkpoint_frequency', metavar='n',
                            type=utils.argparse.positive_int, default=1,
                            help='do checkpointing every n generations (default: 1)')


# def from_command_line(IndividualClass, assessment_runner, parser=argparse.ArgumentParser()):
def default_console_app(IndividualClass, AssessmentRunnerClass, parser=argparse.ArgumentParser()):
    """Factory function for a console application."""
    Application.add_options(parser)
    cp_group = parser.add_mutually_exclusive_group(required=False)
    cp_group.add_argument('--resume', dest='resume_file', metavar='FILE', type=str,
                          help='continue previous run from a checkpoint file')
    cp_group.add_argument('-o', dest='checkpoint_file', metavar='FILE', type=str,
                          default=os.path.join('.', 'checkpoint.pickle'),
                          help='checkpoint to FILE (default: ./checkpoint.pickle)')
    parser.add_argument('--verbose', '-v', dest='verbosity', action='count', default=0,
                        help='set verbose output; raise verbosity level with -vv, -vvv, -vvvv')
    parser.add_argument('--logging', '-l', dest='logging_config', type=str, default='logging.yaml',
                        help='set config file for logging; overides --verbose (default: logging.yaml)')
    AlgorithmFactory.add_options(parser.add_argument_group('algorithm'))
    group_breeding = parser.add_argument_group('breeding')
    MateFactory.add_options(group_breeding)
    MutateFactory.add_options(group_breeding)
    ParallelizationFactory.add_options(parser.add_argument_group('parallel execution'))
    args = parser.parse_args()

    workdir = os.path.dirname(os.path.abspath(args.checkpoint_file))
    if not os.path.exists(workdir):
        raise RuntimeError('Path does not exist: "{}"'.format(workdir))
    log_level = utils.logging.log_level(args.verbosity)
    utils.logging.load_config(config_file=args.logging_config, default_level=log_level, placeholders=dict(workdir=workdir))
    logger = logging.getLogger(__name__)

    if args.resume_file is not None:
        logger.debug('Loading checkpoint {}'.format(args.resume_file))
        app = Application.from_checkpoint(args.resume_file)
        return app, args
    else:
        mate = MateFactory.create(args, IndividualClass)
        mutate = MutateFactory.create(args, IndividualClass)
        algorithm_factory = toolz.partial(AlgorithmFactory.create, args, mate, mutate)
        parallel_factory = toolz.partial(ParallelizationFactory.create, args)
        assessment_runner = AssessmentRunnerClass(parallel_factory)
        gp_runner = GPRunner(IndividualClass, algorithm_factory, assessment_runner)
        app = Application(args, gp_runner, args.checkpoint_file)
        return app, args


class AFactory(object):

    mapping = {}

    @classmethod
    def create(cls, config, *args, **kwargs):
        config = to_argparse_namespace(config)
        return cls._create(config, *args, **kwargs)

    @staticmethod
    def add_options(parser):
        """Add available parser options."""
        raise NotImplementedError

    @classmethod
    def get_from_mapping(cls, key):
        try:
            func = cls.mapping[key]
        except KeyError:
            raise RuntimeError('Option {} not supported'.format(key))
        return func


def get_mapping(group):
    return {obj.__name__.lower(): obj for obj in group}


class AlgorithmFactory(AFactory):
    """Factory class for gp algorithms."""

    mapping = get_mapping(gp.all_algorithms)

    @classmethod
    def _create(cls, args, mate, mutate):
        """Setup gp algorithm."""
        args.algorithm = args.algorithm.lower()
        algorithm_class = cls.get_from_mapping(args.algorithm)
        algorithm = algorithm_class(mate, mutate)
        algorithm.crossover_prob = args.crossover_prob
        algorithm.mutation_prob = args.mutation_prob
        return algorithm

    @staticmethod
    def add_options(parser):
        """Add available parser options."""
        parser.add_argument('--algorithm', dest='algorithm', type=str, default='nsga2',
                            choices=list(AlgorithmFactory.mapping.keys()),
                            help='the gp algorithm (default: nsga2)')
        parser.add_argument('--cxpb', dest='crossover_prob', metavar='p', type=utils.argparse.unit_interval, default=0.5,
                            help='crossover probability for mating (default: 0.5)')
        parser.add_argument('--mutpb', dest='mutation_prob', metavar='p', type=utils.argparse.unit_interval, default=0.2,
                            help='mutation probability (default: 0.2)')
        parser.add_argument('--tournament-size', dest='tournament_size', metavar='n', type=utils.argparse.unit_interval, default=2,
                            help='tournament size for tournament selection (default: 2)')


class MateFactory(AFactory):
    """Factory class for gp mating functions."""

    mapping = get_mapping(gp.all_crossover)

    @staticmethod
    def _create(args, IndividualClass):
        """Setup mating function."""
        args.mating = args.mating.lower()
        mate = MateFactory.get_from_mapping(args.mating)(**vars(args))
        static_limit_decorator = deap.gp.staticLimit(key=operator.attrgetter("height"), max_value=args.mating_max_height)
        static_limit_decorator(mate)
        return mate

    @staticmethod
    def add_options(parser):
        """Add available parser options."""
        parser.add_argument('--mating', dest='mating', type=str, default='cxonepoint',
                            choices=list(MateFactory.mapping.keys()),
                            help='the mating method (default: cxonepoint)')
        parser.add_argument('--mating-max-height', dest='mating_max_height', metavar='n', type=utils.argparse.positive_int, default=20,
                            help='limit for the expression tree height as a result of mating (default: 20)')


class MutateFactory(AFactory):
    """Factory class for gp mutation functions."""

    mapping = get_mapping(gp.all_mutations)

    @staticmethod
    def _create(args, IndividualClass):
        """Setup mutation function."""
        args.mutation = args.mutation.lower()
        mutate = MutateFactory.get_from_mapping(args.mutation)(IndividualClass.pset, **vars(args))
        static_limit_decorator = deap.gp.staticLimit(key=operator.attrgetter("height"), max_value=args.mutation_max_height)
        mutate = static_limit_decorator(mutate)
        return mutate

    @staticmethod
    def add_options(parser):
        """Add available parser options."""
        parser.add_argument('--mutation', dest='mutation', type=str, default='mutuniform',
                            choices=list(MutateFactory.mapping.keys()),
                            help='the mutation method (default: mutuniform)')
        parser.add_argument('--mutation-max-height', dest='mutation_max_height', metavar='n', type=utils.argparse.positive_int, default=20,
                            help='limit for the expression tree height as a result of mutation (default: 20)')

        parser.add_argument('--mutate_tree_min', dest='mutate_tree_min', default=0, metavar='min_', type=utils.argparse.positive_int,
                            help="minimum value for tree based mutation methods (default: 0)")

        parser.add_argument('--mutate_tree_max', dest='mutate_tree_max', default=2, metavar='max_', type=utils.argparse.positive_int,
                            help="maximum value for tree based mutation methods (default: 2)")


class SingleProcessFactory:
    map = map


class ParallelizationFactory(AFactory):
    """Factory class for parallel execution schemes."""

    @staticmethod
    def _create(args):
        return SingleProcessFactory

    @staticmethod
    def add_options(parser):
        """Add available parser options."""
        # todo


def safe(file_name, **kwargs):
    """Dump kwargs to file."""
    with open(file_name, 'wb') as file:
        dill.dump(kwargs, file)


def load(file_name):
    """Load data saved with safe()."""
    with open(file_name, 'rb') as file:
        cp = dill.load(file)
    return cp


def create_tmp_dir(prefix='run-'):
    """Create directory with current time as signature."""
    start_date_str = time.strftime('%Y-%m-%d-%H%M%S', time.localtime())
    workdir = os.path.relpath(prefix + start_date_str)
    os.mkdir(workdir)
    return workdir


def _create_logger(verbosity, config_file, workdir):
    log_level = utils.logging.log_level(verbosity)
    utils.logging.load_config(config_file=config_file, default_level=log_level, placeholders=dict(workdir=workdir))
    return logging.getLogger(__name__)


def create_stats(n):
    """Create deap.tools.MultiStatistics object for n fitness values."""
    def val(i, ind):
        return ind.fitness.values[i]
    stats = dict()
    for i in range(n):
        stats['fit{}'.format(i)] = deap.tools.Statistics(toolz.partial(val, i))
    mstats = deap.tools.MultiStatistics(**stats)
    mstats.register("min", numpy.nanmin)
    mstats.register("max", numpy.nanmax)
    return mstats


def to_argparse_namespace(d):
    """Return argparse.Namespace object created from dictionary d."""
    if isinstance(d, argparse.Namespace):
        return d
    elif isinstance(d, dict):
        return argparse.Namespace(**d)
    else:
        raise RuntimeError('Cannot convert {} to argparse.Namespace.'.format(type(d)))